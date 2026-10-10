use rustc_hash::{FxHashMap, FxHashSet};

use crate::model::{LogCluster, Token};

/// Identifier for a [`LogCluster`] within a [`ParseState`].
pub type ClusterId = usize;

/// Upper bound for the exact-template index. Oversized indexes are cleared
/// wholesale (they are pure acceleration, rebuilt lazily); mirrors the
/// upstream Python `_CACHE_LIMIT` policy.
const EXACT_INDEX_LIMIT: usize = 1 << 16;

/// Key used to partition clusters into constant buckets.
///
/// `ConstantKey::None` is the fallback bucket used when no explicit constant
/// token matches. `ConstantKey::Token(s)` groups clusters whose template
/// contains the constant string `s`.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum ConstantKey {
    /// No explicit constant matched.
    None,
    /// A specific constant token used for bucket assignment.
    Token(String),
}

/// A bucket of clusters sharing a common [`ConstantKey`].
///
/// Buckets store the full list of cluster ids and a secondary index by
/// template length to accelerate matching.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ConstantBucket {
    /// All cluster ids in this bucket.
    pub cluster_ids: Vec<ClusterId>,
    /// Cluster ids grouped by template length.
    pub by_len: FxHashMap<usize, Vec<ClusterId>>,
}

/// In-memory state of the online parser.
///
/// `ParseState` owns all discovered clusters and the constant buckets used to
/// route incoming token sequences to candidate clusters.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParseState {
    /// All clusters discovered so far.
    pub clusters: Vec<LogCluster>,
    /// Buckets indexed by constant key.
    pub buckets: FxHashMap<ConstantKey, ConstantBucket>,
    /// Order in which constant keys were first seen; used for bucket selection.
    pub constant_order: Vec<ConstantKey>,
    /// Derived index: ASCII-lowercased constant token → position in
    /// `constant_order`. Lets bucket routing answer in O(tokens) hash
    /// lookups instead of scanning every registered key per line.
    /// Rebuilt by [`ParseState::rebuild_derived_indexes`].
    pub(crate) constant_lookup: FxHashMap<String, u32>,
    /// Derived index: allocation-free exact-template hash (ASCII-lowercased
    /// rendered bytes) → cluster id. Gives repeated identical lines an O(1)
    /// path that bypasses bucket scanning entirely. Hash entries are
    /// verified on hit (`matches_exactly`), so collisions and stale entries
    /// are safe — they fail verification and the normal search path runs.
    pub(crate) exact_index: FxHashMap<u64, ClusterId>,
    /// TIG-PIPLUP level-1 cache: digit-normalized raw-content CRC32 →
    /// candidate clusters. Candidates are verified against current templates.
    pub(crate) light_cache: FxHashMap<u32, Vec<ClusterId>>,
    /// TIG-PIPLUP level-2 cache: preprocessed token-sequence CRC32 → cluster.
    pub(crate) template_cache: FxHashMap<u32, ClusterId>,
}

impl ParseState {
    /// Create an empty parse state with a single `None` bucket.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::ParseState;
    ///
    /// let state = ParseState::new();
    /// assert!(state.clusters.is_empty());
    /// ```
    pub fn new() -> Self {
        let mut buckets = FxHashMap::default();
        buckets.insert(ConstantKey::None, ConstantBucket::default());
        Self {
            clusters: Vec::new(),
            buckets,
            constant_order: vec![ConstantKey::None],
            constant_lookup: FxHashMap::default(),
            exact_index: FxHashMap::default(),
            light_cache: FxHashMap::default(),
            template_cache: FxHashMap::default(),
        }
    }

    /// Insert `key` into the bucket map if it does not already exist.
    pub fn ensure_bucket(&mut self, key: ConstantKey) -> &mut ConstantBucket {
        let order_position = self.constant_order.len() as u32;
        self.buckets.entry(key.clone()).or_insert_with(|| {
            self.constant_order.push(key.clone());
            if let ConstantKey::Token(constant) = &key {
                // First registration wins: `or_insert` keeps the earliest
                // order position for case-variant spellings of the same
                // constant, matching the first-match-in-order semantics of
                // the previous full scan.
                self.constant_lookup
                    .entry(constant.clone())
                    .or_insert(order_position);
            }
            ConstantBucket::default()
        })
    }

    /// Rebuild the derived indexes ([`Self::constant_lookup`],
    /// [`Self::exact_index`]) from the authoritative structures. Call after
    /// restoring a `ParseState` from a cache or index payload.
    pub fn rebuild_derived_indexes(&mut self) {
        self.constant_lookup = FxHashMap::default();
        for (position, key) in self.constant_order.iter().enumerate() {
            if let ConstantKey::Token(constant) = key {
                self.constant_lookup
                    .entry(constant.clone())
                    .or_insert(position as u32);
            }
        }
        self.exact_index = FxHashMap::default();
        let exact_hashes: Vec<(u64, ClusterId)> = self
            .clusters
            .iter()
            .enumerate()
            .map(|(cluster_id, cluster)| (exact_template_hash(&cluster.log_template), cluster_id))
            .collect();
        for (hash, cluster_id) in exact_hashes {
            self.insert_exact_entry(hash, cluster_id);
        }
    }

    fn insert_exact_entry(&mut self, hash: u64, cluster_id: ClusterId) {
        if self.exact_index.len() >= EXACT_INDEX_LIMIT {
            self.exact_index.clear();
        }
        self.exact_index.entry(hash).or_insert(cluster_id);
    }

    /// O(1) exact-template lookup for a tokenized line.
    ///
    /// Returns the cluster id whose canonical template renders equal to
    /// `seq` case-sensitively, or `None`. Hash entries are verified
    /// with [`LogCluster::matches_exactly`]; stale entries whose cluster
    /// template has since evolved are repaired in place, and the caller
    /// falls back to the normal search.
    pub(crate) fn exact_lookup(&mut self, seq: &[Token]) -> Option<ClusterId> {
        if self.exact_index.is_empty() {
            return None;
        }
        let hash = exact_template_hash(seq);
        if let Some(&cluster_id) = self.exact_index.get(&hash) {
            if self.clusters[cluster_id].matches_exactly(seq) {
                return Some(cluster_id);
            }
            // Stale: the registered cluster's template evolved away.
            // Repair so future identical lines hit the current form.
            self.exact_index.remove(&hash);
            let current = exact_template_hash(&self.clusters[cluster_id].log_template);
            self.insert_exact_entry(current, cluster_id);
        }
        None
    }

    /// Return a reference to the bucket for `key`, if any.
    pub fn bucket(&self, key: &ConstantKey) -> Option<&ConstantBucket> {
        self.buckets.get(key)
    }

    /// Choose the most specific constant key whose constant appears in `tokens`.
    ///
    /// Semantics identical to the previous full scan over `constant_order`
    /// (first key, in registration order, that any token matches
    /// case-sensitively) but answered with O(tokens) hash lookups through
    /// the derived [`Self::constant_lookup`] index.
    pub fn select_constant_for_tokens(&self, tokens: &[Token]) -> ConstantKey {
        if self.constant_lookup.is_empty() {
            return ConstantKey::None;
        }
        let mut best: Option<(u32, &ConstantKey)> = None;
        for token in tokens {
            let rendered = token.render();
            if let Some(&position) = self.constant_lookup.get(rendered.as_ref())
                && best.is_none_or(|(best_position, _)| position < best_position)
            {
                best = Some((position, &self.constant_order[position as usize]));
            }
        }
        match best {
            Some((_, key)) => key.clone(),
            None => ConstantKey::None,
        }
    }

    /// Add a cluster to the state and return its assigned id and bucket key.
    pub fn add_cluster(&mut self, cluster: LogCluster) -> (ClusterId, ConstantKey) {
        let constant = self.select_constant_for_tokens(&cluster.log_template);
        let seq_len = cluster.log_template.len();
        let cluster_id = self.clusters.len();
        let exact_hash = exact_template_hash(&cluster.log_template);
        self.clusters.push(cluster);

        let bucket = self.ensure_bucket(constant.clone());
        bucket.cluster_ids.push(cluster_id);
        bucket.by_len.entry(seq_len).or_default().push(cluster_id);
        self.insert_exact_entry(exact_hash, cluster_id);

        (cluster_id, constant)
    }

    /// Borrow a cluster by id.
    pub fn cluster(&self, cluster_id: ClusterId) -> &LogCluster {
        &self.clusters[cluster_id]
    }

    /// Mutably borrow a cluster by id.
    pub fn cluster_mut(&mut self, cluster_id: ClusterId) -> &mut LogCluster {
        &mut self.clusters[cluster_id]
    }

    /// Move clusters containing `constant` from the `None` bucket to a new
    /// dedicated bucket.
    ///
    /// Returns the key of the target bucket.
    pub fn promote_none_bucket(&mut self, constant: &str) -> ConstantKey {
        if constant == "None" {
            return ConstantKey::None;
        }

        let target_key = ConstantKey::Token(constant.to_string());
        self.ensure_bucket(target_key.clone());

        let matching_cluster_ids = self
            .bucket(&ConstantKey::None)
            .map(|bucket| {
                bucket
                    .cluster_ids
                    .iter()
                    .copied()
                    .filter(|cluster_id| {
                        self.cluster(*cluster_id)
                            .log_template
                            .iter()
                            .any(|token| token.matches_rendered(constant))
                    })
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();

        if matching_cluster_ids.is_empty() {
            return target_key;
        }

        let matching_set: FxHashSet<ClusterId> = matching_cluster_ids.iter().copied().collect();

        {
            let none_bucket = self.ensure_bucket(ConstantKey::None);
            none_bucket
                .cluster_ids
                .retain(|cluster_id| !matching_set.contains(cluster_id));
            for cluster_ids in none_bucket.by_len.values_mut() {
                cluster_ids.retain(|cluster_id| !matching_set.contains(cluster_id));
            }
        }

        for cluster_id in &matching_cluster_ids {
            let seq_len = self.cluster(*cluster_id).log_template.len();
            let target_bucket = self.ensure_bucket(target_key.clone());
            if !target_bucket.cluster_ids.contains(cluster_id) {
                target_bucket.cluster_ids.push(*cluster_id);
            }
            let by_len = target_bucket.by_len.entry(seq_len).or_default();
            if !by_len.contains(cluster_id) {
                by_len.push(*cluster_id);
            }
        }

        target_key
    }
}

impl Default for ParseState {
    fn default() -> Self {
        Self::new()
    }
}

/// Allocation-free exact-template hash for a token sequence: the rendered
/// bytes of every token, fed through `FxHasher`. Two
/// sequences produce the same hash whenever [`LogCluster::matches_exactly`]
/// would accept them; the converse is only probable, not guaranteed, which
/// is why hits are verified.
pub(crate) fn exact_template_hash(tokens: &[Token]) -> u64 {
    use rustc_hash::FxHasher;
    use std::hash::Hasher;

    let mut hasher = FxHasher::default();
    for token in tokens {
        match token {
            Token::Literal(value) => hasher.write(value.as_bytes()),
            Token::Wildcard { head, tail } => {
                hasher.write(head.as_bytes());
                hasher.write(b"<*>");
                hasher.write(tail.as_bytes());
            }
        }
    }
    hasher.finish()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tokens(items: &[&str]) -> Vec<Token> {
        items.iter().copied().map(Token::literal).collect()
    }

    #[test]
    fn exact_lookup_hits_identical_template() {
        let mut state = ParseState::new();
        let _ = state.add_cluster(LogCluster::new(tokens(&["INFO", "node", "456"]), 1, 2));

        let hit = state.exact_lookup(&tokens(&["INFO", "node", "456"]));
        assert_eq!(hit, Some(0));

        // Different tokens must not hit.
        assert_eq!(state.exact_lookup(&tokens(&["INFO", "node", "789"])), None);
    }

    #[test]
    fn exact_lookup_is_case_sensitive_like_python() {
        let mut state = ParseState::new();
        let _ = state.add_cluster(LogCluster::new(tokens(&["info", "node"]), 1, 2));

        assert_eq!(state.exact_lookup(&tokens(&["INFO", "NODE"])), None);
    }

    #[test]
    fn exact_lookup_repairs_stale_entries() {
        let mut state = ParseState::new();
        let _ = state.add_cluster(LogCluster::new(tokens(&["a", "1"]), 1, 2));
        assert_eq!(state.exact_lookup(&tokens(&["a", "1"])), Some(0));

        // Template evolves: the old key becomes stale.
        state.cluster_mut(0).log_template = tokens(&["a", "2"]);
        assert_eq!(state.exact_lookup(&tokens(&["a", "1"])), None);

        // Repair registered the current form.
        assert_eq!(state.exact_lookup(&tokens(&["a", "2"])), Some(0));
    }

    #[test]
    fn select_constant_prefers_earliest_registered_key() {
        let mut state = ParseState::new();
        // Constants enter the order through bucket creation (promotion path).
        state.ensure_bucket(ConstantKey::Token("worker".into()));
        state.ensure_bucket(ConstantKey::Token("server".into()));

        // Both constants present: earliest registration wins, like the
        // previous full scan over constant_order.
        let key = state.select_constant_for_tokens(&tokens(&["server", "worker"]));
        assert_eq!(key, ConstantKey::Token("worker".into()));

        // Python bucket routing is case-sensitive.
        let key = state.select_constant_for_tokens(&tokens(&["WORKER"]));
        assert_eq!(key, ConstantKey::None);

        // Wildcard-rendered constants match their exact rendering only.
        state.ensure_bucket(ConstantKey::Token("user=<*>".into()));
        let key = state.select_constant_for_tokens(&[Token::wildcard("user=", "")]);
        assert_eq!(key, ConstantKey::Token("user=<*>".into()));
    }

    #[test]
    fn rebuild_derived_indexes_restores_lookups() {
        let mut state = ParseState::new();
        let _ = state.add_cluster(LogCluster::new(tokens(&["INFO", "node", "456"]), 1, 2));
        let _ = state.promote_none_bucket("INFO");

        // Simulate a cache round trip wiping the derived structures.
        let mut restored = ParseState {
            clusters: state.clusters.clone(),
            buckets: state.buckets.clone(),
            constant_order: state.constant_order.clone(),
            constant_lookup: FxHashMap::default(),
            exact_index: FxHashMap::default(),
            light_cache: FxHashMap::default(),
            template_cache: FxHashMap::default(),
        };
        assert_eq!(
            restored.exact_lookup(&tokens(&["INFO", "node", "456"])),
            None
        );
        assert_eq!(
            restored.select_constant_for_tokens(&tokens(&["INFO", "x"])),
            ConstantKey::None
        );
        restored.rebuild_derived_indexes();
        assert_eq!(
            restored.exact_lookup(&tokens(&["INFO", "node", "456"])),
            Some(0)
        );
        assert_eq!(
            restored.select_constant_for_tokens(&tokens(&["INFO", "x"])),
            ConstantKey::Token("INFO".into())
        );
    }
}

//! Drain3 algorithm state: routing keys, clusters, fixed-depth tree, and state.
//!
//! The tree routes first by token count and then by up to `depth - 3` content
//! tokens. Each cluster stores the exact route path chosen at creation time,
//! after numeric wildcarding and `max_children` fallback have been applied.

use std::collections::BTreeMap;

use crate::drain3::minhash;
use crate::drain3::token;
use crate::model::{
    Token,
    token::{WILDCARD_MARKER, render_token_list, render_token_list_joined},
};
use crate::placeholder::{
    PlaceholderKind, PlaceholderMask, classify_observed_kind, maintain_mask_for_template,
    typed_slot_matches_kind,
};

const WILDCARD: &str = WILDCARD_MARKER;

/// ADR-0033 改良③ minhash configuration, carried on [`DrainState`] so `search`
/// can run the variable-length fallback without per-call parameters.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct MinhashConfig {
    /// Whether the variable-length minhash fallback is enabled.
    pub enabled: bool,
    /// Token-count neighborhood radius: search clusters whose template length
    /// falls in `[L - neighborhood, L + neighborhood]`.
    pub length_neighborhood: usize,
    /// Estimated-Jaccard threshold for accepting a variable-length match.
    pub jaccard_threshold: f64,
    /// Number of minhash permutations (signature length).
    pub signature_size: usize,
}

impl Default for MinhashConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            length_neighborhood: 5,
            jaccard_threshold: 0.5,
            signature_size: 128,
        }
    }
}

/// Per-parse search parameters, bundled to keep [`DrainState::search`] under
/// clippy's argument-count limit.
#[derive(Debug, Clone, Copy)]
pub struct SearchConfig {
    pub max_children: usize,
    pub sim_thresh: f64,
    pub parametrize_numeric: bool,
    pub depth: usize,
    pub typed_placeholders: bool,
}

/// A single routing key in a Drain3 tree path.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
#[non_exhaustive]
pub enum DrainKey {
    TokenCount(usize),
    Literal(String),
    Wildcard,
    /// ADR-0033 typed-placeholder route key: a digit-bearing token classified as
    /// [`PlaceholderKind`] routes to a per-kind branch instead of the uniform
    /// [`Wildcard`](Self::Wildcard) branch, so mismatched kinds separate at
    /// routing time. Only produced when `typed_placeholders` is enabled.
    Placeholder(PlaceholderKind),
}

impl DrainKey {
    /// Derive the preferred route path before `max_children` fallback.
    ///
    /// When `typed_placeholders` is enabled, content tokens are routed by
    /// classified kind ([`DrainKey::Placeholder`]) instead of being eagerly
    /// wildcarded; otherwise the legacy `parametrize_numeric` behavior applies.
    pub fn route_path_from_tokens(
        tokens: &[String],
        depth: usize,
        parametrize_numeric: bool,
        typed_placeholders: bool,
    ) -> Vec<Self> {
        let content_levels = content_route_levels(tokens, depth);
        let mut path = Vec::with_capacity(1 + content_levels);
        path.push(Self::TokenCount(tokens.len()));
        for token in tokens.iter().take(content_levels) {
            path.push(Self::from_content_token(
                token,
                parametrize_numeric,
                typed_placeholders,
            ));
        }
        path
    }

    fn from_content_token(
        token: &str,
        parametrize_numeric: bool,
        typed_placeholders: bool,
    ) -> Self {
        if typed_placeholders {
            // ADR-0033: route with the shared typed-token adapter. Whole-token
            // placeholders route by kind; embedded placeholders keep their
            // rendered head/tail shape so `value=<*>` and `pid=<*>` do not share
            // the same prefix-tree branch unless later generalized by similarity.
            let (template_token, kind) = Token::from_typed_observed(token);
            if template_token.is_pure_wildcard() {
                return kind.map_or(Self::Wildcard, Self::Placeholder);
            }
            return Self::Literal(template_token.to_rendered_string());
        }
        match token::normalize_for_routing(token, parametrize_numeric) {
            WILDCARD => Self::Wildcard,
            literal => Self::Literal(literal.to_string()),
        }
    }
}

/// A cluster of log lines that share the same template pattern.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct DrainCluster {
    pub id: usize,
    /// Rendered template mirror kept for the existing public Drain3 API.
    pub log_template: Vec<String>,
    /// Shared token model used internally for template semantics.
    pub(crate) template_tokens: Vec<Token>,
    pub route_path: Vec<DrainKey>,
    // ADR-0028 requires cache serialization to preserve complete `log_ids`.
    // Keeping them live is simple and correct; large-log memory reduction can
    // be revisited if cache DTOs start deriving them lazily.
    pub log_ids: Vec<usize>,
    pub size: usize,
    /// ADR-0033 internal placeholder metadata, aligned with [`log_template`] by
    /// position. `Some(kind)` records that every observation merged into that
    /// template slot carried `kind`; `None` means untyped (wildcard/literal
    /// under existing semantics). Parallel metadata — never rendered; preserved
    /// through [`DrainState::update_cluster_template_for_line`] via
    /// [`placeholder::join`](crate::placeholder::join). Drain3 cache format
    /// version 2 persists the mask and rejects incomplete masks.
    pub placeholder_mask: PlaceholderMask,
    /// ADR-0033 改良③: memoized minhash signature over `log_template` tokens,
    /// for variable-length clustering. A pure function of `log_template`, so it
    /// is not persisted in the cache — restore recomputes it.
    pub minhash_sig: Vec<u64>,
}

impl DrainCluster {
    pub fn new(id: usize, log_template: Vec<String>, route_path: Vec<DrainKey>) -> Self {
        Self::with_log_id(id, log_template, route_path, 0)
    }

    pub fn with_log_id(
        id: usize,
        log_template: Vec<String>,
        route_path: Vec<DrainKey>,
        log_id: usize,
    ) -> Self {
        let template_tokens = rendered_tokens_to_template_tokens(&log_template);
        let log_template = render_token_list(&template_tokens);
        let placeholder_mask = mask_from_template(&template_tokens);
        Self {
            id,
            log_template,
            template_tokens,
            route_path,
            log_ids: vec![log_id],
            size: 1,
            placeholder_mask,
            minhash_sig: Vec::new(),
        }
    }

    /// Render the template as a space-joined string.
    pub fn template_string(&self) -> String {
        render_token_list_joined(&self.template_tokens)
    }

    fn sync_rendered_template(&mut self) {
        self.log_template = render_token_list(&self.template_tokens);
    }
}

/// A node in Drain3's fixed-depth tree.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DrainNode {
    children: BTreeMap<DrainKey, DrainNode>,
    cluster_ids: Vec<usize>,
}

impl DrainNode {
    pub fn empty_branch() -> Self {
        Self {
            children: BTreeMap::new(),
            cluster_ids: Vec::new(),
        }
    }

    fn cluster_ids(&self) -> &[usize] {
        &self.cluster_ids
    }

    fn cluster_ids_mut(&mut self) -> &mut Vec<usize> {
        &mut self.cluster_ids
    }
}

/// Complete state of the Drain3 algorithm.
#[derive(Debug, Clone)]
pub struct DrainState {
    root: DrainNode,
    clusters: Vec<DrainCluster>,
    next_cluster_id: usize,
    minhash_config: MinhashConfig,
}

impl DrainState {
    pub fn new() -> Self {
        Self {
            root: DrainNode::empty_branch(),
            clusters: Vec::new(),
            next_cluster_id: 0,
            minhash_config: MinhashConfig::default(),
        }
    }

    pub fn clusters(&self) -> &[DrainCluster] {
        &self.clusters
    }

    /// Return the cluster with `id`.
    ///
    /// The caller must provide an `id` returned by `search`, `add_cluster`, or
    /// `add_cluster_for_line`; out-of-bounds ids will panic.
    pub(crate) fn cluster(&self, id: usize) -> &DrainCluster {
        &self.clusters[id]
    }

    pub fn cluster_count(&self) -> usize {
        self.clusters.len()
    }

    /// Search the tree for a matching cluster, or return the path for a new one.
    pub fn search(
        &mut self,
        tokens: &[String],
        ctx: &SearchConfig,
        line_sig: &[u64],
    ) -> SearchResult {
        let mut route_path = DrainKey::route_path_from_tokens(
            tokens,
            ctx.depth,
            ctx.parametrize_numeric,
            ctx.typed_placeholders,
        );

        let candidate_ids = Self::walk(&mut self.root, &mut route_path, 0, ctx.max_children);

        let best = candidate_ids
            .iter()
            .map(|&id| {
                let cluster = &self.clusters[id];
                let sim = cluster_similarity(
                    tokens,
                    cluster,
                    ctx.typed_placeholders,
                    ctx.parametrize_numeric,
                );
                let wc = count_wildcards(&cluster.template_tokens);
                (id, sim, wc)
            })
            .filter(|(_, sim, _)| *sim >= ctx.sim_thresh)
            .max_by(|a, b| {
                a.1.partial_cmp(&b.1)
                    .unwrap_or(std::cmp::Ordering::Equal)
                    // Python Drain3 `param_count`: more wildcards wins ties.
                    .then_with(|| a.2.cmp(&b.2))
                    // Deterministic fallback: lower cluster id wins.
                    .then_with(|| b.0.cmp(&a.0))
            });

        if let Some((id, _, _)) = best {
            return SearchResult::Match(id);
        }

        // ADR-0033 改良③: variable-length fallback via minhash Jaccard over a
        // token-count neighborhood (per the reference article).
        if let Some(id) = self.minhash_fallback(tokens.len(), line_sig) {
            return SearchResult::Match(id);
        }

        SearchResult::NewCluster(route_path)
    }

    /// Search without mutating the tree. Used by read-only assignment passes.
    pub fn search_read_only(
        &self,
        tokens: &[String],
        ctx: &SearchConfig,
        line_sig: &[u64],
    ) -> Option<usize> {
        let route_path = DrainKey::route_path_from_tokens(
            tokens,
            ctx.depth,
            ctx.parametrize_numeric,
            ctx.typed_placeholders,
        );
        let candidate_ids = Self::walk_existing(&self.root, &route_path, 0, ctx.max_children);

        let positional = candidate_ids
            .iter()
            .map(|&id| {
                let cluster = &self.clusters[id];
                let sim = cluster_similarity(
                    tokens,
                    cluster,
                    ctx.typed_placeholders,
                    ctx.parametrize_numeric,
                );
                let wc = count_wildcards(&cluster.template_tokens);
                (id, sim, wc)
            })
            .filter(|(_, sim, _)| *sim >= ctx.sim_thresh)
            .max_by(|a, b| {
                a.1.partial_cmp(&b.1)
                    .unwrap_or(std::cmp::Ordering::Equal)
                    .then_with(|| a.2.cmp(&b.2))
                    .then_with(|| b.0.cmp(&a.0))
            })
            .map(|(id, _, _)| id);

        if positional.is_some() {
            return positional;
        }

        // ADR-0033 改良③: variable-length minhash fallback.
        self.minhash_fallback(tokens.len(), line_sig)
    }

    /// ADR-0033 改良③: variable-length fallback. When same-length positional
    /// similarity finds no match, estimate Jaccard similarity between `line_sig`
    /// and each cluster's [`minhash_sig`](DrainCluster::minhash_sig) over a
    /// token-count neighborhood `[L - neighborhood, L + neighborhood]`, returning
    /// the best cluster at or above `jaccard_threshold`.
    fn minhash_fallback(&self, line_len: usize, line_sig: &[u64]) -> Option<usize> {
        if !self.minhash_config.enabled || line_sig.is_empty() {
            return None;
        }
        let lo = line_len.saturating_sub(self.minhash_config.length_neighborhood);
        let hi = line_len + self.minhash_config.length_neighborhood;
        let threshold = self.minhash_config.jaccard_threshold;
        let mut best: Option<(usize, f64)> = None;
        for id in self.clusters_by_length_range(lo, hi) {
            let cluster = &self.clusters[id];
            if cluster.minhash_sig.is_empty() {
                continue;
            }
            let j = minhash::estimated_jaccard(line_sig, &cluster.minhash_sig);
            if j >= threshold && best.is_none_or(|(_, bj)| j > bj) {
                best = Some((id, j));
            }
        }
        best.map(|(id, _)| id)
    }

    /// Collect all cluster ids whose route token count falls in `[lo, hi]`, by
    /// walking the tree's first-level `TokenCount` subtrees in that range.
    fn clusters_by_length_range(&self, lo: usize, hi: usize) -> Vec<usize> {
        let mut ids = Vec::new();
        let start = DrainKey::TokenCount(lo);
        let end = DrainKey::TokenCount(hi + 1);
        for (key, child) in self.root.children.range(start..end) {
            if matches!(key, DrainKey::TokenCount(_)) {
                Self::collect_cluster_ids(child, &mut ids);
            }
        }
        ids
    }

    fn collect_cluster_ids(node: &DrainNode, out: &mut Vec<usize>) {
        out.extend_from_slice(node.cluster_ids());
        for child in node.children.values() {
            Self::collect_cluster_ids(child, out);
        }
    }

    fn walk(
        current: &mut DrainNode,
        route_path: &mut Vec<DrainKey>,
        level: usize,
        max_children: usize,
    ) -> Vec<usize> {
        if level >= route_path.len() {
            return current.cluster_ids().to_vec();
        }

        let requested_key = route_path[level].clone();
        let effective_key = Self::effective_child_key(current, requested_key, level, max_children);
        route_path[level] = effective_key.clone();
        let child = current
            .children
            .entry(effective_key)
            .or_insert_with(DrainNode::empty_branch);
        Self::walk(child, route_path, level + 1, max_children)
    }

    fn effective_child_key(
        current: &mut DrainNode,
        requested_key: DrainKey,
        level: usize,
        max_children: usize,
    ) -> DrainKey {
        if level == 0 || matches!(requested_key, DrainKey::Wildcard) {
            return requested_key;
        }

        if matches!(requested_key, DrainKey::Placeholder(_)) {
            // Prefer the precise per-kind branch. Fall back to an existing
            // Wildcard subtree for defensive compatibility with in-memory
            // states built before typed routing became durable.
            if current.children.contains_key(&requested_key) {
                return requested_key;
            }
            if current.children.contains_key(&DrainKey::Wildcard) {
                return DrainKey::Wildcard;
            }
            return requested_key;
        }

        if current.children.contains_key(&requested_key) {
            return requested_key;
        }

        let literal_children = current
            .children
            .keys()
            .filter(|key| matches!(key, DrainKey::Literal(_)))
            .count();

        if literal_children >= max_children {
            DrainKey::Wildcard
        } else {
            requested_key
        }
    }

    fn walk_existing(
        current: &DrainNode,
        route_path: &[DrainKey],
        level: usize,
        max_children: usize,
    ) -> Vec<usize> {
        if level >= route_path.len() {
            return current.cluster_ids().to_vec();
        }

        let requested_key = &route_path[level];
        let effective_key =
            Self::effective_existing_child_key(current, requested_key, level, max_children);
        match current.children.get(&effective_key) {
            Some(child) => Self::walk_existing(child, route_path, level + 1, max_children),
            None => Vec::new(),
        }
    }

    fn effective_existing_child_key(
        current: &DrainNode,
        requested_key: &DrainKey,
        level: usize,
        max_children: usize,
    ) -> DrainKey {
        if level == 0 || matches!(requested_key, DrainKey::Wildcard) {
            return requested_key.clone();
        }

        if matches!(requested_key, DrainKey::Placeholder(_)) {
            // Prefer the precise per-kind branch; fall back to an existing
            // Wildcard subtree for defensive compatibility with in-memory
            // states built before typed routing became durable.
            if current.children.contains_key(requested_key) {
                return requested_key.clone();
            }
            if current.children.contains_key(&DrainKey::Wildcard) {
                return DrainKey::Wildcard;
            }
            return requested_key.clone();
        }

        if current.children.contains_key(requested_key) {
            return requested_key.clone();
        }

        let literal_children = current
            .children
            .keys()
            .filter(|key| matches!(key, DrainKey::Literal(_)))
            .count();

        if literal_children >= max_children {
            DrainKey::Wildcard
        } else {
            requested_key.clone()
        }
    }

    /// Add a new cluster and insert its id at the exact stored route path.
    pub fn add_cluster(&mut self, log_template: Vec<String>, route_path: Vec<DrainKey>) -> usize {
        self.add_cluster_for_line(log_template, route_path, 0)
    }

    /// Add a new cluster associated with the first matched line id.
    pub fn add_cluster_for_line(
        &mut self,
        log_template: Vec<String>,
        route_path: Vec<DrainKey>,
        line_number: usize,
    ) -> usize {
        let id = self.next_cluster_id;
        self.next_cluster_id += 1;
        let cluster = DrainCluster::with_log_id(id, log_template, route_path.clone(), line_number);
        self.clusters.push(cluster);
        self.insert_cluster_at_path(&route_path, id);
        self.recompute_minhash_sig(id);
        id
    }

    /// Update an existing cluster's template and record the matching line id.
    ///
    /// When `typed_placeholders` is enabled, `raw_tokens` (the observed line's
    /// real tokens) maintain the cluster's [`placeholder_mask`](DrainCluster::placeholder_mask)
    /// aligned with the generalized `new_template`: each `<*>` slot is joined
    /// with the observed token's kind, so conflicting kinds downgrade to
    /// untyped. When disabled, the mask is left untouched (flag-off parity).
    ///
    /// The caller must provide a valid `cluster_id` returned by `search` or
    /// `add_cluster`; out-of-bounds ids will panic.
    pub(crate) fn update_cluster_template_for_line(
        &mut self,
        cluster_id: usize,
        new_template: Vec<Token>,
        line_number: usize,
        line_kinds: &[Option<PlaceholderKind>],
        typed_placeholders: bool,
    ) {
        let cluster = &mut self.clusters[cluster_id];
        cluster.template_tokens = new_template;
        cluster.sync_rendered_template();
        cluster.log_ids.push(line_number);
        cluster.size += 1;
        if typed_placeholders {
            maintain_placeholder_mask(cluster, line_kinds);
        } else {
            reset_placeholder_mask(cluster);
        }
        if self.minhash_config.enabled {
            cluster.minhash_sig =
                minhash::signature(&cluster.log_template, self.minhash_config.signature_size);
        }
    }

    /// Record a match on an existing cluster without changing its template.
    ///
    /// When `typed_placeholders` is enabled, the observed `raw_tokens` are still
    /// joined into the [`placeholder_mask`](DrainCluster::placeholder_mask) — a
    /// soft-similarity match may accept a cross-kind token at a typed slot, in
    /// which case the slot downgrades to untyped.
    ///
    /// The caller must provide a valid `cluster_id` returned by `search` or
    /// `add_cluster`; out-of-bounds ids will panic.
    pub(crate) fn record_match_for_line(
        &mut self,
        cluster_id: usize,
        line_number: usize,
        line_kinds: &[Option<PlaceholderKind>],
        typed_placeholders: bool,
    ) {
        let cluster = &mut self.clusters[cluster_id];
        cluster.log_ids.push(line_number);
        cluster.size += 1;
        if typed_placeholders {
            maintain_placeholder_mask(cluster, line_kinds);
        } else {
            reset_placeholder_mask(cluster);
        }
    }

    /// ADR-0033: (re)build a cluster's [`placeholder_mask`](DrainCluster::placeholder_mask)
    /// from the real observed tokens.
    ///
    /// Used at NewCluster creation in typed mode: the stored template already
    /// wildcarded classified tokens to `<*>`, but the mask must record each
    /// placeholder slot's kind. `tokens.len()` must equal the template length.
    /// ADR-0033: set a cluster's [`placeholder_mask`](DrainCluster::placeholder_mask)
    /// directly from the per-line precomputed kinds, avoiding a second
    /// classification pass over the raw tokens.
    pub(crate) fn set_cluster_mask(
        &mut self,
        cluster_id: usize,
        kinds: &[Option<PlaceholderKind>],
    ) {
        let cluster = &mut self.clusters[cluster_id];
        cluster.placeholder_mask = kinds.to_vec();
    }

    /// ADR-0033 改良③: set the minhash configuration and recompute every
    /// cluster's signature (the signature size may have changed). Called by the
    /// parser at construction and after a cache warm-start.
    pub(crate) fn set_minhash_config(&mut self, config: MinhashConfig) {
        self.minhash_config = config;
        self.recompute_all_minhash_sigs();
    }

    /// Recompute every cluster's minhash signature from its current template.
    pub(crate) fn recompute_all_minhash_sigs(&mut self) {
        if !self.minhash_config.enabled {
            return;
        }
        let sig_size = self.minhash_config.signature_size;
        for cluster in &mut self.clusters {
            cluster.minhash_sig = minhash::signature(&cluster.log_template, sig_size);
        }
    }

    fn recompute_minhash_sig(&mut self, cluster_id: usize) {
        if !self.minhash_config.enabled {
            return;
        }
        let sig_size = self.minhash_config.signature_size;
        let cluster = &mut self.clusters[cluster_id];
        cluster.minhash_sig = minhash::signature(&cluster.log_template, sig_size);
    }

    /// Rebuild a Drain3 state from cached clusters and their immutable route
    /// paths. The tree is reconstructed from `route_path`, never from final
    /// generalized templates.
    pub fn from_cached_clusters(clusters: Vec<DrainCluster>, depth: usize) -> Result<Self, String> {
        let mut state = Self {
            root: DrainNode::empty_branch(),
            next_cluster_id: clusters.len(),
            clusters,
            minhash_config: MinhashConfig::default(),
        };

        let cluster_count = state.clusters.len();
        let mut paths = Vec::with_capacity(cluster_count);
        for (index, cluster) in state.clusters.iter().enumerate() {
            validate_cached_cluster(cluster, index, depth)?;
            paths.push((cluster.route_path.clone(), cluster.id));
        }

        for (route_path, cluster_id) in paths {
            state.insert_cluster_at_path(&route_path, cluster_id);
        }

        Ok(state)
    }

    fn insert_cluster_at_path(&mut self, route_path: &[DrainKey], cluster_id: usize) {
        let mut current = &mut self.root;
        for key in route_path {
            current = current
                .children
                .entry(key.clone())
                .or_insert_with(DrainNode::empty_branch);
        }
        current.cluster_ids_mut().push(cluster_id);
    }
}

impl Default for DrainState {
    fn default() -> Self {
        Self::new()
    }
}

/// Result of a tree search.
#[derive(Debug)]
pub enum SearchResult {
    Match(usize),
    NewCluster(Vec<DrainKey>),
}

fn content_route_levels(tokens: &[String], depth: usize) -> usize {
    tokens.len().min(depth.saturating_sub(3))
}

fn validate_cached_cluster(
    cluster: &DrainCluster,
    expected_id: usize,
    depth: usize,
) -> Result<(), String> {
    if cluster.id != expected_id {
        return Err(format!(
            "cached cluster id {} is not contiguous at position {expected_id}",
            cluster.id
        ));
    }
    if cluster.route_path.is_empty() {
        return Err(format!(
            "cached cluster {} has empty route_path",
            cluster.id
        ));
    }
    let route_token_count = match cluster.route_path.first() {
        Some(DrainKey::TokenCount(count)) => *count,
        _ => {
            return Err(format!(
                "cached cluster {} route_path must start with token count",
                cluster.id
            ));
        }
    };
    // The route's TokenCount is the cluster's creation length. Variable-length
    // (minhash) matches may grow `log_template` beyond it via generalization,
    // so allow `template.len() >= route_token_count`; a shrink indicates
    // corruption.
    if route_token_count > cluster.template_tokens.len() {
        return Err(format!(
            "cached cluster {} token count route key {route_token_count} exceeds template len {}",
            cluster.id,
            cluster.template_tokens.len()
        ));
    }
    // `expected_path_len` is derived from the creation length (the route's
    // TokenCount), not the current template length: the route path is immutable
    // and was built for the creation length, so it must not change as the
    // template grows.
    let expected_path_len = 1 + route_token_count.min(depth.saturating_sub(3));
    if cluster.route_path.len() != expected_path_len {
        return Err(format!(
            "cached cluster {} route_path len {} does not match expected {expected_path_len}",
            cluster.id,
            cluster.route_path.len()
        ));
    }

    for key in cluster.route_path.iter().skip(1) {
        match key {
            DrainKey::Literal(value) if value == WILDCARD => {
                return Err(format!(
                    "cached cluster {} route_path literal must not be <*>",
                    cluster.id
                ));
            }
            DrainKey::Literal(_) | DrainKey::Wildcard | DrainKey::Placeholder(_) => {}
            DrainKey::TokenCount(_) => {
                return Err(format!(
                    "cached cluster {} route_path contains nested token count",
                    cluster.id
                ));
            }
        }
    }

    if cluster.size != cluster.log_ids.len() {
        return Err(format!(
            "cached cluster {} size {} does not match log_ids len {}",
            cluster.id,
            cluster.size,
            cluster.log_ids.len()
        ));
    }

    Ok(())
}

/// Count `<*>` wildcards in a template. Used for tie-breaking.
pub fn count_wildcards(template: &[Token]) -> usize {
    template.iter().filter(|t| t.contains_wildcard()).count()
}

fn rendered_tokens_to_template_tokens(tokens: &[String]) -> Vec<Token> {
    tokens.iter().map(Token::from_rendered_template).collect()
}

fn mask_from_template(tokens: &[Token]) -> PlaceholderMask {
    vec![None; tokens.len()]
}

/// ADR-0033: keep `placeholder_mask` aligned with `log_template` after a match.
///
/// Resize to the template length (new slots untyped), then join each `<*>` slot
/// with the observed token's kind. `placeholder::join` downgrades conflicting
/// kinds to `None` rather than inventing a kind after a merge. Called on every
/// match (template changed or not) so a soft-similarity cross-kind match
/// downgrades the slot correctly.
fn maintain_placeholder_mask(cluster: &mut DrainCluster, line_kinds: &[Option<PlaceholderKind>]) {
    maintain_mask_for_template(
        &mut cluster.placeholder_mask,
        &cluster.template_tokens,
        line_kinds,
        |token| token.contains_wildcard(),
    );
}

fn reset_placeholder_mask(cluster: &mut DrainCluster) {
    cluster.placeholder_mask.clear();
    cluster
        .placeholder_mask
        .resize(cluster.template_tokens.len(), None);
}

/// Pick the similarity function for a candidate cluster: typed-aware (soft,
/// ADR-0033 改良① per the reference article) when `typed_placeholders` is on,
/// otherwise the legacy ADR-0028 `similarity`.
fn cluster_similarity(
    tokens: &[String],
    cluster: &DrainCluster,
    typed_placeholders: bool,
    parametrize_numeric: bool,
) -> f64 {
    if typed_placeholders {
        typed_similarity(tokens, &cluster.template_tokens, &cluster.placeholder_mask)
    } else {
        similarity_tokens(tokens, &cluster.template_tokens, parametrize_numeric)
    }
}

/// ADR-0033 改良① (soft, per the reference article): typed-aware similarity.
///
/// A template position counts as a match iff:
/// - it is a pure typed `<*>` slot (`mask[i] = Some(kind)`) and the observed
///   token's own kind (whole-token or embedded-span) equals it,
/// - it is a contextual wildcard (`value=<*>`) whose head/tail covers the
///   observed token's shared typed-token rendering, or
/// - it is a literal equal to the observed token.
///
/// An untyped pure `<*>` slot (`mask[i] = None`) never counts (as eager
/// [`similarity`]). Contextual wildcards behave like anchored template tokens;
/// mask conflicts are still joined/downgraded during template maintenance.
/// `sim = matches / template.len()`; the caller applies `sim_thresh` (soft — a
/// mismatched typed slot lowers similarity rather than hard-rejecting).
fn typed_similarity(
    tokens: &[String],
    template: &[Token],
    mask: &[Option<PlaceholderKind>],
) -> f64 {
    if template.is_empty() {
        return if tokens.is_empty() { 1.0 } else { 0.0 };
    }
    let mut matches = 0usize;
    for (i, tmpl) in template.iter().enumerate() {
        let Some(observed) = tokens.get(i) else {
            continue;
        };
        if tmpl.contains_wildcard() {
            if wildcard_slot_matches(tmpl, observed, mask, i) {
                matches += 1;
            }
        } else if tmpl.render().as_ref() == observed {
            matches += 1;
        }
    }
    matches as f64 / template.len() as f64
}

fn wildcard_slot_matches(
    template: &Token,
    observed: &str,
    mask: &[Option<PlaceholderKind>],
    index: usize,
) -> bool {
    if template.is_pure_wildcard() {
        return typed_slot_matches_kind(mask, index, classify_observed_kind(observed));
    }
    wildcard_token_covers_text(template, observed)
}

fn similarity_tokens(
    log_tokens: &[String],
    template_tokens: &[Token],
    _parametrize_numeric: bool,
) -> f64 {
    if template_tokens.is_empty() {
        return if log_tokens.is_empty() { 1.0 } else { 0.0 };
    }

    let literal_matches = log_tokens
        .iter()
        .zip(template_tokens.iter())
        .filter(|(log, tmpl)| !tmpl.contains_wildcard() && tmpl.render().as_ref() == log.as_str())
        .count();

    literal_matches as f64 / template_tokens.len() as f64
}

/// Compute Drain3 similarity according to ADR-0028.
///
/// Template wildcard positions are excluded from the numerator. Input-side
/// `<*>` tokens only match if the template position is the same literal, which
/// cannot happen for wildcard template slots because those positions are
/// excluded by the formula.
pub fn similarity(
    log_tokens: &[String],
    template_tokens: &[String],
    parametrize_numeric: bool,
) -> f64 {
    let template_tokens = rendered_tokens_to_template_tokens(template_tokens);
    similarity_tokens(log_tokens, &template_tokens, parametrize_numeric)
}

pub(crate) fn generalize_template_tokens(log_tokens: &[String], template_tokens: &mut Vec<Token>) {
    let len = log_tokens.len().max(template_tokens.len());
    if template_tokens.len() < len {
        template_tokens.resize_with(len, || Token::wildcard("", ""));
    }

    for (i, template_token) in template_tokens.iter_mut().enumerate() {
        if template_token.is_pure_wildcard() {
            continue;
        }
        if template_token.contains_wildcard()
            && log_tokens
                .get(i)
                .is_some_and(|log_tok| wildcard_token_covers_text(template_token, log_tok))
        {
            continue;
        }

        let log_matches = log_tokens
            .get(i)
            .is_some_and(|log_tok| template_token.render().as_ref() == log_tok.as_str());

        if !log_matches {
            *template_token = Token::wildcard("", "");
        }
    }
}

/// Generalize a template by replacing mismatched positions with `<*>`.
pub fn generalize_template(log_tokens: &[String], template_tokens: &mut Vec<String>) {
    let mut tokens = rendered_tokens_to_template_tokens(template_tokens);
    generalize_template_tokens(log_tokens, &mut tokens);
    *template_tokens = render_token_list(&tokens);
}

/// Generalize a template to cover a variable-length observed log by aligning
/// the longest common prefix and suffix and collapsing the differing middle
/// into `<*>` wildcards. The result length is `max(template, log)`, so the
/// template only grows (never shrinks below its creation length).
///
/// Unlike [`generalize_template`] — which pads to the longer length and
/// compares positionally, misaligning a stable trailing literal (so
/// `worker alpha started` + `worker alpha beta started` degrades to
/// `worker alpha <*> <*>`, losing `started`) — this preserves shared
/// prefix/suffix literals across a length difference, yielding
/// `worker alpha <*> started`.
///
/// Used for minhash variable-length matches (改良③). The caller must ensure
/// `log_tokens` and `template_tokens` are non-empty and of different length.
#[cfg(test)]
pub(crate) fn generalize_template_variable(
    log_tokens: &[String],
    template_tokens: &mut Vec<String>,
) {
    let mut tokens = rendered_tokens_to_template_tokens(template_tokens);
    generalize_template_variable_tokens(log_tokens, &mut tokens);
    *template_tokens = render_token_list(&tokens);
}

pub(crate) fn generalize_template_variable_tokens(
    log_tokens: &[String],
    template_tokens: &mut Vec<Token>,
) {
    let m = template_tokens.len();
    let n = log_tokens.len();
    let matches = |tmpl: &Token, obs: &str| {
        tmpl.is_pure_wildcard()
            || (tmpl.contains_wildcard() && wildcard_token_covers_text(tmpl, obs))
            || tmpl.render().as_ref() == obs
    };

    let mut p = 0;
    while p < m && p < n && matches(&template_tokens[p], &log_tokens[p]) {
        p += 1;
    }
    let mut s = 0;
    while s < m - p && s < n - p && matches(&template_tokens[m - 1 - s], &log_tokens[n - 1 - s]) {
        s += 1;
    }
    let middle = (m - p - s).max(n - p - s);
    let mut result = Vec::with_capacity(p + middle + s);
    result.extend_from_slice(&template_tokens[..p]);
    for _ in 0..middle {
        result.push(Token::wildcard("", ""));
    }
    result.extend_from_slice(&template_tokens[m - s..]);
    *template_tokens = result;
}

fn wildcard_token_covers_text(template: &Token, observed: &str) -> bool {
    match template {
        Token::Literal(value) => value.eq_ignore_ascii_case(observed),
        Token::Wildcard { head, tail } => {
            observed.len() >= head.len() + tail.len()
                && observed
                    .get(..head.len())
                    .is_some_and(|prefix| prefix.eq_ignore_ascii_case(head))
                && observed
                    .get(observed.len().saturating_sub(tail.len())..)
                    .is_some_and(|suffix| suffix.eq_ignore_ascii_case(tail))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_tokens(tokens: &[&str]) -> Vec<String> {
        tokens.iter().map(|s| s.to_string()).collect()
    }

    fn make_template(tokens: &[&str]) -> Vec<Token> {
        tokens
            .iter()
            .map(|token| Token::from_rendered_template(*token))
            .collect()
    }

    fn add_from_search(
        state: &mut DrainState,
        tokens: &[String],
        max_children: usize,
        depth: usize,
    ) -> usize {
        match state.search(
            tokens,
            &SearchConfig {
                max_children,
                sim_thresh: 0.4,
                parametrize_numeric: true,
                depth,
                typed_placeholders: false,
            },
            &[],
        ) {
            SearchResult::NewCluster(path) => state.add_cluster(tokens.to_vec(), path),
            SearchResult::Match(id) => id,
        }
    }

    #[test]
    fn route_path_uses_depth_minus_three_content_tokens() {
        let tokens = make_tokens(&["a", "b", "c", "d"]);
        let path = DrainKey::route_path_from_tokens(&tokens, 6, true, false);
        assert_eq!(
            path,
            vec![
                DrainKey::TokenCount(4),
                DrainKey::Literal("a".to_string()),
                DrainKey::Literal("b".to_string()),
                DrainKey::Literal("c".to_string()),
            ]
        );
    }

    #[test]
    fn route_path_depth_four_uses_one_content_token() {
        let tokens = make_tokens(&["a", "b", "c"]);
        let path = DrainKey::route_path_from_tokens(&tokens, 4, true, false);
        assert_eq!(
            path,
            vec![DrainKey::TokenCount(3), DrainKey::Literal("a".to_string())]
        );
    }

    #[test]
    fn route_path_depth_three_uses_only_token_count() {
        let tokens = make_tokens(&["a", "b", "c"]);
        let path = DrainKey::route_path_from_tokens(&tokens, 3, true, false);
        assert_eq!(path, vec![DrainKey::TokenCount(3)]);
    }

    #[test]
    fn route_path_numeric_parametrization() {
        let tokens = make_tokens(&["v2", "worker", "123"]);
        let path = DrainKey::route_path_from_tokens(&tokens, 6, true, false);
        assert_eq!(
            path,
            vec![
                DrainKey::TokenCount(3),
                DrainKey::Wildcard,
                DrainKey::Literal("worker".to_string()),
                DrainKey::Wildcard,
            ]
        );
    }

    #[test]
    fn route_path_nan_is_literal() {
        let tokens = make_tokens(&["NaN", "worker"]);
        let path = DrainKey::route_path_from_tokens(&tokens, 4, true, false);
        assert_eq!(path[1], DrainKey::Literal("NaN".to_string()));
    }

    #[test]
    fn route_path_already_wildcard() {
        let tokens = make_tokens(&["<*>", "worker"]);
        let path = DrainKey::route_path_from_tokens(&tokens, 4, false, false);
        assert_eq!(path[1], DrainKey::Wildcard);
    }

    #[test]
    fn route_path_empty_tokens() {
        let tokens: Vec<String> = vec![];
        let path = DrainKey::route_path_from_tokens(&tokens, 4, true, false);
        assert_eq!(path, vec![DrainKey::TokenCount(0)]);
    }

    #[test]
    fn similarity_identical() {
        let log = make_tokens(&["worker", "started"]);
        let tmpl = make_tokens(&["worker", "started"]);
        assert_eq!(similarity(&log, &tmpl, true), 1.0);
    }

    #[test]
    fn similarity_excludes_template_wildcards_from_matches() {
        let log = make_tokens(&["worker", "started"]);
        let tmpl = make_tokens(&["worker", "<*>"]);
        assert_eq!(similarity(&log, &tmpl, true), 0.5);
    }

    #[test]
    fn similarity_mismatch() {
        let log = make_tokens(&["worker", "started"]);
        let tmpl = make_tokens(&["worker", "stopped"]);
        assert_eq!(similarity(&log, &tmpl, true), 0.5);
    }

    #[test]
    fn similarity_empty() {
        let log: Vec<String> = vec![];
        let tmpl: Vec<String> = vec![];
        assert_eq!(similarity(&log, &tmpl, true), 1.0);
    }

    #[test]
    fn similarity_uses_template_length_denominator() {
        let log = make_tokens(&["a", "b", "c"]);
        let tmpl = make_tokens(&["a", "b"]);
        assert_eq!(similarity(&log, &tmpl, true), 1.0);
    }

    #[test]
    fn similarity_input_wildcard_does_not_match_literal_template() {
        let log = make_tokens(&["<*>", "worker"]);
        let tmpl = make_tokens(&["abc", "worker"]);
        assert_eq!(similarity(&log, &tmpl, true), 0.5);
    }

    #[test]
    fn similarity_template_wildcard_does_not_increment_score() {
        let log = make_tokens(&["<*>", "worker"]);
        let tmpl = make_tokens(&["<*>", "worker"]);
        assert_eq!(similarity(&log, &tmpl, true), 0.5);
    }

    #[test]
    fn similarity_nan_is_literal() {
        let log = make_tokens(&["NaN", "worker"]);
        let tmpl = make_tokens(&["abc", "worker"]);
        assert_eq!(similarity(&log, &tmpl, true), 0.5);
    }

    #[test]
    fn generalize_mismatched_positions() {
        let log = make_tokens(&["worker", "42", "started"]);
        let mut tmpl = make_tokens(&["worker", "0", "started"]);
        generalize_template(&log, &mut tmpl);
        assert_eq!(tmpl, make_tokens(&["worker", "<*>", "started"]));
    }

    #[test]
    fn generalize_preserves_existing_wildcards() {
        let log = make_tokens(&["worker", "42", "started"]);
        let mut tmpl = make_tokens(&["worker", "<*>", "started"]);
        generalize_template(&log, &mut tmpl);
        assert_eq!(tmpl, make_tokens(&["worker", "<*>", "started"]));
    }

    #[test]
    fn generalize_all_mismatched() {
        let log = make_tokens(&["a", "b", "c"]);
        let mut tmpl = make_tokens(&["x", "y", "z"]);
        generalize_template(&log, &mut tmpl);
        assert_eq!(tmpl, make_tokens(&["<*>", "<*>", "<*>"]));
    }

    #[test]
    fn generalize_template_shorter_than_log() {
        let log = make_tokens(&["a", "b", "c"]);
        let mut tmpl = make_tokens(&["a"]);
        generalize_template(&log, &mut tmpl);
        assert_eq!(tmpl, make_tokens(&["a", "<*>", "<*>"]));
    }

    #[test]
    fn generalize_template_longer_than_log() {
        let log = make_tokens(&["a"]);
        let mut tmpl = make_tokens(&["a", "b", "c"]);
        generalize_template(&log, &mut tmpl);
        assert_eq!(tmpl, make_tokens(&["a", "<*>", "<*>"]));
    }

    #[test]
    fn generalize_variable_aligns_prefix_and_suffix() {
        // `worker alpha started` + `worker alpha beta started` → the inserted
        // `beta` becomes `<*>` and the trailing `started` literal survives.
        let log = make_tokens(&["worker", "alpha", "beta", "started"]);
        let mut tmpl = make_tokens(&["worker", "alpha", "started"]);
        generalize_template_variable(&log, &mut tmpl);
        assert_eq!(tmpl, make_tokens(&["worker", "alpha", "<*>", "started"]));
    }

    #[test]
    fn generalize_variable_preserves_trailing_literal_on_insertion() {
        let log = make_tokens(&["a", "b", "c"]);
        let mut tmpl = make_tokens(&["a", "c"]);
        generalize_template_variable(&log, &mut tmpl);
        assert_eq!(tmpl, make_tokens(&["a", "<*>", "c"]));
    }

    #[test]
    fn generalize_variable_grows_to_max_length() {
        // A shorter log wildcards the uncovered tail; the template keeps length
        // max(template, log) so it never shrinks below its creation length.
        let log = make_tokens(&["a", "b"]);
        let mut tmpl = make_tokens(&["a", "b", "c"]);
        generalize_template_variable(&log, &mut tmpl);
        assert_eq!(tmpl, make_tokens(&["a", "b", "<*>"]));
    }

    #[test]
    fn walk_finds_existing_leaf_after_insert() {
        let mut state = DrainState::new();
        let tokens = make_tokens(&["worker", "started"]);
        let id = add_from_search(&mut state, &tokens, 100, 4);
        assert_eq!(id, 0);

        let result = state.search(
            &tokens,
            &SearchConfig {
                max_children: 100,
                sim_thresh: 0.4,
                parametrize_numeric: true,
                depth: 4,
                typed_placeholders: false,
            },
            &[],
        );
        assert!(matches!(result, SearchResult::Match(0)));
    }

    #[test]
    fn leaf_holds_multiple_candidates() {
        let mut state = DrainState::new();
        let tokens_a = make_tokens(&["worker", "job", "started", "done"]);
        let tokens_b = make_tokens(&["worker", "job", "stopped", "done"]);

        let path_a = DrainKey::route_path_from_tokens(&tokens_a, 5, true, false);
        let path_b = DrainKey::route_path_from_tokens(&tokens_b, 5, true, false);
        assert_eq!(path_a, path_b);

        let id_a = state.add_cluster(tokens_a.clone(), path_a);
        let id_b = state.add_cluster(tokens_b.clone(), path_b);
        assert_ne!(id_a, id_b);

        let result = state.search(
            &tokens_a,
            &SearchConfig {
                max_children: 100,
                sim_thresh: 0.4,
                parametrize_numeric: true,
                depth: 5,
                typed_placeholders: false,
            },
            &[],
        );
        assert!(matches!(result, SearchResult::Match(id) if id == id_a));

        let result = state.search(
            &tokens_b,
            &SearchConfig {
                max_children: 100,
                sim_thresh: 0.4,
                parametrize_numeric: true,
                depth: 5,
                typed_placeholders: false,
            },
            &[],
        );
        assert!(matches!(result, SearchResult::Match(id) if id == id_b));
    }

    #[test]
    fn drain_state_new_cluster() {
        let mut state = DrainState::new();
        let tokens = make_tokens(&["worker", "started"]);
        let path = DrainKey::route_path_from_tokens(&tokens, 4, true, false);
        let id = state.add_cluster(tokens.clone(), path);
        assert_eq!(id, 0);
        assert_eq!(state.cluster_count(), 1);
        assert_eq!(state.cluster(0).log_template, tokens);
    }

    #[test]
    fn drain_state_update_template() {
        let mut state = DrainState::new();
        let tokens = make_tokens(&["worker", "started"]);
        let path = DrainKey::route_path_from_tokens(&tokens, 4, true, false);
        let id = state.add_cluster(tokens, path);
        state.update_cluster_template_for_line(
            id,
            make_template(&["worker", "<*>"]),
            0,
            &[],
            false,
        );
        assert_eq!(
            state.cluster(0).log_template,
            make_tokens(&["worker", "<*>"])
        );
    }

    #[test]
    fn drain_state_route_path_immutable() {
        let mut state = DrainState::new();
        let tokens = make_tokens(&["worker", "started"]);
        let path = DrainKey::route_path_from_tokens(&tokens, 4, true, false);
        let original_path = path.clone();
        let id = state.add_cluster(tokens, path);

        state.update_cluster_template_for_line(
            id,
            make_template(&["worker", "<*>"]),
            0,
            &[],
            false,
        );
        assert_eq!(state.cluster(id).route_path, original_path);
    }

    #[test]
    fn route_path_reflects_deep_max_children_fallback() {
        let mut state = DrainState::new();
        let tokens_a = make_tokens(&["root", "branch", "alpha", "tail"]);
        let tokens_b = make_tokens(&["root", "branch", "beta", "tail"]);

        let result_a = state.search(
            &tokens_a,
            &SearchConfig {
                max_children: 1,
                sim_thresh: 0.4,
                parametrize_numeric: true,
                depth: 6,
                typed_placeholders: false,
            },
            &[],
        );
        let path_a = match result_a {
            SearchResult::NewCluster(path) => path,
            _ => panic!("expected NewCluster"),
        };
        assert_eq!(path_a[3], DrainKey::Literal("alpha".to_string()));
        let id_a = state.add_cluster(tokens_a.clone(), path_a);

        let result_b = state.search(
            &tokens_b,
            &SearchConfig {
                max_children: 1,
                sim_thresh: 0.4,
                parametrize_numeric: true,
                depth: 6,
                typed_placeholders: false,
            },
            &[],
        );
        let path_b = match result_b {
            SearchResult::NewCluster(path) => path,
            _ => panic!("expected NewCluster"),
        };
        assert_eq!(path_b[3], DrainKey::Wildcard);
        let id_b = state.add_cluster(tokens_b.clone(), path_b.clone());

        assert_ne!(id_a, id_b);
        assert_eq!(state.cluster(id_b).route_path, path_b);
        assert_eq!(state.cluster(id_b).route_path.len(), 4);
    }

    #[test]
    fn tie_break_prefers_more_wildcards_then_lowest_cluster_id() {
        let mut state = DrainState::new();
        let path = vec![DrainKey::TokenCount(3)];
        let specific = state.add_cluster(make_tokens(&["a", "b", "x"]), path.clone());
        let generalized = state.add_cluster(make_tokens(&["a", "<*>", "c"]), path);

        let log = make_tokens(&["a", "b", "c"]);
        let result = state.search(
            &log,
            &SearchConfig {
                max_children: 100,
                sim_thresh: 0.0,
                parametrize_numeric: true,
                depth: 3,
                typed_placeholders: false,
            },
            &[],
        );
        assert!(matches!(result, SearchResult::Match(id) if id == generalized));
        assert_ne!(specific, generalized);
    }

    #[test]
    fn typed_mask_join_downgrades_conflict_to_untyped() {
        // Defensive: if a typed slot ever observed a conflicting kind (the lazy
        // gate prevents this in practice), the join rule downgrades it to
        // untyped rather than inventing a kind.
        let mut state = DrainState::new();
        let path = DrainKey::route_path_from_tokens(
            &["worker".into(), "42".into(), "started".into()],
            4,
            true,
            true,
        );
        let id = state.add_cluster_for_line(
            vec!["worker".into(), "42".into(), "started".into()],
            path,
            1,
        );
        state.set_cluster_mask(id, &[None, Some(PlaceholderKind::Number), None]);
        assert_eq!(
            state.cluster(id).placeholder_mask[1],
            Some(PlaceholderKind::Number)
        );

        // Feed a conflicting Hex observation at the typed slot.
        state.update_cluster_template_for_line(
            id,
            make_template(&["worker", "<*>", "started"]),
            2,
            // line_kinds: `0x1a` pre-classified as Hex (the conflicting observation).
            &[None, Some(PlaceholderKind::Hex), None],
            true,
        );
        assert_eq!(
            state.cluster(id).placeholder_mask[1],
            None,
            "conflicting Number+Hex must downgrade to untyped"
        );
    }
}

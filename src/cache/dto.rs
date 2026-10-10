use std::collections::BTreeMap;

use rustc_hash::FxHashMap;
use serde::{Deserialize, Serialize};

use crate::error::LogParseError;
use crate::model::{ClusterId, ConstantBucket, ConstantKey, LogCluster, ParseState, Token};
use crate::placeholder::PlaceholderMask;
use crate::preprocessor::{PreprocessPlan, RuleId};

/// Bump when the on-disk DTO layout changes incompatibly.
pub const CACHE_STATE_VERSION: u32 = 4;

/// Errors raised while converting a [`CacheState`] back into a [`ParseState`].
///
/// Library consumers can match on these variants independently of
/// `LogParseError`; a blanket `From<CacheStateError> for LogParseError` is
/// provided so the manager can use `?` to propagate.
#[derive(Debug, thiserror::Error)]
#[non_exhaustive]
pub enum CacheStateError {
    /// The cache file version is not the exact state version supported by this crate.
    #[error("unsupported cache state version: {found} (expected {expected})")]
    UnsupportedVersion {
        /// Version found in the file.
        found: u32,
        /// Version expected by this crate.
        expected: u32,
    },

    /// `constant_order` references a bucket that does not exist.
    #[error("constant_order key missing from buckets map: {key:?}")]
    ConstantOrderKeyMissing {
        /// The missing key.
        key: ConstantKey,
    },

    /// A bucket references a cluster id not present in `clusters`.
    #[error("bucket refers to unknown cluster id: {cluster_id}")]
    UnknownClusterId {
        /// The unknown cluster id.
        cluster_id: ClusterId,
    },

    /// A cluster id in `bucket.cluster_ids` is out of range.
    #[error("bucket.cluster_ids out of range: {cluster_id} >= {len}")]
    BucketClusterOutOfRange {
        /// The out-of-range cluster id.
        cluster_id: ClusterId,
        /// Number of clusters in the cache state.
        len: usize,
    },

    /// A cluster id in `bucket.by_len` is not present in `clusters`.
    #[error("by_len references unknown cluster id: {cluster_id}")]
    ByLenUnknownCluster {
        /// The unknown cluster id.
        cluster_id: ClusterId,
    },

    /// A cluster id in `bucket.by_len` is out of range.
    #[error("by_len cluster out of range: {cluster_id} >= {len}")]
    ByLenClusterOutOfRange {
        /// The out-of-range cluster id.
        cluster_id: ClusterId,
        /// Number of clusters in the cache state.
        len: usize,
    },

    /// A cluster's placeholder mask is not aligned with its template.
    #[error("invalid placeholder mask: {message}")]
    InvalidPlaceholderMask {
        /// Human-readable invariant failure.
        message: String,
    },
}

impl From<CacheStateError> for LogParseError {
    fn from(error: CacheStateError) -> Self {
        LogParseError::InvalidCache {
            message: error.to_string(),
        }
    }
}

/// Serializable counterpart to [`ConstantKey`].
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub enum CacheConstantKey {
    /// Catch-all bucket.
    None,
    /// A specific constant token.
    Token(String),
}

impl From<&ConstantKey> for CacheConstantKey {
    fn from(value: &ConstantKey) -> Self {
        match value {
            ConstantKey::None => CacheConstantKey::None,
            ConstantKey::Token(s) => CacheConstantKey::Token(s.clone()),
        }
    }
}

impl From<CacheConstantKey> for ConstantKey {
    fn from(value: CacheConstantKey) -> Self {
        match value {
            CacheConstantKey::None => ConstantKey::None,
            CacheConstantKey::Token(s) => ConstantKey::Token(s),
        }
    }
}

/// Serializable counterpart to [`ConstantBucket`].
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct CacheConstantBucket {
    /// All cluster ids in this bucket.
    pub cluster_ids: Vec<ClusterId>,
    /// `BTreeMap` to make serialization deterministic. We rebuild the
    /// `FxHashMap` on conversion back to memory.
    pub by_len: BTreeMap<usize, Vec<ClusterId>>,
}

impl From<&ConstantBucket> for CacheConstantBucket {
    fn from(bucket: &ConstantBucket) -> Self {
        Self {
            cluster_ids: bucket.cluster_ids.clone(),
            by_len: bucket.by_len.iter().map(|(k, v)| (*k, v.clone())).collect(),
        }
    }
}

impl From<CacheConstantBucket> for ConstantBucket {
    fn from(bucket: CacheConstantBucket) -> Self {
        let mut by_len = FxHashMap::default();
        for (k, v) in bucket.by_len {
            by_len.insert(k, v);
        }
        Self {
            cluster_ids: bucket.cluster_ids,
            by_len,
        }
    }
}

/// Serializable counterpart to [`Token`].
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CacheToken {
    /// Literal prefix before the wildcard marker.
    pub head: String,
    /// Literal suffix after the wildcard marker.
    pub tail: String,
    /// `Some` for literal tokens, `None` for pure wildcards.
    pub literal: Option<String>,
}

impl From<&Token> for CacheToken {
    fn from(token: &Token) -> Self {
        match token {
            Token::Literal(value) => Self {
                head: String::new(),
                tail: String::new(),
                literal: Some(value.clone()),
            },
            Token::Wildcard { head, tail } => Self {
                head: head.clone(),
                tail: tail.clone(),
                literal: None,
            },
        }
    }
}

impl From<CacheToken> for Token {
    fn from(token: CacheToken) -> Self {
        match token.literal {
            Some(value) => Token::Literal(value),
            None => Token::Wildcard {
                head: token.head,
                tail: token.tail,
            },
        }
    }
}

/// Serializable counterpart to [`LogCluster`].
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CacheCluster {
    /// Current canonical template as cache tokens.
    pub log_template: Vec<CacheToken>,
    /// Line ids assigned to this cluster.
    pub log_ids: Vec<usize>,
    /// Observed token paths.
    pub path_list: Vec<Vec<CacheToken>>,
    /// Rendered template variants.
    pub template_list: Vec<String>,
    /// Number of times this cluster has been matched.
    pub hit_time: usize,
    /// Branching threshold.
    pub br_thresh: usize,
    /// ADR-0033 per-position placeholder kinds, aligned with `log_template`.
    /// Required since `CACHE_STATE_VERSION = 3`: old payloads without this
    /// field must fail restore instead of degrading to all-`None`.
    pub placeholder_mask: PlaceholderMask,
}

impl From<&LogCluster> for CacheCluster {
    fn from(cluster: &LogCluster) -> Self {
        Self {
            log_template: cluster.log_template.iter().map(CacheToken::from).collect(),
            log_ids: cluster.log_ids.clone(),
            path_list: cluster
                .path_list
                .iter()
                .map(|path| path.iter().map(CacheToken::from).collect())
                .collect(),
            template_list: cluster.template_list.clone(),
            hit_time: cluster.hit_time,
            br_thresh: cluster.br_thresh,
            placeholder_mask: cluster.placeholder_mask.clone(),
        }
    }
}

impl TryFrom<CacheCluster> for LogCluster {
    type Error = CacheStateError;

    fn try_from(cluster: CacheCluster) -> Result<Self, Self::Error> {
        let log_template = cluster
            .log_template
            .into_iter()
            .map(Token::from)
            .collect::<Vec<_>>();
        validate_placeholder_mask(&log_template, &cluster.placeholder_mask)?;

        Ok(Self {
            log_template,
            log_ids: cluster.log_ids,
            path_list: cluster
                .path_list
                .into_iter()
                .map(|path| path.into_iter().map(Token::from).collect())
                .collect(),
            template_list: cluster.template_list,
            hit_time: cluster.hit_time,
            br_thresh: cluster.br_thresh,
            placeholder_mask: cluster.placeholder_mask,
        })
    }
}

/// Serializable counterpart to [`PreprocessPlan`].
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CachePreprocessPlan {
    /// Enabled preprocessing rule ids.
    pub enabled_rule_ids: Vec<RuleId>,
}

impl From<&PreprocessPlan> for CachePreprocessPlan {
    fn from(plan: &PreprocessPlan) -> Self {
        Self {
            enabled_rule_ids: plan.enabled_rule_ids.clone(),
        }
    }
}

impl From<CachePreprocessPlan> for PreprocessPlan {
    fn from(plan: CachePreprocessPlan) -> Self {
        Self::new(plan.enabled_rule_ids)
    }
}

/// On-disk representation of [`ParseState`] plus the metadata required
/// for resume.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[non_exhaustive]
pub struct CacheState {
    /// DTO layout version.
    pub version: u32,
    /// Clusters in the cache.
    pub clusters: Vec<CacheCluster>,
    /// Buckets indexed by constant key.
    pub buckets: BTreeMap<CacheConstantKey, CacheConstantBucket>,
    /// Order in which constant keys were first seen.
    pub constant_order: Vec<CacheConstantKey>,
    /// Python-compatible preprocessed CRC32 template cache.
    pub template_cache: BTreeMap<u32, ClusterId>,
    /// Python-compatible digit-normalized raw CRC32 light cache.
    pub light_cache: BTreeMap<u32, Vec<ClusterId>>,
    /// Preprocess plan captured at save time.
    pub preprocess_plan: CachePreprocessPlan,
    /// Cursor into the current input's entry list. Default `0`.
    #[serde(default)]
    pub processed_line_count: u64,
    /// SHA-256 of the input path this cursor refers to. `None` when
    /// `processed_line_count == 0` or for caches written without
    /// segmentation.
    #[serde(default)]
    pub input_identity: Option<[u8; 32]>,
    /// Runtime config hash, stamped at save time. Default zero on
    /// deserialization of an older cache; populated by the manager.
    #[serde(default)]
    pub runtime_config_hash: [u8; 32],
    /// Preprocess plan hash, stamped at save time. Default zero on
    /// deserialization of an older cache; populated by the manager.
    #[serde(default)]
    pub preprocess_plan_hash: [u8; 32],
}

impl CacheState {
    /// Create a new cache state with default metadata.
    pub fn new(
        clusters: Vec<CacheCluster>,
        buckets: BTreeMap<CacheConstantKey, CacheConstantBucket>,
        constant_order: Vec<CacheConstantKey>,
        preprocess_plan: CachePreprocessPlan,
    ) -> Self {
        Self {
            version: CACHE_STATE_VERSION,
            clusters,
            buckets,
            constant_order,
            template_cache: BTreeMap::new(),
            light_cache: BTreeMap::new(),
            preprocess_plan,
            processed_line_count: 0,
            input_identity: None,
            runtime_config_hash: [0; 32],
            preprocess_plan_hash: [0; 32],
        }
    }
}

impl Default for CacheState {
    fn default() -> Self {
        Self::new(
            Vec::new(),
            BTreeMap::new(),
            Vec::new(),
            CachePreprocessPlan {
                enabled_rule_ids: Vec::new(),
            },
        )
        .with_cursor(0, None)
    }
}

impl From<&ParseState> for CacheState {
    fn from(state: &ParseState) -> Self {
        Self::from_parse_state(state, &PreprocessPlan::default(), 0, None)
    }
}

/// Bundle returned by [`CacheState::into_parse_state`] carrying the
/// parts needed to restore a parser.
#[derive(Debug, Clone)]
pub struct RestoredState {
    /// Restored parse state.
    pub state: ParseState,
    /// Restored preprocess plan.
    pub preprocess_plan: PreprocessPlan,
    /// Number of lines already processed.
    pub processed_line_count: u64,
    /// Identity of the input being processed.
    pub input_identity: Option<[u8; 32]>,
}

impl CacheState {
    /// Convert a `CacheState` into the runtime types needed by the parser.
    /// Returns a `CacheStateError` if invariants are broken.
    pub fn into_parse_state(self) -> Result<RestoredState, CacheStateError> {
        let processed = self.processed_line_count;
        let identity = self.input_identity;
        let plan = PreprocessPlan::new(self.preprocess_plan.enabled_rule_ids.clone());
        Ok(RestoredState {
            state: ParseState::try_from(self)?,
            preprocess_plan: plan,
            processed_line_count: processed,
            input_identity: identity,
        })
    }

    /// Build a `CacheState` from a runtime `ParseState`, capturing the
    /// preprocess plan, cursor, and input identity.
    pub fn from_parse_state(
        state: &ParseState,
        plan: &PreprocessPlan,
        processed_line_count: u64,
        input_identity: Option<[u8; 32]>,
    ) -> Self {
        let mut constant_order = Vec::with_capacity(state.constant_order.len());
        for key in &state.constant_order {
            constant_order.push(CacheConstantKey::from(key));
        }
        let mut buckets = BTreeMap::new();
        for (key, bucket) in &state.buckets {
            buckets.insert(
                CacheConstantKey::from(key),
                CacheConstantBucket::from(bucket),
            );
        }
        let clusters: Vec<CacheCluster> = state.clusters.iter().map(CacheCluster::from).collect();
        let mut cache = Self::new(
            clusters,
            buckets,
            constant_order,
            CachePreprocessPlan {
                enabled_rule_ids: plan.enabled_rule_ids.clone(),
            },
        );
        cache.template_cache = state.template_cache.iter().map(|(k, v)| (*k, *v)).collect();
        cache.light_cache = state
            .light_cache
            .iter()
            .map(|(k, v)| (*k, v.clone()))
            .collect();
        cache.with_cursor(processed_line_count, input_identity)
    }
}

impl TryFrom<CacheState> for ParseState {
    type Error = CacheStateError;

    fn try_from(state: CacheState) -> Result<Self, Self::Error> {
        if state.version != CACHE_STATE_VERSION {
            return Err(CacheStateError::UnsupportedVersion {
                found: state.version,
                expected: CACHE_STATE_VERSION,
            });
        }

        let mut buckets = FxHashMap::default();
        for (key, bucket) in &state.buckets {
            let constant_key = ConstantKey::from(key.clone());
            let mut by_len = FxHashMap::default();
            for (len, ids) in &bucket.by_len {
                for &id in ids {
                    if id >= state.clusters.len() {
                        return Err(CacheStateError::ByLenClusterOutOfRange {
                            cluster_id: id,
                            len: state.clusters.len(),
                        });
                    }
                }
                by_len.insert(*len, ids.clone());
            }
            for &id in &bucket.cluster_ids {
                if id >= state.clusters.len() {
                    return Err(CacheStateError::BucketClusterOutOfRange {
                        cluster_id: id,
                        len: state.clusters.len(),
                    });
                }
            }
            buckets.insert(constant_key, ConstantBucket::from(bucket.clone()));
        }

        for key in &state.constant_order {
            if !buckets.contains_key(&ConstantKey::from(key.clone())) {
                return Err(CacheStateError::ConstantOrderKeyMissing {
                    key: ConstantKey::from(key.clone()),
                });
            }
        }

        let mut clusters = Vec::with_capacity(state.clusters.len());
        for cluster in state.clusters {
            let log_cluster = LogCluster::try_from(cluster)?;
            clusters.push(log_cluster);
        }

        let constant_order: Vec<ConstantKey> = state
            .constant_order
            .into_iter()
            .map(ConstantKey::from)
            .collect();

        let _preprocess_plan: PreprocessPlan = state.preprocess_plan.into();

        for &cluster_id in state.template_cache.values() {
            if cluster_id >= clusters.len() {
                return Err(CacheStateError::UnknownClusterId { cluster_id });
            }
        }
        for cluster_ids in state.light_cache.values() {
            for &cluster_id in cluster_ids {
                if cluster_id >= clusters.len() {
                    return Err(CacheStateError::UnknownClusterId { cluster_id });
                }
            }
        }

        let mut restored = Self {
            clusters,
            buckets,
            constant_order,
            constant_lookup: FxHashMap::default(),
            exact_index: FxHashMap::default(),
            template_cache: state.template_cache.into_iter().collect(),
            light_cache: state.light_cache.into_iter().collect(),
        };
        restored.rebuild_derived_indexes();
        Ok(restored)
    }
}

fn validate_placeholder_mask(
    log_template: &[Token],
    placeholder_mask: &PlaceholderMask,
) -> Result<(), CacheStateError> {
    if placeholder_mask.len() != log_template.len() {
        return Err(CacheStateError::InvalidPlaceholderMask {
            message: format!(
                "mask len {} does not match template len {}",
                placeholder_mask.len(),
                log_template.len()
            ),
        });
    }

    for (index, (token, kind)) in log_template.iter().zip(placeholder_mask).enumerate() {
        if kind.is_some() && !matches!(token, Token::Wildcard { .. }) {
            return Err(CacheStateError::InvalidPlaceholderMask {
                message: format!("mask position {index} is typed but template token is literal"),
            });
        }
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_state() -> ParseState {
        let mut state = ParseState::new();
        let cluster = LogCluster::new(vec![Token::literal("hello"), Token::literal("world")], 1, 5);
        state.add_cluster(cluster);
        state
    }

    #[test]
    fn cache_state_default_values() {
        let state = CacheState::default();
        assert_eq!(state.version, CACHE_STATE_VERSION);
        assert_eq!(state.processed_line_count, 0);
        assert!(state.input_identity.is_none());
    }

    #[test]
    fn parse_state_to_cache_state_preserves_clusters() {
        let state = make_state();
        let cache: CacheState = (&state).into();
        assert_eq!(cache.clusters.len(), state.clusters.len());
        assert!(cache.buckets.contains_key(&CacheConstantKey::None));
        assert!(!cache.constant_order.is_empty());
    }

    #[test]
    fn cache_state_to_parse_state_round_trip() {
        let mut original = make_state();
        original.template_cache.insert(7, 0);
        original.light_cache.insert(11, vec![0]);
        let cache: CacheState = (&original).into();
        let restored: ParseState = cache.try_into().expect("round-trip should succeed");
        assert_eq!(restored.clusters.len(), original.clusters.len());
        assert_eq!(restored.constant_order, original.constant_order);
        assert_eq!(restored.template_cache, original.template_cache);
        assert_eq!(restored.light_cache, original.light_cache);
        assert_eq!(
            restored
                .bucket(&ConstantKey::None)
                .map(|b| b.cluster_ids.clone()),
            original
                .bucket(&ConstantKey::None)
                .map(|b| b.cluster_ids.clone()),
        );
    }

    #[test]
    fn cache_state_unsupported_version_is_error() {
        let cache = CacheState {
            version: 99,
            ..Default::default()
        };
        let error = ParseState::try_from(cache).unwrap_err();
        assert!(matches!(
            error,
            CacheStateError::UnsupportedVersion { found: 99, .. }
        ));
    }

    #[test]
    fn old_cache_state_version_is_error() {
        let cache = CacheState {
            version: CACHE_STATE_VERSION - 1,
            ..Default::default()
        };
        let error = ParseState::try_from(cache).unwrap_err();
        assert!(matches!(error, CacheStateError::UnsupportedVersion { .. }));
    }

    #[test]
    fn placeholder_mask_len_mismatch_is_error() {
        let mut cache: CacheState = (&make_state()).into();
        cache.clusters[0].placeholder_mask.pop();
        let error = ParseState::try_from(cache).unwrap_err();
        assert!(matches!(
            error,
            CacheStateError::InvalidPlaceholderMask { .. }
        ));
    }

    #[test]
    fn placeholder_mask_kind_on_literal_is_error() {
        let mut cache: CacheState = (&make_state()).into();
        cache.clusters[0].placeholder_mask[0] = Some(crate::placeholder::PlaceholderKind::Number);
        let error = ParseState::try_from(cache).unwrap_err();
        assert!(matches!(
            error,
            CacheStateError::InvalidPlaceholderMask { .. }
        ));
    }

    #[test]
    fn constant_order_with_missing_bucket_is_error() {
        let mut cache = CacheState::default();
        cache
            .constant_order
            .push(CacheConstantKey::Token("ghost".into()));
        let error = ParseState::try_from(cache).unwrap_err();
        assert!(matches!(
            error,
            CacheStateError::ConstantOrderKeyMissing { .. }
        ));
    }

    #[test]
    fn bucket_with_out_of_range_cluster_is_error() {
        let mut cache = CacheState::default();
        cache.buckets.insert(
            CacheConstantKey::None,
            CacheConstantBucket {
                cluster_ids: vec![42],
                by_len: BTreeMap::new(),
            },
        );
        cache.constant_order.push(CacheConstantKey::None);
        let error = ParseState::try_from(cache).unwrap_err();
        assert!(matches!(
            error,
            CacheStateError::BucketClusterOutOfRange { cluster_id: 42, .. }
        ));
    }

    #[test]
    fn bincode_round_trip_is_identical() {
        let cache: CacheState = (&make_state()).into();
        let bytes =
            bincode::serde::encode_to_vec(&cache, bincode::config::standard()).expect("serialize");
        let restored: CacheState =
            bincode::serde::decode_from_slice(&bytes, bincode::config::standard())
                .expect("deserialize")
                .0;
        assert_eq!(restored.version, cache.version);
        assert_eq!(restored.clusters.len(), cache.clusters.len());
        assert_eq!(restored.constant_order, cache.constant_order);
    }

    #[test]
    fn cache_state_error_converts_to_log_parse_error() {
        let error = CacheStateError::UnsupportedVersion {
            found: 99,
            expected: CACHE_STATE_VERSION,
        };
        let piplup: LogParseError = error.into();
        match piplup {
            LogParseError::InvalidCache { message } => {
                assert!(message.contains("99"));
            }
            other => panic!("expected InvalidCache, got {other:?}"),
        }
    }
}

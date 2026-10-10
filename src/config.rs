//! Configuration types for the piplup parser.
//!
//! This module defines the top-level [`PiplupConfig`] struct and the
//! similarity-threshold strategy [`SimThresh`] and its concrete expansion
//! [`SimThresholds`].

use std::path::PathBuf;

use serde::{Deserialize, Serialize};

use crate::index::PREPROCESS_RULES_VERSION;

/// Version of the PIPLUP matching/template semantics. Bump whenever scoring or
/// merge behavior changes so caches and indexes trained under older
/// semantics are rejected instead of silently reused.
pub(crate) const MATCHING_SEMANTICS_VERSION: u32 = 4;

/// Version of the ADR-0033 typed-placeholder metadata pipeline.
pub(crate) const PLACEHOLDER_PIPELINE_VERSION: u32 = 1;

const fn default_true() -> bool {
    true
}

/// Version of the placeholder classifier predicate set.
pub(crate) const PLACEHOLDER_CLASSIFIER_VERSION: u32 = 2;

/// Strategy used to pick a similarity threshold for template matching.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum SimThresh {
    /// Use length-dependent thresholds tuned for the piplup algorithm.
    Dynamic,
    /// Use a single fixed threshold for all sequence lengths.
    Fixed(f64),
}

/// Per-length similarity thresholds used by the piplup matcher.
///
/// The piplup algorithm compares token sequences of varying lengths, and the
/// appropriate similarity threshold depends on that length. `SimThresholds`
/// stores a threshold for each length in `1..=8` plus a fallback for longer
/// sequences.
#[derive(Debug, Clone, PartialEq)]
pub struct SimThresholds {
    thresholds: [f64; 8],
    others: f64,
}

impl SimThresholds {
    /// Return the default dynamic thresholds.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::SimThresholds;
    ///
    /// let thresholds = SimThresholds::dynamic();
    /// assert!((thresholds.get(3) - 2.0 / 3.0).abs() < 1e-9);
    /// ```
    pub fn dynamic() -> Self {
        Self {
            thresholds: [1.0, 0.5, 2.0 / 3.0, 0.5, 0.6, 0.5, 4.0 / 7.0, 0.625],
            others: 0.5,
        }
    }

    /// Return thresholds that use `value` for every sequence length.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::SimThresholds;
    ///
    /// let thresholds = SimThresholds::fixed(0.75);
    /// assert!((thresholds.get(1) - 0.75).abs() < 1e-9);
    /// assert!((thresholds.get(100) - 0.75).abs() < 1e-9);
    /// ```
    pub fn fixed(value: f64) -> Self {
        Self {
            thresholds: [value; 8],
            others: value,
        }
    }

    /// Convert a [`SimThresh`] mode into concrete thresholds.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::{SimThresh, SimThresholds};
    ///
    /// let dynamic = SimThresholds::from_mode(&SimThresh::Dynamic);
    /// let fixed = SimThresholds::from_mode(&SimThresh::Fixed(0.8));
    /// assert!((dynamic.get(2) - 0.5).abs() < 1e-9);
    /// assert!((fixed.get(2) - 0.8).abs() < 1e-9);
    /// ```
    pub fn from_mode(mode: &SimThresh) -> Self {
        match mode {
            SimThresh::Dynamic => Self::dynamic(),
            SimThresh::Fixed(value) => Self::fixed(*value),
        }
    }

    /// Return the threshold for a token sequence of length `len`.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::SimThresholds;
    ///
    /// let thresholds = SimThresholds::dynamic();
    /// assert!((thresholds.get(3) - 2.0 / 3.0).abs() < 1e-9);
    /// assert!((thresholds.get(20) - 0.5).abs() < 1e-9);
    /// ```
    pub fn get(&self, len: usize) -> f64 {
        if (1..=8).contains(&len) {
            self.thresholds[len - 1]
        } else {
            self.others
        }
    }
}

/// Top-level configuration for the piplup parser.
///
/// `PiplupConfig` is serialized from CLI arguments or a configuration file and
/// drives training, parsing, and caching behavior. It is also hashed to guard
/// cache and index compatibility.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PiplupConfig {
    /// Log-format strings describing how to split each line into named fields.
    ///
    /// Each string must contain a `<Content>` field; other fields are copied
    /// through to structured output.
    pub log_format: Vec<String>,
    /// Directory containing input log files when training from disk.
    pub input_dir: PathBuf,
    /// Directory where structured output is written.
    pub output_dir: PathBuf,
    /// Maximum number of cached prefix-tree search results per cluster.
    pub hit_limit: usize,
    /// Branching threshold: maximum number of children a prefix-tree node may
    /// have before a new cluster is created.
    pub br_thresh: usize,
    /// Similarity-threshold strategy for matching a line to a template.
    pub sim_thresh: SimThresh,
    /// Whether to run the preprocessor over `<Content>` before parsing.
    pub preprocess: bool,
    /// Whether to merge constant tokens across clusters after training.
    pub merge: bool,
    /// Enable TIG-PIPLUP's two-level CRC32 template caches.
    #[serde(default = "default_true")]
    pub use_template_cache: bool,
    /// Whether to preserve parameterized tokens in template output.
    pub keep_para: bool,
    /// Emit warnings when a line cannot be matched to any template.
    pub warn_miss: bool,
    /// Skip lines that do not match any known template instead of failing.
    pub skip_unmatched: bool,
    /// When set, segment the input into N-entry processing windows. Independent
    /// of cache: works with stdin and without `--cache`. Segment boundaries
    /// control temporary allocation release and progress reporting cadence
    /// only — they do not trigger any I/O. `None` or `0` disables segmentation.
    #[serde(default)]
    pub segment_lines: Option<usize>,
    /// When set, write a cache checkpoint every M entries during training.
    /// Requires `cache_path` to be set. Checkpoint trigger is per-line inside
    /// the inner loop (independent of segment cadence). `None` or `0` disables
    /// intermediate checkpoints — only the end-of-training save is emitted.
    #[serde(default)]
    pub cache_checkpoint_lines: Option<usize>,
    /// Path to the cache file. When `None`, caching is disabled.
    #[serde(default)]
    pub cache_path: Option<PathBuf>,
    /// Compress cache payload with LZ4.
    #[serde(default)]
    pub cache_compress: bool,
    /// ADR-0033: when `true`, classify digit/address/path-shaped tokens as
    /// `PlaceholderKind` and match them lazily by kind (see `src/placeholder.rs`)
    /// instead of eagerly wildcarding them via the regex catalog. Typed mode
    /// SKIPS the eager regex rules whose shapes the classifier owns
    /// (`TYPED_OWNED_RULE_IDS`) via `Preprocessor::estimate_line_excluding`.
    /// When `false`, behavior is byte-identical to the legacy pipeline.
    #[serde(default)]
    pub typed_placeholders: bool,
    /// When set, filter tokens longer than this many bytes out of the token
    /// sequence before clustering. A line whose tokens are all filtered out
    /// (empty token sequence) is excluded from clustering — it is still
    /// emitted in output with the `"0"` event_id sentinel. `None` disables
    /// the filter (legacy behavior).
    #[serde(default)]
    pub max_token_bytes: Option<usize>,
    /// When set, exclude a line from clustering when its preprocessed content
    /// exceeds this many bytes. The line is still emitted in output with the
    /// `"0"` event_id sentinel. `None` disables the filter (legacy behavior).
    #[serde(default)]
    pub max_line_bytes: Option<usize>,
    /// ADR-0046: caller-supplied separators, treated as whitespace-
    /// equivalent token boundaries. Each matched separator is replaced with
    /// one space after preprocessing and dropped by tokenization, so
    /// separator-bearing logs cluster exactly like their space-written
    /// equivalents. Empty (the default) preserves existing tokenization.
    /// Facade-only in this phase; not exposed via CLI.
    #[serde(default)]
    pub separators: Vec<String>,
}

impl Default for PiplupConfig {
    fn default() -> Self {
        Self {
            log_format: vec!["<Date> <Time> <Content>".to_string()],
            input_dir: PathBuf::from("."),
            output_dir: PathBuf::from("./result"),
            hit_limit: 385,
            br_thresh: 5,
            sim_thresh: SimThresh::Dynamic,
            preprocess: true,
            merge: true,
            use_template_cache: true,
            keep_para: false,
            warn_miss: false,
            skip_unmatched: false,
            segment_lines: None,
            cache_checkpoint_lines: None,
            cache_path: None,
            cache_compress: false,
            typed_placeholders: false,
            max_token_bytes: None,
            max_line_bytes: None,
            separators: Vec::new(),
        }
    }
}

impl PiplupConfig {
    /// Compute a deterministic SHA-256 over the fields that affect parse
    /// determinism. Used for cache compatibility metadata.
    ///
    /// SYNC: when adding a new field to `PiplupConfig` that affects
    /// parse output, include it here and add a unit test in
    /// `tests/cache_integration.rs`.
    pub fn compute_runtime_hash(&self) -> [u8; 32] {
        use sha2::{Digest, Sha256};
        let seed = self.runtime_hash_seed();
        Sha256::digest(seed.as_bytes()).into()
    }

    pub(crate) fn runtime_hash_seed(&self) -> String {
        let mut seed = String::new();
        seed.push_str(&format!("log_format={:?};", self.log_format));
        seed.push_str(&format!("hit_limit={};", self.hit_limit));
        seed.push_str(&format!("br_thresh={};", self.br_thresh));
        seed.push_str(&format!("sim_thresh={:?};", self.sim_thresh));
        seed.push_str(&format!("preprocess={};", self.preprocess));
        seed.push_str(&format!(
            "preprocess_rules_version={};",
            PREPROCESS_RULES_VERSION
        ));
        // ADR-0047/ADR-0048: partial-credit scoring and structure-aware
        // merging change template semantics for identical configs; caches
        // and indexes trained under older semantics must not be reused.
        seed.push_str(&format!(
            "matching_semantics_version={MATCHING_SEMANTICS_VERSION};"
        ));
        seed.push_str(&format!("merge={};", self.merge));
        seed.push_str(&format!("use_template_cache={};", self.use_template_cache));
        seed.push_str(&format!("skip_unmatched={};", self.skip_unmatched));
        seed.push_str(&format!("typed_placeholders={};", self.typed_placeholders));
        seed.push_str(&format!(
            "placeholder_pipeline_version={};",
            PLACEHOLDER_PIPELINE_VERSION
        ));
        seed.push_str(&format!(
            "placeholder_classifier_version={};",
            PLACEHOLDER_CLASSIFIER_VERSION
        ));
        seed.push_str(&format!("max_token_bytes={:?};", self.max_token_bytes));
        seed.push_str(&format!("max_line_bytes={:?};", self.max_line_bytes));
        // ADR-0046: mix in separators only when non-empty, so default
        // configurations keep byte-identical hashes (cache/checkpoint
        // compat). Each separator is length-prefixed as `{byte_len}:{bytes},`
        // — a self-describing textual framing where the byte length fixes
        // each entry's extent, mirroring the ADR-0042 `extra_delimiters`
        // length-prefix stance. A plain separator-less join would make
        // `["a","b"]` collide with `["a,b"]`; the length prefix keeps the
        // encoding unambiguous even for bytes validation permits (e.g. NUL).
        if !self.separators.is_empty() {
            seed.push_str("separators=");
            for separator in &self.separators {
                seed.push_str(&format!("{}:{},", separator.len(), separator));
            }
        }
        seed
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_clustering_length_filters_are_none() {
        let config = PiplupConfig::default();
        assert_eq!(config.max_token_bytes, None);
        assert_eq!(config.max_line_bytes, None);
    }

    #[test]
    fn runtime_hash_seed_includes_clustering_length_filters() {
        let config = PiplupConfig::default();
        let seed = config.runtime_hash_seed();
        assert!(seed.contains("max_token_bytes=None;"));
        assert!(seed.contains("max_line_bytes=None;"));

        let with_filters = PiplupConfig {
            max_token_bytes: Some(64),
            max_line_bytes: Some(512),
            ..config.clone()
        };
        let filtered_seed = with_filters.runtime_hash_seed();
        assert!(filtered_seed.contains("max_token_bytes=Some(64);"));
        assert!(filtered_seed.contains("max_line_bytes=Some(512);"));
    }

    #[test]
    fn default_separators_are_empty() {
        let config = PiplupConfig::default();
        assert!(config.separators.is_empty());
    }

    #[test]
    fn runtime_hash_seed_excludes_separators_when_empty() {
        // ADR-0046: default configurations must keep byte-identical hashes.
        let config = PiplupConfig::default();
        assert!(!config.runtime_hash_seed().contains("separators="));

        let explicit_empty = PiplupConfig {
            separators: Vec::new(),
            ..config.clone()
        };
        assert_eq!(
            config.compute_runtime_hash(),
            explicit_empty.compute_runtime_hash()
        );
    }

    #[test]
    fn runtime_hash_seed_includes_separators_when_non_empty() {
        let config = PiplupConfig {
            separators: vec![",".to_string()],
            ..PiplupConfig::default()
        };
        // One comma is 1 byte: `separators=1:,`.
        assert!(config.runtime_hash_seed().contains("separators=1:,"));
    }

    #[test]
    fn runtime_hash_differs_between_separator_lists() {
        let default = PiplupConfig::default();
        let comma = PiplupConfig {
            separators: vec![",".to_string()],
            ..default.clone()
        };
        let full_width = PiplupConfig {
            separators: vec!["，".to_string()],
            ..default.clone()
        };
        assert_ne!(
            default.compute_runtime_hash(),
            comma.compute_runtime_hash(),
            "separator-trained caches must not be reused by a default config"
        );
        assert_ne!(
            comma.compute_runtime_hash(),
            full_width.compute_runtime_hash(),
            "caches trained under different separator lists must not be shared"
        );
    }

    #[test]
    fn runtime_hash_separator_encoding_is_unambiguous() {
        // ADR-0046 §6: the length-prefixed encoding must distinguish lists
        // that a naive join would collide: `["a","b"]` vs `["a\0b"]`.
        let two = PiplupConfig {
            separators: vec!["a".to_string(), "b".to_string()],
            ..PiplupConfig::default()
        };
        let one_nul = PiplupConfig {
            separators: vec!["a\0b".to_string()],
            ..PiplupConfig::default()
        };
        assert_ne!(
            two.compute_runtime_hash(),
            one_nul.compute_runtime_hash(),
            "separator lists `a`+`b` and `a\\0b` must not collide"
        );
    }
}

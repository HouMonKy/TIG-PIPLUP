use std::path::PathBuf;

use serde::{Deserialize, Serialize};

use crate::{
    config::{PLACEHOLDER_CLASSIFIER_VERSION, PLACEHOLDER_PIPELINE_VERSION},
    error::LogParseError,
    index::PREPROCESS_RULES_VERSION,
};

fn default_minhash_neighborhood() -> usize {
    5
}
fn default_minhash_threshold() -> f64 {
    0.5
}
fn default_minhash_signature_size() -> usize {
    128
}

/// Drain3 algorithm configuration.
///
/// Defaults match ADR-0028 section 2.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Drain3Config {
    /// Log format patterns for parsing raw lines.
    pub log_format: Vec<String>,
    /// Input directory for resolving file paths.
    pub input_dir: PathBuf,
    /// Output directory for writing artifacts.
    pub output_dir: PathBuf,
    /// Fixed-depth parse tree depth. Minimum 3.
    pub depth: usize,
    /// Cluster acceptance threshold, range `0.0..=1.0`.
    pub sim_thresh: f64,
    /// Maximum literal children per internal node before routing to `<*>`.
    pub max_children: usize,
    /// When `true`, tokens containing an ASCII digit are routed through the
    /// wildcard child and written as `<*>` in a new cluster's initial template.
    pub parametrize_numeric_tokens: bool,
    /// ADR-0033: when `true`, classify digit-bearing tokens as `Number`/`Hex`/`Id`
    /// and match them lazily by kind (see `src/placeholder.rs`) instead of
    /// eagerly wildcarding them via `parametrize_numeric_tokens`. Typed slots
    /// still render as `<*>`; routing branches by kind. When `false`, behavior
    /// is byte-identical to ADR-0028. Cache format version 2 persists the
    /// placeholder mask and typed route keys, so typed checkpoints restore with
    /// kind metadata intact.
    #[serde(default)]
    pub typed_placeholders: bool,
    /// ADR-0033 改良②: when `true`, tokenize by splitting on common log delimiters
    /// (`: . , ; " / = ( )`) in addition to whitespace, so composite tokens like
    /// `10.0.0.1` or `pam_unix(sshd:auth):` break into classifiable sub-tokens
    /// (`10 . 0 . 0 . 1`). When `false`, plain `split_whitespace` (ADR-0028).
    #[serde(default)]
    pub delimiter_split: bool,
    /// ADR-0042: caller-supplied extra delimiters, matched as substrings
    /// (leftmost, non-overlapping, longest-first) and kept as standalone
    /// tokens, independently of `delimiter_split`. Empty preserves existing
    /// tokenization. Facade-only in this phase; not exposed via CLI.
    #[serde(default)]
    pub extra_delimiters: Vec<String>,
    /// ADR-0033 改良③: when `true`, fall back to minhash Jaccard over a
    /// token-count neighborhood when same-length similarity finds no match,
    /// enabling variable-length log clustering. When `false`, same-length only
    /// (ADR-0028).
    #[serde(default)]
    pub variable_length_clustering: bool,
    /// Token-count neighborhood radius for the minhash fallback.
    #[serde(default = "default_minhash_neighborhood")]
    pub minhash_length_neighborhood: usize,
    /// Estimated-Jaccard threshold for accepting a variable-length match.
    #[serde(default = "default_minhash_threshold")]
    pub minhash_jaccard_threshold: f64,
    /// Number of minhash permutations (signature length).
    #[serde(default = "default_minhash_signature_size")]
    pub minhash_signature_size: usize,
    /// Enable preprocessing of log lines.
    pub preprocess: bool,
    /// Keep paragraph content in preprocessed output.
    pub keep_para: bool,
    /// Emit warnings for unmatched lines.
    pub warn_miss: bool,
    /// Skip unmatched lines in output.
    pub skip_unmatched: bool,
    /// When set, segment the input into N-entry processing windows.
    #[serde(default)]
    pub segment_lines: Option<usize>,
    /// When set, write a cache checkpoint every M entries during training.
    /// Requires `cache_path` to be set.
    #[serde(default)]
    pub cache_checkpoint_lines: Option<usize>,
    /// Path to the cache file. When `None`, caching is disabled.
    #[serde(default)]
    pub cache_path: Option<PathBuf>,
    /// Compress cache payload with LZ4.
    #[serde(default)]
    pub cache_compress: bool,
    /// Maximum content byte length for clustering. Lines whose preprocessed
    /// content exceeds this length are excluded from clustering, receiving
    /// event_id = "0" in the output.
    #[serde(default)]
    pub max_line_bytes: Option<usize>,
    /// Maximum token byte length. Tokens longer than this are filtered out
    /// before clustering. If all tokens are filtered, the line is excluded
    /// from clustering (handled by the existing empty-tokens guard).
    #[serde(default)]
    pub max_token_bytes: Option<usize>,
}

impl Default for Drain3Config {
    fn default() -> Self {
        Self {
            log_format: vec!["<Date> <Time> <Content>".to_string()],
            input_dir: PathBuf::from("."),
            output_dir: PathBuf::from("./result"),
            depth: 4,
            sim_thresh: 0.4,
            max_children: 100,
            parametrize_numeric_tokens: true,
            typed_placeholders: false,
            delimiter_split: false,
            extra_delimiters: Vec::new(),
            variable_length_clustering: false,
            minhash_length_neighborhood: default_minhash_neighborhood(),
            minhash_jaccard_threshold: default_minhash_threshold(),
            minhash_signature_size: default_minhash_signature_size(),
            preprocess: true,
            keep_para: false,
            warn_miss: false,
            skip_unmatched: false,
            segment_lines: None,
            cache_checkpoint_lines: None,
            cache_path: None,
            cache_compress: false,
            max_line_bytes: None,
            max_token_bytes: None,
        }
    }
}

impl Drain3Config {
    /// Validate the configuration, returning an error if any field is invalid.
    pub fn validate(&self) -> Result<(), LogParseError> {
        if self.depth < 3 {
            return Err(LogParseError::InvalidAlgorithmConfig {
                algorithm: "drain3",
                field: "depth",
                message: format!("must be at least 3, got {}", self.depth),
            });
        }
        if !(0.0..=1.0).contains(&self.sim_thresh) {
            return Err(LogParseError::InvalidAlgorithmConfig {
                algorithm: "drain3",
                field: "sim_thresh",
                message: format!("must be in 0.0..=1.0, got {}", self.sim_thresh),
            });
        }
        if self.max_children == 0 {
            return Err(LogParseError::InvalidAlgorithmConfig {
                algorithm: "drain3",
                field: "max_children",
                message: "must be greater than 0".to_string(),
            });
        }
        if self.variable_length_clustering {
            if !(0.0..=1.0).contains(&self.minhash_jaccard_threshold) {
                return Err(LogParseError::InvalidAlgorithmConfig {
                    algorithm: "drain3",
                    field: "minhash_jaccard_threshold",
                    message: format!(
                        "must be in 0.0..=1.0, got {}",
                        self.minhash_jaccard_threshold
                    ),
                });
            }
            if self.minhash_signature_size == 0 {
                return Err(LogParseError::InvalidAlgorithmConfig {
                    algorithm: "drain3",
                    field: "minhash_signature_size",
                    message: "must be greater than 0 when variable_length_clustering is enabled"
                        .to_string(),
                });
            }
        }
        Ok(())
    }

    /// Compute a deterministic SHA-256 over the fields that affect parse
    /// determinism. Used for cache compatibility metadata.
    pub fn compute_runtime_hash(&self) -> [u8; 32] {
        use sha2::{Digest, Sha256};
        let mut hasher = Sha256::new();
        hasher.update(b"algorithm=drain3;");
        // Serialize `log_format` without relying on `Debug` so field renames
        // or `Vec` formatting changes do not affect cache compatibility.
        hasher.update(b"log_format=");
        hasher.update(self.log_format.join("\0").as_bytes());
        hasher.update(b";");
        hasher.update(format!("depth={};", self.depth).as_bytes());
        hasher.update(format!("sim_thresh={};", self.sim_thresh).as_bytes());
        hasher.update(format!("max_children={};", self.max_children).as_bytes());
        hasher.update(
            format!(
                "parametrize_numeric_tokens={};",
                self.parametrize_numeric_tokens
            )
            .as_bytes(),
        );
        hasher.update(format!("typed_placeholders={};", self.typed_placeholders).as_bytes());
        hasher.update(
            format!(
                "placeholder_pipeline_version={};placeholder_classifier_version={};",
                PLACEHOLDER_PIPELINE_VERSION, PLACEHOLDER_CLASSIFIER_VERSION
            )
            .as_bytes(),
        );
        hasher.update(format!("delimiter_split={};", self.delimiter_split).as_bytes());
        // ADR-0042: mix in extra delimiters only when non-empty, so default
        // configurations keep byte-identical hashes (cache/checkpoint compat).
        // Length-prefix each delimiter so the encoding is self-describing and
        // unambiguous without relying on validation to reject NUL: a naive
        // `\0`-join would make `["a","b"]` collide with `["a\0b"]`, violating
        // ADR-0042 §6 (configs differing only in delimiters must hash
        // differently). Defense in depth, matching token.rs's stance on
        // malformed input.
        if !self.extra_delimiters.is_empty() {
            hasher.update(b"extra_delimiters=");
            for delim in &self.extra_delimiters {
                hasher.update((delim.len() as u64).to_le_bytes());
                hasher.update(delim.as_bytes());
            }
        }
        hasher.update(
            format!(
                "variable_length_clustering={};minhash_length_neighborhood={};minhash_jaccard_threshold={};minhash_signature_size={};",
                self.variable_length_clustering,
                self.minhash_length_neighborhood,
                self.minhash_jaccard_threshold,
                self.minhash_signature_size
            )
            .as_bytes(),
        );
        hasher.update(format!("preprocess={};", self.preprocess).as_bytes());
        hasher.update(format!("preprocess_rules_version={PREPROCESS_RULES_VERSION};").as_bytes());
        hasher.update(format!("skip_unmatched={};", self.skip_unmatched).as_bytes());
        hasher.update(
            format!(
                "max_token_bytes={:?};max_line_bytes={:?};",
                self.max_token_bytes, self.max_line_bytes
            )
            .as_bytes(),
        );
        hasher.finalize().into()
    }

    /// Build the [`MinhashConfig`](crate::drain3::state::MinhashConfig) carried on
    /// `DrainState` for the variable-length fallback.
    pub fn minhash_config(&self) -> crate::drain3::state::MinhashConfig {
        crate::drain3::state::MinhashConfig {
            enabled: self.variable_length_clustering,
            length_neighborhood: self.minhash_length_neighborhood,
            jaccard_threshold: self.minhash_jaccard_threshold,
            signature_size: self.minhash_signature_size,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_matches_adr0028() {
        let config = Drain3Config::default();
        assert_eq!(config.depth, 4);
        assert_eq!(config.sim_thresh, 0.4);
        assert_eq!(config.max_children, 100);
        assert!(config.parametrize_numeric_tokens);
        assert!(!config.typed_placeholders);
        assert!(!config.delimiter_split);
        assert!(config.extra_delimiters.is_empty());
        assert!(!config.variable_length_clustering);
        assert_eq!(config.minhash_length_neighborhood, 5);
        assert_eq!(config.minhash_jaccard_threshold, 0.5);
        assert_eq!(config.minhash_signature_size, 128);
        assert_eq!(config.max_line_bytes, None);
        assert_eq!(config.max_token_bytes, None);
    }

    #[test]
    fn validate_accepts_valid_config() {
        let config = Drain3Config::default();
        assert!(config.validate().is_ok());
    }

    #[test]
    fn validate_rejects_depth_below_3() {
        let config = Drain3Config {
            depth: 2,
            ..Drain3Config::default()
        };
        let err = config.validate().unwrap_err();
        assert!(matches!(
            err,
            LogParseError::InvalidAlgorithmConfig {
                algorithm: "drain3",
                field: "depth",
                ..
            }
        ));
    }

    #[test]
    fn validate_rejects_sim_thresh_out_of_range() {
        let config = Drain3Config {
            sim_thresh: 1.5,
            ..Drain3Config::default()
        };
        let err = config.validate().unwrap_err();
        assert!(matches!(
            err,
            LogParseError::InvalidAlgorithmConfig {
                algorithm: "drain3",
                field: "sim_thresh",
                ..
            }
        ));
    }

    #[test]
    fn validate_rejects_zero_max_children() {
        let config = Drain3Config {
            max_children: 0,
            ..Drain3Config::default()
        };
        let err = config.validate().unwrap_err();
        assert!(matches!(
            err,
            LogParseError::InvalidAlgorithmConfig {
                algorithm: "drain3",
                field: "max_children",
                ..
            }
        ));
    }

    #[test]
    #[allow(deprecated)]
    fn log_parser_error_alias_compiles() {
        // Verify that deprecated aliases still compile.
        let _: crate::LogParserError = LogParseError::Unsupported("test");
        let _: crate::PiplupError = LogParseError::Unsupported("test");
    }

    #[test]
    fn runtime_hash_is_algorithm_scoped() {
        let config = Drain3Config::default();
        let hash = config.compute_runtime_hash();

        use sha2::{Digest, Sha256};
        let mut hasher = Sha256::new();
        hasher.update(b"algorithm=drain3;");
        hasher.update(b"log_format=");
        hasher.update(config.log_format.join("\0").as_bytes());
        hasher.update(b";");
        hasher.update(format!("depth={};", config.depth).as_bytes());
        hasher.update(format!("sim_thresh={};", config.sim_thresh).as_bytes());
        hasher.update(format!("max_children={};", config.max_children).as_bytes());
        hasher.update(
            format!(
                "parametrize_numeric_tokens={};",
                config.parametrize_numeric_tokens
            )
            .as_bytes(),
        );
        hasher.update(format!("typed_placeholders={};", config.typed_placeholders).as_bytes());
        hasher.update(
            format!(
                "placeholder_pipeline_version={};placeholder_classifier_version={};",
                PLACEHOLDER_PIPELINE_VERSION, PLACEHOLDER_CLASSIFIER_VERSION
            )
            .as_bytes(),
        );
        hasher.update(format!("delimiter_split={};", config.delimiter_split).as_bytes());
        hasher.update(
            format!(
                "variable_length_clustering={};minhash_length_neighborhood={};minhash_jaccard_threshold={};minhash_signature_size={};",
                config.variable_length_clustering,
                config.minhash_length_neighborhood,
                config.minhash_jaccard_threshold,
                config.minhash_signature_size
            )
            .as_bytes(),
        );
        hasher.update(format!("preprocess={};", config.preprocess).as_bytes());
        hasher.update(format!("preprocess_rules_version={PREPROCESS_RULES_VERSION};").as_bytes());
        hasher.update(format!("skip_unmatched={};", config.skip_unmatched).as_bytes());
        hasher.update(
            format!(
                "max_token_bytes={:?};max_line_bytes={:?};",
                config.max_token_bytes, config.max_line_bytes
            )
            .as_bytes(),
        );

        let expected: [u8; 32] = hasher.finalize().into();
        assert_eq!(hash, expected);
    }

    #[test]
    fn runtime_hash_differs_only_when_extra_delimiters_non_empty() {
        let default_config = Drain3Config::default();
        let with_delimiters = Drain3Config {
            extra_delimiters: vec!["||".to_string()],
            ..Drain3Config::default()
        };
        assert_ne!(
            default_config.compute_runtime_hash(),
            with_delimiters.compute_runtime_hash(),
            "configs differing only in extra_delimiters must hash differently"
        );

        let other_delimiters = Drain3Config {
            extra_delimiters: vec!["|".to_string()],
            ..Drain3Config::default()
        };
        assert_ne!(
            with_delimiters.compute_runtime_hash(),
            other_delimiters.compute_runtime_hash(),
        );
    }

    #[test]
    fn runtime_hash_extra_delimiters_encoding_is_unambiguous() {
        // ADR-0042 §6: configs differing only in delimiters must hash
        // differently. The length-prefixed encoding must distinguish lists
        // that a naive `\0`-join would collide: `["a","b"]` vs `["a\0b"]`.
        let two_delims = Drain3Config {
            extra_delimiters: vec!["a".to_string(), "b".to_string()],
            ..Drain3Config::default()
        };
        let one_nul_delim = Drain3Config {
            extra_delimiters: vec!["a\0b".to_string()],
            ..Drain3Config::default()
        };
        assert_ne!(
            two_delims.compute_runtime_hash(),
            one_nul_delim.compute_runtime_hash(),
            "delimiter lists `a`+`b` and `a\\0b` must not collide"
        );
    }

    #[test]
    fn runtime_hash_default_is_pinned() {
        // ADR-0042: an empty extra_delimiters list must not change the hash.
        // The pinned value was rotated when MATCHING_SEMANTICS_VERSION
        // entered the runtime hash seed (ADR-0047/0048).
        let hash = Drain3Config::default().compute_runtime_hash();
        assert_eq!(hex_encode(&hash), DEFAULT_RUNTIME_HASH_HEX);
    }

    fn hex_encode(bytes: &[u8]) -> String {
        bytes.iter().map(|b| format!("{b:02x}")).collect()
    }

    const DEFAULT_RUNTIME_HASH_HEX: &str =
        "9d55eece9830f6b351422a029552116fd3faed9e0354133db1da80564e783924";

    #[test]
    fn runtime_hash_rejects_pre_wp7_preprocess_rule_catalog_seed() {
        let config = Drain3Config::default();

        use sha2::{Digest, Sha256};
        let mut hasher = Sha256::new();
        hasher.update(b"algorithm=drain3;");
        hasher.update(b"log_format=");
        hasher.update(config.log_format.join("\0").as_bytes());
        hasher.update(b";");
        hasher.update(format!("depth={};", config.depth).as_bytes());
        hasher.update(format!("sim_thresh={};", config.sim_thresh).as_bytes());
        hasher.update(format!("max_children={};", config.max_children).as_bytes());
        hasher.update(
            format!(
                "parametrize_numeric_tokens={};",
                config.parametrize_numeric_tokens
            )
            .as_bytes(),
        );
        hasher.update(format!("typed_placeholders={};", config.typed_placeholders).as_bytes());
        hasher.update(
            format!(
                "placeholder_pipeline_version={};placeholder_classifier_version={};",
                PLACEHOLDER_PIPELINE_VERSION, PLACEHOLDER_CLASSIFIER_VERSION
            )
            .as_bytes(),
        );
        hasher.update(format!("delimiter_split={};", config.delimiter_split).as_bytes());
        hasher.update(
            format!(
                "variable_length_clustering={};minhash_length_neighborhood={};minhash_jaccard_threshold={};minhash_signature_size={};",
                config.variable_length_clustering,
                config.minhash_length_neighborhood,
                config.minhash_jaccard_threshold,
                config.minhash_signature_size
            )
            .as_bytes(),
        );
        hasher.update(format!("preprocess={};", config.preprocess).as_bytes());
        hasher.update(format!("skip_unmatched={};", config.skip_unmatched).as_bytes());
        let pre_wp7_hash: [u8; 32] = hasher.finalize().into();

        assert_ne!(
            config.compute_runtime_hash(),
            pre_wp7_hash,
            "drain3 runtime hash must reject caches produced before the WP7 catalog version bump"
        );
    }
}

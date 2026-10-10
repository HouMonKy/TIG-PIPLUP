use serde::{Deserialize, Serialize};

use crate::{
    PreprocessPlan,
    config::{
        PLACEHOLDER_CLASSIFIER_VERSION, PLACEHOLDER_PIPELINE_VERSION, PiplupConfig, SimThresh,
    },
    placeholder::PlaceholderKind,
};

/// Magic bytes that identify a piplup index file.
pub const INDEX_MAGIC: &[u8; 7] = b"PPLIDX1";

/// Current index file format version.
pub const INDEX_FORMAT_VERSION: u32 = 5;

/// Version of the post-processing normalization applied during index build.
pub const NORMALIZATION_VERSION: u32 = 1;

/// Version of the preprocess rule catalog used during index build.
pub const PREPROCESS_RULES_VERSION: u32 = 5;

/// Version of the tokenization scheme used during index build.
pub const TOKENIZATION_VERSION: u32 = 1;

/// Version of the parameter extraction scheme used during index build.
pub const PARAMETER_EXTRACTION_VERSION: u32 = 1;

/// Fixed-size binary header for a piplup index file.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct IndexHeader {
    /// Magic bytes identifying the file format.
    pub magic: [u8; 7],
    /// Index file format version.
    pub format_version: u32,
    /// Length of this header in bytes.
    pub header_len: u32,
    /// Length of the JSON payload in bytes.
    pub payload_len: u64,
    /// SHA-256 of the runtime compatibility metadata.
    pub runtime_config_hash: [u8; 32],
    /// SHA-256 of the template set.
    pub template_set_hash: [u8; 32],
    /// SHA-256 of the preprocess plan.
    pub preprocess_plan_hash: [u8; 32],
    /// SHA-256 of the payload bytes.
    pub checksum: [u8; 32],
}

impl IndexHeader {
    /// Serialized length of the header in bytes.
    pub const SERIALIZED_LEN: u32 = 7 + 4 + 4 + 8 + 32 + 32 + 32 + 32;
}

/// Runtime compatibility metadata stored in the index.
///
/// [`IndexedParser`] requires the runtime configuration to match these values
/// exactly, so that tokenization and parameter extraction behave the same as
/// during training.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RuntimeCompatibility {
    /// Log format strings used to parse lines.
    pub log_format: Vec<String>,
    /// Whether preprocessing was enabled during training.
    pub preprocess: bool,
    /// Normalization version.
    pub normalization_version: u32,
    /// Preprocess rule catalog version.
    pub preprocess_rules_version: u32,
    /// Tokenization version.
    pub tokenization_version: u32,
    /// Parameter extraction version.
    pub parameter_extraction_version: u32,
    /// Whether typed placeholder metadata was enabled during training.
    pub typed_placeholders: bool,
    /// Version of the typed placeholder pipeline.
    pub placeholder_pipeline_version: u32,
    /// Version of the placeholder classifier predicates.
    pub placeholder_classifier_version: u32,
    /// Caller-declared whitespace-equivalent separators active during
    /// training (ADR-0046). Serialized only when non-empty, so indexes
    /// built with default (empty) configurations are byte-identical to
    /// pre-ADR-0046 files; deserialization of old indexes yields an empty
    /// list.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub separators: Vec<String>,
}

impl RuntimeCompatibility {
    pub(crate) fn from_parts(
        log_format: Vec<String>,
        preprocess: bool,
        typed_placeholders: bool,
        separators: Vec<String>,
    ) -> Self {
        Self {
            log_format,
            preprocess,
            normalization_version: NORMALIZATION_VERSION,
            preprocess_rules_version: PREPROCESS_RULES_VERSION,
            tokenization_version: TOKENIZATION_VERSION,
            parameter_extraction_version: PARAMETER_EXTRACTION_VERSION,
            typed_placeholders,
            placeholder_pipeline_version: PLACEHOLDER_PIPELINE_VERSION,
            placeholder_classifier_version: PLACEHOLDER_CLASSIFIER_VERSION,
            separators,
        }
    }

    pub(crate) fn for_index_context(ctx: &IndexBuildContext, separators: &[String]) -> Self {
        Self::from_parts(
            ctx.log_format.clone(),
            ctx.preprocess,
            ctx.typed_placeholders,
            separators.to_vec(),
        )
    }

    pub(crate) fn for_piplup_config(config: &PiplupConfig) -> Self {
        Self::from_parts(
            config.log_format.clone(),
            config.preprocess,
            config.typed_placeholders,
            config.separators.clone(),
        )
    }
}

/// Algorithm-specific training metadata.
///
/// Each variant carries the parameters that affect template learning but do
/// **not** affect read-only parse compatibility. The `parse --index` path is
/// algorithm-independent and only consumes [`RuntimeCompatibility`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum AlgorithmTrainingMetadata {
    /// Parameters from the piplup online learner.
    Piplup {
        /// Whether template merging was enabled.
        merge: bool,
        /// Whether parameter lists were retained.
        keep_para: bool,
        /// Hit limit for constant promotion.
        hit_limit: usize,
        /// Branching threshold for new paths.
        br_thresh: usize,
        /// Similarity threshold strategy.
        sim_thresh: SimThresh,
    },
    /// Parameters from the Drain3 algorithm.
    Drain3 {
        /// Whether parameter lists were retained.
        keep_para: bool,
        /// Tree depth.
        depth: usize,
        /// Similarity threshold.
        sim_thresh: f64,
        /// Maximum children per node.
        max_children: usize,
        /// Whether numeric tokens are parameterized.
        parametrize_numeric_tokens: bool,
    },
}

/// Training metadata stored in the index payload.
///
/// The `algorithm` field replaces the former flat PIPLUP-only fields.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TrainingMetadata {
    /// Algorithm-specific training parameters.
    pub algorithm: AlgorithmTrainingMetadata,
    /// Crate version that wrote the index.
    pub parser_version: String,
    /// Number of lines used for training.
    pub line_count: usize,
}

/// Bridge from algorithm config to index metadata.
///
/// Used by [`IndexBuilder`](crate::index::IndexBuilder) to construct
/// [`RuntimeCompatibility`] and [`TrainingMetadata`] without duplicating field
/// extraction.
#[derive(Debug, Clone)]
pub struct IndexBuildContext {
    /// Log format strings.
    pub log_format: Vec<String>,
    /// Whether preprocessing is enabled.
    pub preprocess: bool,
    /// Whether typed placeholders are enabled.
    pub typed_placeholders: bool,
    /// Algorithm-specific training metadata.
    pub training: AlgorithmTrainingMetadata,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) enum TokenAtom {
    Literal { value: String },
    Wildcard { head: String, tail: String },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) enum SegmentAtom {
    Literal(String),
    Whitespace,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct TemplateEntry {
    pub(crate) template_id: u32,
    pub(crate) event_id: String,
    pub(crate) template_text: String,
    pub(crate) match_order: u32,
    pub(crate) token_atoms: Vec<TokenAtom>,
    pub(crate) segments: Vec<String>,
    pub(crate) segment_atoms: Vec<Vec<SegmentAtom>>,
    pub(crate) token_count: u16,
    pub(crate) wildcard_count: u16,
    pub(crate) literal_count: u16,
    pub(crate) anchors: Vec<String>,
    pub(crate) placeholder_mask: Vec<Option<PlaceholderKind>>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct LengthBucket {
    pub(crate) token_count: u16,
    pub(crate) template_ids: Vec<u32>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct AnchorPosting {
    pub(crate) anchor: String,
    pub(crate) template_ids: Vec<u32>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct TrainingAssignment {
    pub(crate) content_hash: [u8; 32],
    pub(crate) event_id: String,
    pub(crate) template_text: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct TrainingMiss {
    pub(crate) content_hash: [u8; 32],
}

/// In-memory representation of a complete index file.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TemplateIndexFile {
    pub(crate) header: IndexHeader,
    pub(crate) runtime: RuntimeCompatibility,
    pub(crate) training: TrainingMetadata,
    pub(crate) preprocess_plan: PreprocessPlan,
    pub(crate) templates: Vec<TemplateEntry>,
    pub(crate) length_buckets: Vec<LengthBucket>,
    pub(crate) anchor_postings: Vec<AnchorPosting>,
    pub(crate) training_assignments: Vec<TrainingAssignment>,
    pub(crate) training_misses: Vec<TrainingMiss>,
}

impl TemplateIndexFile {
    /// Return the index header.
    pub fn header(&self) -> &IndexHeader {
        &self.header
    }

    /// Return the runtime compatibility metadata.
    pub fn runtime(&self) -> &RuntimeCompatibility {
        &self.runtime
    }

    /// Return the training metadata.
    pub fn training(&self) -> &TrainingMetadata {
        &self.training
    }

    /// Return the preprocess plan stored in the index.
    pub fn preprocess_plan(&self) -> &PreprocessPlan {
        &self.preprocess_plan
    }

    /// Return the number of templates in the index.
    pub fn template_count(&self) -> usize {
        self.templates.len()
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub(crate) struct TemplateIndexPayload {
    pub runtime: RuntimeCompatibility,
    pub training: TrainingMetadata,
    pub preprocess_plan: PreprocessPlan,
    pub templates: Vec<TemplateEntry>,
    pub length_buckets: Vec<LengthBucket>,
    pub anchor_postings: Vec<AnchorPosting>,
    pub training_assignments: Vec<TrainingAssignment>,
    pub training_misses: Vec<TrainingMiss>,
}

impl From<&TemplateIndexFile> for TemplateIndexPayload {
    fn from(value: &TemplateIndexFile) -> Self {
        Self {
            runtime: value.runtime.clone(),
            training: value.training.clone(),
            preprocess_plan: value.preprocess_plan.clone(),
            templates: value.templates.clone(),
            length_buckets: value.length_buckets.clone(),
            anchor_postings: value.anchor_postings.clone(),
            training_assignments: value.training_assignments.clone(),
            training_misses: value.training_misses.clone(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{PLACEHOLDER_CLASSIFIER_VERSION, PLACEHOLDER_PIPELINE_VERSION};

    /// Reproduce the legacy `IndexBuilder::build_from_parts` hand-written
    /// `RuntimeCompatibility` construction to guard field wiring.
    fn legacy_from_index_context(ctx: &IndexBuildContext) -> RuntimeCompatibility {
        RuntimeCompatibility {
            log_format: ctx.log_format.clone(),
            preprocess: ctx.preprocess,
            normalization_version: NORMALIZATION_VERSION,
            preprocess_rules_version: PREPROCESS_RULES_VERSION,
            tokenization_version: TOKENIZATION_VERSION,
            parameter_extraction_version: PARAMETER_EXTRACTION_VERSION,
            typed_placeholders: ctx.typed_placeholders,
            placeholder_pipeline_version: PLACEHOLDER_PIPELINE_VERSION,
            placeholder_classifier_version: PLACEHOLDER_CLASSIFIER_VERSION,
            separators: Vec::new(),
        }
    }

    /// Reproduce the legacy `validate_runtime_hash` hand-written
    /// `RuntimeCompatibility` construction to guard field wiring.
    fn legacy_from_piplup_config(config: &PiplupConfig) -> RuntimeCompatibility {
        RuntimeCompatibility {
            log_format: config.log_format.clone(),
            preprocess: config.preprocess,
            normalization_version: NORMALIZATION_VERSION,
            preprocess_rules_version: PREPROCESS_RULES_VERSION,
            tokenization_version: TOKENIZATION_VERSION,
            parameter_extraction_version: PARAMETER_EXTRACTION_VERSION,
            typed_placeholders: config.typed_placeholders,
            placeholder_pipeline_version: PLACEHOLDER_PIPELINE_VERSION,
            placeholder_classifier_version: PLACEHOLDER_CLASSIFIER_VERSION,
            separators: Vec::new(),
        }
    }

    fn sample_context(typed_placeholders: bool) -> IndexBuildContext {
        IndexBuildContext {
            log_format: vec!["<Date> <Time> <Content>".to_string()],
            preprocess: true,
            typed_placeholders,
            training: AlgorithmTrainingMetadata::Piplup {
                merge: true,
                keep_para: false,
                hit_limit: 385,
                br_thresh: 5,
                sim_thresh: SimThresh::Dynamic,
            },
        }
    }

    #[test]
    fn for_index_context_matches_legacy_build_from_parts_construction() {
        for typed in [false, true] {
            let ctx = sample_context(typed);
            assert_eq!(
                RuntimeCompatibility::for_index_context(&ctx, &[]),
                legacy_from_index_context(&ctx),
                "typed_placeholders = {typed}"
            );
        }
    }

    #[test]
    fn for_piplup_config_matches_legacy_validate_runtime_hash_construction() {
        for typed in [false, true] {
            let config = PiplupConfig {
                typed_placeholders: typed,
                log_format: vec!["<Content>".to_string()],
                preprocess: false,
                separators: Vec::new(),
                ..PiplupConfig::default()
            };
            assert_eq!(
                RuntimeCompatibility::for_piplup_config(&config),
                legacy_from_piplup_config(&config),
                "typed_placeholders = {typed}"
            );
        }
    }

    #[test]
    fn default_runtime_compatibility_serializes_without_separators_key() {
        // ADR-0046 amendment: the byte-identity guarantee — a default
        // runtime must serialize exactly like a pre-ADR-0046 one.
        let runtime = RuntimeCompatibility::for_piplup_config(&PiplupConfig::default());
        let json = serde_json::to_string(&runtime).expect("runtime should serialize");
        assert!(!json.contains("separators"));
    }

    #[test]
    fn non_empty_separators_serialize_the_key() {
        let config = PiplupConfig {
            separators: vec![",".to_string()],
            ..PiplupConfig::default()
        };
        let runtime = RuntimeCompatibility::for_piplup_config(&config);
        let json = serde_json::to_string(&runtime).expect("runtime should serialize");
        assert!(json.contains("separators"));
        let restored: RuntimeCompatibility =
            serde_json::from_str(&json).expect("runtime should deserialize");
        assert_eq!(restored.separators, config.separators);
    }

    #[test]
    fn deserializing_json_without_separators_yields_empty_list() {
        // Simulates reading a pre-ADR-0046 index: the serialized form of a
        // default runtime omits the field entirely.
        let runtime = RuntimeCompatibility::for_piplup_config(&PiplupConfig::default());
        let json = serde_json::to_string(&runtime).expect("runtime should serialize");
        let restored: RuntimeCompatibility =
            serde_json::from_str(&json).expect("runtime should deserialize");
        assert!(restored.separators.is_empty());
    }

    #[test]
    fn for_piplup_config_carries_config_separators() {
        let config = PiplupConfig {
            separators: vec![",".to_string(), "，".to_string()],
            ..PiplupConfig::default()
        };
        assert_eq!(
            RuntimeCompatibility::for_piplup_config(&config).separators,
            config.separators
        );
    }

    #[test]
    fn for_index_context_carries_threaded_separators() {
        let ctx = sample_context(true);
        let separators = vec![",".to_string()];
        assert_eq!(
            RuntimeCompatibility::for_index_context(&ctx, &separators).separators,
            separators
        );
    }
}

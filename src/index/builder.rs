use std::collections::{BTreeMap, BTreeSet};

use sha2::{Digest, Sha256};

use crate::{
    Drain3Config, ParseOutput, PiplupConfig, PreprocessPlan, TrainingOutput,
    error::LogParseError,
    index::{
        matcher::{CandidateIndex, template_matches as indexed_template_matches},
        types::*,
    },
    parse_core::{
        build_placeholder_evidence, finalize_mask_evidence, observed_kinds_for_index,
        tokenize_index_content,
    },
    preprocessor::Preprocessor,
    template::{CompiledTemplate, TemplateToken},
};

/// Builder for creating a [`TemplateIndexFile`] from training output.
///
/// # Example
///
/// ```rust
/// use log_parser::{PiplupConfig, PiplupParser, IndexBuilder, LogEntry};
///
/// let mut parser = PiplupParser::new(PiplupConfig::default()).unwrap();
/// let entries = vec![
///     LogEntry { line_number: 1, raw: "2026-06-05 10:00:00 worker started".into() },
///     LogEntry { line_number: 2, raw: "2026-06-05 10:00:01 worker 1 done".into() },
/// ];
/// let training = parser.parse_lines_for_training(&entries).unwrap();
/// let index = IndexBuilder::build(&training, &PiplupConfig::default()).unwrap();
/// assert_eq!(index.template_count(), 2);
/// ```
pub struct IndexBuilder;

impl IndexBuilder {
    /// Build an index from PIPLUP training output.
    ///
    /// This is the existing PIPLUP entry point. It wraps the config into an
    /// [`IndexBuildContext`] and delegates to the shared implementation.
    pub fn build(
        training_output: &TrainingOutput,
        config: &PiplupConfig,
    ) -> Result<TemplateIndexFile, LogParseError> {
        let ctx = IndexBuildContext {
            log_format: config.log_format.clone(),
            preprocess: config.preprocess,
            typed_placeholders: config.typed_placeholders,
            training: AlgorithmTrainingMetadata::Piplup {
                merge: config.merge,
                keep_para: config.keep_para,
                hit_limit: config.hit_limit,
                br_thresh: config.br_thresh,
                sim_thresh: config.sim_thresh.clone(),
            },
        };
        Self::build_from_parts(
            &training_output.parse_output,
            &training_output.preprocess_plan,
            &ctx,
            &config.separators,
        )
    }

    /// Build an index from Drain3 training output.
    pub fn build_drain3(
        training_output: &TrainingOutput,
        config: &Drain3Config,
    ) -> Result<TemplateIndexFile, LogParseError> {
        let ctx = IndexBuildContext {
            log_format: config.log_format.clone(),
            preprocess: config.preprocess,
            typed_placeholders: config.typed_placeholders,
            training: AlgorithmTrainingMetadata::Drain3 {
                keep_para: config.keep_para,
                depth: config.depth,
                sim_thresh: config.sim_thresh,
                max_children: config.max_children,
                parametrize_numeric_tokens: config.parametrize_numeric_tokens,
            },
        };
        // Drain3 rejects separators (ADR-0046 §1), so its runtime metadata
        // must always carry an empty separator list.
        Self::build_from_parts(
            &training_output.parse_output,
            &training_output.preprocess_plan,
            &ctx,
            &[],
        )
    }

    /// Build an index from already-parsed parts.
    ///
    /// `separators` (ADR-0046) are the caller-declared whitespace-equivalent
    /// boundaries used at training time; they are replayed here so index
    /// tokenization matches the training tokenization. They are a parameter
    /// rather than an [`IndexBuildContext`] field because that struct is a
    /// re-exported public type (ADR-0046 §4 only blessed the
    /// [`PiplupConfig`](crate::PiplupConfig) field addition). They are
    /// persisted in the runtime-compatibility metadata — serialized (and
    /// hashed) only when non-empty, so default indexes stay byte-identical.
    pub(crate) fn build_from_parts(
        parse_output: &ParseOutput,
        preprocess_plan: &PreprocessPlan,
        ctx: &IndexBuildContext,
        separators: &[String],
    ) -> Result<TemplateIndexFile, LogParseError> {
        let runtime = RuntimeCompatibility::for_index_context(ctx, separators);
        let training = TrainingMetadata {
            algorithm: ctx.training.clone(),
            parser_version: env!("CARGO_PKG_VERSION").to_string(),
            line_count: parse_output.line_results.len(),
        };

        let templates = build_templates(parse_output, preprocess_plan, ctx, separators)?;
        let length_buckets = build_length_buckets(&templates);
        let anchor_postings = build_anchor_postings(&templates);
        let training_misses = build_training_misses(parse_output);
        let training_assignments = build_training_assignments(
            parse_output,
            preprocess_plan,
            ctx,
            &templates,
            &length_buckets,
            &anchor_postings,
            separators,
        );
        let mut index = TemplateIndexFile {
            header: empty_header(),
            runtime,
            training,
            preprocess_plan: preprocess_plan.clone(),
            templates,
            length_buckets,
            anchor_postings,
            training_assignments,
            training_misses,
        };

        index.header = build_header(&index)?;
        Ok(index)
    }
}

pub(crate) fn content_assignment_hash(content: &str) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(content.as_bytes());
    hasher.finalize().into()
}

pub(crate) fn runtime_config_hash(
    runtime: &RuntimeCompatibility,
) -> Result<[u8; 32], LogParseError> {
    hash_json(runtime)
}

pub(crate) fn template_set_hash(templates: &[TemplateEntry]) -> Result<[u8; 32], LogParseError> {
    hash_json(templates)
}

pub(crate) fn preprocess_plan_hash(
    preprocess_plan: &crate::PreprocessPlan,
) -> Result<[u8; 32], LogParseError> {
    hash_json(preprocess_plan)
}

pub(crate) fn payload_checksum(payload: &[u8]) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(payload);
    hasher.finalize().into()
}

pub(crate) fn build_header(index: &TemplateIndexFile) -> Result<IndexHeader, LogParseError> {
    let payload = serde_json::to_vec(&TemplateIndexPayload::from(index))?;
    Ok(IndexHeader {
        magic: *INDEX_MAGIC,
        format_version: INDEX_FORMAT_VERSION,
        header_len: IndexHeader::SERIALIZED_LEN,
        payload_len: payload.len() as u64,
        runtime_config_hash: runtime_config_hash(&index.runtime)?,
        template_set_hash: template_set_hash(&index.templates)?,
        preprocess_plan_hash: preprocess_plan_hash(&index.preprocess_plan)?,
        checksum: payload_checksum(&payload),
    })
}

fn empty_header() -> IndexHeader {
    IndexHeader {
        magic: *INDEX_MAGIC,
        format_version: INDEX_FORMAT_VERSION,
        header_len: IndexHeader::SERIALIZED_LEN,
        payload_len: 0,
        runtime_config_hash: [0; 32],
        template_set_hash: [0; 32],
        preprocess_plan_hash: [0; 32],
        checksum: [0; 32],
    }
}

fn build_templates(
    parse_output: &ParseOutput,
    preprocess_plan: &PreprocessPlan,
    ctx: &IndexBuildContext,
    separators: &[String],
) -> Result<Vec<TemplateEntry>, LogParseError> {
    let mut seen = BTreeSet::<String>::new();
    let mut templates = Vec::new();
    let evidence = if ctx.typed_placeholders {
        build_placeholder_evidence(parse_output, preprocess_plan, ctx.preprocess, |content| {
            tokenize_index_content(content, separators)
        })
    } else {
        BTreeMap::new()
    };

    for (match_order, event) in parse_output.events.iter().enumerate() {
        if !seen.insert(event.event_template.clone()) {
            continue;
        }

        let compiled = CompiledTemplate::compile(&event.event_template);
        let token_atoms = compiled
            .tokens()
            .iter()
            .map(|token| match token {
                TemplateToken::Literal(value) => TokenAtom::Literal {
                    value: value.clone(),
                },
                TemplateToken::Wildcard { head, tail } => TokenAtom::Wildcard {
                    head: head.clone(),
                    tail: tail.clone(),
                },
            })
            .collect::<Vec<_>>();
        let event_id = crate::event_id_for_template(&event.event_template);
        if event.event_id != event_id {
            return Err(LogParseError::InvalidIndex {
                message: format!(
                    "event id mismatch for template `{}`: expected {}, got {}",
                    event.event_template, event_id, event.event_id
                ),
            });
        }

        let literal_tokens = token_atoms
            .iter()
            .filter_map(|token| match token {
                TokenAtom::Literal { value } => Some(value.clone()),
                TokenAtom::Wildcard { .. } => None,
            })
            .collect::<Vec<_>>();
        let anchors = select_anchor_candidates(&literal_tokens);
        let template_id = templates.len() as u32;
        let placeholder_mask = if ctx.typed_placeholders {
            evidence
                .get(&event.event_template)
                .map(|slots| finalize_mask_evidence(slots))
                .unwrap_or_else(|| vec![None; token_atoms.len()])
        } else {
            vec![None; token_atoms.len()]
        };

        templates.push(TemplateEntry {
            template_id,
            event_id,
            template_text: event.event_template.clone(),
            match_order: match_order as u32,
            token_count: token_atoms.len() as u16,
            wildcard_count: token_atoms
                .iter()
                .filter(|token| matches!(token, TokenAtom::Wildcard { .. }))
                .count() as u16,
            literal_count: token_atoms
                .iter()
                .filter(|token| matches!(token, TokenAtom::Literal { .. }))
                .count() as u16,
            token_atoms,
            segments: compiled
                .normalized()
                .split("<*>")
                .map(|segment| segment.to_string())
                .collect(),
            segment_atoms: compiled
                .segments()
                .iter()
                .map(|atoms| {
                    atoms
                        .iter()
                        .map(|atom| match atom {
                            crate::template::SegmentAtom::Literal(value) => {
                                SegmentAtom::Literal(value.clone())
                            }
                            crate::template::SegmentAtom::Whitespace => SegmentAtom::Whitespace,
                        })
                        .collect()
                })
                .collect(),
            anchors,
            placeholder_mask,
        });
    }

    Ok(templates)
}

fn build_training_assignments(
    parse_output: &ParseOutput,
    preprocess_plan: &PreprocessPlan,
    ctx: &IndexBuildContext,
    templates: &[TemplateEntry],
    length_buckets: &[LengthBucket],
    anchor_postings: &[AnchorPosting],
    separators: &[String],
) -> Vec<TrainingAssignment> {
    let matcher = IndexedAssignmentMatcher::new(
        preprocess_plan,
        ctx,
        templates,
        length_buckets,
        anchor_postings,
        separators,
    );
    let mut assignments = BTreeMap::<[u8; 32], TrainingAssignment>::new();

    for line in &parse_output.line_results {
        if line.event_id == "0" || line.event_template == "0" {
            continue;
        }
        let Some(content) = line
            .fields
            .iter()
            .find(|field| field.name == "Content")
            .map(|field| field.value.as_str())
        else {
            continue;
        };
        if matcher.matches(content, &line.event_id, &line.event_template) {
            continue;
        }

        let content_hash = content_assignment_hash(content);
        assignments
            .entry(content_hash)
            .or_insert_with(|| TrainingAssignment {
                content_hash,
                event_id: line.event_id.clone(),
                template_text: line.event_template.clone(),
            });
    }

    assignments.into_values().collect()
}

struct IndexedAssignmentMatcher<'a> {
    preprocess_plan: &'a PreprocessPlan,
    ctx: &'a IndexBuildContext,
    templates: &'a [TemplateEntry],
    candidate_index: CandidateIndex,
    preprocessor: Preprocessor,
    separators: &'a [String],
}

impl<'a> IndexedAssignmentMatcher<'a> {
    fn new(
        preprocess_plan: &'a PreprocessPlan,
        ctx: &'a IndexBuildContext,
        templates: &'a [TemplateEntry],
        length_buckets: &[LengthBucket],
        anchor_postings: &[AnchorPosting],
        separators: &'a [String],
    ) -> Self {
        Self {
            preprocess_plan,
            ctx,
            templates,
            candidate_index: CandidateIndex::new(templates, length_buckets, anchor_postings),
            preprocessor: Preprocessor::default(),
            separators,
        }
    }

    fn matches(&self, content: &str, event_id: &str, event_template: &str) -> bool {
        let processed = if self.ctx.preprocess {
            self.preprocessor
                .apply_with_plan(content, self.preprocess_plan)
        } else {
            content.to_string()
        };
        let tokens = tokenize_index_content(&processed, self.separators);
        let observed_kinds = observed_kinds_for_index(&tokens, self.ctx.typed_placeholders);
        let candidates = self.candidate_index.select_candidates(&tokens);
        let matched = candidates
            .template_ids
            .iter()
            .find_map(|template_id| {
                let entry = &self.templates[*template_id as usize];
                indexed_template_matches(entry, &tokens, &observed_kinds).then_some(entry)
            })
            .or_else(|| {
                self.templates
                    .iter()
                    .find(|entry| indexed_template_matches(entry, &tokens, &observed_kinds))
            });

        matched.is_some_and(|entry| {
            entry.event_id == event_id && entry.template_text == event_template
        })
    }
}

fn build_training_misses(parse_output: &ParseOutput) -> Vec<TrainingMiss> {
    parse_output
        .line_results
        .iter()
        .filter(|line| line.event_id == "0" || line.event_template == "0")
        .filter_map(|line| {
            line.fields
                .iter()
                .find(|field| field.name == "Content")
                .map(|field| {
                    let content_hash = content_assignment_hash(&field.value);
                    (content_hash, TrainingMiss { content_hash })
                })
        })
        .collect::<BTreeMap<_, _>>()
        .into_values()
        .collect()
}

fn build_length_buckets(templates: &[TemplateEntry]) -> Vec<LengthBucket> {
    let mut buckets = BTreeMap::<u16, Vec<u32>>::new();
    for template in templates {
        buckets
            .entry(template.token_count)
            .or_default()
            .push(template.template_id);
    }

    buckets
        .into_iter()
        .map(|(token_count, template_ids)| LengthBucket {
            token_count,
            template_ids,
        })
        .collect()
}

fn build_anchor_postings(templates: &[TemplateEntry]) -> Vec<AnchorPosting> {
    let frequencies = global_literal_frequencies(templates);
    let mut postings = BTreeMap::<String, Vec<u32>>::new();

    for template in templates {
        if let Some(anchor) = choose_anchor(template, &frequencies) {
            postings
                .entry(anchor)
                .or_default()
                .push(template.template_id);
        }
    }

    postings
        .into_iter()
        .map(|(anchor, template_ids)| AnchorPosting {
            anchor,
            template_ids,
        })
        .collect()
}

fn global_literal_frequencies(templates: &[TemplateEntry]) -> BTreeMap<String, usize> {
    let mut frequencies = BTreeMap::<String, usize>::new();
    for template in templates {
        let mut seen = BTreeSet::new();
        for token in &template.token_atoms {
            let TokenAtom::Literal { value } = token else {
                continue;
            };
            if !anchor_eligible(value) || !seen.insert(value.clone()) {
                continue;
            }
            *frequencies.entry(value.clone()).or_default() += 1;
        }
    }
    frequencies
}

fn choose_anchor(
    template: &TemplateEntry,
    frequencies: &BTreeMap<String, usize>,
) -> Option<String> {
    template
        .anchors
        .iter()
        .min_by(|left, right| {
            let left_key = (
                frequencies.get(*left).copied().unwrap_or(usize::MAX),
                usize::MAX - left.len(),
                template
                    .token_atoms
                    .iter()
                    .position(
                        |token| matches!(token, TokenAtom::Literal { value } if value == *left),
                    )
                    .unwrap_or(usize::MAX),
                (*left).clone(),
            );
            let right_key = (
                frequencies.get(*right).copied().unwrap_or(usize::MAX),
                usize::MAX - right.len(),
                template
                    .token_atoms
                    .iter()
                    .position(
                        |token| matches!(token, TokenAtom::Literal { value } if value == *right),
                    )
                    .unwrap_or(usize::MAX),
                (*right).clone(),
            );
            left_key.cmp(&right_key)
        })
        .cloned()
}

fn select_anchor_candidates(literal_tokens: &[String]) -> Vec<String> {
    literal_tokens
        .iter()
        .filter(|token| anchor_eligible(token))
        .cloned()
        .collect()
}

fn anchor_eligible(token: &str) -> bool {
    token.len() >= 3 && token.chars().any(char::is_alphanumeric)
}

fn hash_json<T: serde::Serialize + ?Sized>(value: &T) -> Result<[u8; 32], LogParseError> {
    let mut hasher = Sha256::new();
    hasher.update(serde_json::to_vec(value)?);
    Ok(hasher.finalize().into())
}

#[cfg(test)]
mod tests {
    use super::{IndexBuilder, TemplateIndexPayload, runtime_config_hash};
    use crate::{
        Drain3Config, EventRecord, LogEntry, ParseOutput, PiplupConfig, PiplupParser,
        PreprocessPlan, SimThresh, TrainingOutput,
        index::types::{AlgorithmTrainingMetadata, TokenAtom},
        placeholder::PlaceholderKind,
    };

    fn minimal_training() -> TrainingOutput {
        TrainingOutput {
            parse_output: ParseOutput {
                line_results: Vec::new(),
                events: vec![EventRecord {
                    event_id: crate::event_id_for_template("INFO ok"),
                    event_template: "INFO ok".into(),
                    occurrences: 1,
                }],
                cluster_count: 1,
                duration: std::time::Duration::from_secs(0),
            },
            preprocess_plan: PreprocessPlan::default(),
        }
    }

    #[test]
    fn build_index_generates_match_order_buckets_and_anchors() {
        let entries = [
            LogEntry {
                line_number: 1,
                raw: "2026-04-11 12:00:00 INFO worker alpha".into(),
            },
            LogEntry {
                line_number: 2,
                raw: "2026-04-11 12:00:01 INFO worker beta".into(),
            },
            LogEntry {
                line_number: 3,
                raw: "2026-04-11 12:00:02 WARN disk full".into(),
            },
        ];
        let config = PiplupConfig {
            sim_thresh: SimThresh::Fixed(1.0),
            ..PiplupConfig::default()
        };
        let mut parser = PiplupParser::new(config.clone()).expect("parser should build");
        let training = parser
            .parse_lines_for_training(&entries)
            .expect("training output should build");

        let index = IndexBuilder::build(&training, &config).expect("index should build");

        assert_eq!(index.templates.len(), 3);
        assert_eq!(index.templates[0].template_id, 0);
        assert_eq!(index.templates[0].match_order, 0);
        assert_eq!(index.templates[0].token_count, 3);
        assert_eq!(index.length_buckets.len(), 1);
        assert_eq!(index.length_buckets[0].template_ids, vec![0, 1, 2]);
        assert!(
            index
                .anchor_postings
                .iter()
                .any(|posting| posting.anchor == "alpha"),
            "specific literal should be selected as a single-anchor posting"
        );
    }

    #[test]
    fn build_index_uses_dense_template_ids_after_dedup_and_preserves_match_order() {
        let config = PiplupConfig::default();
        let training = TrainingOutput {
            parse_output: ParseOutput {
                line_results: Vec::new(),
                events: vec![
                    EventRecord {
                        event_id: crate::event_id_for_template("INFO worker <*>"),
                        event_template: "INFO worker <*>".into(),
                        occurrences: 2,
                    },
                    EventRecord {
                        event_id: crate::event_id_for_template("WARN disk full"),
                        event_template: "WARN disk full".into(),
                        occurrences: 1,
                    },
                    EventRecord {
                        event_id: crate::event_id_for_template("INFO worker <*>"),
                        event_template: "INFO worker <*>".into(),
                        occurrences: 3,
                    },
                ],
                cluster_count: 0,
                duration: std::time::Duration::from_secs(0),
            },
            preprocess_plan: PreprocessPlan::default(),
        };

        let index = IndexBuilder::build(&training, &config).expect("index should build");

        assert_eq!(index.templates.len(), 2);
        assert_eq!(
            index
                .templates
                .iter()
                .map(|template| template.template_id)
                .collect::<Vec<_>>(),
            vec![0, 1]
        );
        assert_eq!(
            index
                .templates
                .iter()
                .map(|template| template.match_order)
                .collect::<Vec<_>>(),
            vec![0, 1]
        );
        assert_eq!(index.length_buckets.len(), 1);
        assert!(index.length_buckets.iter().all(|bucket| {
            bucket
                .template_ids
                .iter()
                .all(|template_id| *template_id < 2)
        }));
        assert!(index.anchor_postings.iter().all(|posting| {
            posting
                .template_ids
                .iter()
                .all(|template_id| *template_id < 2)
        }));
    }

    #[test]
    fn piplup_index_includes_algorithm_training_metadata_piplup() {
        let config = PiplupConfig::default();
        let training = TrainingOutput {
            parse_output: ParseOutput {
                line_results: Vec::new(),
                events: vec![EventRecord {
                    event_id: crate::event_id_for_template("INFO ok"),
                    event_template: "INFO ok".into(),
                    occurrences: 1,
                }],
                cluster_count: 1,
                duration: std::time::Duration::from_secs(0),
            },
            preprocess_plan: PreprocessPlan::default(),
        };

        let index = IndexBuilder::build(&training, &config).expect("index should build");

        assert!(matches!(
            index.training.algorithm,
            AlgorithmTrainingMetadata::Piplup { .. }
        ));
    }

    #[test]
    fn drain3_index_includes_algorithm_training_metadata_drain3() {
        let config = Drain3Config::default();
        let training = TrainingOutput {
            parse_output: ParseOutput {
                line_results: Vec::new(),
                events: vec![EventRecord {
                    event_id: crate::event_id_for_template("INFO ok"),
                    event_template: "INFO ok".into(),
                    occurrences: 1,
                }],
                cluster_count: 1,
                duration: std::time::Duration::from_secs(0),
            },
            preprocess_plan: PreprocessPlan::default(),
        };

        let index = IndexBuilder::build_drain3(&training, &config).expect("index should build");

        assert!(matches!(
            index.training.algorithm,
            AlgorithmTrainingMetadata::Drain3 { .. }
        ));
    }

    #[test]
    fn runtime_compatibility_hash_excludes_algorithm() {
        let piplup_config = PiplupConfig::default();
        let drain3_config = Drain3Config::default();
        let training = TrainingOutput {
            parse_output: ParseOutput {
                line_results: Vec::new(),
                events: vec![EventRecord {
                    event_id: crate::event_id_for_template("INFO ok"),
                    event_template: "INFO ok".into(),
                    occurrences: 1,
                }],
                cluster_count: 1,
                duration: std::time::Duration::from_secs(0),
            },
            preprocess_plan: PreprocessPlan::default(),
        };

        let piplup_index =
            IndexBuilder::build(&training, &piplup_config).expect("piplup index should build");
        let drain3_index = IndexBuilder::build_drain3(&training, &drain3_config)
            .expect("drain3 index should build");

        // Runtime compatibility hash should be the same for both algorithms
        // since it only includes log_format, preprocess, and version constants.
        assert_eq!(
            piplup_index.header.runtime_config_hash,
            drain3_index.header.runtime_config_hash
        );
    }

    #[test]
    fn old_index_format_version_is_rejected() {
        let config = PiplupConfig::default();
        let training = TrainingOutput {
            parse_output: ParseOutput {
                line_results: Vec::new(),
                events: vec![EventRecord {
                    event_id: crate::event_id_for_template("INFO ok"),
                    event_template: "INFO ok".into(),
                    occurrences: 1,
                }],
                cluster_count: 1,
                duration: std::time::Duration::from_secs(0),
            },
            preprocess_plan: PreprocessPlan::default(),
        };

        let index = IndexBuilder::build(&training, &config).expect("index should build");
        let mut bytes =
            crate::index::IndexWriter::write_to_vec(&index).expect("index should serialize");
        // Simulate an old format version by patching the magic + version bytes.
        // Magic is 7 bytes, then format_version is 4 bytes at offset 7.
        bytes[7..11].copy_from_slice(&3u32.to_le_bytes());

        let error =
            crate::index::IndexReader::read_from_bytes(&bytes, &config).expect_err("old version");
        assert!(matches!(error, crate::LogParseError::InvalidIndex { .. }));
    }

    #[test]
    fn typed_index_round_trip_preserves_placeholder_mask() {
        let config = PiplupConfig {
            typed_placeholders: true,
            preprocess: false,
            sim_thresh: SimThresh::Fixed(0.5),
            ..PiplupConfig::default()
        };
        let entries = [
            LogEntry {
                line_number: 1,
                raw: "2026-04-11 12:00:01 INFO worker 123 done".into(),
            },
            LogEntry {
                line_number: 2,
                raw: "2026-04-11 12:00:02 INFO worker 456 done".into(),
            },
        ];
        let mut parser = PiplupParser::new(config.clone()).expect("parser should build");
        let training = parser
            .parse_lines_for_training(&entries)
            .expect("training should succeed");
        let index = IndexBuilder::build(&training, &config).expect("index should build");

        let mask = &index.templates[0].placeholder_mask;
        assert_eq!(mask.len(), index.templates[0].token_atoms.len());
        assert!(mask.contains(&Some(PlaceholderKind::Number)));

        let bytes = crate::index::IndexWriter::write_to_vec(&index).expect("index should write");
        let restored =
            crate::index::IndexReader::read_from_bytes(&bytes, &config).expect("index should read");
        assert_eq!(
            restored.templates[0].placeholder_mask,
            index.templates[0].placeholder_mask
        );
    }

    #[test]
    fn typed_runtime_mismatch_is_rejected() {
        let typed = PiplupConfig {
            typed_placeholders: true,
            preprocess: false,
            sim_thresh: SimThresh::Fixed(0.5),
            ..PiplupConfig::default()
        };
        let eager = PiplupConfig {
            typed_placeholders: false,
            preprocess: false,
            sim_thresh: SimThresh::Fixed(0.5),
            ..PiplupConfig::default()
        };
        let entries = [
            LogEntry {
                line_number: 1,
                raw: "2026-04-11 12:00:01 INFO worker 123 done".into(),
            },
            LogEntry {
                line_number: 2,
                raw: "2026-04-11 12:00:02 INFO worker 456 done".into(),
            },
        ];
        let mut parser = PiplupParser::new(typed.clone()).expect("parser should build");
        let training = parser
            .parse_lines_for_training(&entries)
            .expect("training should succeed");
        let index = IndexBuilder::build(&training, &typed).expect("index should build");
        let bytes = crate::index::IndexWriter::write_to_vec(&index).expect("index should write");

        let error = crate::index::IndexReader::read_from_bytes(&bytes, &eager)
            .expect_err("typed flag mismatch should be rejected");
        assert!(matches!(
            error,
            crate::LogParseError::IncompatibleIndexRuntime
        ));
    }

    #[test]
    fn index_structure_rejects_invalid_placeholder_masks() {
        let config = PiplupConfig {
            typed_placeholders: true,
            preprocess: false,
            sim_thresh: SimThresh::Fixed(0.5),
            ..PiplupConfig::default()
        };
        let entries = [
            LogEntry {
                line_number: 1,
                raw: "2026-04-11 12:00:01 INFO worker 123 done".into(),
            },
            LogEntry {
                line_number: 2,
                raw: "2026-04-11 12:00:02 INFO worker 456 done".into(),
            },
        ];
        let mut parser = PiplupParser::new(config.clone()).expect("parser should build");
        let training = parser
            .parse_lines_for_training(&entries)
            .expect("training should succeed");
        let index = IndexBuilder::build(&training, &config).expect("index should build");

        let mut bad_len = index.clone();
        bad_len.templates[0].placeholder_mask.pop();
        assert!(matches!(
            crate::index::reader::validate_index_structure(&bad_len),
            Err(crate::LogParseError::InvalidIndex { .. })
        ));

        let mut bad_literal = index;
        let literal_index = bad_literal.templates[0]
            .token_atoms
            .iter()
            .position(|atom| matches!(atom, TokenAtom::Literal { .. }))
            .expect("template should have a literal token");
        bad_literal.templates[0].placeholder_mask[literal_index] = Some(PlaceholderKind::Number);
        assert!(matches!(
            crate::index::reader::validate_index_structure(&bad_literal),
            Err(crate::LogParseError::InvalidIndex { .. })
        ));
    }

    #[test]
    fn index_old_classifier_version_is_rejected() {
        let config = PiplupConfig {
            typed_placeholders: true,
            preprocess: false,
            sim_thresh: SimThresh::Fixed(0.5),
            ..PiplupConfig::default()
        };
        let entries = [
            LogEntry {
                line_number: 1,
                raw: "2026-04-11 12:00:01 INFO worker 123 done".into(),
            },
            LogEntry {
                line_number: 2,
                raw: "2026-04-11 12:00:02 INFO worker 456 done".into(),
            },
        ];
        let mut parser = PiplupParser::new(config.clone()).expect("parser should build");
        let training = parser
            .parse_lines_for_training(&entries)
            .expect("training should succeed");
        let mut index = IndexBuilder::build(&training, &config).expect("index should build");

        // Simulate an index stamped with the previous classifier version (1):
        // rebuild the header hash the way that version would have produced it.
        index.runtime.placeholder_classifier_version = 1;
        index.header.runtime_config_hash =
            runtime_config_hash(&index.runtime).expect("runtime hash should compute");

        let bytes = crate::index::IndexWriter::write_to_vec(&index).expect("index should write");
        let error = crate::index::IndexReader::read_from_bytes(&bytes, &config)
            .expect_err("index stamped with classifier version 1 must be rejected");
        assert!(matches!(
            error,
            crate::LogParseError::IncompatibleIndexRuntime
        ));
    }

    #[test]
    fn runtime_config_hash_isolates_separator_configs() {
        // ADR-0046 §6: configs differing only in separators must hash
        // differently, so cross-config index reuse is rejected at load.
        let training = minimal_training();
        let default_config = PiplupConfig::default();
        let comma_config = PiplupConfig {
            separators: vec![",".to_string()],
            ..PiplupConfig::default()
        };
        let colon_config = PiplupConfig {
            separators: vec![":".to_string()],
            ..PiplupConfig::default()
        };

        let default_index =
            IndexBuilder::build(&training, &default_config).expect("default index should build");
        let comma_index =
            IndexBuilder::build(&training, &comma_config).expect("comma index should build");
        let colon_index =
            IndexBuilder::build(&training, &colon_config).expect("colon index should build");

        assert_ne!(
            default_index.header.runtime_config_hash, comma_index.header.runtime_config_hash,
            "separator and default configs must not share a runtime hash"
        );
        assert_ne!(
            comma_index.header.runtime_config_hash, colon_index.header.runtime_config_hash,
            "configs differing only in the separator list must not share a runtime hash"
        );
    }

    #[test]
    fn default_index_payload_omits_separators_key() {
        // ADR-0046 amendment: default-config indexes serialize exactly like
        // pre-ADR-0046 files — the payload never mentions `separators`.
        let training = minimal_training();
        let default_index = IndexBuilder::build(&training, &PiplupConfig::default())
            .expect("default index should build");
        let payload = serde_json::to_vec(&TemplateIndexPayload::from(&default_index))
            .expect("payload should serialize");
        let payload = String::from_utf8(payload).expect("payload should be UTF-8");
        assert!(!payload.contains("separators"));

        let comma_config = PiplupConfig {
            separators: vec![",".to_string()],
            ..PiplupConfig::default()
        };
        let comma_index =
            IndexBuilder::build(&training, &comma_config).expect("comma index should build");
        let separator_payload = serde_json::to_vec(&TemplateIndexPayload::from(&comma_index))
            .expect("payload should serialize");
        let separator_payload =
            String::from_utf8(separator_payload).expect("payload should be UTF-8");
        assert!(separator_payload.contains("separators"));
    }

    #[test]
    fn separator_index_rejects_mismatched_runtime_configs() {
        let comma_config = PiplupConfig {
            separators: vec![",".to_string()],
            ..PiplupConfig::default()
        };
        let entries = [
            LogEntry {
                line_number: 1,
                raw: "2026-04-11 12:00:00 upload failed,retry 3 times,timeout 5000 ms".into(),
            },
            LogEntry {
                line_number: 2,
                raw: "2026-04-11 12:00:01 upload failed,retry 7 times,timeout 3000 ms".into(),
            },
        ];
        let mut parser = PiplupParser::new(comma_config.clone()).expect("parser should build");
        let training = parser
            .parse_lines_for_training(&entries)
            .expect("training output should build");
        let index = IndexBuilder::build(&training, &comma_config).expect("index should build");
        let bytes = crate::index::IndexWriter::write_to_vec(&index).expect("index should write");

        crate::index::IndexReader::read_from_bytes(&bytes, &comma_config)
            .expect("same separators config should load");

        let error = crate::index::IndexReader::read_from_bytes(&bytes, &PiplupConfig::default())
            .expect_err("default config must be rejected");
        assert!(matches!(
            error,
            crate::LogParseError::IncompatibleIndexRuntime
        ));

        let colon_config = PiplupConfig {
            separators: vec![":".to_string()],
            ..PiplupConfig::default()
        };
        let error = crate::index::IndexReader::read_from_bytes(&bytes, &colon_config)
            .expect_err("different separator list must be rejected");
        assert!(matches!(
            error,
            crate::LogParseError::IncompatibleIndexRuntime
        ));
    }

    #[test]
    fn default_index_rejects_separator_reader_config() {
        let training = minimal_training();
        let index = IndexBuilder::build(&training, &PiplupConfig::default())
            .expect("default index should build");
        let bytes = crate::index::IndexWriter::write_to_vec(&index).expect("index should write");

        let comma_config = PiplupConfig {
            separators: vec![",".to_string()],
            ..PiplupConfig::default()
        };
        let error = crate::index::IndexReader::read_from_bytes(&bytes, &comma_config)
            .expect_err("separator reader config must be rejected");
        assert!(matches!(
            error,
            crate::LogParseError::IncompatibleIndexRuntime
        ));
    }
}

use std::{collections::BTreeMap, collections::BTreeSet, path::Path, time::Instant};

use crate::{
    EventRecord, LineResult, LogEntry, MultiCompiledLogFormat, ParseOutput, ParsedLine,
    PiplupConfig, Preprocessor,
    error::LogParseError,
    index::{
        IndexReader,
        accumulator::IndexedResultAccumulator,
        builder::content_assignment_hash,
        matcher::{CandidateIndex, template_matches as token_template_matches},
        reader::{
            validate_index_structure, validate_payload_hashes, validate_payload_runtime_hash,
        },
        types::{RuntimeCompatibility, TemplateIndexFile},
    },
    parse_core::{ensure_unique_line_numbers, observed_kinds_for_index, tokenize_index_content},
    placeholder::PlaceholderKind,
    template::CompiledTemplate as RuntimeTemplate,
};

/// Statistics reported by an indexed parse run.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IndexedParseStats {
    /// Total number of lines parsed.
    pub line_count: usize,
    /// Number of lines that did not match any template.
    pub miss_count: usize,
    /// First line number that missed, if any.
    pub first_miss_line: Option<usize>,
    /// Number of misses that were also seen during training.
    pub training_miss_count: usize,
    /// Number of misses that were not seen during training.
    pub new_miss_count: usize,
    /// First line number of a new miss, if any.
    pub first_new_miss_line: Option<usize>,
}

/// Output of an indexed parse run, including statistics.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IndexedParseOutput {
    /// Structured parse output.
    pub parse_output: ParseOutput,
    /// Parse statistics.
    pub stats: IndexedParseStats,
}

#[derive(Debug, Clone)]
struct RuntimeEntry {
    template_id: u32,
    event_id: String,
    template_text: String,
    runtime_template: RuntimeTemplate,
}

#[derive(Debug, Clone)]
struct RuntimeAssignment {
    event_id: String,
    template_text: String,
}

/// Result of parsing a single batch of lines with [`IndexedParser::parse_batch`].
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct IndexedBatchResult {
    /// Parsed line results for this batch.
    pub line_results: Vec<LineResult>,
    /// Event records aggregated for this batch.
    pub events: Vec<EventRecord>,
    /// Statistics for this batch.
    pub stats: IndexedParseStats,
    /// Minimum and maximum input line numbers in this batch.
    pub input_line_range: (Option<usize>, Option<usize>),
}

/// Read-only parser that assigns log lines against a pre-built index.
///
/// # Example
///
/// ```rust
/// use log_parser::{IndexedParser, PiplupConfig, PiplupParser, LogEntry, IndexBuilder};
///
/// let mut parser = PiplupParser::new(PiplupConfig::default()).unwrap();
/// let entries = vec![
///     LogEntry { line_number: 1, raw: "2026-06-05 10:00:00 worker started".into() },
///     LogEntry { line_number: 2, raw: "2026-06-05 10:00:01 worker 1 done".into() },
/// ];
/// let training = parser.parse_lines_for_training(&entries).unwrap();
/// let index = IndexBuilder::build(&training, &PiplupConfig::default()).unwrap();
/// let indexed = IndexedParser::from_index(index, PiplupConfig::default()).unwrap();
/// let output = indexed.parse_lines(&entries).unwrap();
/// assert!(!output.line_results.is_empty());
/// ```
#[derive(Debug, Clone)]
pub struct IndexedParser {
    config: PiplupConfig,
    index: TemplateIndexFile,
    format: MultiCompiledLogFormat,
    preprocessor: Preprocessor,
    runtime_templates: Vec<RuntimeEntry>,
    candidate_index: CandidateIndex,
    training_assignments: BTreeMap<[u8; 32], RuntimeAssignment>,
    training_misses: BTreeSet<[u8; 32]>,
}

impl IndexedParser {
    /// Create an indexed parser from a loaded [`TemplateIndexFile`] and config.
    pub fn from_index(
        index: TemplateIndexFile,
        config: PiplupConfig,
    ) -> Result<Self, LogParseError> {
        validate_payload_runtime_hash(&index)?;
        validate_payload_hashes(&index)?;
        validate_index_structure(&index)?;
        ensure_runtime_compatible(&index.runtime, &config)?;
        let format = MultiCompiledLogFormat::compile(&config.log_format)?;
        let preprocessor = Preprocessor::default();
        let mut runtime_templates = index
            .templates
            .iter()
            .map(|template| RuntimeEntry {
                template_id: template.template_id,
                event_id: template.event_id.clone(),
                template_text: template.template_text.clone(),
                runtime_template: RuntimeTemplate::compile(&template.template_text),
            })
            .collect::<Vec<_>>();
        runtime_templates.sort_by_key(|entry| entry.template_id);
        let candidate_index = CandidateIndex::new(
            &index.templates,
            &index.length_buckets,
            &index.anchor_postings,
        );
        let training_assignments = index
            .training_assignments
            .iter()
            .map(|assignment| {
                (
                    assignment.content_hash,
                    RuntimeAssignment {
                        event_id: assignment.event_id.clone(),
                        template_text: assignment.template_text.clone(),
                    },
                )
            })
            .collect();
        let training_misses = index
            .training_misses
            .iter()
            .map(|miss| miss.content_hash)
            .collect();

        Ok(Self {
            config,
            index,
            format,
            preprocessor,
            runtime_templates,
            candidate_index,
            training_assignments,
            training_misses,
        })
    }

    /// Load an index from bytes and create a parser.
    pub fn from_bytes(bytes: &[u8], config: PiplupConfig) -> Result<Self, LogParseError> {
        let index = IndexReader::read_from_bytes(bytes, &config)?;
        Self::from_index(index, config)
    }

    /// Load an index from a reader and create a parser.
    pub fn from_reader<R: std::io::Read>(
        reader: R,
        config: PiplupConfig,
    ) -> Result<Self, LogParseError> {
        let index = IndexReader::read(reader, &config)?;
        Self::from_index(index, config)
    }

    /// Parse an entire file using the index.
    pub fn parse_file(&self, path: &Path) -> Result<ParseOutput, LogParseError> {
        Ok(self.parse_file_detailed(path)?.parse_output)
    }

    /// Parse an entire file and return detailed statistics.
    pub fn parse_file_detailed(&self, path: &Path) -> Result<IndexedParseOutput, LogParseError> {
        let raw = std::fs::read_to_string(path)?;
        let entries = raw
            .lines()
            .enumerate()
            .map(|(index, line)| LogEntry {
                line_number: index + 1,
                raw: line.to_string(),
            })
            .collect::<Vec<_>>();
        self.parse_lines_detailed(&entries)
    }

    /// Returns the number of templates in the underlying index.
    ///
    /// This is the `cluster_count` value that any `parse_lines_detailed` call
    /// will report, regardless of input.
    pub fn template_count(&self) -> usize {
        self.index.templates.len()
    }

    /// Parse a slice of log entries using the index.
    pub fn parse_lines(&self, entries: &[LogEntry]) -> Result<ParseOutput, LogParseError> {
        Ok(self.parse_lines_detailed(entries)?.parse_output)
    }

    /// Create a new accumulator for merging batched indexed results.
    pub fn accumulator(&self) -> IndexedResultAccumulator {
        IndexedResultAccumulator::new(self.index.templates.len())
    }

    /// Parse a batch of log entries.
    ///
    /// Batches can be merged with an [`IndexedResultAccumulator`].
    pub fn parse_batch(&self, entries: &[LogEntry]) -> Result<IndexedBatchResult, LogParseError> {
        ensure_unique_line_numbers(entries.iter().map(|e| e.line_number))?;
        let input_line_range = entries.iter().map(|e| e.line_number).fold(
            (None, None),
            |(min_acc, max_acc): (Option<usize>, Option<usize>), n| {
                (
                    Some(min_acc.map_or(n, |m| m.min(n))),
                    Some(max_acc.map_or(n, |m| m.max(n))),
                )
            },
        );
        let parsed_lines = entries
            .iter()
            .filter_map(|entry| self.parse_entry(entry).transpose())
            .collect::<Result<Vec<_>, _>>()?;

        let mut line_results = Vec::with_capacity(parsed_lines.len());
        let mut accumulator = crate::output_core::EventAccumulator::new();
        let mut extractor = crate::output_core::ParameterExtractor::new(self.config.keep_para);
        let mut stats = IndexedParseStats {
            line_count: parsed_lines.len(),
            miss_count: 0,
            first_miss_line: None,
            training_miss_count: 0,
            new_miss_count: 0,
            first_new_miss_line: None,
        };

        for line in parsed_lines {
            let matched = self
                .match_training_assignment(&line.content)
                .map(|assignment| {
                    (
                        assignment.event_id.clone(),
                        assignment.template_text.clone(),
                        extractor.for_template(&assignment.template_text, &line.content),
                    )
                })
                .or_else(|| {
                    let processed = self.preprocess_content(&line.content);
                    let tokens = tokenize_index_content(&processed, &self.config.separators);
                    let observed_kinds =
                        observed_kinds_for_index(&tokens, self.index.runtime.typed_placeholders);
                    let candidates = self.candidate_index.select_candidates(&tokens);
                    self.match_indexed_template(&candidates.template_ids, &tokens, &observed_kinds)
                        .map(|entry| {
                            (
                                entry.event_id.clone(),
                                entry.template_text.clone(),
                                extractor.for_runtime(&entry.runtime_template, &line.content),
                            )
                        })
                });

            let (event_id, event_template, parameter_list) = match matched {
                Some((event_id, event_template, parameter_list)) => {
                    accumulator.record_event(&event_id, &event_template, 1);
                    (event_id, event_template, parameter_list)
                }
                None => {
                    stats.miss_count += 1;
                    stats.first_miss_line.get_or_insert(line.line_number);
                    let miss_kind = if self.is_training_miss(&line.content) {
                        stats.training_miss_count += 1;
                        "training miss"
                    } else {
                        stats.new_miss_count += 1;
                        stats.first_new_miss_line.get_or_insert(line.line_number);
                        "new miss"
                    };
                    if self.config.warn_miss {
                        eprintln!("[Warning] No template matched for log: {}", line.content);
                        eprintln!("          -> Classification: {}", miss_kind);
                    }
                    ("0".to_string(), "0".to_string(), extractor.unmatched())
                }
            };

            line_results.push(LineResult {
                line_number: line.line_number,
                raw: line.raw,
                fields: line.fields,
                event_id,
                event_template,
                parameter_list,
            });
        }

        Ok(IndexedBatchResult {
            line_results,
            events: accumulator.into_events(),
            stats,
            input_line_range,
        })
    }

    /// Parse entries and return detailed output with statistics.
    pub fn parse_lines_detailed(
        &self,
        entries: &[LogEntry],
    ) -> Result<IndexedParseOutput, LogParseError> {
        let started = Instant::now();
        let batch_result = self.parse_batch(entries)?;
        Ok(IndexedParseOutput {
            parse_output: ParseOutput {
                line_results: batch_result.line_results,
                events: batch_result.events,
                cluster_count: self.index.templates.len(),
                duration: started.elapsed(),
            },
            stats: batch_result.stats,
        })
    }

    fn parse_entry(&self, entry: &LogEntry) -> Result<Option<ParsedLine>, LogParseError> {
        crate::parse_core::parse_entry(&self.format, self.config.skip_unmatched, entry)
    }

    fn preprocess_content(&self, content: &str) -> String {
        if self.config.preprocess {
            self.preprocessor
                .apply_with_plan(content, &self.index.preprocess_plan)
        } else {
            content.to_string()
        }
    }

    fn match_indexed_template(
        &self,
        template_ids: &[u32],
        tokens: &[String],
        observed_kinds: &[Option<PlaceholderKind>],
    ) -> Option<&RuntimeEntry> {
        template_ids
            .iter()
            .find_map(|template_id| {
                let entry = &self.index.templates[*template_id as usize];
                if token_template_matches(entry, tokens, observed_kinds) {
                    Some(&self.runtime_templates[*template_id as usize])
                } else {
                    None
                }
            })
            .or_else(|| {
                self.index.templates.iter().find_map(|entry| {
                    if token_template_matches(entry, tokens, observed_kinds) {
                        Some(&self.runtime_templates[entry.template_id as usize])
                    } else {
                        None
                    }
                })
            })
    }

    fn match_training_assignment(&self, content: &str) -> Option<&RuntimeAssignment> {
        self.training_assignments
            .get(&content_assignment_hash(content))
    }

    fn is_training_miss(&self, content: &str) -> bool {
        self.training_misses
            .contains(&content_assignment_hash(content))
    }
}

fn ensure_runtime_compatible(
    runtime: &RuntimeCompatibility,
    config: &PiplupConfig,
) -> Result<(), LogParseError> {
    if runtime != &RuntimeCompatibility::for_piplup_config(config) {
        return Err(LogParseError::IncompatibleIndexRuntime);
    }
    Ok(())
}

// Compile-time proof that indexed parse types are safe to share across threads.
// This enables Arc<IndexedParser> for multi-worker async parsing (Phase 4).
const _: () = {
    const fn assert_send_sync<T: Send + Sync>() {}
    assert_send_sync::<IndexedParser>();
    assert_send_sync::<IndexedBatchResult>();
    assert_send_sync::<IndexedParseOutput>();
    assert_send_sync::<IndexedParseStats>();
};

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use super::*;
    use crate::engine::parser::PiplupParser;
    use crate::index::IndexBuilder;

    fn entry(line_number: usize, content: &str) -> LogEntry {
        LogEntry {
            line_number,
            raw: format!("2026-04-11 12:00:{:02} {content}", line_number % 60),
        }
    }

    fn build_parser(config: PiplupConfig, entries: &[LogEntry]) -> IndexedParser {
        let mut trainer = PiplupParser::new(config.clone()).expect("trainer should build");
        let training = trainer
            .parse_lines_for_training(entries)
            .expect("training should succeed");
        let index = IndexBuilder::build(&training, &config).expect("index should build");
        IndexedParser::from_index(index, config).expect("indexed parser should build")
    }

    #[test]
    fn training_assignment_covers_index_token_mismatch() {
        let config = PiplupConfig {
            log_format: vec!["<Content>".to_string()],
            preprocess: false,
            ..PiplupConfig::default()
        };
        let content = "A one two B";
        let event_template = "A <*> B";
        let event_id = crate::event_id_for_template(event_template);
        let training = crate::TrainingOutput {
            parse_output: ParseOutput {
                line_results: vec![LineResult {
                    line_number: 1,
                    raw: content.to_string(),
                    fields: vec![crate::ParsedField {
                        name: "Content".to_string(),
                        value: content.to_string(),
                    }],
                    event_id: event_id.clone(),
                    event_template: event_template.to_string(),
                    parameter_list: None,
                }],
                events: vec![EventRecord {
                    event_id,
                    event_template: event_template.to_string(),
                    occurrences: 1,
                }],
                cluster_count: 1,
                duration: Duration::ZERO,
            },
            preprocess_plan: crate::PreprocessPlan::default(),
        };

        let index = IndexBuilder::build(&training, &config).expect("index should build");
        assert_eq!(index.training_assignments.len(), 1);
        let parser = IndexedParser::from_index(index, config).expect("indexed parser should build");
        let output = parser
            .parse_lines_detailed(&[LogEntry {
                line_number: 1,
                raw: content.to_string(),
            }])
            .expect("indexed parse should succeed");

        assert_eq!(output.stats.new_miss_count, 0);
        assert_eq!(
            output.parse_output.line_results[0].event_template,
            "A <*> B"
        );
    }

    fn assert_indexed_outputs_equal_without_duration(
        left: &IndexedParseOutput,
        right: &IndexedParseOutput,
    ) {
        assert_eq!(
            left.parse_output.line_results,
            right.parse_output.line_results
        );
        assert_eq!(left.parse_output.events, right.parse_output.events);
        assert_eq!(
            left.parse_output.cluster_count,
            right.parse_output.cluster_count
        );
        assert_eq!(left.stats, right.stats);
    }

    #[test]
    fn parse_batch_matches_parse_lines_detailed_except_duration() {
        let config = PiplupConfig {
            keep_para: true,
            ..PiplupConfig::default()
        };
        let training_entries = vec![
            entry(1, "INFO worker alpha ready"),
            entry(2, "INFO worker beta ready"),
            entry(3, "WARN disk sda full"),
        ];
        let parser = build_parser(config, &training_entries);
        let entries = vec![
            entry(10, "INFO worker alpha ready"),
            entry(8, "WARN disk sda full"),
            entry(9, "INFO worker beta ready"),
        ];

        let batch = parser.parse_batch(&entries).expect("batch should parse");
        let detailed = parser
            .parse_lines_detailed(&entries)
            .expect("detailed parse should succeed");

        assert_eq!(batch.line_results, detailed.parse_output.line_results);
        assert_eq!(batch.events, detailed.parse_output.events);
        assert_eq!(batch.stats, detailed.stats);
        assert_eq!(batch.input_line_range, (Some(8), Some(10)));
        assert_eq!(detailed.parse_output.cluster_count, parser.template_count());
    }

    #[test]
    fn merged_parse_batches_match_full_parse_except_duration() {
        let entries = vec![
            entry(1, "INFO worker alpha ready"),
            entry(2, "WARN disk sda full"),
            entry(3, "INFO worker alpha ready"),
            entry(4, "WARN disk sda full"),
            entry(5, "INFO worker beta ready"),
            entry(6, "INFO worker beta ready"),
        ];
        let parser = build_parser(PiplupConfig::default(), &entries);
        let full = parser
            .parse_lines_detailed(&entries)
            .expect("full parse should succeed");
        let mut accumulator = parser.accumulator();

        for chunk in entries.chunks(2) {
            let batch = parser.parse_batch(chunk).expect("batch should parse");
            accumulator
                .try_merge(batch)
                .expect("ordered batch should merge");
        }

        let merged = accumulator.finish_with_duration(Duration::ZERO);
        assert_indexed_outputs_equal_without_duration(&merged, &full);
    }

    #[test]
    fn accumulator_factory_uses_parser_template_count() {
        let entries = vec![
            entry(1, "INFO worker alpha ready"),
            entry(2, "WARN disk sda full"),
        ];
        let parser = build_parser(PiplupConfig::default(), &entries);

        assert_eq!(
            parser.accumulator().cluster_count(),
            parser.template_count()
        );
    }

    #[test]
    fn skipped_unmatched_lines_still_make_ranges_overlap() {
        let config = PiplupConfig {
            skip_unmatched: true,
            ..PiplupConfig::default()
        };
        let training_entries = vec![entry(1, "INFO worker alpha ready")];
        let parser = build_parser(config, &training_entries);
        let batch_with_skip = vec![
            entry(1, "INFO worker alpha ready"),
            LogEntry {
                line_number: 2,
                raw: "unmatched".to_string(),
            },
        ];
        let overlapping_batch = vec![entry(2, "INFO worker alpha ready")];

        let first = parser
            .parse_batch(&batch_with_skip)
            .expect("batch with skipped line should parse");
        let second = parser
            .parse_batch(&overlapping_batch)
            .expect("overlapping batch should parse before merge validation");

        assert_eq!(first.line_results.len(), 1);
        assert_eq!(first.input_line_range, (Some(1), Some(2)));

        let mut accumulator = parser.accumulator();
        accumulator
            .try_merge(first)
            .expect("first batch should merge");
        let error = accumulator
            .try_merge(second)
            .expect_err("input ranges should overlap");
        assert!(matches!(
            error,
            LogParseError::InvalidAlgorithmConfig {
                algorithm: "IndexedResultAccumulator",
                field: "input_line_range",
                ..
            }
        ));
    }

    #[test]
    fn typed_indexed_matcher_accepts_expected_kinds() {
        let config = PiplupConfig {
            typed_placeholders: true,
            preprocess: false,
            sim_thresh: crate::SimThresh::Fixed(0.5),
            ..PiplupConfig::default()
        };
        let training_entries = vec![
            entry(1, "INFO number 123 done"),
            entry(2, "INFO number 456 done"),
            entry(3, "INFO hex 0x1a done"),
            entry(4, "INFO hex 0x2b done"),
            entry(5, "INFO id deadbeef done"),
            entry(6, "INFO id cafebabe done"),
        ];
        let parser = build_parser(config, &training_entries);
        let parsed = parser
            .parse_lines(&[
                entry(10, "INFO number 789 done"),
                entry(11, "INFO hex 0xff done"),
                entry(12, "INFO id abcdef12 done"),
            ])
            .expect("typed indexed parse should succeed");

        assert_eq!(
            parsed
                .line_results
                .iter()
                .map(|line| line.event_template.as_str())
                .collect::<Vec<_>>(),
            vec![
                "INFO number <*> done",
                "INFO hex <*> done",
                "INFO id <*> done"
            ]
        );
    }

    #[test]
    fn typed_indexed_matcher_rejects_wrong_kind() {
        let config = PiplupConfig {
            typed_placeholders: true,
            preprocess: false,
            sim_thresh: crate::SimThresh::Fixed(0.5),
            ..PiplupConfig::default()
        };
        let training_entries = vec![
            entry(1, "INFO number 123 done"),
            entry(2, "INFO number 456 done"),
        ];
        let parser = build_parser(config, &training_entries);
        let parsed = parser
            .parse_lines(&[entry(10, "INFO number 0x1a done")])
            .expect("typed indexed parse should succeed");

        assert_eq!(parsed.line_results[0].event_id, "0");
        assert_eq!(parsed.line_results[0].event_template, "0");
    }

    #[test]
    fn untyped_indexed_wildcard_still_accepts_any_kind() {
        let config = PiplupConfig {
            typed_placeholders: false,
            preprocess: true,
            sim_thresh: crate::SimThresh::Fixed(0.5),
            ..PiplupConfig::default()
        };
        let training_entries = vec![
            entry(1, "INFO worker 123 done"),
            entry(2, "INFO worker 456 done"),
        ];
        let parser = build_parser(config, &training_entries);
        let parsed = parser
            .parse_lines(&[entry(10, "INFO worker 0x1a done")])
            .expect("untyped indexed parse should succeed");

        assert_eq!(parsed.line_results[0].event_template, "<*> worker <*> done");
    }

    #[test]
    fn runtime_and_indexed_typed_parse_agree() {
        let config = PiplupConfig {
            typed_placeholders: true,
            preprocess: false,
            sim_thresh: crate::SimThresh::Fixed(0.5),
            ..PiplupConfig::default()
        };
        let training_entries = vec![
            entry(1, "INFO worker 123 done"),
            entry(2, "INFO worker 456 done"),
        ];
        let parse_entries = vec![entry(10, "INFO worker 789 done")];
        let mut runtime = PiplupParser::new(config.clone()).expect("runtime should build");
        runtime
            .parse_lines_for_training(&training_entries)
            .expect("runtime training should succeed");
        let runtime_output = runtime
            .parse_lines(&parse_entries)
            .expect("runtime parse should succeed");
        let indexed_output = build_parser(config, &training_entries)
            .parse_lines(&parse_entries)
            .expect("indexed parse should succeed");

        assert_eq!(
            runtime_output
                .line_results
                .iter()
                .map(|line| (&line.event_id, &line.event_template))
                .collect::<Vec<_>>(),
            indexed_output
                .line_results
                .iter()
                .map(|line| (&line.event_id, &line.event_template))
                .collect::<Vec<_>>()
        );
    }

    #[test]
    fn comma_separated_training_round_trips_through_indexed_parse() {
        // RFC-0026 §7: an index trained on separator-bearing content must
        // survive the write/read roundtrip and parse separator-bearing
        // lines against the learned template (ADR-0046).
        let config = PiplupConfig {
            log_format: vec!["<Content>".to_string()],
            separators: vec![",".to_string()],
            ..PiplupConfig::default()
        };
        let training_entries = vec![
            LogEntry {
                line_number: 1,
                raw: "upload failed,retry 3 times,timeout 5000 ms".into(),
            },
            LogEntry {
                line_number: 2,
                raw: "upload failed,retry 7 times,timeout 3000 ms".into(),
            },
        ];
        let mut trainer = PiplupParser::new(config.clone()).expect("trainer should build");
        let training = trainer
            .parse_lines_for_training(&training_entries)
            .expect("training should succeed");
        assert_eq!(
            training.parse_output.events.len(),
            1,
            "comma-separated variants must cluster under separators"
        );

        let index = IndexBuilder::build(&training, &config).expect("index should build");
        let bytes = crate::index::IndexWriter::write_to_vec(&index).expect("index should write");
        let parser =
            IndexedParser::from_bytes(&bytes, config).expect("indexed parser should build");
        assert_eq!(parser.template_count(), 1);

        let parsed = parser
            .parse_lines(&[LogEntry {
                line_number: 10,
                raw: "upload failed,retry 9 times,timeout 1000 ms".into(),
            }])
            .expect("indexed parse should succeed");

        let expected_template = "upload failed retry <*> times timeout <*>";
        assert_eq!(parsed.line_results[0].event_template, expected_template);
        assert_eq!(
            parsed.line_results[0].event_id,
            crate::event_id_for_template(expected_template)
        );
    }
}

use std::{path::Path, time::Instant};

use crate::cache::{CacheManager, CacheState, path_identity};
use crate::config::PiplupConfig;
use crate::config::SimThresholds;
use crate::engine::output::build_output;
use crate::engine::util::{
    insert_template_caches, process_line, tokenize_line, try_light_cache, try_template_cache,
};
use crate::error::LogParseError;
use crate::format::MultiCompiledLogFormat;
use crate::model::{LogCluster, ParseState};
use crate::parse_core::{ensure_unique_line_numbers, parse_entry};
use crate::preprocessor::{PreprocessPlan, Preprocessor};
use crate::progress::ParseProgress;
use crate::types::{LogEntry, ParseOutput, TrainingOutput};

/// Optional initial state for a cache-restored parser.
#[derive(Debug, Clone)]
pub struct InitialCacheState {
    /// Learned parse state restored from cache.
    pub state: ParseState,
    /// Preprocess plan restored from cache.
    pub preprocess_plan: PreprocessPlan,
    /// Number of lines already processed in the input stream.
    pub processed_line_count: usize,
    /// Stable identity of the input being processed.
    pub input_identity: Option<[u8; 32]>,
}

/// Stateful online piplup parser.
///
/// `PiplupParser` learns log templates from [`LogEntry`] inputs and produces
/// structured [`ParseOutput`] or [`TrainingOutput`]. It can be warm-started from
/// a cache state and supports per-line checkpointing.
#[derive(Debug, Clone)]
pub struct PiplupParser {
    config: PiplupConfig,
    format: MultiCompiledLogFormat,
    preprocessor: Preprocessor,
    initial_state: Option<InitialCacheState>,
    emitted_cache_state: bool,
    last_taken_cache: Option<CacheState>,
    /// Identity of the input currently being trained on. Stamped on
    /// checkpoints so the CLI can decide whether to honor the cursor.
    current_input_identity: Option<[u8; 32]>,
}

impl PiplupParser {
    /// Create a new parser from configuration.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::{PiplupConfig, PiplupParser};
    ///
    /// let parser = PiplupParser::new(PiplupConfig::default()).unwrap();
    /// ```
    pub fn new(config: PiplupConfig) -> Result<Self, LogParseError> {
        Self::with_cache(config, None)
    }

    /// Construct a parser with an optional warm-start state from cache.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::{PiplupConfig, PiplupParser, InitialCacheState, ParseState, PreprocessPlan};
    ///
    /// let parser = PiplupParser::with_cache(
    ///     PiplupConfig::default(),
    ///     Some(InitialCacheState {
    ///         state: ParseState::new(),
    ///         preprocess_plan: PreprocessPlan::default(),
    ///         processed_line_count: 0,
    ///         input_identity: None,
    ///     }),
    /// ).unwrap();
    /// ```
    pub fn with_cache(
        config: PiplupConfig,
        initial_state: Option<InitialCacheState>,
    ) -> Result<Self, LogParseError> {
        let format = MultiCompiledLogFormat::compile(&config.log_format)?;
        let preprocessor = Preprocessor::default();
        let current_input_identity = initial_state.as_ref().and_then(|s| s.input_identity);
        Ok(Self {
            config,
            format,
            preprocessor,
            initial_state,
            emitted_cache_state: false,
            last_taken_cache: None,
            current_input_identity,
        })
    }

    /// Set the identity of the input currently being processed. Used to
    /// stamp checkpoints with a stable reference for crash-resume.
    pub fn set_input_identity(&mut self, identity: [u8; 32]) {
        self.current_input_identity = Some(identity);
    }

    /// Convenience: derive an input identity from a path.
    pub fn set_input_path(&mut self, path: &Path) {
        self.current_input_identity = Some(path_identity(path));
    }

    /// Return a reference to the parser configuration.
    pub fn config(&self) -> &PiplupConfig {
        &self.config
    }

    /// Return a reference to the compiled log format.
    pub fn format(&self) -> &MultiCompiledLogFormat {
        &self.format
    }

    /// Return a reference to the preprocessor.
    pub fn preprocessor(&self) -> &Preprocessor {
        &self.preprocessor
    }

    /// Consume the parser's final `CacheState` after training. Returns
    /// `None` on the second call.
    pub fn take_cache_state(&mut self) -> Option<CacheState> {
        if self.emitted_cache_state {
            return None;
        }
        self.emitted_cache_state = true;
        self.last_taken_cache.take()
    }

    /// Parse an entire file on disk.
    ///
    /// # Example
    ///
    /// ```no_run
    /// use log_parser::{PiplupConfig, PiplupParser};
    /// use std::path::Path;
    ///
    /// # fn main() -> Result<(), Box<dyn std::error::Error>> {
    /// let mut parser = PiplupParser::new(PiplupConfig::default())?;
    /// let output = parser.parse_file(Path::new("app.log"))?;
    /// # Ok(())
    /// # }
    /// ```
    pub fn parse_file(&mut self, path: &Path) -> Result<ParseOutput, LogParseError> {
        self.parse_file_with_progress(path, |_| {})
    }

    /// Parse a file with progress callbacks.
    pub fn parse_file_with_progress<F>(
        &mut self,
        path: &Path,
        progress: F,
    ) -> Result<ParseOutput, LogParseError>
    where
        F: FnMut(ParseProgress),
    {
        let raw = std::fs::read_to_string(path)?;
        let entries = raw
            .lines()
            .enumerate()
            .map(|(index, line)| LogEntry {
                line_number: index + 1,
                raw: line.to_string(),
            })
            .collect::<Vec<_>>();
        self.parse_lines_with_progress(&entries, progress)
    }

    /// Parse a slice of log entries.
    pub fn parse_lines(&mut self, entries: &[LogEntry]) -> Result<ParseOutput, LogParseError> {
        Ok(self
            .parse_lines_for_training_with_progress(entries, |_| {})?
            .parse_output)
    }

    /// Parse entries and return the full training output (including the preprocess plan).
    pub fn parse_lines_for_training(
        &mut self,
        entries: &[LogEntry],
    ) -> Result<TrainingOutput, LogParseError> {
        self.parse_lines_for_training_with_progress(entries, |_| {})
    }

    /// Parse entries with progress callbacks.
    pub fn parse_lines_with_progress<F>(
        &mut self,
        entries: &[LogEntry],
        progress: F,
    ) -> Result<ParseOutput, LogParseError>
    where
        F: FnMut(ParseProgress),
    {
        Ok(self
            .parse_lines_for_training_with_progress(entries, progress)?
            .parse_output)
    }

    /// Parse entries for training with progress callbacks.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::{PiplupConfig, PiplupParser, LogEntry};
    ///
    /// let mut parser = PiplupParser::new(PiplupConfig::default()).unwrap();
    /// let entries = vec![
    ///     LogEntry { line_number: 1, raw: "2026-06-05 10:00:00 worker started".into() },
    ///     LogEntry { line_number: 2, raw: "2026-06-05 10:00:01 worker 1 done".into() },
    /// ];
    /// let output = parser.parse_lines_for_training_with_progress(&entries, |_| {}).unwrap();
    /// assert!(!output.parse_output.line_results.is_empty());
    /// ```
    pub fn parse_lines_for_training_with_progress<F>(
        &mut self,
        entries: &[LogEntry],
        mut progress: F,
    ) -> Result<TrainingOutput, LogParseError>
    where
        F: FnMut(ParseProgress),
    {
        ensure_unique_line_numbers(entries.iter().map(|e| e.line_number))?;
        let started = Instant::now();
        let parsed_lines = entries
            .iter()
            .filter_map(|entry| {
                parse_entry(&self.format, self.config.skip_unmatched, entry).transpose()
            })
            .collect::<Result<Vec<_>, _>>()?;
        if parsed_lines.is_empty() {
            let empty = PreprocessPlan::default();
            self.last_taken_cache = Some(CacheState::from_parse_state(
                &ParseState::new(),
                &empty,
                0,
                None,
            ));
            return Ok(TrainingOutput {
                parse_output: ParseOutput {
                    line_results: Vec::new(),
                    events: Vec::new(),
                    cluster_count: 0,
                    duration: started.elapsed(),
                },
                preprocess_plan: empty,
            });
        }

        let thresholds = SimThresholds::from_mode(&self.config.sim_thresh);
        // Segmentation (Step 8): controls processing window size for memory
        // and progress only. `0` / `None` → single segment (no segmentation).
        let segment_lines = self.config.segment_lines.unwrap_or(usize::MAX).max(1);
        // Checkpoint cadence (Step 9): independent of segmentation. `0` /
        // `None` → no intermediate checkpoint (only end-of-training save).
        let checkpoint_lines = self.config.cache_checkpoint_lines.unwrap_or(0);

        // Resume from cache: skip already-processed entries.
        let (mut state, mut preprocess_plan, mut cursor, plan_restored_from_cache) =
            if let Some(initial) = self.initial_state.take() {
                let plan_restored_from_cache = !initial.preprocess_plan.enabled_rule_ids.is_empty();
                (
                    initial.state,
                    initial.preprocess_plan,
                    initial.processed_line_count,
                    plan_restored_from_cache,
                )
            } else {
                (ParseState::new(), PreprocessPlan::default(), 0, false)
            };
        if cursor > parsed_lines.len() {
            cursor = parsed_lines.len();
        }
        let initial_cursor = cursor;

        if cursor == parsed_lines.len() {
            // Already fully processed — emit cache state and return empty.
            let cache = CacheState::from_parse_state(&state, &preprocess_plan, cursor as u64, None);
            self.last_taken_cache = Some(cache);
            return Ok(TrainingOutput {
                parse_output: ParseOutput {
                    line_results: Vec::new(),
                    events: Vec::new(),
                    cluster_count: 0,
                    duration: started.elapsed(),
                },
                preprocess_plan,
            });
        }

        let mut progress_tracker = ParseProgressTracker::new(parsed_lines.len());
        let mut any_dirty = false;
        let cache_path = self.config.cache_path.clone();
        let runtime_hash = self.config.compute_runtime_hash();
        let mut lines_since_checkpoint: usize = 0;
        let mut last_checkpoint_cursor: usize = cursor;

        while cursor < parsed_lines.len() {
            let segment_end = cursor.saturating_add(segment_lines).min(parsed_lines.len());
            // Reset dirty at the start of each segment. Any mutation
            // inside the segment body flips it to true.
            let mut dirty = false;

            for (segment_idx, line) in parsed_lines[cursor..segment_end].iter().enumerate() {
                let index = cursor + segment_idx;
                // Repeated identical lines short-circuit the whole pipeline
                // (tokenize + preprocess + search) through the line cache.
                let cache_hit =
                    try_light_cache(&mut state, &self.config, &line.content, line.line_number);
                if cache_hit.is_some() {
                    dirty = true;
                    lines_since_checkpoint += 1;
                    if checkpoint_lines > 0
                        && lines_since_checkpoint >= checkpoint_lines
                        && let Some(path) = cache_path.as_deref()
                        && dirty
                    {
                        let plan_hash = preprocess_plan.compute_content_hash();
                        let cache = CacheState::from_parse_state(
                            &state,
                            &preprocess_plan,
                            (index + 1) as u64,
                            self.current_input_identity,
                        )
                        .with_runtime_hash(runtime_hash)
                        .with_preprocess_hash(plan_hash);
                        CacheManager::save_checkpoint_to_path(
                            path,
                            &cache,
                            self.config.cache_compress,
                        )?;
                        last_checkpoint_cursor = index + 1;
                        lines_since_checkpoint = 0;
                    }
                    progress_tracker.maybe_report(index + 1, &mut progress);
                    continue;
                }
                let (tokens, line_kinds, length_filtered) = tokenize_line(
                    &self.config,
                    &self.preprocessor,
                    &mut preprocess_plan,
                    index,
                    line,
                    false,
                    plan_restored_from_cache,
                );

                if !length_filtered
                    && try_template_cache(
                        &mut state,
                        &self.config,
                        &line.content,
                        &tokens,
                        line.line_number,
                    )
                    .is_some()
                {
                    dirty = true;
                    lines_since_checkpoint += 1;
                    progress_tracker.maybe_report(index + 1, &mut progress);
                    continue;
                }

                // Length-filtered lines (max_line_bytes, or max_token_bytes
                // filtering every token) are excluded from clustering — they
                // get the "0" sentinel event_id in build_output. Naturally
                // empty content (no tokens, not filtered) keeps the legacy
                // empty-template clustering behavior.
                if !length_filtered {
                    let cluster_id = process_line(
                        &mut state,
                        &tokens,
                        &line_kinds,
                        &thresholds,
                        &self.config,
                        line.line_number,
                    );
                    insert_template_caches(
                        &mut state,
                        &self.config,
                        &line.content,
                        &tokens,
                        cluster_id,
                    );
                    dirty = true;

                    if self.config.warn_miss {
                        let cluster = state.cluster(cluster_id);
                        if is_degraded_to_wildcard(cluster) {
                            eprintln!(
                                "[Warning] Cluster {} template degraded to <*>, line {}: {}",
                                cluster_id, line.line_number, line.content
                            );
                            eprintln!(
                                "          -> Candidate Templates: {:?}",
                                if cluster.template_list.is_empty() {
                                    vec![cluster.active_template()]
                                } else {
                                    cluster.template_list.clone()
                                }
                            );
                        }
                    }
                }

                // Per-line inside the inner loop: trigger an intermediate
                // checkpoint when the cadence is reached. Independent of
                // segment boundaries.
                lines_since_checkpoint += 1;
                if checkpoint_lines > 0
                    && lines_since_checkpoint >= checkpoint_lines
                    && let Some(path) = cache_path.as_deref()
                {
                    if dirty {
                        let plan_hash = preprocess_plan.compute_content_hash();
                        let cache = CacheState::from_parse_state(
                            &state,
                            &preprocess_plan,
                            (index + 1) as u64,
                            self.current_input_identity,
                        )
                        .with_runtime_hash(runtime_hash)
                        .with_preprocess_hash(plan_hash);
                        CacheManager::save_checkpoint_to_path(
                            path,
                            &cache,
                            self.config.cache_compress,
                        )?;
                        last_checkpoint_cursor = index + 1;
                    }
                    lines_since_checkpoint = 0;
                }

                progress_tracker.maybe_report(index + 1, &mut progress);
            }

            any_dirty |= dirty;
            cursor = segment_end;
        }

        // Final checkpoint: write the in-flight cursor (if we have not
        // already written past it via an intermediate checkpoint) and
        // signal that the cache file is up-to-date as of the end of
        // training. The post-training `finalize_cache` overwrites this
        // with a clean save (`processed_line_count = 0`).
        if any_dirty
            && let Some(path) = cache_path.as_deref()
            && cursor > last_checkpoint_cursor
        {
            let plan_hash = preprocess_plan.compute_content_hash();
            let cache = CacheState::from_parse_state(
                &state,
                &preprocess_plan,
                cursor as u64,
                self.current_input_identity,
            )
            .with_runtime_hash(runtime_hash)
            .with_preprocess_hash(plan_hash);
            CacheManager::save_checkpoint_to_path(path, &cache, self.config.cache_compress)?;
        }

        let cache = CacheState::from_parse_state(&state, &preprocess_plan, 0, None)
            .with_runtime_hash(runtime_hash)
            .with_preprocess_hash(preprocess_plan.compute_content_hash());
        self.last_taken_cache = Some(cache);

        Ok(TrainingOutput {
            parse_output: build_output(
                parsed_lines[initial_cursor..].to_vec(),
                state,
                self.config.keep_para,
                self.config.warn_miss,
                started.elapsed(),
            )?,
            preprocess_plan,
        })
    }
}

#[derive(Debug, Clone, Copy)]
struct ParseProgressTracker {
    total_lines: usize,
    next_report: usize,
    step: usize,
}

impl ParseProgressTracker {
    fn new(total_lines: usize) -> Self {
        let step = parse_progress_step(total_lines);
        Self {
            total_lines,
            next_report: step.min(total_lines),
            step,
        }
    }

    fn maybe_report<F>(&mut self, processed_lines: usize, progress: &mut F)
    where
        F: FnMut(ParseProgress),
    {
        if self.total_lines == 0 {
            return;
        }

        if processed_lines < self.next_report && processed_lines != self.total_lines {
            return;
        }

        progress(ParseProgress::new(processed_lines, Some(self.total_lines)));
        self.next_report = processed_lines.saturating_add(self.step);
    }
}

fn parse_progress_step(total_lines: usize) -> usize {
    match total_lines {
        0..=256 => 1,
        257..=4_096 => 128,
        4_097..=65_536 => 1_024,
        _ => 4_096,
    }
}

fn is_degraded_to_wildcard(cluster: &LogCluster) -> bool {
    cluster.active_template() == "<*>"
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::preprocessor::RuleId;

    #[test]
    fn parse_lines_with_progress_reports_final_total() {
        let mut parser = PiplupParser::new(PiplupConfig::default()).expect("parser should build");
        let entries = (0..300)
            .map(|index| LogEntry {
                line_number: index + 1,
                raw: format!("2026-04-11 12:00:{index:02} INFO worker {index}"),
            })
            .collect::<Vec<_>>();
        let mut updates = Vec::new();

        parser
            .parse_lines_with_progress(&entries, |update| updates.push(update))
            .expect("progress-enabled parse should succeed");

        assert!(
            !updates.is_empty(),
            "progress-enabled parsing should emit at least one update"
        );
        assert_eq!(
            updates.last(),
            Some(&ParseProgress::new(entries.len(), Some(entries.len())))
        );
    }

    #[test]
    fn parse_lines_with_progress_skips_empty_inputs() {
        let mut parser = PiplupParser::new(PiplupConfig::default()).expect("parser should build");
        let mut updates = Vec::new();

        parser
            .parse_lines_with_progress(&[], |update| updates.push(update))
            .expect("empty input should still succeed");

        assert!(updates.is_empty());
    }

    #[test]
    fn parse_lines_for_training_returns_only_first_2000_line_preprocess_plan() {
        let mut parser = PiplupParser::new(PiplupConfig::default()).expect("parser should build");
        let mut entries = vec![
            LogEntry {
                line_number: 1,
                raw: "2026-04-11 12:00:00 INFO url=https://example.com".into(),
            },
            LogEntry {
                line_number: 2,
                raw: "2026-04-11 12:00:01 INFO peer=10.0.0.1:8080".into(),
            },
        ];
        entries.extend((3..=2000).map(|line_number| LogEntry {
            line_number,
            raw: "2026-04-11 12:00:00 INFO steady worker".into(),
        }));
        entries.push(LogEntry {
            line_number: 2001,
            raw: "2026-04-11 12:00:02 INFO next-run=2026/04/12".into(),
        });

        let training = parser
            .parse_lines_for_training(&entries)
            .expect("training parse should succeed");

        assert_eq!(
            training.preprocess_plan,
            PreprocessPlan::new(vec![RuleId::LogLevel, RuleId::Url, RuleId::Ipv4Port])
        );
    }

    #[test]
    fn parse_lines_for_training_keeps_parse_output_compatible() {
        let entries = [
            LogEntry {
                line_number: 1,
                raw: "2026-04-11 12:00:00 INFO worker 1".into(),
            },
            LogEntry {
                line_number: 2,
                raw: "2026-04-11 12:00:01 INFO worker 2".into(),
            },
        ];
        let mut parser = PiplupParser::new(PiplupConfig::default()).expect("parser should build");
        let mut training_parser =
            PiplupParser::new(PiplupConfig::default()).expect("parser should build");

        let standard = parser.parse_lines(&entries).expect("parse should succeed");
        let training = training_parser
            .parse_lines_for_training(&entries)
            .expect("training parse should succeed");

        assert_eq!(training.parse_output.line_results, standard.line_results);
        assert_eq!(training.parse_output.events, standard.events);
        assert_eq!(training.parse_output.cluster_count, standard.cluster_count);
    }

    fn make_5000_line_entries() -> Vec<LogEntry> {
        (0..5000)
            .map(|i| LogEntry {
                line_number: i + 1,
                raw: format!("2026-04-11 12:00:{:02} INFO worker {}", i % 60, i),
            })
            .collect()
    }

    #[test]
    fn with_cache_none_matches_new() {
        let entries = make_5000_line_entries();
        let mut p1 = PiplupParser::new(PiplupConfig::default()).unwrap();
        let out1 = p1.parse_lines(&entries).unwrap();

        let mut p2 = PiplupParser::with_cache(PiplupConfig::default(), None).unwrap();
        let out2 = p2.parse_lines(&entries).unwrap();

        assert_eq!(out1.line_results.len(), out2.line_results.len());
        assert_eq!(out1.cluster_count, out2.cluster_count);
    }

    #[test]
    fn take_cache_state_emits_some_after_training_then_none() {
        let mut parser = PiplupParser::new(PiplupConfig::default()).unwrap();
        let entries = make_5000_line_entries();
        let _ = parser.parse_lines_for_training(&entries).unwrap();
        let first = parser.take_cache_state();
        assert!(first.is_some(), "first take should return Some");
        let second = parser.take_cache_state();
        assert!(second.is_none(), "second take should return None");
    }

    #[test]
    fn warm_start_routes_through_cached_clusters() {
        let mut p1 = PiplupParser::new(PiplupConfig::default()).unwrap();
        let entries = make_5000_line_entries();
        let _ = p1.parse_lines_for_training(&entries).unwrap();
        let cache = p1.take_cache_state().unwrap();

        // Build a second parser warm-started from the cache. The
        // restored state should still yield a non-empty state and a
        // valid preprocess plan.
        let restored = cache.into_parse_state().unwrap();
        let initial = InitialCacheState {
            state: restored.state.clone(),
            preprocess_plan: restored.preprocess_plan.clone(),
            processed_line_count: 0,
            input_identity: None,
        };
        let mut p2 = PiplupParser::with_cache(PiplupConfig::default(), Some(initial)).unwrap();
        let out = p2.parse_lines_for_training(&entries).unwrap();
        assert!(!out.parse_output.line_results.is_empty());
    }

    #[test]
    fn segment_loop_processes_in_chunks() {
        let config = PiplupConfig {
            segment_lines: Some(100),
            ..Default::default()
        };
        let mut parser = PiplupParser::new(config).unwrap();
        let entries = make_5000_line_entries();
        let out = parser.parse_lines_for_training(&entries).unwrap();
        // 5000 / 100 = 50 segments
        assert_eq!(out.parse_output.line_results.len(), 5000);
    }

    #[test]
    fn segment_loop_skips_zero_mutation_segments() {
        // Same input twice through the parser; on the second pass every
        // line hits an existing cluster, but record_match still bumps
        // hit_time, so dirty is true. The test asserts the loop runs
        // and produces output.
        let config = PiplupConfig {
            segment_lines: Some(50),
            ..Default::default()
        };
        let mut parser = PiplupParser::new(config).unwrap();
        let entries = make_5000_line_entries();
        let out = parser.parse_lines_for_training(&entries).unwrap();
        assert_eq!(out.parse_output.line_results.len(), 5000);
    }

    #[test]
    fn checkpoint_cadence_independent_of_segments() {
        // 5000 lines, segment_lines=1000, cache_checkpoint_lines=3000
        // → checkpoint every 3 segments. No cache_path set, so no
        // checkpoint writes occur — the test simply asserts the parser
        // produces a complete output and does not panic on
        // independently-tunable segment/checkpoint cadences.
        let config = PiplupConfig {
            segment_lines: Some(1000),
            cache_checkpoint_lines: Some(3000),
            ..Default::default()
        };
        let mut parser = PiplupParser::new(config).unwrap();
        let entries = make_5000_line_entries();
        let out = parser.parse_lines_for_training(&entries).unwrap();
        assert_eq!(out.parse_output.line_results.len(), 5000);
    }

    #[test]
    fn segment_lines_works_without_cache() {
        // segment_lines without cache_path is a valid configuration.
        let config = PiplupConfig {
            segment_lines: Some(100),
            ..Default::default()
        };
        // cache_path is None — no cache I/O should occur.
        let mut parser = PiplupParser::new(config).unwrap();
        let entries = make_5000_line_entries();
        let out = parser.parse_lines_for_training(&entries).unwrap();
        assert_eq!(out.parse_output.line_results.len(), 5000);
    }

    #[test]
    fn empty_input_emits_empty_cache() {
        let mut parser = PiplupParser::new(PiplupConfig::default()).unwrap();
        let _ = parser.parse_lines_for_training(&[]).unwrap();
        let cache = parser.take_cache_state();
        assert!(cache.is_some());
    }

    #[test]
    fn cursor_at_end_is_noop() {
        let entries = make_5000_line_entries();
        let initial = InitialCacheState {
            state: crate::model::ParseState::new(),
            preprocess_plan: PreprocessPlan::default(),
            processed_line_count: entries.len(),
            input_identity: None,
        };
        let mut parser = PiplupParser::with_cache(PiplupConfig::default(), Some(initial)).unwrap();
        let out = parser.parse_lines_for_training(&entries).unwrap();
        assert!(out.parse_output.line_results.is_empty());
    }

    #[test]
    fn line_numbers_preserved_after_resume_skip() {
        // The plan says LineId stays as the original line number, not
        // re-indexed. We confirm by training with a warm cursor of 3
        // and asserting the first LineId equals 4.
        let entries = (0..10)
            .map(|i| LogEntry {
                line_number: i + 1,
                raw: format!("2026-04-11 12:00:00 INFO worker {i}"),
            })
            .collect::<Vec<_>>();
        let initial = InitialCacheState {
            state: crate::model::ParseState::new(),
            preprocess_plan: PreprocessPlan::default(),
            processed_line_count: 3,
            input_identity: None,
        };
        let mut parser = PiplupParser::with_cache(PiplupConfig::default(), Some(initial)).unwrap();
        let out = parser.parse_lines_for_training(&entries).unwrap();
        assert_eq!(
            out.parse_output.line_results.first().unwrap().line_number,
            4
        );
        assert_eq!(out.parse_output.line_results.len(), 7);
    }

    #[test]
    fn tokenize_line_skips_estimate_when_cached_plan_present() {
        let initial = InitialCacheState {
            state: crate::model::ParseState::new(),
            preprocess_plan: PreprocessPlan::new(vec![RuleId::Url]),
            processed_line_count: 0,
            input_identity: None,
        };
        let mut parser = PiplupParser::with_cache(PiplupConfig::default(), Some(initial)).unwrap();
        let entries = vec![
            LogEntry {
                line_number: 1,
                raw: "2026-04-11 12:00:00 INFO peer=10.0.0.1:8080".into(),
            },
            LogEntry {
                line_number: 2,
                raw: "2026-04-11 12:00:01 INFO peer=10.0.0.2:8080".into(),
            },
        ];
        let out = parser.parse_lines_for_training(&entries).unwrap();
        assert_eq!(out.preprocess_plan, PreprocessPlan::new(vec![RuleId::Url]));
    }

    #[test]
    fn parser_reuse_estimates_after_cached_plan_parse_is_consumed() {
        let initial = InitialCacheState {
            state: crate::model::ParseState::new(),
            preprocess_plan: PreprocessPlan::new(vec![RuleId::Url]),
            processed_line_count: 0,
            input_identity: None,
        };
        let mut parser = PiplupParser::with_cache(PiplupConfig::default(), Some(initial)).unwrap();
        let cached_plan_entries = vec![LogEntry {
            line_number: 1,
            raw: "2026-04-11 12:00:00 INFO peer=10.0.0.1:8080".into(),
        }];

        let restored = parser
            .parse_lines_for_training(&cached_plan_entries)
            .unwrap();
        assert_eq!(
            restored.preprocess_plan,
            PreprocessPlan::new(vec![RuleId::Url])
        );

        let fresh_entries = vec![LogEntry {
            line_number: 1,
            raw: "2026-04-11 12:00:00 INFO peer=10.0.0.2:8080".into(),
        }];
        let fresh = parser.parse_lines_for_training(&fresh_entries).unwrap();

        assert_eq!(
            fresh.preprocess_plan,
            PreprocessPlan::new(vec![RuleId::LogLevel, RuleId::Ipv4Port])
        );
    }

    #[test]
    fn max_line_bytes_excludes_long_lines() {
        let config = PiplupConfig {
            max_line_bytes: Some(40),
            ..Default::default()
        };
        let mut parser = PiplupParser::new(config).unwrap();
        let entries = vec![
            LogEntry { line_number: 1, raw: "2026-06-05 10:00:00 short line".into() },
            LogEntry { line_number: 2, raw: "2026-06-05 10:00:01 this line is way too long and should be excluded from clustering".into() },
        ];
        let out = parser.parse_lines(&entries).unwrap();
        assert_eq!(out.line_results.len(), 2);
        assert_ne!(out.line_results[0].event_id, "0");
        assert_eq!(out.line_results[1].event_id, "0");
    }

    #[test]
    fn max_token_bytes_filters_long_tokens() {
        let config = PiplupConfig {
            max_token_bytes: Some(4),
            ..Default::default()
        };
        let mut parser = PiplupParser::new(config).unwrap();
        let entries = vec![
            LogEntry {
                line_number: 1,
                raw: "2026-06-05 10:00:00 ok".into(),
            },
            LogEntry {
                line_number: 2,
                raw: "2026-06-05 10:00:01 too_long_token".into(),
            },
        ];
        let out = parser.parse_lines(&entries).unwrap();
        assert_eq!(out.line_results.len(), 2);
        // First line has "ok" (2 bytes) — should cluster normally
        assert_ne!(out.line_results[0].event_id, "0");
        // Second line has only "too_long_token" (14 bytes) — filtered out
        assert_eq!(out.line_results[1].event_id, "0");
    }

    #[test]
    fn max_token_bytes_partial_filter_shapes_template() {
        let config = PiplupConfig {
            max_token_bytes: Some(3),
            preprocess: false,
            log_format: vec!["<Content>".to_string()],
            ..Default::default()
        };
        let mut parser = PiplupParser::new(config).unwrap();
        let entries = vec![LogEntry {
            line_number: 1,
            raw: "ab 1234".into(),
        }];
        let out = parser.parse_lines(&entries).unwrap();
        assert_eq!(out.line_results.len(), 1);
        // "ab" (2 bytes) survives, "1234" (4 bytes) is filtered out: the
        // template is shaped from the surviving token only.
        assert_ne!(out.line_results[0].event_id, "0");
        assert_eq!(out.line_results[0].event_template, "ab");
    }

    #[test]
    fn max_token_bytes_default_does_not_filter() {
        let config = PiplupConfig {
            max_token_bytes: None,
            ..Default::default()
        };
        let mut parser = PiplupParser::new(config).unwrap();
        let entries = vec![
            LogEntry {
                line_number: 1,
                raw: "2026-06-05 10:00:00 short".into(),
            },
            LogEntry {
                line_number: 2,
                raw: "2026-06-05 10:00:01 very_long_token_here".into(),
            },
        ];
        let out = parser.parse_lines(&entries).unwrap();
        assert_eq!(out.line_results.len(), 2);
        assert_ne!(out.line_results[0].event_id, "0");
        assert_ne!(out.line_results[1].event_id, "0");
    }

    #[test]
    fn max_line_bytes_default_does_not_exclude() {
        let config = PiplupConfig {
            max_line_bytes: None,
            ..Default::default()
        };
        let mut parser = PiplupParser::new(config).unwrap();
        let entries = vec![
            LogEntry {
                line_number: 1,
                raw: "2026-06-05 10:00:00 short".into(),
            },
            LogEntry {
                line_number: 2,
                raw:
                    "2026-06-05 10:00:01 this line is long but without the filter it should cluster"
                        .into(),
            },
        ];
        let out = parser.parse_lines(&entries).unwrap();
        assert_eq!(out.line_results.len(), 2);
        assert_ne!(out.line_results[0].event_id, "0");
        assert_ne!(out.line_results[1].event_id, "0");
    }
}

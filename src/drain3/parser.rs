use std::path::Path;
use std::time::Instant;

use crate::drain3::cache::{Drain3CacheManager, Drain3CacheState, drain3_path_identity};
use crate::drain3::config::Drain3Config;
use crate::drain3::minhash;
use crate::drain3::state::{self, DrainState, SearchResult};
use crate::drain3::token;
use crate::error::LogParseError;
use crate::format::MultiCompiledLogFormat;
use crate::model::{Token, token::render_token_list_joined};
use crate::output_core::assignment::{LearnedTemplateAssignment, build_output_from_assignments};
use crate::parse_core::{ensure_unique_line_numbers, parse_entry};
use crate::placeholder::PlaceholderKind;
use crate::preprocessor::{PreprocessPlan, Preprocessor, TYPED_OWNED_RULE_IDS};
use crate::types::{LogEntry, ParseOutput, ParsedLine, TrainingOutput};

/// Optional initial state for a Drain3 parser restored from checkpoint cache.
#[derive(Debug, Clone)]
pub struct Drain3InitialCacheState {
    pub state: DrainState,
    pub preprocess_plan: PreprocessPlan,
    pub processed_line_count: usize,
    pub input_identity: [u8; 32],
}

impl From<crate::drain3::cache::Drain3RestoredState> for Drain3InitialCacheState {
    fn from(restored: crate::drain3::cache::Drain3RestoredState) -> Self {
        Self {
            state: restored.state,
            preprocess_plan: restored.preprocess_plan,
            processed_line_count: restored.processed_line_count,
            input_identity: restored.input_identity,
        }
    }
}

/// Drain3 log template parser.
///
/// Implements the Drain3 fixed-depth tree clustering algorithm. The public API
/// mirrors `PiplupParser` so the CLI can dispatch to either algorithm.
///
/// Drain3 keeps the public rendered `Vec<String>` template surface for API
/// compatibility, while using the shared [`Token`] model internally for
/// wildcard semantics.
#[derive(Debug, Clone)]
pub struct Drain3Parser {
    config: Drain3Config,
    format: MultiCompiledLogFormat,
    preprocessor: Preprocessor,
    state: DrainState,
    initial_state: Option<Drain3InitialCacheState>,
    emitted_cache_state: bool,
    last_taken_cache: Option<Drain3CacheState>,
    current_input_identity: Option<[u8; 32]>,
}

impl Drain3Parser {
    /// Create a new parser with the given configuration.
    pub fn new(config: Drain3Config) -> Result<Self, LogParseError> {
        Self::with_cache(config, None)
    }

    /// Construct a parser with an optional warm-start state from cache.
    pub fn with_cache(
        config: Drain3Config,
        initial_state: Option<Drain3InitialCacheState>,
    ) -> Result<Self, LogParseError> {
        config.validate()?;
        let format = MultiCompiledLogFormat::compile(&config.log_format)?;
        let preprocessor = Preprocessor::default();
        let current_input_identity = initial_state.as_ref().map(|state| state.input_identity);
        let mut state = initial_state
            .as_ref()
            .map(|state| state.state.clone())
            .unwrap_or_default();
        // ADR-0033 改良③: carry the minhash config on the state and (re)compute
        // cluster signatures. On warm-start the restored clusters get fresh sigs.
        state.set_minhash_config(config.minhash_config());
        Ok(Self {
            config,
            format,
            preprocessor,
            state,
            initial_state,
            emitted_cache_state: false,
            last_taken_cache: None,
            current_input_identity,
        })
    }

    /// Set the identity of the input currently being processed.
    pub fn set_input_identity(&mut self, identity: [u8; 32]) {
        self.current_input_identity = Some(identity);
    }

    /// Convenience: derive the Drain3 input identity from a path.
    pub fn set_input_path(&mut self, path: &Path) {
        self.current_input_identity = Some(drain3_path_identity(path));
    }

    /// Consume the parser's final `Drain3CacheState` after training.
    pub fn take_cache_state(&mut self) -> Option<Drain3CacheState> {
        if self.emitted_cache_state {
            return None;
        }
        self.emitted_cache_state = true;
        self.last_taken_cache.take()
    }

    /// Return a reference to the parser configuration.
    pub fn config(&self) -> &Drain3Config {
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

    /// Return a reference to the Drain3 state (tree + clusters).
    pub fn state(&self) -> &DrainState {
        &self.state
    }

    /// Parse a file and return structured output.
    pub fn parse_file(&mut self, path: &Path) -> Result<ParseOutput, LogParseError> {
        self.set_input_path(path);
        let raw = std::fs::read_to_string(path)?;
        let entries = raw
            .lines()
            .enumerate()
            .map(|(index, line)| LogEntry {
                line_number: index + 1,
                raw: line.to_string(),
            })
            .collect::<Vec<_>>();
        self.parse_lines(&entries)
    }

    /// Parse lines and return structured output.
    ///
    /// Delegates to `parse_lines_for_training` and discards the
    /// `PreprocessPlan`. The tree is populated/updated in-place.
    pub fn parse_lines(&mut self, entries: &[LogEntry]) -> Result<ParseOutput, LogParseError> {
        Ok(self.parse_lines_for_training(entries)?.parse_output)
    }

    /// Parse lines and return training output with learned templates.
    ///
    /// This is the main entry point for Drain3's online learning algorithm.
    /// For each log line it:
    /// 1. Parses through the log format → `ParsedLine`
    /// 2. Preprocesses the content (if enabled)
    /// 3. Tokenizes on whitespace
    /// 4. Computes the route path and navigates the fixed-depth tree
    /// 5. Matches against clusters or creates a new one
    /// 6. Updates the template via generalization
    pub fn parse_lines_for_training(
        &mut self,
        entries: &[LogEntry],
    ) -> Result<TrainingOutput, LogParseError> {
        ensure_unique_line_numbers(entries.iter().map(|entry| entry.line_number))?;
        self.emitted_cache_state = false;
        let started = Instant::now();

        let parsed_lines: Vec<ParsedLine> = entries
            .iter()
            .filter_map(|entry| {
                parse_entry(&self.format, self.config.skip_unmatched, entry).transpose()
            })
            .collect::<Result<Vec<_>, _>>()?;

        if parsed_lines.is_empty() {
            let preprocess_plan = PreprocessPlan::default();
            self.last_taken_cache = Some(self.cache_state(&preprocess_plan, 0)?);
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

        let (mut preprocess_plan, mut cursor) = if let Some(initial) = self.initial_state.take() {
            self.state = initial.state;
            (initial.preprocess_plan, initial.processed_line_count)
        } else {
            (PreprocessPlan::default(), 0)
        };
        // Re-apply minhash config after any warm-start state replacement so the
        // restored clusters get signatures sized to the current config.
        self.state.set_minhash_config(self.config.minhash_config());
        cursor = cursor.min(parsed_lines.len());
        let initial_cursor = cursor;

        if cursor == parsed_lines.len() {
            self.last_taken_cache =
                Some(self.cache_state(&preprocess_plan, parsed_lines.len() as u64)?);
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

        let segment_lines = self.config.segment_lines.unwrap_or(usize::MAX).max(1);
        let checkpoint_lines = self.config.cache_checkpoint_lines.unwrap_or(0);
        let cache_path = self.config.cache_path.clone();
        let mut assignments: Vec<Option<LearnedTemplateAssignment>> =
            vec![None; parsed_lines.len() - initial_cursor];
        let mut dirty = false;
        let mut lines_since_checkpoint = 0usize;
        let mut last_checkpoint_cursor = cursor;
        let search_ctx = state::SearchConfig {
            max_children: self.config.max_children,
            sim_thresh: self.config.sim_thresh,
            parametrize_numeric: self.config.parametrize_numeric_tokens,
            depth: self.config.depth,
            typed_placeholders: self.config.typed_placeholders,
        };

        while cursor < parsed_lines.len() {
            let segment_end = cursor.saturating_add(segment_lines).min(parsed_lines.len());

            for (segment_idx, line) in parsed_lines[cursor..segment_end].iter().enumerate() {
                let index = cursor + segment_idx;
                let (raw_tokens, normalized_tokens, line_kinds) =
                    self.tokenize_line_for_training(index, line, &mut preprocess_plan);

                if !raw_tokens.is_empty() {
                    // ADR-0033: in typed mode the search input is the REAL token
                    // text (so the lazy gate can match kinds against observed
                    // tokens — `<*>` would fail every kind predicate). Routing
                    // classifies the real tokens into per-kind branches. In eager
                    // mode the normalized (`<*>`-wildcarded) tokens are used as
                    // before.
                    let search_tokens: &[String] = if self.config.typed_placeholders {
                        &raw_tokens
                    } else {
                        &normalized_tokens
                    };
                    // ADR-0033 改良③: minhash signature over the normalized tokens
                    // for the variable-length fallback (empty when disabled).
                    let line_sig: Vec<u64> = if self.config.variable_length_clustering {
                        minhash::signature(&normalized_tokens, self.config.minhash_signature_size)
                    } else {
                        Vec::new()
                    };
                    let result = self.state.search(search_tokens, &search_ctx, &line_sig);

                    let (cluster_id, template) = match result {
                        SearchResult::Match(id) => {
                            let old_template = self.state.cluster(id).template_tokens.clone();
                            let mut template = old_template.clone();
                            // Generalize the template to cover the observed line. A
                            // same-length match uses positional generalization; a
                            // variable-length (minhash) match aligns by common prefix/suffix
                            // so a stable trailing literal survives (e.g. `worker alpha
                            // started` + `worker alpha beta started` → `worker alpha <*>
                            // started`). The route's TokenCount stays at the cluster's
                            // creation length (validate_cached_cluster allows the template
                            // to grow beyond it).
                            if template.len() == raw_tokens.len() {
                                state::generalize_template_tokens(&raw_tokens, &mut template);
                            } else {
                                state::generalize_template_variable_tokens(
                                    &raw_tokens,
                                    &mut template,
                                );
                            }
                            if template == old_template {
                                self.state.record_match_for_line(
                                    id,
                                    line.line_number,
                                    &line_kinds,
                                    self.config.typed_placeholders,
                                );
                            } else {
                                self.state.update_cluster_template_for_line(
                                    id,
                                    template.clone(),
                                    line.line_number,
                                    &line_kinds,
                                    self.config.typed_placeholders,
                                );
                            }
                            (id, template)
                        }
                        SearchResult::NewCluster(route_path) => {
                            let id = self.state.add_cluster_for_line(
                                normalized_tokens.clone(),
                                route_path,
                                line.line_number,
                            );
                            // The stored template wildcarded classified tokens to
                            // `<*>`; rebuild the mask from the real tokens so each
                            // placeholder slot records its kind.
                            if self.config.typed_placeholders {
                                self.state.set_cluster_mask(id, &line_kinds);
                            }
                            (id, self.state.cluster(id).template_tokens.clone())
                        }
                    };

                    assignments[index - initial_cursor] = Some(LearnedTemplateAssignment {
                        line_number: line.line_number,
                        cluster_id,
                        template: render_token_list_joined(&template),
                    });
                }

                // Cursor advancement is persisted state even when a parsed
                // line has no content tokens and receives the no-match sentinel;
                // checkpointing it avoids replay after a crash.
                dirty = true;
                lines_since_checkpoint += 1;
                if checkpoint_lines > 0
                    && lines_since_checkpoint >= checkpoint_lines
                    && let Some(path) = cache_path.as_deref()
                {
                    if dirty {
                        let checkpoint = self.cache_state(&preprocess_plan, (index + 1) as u64)?;
                        Drain3CacheManager::save_checkpoint_to_path(
                            path,
                            &checkpoint,
                            self.config.cache_compress,
                        )?;
                        dirty = false;
                        last_checkpoint_cursor = index + 1;
                    }
                    lines_since_checkpoint = 0;
                }
            }

            cursor = segment_end;
        }

        if dirty
            && let Some(path) = cache_path.as_deref()
            && cursor > last_checkpoint_cursor
        {
            let checkpoint = self.cache_state(&preprocess_plan, cursor as u64)?;
            Drain3CacheManager::save_checkpoint_to_path(
                path,
                &checkpoint,
                self.config.cache_compress,
            )?;
        }

        // Update all assignments with the FINAL cluster templates.
        // Earlier assignments captured snapshot templates; after all lines
        // are processed, each cluster's template reflects its final
        // generalized form.  Use that for every assignment so the output
        // has consistent EventTemplate values per cluster.
        for a in assignments.iter_mut().flatten() {
            a.template = self.state.cluster(a.cluster_id).template_string();
        }

        // Build output. Fill missing assignments with a no-match sentinel.
        let parsed_suffix = parsed_lines[initial_cursor..].to_vec();
        let final_assignments: Vec<LearnedTemplateAssignment> = assignments
            .into_iter()
            .enumerate()
            .map(|(idx, a)| {
                a.unwrap_or_else(|| LearnedTemplateAssignment {
                    line_number: parsed_suffix[idx].line_number,
                    cluster_id: usize::MAX,
                    template: "0".to_string(),
                })
            })
            .collect();
        let result = build_output_from_assignments(
            parsed_suffix,
            final_assignments,
            self.config.keep_para,
            self.config.warn_miss,
            started.elapsed(),
        )?;

        self.last_taken_cache = Some(self.cache_state(&preprocess_plan, cursor as u64)?);

        Ok(TrainingOutput {
            parse_output: result,
            preprocess_plan,
        })
    }

    /// Build structured output from the current state without mutating it or
    /// writing checkpoints. This is intended for train-resume artifact rebuilds.
    pub fn parse_lines_read_only(
        &self,
        entries: &[LogEntry],
        preprocess_plan: &PreprocessPlan,
    ) -> Result<ParseOutput, LogParseError> {
        ensure_unique_line_numbers(entries.iter().map(|entry| entry.line_number))?;
        let started = Instant::now();
        let parsed_lines: Vec<ParsedLine> = entries
            .iter()
            .filter_map(|entry| {
                parse_entry(&self.format, self.config.skip_unmatched, entry).transpose()
            })
            .collect::<Result<Vec<_>, _>>()?;

        let assignments = parsed_lines
            .iter()
            .map(|line| {
                let raw_tokens = self.tokenize_line_read_only(line, preprocess_plan);
                if raw_tokens.is_empty() {
                    return LearnedTemplateAssignment {
                        line_number: line.line_number,
                        cluster_id: usize::MAX,
                        template: "0".to_string(),
                    };
                }

                let (normalized_tokens, _line_kinds) = if self.config.typed_placeholders {
                    typed_template_tokens(&raw_tokens)
                } else {
                    (self.normalize_tokens(&raw_tokens), Vec::new())
                };
                // ADR-0033: typed mode searches against real token text so the
                // lazy gate can match kinds; eager mode uses normalized tokens.
                let search_tokens: &[String] = if self.config.typed_placeholders {
                    &raw_tokens
                } else {
                    &normalized_tokens
                };
                let line_sig: Vec<u64> = if self.config.variable_length_clustering {
                    minhash::signature(&normalized_tokens, self.config.minhash_signature_size)
                } else {
                    Vec::new()
                };
                let search_ctx = state::SearchConfig {
                    max_children: self.config.max_children,
                    sim_thresh: self.config.sim_thresh,
                    parametrize_numeric: self.config.parametrize_numeric_tokens,
                    depth: self.config.depth,
                    typed_placeholders: self.config.typed_placeholders,
                };
                match self
                    .state
                    .search_read_only(search_tokens, &search_ctx, &line_sig)
                {
                    Some(cluster_id) => LearnedTemplateAssignment {
                        line_number: line.line_number,
                        cluster_id,
                        template: self.state.cluster(cluster_id).template_string(),
                    },
                    None => LearnedTemplateAssignment {
                        line_number: line.line_number,
                        cluster_id: usize::MAX,
                        template: "0".to_string(),
                    },
                }
            })
            .collect::<Vec<_>>();

        build_output_from_assignments(
            parsed_lines,
            assignments,
            self.config.keep_para,
            self.config.warn_miss,
            started.elapsed(),
        )
    }

    fn tokenize_line_for_training(
        &self,
        index: usize,
        line: &ParsedLine,
        preprocess_plan: &mut PreprocessPlan,
    ) -> (Vec<String>, Vec<String>, Vec<Option<PlaceholderKind>>) {
        let processed = if !self.config.preprocess {
            line.content.clone()
        } else if index < 2000 {
            // ADR-0033 coexistence: in typed mode, skip the eager regex rules
            // whose shapes the typed classifier owns — neither run them (perf)
            // nor let them erase the shape before classification (mask correctness).
            let estimated = if self.config.typed_placeholders {
                self.preprocessor
                    .estimate_line_excluding(&line.content, TYPED_OWNED_RULE_IDS)
            } else {
                self.preprocessor.estimate_line(&line.content)
            };
            for rule_id in estimated.matched_rule_ids {
                preprocess_plan.push_unique_rule_id(rule_id);
            }
            estimated.output
        } else {
            self.preprocessor
                .apply_with_plan(&line.content, preprocess_plan)
        };

        // Exclude lines exceeding max_line_bytes from clustering.
        if let Some(max) = self.config.max_line_bytes
            && processed.len() > max
        {
            return (Vec::new(), Vec::new(), Vec::new());
        }

        let raw_tokens: Vec<String> = token::split_tokens(
            &processed,
            self.config.delimiter_split,
            &self.config.extra_delimiters,
        );
        // Filter tokens exceeding max_token_bytes.
        let raw_tokens: Vec<String> = if let Some(max) = self.config.max_token_bytes {
            raw_tokens.into_iter().filter(|t| t.len() <= max).collect()
        } else {
            raw_tokens
        };
        let (normalized_tokens, line_kinds) = if self.config.typed_placeholders {
            typed_template_tokens(&raw_tokens)
        } else {
            (self.normalize_tokens(&raw_tokens), Vec::new())
        };
        (raw_tokens, normalized_tokens, line_kinds)
    }

    fn tokenize_line_read_only(
        &self,
        line: &ParsedLine,
        preprocess_plan: &PreprocessPlan,
    ) -> Vec<String> {
        let processed = if !self.config.preprocess {
            line.content.clone()
        } else {
            self.preprocessor
                .apply_with_plan(&line.content, preprocess_plan)
        };

        // Exclude lines exceeding max_line_bytes from clustering.
        if let Some(max) = self.config.max_line_bytes
            && processed.len() > max
        {
            return Vec::new();
        }

        let tokens = token::split_tokens(
            &processed,
            self.config.delimiter_split,
            &self.config.extra_delimiters,
        );
        // Filter tokens exceeding max_token_bytes.
        if let Some(max) = self.config.max_token_bytes {
            tokens.into_iter().filter(|t| t.len() <= max).collect()
        } else {
            tokens
        }
    }

    fn normalize_tokens(&self, raw_tokens: &[String]) -> Vec<String> {
        raw_tokens
            .iter()
            .map(|t| {
                if token::is_wildcard(t, self.config.parametrize_numeric_tokens) {
                    "<*>".to_string()
                } else {
                    t.clone()
                }
            })
            .collect()
    }

    fn cache_state(
        &self,
        preprocess_plan: &PreprocessPlan,
        processed_line_count: u64,
    ) -> Result<Drain3CacheState, LogParseError> {
        let identity = self.cache_identity()?;
        Ok(Drain3CacheState::from_state(
            &self.state,
            preprocess_plan,
            processed_line_count,
            identity,
            &self.config,
        ))
    }

    fn cache_identity(&self) -> Result<[u8; 32], LogParseError> {
        match self.current_input_identity {
            Some(identity) => Ok(identity),
            None if self.config.cache_path.is_some() => {
                Err(LogParseError::CacheNotApplicableToStdin)
            }
            None => Ok([0; 32]),
        }
    }
}

fn typed_template_tokens(tokens: &[String]) -> (Vec<String>, Vec<Option<PlaceholderKind>>) {
    tokens
        .iter()
        .map(|token| {
            let (template_token, kind) = Token::from_typed_observed(token);
            (template_token.to_rendered_string(), kind)
        })
        .unzip()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::drain3::config::Drain3Config;
    use crate::drain3::state::DrainKey;
    use std::collections::BTreeSet;

    /// Helper: build a `Drain3Config` with a single log format.
    fn test_config() -> Drain3Config {
        Drain3Config {
            log_format: vec!["<Date> <Time> <Level> <Content>".to_string()],
            ..Drain3Config::default()
        }
    }

    fn make_entries(lines: &[&str]) -> Vec<LogEntry> {
        lines
            .iter()
            .enumerate()
            .map(|(i, line)| LogEntry {
                line_number: i + 1,
                raw: format!("2026-06-07 10:00:00 INFO {line}"),
            })
            .collect()
    }

    fn cache_identity() -> [u8; 32] {
        [0x55; 32]
    }

    // ── Basic parsing ────────────────────────────────────────────────

    #[test]
    fn parse_lines_for_training_produces_output() {
        let mut parser = Drain3Parser::new(test_config()).unwrap();
        let entries = make_entries(&["worker started", "worker stopped"]);
        let output = parser.parse_lines_for_training(&entries).unwrap();
        assert_eq!(output.parse_output.line_results.len(), 2);
        assert!(output.parse_output.cluster_count >= 1);
    }

    #[test]
    fn parse_lines_matches_training() {
        let mut p1 = Drain3Parser::new(test_config()).unwrap();
        let mut p2 = Drain3Parser::new(test_config()).unwrap();
        let entries = make_entries(&["worker started", "worker stopped"]);
        let out1 = p1.parse_lines(&entries).unwrap();
        let out2 = p2.parse_lines_for_training(&entries).unwrap().parse_output;
        assert_eq!(out1.line_results, out2.line_results);
        assert_eq!(out1.cluster_count, out2.cluster_count);
    }

    #[test]
    fn valid_cache_resumes_from_cursor() {
        let temp = tempfile::tempdir().unwrap();
        let cache_path = temp.path().join("drain3.cache");
        let config = Drain3Config {
            cache_path: Some(cache_path.clone()),
            preprocess: false,
            ..test_config()
        };
        let entries = make_entries(&[
            "worker 1 started",
            "worker 2 started",
            "worker 3 started",
            "worker 4 started",
        ]);

        let mut first = Drain3Parser::new(config.clone()).unwrap();
        first.set_input_identity(cache_identity());
        let _ = first.parse_lines_for_training(&entries[..2]).unwrap();
        let first_cache = first.take_cache_state().unwrap();
        Drain3CacheManager::save_to_path(&cache_path, &first_cache, false).unwrap();

        let restored = Drain3CacheManager::load_from_path(&cache_path)
            .unwrap()
            .into_restored_state(&config, cache_identity())
            .unwrap();
        let mut resumed = Drain3Parser::with_cache(config, Some(restored.into())).unwrap();
        let output = resumed.parse_lines_for_training(&entries).unwrap();

        assert_eq!(output.parse_output.line_results.len(), 2);
        assert_eq!(output.parse_output.line_results[0].line_number, 3);
        assert_eq!(
            resumed.take_cache_state().unwrap().processed_line_count,
            entries.len() as u64
        );
    }

    #[test]
    fn final_cache_writes_full_processed_count() {
        let temp = tempfile::tempdir().unwrap();
        let cache_path = temp.path().join("drain3.cache");
        let config = Drain3Config {
            cache_path: Some(cache_path.clone()),
            cache_checkpoint_lines: Some(2),
            preprocess: false,
            ..test_config()
        };
        let entries = make_entries(&[
            "worker 1 started",
            "worker 2 started",
            "worker 3 started",
            "worker 4 started",
            "worker 5 started",
        ]);

        let mut parser = Drain3Parser::new(config).unwrap();
        parser.set_input_identity(cache_identity());
        let _ = parser.parse_lines_for_training(&entries).unwrap();

        let cache = Drain3CacheManager::load_from_path(&cache_path).unwrap();
        assert_eq!(cache.processed_line_count, entries.len() as u64);
    }

    #[test]
    fn max_children_overflow_and_generalization_round_trip_matches_uninterrupted() {
        let config = Drain3Config {
            depth: 6,
            max_children: 1,
            preprocess: false,
            ..test_config()
        };
        let entries = make_entries(&[
            "root branch alpha tail",
            "root branch beta tail",
            "root branch gamma tail",
            "root branch delta tail",
        ]);

        let mut uninterrupted = Drain3Parser::new(config.clone()).unwrap();
        let uninterrupted_output = uninterrupted
            .parse_lines_for_training(&entries)
            .unwrap()
            .parse_output;

        let temp = tempfile::tempdir().unwrap();
        let cache_path = temp.path().join("drain3.cache");
        let cached_config = Drain3Config {
            cache_path: Some(cache_path.clone()),
            ..config.clone()
        };
        let mut first = Drain3Parser::new(cached_config.clone()).unwrap();
        first.set_input_identity(cache_identity());
        let _ = first.parse_lines_for_training(&entries[..2]).unwrap();
        let first_cache = first.take_cache_state().unwrap();
        Drain3CacheManager::save_to_path(&cache_path, &first_cache, false).unwrap();

        let wildcard_route = first_cache.clusters.iter().any(|cluster| {
            cluster.route_path.last() == Some(&crate::drain3::cache::Drain3CacheRouteKey::Wildcard)
        });
        assert!(wildcard_route, "cache must preserve max_children fallback");

        let restored = Drain3CacheManager::load_from_path(&cache_path)
            .unwrap()
            .into_restored_state(&cached_config, cache_identity())
            .unwrap();
        let mut resumed = Drain3Parser::with_cache(cached_config, Some(restored.into())).unwrap();
        let resumed_training = resumed.parse_lines_for_training(&entries).unwrap();
        let rebuilt = resumed
            .parse_lines_read_only(&entries, &resumed_training.preprocess_plan)
            .unwrap();

        assert_eq!(rebuilt.line_results, uninterrupted_output.line_results);
        assert_eq!(rebuilt.events, uninterrupted_output.events);
        assert_eq!(rebuilt.cluster_count, uninterrupted_output.cluster_count);
    }

    #[test]
    fn read_only_assignment_pass_does_not_mutate_cache_state() {
        let mut parser = Drain3Parser::new(Drain3Config {
            preprocess: false,
            ..test_config()
        })
        .unwrap();
        parser.set_input_identity(cache_identity());
        let entries = make_entries(&["worker 1 started", "worker 2 started"]);
        let training = parser.parse_lines_for_training(&entries).unwrap();
        let before = parser.take_cache_state().unwrap().state_hash().unwrap();

        let _ = parser
            .parse_lines_read_only(&entries, &training.preprocess_plan)
            .unwrap();
        let after = crate::drain3::cache::Drain3CacheState::from_state(
            parser.state(),
            &training.preprocess_plan,
            entries.len() as u64,
            cache_identity(),
            parser.config(),
        )
        .state_hash()
        .unwrap();

        assert_eq!(before, after);
    }

    #[test]
    fn parse_file_reads_and_parses() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("test.log");
        std::fs::write(
            &file,
            "2026-06-07 10:00:00 INFO worker started\n2026-06-07 10:00:01 INFO worker stopped\n",
        )
        .unwrap();
        let mut parser = Drain3Parser::new(test_config()).unwrap();
        let output = parser.parse_file(&file).unwrap();
        assert_eq!(output.line_results.len(), 2);
    }

    #[test]
    fn empty_input_returns_empty() {
        let mut parser = Drain3Parser::new(test_config()).unwrap();
        let output = parser.parse_lines_for_training(&[]).unwrap();
        assert!(output.parse_output.line_results.is_empty());
        assert_eq!(output.parse_output.cluster_count, 0);
    }

    // ── Template learning ────────────────────────────────────────────

    #[test]
    fn identical_lines_share_cluster() {
        let mut parser = Drain3Parser::new(test_config()).unwrap();
        let entries = make_entries(&["worker started", "worker started", "worker started"]);
        let output = parser.parse_lines_for_training(&entries).unwrap();
        assert_eq!(output.parse_output.cluster_count, 1);
        // All three should have the same event_id.
        let ids: BTreeSet<&str> = output
            .parse_output
            .line_results
            .iter()
            .map(|r| r.event_id.as_str())
            .collect();
        assert_eq!(ids.len(), 1);
    }

    #[test]
    fn different_lengths_create_separate_clusters() {
        let mut parser = Drain3Parser::new(test_config()).unwrap();
        let entries = make_entries(&["worker started", "worker has been started"]);
        let output = parser.parse_lines_for_training(&entries).unwrap();
        assert_eq!(output.parse_output.cluster_count, 2);
    }

    #[test]
    fn template_generalization_creates_wildcard() {
        let mut parser = Drain3Parser::new(test_config()).unwrap();
        let entries = make_entries(&["worker 1 started", "worker 2 started", "worker 3 started"]);
        let output = parser.parse_lines_for_training(&entries).unwrap();
        // All three should be in the same cluster with <*> in the template.
        assert_eq!(output.parse_output.cluster_count, 1);
        let template = &output.parse_output.events[0].event_template;
        assert!(
            template.contains("<*>"),
            "template should contain <*> after generalization: {template}"
        );
    }

    // ── Numeric token parametrization ────────────────────────────────

    #[test]
    fn numeric_tokens_parametrized_by_default() {
        let mut parser = Drain3Parser::new(test_config()).unwrap();
        let entries = make_entries(&["v2 worker started", "v3 worker started"]);
        let output = parser.parse_lines_for_training(&entries).unwrap();
        // v2 and v3 are numeric-containing → both route as <*>, same cluster.
        assert_eq!(output.parse_output.cluster_count, 1);
    }

    #[test]
    fn nan_is_not_parametrized() {
        let mut parser = Drain3Parser::new(test_config()).unwrap();
        let entries = make_entries(&["NaN worker started", "abc worker started"]);
        let output = parser.parse_lines_for_training(&entries).unwrap();
        // NaN is literal, abc is literal → different routing → 2 clusters.
        assert_eq!(output.parse_output.cluster_count, 2);
    }

    #[test]
    fn already_wildcard_is_wildcard() {
        let mut parser = Drain3Parser::new(test_config()).unwrap();
        let entries = make_entries(&["<*> worker started"]);
        let output = parser.parse_lines_for_training(&entries).unwrap();
        assert_eq!(output.parse_output.cluster_count, 1);
        let template = &output.parse_output.events[0].event_template;
        assert!(template.starts_with("<*>"));
    }

    #[test]
    fn numeric_parametrization_disabled_treats_numbers_as_literals() {
        let config = Drain3Config {
            log_format: vec!["<Date> <Time> <Level> <Content>".to_string()],
            parametrize_numeric_tokens: false,
            ..Drain3Config::default()
        };
        let mut parser = Drain3Parser::new(config).unwrap();
        let entries = make_entries(&["v2 worker started", "v3 worker started"]);
        let output = parser.parse_lines_for_training(&entries).unwrap();
        // v2 and v3 are different literals → different routing → 2 clusters.
        assert_eq!(output.parse_output.cluster_count, 2);
    }

    // ── EventId and parameter extraction ─────────────────────────────

    #[test]
    fn event_id_assigned_from_template() {
        let mut parser = Drain3Parser::new(test_config()).unwrap();
        let entries = make_entries(&["worker started"]);
        let output = parser.parse_lines_for_training(&entries).unwrap();
        let line = &output.parse_output.line_results[0];
        assert!(!line.event_id.is_empty());
        assert_ne!(line.event_id, "0");
    }

    #[test]
    fn unmatched_lines_get_zero_event_id() {
        // A line that doesn't match the format (requires literal brackets).
        let mut parser = Drain3Parser::new(Drain3Config {
            log_format: vec!["[<Date>] [<Time>] <Level> <Content>".to_string()],
            skip_unmatched: true,
            ..Drain3Config::default()
        })
        .unwrap();
        let entries = vec![LogEntry {
            line_number: 1,
            raw: "this does not match format".to_string(),
        }];
        let output = parser.parse_lines_for_training(&entries).unwrap();
        assert!(output.parse_output.line_results.is_empty());
    }

    #[test]
    fn keep_para_extracts_parameters() {
        let config = Drain3Config {
            log_format: vec!["<Date> <Time> <Level> <Content>".to_string()],
            keep_para: true,
            ..Drain3Config::default()
        };
        let mut parser = Drain3Parser::new(config).unwrap();
        let entries = make_entries(&["worker 42 started", "worker 99 started"]);
        let output = parser.parse_lines_for_training(&entries).unwrap();
        let line = &output.parse_output.line_results[0];
        assert!(line.parameter_list.is_some());
    }

    // ── Duplicate line numbers ───────────────────────────────────────

    #[test]
    fn duplicate_line_numbers_error() {
        let mut parser = Drain3Parser::new(test_config()).unwrap();
        let entries = vec![
            LogEntry {
                line_number: 1,
                raw: "2026-06-07 10:00:00 INFO worker started".to_string(),
            },
            LogEntry {
                line_number: 1,
                raw: "2026-06-07 10:00:01 INFO worker stopped".to_string(),
            },
        ];
        let result = parser.parse_lines_for_training(&entries);
        assert!(result.is_err());
    }

    // ── Preprocess plan ──────────────────────────────────────────────

    #[test]
    fn preprocess_plan_populated_when_enabled() {
        let config = Drain3Config {
            log_format: vec!["<Date> <Time> <Level> <Content>".to_string()],
            preprocess: true,
            ..Drain3Config::default()
        };
        let mut parser = Drain3Parser::new(config).unwrap();
        let entries = vec![LogEntry {
            line_number: 1,
            raw: "2026-06-07 10:00:00 INFO url=https://example.com".to_string(),
        }];
        let output = parser.parse_lines_for_training(&entries).unwrap();
        // The preprocess plan should have at least the URL rule.
        assert!(!output.preprocess_plan.enabled_rule_ids.is_empty());
    }

    #[test]
    fn preprocess_plan_empty_when_disabled() {
        let config = Drain3Config {
            log_format: vec!["<Date> <Time> <Level> <Content>".to_string()],
            preprocess: false,
            ..Drain3Config::default()
        };
        let mut parser = Drain3Parser::new(config).unwrap();
        let entries = make_entries(&["worker started"]);
        let output = parser.parse_lines_for_training(&entries).unwrap();
        assert!(output.preprocess_plan.enabled_rule_ids.is_empty());
    }

    // ── DrainKey routing coverage ────────────────────────────────────

    #[test]
    fn routing_key_v2_parametrized() {
        let path =
            DrainKey::route_path_from_tokens(&["v2".into(), "worker".into()], 4, true, false);
        assert_eq!(path[1], DrainKey::Wildcard);
    }

    #[test]
    fn routing_key_123_parametrized() {
        let path =
            DrainKey::route_path_from_tokens(&["123".into(), "worker".into()], 4, true, false);
        assert_eq!(path[1], DrainKey::Wildcard);
    }

    #[test]
    fn routing_key_0_parametrized() {
        let path = DrainKey::route_path_from_tokens(&["0".into(), "worker".into()], 4, true, false);
        assert_eq!(path[1], DrainKey::Wildcard);
    }

    #[test]
    fn routing_key_a1b_parametrized() {
        let path =
            DrainKey::route_path_from_tokens(&["a1b".into(), "worker".into()], 4, true, false);
        assert_eq!(path[1], DrainKey::Wildcard);
    }

    #[test]
    fn routing_key_1e5_parametrized() {
        let path =
            DrainKey::route_path_from_tokens(&["1e5".into(), "worker".into()], 4, true, false);
        assert_eq!(path[1], DrainKey::Wildcard);
    }

    #[test]
    fn routing_key_nan_is_literal() {
        let path =
            DrainKey::route_path_from_tokens(&["NaN".into(), "worker".into()], 4, true, false);
        assert_eq!(path[1], DrainKey::Literal("NaN".to_string()));
    }

    #[test]
    fn routing_key_already_wildcard() {
        let path =
            DrainKey::route_path_from_tokens(&["<*>".into(), "worker".into()], 4, true, false);
        assert_eq!(path[1], DrainKey::Wildcard);
    }

    // ── Cluster size tracking ────────────────────────────────────────

    #[test]
    fn cluster_size_tracks_matches() {
        let mut parser = Drain3Parser::new(test_config()).unwrap();
        let entries = make_entries(&["worker started", "worker started", "worker started"]);
        let output = parser.parse_lines_for_training(&entries).unwrap();
        // Should have 1 event with 3 occurrences.
        assert_eq!(output.parse_output.events.len(), 1);
        assert_eq!(output.parse_output.events[0].occurrences, 3);
    }

    #[test]
    fn route_path_immutable_after_template_update() {
        let mut parser = Drain3Parser::new(test_config()).unwrap();
        let entries = make_entries(&["worker 1 started", "worker 2 started"]);
        let _ = parser.parse_lines_for_training(&entries).unwrap();

        // Get the cluster's route_path.
        let cluster = parser.state().cluster(0);
        let original_route = cluster.route_path.clone();

        // Parse more lines to trigger template generalization.
        let more_entries = make_entries(&["worker 3 started"]);
        let _ = parser.parse_lines_for_training(&more_entries).unwrap();

        // Route path must not have changed.
        let cluster = parser.state().cluster(0);
        assert_eq!(cluster.route_path, original_route);
    }

    // ── Online-style parsing with structured output ──────────────────

    #[test]
    fn online_parse_produces_structured_output_and_templates() {
        let mut parser = Drain3Parser::new(test_config()).unwrap();
        let entries = make_entries(&[
            "worker 1 started",
            "worker 2 started",
            "worker 3 started",
            "task completed",
        ]);
        let output = parser.parse_lines_for_training(&entries).unwrap();

        // Should have templates.
        assert!(!output.parse_output.events.is_empty());
        for event in &output.parse_output.events {
            assert!(!event.event_template.is_empty());
            assert!(!event.event_id.is_empty());
        }

        // All lines should have an event_id.
        for line in &output.parse_output.line_results {
            assert!(!line.event_id.is_empty());
        }
    }

    // ── max_line_bytes / max_token_bytes filtering ──────────────────

    #[test]
    fn max_line_bytes_excludes_overlong_lines_from_clustering() {
        let config = Drain3Config {
            max_line_bytes: Some(8),
            preprocess: false,
            ..test_config()
        };
        let mut parser = Drain3Parser::new(config).unwrap();
        // "abc" (3 bytes) ≤ 8 → clustered
        // "123456789" (9 bytes) > 8 → excluded, event_id = "0"
        let entries = make_entries(&["abc", "123456789"]);
        let output = parser.parse_lines_for_training(&entries).unwrap();
        assert_eq!(output.parse_output.cluster_count, 1);
        assert_eq!(output.parse_output.line_results.len(), 2);
        assert_ne!(output.parse_output.line_results[0].event_id, "0");
        assert_eq!(output.parse_output.line_results[1].event_id, "0");
    }

    #[test]
    fn max_token_bytes_filters_all_tokens_yields_zero() {
        let config = Drain3Config {
            max_token_bytes: Some(3),
            preprocess: false,
            ..test_config()
        };
        let mut parser = Drain3Parser::new(config).unwrap();
        // "abc" (3 bytes) ≤ 3 → kept, clustered
        // "toolong" (7 bytes) > 3 → filtered → empty → event_id "0"
        let entries = make_entries(&["abc", "toolong"]);
        let output = parser.parse_lines_for_training(&entries).unwrap();
        assert_eq!(output.parse_output.cluster_count, 1);
        assert_eq!(output.parse_output.line_results.len(), 2);
        assert_ne!(output.parse_output.line_results[0].event_id, "0");
        assert_eq!(output.parse_output.line_results[1].event_id, "0");
    }

    #[test]
    fn max_token_bytes_affects_clustering() {
        let config = Drain3Config {
            max_token_bytes: Some(3),
            preprocess: false,
            ..test_config()
        };
        let mut parser = Drain3Parser::new(config).unwrap();
        // Without filtering both lines have 2 tokens and can share a cluster.
        // With max_token_bytes=3:
        //   "abc xyz" → ["abc", "xyz"] (both ≤ 3, 2 tokens)
        //   "abc toolong" → ["abc"] ("toolong" filtered, 1 token)
        // Different token count → different clusters.
        let entries = make_entries(&["abc xyz", "abc toolong"]);
        let output = parser.parse_lines_for_training(&entries).unwrap();
        assert_eq!(output.parse_output.cluster_count, 2);
    }
}

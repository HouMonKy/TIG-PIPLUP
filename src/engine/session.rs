use crate::config::{PiplupConfig, SimThresholds};
use crate::engine::parser::InitialCacheState;
use crate::engine::util::{
    insert_template_caches, process_line, tokenize_line, try_light_cache, try_template_cache,
};
use crate::error::LogParseError;
use crate::format::MultiCompiledLogFormat;
use crate::model::ParseState;
use crate::parse_core::{ensure_unique_line_numbers, parse_entry};
use crate::preprocessor::{PreprocessPlan, Preprocessor};
use crate::types::{LogEntry, ParsedLine};

/// Number of lines used to estimate the preprocess plan before freezing it.
pub(crate) const PREPROCESS_PLAN_ESTIMATE_LIMIT: usize = 2000;

/// Streaming training session. Holds cross-batch continuous ParseState
/// and PreprocessPlan. Parsed lines are retained only when the builder enables
/// `retain_parsed(true)`.
///
/// Batches committed to a session are expected to be contiguous in the
/// original input: the first `line_number` of each batch must equal
/// `committed_lines + 1`. Line numbers count skipped/unmatched entries as
/// well, so gaps in the input stream surface as [`LogParseError::SessionGap`].
///
/// # Example
///
/// ```rust
/// use log_parser::{PiplupConfig, TrainingSession, LogEntry};
///
/// let mut session = TrainingSession::builder(PiplupConfig::default()).build().unwrap();
/// let entries = vec![
///     LogEntry { line_number: 1, raw: "2026-06-05 10:00:00 worker started".into() },
///     LogEntry { line_number: 2, raw: "2026-06-05 10:00:01 worker 1 done".into() },
/// ];
/// let info = session.commit_batch(&entries).unwrap();
/// assert_eq!(info.committed_lines, 2);
/// ```
pub struct TrainingSession {
    config: PiplupConfig,
    format: MultiCompiledLogFormat,
    preprocessor: Preprocessor,
    state: ParseState,
    preprocess_plan: PreprocessPlan,
    plan_frozen: bool,
    plan_restored_from_cache: bool,
    committed_lines: usize,
    retain_parsed: bool,
    retained_parsed_lines: Vec<ParsedLine>,
}

/// Builder for [`TrainingSession`].
///
/// # Example
///
/// ```rust
/// use log_parser::{PiplupConfig, TrainingSessionBuilder};
///
/// let builder = TrainingSessionBuilder::new(PiplupConfig::default());
/// ```
pub struct TrainingSessionBuilder {
    config: PiplupConfig,
    initial_state: Option<InitialCacheState>,
    retain_parsed: bool,
}

/// Return value of [`TrainingSession::finish`]. Contains ParseState and PreprocessPlan.
#[non_exhaustive]
#[derive(Debug)]
pub struct SessionState {
    /// Final learned parse state.
    pub state: ParseState,
    /// Final preprocess plan.
    pub preprocess_plan: PreprocessPlan,
}

/// Return value of [`TrainingSession::commit_batch`]. Reports batch progress.
#[non_exhaustive]
pub struct BatchCommitInfo {
    /// Total lines committed to the session so far.
    pub committed_lines: usize,
    /// Number of clusters discovered so far.
    pub cluster_count: usize,
    /// Current status of preprocess-plan estimation.
    pub plan_status: PlanStatus,
}

/// Current status of preprocess-plan estimation.
#[non_exhaustive]
pub enum PlanStatus {
    /// The plan is still being estimated from the first lines.
    Estimating { lines_seen: usize },
    /// The plan has been frozen and will not change.
    Frozen,
}

impl TrainingSession {
    /// Create a builder for a training session.
    pub fn builder(config: PiplupConfig) -> TrainingSessionBuilder {
        TrainingSessionBuilder {
            config,
            initial_state: None,
            retain_parsed: false,
        }
    }

    /// Commit a contiguous batch of log entries to the session.
    ///
    /// Returns batch metadata including the updated cluster count and plan status.
    pub fn commit_batch(&mut self, entries: &[LogEntry]) -> Result<BatchCommitInfo, LogParseError> {
        // 1. Gap check
        if let Some(first) = entries.first() {
            let expected = self.committed_lines + 1;
            if first.line_number != expected {
                return Err(LogParseError::SessionGap {
                    expected,
                    actual: first.line_number,
                });
            }
        }

        // 2. Uniqueness check
        ensure_unique_line_numbers(entries.iter().map(|e| e.line_number))?;

        // 3. Per-line PIPLUP processing
        let base_index = self.committed_lines;
        let thresholds = SimThresholds::from_mode(&self.config.sim_thresh);

        for (batch_idx, entry) in entries.iter().enumerate() {
            let global_index = base_index + batch_idx;

            // 3a. Format parsing
            let parsed = parse_entry(&self.format, self.config.skip_unmatched, entry)?;
            let Some(line) = parsed else { continue };
            if self.retain_parsed {
                self.retained_parsed_lines.push(line.clone());
            }

            // 3a. Repeated identical lines short-circuit the pipeline
            // through the line cache (tokenize + preprocess + search all
            // skipped after template verification).
            if try_light_cache(
                &mut self.state,
                &self.config,
                &line.content,
                line.line_number,
            )
            .is_some()
            {
                continue;
            }

            // 3b. Tokenization (with PreprocessPlan estimation/application)
            let (tokens, line_kinds, length_filtered) = tokenize_line(
                &self.config,
                &self.preprocessor,
                &mut self.preprocess_plan,
                global_index,
                &line,
                self.plan_frozen,
                self.plan_restored_from_cache,
            );

            if !length_filtered
                && try_template_cache(
                    &mut self.state,
                    &self.config,
                    &line.content,
                    &tokens,
                    line.line_number,
                )
                .is_some()
            {
                continue;
            }

            // 3c. PIPLUP four stages. Length-filtered lines (max_line_bytes,
            // or max_token_bytes filtering every token) are excluded from
            // clustering; naturally empty content keeps the legacy behavior.
            if !length_filtered {
                let cluster_id = process_line(
                    &mut self.state,
                    &tokens,
                    &line_kinds,
                    &thresholds,
                    &self.config,
                    line.line_number,
                );
                insert_template_caches(
                    &mut self.state,
                    &self.config,
                    &line.content,
                    &tokens,
                    cluster_id,
                );
            }
        }

        self.committed_lines += entries.len();

        // 4. Return BatchCommitInfo
        Ok(BatchCommitInfo {
            committed_lines: self.committed_lines,
            cluster_count: self.state.clusters.len(),
            plan_status: if self.plan_frozen {
                PlanStatus::Frozen
            } else {
                PlanStatus::Estimating {
                    lines_seen: self.committed_lines,
                }
            },
        })
    }

    /// Freeze the preprocess plan so it is no longer estimated from new lines.
    pub fn freeze_plan(&mut self) {
        self.plan_frozen = true;
    }

    /// Return a reference to the current preprocess plan.
    pub fn preprocess_plan(&self) -> &PreprocessPlan {
        &self.preprocess_plan
    }

    /// Return the number of lines committed so far.
    pub fn committed_lines(&self) -> usize {
        self.committed_lines
    }

    /// Consume the session and return the final ParseState and PreprocessPlan.
    ///
    /// This method never fails. The `Result` return type was removed to avoid
    /// forcing callers to handle an impossible error path.
    pub fn finish(self) -> SessionState {
        SessionState {
            state: self.state,
            preprocess_plan: self.preprocess_plan,
        }
    }

    /// Consume the session and return the final state along with retained parsed lines.
    ///
    /// Requires `retain_parsed(true)` to be set on the builder; otherwise returns
    /// [`LogParseError::InvalidAlgorithmConfig`].
    pub fn try_finish_with_parsed(self) -> Result<(SessionState, Vec<ParsedLine>), LogParseError> {
        if !self.retain_parsed {
            return Err(LogParseError::InvalidAlgorithmConfig {
                algorithm: "TrainingSession",
                field: "retain_parsed",
                message: "try_finish_with_parsed requires retain_parsed(true)".to_owned(),
            });
        }
        let state = SessionState {
            state: self.state,
            preprocess_plan: self.preprocess_plan,
        };
        Ok((state, self.retained_parsed_lines))
    }
}

impl TrainingSessionBuilder {
    /// Create a new builder.
    pub fn new(config: PiplupConfig) -> Self {
        Self {
            config,
            initial_state: None,
            retain_parsed: false,
        }
    }

    /// Warm-start the session from a cache-restored state.
    pub fn with_cache(mut self, initial_state: InitialCacheState) -> Self {
        self.initial_state = Some(initial_state);
        self
    }

    /// Build the [`TrainingSession`].
    pub fn build(self) -> Result<TrainingSession, LogParseError> {
        let format = MultiCompiledLogFormat::compile(&self.config.log_format)?;
        let preprocessor = Preprocessor::default();

        let (state, preprocess_plan, committed_lines, plan_restored_from_cache) =
            if let Some(initial) = self.initial_state {
                let plan_restored = !initial.preprocess_plan.enabled_rule_ids.is_empty();
                (
                    initial.state,
                    initial.preprocess_plan,
                    initial.processed_line_count,
                    plan_restored,
                )
            } else {
                (ParseState::new(), PreprocessPlan::default(), 0, false)
            };

        Ok(TrainingSession {
            config: self.config,
            format,
            preprocessor,
            state,
            preprocess_plan,
            plan_frozen: false,
            plan_restored_from_cache,
            committed_lines,
            retain_parsed: self.retain_parsed,
            retained_parsed_lines: Vec::new(),
        })
    }

    /// Retain parsed lines so they can be retrieved via
    /// [`TrainingSession::try_finish_with_parsed`].
    pub fn retain_parsed(mut self, retain: bool) -> Self {
        self.retain_parsed = retain;
        self
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn entry(line_number: usize, content: &str) -> LogEntry {
        LogEntry {
            line_number,
            raw: format!("2026-04-11 12:00:{:02} {content}", line_number % 60),
        }
    }

    #[test]
    fn retain_parsed_true_returns_session_state_and_parsed_lines() {
        let entries = [
            entry(1, "INFO worker alpha ready"),
            entry(2, "INFO worker beta ready"),
            entry(3, "WARN disk sda full"),
        ];
        let mut session = TrainingSession::builder(PiplupConfig::default())
            .retain_parsed(true)
            .build()
            .expect("session should build");

        session
            .commit_batch(&entries[..2])
            .expect("first batch should commit");
        session
            .commit_batch(&entries[2..])
            .expect("second batch should commit");

        let (state, parsed_lines) = session
            .try_finish_with_parsed()
            .expect("retained parsed lines should be returned");

        assert!(!state.state.clusters.is_empty());
        assert_eq!(
            parsed_lines
                .iter()
                .map(|line| line.line_number)
                .collect::<Vec<_>>(),
            vec![1, 2, 3]
        );
        assert_eq!(parsed_lines[0].content, "INFO worker alpha ready");
    }

    #[test]
    fn retain_parsed_false_rejects_finish_with_parsed() {
        let entries = [entry(1, "INFO worker alpha ready")];
        let mut session = TrainingSession::builder(PiplupConfig::default())
            .build()
            .expect("session should build");

        session.commit_batch(&entries).expect("batch should commit");

        let error = session
            .try_finish_with_parsed()
            .expect_err("retain_parsed(false) should reject parsed finish");
        assert!(matches!(
            error,
            LogParseError::InvalidAlgorithmConfig {
                algorithm: "TrainingSession",
                field: "retain_parsed",
                ..
            }
        ));
    }
}

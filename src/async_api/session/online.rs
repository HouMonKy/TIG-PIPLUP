//! PIPLUP parse-output session entry points (reader, file, plan).
//!
//! Each wrapper constructs a source task factory and delegates to the shared
//! [`run_piplup_session`](super::common::run_piplup_session) pipeline, returning
//! a [`ParseOutput`].

use std::path::PathBuf;

use tokio::io::AsyncBufRead;

use super::super::source_task;
use super::super::types::{
    LineNumbering, SchedulerConfig, SessionPlan, SourceContext, SourceName, SourcePlan,
    SourceTaskEvent,
};
use super::common::{build_parse_output, run_piplup_session};
use crate::config::PiplupConfig;
use crate::error::LogParseError;
use crate::types::ParseOutput;

/// Run a PIPLUP learning session from a single `AsyncBufRead` source.
///
/// This is the primary entry point for async PIPLUP parsing from a reader.
/// It reads all lines in batches and streams them through a [`TrainingSession`].
///
/// # Arguments
///
/// * `config` — PIPLUP parser configuration.
/// * `scheduler_config` — Scheduler and channel configuration.
/// * `reader` — The async buffered reader to read from.
///
/// # Errors
///
/// Returns `LogParseError` if the reader fails, the parser fails, or the
/// scheduler configuration is invalid.
///
/// # Examples
///
/// ```no_run
/// use log_parser::async_api::session::run_piplup_reader_session;
/// use log_parser::async_api::types::SchedulerConfig;
/// use log_parser::PiplupConfig;
///
/// # #[tokio::main]
/// # async fn main() -> Result<(), log_parser::LogParseError> {
/// let reader = tokio::io::BufReader::new(tokio::fs::File::open("app.log").await?);
/// let config = PiplupConfig::default();
/// let output = run_piplup_reader_session(config, SchedulerConfig::default(), reader).await?;
/// println!("parsed {} lines into {} templates", output.line_results.len(), output.cluster_count);
/// # Ok(())
/// # }
/// ```
pub async fn run_piplup_reader_session<S>(
    config: PiplupConfig,
    scheduler_config: SchedulerConfig,
    reader: S,
) -> Result<ParseOutput, LogParseError>
where
    S: AsyncBufRead + Unpin + Send + 'static,
{
    let batch_size = scheduler_config.batch_size;
    let context = SourceContext {
        source_seq: 0,
        source_name: SourceName::new("<reader>"),
    };

    run_piplup_session(
        config,
        scheduler_config,
        LineNumbering::PreserveSourceLocal,
        1,
        |source_tx| {
            tokio::spawn(async move {
                source_task::read_lines_to_batches(reader, 0, context, batch_size, source_tx).await;
            })
        },
        build_parse_output,
    )
    .await
}

/// Run a PIPLUP learning session from a single file.
///
/// Opens the file asynchronously, reads all lines in batches, and streams them
/// through a [`TrainingSession`].
///
/// # Errors
///
/// Returns `LogParseError` if the file cannot be opened, reading fails, the
/// parser fails, or the scheduler configuration is invalid.
///
/// # Examples
///
/// ```no_run
/// use std::path::PathBuf;
/// use log_parser::async_api::session::run_piplup_file_session;
/// use log_parser::async_api::types::SchedulerConfig;
/// use log_parser::PiplupConfig;
///
/// # #[tokio::main]
/// # async fn main() -> Result<(), log_parser::LogParseError> {
/// let config = PiplupConfig::default();
/// let output = run_piplup_file_session(config, SchedulerConfig::default(), PathBuf::from("app.log")).await?;
/// println!("parsed {} lines", output.line_results.len());
/// # Ok(())
/// # }
/// ```
pub async fn run_piplup_file_session(
    config: PiplupConfig,
    scheduler_config: SchedulerConfig,
    path: PathBuf,
) -> Result<ParseOutput, LogParseError> {
    let batch_size = scheduler_config.batch_size;
    let context = SourceContext {
        source_seq: 0,
        source_name: SourceName::new(path.display().to_string()),
    };

    run_piplup_session(
        config,
        scheduler_config,
        LineNumbering::PreserveSourceLocal,
        1,
        |source_tx| {
            tokio::spawn(async move {
                source_task::read_file_to_batches(path, 0, context, batch_size, source_tx).await;
            })
        },
        build_parse_output,
    )
    .await
}

/// Run a PIPLUP learning session from a [`SessionPlan`].
///
/// Processes all sources in the plan in order. For multi-file plans, files
/// are read sequentially and line numbering is applied according to the
/// plan's [`LineNumbering`] policy.
///
/// # Errors
///
/// Returns `LogParseError` if any file cannot be opened, reading fails, the
/// parser fails, or the scheduler configuration is invalid.
pub async fn run_piplup_plan_session(
    config: PiplupConfig,
    scheduler_config: SchedulerConfig,
    plan: SessionPlan,
) -> Result<ParseOutput, LogParseError> {
    let batch_size = scheduler_config.batch_size;
    let source_count = plan.sources.len();
    let line_numbering = plan.line_numbering;

    run_piplup_session(
        config,
        scheduler_config,
        line_numbering,
        source_count,
        |source_tx| {
            tokio::spawn(async move {
                let tx = source_tx;
                for (seq, source) in plan.sources.into_iter().enumerate() {
                    let path = match source {
                        SourcePlan::File { path } => path,
                        SourcePlan::Reader { .. } => {
                            let context = SourceContext {
                                source_seq: seq,
                                source_name: SourceName::new("<reader>"),
                            };
                            let _ = tx
                                .send(SourceTaskEvent::Failed {
                                    source_seq: seq,
                                    local_batch_seq: 0,
                                    context,
                                    error: LogParseError::InvalidAlgorithmConfig {
                                        algorithm: "async",
                                        field: "source_plan",
                                        message: "Reader sources are not supported in plan-based sessions".to_owned(),
                                    },
                                })
                                .await;
                            return;
                        }
                    };
                    let context = SourceContext {
                        source_seq: seq,
                        source_name: SourceName::new(path.display().to_string()),
                    };
                    source_task::read_file_to_batches(path, seq, context, batch_size, tx.clone())
                        .await;
                }
            })
        },
        build_parse_output,
    )
    .await
}

//! PIPLUP training-output session entry points (reader, file, plan).
//!
//! Each wrapper constructs a source task factory and delegates to the shared
//! [`run_piplup_session`](super::common::run_piplup_session) pipeline, returning
//! a [`TrainingOutput`](crate::types::TrainingOutput).

use std::path::PathBuf;

use tokio::io::AsyncBufRead;

use super::super::source_task;
use super::super::types::{
    LineNumbering, SchedulerConfig, SessionPlan, SourceContext, SourceName, SourcePlan,
    SourceTaskEvent,
};
use super::common::{build_training_output, run_piplup_session};
use crate::config::PiplupConfig;
use crate::error::LogParseError;

/// Run a PIPLUP learning session from a single `AsyncBufRead` source,
/// returning a `TrainingOutput` (includes the preprocess plan).
///
/// Similar to [`run_piplup_reader_session`] but returns a [`TrainingOutput`]
/// that includes the preprocess plan.
///
/// # Errors
///
/// Returns `LogParseError` if the reader fails, the parser fails, or the
/// scheduler configuration is invalid.
pub async fn run_piplup_reader_training_session<S>(
    config: PiplupConfig,
    scheduler_config: SchedulerConfig,
    reader: S,
) -> Result<crate::types::TrainingOutput, LogParseError>
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
        build_training_output,
    )
    .await
}

/// Run a PIPLUP training session from a single file.
///
/// Like [`run_piplup_file_session`] but returns a `TrainingOutput` (includes
/// the preprocess plan).
///
/// # Errors
///
/// Returns `LogParseError` if the file cannot be opened, reading fails, the
/// parser fails, or the scheduler configuration is invalid.
pub async fn run_piplup_file_training_session(
    config: PiplupConfig,
    scheduler_config: SchedulerConfig,
    path: PathBuf,
) -> Result<crate::types::TrainingOutput, LogParseError> {
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
        build_training_output,
    )
    .await
}

/// Run a PIPLUP training session from a [`SessionPlan`].
///
/// Like [`run_piplup_plan_session`] but returns a `TrainingOutput` (includes
/// the preprocess plan).
///
/// # Errors
///
/// Returns `LogParseError` if any file cannot be opened, reading fails, the
/// parser fails, or the scheduler configuration is invalid.
pub async fn run_piplup_plan_training_session(
    config: PiplupConfig,
    scheduler_config: SchedulerConfig,
    plan: SessionPlan,
) -> Result<crate::types::TrainingOutput, LogParseError> {
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
        build_training_output,
    )
    .await
}

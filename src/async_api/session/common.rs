//! Shared PIPLUP learning-session pipeline and output helpers.
//!
//! These helpers are shared by the online and training session families
//! (siblings under `session`) and are therefore `pub(super)`.

use std::time::Duration;

use tokio::sync::mpsc;
use tokio::task::JoinHandle;

use super::super::batch_stream::BatchStream;
use super::super::types::{LineNumbering, SchedulerConfig, SourceTaskEvent};
use crate::config::PiplupConfig;
use crate::engine::session::{BatchCommitInfo, SessionState, TrainingSession};
use crate::error::LogParseError;
use crate::types::{ParseOutput, ParsedLine};

/// Build a [`ParseOutput`] from a completed learning session.
pub(super) fn build_parse_output(
    parsed_lines: Vec<ParsedLine>,
    session_state: SessionState,
    keep_para: bool,
    warn_miss: bool,
    duration: Duration,
) -> Result<ParseOutput, LogParseError> {
    crate::engine::output::build_output(
        parsed_lines,
        session_state.state,
        keep_para,
        warn_miss,
        duration,
    )
}

/// Build a [`TrainingOutput`] from a completed learning session.
pub(super) fn build_training_output(
    parsed_lines: Vec<ParsedLine>,
    session_state: SessionState,
    keep_para: bool,
    warn_miss: bool,
    duration: Duration,
) -> Result<crate::types::TrainingOutput, LogParseError> {
    crate::engine::output::build_training_output(
        parsed_lines,
        session_state,
        keep_para,
        warn_miss,
        duration,
    )
}

/// Shared PIPLUP learning-session pipeline.
///
/// Drives a [`BatchStream`] created by `produce`, commits each batch through a
/// [`TrainingSession`] inside `spawn_blocking`, and converts the final state
/// with `build_output`.
pub(super) async fn run_piplup_session<O, SourceFactory, BuildOutput>(
    config: PiplupConfig,
    scheduler_config: SchedulerConfig,
    line_numbering: LineNumbering,
    source_count: usize,
    produce: SourceFactory,
    build_output: BuildOutput,
) -> Result<O, LogParseError>
where
    SourceFactory: FnOnce(mpsc::Sender<SourceTaskEvent>) -> JoinHandle<()>,
    BuildOutput:
        FnOnce(Vec<ParsedLine>, SessionState, bool, bool, Duration) -> Result<O, LogParseError>,
{
    let keep_para = config.keep_para;
    let warn_miss = config.warn_miss;

    let mut stream =
        BatchStream::new(scheduler_config, line_numbering, source_count, produce).await?;

    let started = std::time::Instant::now();
    let mut session = TrainingSession::builder(config)
        .retain_parsed(true)
        .build()?;

    while let Some(batch_result) = stream.next_batch().await {
        let entries = batch_result?;
        let (session_out, _info): (TrainingSession, BatchCommitInfo) = tokio::task::spawn_blocking(
            move || -> Result<(TrainingSession, BatchCommitInfo), LogParseError> {
                let info = session.commit_batch(&entries)?;
                Ok((session, info))
            },
        )
        .await
        .map_err(|e| LogParseError::Io(std::io::Error::other(e.to_string())))??;
        session = session_out;
    }

    let (session_state, parsed_lines): (SessionState, Vec<ParsedLine>) =
        tokio::task::spawn_blocking(
            move || -> Result<(SessionState, Vec<ParsedLine>), LogParseError> {
                session.try_finish_with_parsed()
            },
        )
        .await
        .map_err(|e| LogParseError::Io(std::io::Error::other(e.to_string())))??;

    build_output(
        parsed_lines,
        session_state,
        keep_para,
        warn_miss,
        started.elapsed(),
    )
}

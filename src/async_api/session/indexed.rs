//! Indexed parse session entry points (reader, file, plan, parallel reader).
//!
//! Single-worker and multi-worker indexed parse pipelines. The immutable
//! [`IndexedParser`] is shared across `spawn_blocking` workers via `Arc`.

use std::collections::VecDeque;
use std::path::PathBuf;

use tokio::io::AsyncBufRead;
use tokio::sync::mpsc;
use tokio::task::JoinHandle;

use super::super::batch_stream::BatchStream;
use super::super::source_task;
use super::super::types::{
    IndexedWorkerConfig, LineNumbering, SchedulerConfig, SessionPlan, SourceContext, SourceName,
    SourcePlan, SourceTaskEvent,
};
use crate::error::LogParseError;
use crate::index::{IndexedBatchResult, IndexedParseOutput, IndexedParser};
use crate::types::LogEntry;

/// Shared indexed parse pipeline for single-worker sessions.
///
/// Drives a [`BatchStream`], parses each ordered batch inside `spawn_blocking`,
/// and merges the results with an
/// [`IndexedResultAccumulator`](crate::IndexedResultAccumulator).
async fn run_indexed_session<SourceFactory>(
    parser: IndexedParser,
    scheduler_config: SchedulerConfig,
    line_numbering: LineNumbering,
    source_count: usize,
    produce: SourceFactory,
) -> Result<IndexedParseOutput, LogParseError>
where
    SourceFactory: FnOnce(mpsc::Sender<SourceTaskEvent>) -> JoinHandle<()>,
{
    let parser = std::sync::Arc::new(parser);
    let started = std::time::Instant::now();
    let mut accumulator = parser.accumulator();
    let mut stream =
        BatchStream::new(scheduler_config, line_numbering, source_count, produce).await?;

    while let Some(batch_result) = stream.next_batch().await {
        let entries = batch_result?;
        let parser_clone = std::sync::Arc::clone(&parser);
        let result = tokio::task::spawn_blocking(move || parser_clone.parse_batch(&entries))
            .await
            .map_err(|e| LogParseError::Io(std::io::Error::other(e.to_string())))??;
        accumulator.try_merge(result)?;
    }

    Ok(accumulator.finish_with_duration(started.elapsed()))
}

/// Run an indexed parse session from a single `AsyncBufRead` source.
///
/// Reads ordered batches and calls [`IndexedParser::parse_batch`] for each
/// batch inside `spawn_blocking`.
///
/// # Arguments
///
/// * `parser` — A pre-constructed [`IndexedParser`]. The session wraps it in
///   an internal `Arc` so each batch can be parsed on a blocking worker.
/// * `scheduler_config` — Scheduler and channel configuration.
/// * `reader` — The async buffered reader to read from.
///
/// # Errors
///
/// Returns `LogParseError` if the reader fails, the parser fails, or the
/// scheduler configuration is invalid.
pub async fn run_indexed_reader_session<S>(
    parser: IndexedParser,
    scheduler_config: SchedulerConfig,
    reader: S,
) -> Result<IndexedParseOutput, LogParseError>
where
    S: AsyncBufRead + Unpin + Send + 'static,
{
    let batch_size = scheduler_config.batch_size;
    let context = SourceContext {
        source_seq: 0,
        source_name: SourceName::new("<reader>"),
    };

    run_indexed_session(
        parser,
        scheduler_config,
        LineNumbering::PreserveSourceLocal,
        1,
        |source_tx| {
            tokio::spawn(async move {
                source_task::read_lines_to_batches(reader, 0, context, batch_size, source_tx).await;
            })
        },
    )
    .await
}

/// Run an indexed parse session from a single file.
///
/// Opens the file asynchronously, reads ordered batches, and calls
/// [`IndexedParser::parse_batch`] for each batch inside `spawn_blocking`.
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
/// use log_parser::async_api::session::run_indexed_file_session;
/// use log_parser::async_api::types::SchedulerConfig;
/// use log_parser::IndexedParser;
///
/// # #[tokio::main]
/// # async fn main() -> Result<(), log_parser::LogParseError> {
/// let index_bytes = std::fs::read("templates.idx")?;
/// let parser = IndexedParser::from_bytes(&index_bytes, Default::default())?;
/// let output = run_indexed_file_session(parser, SchedulerConfig::default(), PathBuf::from("app.log")).await?;
/// println!("{} misses", output.stats.miss_count);
/// # Ok(())
/// # }
/// ```
pub async fn run_indexed_file_session(
    parser: IndexedParser,
    scheduler_config: SchedulerConfig,
    path: PathBuf,
) -> Result<IndexedParseOutput, LogParseError> {
    let batch_size = scheduler_config.batch_size;
    let context = SourceContext {
        source_seq: 0,
        source_name: SourceName::new(path.display().to_string()),
    };

    run_indexed_session(
        parser,
        scheduler_config,
        LineNumbering::PreserveSourceLocal,
        1,
        |source_tx| {
            tokio::spawn(async move {
                source_task::read_file_to_batches(path, 0, context, batch_size, source_tx).await;
            })
        },
    )
    .await
}

/// Run an indexed parse session from a [`SessionPlan`].
///
/// Processes all sources in the plan in order. For multi-file plans, files
/// are read sequentially and line numbering is applied according to the
/// plan's [`LineNumbering`] policy.
///
/// **Limitation**: Only [`SourcePlan::File`] sources are supported. Reader
/// sources in the plan will produce a [`LogParseError::InvalidAlgorithmConfig`]
/// error. Use [`run_indexed_reader_session`] for reader-based input.
///
/// # Errors
///
/// Returns `LogParseError` if any file cannot be opened, reading fails, the
/// parser fails, or the scheduler configuration is invalid.
pub async fn run_indexed_plan_session(
    parser: IndexedParser,
    scheduler_config: SchedulerConfig,
    plan: SessionPlan,
) -> Result<IndexedParseOutput, LogParseError> {
    let batch_size = scheduler_config.batch_size;
    let source_count = plan.sources.len();
    let line_numbering = plan.line_numbering;

    run_indexed_session(
        parser,
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
    )
    .await
}

/// Run a multi-worker indexed parse session from a single `AsyncBufRead`
/// source.
///
/// Streams ordered batches, fills worker-sized chunks, and processes chunks in
/// parallel using `spawn_blocking` workers.
/// Results are merged in chunk order to preserve line ordering.
///
/// # Arguments
///
/// * `parser` — A pre-constructed [`IndexedParser`] wrapped in `Arc` for
///   sharing across workers. The parser is immutable, so no locking is needed.
/// * `scheduler_config` — Scheduler and channel configuration for line
///   reading.
/// * `worker_config` — Worker pool configuration (chunk size, worker count).
/// * `reader` — The async buffered reader to read from.
///
/// # Errors
///
/// Returns `LogParseError` if the reader fails, any chunk's parser call fails,
/// or the configuration is invalid.
pub async fn run_indexed_reader_session_parallel<S>(
    parser: std::sync::Arc<IndexedParser>,
    scheduler_config: SchedulerConfig,
    worker_config: IndexedWorkerConfig,
    reader: S,
) -> Result<IndexedParseOutput, LogParseError>
where
    S: AsyncBufRead + Unpin + Send + 'static,
{
    worker_config.validate()?;

    let batch_size = scheduler_config.batch_size;
    let context = SourceContext {
        source_seq: 0,
        source_name: SourceName::new("<reader>"),
    };

    if !worker_config.chunk_size.is_multiple_of(batch_size) {
        eprintln!(
            "[warning] worker_config.chunk_size ({}) is not a multiple of scheduler_config.batch_size ({}); \
             this can cause extra buffering and latency",
            worker_config.chunk_size, batch_size
        );
    }

    let started = std::time::Instant::now();
    let mut accumulator = parser.accumulator();
    let semaphore = std::sync::Arc::new(tokio::sync::Semaphore::new(worker_config.worker_count));
    let mut chunk_buffer: Vec<LogEntry> = Vec::with_capacity(worker_config.chunk_size);
    let max_pending = worker_config.worker_count * 2;
    let mut pending: VecDeque<JoinHandle<Result<IndexedBatchResult, LogParseError>>> =
        VecDeque::new();
    let mut stream = BatchStream::new(
        scheduler_config,
        LineNumbering::PreserveSourceLocal,
        1,
        |source_tx| {
            tokio::spawn(async move {
                source_task::read_lines_to_batches(reader, 0, context, batch_size, source_tx).await;
            })
        },
    )
    .await?;

    while let Some(batch_result) = stream.next_batch().await {
        let entries = batch_result?;
        chunk_buffer.extend(entries);

        while chunk_buffer.len() >= worker_config.chunk_size {
            let permit = semaphore
                .clone()
                .acquire_owned()
                .await
                .map_err(|e| LogParseError::Io(std::io::Error::other(e.to_string())))?;
            let chunk: Vec<LogEntry> = chunk_buffer.drain(..worker_config.chunk_size).collect();
            let parser_clone = std::sync::Arc::clone(&parser);
            pending.push_back(tokio::task::spawn_blocking(move || {
                let _permit = permit;
                parser_clone.parse_batch(&chunk)
            }));

            while pending.len() >= max_pending {
                let handle = pending.pop_front().unwrap();
                let result = handle
                    .await
                    .map_err(|e| LogParseError::Io(std::io::Error::other(e.to_string())))??;
                accumulator.try_merge(result)?;
            }
        }
    }

    if !chunk_buffer.is_empty() {
        let permit = semaphore
            .clone()
            .acquire_owned()
            .await
            .map_err(|e| LogParseError::Io(std::io::Error::other(e.to_string())))?;
        let parser_clone = std::sync::Arc::clone(&parser);
        pending.push_back(tokio::task::spawn_blocking(move || {
            let _permit = permit;
            parser_clone.parse_batch(&chunk_buffer)
        }));
    }

    for handle in pending {
        let result = handle
            .await
            .map_err(|e| LogParseError::Io(std::io::Error::other(e.to_string())))??;
        accumulator.try_merge(result)?;
    }

    Ok(accumulator.finish_with_duration(started.elapsed()))
}

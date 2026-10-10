//! Core types for the async processing pipeline.
//!
//! These types define the data flowing between pipeline layers. Each type is
//! scoped to a specific layer boundary and does not leak implementation details
//! across boundaries.

use std::path::PathBuf;

use crate::error::LogParseError;
use crate::types::LogEntry;

// ---------------------------------------------------------------------------
// Configuration validation
// ---------------------------------------------------------------------------

/// Identifies a logical parser stream for output paths, cache identity,
/// progress messages, and deterministic group ordering.
///
/// `SessionKey` is not a data source. It identifies the session that consumes
/// the data. Examples are stdin/reader, a single file, a leaf directory, or an
/// indexed parse target.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct SessionKey {
    /// Human-readable label for diagnostics and progress messages.
    pub label: String,
    /// Optional path used for cache identity and output directory.
    pub path: Option<PathBuf>,
}

impl SessionKey {
    /// Create a key for a reader-backed session (e.g., stdin).
    pub fn reader(label: impl Into<String>) -> Self {
        SessionKey {
            label: label.into(),
            path: None,
        }
    }

    /// Create a key for a file-backed session.
    pub fn file(path: PathBuf) -> Self {
        let label = path.display().to_string();
        SessionKey {
            label,
            path: Some(path),
        }
    }

    /// Create a key for a directory-backed session.
    pub fn directory(path: PathBuf) -> Self {
        let label = format!("{} (directory)", path.display());
        SessionKey {
            label,
            path: Some(path),
        }
    }
}

impl std::fmt::Display for SessionKey {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.label)
    }
}

/// A human-readable name for a reader source, used in diagnostics.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct SourceName(pub String);

impl SourceName {
    pub fn new(name: impl Into<String>) -> Self {
        SourceName(name.into())
    }
}

impl std::fmt::Display for SourceName {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

/// Planning metadata for a session's sources.
///
/// A source is any input producer that can emit raw log lines in a stable local
/// order.
#[derive(Debug, Clone)]
pub struct SessionPlan {
    pub key: SessionKey,
    pub sources: Vec<SourcePlan>,
    pub line_numbering: LineNumbering,
}

impl SessionPlan {
    /// Create a plan for a single-file session.
    ///
    /// Uses `PreserveSourceLocal` line numbering since there is only one source.
    pub fn for_file(path: PathBuf) -> Self {
        let key = SessionKey::file(path.clone());
        SessionPlan {
            key,
            sources: vec![SourcePlan::File { path }],
            line_numbering: LineNumbering::PreserveSourceLocal,
        }
    }

    /// Create plans for a training directory, grouped by leaf directory.
    ///
    /// Scans for `.log` files under `root`, sorts them, groups by leaf
    /// directory, and creates one `SessionPlan` per leaf group with
    /// `ConcatenateWithinSession` line numbering.
    ///
    /// # Errors
    ///
    /// Returns an I/O error if directory scanning fails.
    /// Returns an error if no `.log` files are found.
    pub fn training_plans_for_directory(root: &std::path::Path) -> std::io::Result<Vec<Self>> {
        let mut files = crate::input_plan::collect_log_files(root)?;
        if files.is_empty() {
            return Err(std::io::Error::new(
                std::io::ErrorKind::NotFound,
                format!("no .log files found under directory {}", root.display()),
            ));
        }
        files.sort();

        let grouped = crate::input_plan::group_files_by_leaf(&files);
        let plans = grouped
            .into_iter()
            .map(|(leaf_dir, leaf_files)| {
                let key = SessionKey::directory(leaf_dir.clone());
                let sources = leaf_files
                    .into_iter()
                    .map(|p| SourcePlan::File { path: p })
                    .collect();
                SessionPlan {
                    key,
                    sources,
                    line_numbering: LineNumbering::ConcatenateWithinSession,
                }
            })
            .collect();
        Ok(plans)
    }

    /// Create plans for an online directory, one session per file.
    ///
    /// Each file is an independent session with `PreserveSourceLocal`
    /// line numbering.
    ///
    /// # Errors
    ///
    /// Returns an I/O error if directory scanning fails.
    /// Returns an error if no `.log` files are found.
    pub fn online_plans_for_directory(root: &std::path::Path) -> std::io::Result<Vec<Self>> {
        let mut files = crate::input_plan::collect_log_files(root)?;
        if files.is_empty() {
            return Err(std::io::Error::new(
                std::io::ErrorKind::NotFound,
                format!("no .log files found under directory {}", root.display()),
            ));
        }
        files.sort();

        Ok(files
            .into_iter()
            .map(|path| {
                let key = SessionKey::file(path.clone());
                SessionPlan {
                    key,
                    sources: vec![SourcePlan::File { path }],
                    line_numbering: LineNumbering::PreserveSourceLocal,
                }
            })
            .collect())
    }
}

/// Planning metadata for a single source within a session.
#[derive(Debug, Clone)]
pub enum SourcePlan {
    File { path: PathBuf },
    Reader { name: SourceName },
}

/// Line numbering policy for a session.
///
/// Controls how `LogEntry::line_number` values are assigned after ordered
/// assembly.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LineNumbering {
    /// Keep source-local line numbers unchanged.
    ///
    /// Used for single-file and single-reader sessions where each source
    /// already produces unique line numbers.
    PreserveSourceLocal,

    /// Rewrite line numbers so all committed batches in the session form one
    /// contiguous logical stream starting from 1.
    ///
    /// Used for `train` directory sessions where sorted files in one leaf
    /// directory are parsed as one concatenated stream.
    ConcatenateWithinSession,
}

/// Context carried with source events for diagnostics and error reporting.
#[derive(Debug, Clone)]
pub struct SourceContext {
    /// The source sequence number assigned by the session planner.
    pub source_seq: usize,
    /// The name of the source for diagnostics.
    pub source_name: SourceName,
}

// ---------------------------------------------------------------------------
// Source-task layer types
// ---------------------------------------------------------------------------

/// A batch of log entries emitted by a source task, scoped to one source.
#[derive(Debug)]
pub struct SourceBatch {
    /// Sequence number assigned by the session planner (0-based).
    pub source_seq: usize,
    /// Batch sequence within this source (0-based, contiguous).
    pub local_batch_seq: usize,
    /// Log entries in this batch. `line_number` values are source-local.
    pub entries: Vec<LogEntry>,
    /// Diagnostic context for this source.
    pub context: SourceContext,
}

/// Events emitted by a source task.
///
/// Source tasks emit these events to the source scheduler, not directly to the
/// ordered assembler.
#[derive(Debug)]
pub enum SourceTaskEvent {
    /// A batch of log entries from this source.
    Batch(SourceBatch),
    /// This source has finished producing events.
    Finished { source_seq: usize },
    /// This source encountered a read error.
    Failed {
        source_seq: usize,
        local_batch_seq: usize,
        context: SourceContext,
        error: LogParseError,
    },
}

// ---------------------------------------------------------------------------
// Transport layer types
// ---------------------------------------------------------------------------

/// Opaque, contiguous batch ordering value assigned by the source scheduler.
///
/// `BatchOrder` is not a file number and does not expose source boundaries.
/// The ordered assembler may compare `BatchOrder` values and advance to the
/// next contiguous value, but it does not know which source produced a batch.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub struct BatchOrder(pub usize);

impl BatchOrder {
    /// Returns the next contiguous batch order.
    pub fn next(self) -> BatchOrder {
        BatchOrder(self.0 + 1)
    }
}

/// A batch of log entries with scheduler-assigned ordering.
#[derive(Debug)]
pub struct EntryBatch {
    /// The scheduler-assigned order for this batch.
    pub order: BatchOrder,
    /// Log entries in this batch.
    pub entries: Vec<LogEntry>,
    /// Diagnostic context from the source.
    pub context: SourceContext,
}

/// Events sent from the source scheduler to the ordered assembler through a
/// bounded channel.
#[derive(Debug)]
pub enum TransportEvent {
    /// An ordered batch of log entries.
    Batch(EntryBatch),
    /// All input has been consumed. `next_order` is the exclusive end of the
    /// contiguous transport order.
    EndOfInput { next_order: BatchOrder },
    /// A source failed. The session is aborted.
    Failed {
        context: SourceContext,
        error: LogParseError,
    },
}

/// Configuration for the source scheduler.
#[derive(Debug, Clone)]
pub struct SchedulerConfig {
    /// Maximum number of source tasks that may run concurrently within one
    /// session.
    ///
    /// Defaults to 1 for learning sessions (ordered source activation).
    pub max_concurrent_sources_per_session: usize,

    /// Hard limit on batches that have been read from later sources but
    /// cannot yet receive final `BatchOrder` values.
    ///
    /// Only relevant when `max_concurrent_sources_per_session > 1`.
    pub max_scheduler_prefetch_batches: usize,

    /// Capacity for the bounded channel between source tasks and the
    /// scheduler.
    pub source_channel_capacity: usize,

    /// Capacity for the bounded channel between the scheduler and the
    /// assembler.
    pub transport_channel_capacity: usize,

    /// Number of lines per source-local batch.
    pub batch_size: usize,
}

impl Default for SchedulerConfig {
    fn default() -> Self {
        SchedulerConfig {
            max_concurrent_sources_per_session: 1,
            max_scheduler_prefetch_batches: 0,
            source_channel_capacity: 16,
            transport_channel_capacity: 16,
            batch_size: 1024,
        }
    }
}

impl SchedulerConfig {
    /// Validate configuration invariants.
    ///
    /// Returns an error if any field violates a hard constraint. Call this
    /// before creating channels or spawning tasks.
    pub fn validate(&self) -> Result<(), LogParseError> {
        if self.source_channel_capacity == 0 {
            return Err(LogParseError::InvalidAlgorithmConfig {
                algorithm: "async",
                field: "source_channel_capacity",
                message: "must be > 0".to_owned(),
            });
        }
        if self.transport_channel_capacity == 0 {
            return Err(LogParseError::InvalidAlgorithmConfig {
                algorithm: "async",
                field: "transport_channel_capacity",
                message: "must be > 0".to_owned(),
            });
        }
        if self.batch_size == 0 {
            return Err(LogParseError::InvalidAlgorithmConfig {
                algorithm: "async",
                field: "batch_size",
                message: "must be > 0".to_owned(),
            });
        }
        // Only single-source ordered activation is currently supported.
        // Multi-source with prefetch requires splitting next_source_seq into
        // "next to activate" and "front to emit" — tracked in the prefetch
        // buffer logic. Until that is implemented, reject > 1.
        if self.max_concurrent_sources_per_session != 1 {
            return Err(LogParseError::InvalidAlgorithmConfig {
                algorithm: "async",
                field: "max_concurrent_sources_per_session",
                message: "must be 1 (multi-source prefetch is not yet supported)".to_owned(),
            });
        }
        Ok(())
    }
}

/// Configuration for running multiple sessions over a directory.
///
/// Controls cross-session concurrency and per-session scheduler settings.
#[derive(Debug, Clone)]
pub struct DirectoryConfig {
    /// Maximum number of sessions that may run concurrently.
    ///
    /// Defaults to 1 (sequential, deterministic order).
    pub max_concurrent_sessions: usize,
    /// Per-session scheduler configuration.
    pub scheduler_config: SchedulerConfig,
}

impl Default for DirectoryConfig {
    fn default() -> Self {
        DirectoryConfig {
            max_concurrent_sessions: 1,
            scheduler_config: SchedulerConfig::default(),
        }
    }
}

impl DirectoryConfig {
    /// Validate configuration invariants.
    ///
    /// Returns an error if `max_concurrent_sessions` is 0 (would deadlock all
    /// sessions) or if the inner [`SchedulerConfig`] is invalid.
    pub fn validate(&self) -> Result<(), LogParseError> {
        if self.max_concurrent_sessions == 0 {
            return Err(LogParseError::InvalidAlgorithmConfig {
                algorithm: "async",
                field: "max_concurrent_sessions",
                message: "must be >= 1 (zero permits would deadlock all sessions)".to_owned(),
            });
        }
        self.scheduler_config.validate()
    }
}

/// Configuration for multi-worker indexed parse sessions.
///
/// Controls how entries are chunked and dispatched to `spawn_blocking` workers.
/// The `IndexedParser` is shared via `Arc` across workers since it is immutable
/// after construction. At most `worker_count` blocking tasks run concurrently;
/// excess chunks wait for a permit before spawning.
#[derive(Debug, Clone)]
pub struct IndexedWorkerConfig {
    /// Maximum number of `spawn_blocking` tasks that may run concurrently.
    ///
    /// Defaults to 1 (single worker, same as the non-parallel sessions).
    pub worker_count: usize,
    /// Number of log entries per chunk.
    ///
    /// Each chunk is dispatched to one worker. Defaults to 1024.
    pub chunk_size: usize,
}

impl Default for IndexedWorkerConfig {
    fn default() -> Self {
        IndexedWorkerConfig {
            worker_count: 1,
            chunk_size: 1024,
        }
    }
}

impl IndexedWorkerConfig {
    /// Validate configuration invariants.
    ///
    /// Returns an error if `worker_count` or `chunk_size` is 0.
    pub fn validate(&self) -> Result<(), LogParseError> {
        if self.worker_count == 0 {
            return Err(LogParseError::InvalidAlgorithmConfig {
                algorithm: "async",
                field: "worker_count",
                message: "must be >= 1 (zero workers would produce no output)".to_owned(),
            });
        }
        if self.chunk_size == 0 {
            return Err(LogParseError::InvalidAlgorithmConfig {
                algorithm: "async",
                field: "chunk_size",
                message: "must be >= 1 (zero-size chunks would loop forever)".to_owned(),
            });
        }
        Ok(())
    }
}

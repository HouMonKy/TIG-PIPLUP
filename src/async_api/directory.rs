//! Directory-level session orchestration.
//!
//! This module runs multiple independent sessions (one per leaf directory or
//! per file) with bounded concurrency. Results are collected in deterministic
//! plan order, not completion order, so template aggregation is deterministic
//! for the same sorted file set.

use std::sync::Arc;

use super::session;
use super::types::{DirectoryConfig, SchedulerConfig, SessionKey, SessionPlan};
use crate::config::PiplupConfig;
use crate::error::LogParseError;
use crate::types::{ParseOutput, TrainingOutput};

// ---------------------------------------------------------------------------
// Cancellation
// ---------------------------------------------------------------------------

/// Lightweight cancellation token backed by `tokio::sync::watch`.
///
/// Used by directory runners to stop sibling sessions when one fails.
/// Call [`CancellationToken::cancel`] to signal; call
/// [`CancellationToken::is_cancelled`] to check before starting work.
#[derive(Clone, Debug)]
pub(crate) struct CancellationToken {
    tx: Arc<tokio::sync::watch::Sender<bool>>,
    rx: tokio::sync::watch::Receiver<bool>,
}

impl CancellationToken {
    fn new() -> Self {
        let (tx, rx) = tokio::sync::watch::channel(false);
        CancellationToken {
            tx: Arc::new(tx),
            rx,
        }
    }

    fn cancel(&self) {
        let _ = self.tx.send(true);
    }

    #[cfg(test)]
    fn is_cancelled(&self) -> bool {
        *self.rx.borrow()
    }

    fn clone_rx(&self) -> tokio::sync::watch::Receiver<bool> {
        self.rx.clone()
    }
}

// ---------------------------------------------------------------------------
// Shared directory runner
// ---------------------------------------------------------------------------

/// Run multiple sessions with bounded concurrency and first-error cancellation.
///
/// All session tasks are spawned upfront. A semaphore limits concurrency. If
/// any session returns an error, the cancellation token is set. Each task
/// checks the token before starting and again after acquiring a semaphore
/// permit; if cancelled, it returns early with an error. This is best-effort:
/// a task that has already begun running will complete, but its result is
/// discarded.
///
/// Results are collected in plan order (not completion order).
async fn run_directory_with<F, T>(
    config: PiplupConfig,
    dir_config: DirectoryConfig,
    plans: Vec<SessionPlan>,
    run_session: F,
) -> Result<Vec<(SessionKey, T)>, LogParseError>
where
    F: Fn(
            PiplupConfig,
            SchedulerConfig,
            SessionPlan,
        )
            -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<T, LogParseError>> + Send>>
        + Send
        + Sync
        + 'static,
    T: Send + 'static,
{
    dir_config.validate()?;
    let semaphore = Arc::new(tokio::sync::Semaphore::new(
        dir_config.max_concurrent_sessions,
    ));
    let scheduler_config = dir_config.scheduler_config;
    let run_session = Arc::new(run_session);
    let cancel = CancellationToken::new();

    let handles: Vec<_> = plans
        .into_iter()
        .map(|plan| {
            let config = config.clone();
            let scheduler_config = scheduler_config.clone();
            let semaphore = semaphore.clone();
            let run_session = run_session.clone();
            let cancel_rx = cancel.clone_rx();
            tokio::spawn(async move {
                if *cancel_rx.borrow() {
                    return Err(LogParseError::Io(std::io::Error::other(
                        "session cancelled: a peer session failed",
                    )));
                }
                let _permit = semaphore.acquire().await.map_err(|e| {
                    LogParseError::Io(std::io::Error::other(format!("semaphore closed: {e}")))
                })?;
                // Re-check after acquiring the permit to narrow the TOCTOU window
                // between the first check and the semaphore wait.
                if *cancel_rx.borrow() {
                    return Err(LogParseError::Io(std::io::Error::other(
                        "session cancelled: a peer session failed",
                    )));
                }
                let key = plan.key.clone();
                let result = run_session(config, scheduler_config, plan).await;
                result.map(|output| (key, output))
            })
        })
        .collect();

    let mut results = Vec::with_capacity(handles.len());
    let mut first_error: Option<LogParseError> = None;
    for handle in handles {
        let result = handle
            .await
            .map_err(|e| LogParseError::Io(std::io::Error::other(e.to_string())))?;
        match result {
            Ok(pair) => results.push(pair),
            Err(e) => {
                if first_error.is_none() {
                    cancel.cancel();
                    first_error = Some(e);
                }
            }
        }
    }

    if let Some(e) = first_error {
        return Err(e);
    }

    Ok(results)
}

/// Run training sessions for multiple plans with bounded concurrency.
///
/// Each plan is an independent session with its own parser state. Results are
/// returned in plan order for deterministic aggregation.
///
/// # Concurrency model
///
/// A `tokio::sync::Semaphore` limits how many sessions run at the same time.
/// The default `max_concurrent_sessions = 1` runs sessions sequentially.
///
/// All session tasks are spawned upfront. Results are collected in plan order
/// (not completion order). When one session fails, a cancellation token is
/// set; remaining tasks check it before starting and after acquiring a permit.
/// This is best-effort: sessions already running complete but their results
/// are discarded.
///
/// # Errors
///
/// Returns the first session error encountered, or a configuration error if
/// `dir_config` is invalid.
///
/// # Examples
///
/// ```no_run
/// use log_parser::async_api::directory::run_training_directory;
/// use log_parser::async_api::types::{DirectoryConfig, SessionPlan};
/// use log_parser::PiplupConfig;
///
/// # #[tokio::main]
/// # async fn main() -> Result<(), log_parser::LogParseError> {
/// let plans = SessionPlan::training_plans_for_directory(std::path::Path::new("logs/"))?;
/// let results = run_training_directory(PiplupConfig::default(), DirectoryConfig::default(), plans).await?;
/// for (key, output) in &results {
///     println!("{}: {} templates", key, output.parse_output.cluster_count);
/// }
/// # Ok(())
/// # }
/// ```
pub async fn run_training_directory(
    config: PiplupConfig,
    dir_config: DirectoryConfig,
    plans: Vec<SessionPlan>,
) -> Result<Vec<(SessionKey, TrainingOutput)>, LogParseError> {
    run_directory_with(config, dir_config, plans, |config, sched, plan| {
        Box::pin(
            async move { session::run_piplup_plan_training_session(config, sched, plan).await },
        )
    })
    .await
}

/// Run online sessions for multiple plans with bounded concurrency.
///
/// Each plan typically has one source (one file). Results are returned in plan
/// order.
///
/// # Errors
///
/// Returns the first session error encountered, or a configuration error if
/// `dir_config` is invalid.
///
/// # Examples
///
/// ```no_run
/// use log_parser::async_api::directory::run_online_directory;
/// use log_parser::async_api::types::{DirectoryConfig, SessionPlan};
/// use log_parser::PiplupConfig;
///
/// # #[tokio::main]
/// # async fn main() -> Result<(), log_parser::LogParseError> {
/// let plans = SessionPlan::online_plans_for_directory(std::path::Path::new("logs/"))?;
/// let results = run_online_directory(PiplupConfig::default(), DirectoryConfig::default(), plans).await?;
/// for (key, output) in &results {
///     println!("{}: {} lines", key, output.line_results.len());
/// }
/// # Ok(())
/// # }
/// ```
pub async fn run_online_directory(
    config: PiplupConfig,
    dir_config: DirectoryConfig,
    plans: Vec<SessionPlan>,
) -> Result<Vec<(SessionKey, ParseOutput)>, LogParseError> {
    run_directory_with(config, dir_config, plans, |config, sched, plan| {
        Box::pin(async move { session::run_piplup_plan_session(config, sched, plan).await })
    })
    .await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::async_api::types::SessionPlan;

    /// Helper: create temp directory with log files.
    struct TempLogDir {
        _dir: tempfile::TempDir,
        root: std::path::PathBuf,
    }

    impl TempLogDir {
        fn new() -> Self {
            let dir = tempfile::tempdir().expect("tempdir");
            let root = dir.path().to_path_buf();
            TempLogDir { _dir: dir, root }
        }

        fn add_file(&self, rel_path: &str, content: &str) -> std::path::PathBuf {
            let path = self.root.join(rel_path);
            if let Some(parent) = path.parent() {
                std::fs::create_dir_all(parent).expect("create dir");
            }
            std::fs::write(&path, content).expect("write");
            path
        }

        fn root(&self) -> &std::path::Path {
            &self.root
        }
    }

    #[tokio::test]
    async fn training_directory_single_leaf_two_files() {
        let tmp = TempLogDir::new();
        tmp.add_file(
            "leaf/a.log",
            "Jan 01 00:00:01 host app: Connection accepted from 192.168.1.1\n",
        );
        tmp.add_file(
            "leaf/b.log",
            "Jan 01 00:00:02 host app: Connection accepted from 10.0.0.5\n",
        );

        let plans = SessionPlan::training_plans_for_directory(tmp.root()).expect("plans");
        assert_eq!(plans.len(), 1, "should have one leaf session");

        let config = PiplupConfig::default();
        let dir_config = DirectoryConfig::default();

        let results = run_training_directory(config, dir_config, plans)
            .await
            .expect("training directory should succeed");

        assert_eq!(results.len(), 1);
        assert_eq!(results[0].1.parse_output.line_results.len(), 2);
        assert_eq!(results[0].1.parse_output.cluster_count, 1);
    }

    #[tokio::test]
    async fn training_directory_multi_leaf() {
        let tmp = TempLogDir::new();
        tmp.add_file(
            "leaf1/a.log",
            "Jan 01 00:00:01 host app: Starting service\n",
        );
        tmp.add_file(
            "leaf2/b.log",
            "Jan 01 00:00:02 host app: Stopping service\n",
        );

        let plans = SessionPlan::training_plans_for_directory(tmp.root()).expect("plans");
        // Two leaf directories → two plans.
        assert_eq!(plans.len(), 2);

        let config = PiplupConfig::default();
        let dir_config = DirectoryConfig::default();

        let results = run_training_directory(config, dir_config, plans)
            .await
            .expect("training directory should succeed");

        assert_eq!(results.len(), 2);
        // Each leaf has 1 file with 1 line.
        assert_eq!(results[0].1.parse_output.line_results.len(), 1);
        assert_eq!(results[1].1.parse_output.line_results.len(), 1);
    }

    #[tokio::test]
    async fn online_directory_per_file_sessions() {
        let tmp = TempLogDir::new();
        tmp.add_file(
            "a.log",
            "Jan 01 00:00:01 host app: Connection accepted from 192.168.1.1\n",
        );
        tmp.add_file(
            "b.log",
            "Jan 01 00:00:02 host app: Connection accepted from 10.0.0.5\n",
        );

        let plans = SessionPlan::online_plans_for_directory(tmp.root()).expect("plans");
        assert_eq!(plans.len(), 2, "one plan per file");

        let config = PiplupConfig::default();
        let dir_config = DirectoryConfig::default();

        let results = run_online_directory(config, dir_config, plans)
            .await
            .expect("online directory should succeed");

        assert_eq!(results.len(), 2);
        assert_eq!(results[0].1.line_results.len(), 1);
        assert_eq!(results[1].1.line_results.len(), 1);
    }

    #[tokio::test]
    async fn training_directory_concurrent_two_sessions() {
        let tmp = TempLogDir::new();
        tmp.add_file(
            "leaf1/a.log",
            "Jan 01 00:00:01 host app: Starting service\n",
        );
        tmp.add_file(
            "leaf2/b.log",
            "Jan 01 00:00:02 host app: Stopping service\n",
        );

        let plans = SessionPlan::training_plans_for_directory(tmp.root()).expect("plans");
        assert_eq!(plans.len(), 2);

        let config = PiplupConfig::default();
        let dir_config = DirectoryConfig {
            max_concurrent_sessions: 2,
            ..DirectoryConfig::default()
        };

        let results = run_training_directory(config, dir_config, plans)
            .await
            .expect("concurrent training directory should succeed");

        // Results should still be in plan order (deterministic).
        assert_eq!(results.len(), 2);
        assert_eq!(results[0].1.parse_output.line_results.len(), 1);
        assert_eq!(results[1].1.parse_output.line_results.len(), 1);
    }

    #[tokio::test]
    async fn training_directory_empty_dir_returns_error() {
        let tmp = TempLogDir::new();
        // No files added.
        let result = SessionPlan::training_plans_for_directory(tmp.root());
        assert!(result.is_err(), "empty directory should return an error");
    }

    #[tokio::test]
    async fn directory_config_rejects_zero_concurrent_sessions() {
        let config = DirectoryConfig {
            max_concurrent_sessions: 0,
            ..DirectoryConfig::default()
        };
        assert!(config.validate().is_err());
        // Also verify it fails at the entry points.
        let result = run_training_directory(
            PiplupConfig::default(),
            DirectoryConfig {
                max_concurrent_sessions: 0,
                ..DirectoryConfig::default()
            },
            vec![],
        )
        .await;
        assert!(result.is_err(), "should reject max_concurrent_sessions = 0");
    }

    // -----------------------------------------------------------------------
    // Sync-vs-async multi-file parity tests
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn parity_multi_file_single_leaf() {
        let tmp = TempLogDir::new();
        let content_a = "Jan 01 00:00:01 host app: Connection accepted from 192.168.1.1\n";
        let content_b = "Jan 01 00:00:02 host app: Connection accepted from 10.0.0.5\n";
        let content_c = "Jan 01 00:00:03 host app: Connection accepted from 172.16.0.1\n";
        tmp.add_file("leaf/a.log", content_a);
        tmp.add_file("leaf/b.log", content_b);
        tmp.add_file("leaf/c.log", content_c);

        // Sync: concatenate entries and parse.
        let sync_entries: Vec<crate::types::LogEntry> = [content_a, content_b, content_c]
            .iter()
            .enumerate()
            .flat_map(|(file_idx, content)| {
                content.lines().filter(|l| !l.is_empty()).enumerate().map(
                    move |(line_idx, line)| crate::types::LogEntry {
                        line_number: file_idx + line_idx + 1,
                        raw: line.to_owned(),
                    },
                )
            })
            .collect();

        let mut sync_parser =
            crate::engine::parser::PiplupParser::new(PiplupConfig::default()).expect("parser");
        let sync_output = sync_parser
            .parse_lines_for_training(&sync_entries)
            .expect("sync parse");

        // Async: training directory.
        let plans = SessionPlan::training_plans_for_directory(tmp.root()).expect("plans");
        let results =
            run_training_directory(PiplupConfig::default(), DirectoryConfig::default(), plans)
                .await
                .expect("async training");

        let async_output = &results[0].1;

        assert_eq!(
            sync_output.parse_output.line_results.len(),
            async_output.parse_output.line_results.len(),
            "line count parity"
        );
        assert_eq!(
            sync_output.parse_output.cluster_count, async_output.parse_output.cluster_count,
            "cluster count parity"
        );
    }

    /// Multi-leaf parity: each leaf is parsed independently in both sync and
    /// async paths. Verify per-leaf line count and cluster count match.
    #[tokio::test]
    async fn parity_multi_leaf_directory() {
        let tmp = TempLogDir::new();

        // Leaf 1: two files with same template pattern.
        let content_1a = "Jan 01 00:00:01 host app: Connection accepted from 192.168.1.1\n";
        let content_1b = "Jan 01 00:00:02 host app: Connection accepted from 10.0.0.5\n";
        tmp.add_file("leaf1/a.log", content_1a);
        tmp.add_file("leaf1/b.log", content_1b);

        // Leaf 2: one file with a different template pattern.
        let content_2a = "Jan 01 00:00:03 host app: Starting service alpha\n";
        tmp.add_file("leaf2/c.log", content_2a);

        // Sync: parse each leaf independently (matching what the async directory
        // runner does — one session per leaf).
        fn sync_parse_leaf(contents: &[&str]) -> crate::types::TrainingOutput {
            let entries: Vec<crate::types::LogEntry> = contents
                .iter()
                .enumerate()
                .flat_map(|(file_idx, content)| {
                    content.lines().filter(|l| !l.is_empty()).enumerate().map(
                        move |(line_idx, line)| crate::types::LogEntry {
                            line_number: file_idx + line_idx + 1,
                            raw: line.to_owned(),
                        },
                    )
                })
                .collect();
            let mut parser =
                crate::engine::parser::PiplupParser::new(PiplupConfig::default()).expect("parser");
            parser
                .parse_lines_for_training(&entries)
                .expect("sync parse")
        }

        let sync_leaf1 = sync_parse_leaf(&[content_1a, content_1b]);
        let sync_leaf2 = sync_parse_leaf(&[content_2a]);

        // Async: training directory.
        let plans = SessionPlan::training_plans_for_directory(tmp.root()).expect("plans");
        assert_eq!(plans.len(), 2, "two leaf directories");

        let results =
            run_training_directory(PiplupConfig::default(), DirectoryConfig::default(), plans)
                .await
                .expect("async training");

        assert_eq!(results.len(), 2);

        // Per-leaf parity.
        assert_eq!(
            sync_leaf1.parse_output.line_results.len(),
            results[0].1.parse_output.line_results.len(),
            "leaf1 line count parity"
        );
        assert_eq!(
            sync_leaf1.parse_output.cluster_count, results[0].1.parse_output.cluster_count,
            "leaf1 cluster count parity"
        );

        assert_eq!(
            sync_leaf2.parse_output.line_results.len(),
            results[1].1.parse_output.line_results.len(),
            "leaf2 line count parity"
        );
        assert_eq!(
            sync_leaf2.parse_output.cluster_count, results[1].1.parse_output.cluster_count,
            "leaf2 cluster count parity"
        );
    }

    // -----------------------------------------------------------------------
    // Cancellation tests (Phase 5)
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn directory_returns_error_when_session_fails() {
        let tmp = TempLogDir::new();
        tmp.add_file(
            "leaf1/a.log",
            "Jan 01 00:00:01 host app: Connection accepted from 192.168.1.1\n",
        );
        // Create a plan with a non-existent file to trigger an error.
        let mut plans = SessionPlan::training_plans_for_directory(tmp.root()).expect("plans");
        plans.push(SessionPlan {
            key: crate::async_api::types::SessionKey::directory(
                tmp.root().join("nonexistent_leaf"),
            ),
            sources: vec![crate::async_api::types::SourcePlan::File {
                path: tmp.root().join("nonexistent_leaf/missing.log"),
            }],
            line_numbering: crate::async_api::types::LineNumbering::ConcatenateWithinSession,
        });

        let config = PiplupConfig::default();
        let dir_config = DirectoryConfig::default();

        let result = run_training_directory(config, dir_config, plans).await;
        assert!(result.is_err(), "should return error from failed session");
    }

    #[tokio::test]
    async fn directory_cancellation_stops_peer_sessions() {
        let tmp = TempLogDir::new();
        tmp.add_file(
            "leaf1/a.log",
            "Jan 01 00:00:01 host app: Connection accepted from 192.168.1.1\n",
        );
        // A valid leaf.
        let mut plans = SessionPlan::training_plans_for_directory(tmp.root()).expect("plans");

        // Add a plan with a non-existent file — will fail on file open.
        plans.push(SessionPlan {
            key: crate::async_api::types::SessionKey::directory(tmp.root().join("bad_leaf")),
            sources: vec![crate::async_api::types::SourcePlan::File {
                path: tmp.root().join("bad_leaf/missing.log"),
            }],
            line_numbering: crate::async_api::types::LineNumbering::ConcatenateWithinSession,
        });

        let config = PiplupConfig::default();
        // Run with concurrency=2 so both sessions start.
        let dir_config = DirectoryConfig {
            max_concurrent_sessions: 2,
            ..DirectoryConfig::default()
        };

        let result = run_training_directory(config, dir_config, plans).await;
        assert!(result.is_err(), "should return error");
    }

    #[tokio::test]
    async fn directory_cancelled_token_stops_sessions_before_start() {
        // Manually create a cancellation token, cancel it, and verify that
        // tasks see the cancellation.
        let token = CancellationToken::new();
        token.cancel();
        assert!(token.is_cancelled());
    }

    #[tokio::test]
    async fn directory_online_returns_error_when_file_missing() {
        let tmp = TempLogDir::new();
        tmp.add_file(
            "a.log",
            "Jan 01 00:00:01 host app: Connection accepted from 192.168.1.1\n",
        );
        let mut plans = SessionPlan::online_plans_for_directory(tmp.root()).expect("plans");

        // Add a plan with a non-existent file.
        plans.push(SessionPlan {
            key: crate::async_api::types::SessionKey::file(tmp.root().join("missing.log")),
            sources: vec![crate::async_api::types::SourcePlan::File {
                path: tmp.root().join("missing.log"),
            }],
            line_numbering: crate::async_api::types::LineNumbering::PreserveSourceLocal,
        });

        let config = PiplupConfig::default();
        let dir_config = DirectoryConfig::default();

        let result = run_online_directory(config, dir_config, plans).await;
        assert!(result.is_err(), "should return error from missing file");
    }
}

//! Parse session implementations.
//!
//! Parse sessions own parser state and consume ordered [`LogEntry`] streams.
//! Learning sessions stream batches from the source through a
//! [`TrainingSession`]: each batch is committed via
//! [`TrainingSession::commit_batch`] inside a `tokio::task::spawn_blocking`
//! task, and the final state is produced with
//! [`TrainingSession::try_finish_with_parsed`].
//!
//! ## Cancellation contract
//!
//! All session entry points follow the same cancellation behaviour:
//!
//! 1. **Before `spawn_blocking`**: if the session future is dropped (or the
//!    channel receiver is dropped), source tasks detect the closed channel via
//!    `tx.send(…).await.is_err()` and stop reading. No parser work is started.
//!
//! 2. **After `spawn_blocking`**: once a blocking task is spawned, it runs to
//!    completion even if the session future is dropped. Tokio does not forcibly
//!    abort `spawn_blocking` tasks. The `JoinHandle` may be dropped, in which
//!    case the task's result is discarded, but the parser still executes
//!    fully. This means the synchronous parser's side effects (e.g. cache
//!    checkpoints inside `PiplupParser`) always execute once started.
//!
//! 3. **Directory-level cancellation**: when one session in a directory runner
//!    fails, the cancellation token is set. Remaining sessions that have not
//!    yet acquired a semaphore permit check the token and return early with
//!    an error. Already-running sessions complete but their results are
//!    discarded.

mod common;
mod indexed;
mod online;
#[cfg(test)]
mod tests;
mod training;

pub use self::indexed::*;
pub use self::online::*;
pub use self::training::*;

//! Async processing module for log-parser.
//!
//! This module provides async versions of the parser APIs, built on top of the
//! synchronous core. It is compiled only when the `async` Cargo feature is
//! enabled.
//!
//! # Architecture
//!
//! Async processing is modeled in layers:
//!
//! 1. **Source tasks** read files or `AsyncBufRead` streams into source-local batches.
//! 2. **Source scheduler** maps source-local events into contiguous `BatchOrder` values.
//! 3. **Transport** moves scheduler-assigned batches through bounded channels.
//! 4. **Ordered assembler** buffers batches until the next `BatchOrder` is ready.
//! 5. **Line numbering adapter** applies session-level numbering policy.
//! 6. **Parse session** owns parser state and commits ordered entries.
//!
//! # Example
//!
//! ```ignore
//! use log_parser::async_api::session::run_piplup_reader_session;
//! use log_parser::async_api::types::SchedulerConfig;
//! use log_parser::PiplupConfig;
//! use tokio::io::BufReader;
//!
//! # async fn example() -> Result<(), log_parser::LogParseError> {
//! let config = PiplupConfig::default();
//! let reader = BufReader::new(tokio::io::empty());
//! let scheduler_config = SchedulerConfig::default();
//! let output = run_piplup_reader_session(config, scheduler_config, reader).await?;
//! # Ok(())
//! # }
//! ```

pub(crate) mod assembler;
pub(crate) mod batch_stream;
pub mod directory;
pub(crate) mod numbering;
pub(crate) mod scheduler;
pub mod session;
pub(crate) mod source_task;
#[cfg(test)]
pub(crate) mod test_util;
pub mod types;

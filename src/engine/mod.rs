//! Core piplup training and matching engine.
//!
//! The engine implements the online template-learning algorithm:
//!
//! - [`parser::PiplupParser`] is the high-level stateful parser.
//! - [`session::TrainingSession`] provides a streaming, batch-commit API.
//! - [`matcher`] contains sequence similarity and prefix-tree search.
//! - [`merge`] merges related templates after cluster updates.
//! - [`output`] builds the final [`ParseOutput`] from parser state.
//! - [`assignment`] converts explicit algorithm assignments (e.g. Drain3) into
//!   the same output shape.

/// Convert explicit algorithm assignments into [`ParseOutput`].
pub mod assignment;
/// Sequence similarity, prefix-tree search, and fast matching helpers.
pub mod matcher;
/// Merge related templates after cluster updates.
pub mod merge;
/// Build the final [`ParseOutput`] from parser state.
pub mod output;
/// High-level online piplup parser.
pub mod parser;
/// Streaming training session with batch commits.
pub mod session;
/// Structure-aware token merging (TIG-PIPLUP `_structure_aware_merge`).
pub(crate) mod structure_merge;
pub(crate) mod util;

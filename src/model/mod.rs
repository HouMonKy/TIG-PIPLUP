//! Data model for log clusters, tokens, and parser state.
//!
//! The model module defines the in-memory representation of a learned log
//! template (`LogCluster`), the tokens it contains (`Token`), and the index
//! structures (`ParseState`) that speed up matching.

/// A learned log template cluster.
pub mod cluster;
/// Parser state and constant-bucket index structures.
pub mod state;
/// Tokens: literals and wildcards with head/tail segments.
pub mod token;
/// Token-internal structure recognition (kv shapes, skeletons).
pub use cluster::LogCluster;
pub use state::{ClusterId, ConstantBucket, ConstantKey, ParseState};
pub use token::Token;

//! Binary `templates.idx` index for read-only template assignment.
//!
//! The index captures the output of training (templates, length buckets, anchor
//! postings, and preprocess plan) and supports fast deterministic parsing of
//! new logs without re-running the online learner.
//!
//! - [`IndexBuilder`] creates an index from training output.
//! - [`IndexWriter`] serializes an index to bytes.
//! - [`IndexReader`] validates and deserializes an index.
//! - [`IndexedParser`] assigns log lines to indexed templates.
//! - [`IndexedResultAccumulator`] merges batched indexed results.

mod accumulator;
mod builder;
pub(crate) mod matcher;
mod parser;
mod reader;
mod types;
mod writer;

pub use accumulator::IndexedResultAccumulator;
pub use builder::IndexBuilder;
pub use parser::{IndexedBatchResult, IndexedParseOutput, IndexedParseStats, IndexedParser};
pub use reader::IndexReader;
pub(crate) use types::PREPROCESS_RULES_VERSION;
pub use types::{
    AlgorithmTrainingMetadata, IndexBuildContext, IndexHeader, RuntimeCompatibility,
    TemplateIndexFile, TrainingMetadata,
};
pub use writer::IndexWriter;

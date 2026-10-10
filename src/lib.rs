//! Online log template learning and indexed read-only parser.
//!
//! The `log-parser` crate implements the *piplup* algorithm for learning log
//! templates from plain text logs, and a binary `templates.idx` index that
//! supports fast, deterministic template assignment without re-training.
//!
//! # Core workflow
//!
//! 1. **Train** [`PiplupParser`] on raw logs to discover templates and produce
//!    structured output.
//! 2. **Write** the learned model to an index file with [`IndexBuilder`].
//! 3. **Parse** new logs offline against the index using [`IndexedParser`].
//!
//! # Example
//!
//! ```rust
//! use log_parser::{
//!     PiplupConfig, PiplupParser, SimThresh, IndexedParser, IndexBuilder,
//! };
//!
//! # fn main() -> Result<(), Box<dyn std::error::Error>> {
//! let config = PiplupConfig {
//!     log_format: vec!["<Date> <Time> <Content>".into()],
//!     sim_thresh: SimThresh::Dynamic,
//!     ..Default::default()
//! };
//! # Ok(())
//! # }
//! ```

#[cfg(feature = "async")]
pub mod async_api;
pub mod cache;
#[cfg(feature = "cli")]
pub mod cli;
pub mod config;
pub mod drain3;
pub mod engine;
pub mod error;
pub mod format;
pub mod index;
pub mod input_plan;
pub mod model;
pub(crate) mod output_core;
pub(crate) mod parse_core;
pub mod patterns;
#[doc(hidden)]
pub mod placeholder;
pub mod postprocess;
pub mod preprocessor;
pub mod progress;
mod template;
pub mod types;

pub use cache::{CacheManager, CacheState, CacheStateError, RestoredState};
pub use config::{PiplupConfig, SimThresh, SimThresholds};
pub use drain3::{
    DRAIN3_CACHE_FORMAT_VERSION, DRAIN3_CACHE_MAGIC, Drain3CacheCluster, Drain3CacheManager,
    Drain3CacheRouteKey, Drain3CacheState, Drain3Config, Drain3InitialCacheState, Drain3Parser,
    Drain3RestoredState, drain3_path_identity,
};
pub use engine::assignment::{LearnedTemplateAssignment, build_output_from_assignments};
pub use engine::matcher::{get_template, jaccard_similarity, seq_dist, template_matches};
pub use engine::output::build_training_output;
pub use engine::parser::{InitialCacheState, PiplupParser};
pub use engine::session::{
    BatchCommitInfo, PlanStatus, SessionState, TrainingSession, TrainingSessionBuilder,
};
pub use error::LogParseError;
#[deprecated(since = "0.2.0", note = "Use LogParseError instead")]
pub use error::LogParseError as PiplupError;
#[deprecated(since = "0.2.0", note = "Use LogParseError instead")]
pub use error::LogParseError as LogParserError;
pub use format::{CompiledLogFormat, MultiCompiledLogFormat};
pub use index::{
    AlgorithmTrainingMetadata, IndexBuildContext, IndexBuilder, IndexHeader, IndexReader,
    IndexWriter, IndexedBatchResult, IndexedParseOutput, IndexedParseStats, IndexedParser,
    IndexedResultAccumulator, RuntimeCompatibility, TemplateIndexFile, TrainingMetadata,
};
pub use model::{ClusterId, ConstantBucket, ConstantKey, LogCluster, ParseState, Token};
pub use patterns::{
    CommonPatternParams, Delimiter, Drain3PatternParams, GetLogPatternsRequest,
    GetLogPatternsResponse, LogPatternAlgorithm, LogPatternAlgorithmTag, LogPatternBuilder,
    LogPatternVariableKind, LogPatterns, LogTemplate, LogTemplateDto, PiplupPatternParams, Ratio,
    RegexpDialect, RowPatternRef, TemplateId,
};
pub use postprocess::correct_single_template;
pub use preprocessor::{EstimateResult, PreprocessPlan, Preprocessor, RuleId};
pub use progress::ParseProgress;
pub use template::event_id_for_template;
pub use types::{
    EventRecord, FieldProjection, LineResult, LogEntry, ParseOutput, ParsedField, ParsedLine,
    TrainingOutput,
};

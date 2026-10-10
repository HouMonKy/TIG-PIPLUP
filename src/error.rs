//! Error type for the `log-parser` library.
//!
//! All fallible public API surfaces return [`std::result::Result<T, LogParseError>`].
//! Variants wrap I/O, regex, CSV, and JSON errors and add domain-specific
//! context such as line numbers, field names, and compatibility failures.

/// The single error type returned by the `log-parser` library.
///
/// # Example
///
/// ```rust
/// use log_parser::LogParseError;
///
/// let err = LogParseError::MissingContentField;
/// assert!(err.to_string().contains("<Content>"));
/// ```
#[derive(Debug, thiserror::Error)]
pub enum LogParseError {
    /// An I/O operation failed.
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),

    /// A regular expression could not be compiled.
    #[error("regex error: {0}")]
    Regex(#[from] regex::Error),

    /// CSV parsing or serialization failed.
    #[error("csv error: {0}")]
    Csv(#[from] csv::Error),

    /// JSON parsing or serialization failed.
    #[error("json error: {0}")]
    Json(#[from] serde_json::Error),

    /// The supplied log format string is invalid.
    #[error("invalid log format: {0}")]
    InvalidLogFormat(String),

    /// The log format string does not contain a `<Content>` field.
    #[error("log format must contain a <Content> field")]
    MissingContentField,

    /// A line could not be parsed.
    #[error("parse error at line {line}: {message}")]
    ParseError {
        /// Line number in the input where the error occurred.
        line: usize,
        /// Human-readable description of what went wrong.
        message: String,
    },

    /// The same line number appeared more than once in a parse input batch.
    #[error("duplicate line_number in parse input: {line_number}")]
    DuplicateLineNumber {
        /// The duplicated line number.
        line_number: usize,
    },

    /// A training or parsing batch contains a gap in line numbering.
    #[error("session batch gap: expected line_number {expected}, got {actual}")]
    SessionGap {
        /// The next expected line number.
        expected: usize,
        /// The line number actually observed.
        actual: usize,
    },

    /// The index file is malformed or unreadable.
    #[error("invalid index file: {message}")]
    InvalidIndex {
        /// Description of the problem.
        message: String,
    },

    /// Runtime configuration does not match the index compatibility record.
    #[error("runtime configuration does not match index compatibility metadata")]
    IncompatibleIndexRuntime,

    /// The cache file is malformed or unreadable.
    #[error("invalid cache file: {message}")]
    InvalidCache {
        /// Description of the problem.
        message: String,
    },

    /// The cache file format version is not supported.
    #[error("cache format version mismatch: file={file}, expected={expected}")]
    CacheVersionMismatch {
        /// Version found in the file.
        file: u32,
        /// Version expected by this crate.
        expected: u32,
    },

    /// Runtime configuration does not match the cache compatibility record.
    #[error("runtime configuration does not match cache compatibility metadata")]
    IncompatibleCacheRuntime,

    /// The preprocess plan in the cache does not match the current plan.
    #[error("preprocess plan does not match cache compatibility metadata")]
    IncompatibleCachePlan,

    /// Caching was requested for stdin input, which is not supported.
    #[error("--cache is not applicable to stdin input")]
    CacheNotApplicableToStdin,

    /// Checkpointing was requested without an active cache path.
    #[error("--checkpoint-lines requires --cache")]
    CacheCheckpointRequiresCache,

    /// An output field requested by the user is not known.
    #[error("unknown output field '{field}'; available fields: {available}")]
    UnknownOutputField {
        /// The field name that could not be found.
        field: String,
        /// Comma-separated list of valid field names.
        available: String,
    },

    /// The same output field was requested twice.
    #[error("duplicate output field: '{field}'")]
    DuplicateOutputField {
        /// The duplicated field name.
        field: String,
    },

    /// An algorithm-specific configuration field is invalid.
    #[error("invalid {algorithm} config field {field}: {message}")]
    InvalidAlgorithmConfig {
        /// Algorithm name, e.g. `"drain3"`.
        algorithm: &'static str,
        /// Configuration field name.
        field: &'static str,
        /// Description of why the value is invalid.
        message: String,
    },

    /// A generic configuration field is invalid.
    #[error("invalid config field {field}: {message}")]
    InvalidConfig {
        /// Configuration field name.
        field: &'static str,
        /// Description of why the value is invalid.
        message: String,
    },

    /// A feature or input is not yet supported.
    #[error("{0}")]
    Unsupported(&'static str),
}

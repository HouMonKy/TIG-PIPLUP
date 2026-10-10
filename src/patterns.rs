//! Content-level log pattern extraction facade.
//!
//! This module wraps the existing PIPLUP and Drain3 learners with a one-shot
//! API for callers that already have content strings and only need deduplicated
//! log templates with per-row assignments.

use std::{fmt, num::NonZeroUsize};

use serde::{Deserialize, Deserializer, Serialize, Serializer, de};
use serde_json::{Map, Value};

use crate::{
    Drain3Config, Drain3Parser, LogEntry, LogParseError, PiplupConfig, PiplupParser, SimThresh,
    TrainingOutput,
    parse_core::{
        MaskEvidence, build_placeholder_evidence, join_mask_evidence, tokenize_index_content,
    },
    placeholder::PlaceholderKind,
    template::compile::{CompiledTemplate, TemplateToken, normalize_template_wildcards},
};

const SYNTHETIC_CONTENT_FORMAT: &str = "(?s:<Content>)";
const CUSTOM_DELIMITERS_UNSUPPORTED_PIPLUP: &str = "custom delimiters are not supported by the piplup algorithm; use `separators` for whitespace-equivalent boundaries";
const CUSTOM_SEPARATORS_UNSUPPORTED_DRAIN3: &str =
    "custom separators are not supported by the drain3 algorithm";

/// Template unique identifier (newtype with private field).
///
/// # Example
///
/// ```rust
/// use log_parser::TemplateId;
///
/// // TemplateId is created internally by LogPatterns
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct TemplateId(usize);

impl TemplateId {
    /// Return the index of this ID in the template table.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::LogPatternBuilder;
    ///
    /// let patterns = LogPatternBuilder::piplup().extract(["a", "b"]).unwrap();
    /// let id = patterns.assignments()[0];
    /// assert_eq!(id.index(), 0);
    /// ```
    pub fn index(self) -> usize {
        self.0
    }
}

/// A single log template.
///
/// # Example
///
/// ```rust
/// use log_parser::LogPatternBuilder;
///
/// let patterns = LogPatternBuilder::piplup().extract(["worker started"]).unwrap();
/// let template = &patterns.templates()[0];
/// assert_eq!(template.pattern(), "worker started");
/// assert_eq!(template.occurrences(), 1);
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LogTemplate {
    pattern: String,
    regexp: String,
    occurrences: usize,
    variables: Vec<LogPatternVariableKind>,
}

impl LogTemplate {
    /// Return the rendered template pattern.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::LogPatternBuilder;
    ///
    /// let patterns = LogPatternBuilder::piplup().extract(["done"]).unwrap();
    /// assert_eq!(patterns.templates()[0].pattern(), "done");
    /// ```
    pub fn pattern(&self) -> &str {
        &self.pattern
    }

    /// Return the regex generated mechanically from the pattern.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::LogPatternBuilder;
    ///
    /// let patterns = LogPatternBuilder::piplup().extract(["done"]).unwrap();
    /// assert!(regex::Regex::new(patterns.templates()[0].regexp()).unwrap().is_match("DONE"));
    /// ```
    pub fn regexp(&self) -> &str {
        &self.regexp
    }

    /// Return the number of rows matched by this template.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::LogPatternBuilder;
    ///
    /// let patterns = LogPatternBuilder::piplup().extract(["a", "a"]).unwrap();
    /// assert_eq!(patterns.templates()[0].occurrences(), 2);
    /// ```
    pub fn occurrences(&self) -> usize {
        self.occurrences
    }

    /// Return the variable kind of each `<*>` wildcard in the pattern.
    ///
    /// The `i`-th element corresponds to the `i`-th `<*>` in
    /// [`pattern()`](Self::pattern), counted left to right; the invariant
    /// `variables().len() == pattern().matches("<*>").count()` always holds.
    ///
    /// Kinds are evidence, not guesses (ADR-0043): a non-`Unknown` kind means
    /// every observation assigned to that slot classified as that kind under
    /// the ADR-0033 conservative join. With `typed_placeholders` disabled (the
    /// default), after conflicting or unclassifiable observations, or when
    /// 1:1 token alignment is impossible, the kind is
    /// [`LogPatternVariableKind::Unknown`]. Eager preprocessing rules that
    /// rewrite a token to `<*>` before classification also leave the slot
    /// `Unknown`; the default catalog's non-classifier dotted-chain rules
    /// (`host_port`, `package_host`) are shape-guarded to require a non-digit
    /// word character, so IPv4 forms survive to the classifier.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::{LogPatternBuilder, LogPatternVariableKind};
    ///
    /// let patterns = LogPatternBuilder::piplup()
    ///     .typed_placeholders(true)
    ///     .extract(["worker 10.0.0.1 connected", "worker 10.0.0.2 connected"])
    ///     .unwrap();
    /// let template = &patterns.templates()[0];
    /// assert_eq!(template.variables(), &[LogPatternVariableKind::Ipv4]);
    /// ```
    pub fn variables(&self) -> &[LogPatternVariableKind] {
        &self.variables
    }
}

/// A row and its corresponding template reference.
///
/// # Example
///
/// ```rust
/// use log_parser::LogPatternBuilder;
///
/// let patterns = LogPatternBuilder::piplup().extract(["worker started"]).unwrap();
/// let row = patterns.iter_rows().next().unwrap();
/// assert_eq!(row.row_index, 0);
/// assert_eq!(row.template.pattern(), "worker started");
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RowPatternRef<'a> {
    /// Zero-based row index in the original input.
    pub row_index: usize,
    /// The template ID for this row.
    pub template_id: TemplateId,
    /// Reference to the template.
    pub template: &'a LogTemplate,
}

/// A finite ratio in the inclusive range `0.0..=1.0`.
///
/// # Example
///
/// ```rust
/// use log_parser::Ratio;
///
/// let ratio = Ratio::try_from(0.5).unwrap();
/// assert_eq!(ratio.as_f64(), 0.5);
/// ```
#[derive(Debug, Clone, Copy, PartialEq, PartialOrd)]
pub struct Ratio(f64);

impl Ratio {
    /// Return the validated ratio as an `f64`.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::Ratio;
    ///
    /// assert_eq!(Ratio::try_from(1.0).unwrap().as_f64(), 1.0);
    /// ```
    pub fn as_f64(self) -> f64 {
        self.0
    }
}

impl TryFrom<f64> for Ratio {
    type Error = LogParseError;

    fn try_from(value: f64) -> Result<Self, Self::Error> {
        if value.is_finite() && (0.0..=1.0).contains(&value) {
            Ok(Self(value))
        } else {
            Err(LogParseError::InvalidConfig {
                field: "ratio",
                message: format!("must be finite and in 0.0..=1.0, got {value}"),
            })
        }
    }
}

/// A non-empty custom delimiter string.
///
/// ADR-0042: custom delimiters are honored by the Drain3 algorithm; the
/// PIPLUP algorithm rejects non-empty delimiter lists as unsupported.
///
/// # Example
///
/// ```rust
/// use log_parser::Delimiter;
///
/// let delimiter = Delimiter::try_from("||").unwrap();
/// assert_eq!(delimiter.as_str(), "||");
/// ```
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Delimiter(String);

impl Delimiter {
    /// Borrow the delimiter text.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::Delimiter;
    ///
    /// let delimiter = Delimiter::try_from("=").unwrap();
    /// assert_eq!(delimiter.as_str(), "=");
    /// ```
    pub fn as_str(&self) -> &str {
        &self.0
    }

    /// Consume the delimiter and return its string.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::Delimiter;
    ///
    /// let delimiter = Delimiter::try_from("/").unwrap();
    /// assert_eq!(delimiter.into_string(), "/");
    /// ```
    pub fn into_string(self) -> String {
        self.0
    }
}

impl TryFrom<String> for Delimiter {
    type Error = LogParseError;

    fn try_from(value: String) -> Result<Self, Self::Error> {
        if value.is_empty() {
            Err(LogParseError::InvalidConfig {
                field: "delimiters",
                message: "delimiter must not be empty".to_string(),
            })
        } else {
            Ok(Self(value))
        }
    }
}

impl TryFrom<&str> for Delimiter {
    type Error = LogParseError;

    fn try_from(value: &str) -> Result<Self, Self::Error> {
        Self::try_from(value.to_string())
    }
}

/// Common RFC-0014 pattern parameters.
///
/// These fields are validated but intentionally unsupported during extraction
/// until an exact parser-independent meaning is defined.
///
/// # Example
///
/// ```rust
/// use log_parser::{CommonPatternParams, Ratio};
///
/// let params = CommonPatternParams::default()
///     .with_tolerance(Ratio::try_from(0.25).unwrap());
/// assert!(params.tolerance().is_some());
/// ```
#[derive(Debug, Clone, Default, PartialEq)]
#[non_exhaustive]
pub struct CommonPatternParams {
    threshold: Option<NonZeroUsize>,
    tolerance: Option<Ratio>,
    max_digit_ratio: Option<Ratio>,
}

impl CommonPatternParams {
    /// Set the common `threshold` parameter.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::CommonPatternParams;
    /// use std::num::NonZeroUsize;
    ///
    /// let params = CommonPatternParams::default()
    ///     .with_threshold(NonZeroUsize::new(3).unwrap());
    /// assert_eq!(params.threshold().unwrap().get(), 3);
    /// ```
    pub fn with_threshold(mut self, threshold: NonZeroUsize) -> Self {
        self.threshold = Some(threshold);
        self
    }

    /// Set the common `tolerance` parameter.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::{CommonPatternParams, Ratio};
    ///
    /// let params = CommonPatternParams::default()
    ///     .with_tolerance(Ratio::try_from(0.1).unwrap());
    /// assert_eq!(params.tolerance().unwrap().as_f64(), 0.1);
    /// ```
    pub fn with_tolerance(mut self, tolerance: Ratio) -> Self {
        self.tolerance = Some(tolerance);
        self
    }

    /// Set the common `max_digit_ratio` parameter.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::{CommonPatternParams, Ratio};
    ///
    /// let params = CommonPatternParams::default()
    ///     .with_max_digit_ratio(Ratio::try_from(0.8).unwrap());
    /// assert_eq!(params.max_digit_ratio().unwrap().as_f64(), 0.8);
    /// ```
    pub fn with_max_digit_ratio(mut self, max_digit_ratio: Ratio) -> Self {
        self.max_digit_ratio = Some(max_digit_ratio);
        self
    }

    /// Return the common `threshold` parameter.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::CommonPatternParams;
    ///
    /// assert!(CommonPatternParams::default().threshold().is_none());
    /// ```
    pub fn threshold(&self) -> Option<NonZeroUsize> {
        self.threshold
    }

    /// Return the common `tolerance` parameter.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::CommonPatternParams;
    ///
    /// assert!(CommonPatternParams::default().tolerance().is_none());
    /// ```
    pub fn tolerance(&self) -> Option<Ratio> {
        self.tolerance
    }

    /// Return the common `max_digit_ratio` parameter.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::CommonPatternParams;
    ///
    /// assert!(CommonPatternParams::default().max_digit_ratio().is_none());
    /// ```
    pub fn max_digit_ratio(&self) -> Option<Ratio> {
        self.max_digit_ratio
    }
}

/// PIPLUP-specific facade parameters with exact mappings to `PiplupConfig`.
///
/// # Example
///
/// ```rust
/// use log_parser::{PiplupPatternParams, Ratio};
/// use std::num::NonZeroUsize;
///
/// let params = PiplupPatternParams::default()
///     .with_hit_limit(NonZeroUsize::new(100).unwrap())
///     .with_sim_thresh(Ratio::try_from(0.4).unwrap());
/// assert_eq!(params.hit_limit().unwrap().get(), 100);
/// ```
#[derive(Debug, Clone, Default, PartialEq)]
#[non_exhaustive]
pub struct PiplupPatternParams {
    hit_limit: Option<NonZeroUsize>,
    br_thresh: Option<NonZeroUsize>,
    sim_thresh: Option<Ratio>,
    merge: Option<bool>,
    max_token_bytes: Option<usize>,
    max_line_bytes: Option<usize>,
}

impl PiplupPatternParams {
    /// Set `PiplupConfig.hit_limit`.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::PiplupPatternParams;
    /// use std::num::NonZeroUsize;
    ///
    /// let params = PiplupPatternParams::default().with_hit_limit(NonZeroUsize::new(100).unwrap());
    /// assert_eq!(params.hit_limit().unwrap().get(), 100);
    /// ```
    pub fn with_hit_limit(mut self, hit_limit: NonZeroUsize) -> Self {
        self.hit_limit = Some(hit_limit);
        self
    }

    /// Set `PiplupConfig.br_thresh`.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::PiplupPatternParams;
    /// use std::num::NonZeroUsize;
    ///
    /// let params = PiplupPatternParams::default().with_br_thresh(NonZeroUsize::new(2).unwrap());
    /// assert_eq!(params.br_thresh().unwrap().get(), 2);
    /// ```
    pub fn with_br_thresh(mut self, br_thresh: NonZeroUsize) -> Self {
        self.br_thresh = Some(br_thresh);
        self
    }

    /// Set `PiplupConfig.sim_thresh` to `SimThresh::Fixed`.
    ///
    /// When unset, the parser keeps its default `SimThresh::Dynamic` mode.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::{PiplupPatternParams, Ratio};
    ///
    /// let params = PiplupPatternParams::default().with_sim_thresh(Ratio::try_from(0.5).unwrap());
    /// assert_eq!(params.sim_thresh().unwrap().as_f64(), 0.5);
    /// ```
    pub fn with_sim_thresh(mut self, sim_thresh: Ratio) -> Self {
        self.sim_thresh = Some(sim_thresh);
        self
    }

    /// Set `PiplupConfig.merge`.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::PiplupPatternParams;
    ///
    /// let params = PiplupPatternParams::default().with_merge(false);
    /// assert_eq!(params.merge(), Some(false));
    /// ```
    pub fn with_merge(mut self, merge: bool) -> Self {
        self.merge = Some(merge);
        self
    }

    /// Return `PiplupConfig.hit_limit` when set.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::PiplupPatternParams;
    ///
    /// assert!(PiplupPatternParams::default().hit_limit().is_none());
    /// ```
    pub fn hit_limit(&self) -> Option<NonZeroUsize> {
        self.hit_limit
    }

    /// Return `PiplupConfig.br_thresh` when set.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::PiplupPatternParams;
    ///
    /// assert!(PiplupPatternParams::default().br_thresh().is_none());
    /// ```
    pub fn br_thresh(&self) -> Option<NonZeroUsize> {
        self.br_thresh
    }

    /// Return `PiplupConfig.sim_thresh` when set.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::PiplupPatternParams;
    ///
    /// assert!(PiplupPatternParams::default().sim_thresh().is_none());
    /// ```
    pub fn sim_thresh(&self) -> Option<Ratio> {
        self.sim_thresh
    }

    /// Set `PiplupConfig.max_token_bytes`.
    ///
    /// Tokens strictly longer than this many bytes are dropped before
    /// clustering. `0` is legal (drops every token); `None` (unset) keeps
    /// unlimited tokens.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::PiplupPatternParams;
    ///
    /// let params = PiplupPatternParams::default().with_max_token_bytes(512);
    /// assert_eq!(params.max_token_bytes(), Some(512));
    /// ```
    pub fn with_max_token_bytes(mut self, max_token_bytes: usize) -> Self {
        self.max_token_bytes = Some(max_token_bytes);
        self
    }

    /// Set `PiplupConfig.max_line_bytes`.
    ///
    /// Lines whose preprocessed content exceeds this many bytes are excluded
    /// from clustering (sentinel event_id "0"). `None` (unset) keeps unlimited
    /// lines.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::PiplupPatternParams;
    ///
    /// let params = PiplupPatternParams::default().with_max_line_bytes(4096);
    /// assert_eq!(params.max_line_bytes(), Some(4096));
    /// ```
    pub fn with_max_line_bytes(mut self, max_line_bytes: usize) -> Self {
        self.max_line_bytes = Some(max_line_bytes);
        self
    }

    /// Return `PiplupConfig.merge` when set.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::PiplupPatternParams;
    ///
    /// assert!(PiplupPatternParams::default().merge().is_none());
    /// ```
    pub fn merge(&self) -> Option<bool> {
        self.merge
    }

    /// Return `PiplupConfig.max_token_bytes` when set.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::PiplupPatternParams;
    ///
    /// assert!(PiplupPatternParams::default().max_token_bytes().is_none());
    /// ```
    pub fn max_token_bytes(&self) -> Option<usize> {
        self.max_token_bytes
    }

    /// Return `PiplupConfig.max_line_bytes` when set.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::PiplupPatternParams;
    ///
    /// assert!(PiplupPatternParams::default().max_line_bytes().is_none());
    /// ```
    pub fn max_line_bytes(&self) -> Option<usize> {
        self.max_line_bytes
    }
}

/// Drain3-specific facade parameters with exact mappings to `Drain3Config`.
///
/// # Example
///
/// ```rust
/// use log_parser::{Drain3PatternParams, Ratio};
/// use std::num::NonZeroUsize;
///
/// let params = Drain3PatternParams::default()
///     .with_depth(NonZeroUsize::new(4).unwrap())
///     .with_sim_thresh(Ratio::try_from(0.4).unwrap());
/// assert_eq!(params.depth().unwrap().get(), 4);
/// ```
#[derive(Debug, Clone, Default, PartialEq)]
#[non_exhaustive]
pub struct Drain3PatternParams {
    depth: Option<NonZeroUsize>,
    sim_thresh: Option<Ratio>,
    max_children: Option<NonZeroUsize>,
    max_token_bytes: Option<usize>,
    max_line_bytes: Option<usize>,
}

impl Drain3PatternParams {
    /// Set `Drain3Config.depth`.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::Drain3PatternParams;
    /// use std::num::NonZeroUsize;
    ///
    /// let params = Drain3PatternParams::default().with_depth(NonZeroUsize::new(5).unwrap());
    /// assert_eq!(params.depth().unwrap().get(), 5);
    /// ```
    pub fn with_depth(mut self, depth: NonZeroUsize) -> Self {
        self.depth = Some(depth);
        self
    }

    /// Set `Drain3Config.sim_thresh`.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::{Drain3PatternParams, Ratio};
    ///
    /// let params = Drain3PatternParams::default().with_sim_thresh(Ratio::try_from(0.5).unwrap());
    /// assert_eq!(params.sim_thresh().unwrap().as_f64(), 0.5);
    /// ```
    pub fn with_sim_thresh(mut self, sim_thresh: Ratio) -> Self {
        self.sim_thresh = Some(sim_thresh);
        self
    }

    /// Set `Drain3Config.max_children`.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::Drain3PatternParams;
    /// use std::num::NonZeroUsize;
    ///
    /// let params = Drain3PatternParams::default()
    ///     .with_max_children(NonZeroUsize::new(16).unwrap());
    /// assert_eq!(params.max_children().unwrap().get(), 16);
    /// ```
    pub fn with_max_children(mut self, max_children: NonZeroUsize) -> Self {
        self.max_children = Some(max_children);
        self
    }

    /// Set `Drain3Config.max_token_bytes`.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::Drain3PatternParams;
    ///
    /// let params = Drain3PatternParams::default().with_max_token_bytes(512);
    /// assert_eq!(params.max_token_bytes(), Some(512));
    /// ```
    pub fn with_max_token_bytes(mut self, max_token_bytes: usize) -> Self {
        self.max_token_bytes = Some(max_token_bytes);
        self
    }

    /// Set `Drain3Config.max_line_bytes`.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::Drain3PatternParams;
    ///
    /// let params = Drain3PatternParams::default().with_max_line_bytes(4096);
    /// assert_eq!(params.max_line_bytes(), Some(4096));
    /// ```
    pub fn with_max_line_bytes(mut self, max_line_bytes: usize) -> Self {
        self.max_line_bytes = Some(max_line_bytes);
        self
    }

    /// Return `Drain3Config.depth` when set.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::Drain3PatternParams;
    ///
    /// assert!(Drain3PatternParams::default().depth().is_none());
    /// ```
    pub fn depth(&self) -> Option<NonZeroUsize> {
        self.depth
    }

    /// Return `Drain3Config.sim_thresh` when set.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::Drain3PatternParams;
    ///
    /// assert!(Drain3PatternParams::default().sim_thresh().is_none());
    /// ```
    pub fn sim_thresh(&self) -> Option<Ratio> {
        self.sim_thresh
    }

    /// Return `Drain3Config.max_children` when set.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::Drain3PatternParams;
    ///
    /// assert!(Drain3PatternParams::default().max_children().is_none());
    /// ```
    pub fn max_children(&self) -> Option<NonZeroUsize> {
        self.max_children
    }

    /// Return `Drain3Config.max_token_bytes` when set.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::Drain3PatternParams;
    ///
    /// assert!(Drain3PatternParams::default().max_token_bytes().is_none());
    /// ```
    pub fn max_token_bytes(&self) -> Option<usize> {
        self.max_token_bytes
    }

    /// Return `Drain3Config.max_line_bytes` when set.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::Drain3PatternParams;
    ///
    /// assert!(Drain3PatternParams::default().max_line_bytes().is_none());
    /// ```
    pub fn max_line_bytes(&self) -> Option<usize> {
        self.max_line_bytes
    }
}

/// The selected pattern extraction algorithm.
///
/// # Example
///
/// ```rust
/// use log_parser::{Drain3PatternParams, LogPatternAlgorithm};
///
/// let _algorithm = LogPatternAlgorithm::drain3(Drain3PatternParams::default());
/// ```
#[derive(Debug, Clone, PartialEq)]
#[non_exhaustive]
pub enum LogPatternAlgorithm {
    /// Extract patterns with the existing PIPLUP learner.
    Piplup(PiplupPatternParams),
    /// Extract patterns with the existing Drain3 learner.
    Drain3(Drain3PatternParams),
}

impl LogPatternAlgorithm {
    /// Select PIPLUP extraction.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::LogPatternAlgorithm;
    ///
    /// let _algorithm = LogPatternAlgorithm::piplup();
    /// ```
    pub fn piplup() -> Self {
        Self::Piplup(PiplupPatternParams::default())
    }

    /// Select PIPLUP extraction with exact PIPLUP-specific params.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::{LogPatternAlgorithm, PiplupPatternParams};
    ///
    /// let _algorithm = LogPatternAlgorithm::piplup_with(PiplupPatternParams::default());
    /// ```
    pub fn piplup_with(params: PiplupPatternParams) -> Self {
        Self::Piplup(params)
    }

    /// Select Drain3 extraction with exact Drain3-specific params.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::{Drain3PatternParams, LogPatternAlgorithm};
    ///
    /// let _algorithm = LogPatternAlgorithm::drain3(Drain3PatternParams::default());
    /// ```
    pub fn drain3(params: Drain3PatternParams) -> Self {
        Self::Drain3(params)
    }
}

/// String tag used by the JSON/FFI request DTO.
///
/// # Example
///
/// ```rust
/// use log_parser::LogPatternAlgorithmTag;
///
/// let json = serde_json::to_string(&LogPatternAlgorithmTag::Drain3).unwrap();
/// assert_eq!(json, "\"drain3\"");
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
#[non_exhaustive]
pub enum LogPatternAlgorithmTag {
    /// PIPLUP algorithm.
    Piplup,
    /// Drain3 algorithm.
    Drain3,
}

/// Variable kind for one wildcard in a rendered pattern.
///
/// Re-exported at the crate root; the `log_parser::patterns` path remains
/// valid. `Ip` and `Datetime` are reserved for forward compatibility: they are
/// accepted on deserialization but are never produced by the current
/// classifier (ADR-0043 §4).
///
/// # Example
///
/// ```rust
/// use log_parser::LogPatternVariableKind;
///
/// assert_eq!(LogPatternVariableKind::Unknown.to_string(), "<*>");
/// assert_eq!(serde_json::to_string(&LogPatternVariableKind::Ipv4).unwrap(), "\"<IPv4>\"");
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
#[non_exhaustive]
pub enum LogPatternVariableKind {
    /// Unknown wildcard kind.
    Unknown,
    /// Signed integer or decimal number.
    Number,
    /// Hexadecimal literal.
    Hex,
    /// Long identifier.
    Id,
    /// URL.
    Url,
    /// Filesystem path.
    Path,
    /// IP address without version specificity.
    Ip,
    /// IPv4 address.
    Ipv4,
    /// IPv6 address.
    Ipv6,
    /// MAC address.
    Mac,
    /// Date.
    Date,
    /// Time.
    Time,
    /// Datetime.
    Datetime,
    /// Duration with a unit.
    Duration,
    /// Size with a byte unit.
    Size,
}

impl LogPatternVariableKind {
    /// Return the stable string representation.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::patterns::LogPatternVariableKind;
    ///
    /// assert_eq!(LogPatternVariableKind::Url.as_str(), "<URL>");
    /// ```
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Unknown => "<*>",
            Self::Number => "<Number>",
            Self::Hex => "<Hex>",
            Self::Id => "<Id>",
            Self::Url => "<URL>",
            Self::Path => "<Path>",
            Self::Ip => "<IP>",
            Self::Ipv4 => "<IPv4>",
            Self::Ipv6 => "<IPv6>",
            Self::Mac => "<MAC>",
            Self::Date => "<Date>",
            Self::Time => "<Time>",
            Self::Datetime => "<Datetime>",
            Self::Duration => "<Duration>",
            Self::Size => "<Size>",
        }
    }
}

impl fmt::Display for LogPatternVariableKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

impl TryFrom<&str> for LogPatternVariableKind {
    type Error = LogParseError;

    fn try_from(value: &str) -> Result<Self, Self::Error> {
        match value {
            "<*>" => Ok(Self::Unknown),
            "<Number>" => Ok(Self::Number),
            "<Hex>" => Ok(Self::Hex),
            "<Id>" => Ok(Self::Id),
            "<URL>" => Ok(Self::Url),
            "<Path>" => Ok(Self::Path),
            "<IP>" => Ok(Self::Ip),
            "<IPv4>" => Ok(Self::Ipv4),
            "<IPv6>" => Ok(Self::Ipv6),
            "<MAC>" => Ok(Self::Mac),
            "<Date>" => Ok(Self::Date),
            "<Time>" => Ok(Self::Time),
            "<Datetime>" => Ok(Self::Datetime),
            "<Duration>" => Ok(Self::Duration),
            "<Size>" => Ok(Self::Size),
            other => Err(LogParseError::InvalidConfig {
                field: "variables",
                message: format!("unknown variable kind {other:?}"),
            }),
        }
    }
}

impl Serialize for LogPatternVariableKind {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        serializer.serialize_str(self.as_str())
    }
}

impl<'de> Deserialize<'de> for LogPatternVariableKind {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        let value = String::deserialize(deserializer)?;
        Self::try_from(value.as_str()).map_err(de::Error::custom)
    }
}

/// Template-oriented pattern extraction result.
///
/// # Example
///
/// ```rust
/// use log_parser::LogPatternBuilder;
///
/// let patterns = LogPatternBuilder::piplup().extract(["a", "b"]).unwrap();
/// assert_eq!(patterns.row_count(), 2);
/// assert!(!patterns.is_empty());
/// ```
#[derive(Debug, Clone, PartialEq)]
pub struct LogPatterns {
    templates: Vec<LogTemplate>,
    assignments: Vec<TemplateId>,
}

impl LogPatterns {
    /// Return all deduplicated templates.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::LogPatternBuilder;
    ///
    /// let patterns = LogPatternBuilder::piplup().extract(["a", "a"]).unwrap();
    /// assert_eq!(patterns.templates().len(), 1);
    /// ```
    pub fn templates(&self) -> &[LogTemplate] {
        &self.templates
    }

    /// Return all row template IDs (index is zero-based original input position).
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::LogPatternBuilder;
    ///
    /// let patterns = LogPatternBuilder::piplup().extract(["a"]).unwrap();
    /// assert_eq!(patterns.assignments().len(), 1);
    /// ```
    pub fn assignments(&self) -> &[TemplateId] {
        &self.assignments
    }

    /// Look up a template by ID.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::LogPatternBuilder;
    ///
    /// let patterns = LogPatternBuilder::piplup().extract(["a"]).unwrap();
    /// let id = patterns.assignments()[0];
    /// assert!(patterns.template(id).is_some());
    /// ```
    pub fn template(&self, id: TemplateId) -> Option<&LogTemplate> {
        self.templates.get(id.0)
    }

    /// Return the template for a specific row.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::LogPatternBuilder;
    ///
    /// let patterns = LogPatternBuilder::piplup().extract(["a"]).unwrap();
    /// assert!(patterns.template_for_row(0).is_some());
    /// assert!(patterns.template_for_row(1).is_none());
    /// ```
    pub fn template_for_row(&self, row: usize) -> Option<&LogTemplate> {
        let id = self.assignments.get(row)?;
        self.template(*id)
    }

    /// Return the number of unique templates.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::LogPatternBuilder;
    ///
    /// let patterns = LogPatternBuilder::piplup().extract(["a", "a"]).unwrap();
    /// assert_eq!(patterns.template_count(), 1);
    /// ```
    pub fn template_count(&self) -> usize {
        self.templates.len()
    }

    /// Return the template ID for a specific row.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::LogPatternBuilder;
    ///
    /// let patterns = LogPatternBuilder::piplup().extract(["a"]).unwrap();
    /// assert!(patterns.assignment(0).is_some());
    /// assert!(patterns.assignment(1).is_none());
    /// ```
    pub fn assignment(&self, row: usize) -> Option<TemplateId> {
        self.assignments.get(row).copied()
    }

    /// Return the number of rows.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::LogPatternBuilder;
    ///
    /// assert_eq!(LogPatternBuilder::piplup().extract(["a", "b"]).unwrap().row_count(), 2);
    /// ```
    pub fn row_count(&self) -> usize {
        self.assignments.len()
    }

    /// Return true when there are no rows.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::LogPatternBuilder;
    ///
    /// assert!(LogPatternBuilder::piplup().extract(std::iter::empty::<&str>()).unwrap().is_empty());
    /// ```
    pub fn is_empty(&self) -> bool {
        self.assignments.is_empty()
    }

    /// Iterate over rows, returning template ID and reference.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::LogPatternBuilder;
    ///
    /// let patterns = LogPatternBuilder::piplup().extract(["a", "b"]).unwrap();
    /// for row in patterns.iter_rows() {
    ///     assert!(row.row_index < patterns.row_count());
    ///     assert!(!row.template.pattern().is_empty());
    /// }
    /// ```
    pub fn iter_rows(&self) -> impl ExactSizeIterator<Item = RowPatternRef<'_>> + '_ {
        self.assignments
            .iter()
            .enumerate()
            .map(move |(index, &id)| RowPatternRef {
                row_index: index,
                template_id: id,
                template: &self.templates[id.0],
            })
    }

    /// Iterate over templates with their IDs.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::LogPatternBuilder;
    ///
    /// let patterns = LogPatternBuilder::piplup().extract(["a"]).unwrap();
    /// for (id, template) in patterns.iter_templates() {
    ///     assert_eq!(id.index(), 0);
    /// }
    /// ```
    pub fn iter_templates(&self) -> impl ExactSizeIterator<Item = (TemplateId, &LogTemplate)> + '_ {
        self.templates
            .iter()
            .enumerate()
            .map(|(i, t)| (TemplateId(i), t))
    }

    /// Scan assignments and return row indices belonging to the specified template.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::LogPatternBuilder;
    ///
    /// let patterns = LogPatternBuilder::piplup().extract(["a", "b", "a"]).unwrap();
    /// let id = patterns.assignments()[0];
    /// let rows: Vec<_> = patterns.rows_for(id).collect();
    /// assert_eq!(rows, vec![0, 2]);
    /// ```
    pub fn rows_for(&self, id: TemplateId) -> impl Iterator<Item = usize> + '_ {
        self.assignments
            .iter()
            .enumerate()
            .filter_map(move |(index, &row_id)| if row_id == id { Some(index) } else { None })
    }

    /// Construct from raw parts (used internally).
    fn from_parts(templates: Vec<LogTemplate>, assignments: Vec<TemplateId>) -> Self {
        Self {
            templates,
            assignments,
        }
    }
}

/// JSON/FFI compatibility request DTO.
///
/// Rust callers should prefer [`LogPatternBuilder`].
///
/// # Example
///
/// ```rust
/// use log_parser::GetLogPatternsRequest;
///
/// let request: GetLogPatternsRequest = serde_json::from_value(serde_json::json!({
///     "content": ["worker started"],
///     "algorithm": "piplup"
/// })).unwrap();
/// let (content, _builder) = request.try_into_parts().unwrap();
/// assert_eq!(content, vec!["worker started"]);
/// ```
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[non_exhaustive]
pub struct GetLogPatternsRequest {
    /// Input content rows.
    pub content: Vec<String>,
    /// Custom delimiters; honored by the Drain3 algorithm, rejected as
    /// unsupported by PIPLUP (ADR-0042).
    #[serde(default)]
    pub delimiters: Vec<String>,
    /// Custom separators; honored by the PIPLUP algorithm as
    /// whitespace-equivalent boundaries, rejected as unsupported by Drain3
    /// (ADR-0046).
    #[serde(default)]
    pub separators: Vec<String>,
    /// Optional labels. Empty means global parser state.
    #[serde(default)]
    pub labels: Vec<String>,
    /// Preferred top-level algorithm selector.
    #[serde(default)]
    pub algorithm: Option<LogPatternAlgorithmTag>,
    /// Regexp rendering dialect for the `regexp` field of each template
    /// (RFC-0025, ADR-0044). Defaults to `rust` when absent.
    #[serde(default)]
    pub regexp_dialect: Option<RegexpDialect>,
    /// JSON parameter bag for compatibility with non-Rust callers.
    #[serde(default)]
    pub params: Map<String, Value>,
}

impl GetLogPatternsRequest {
    /// Convert this DTO into content plus the Rust-native builder.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::GetLogPatternsRequest;
    ///
    /// let request: GetLogPatternsRequest = serde_json::from_value(serde_json::json!({
    ///     "content": ["worker started"],
    ///     "params": { "typedPlaceholders": true }
    /// })).unwrap();
    /// let (content, builder) = request.try_into_parts().unwrap();
    /// assert_eq!(builder.extract(content).unwrap().row_count(), 1);
    /// ```
    pub fn try_into_parts(self) -> Result<(Vec<String>, LogPatternBuilder), LogParseError> {
        let Self {
            content,
            delimiters,
            separators,
            labels,
            algorithm,
            regexp_dialect,
            params,
        } = self;

        validate_label_len(labels.len(), content.len())?;
        validate_delimiter_strings(&delimiters)?;
        validate_separator_strings(&separators)?;

        let legacy_algorithm = parse_legacy_algorithm(&params)?;
        let algorithm_tag = resolve_algorithm_tag(algorithm, legacy_algorithm)?;
        let parsed = parse_dto_params(&params, algorithm_tag)?;

        let algorithm = match algorithm_tag {
            LogPatternAlgorithmTag::Piplup => LogPatternAlgorithm::piplup_with(parsed.piplup),
            LogPatternAlgorithmTag::Drain3 => LogPatternAlgorithm::drain3(parsed.drain3),
        };
        let builder = LogPatternBuilder {
            algorithm,
            common: parsed.common,
            delimiters,
            separators,
            labels: Some(labels),
            preprocess: parsed.preprocess.unwrap_or(true),
            typed_placeholders: parsed.typed_placeholders.unwrap_or(false),
            regexp_dialect: regexp_dialect.unwrap_or_default(),
        };

        Ok((content, builder))
    }
}

/// Template DTO for JSON response.
///
/// # Example
///
/// ```rust
/// use log_parser::LogTemplateDto;
///
/// let json = r#"{"pattern":"worker <*>","regexp":"(?s)^worker\\s+(.*?)$","occurrences":5}"#;
/// let dto: LogTemplateDto = serde_json::from_str(json).unwrap();
/// assert_eq!(dto.pattern, "worker <*>");
/// assert_eq!(dto.occurrences, 5);
/// // An absent `variables` field deserializes to an empty array.
/// assert!(dto.variables.is_empty());
/// ```
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[non_exhaustive]
pub struct LogTemplateDto {
    /// Rendered template pattern.
    pub pattern: String,
    /// Regex generated from the pattern.
    pub regexp: String,
    /// Number of rows matched by this template.
    pub occurrences: usize,
    /// Variable kind of each `<*>` in `pattern`, counted left to right.
    ///
    /// Always populated on serialize. On deserialize an absent field defaults
    /// to an empty array, documented as "absent means unspecified" — the DTO
    /// layer does not invent data (ADR-0043 §5).
    #[serde(default)]
    pub variables: Vec<LogPatternVariableKind>,
}

/// JSON/FFI compatibility response DTO.
///
/// This is a lossy projection of `LogPatterns` — it only contains the
/// template summary, not row assignments.
///
/// # Example
///
/// ```rust
/// use log_parser::{GetLogPatternsResponse, LogPatternBuilder};
///
/// let patterns = LogPatternBuilder::piplup().extract(["worker started"]).unwrap();
/// let response = GetLogPatternsResponse::from(patterns);
/// assert_eq!(response.templates.len(), 1);
/// assert_eq!(response.templates[0].pattern, "worker started");
/// ```
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[non_exhaustive]
pub struct GetLogPatternsResponse {
    /// Deduplicated templates with occurrence counts.
    pub templates: Vec<LogTemplateDto>,
}

impl From<LogPatterns> for GetLogPatternsResponse {
    fn from(patterns: LogPatterns) -> Self {
        Self {
            templates: patterns
                .templates
                .into_iter()
                .map(|t| LogTemplateDto {
                    pattern: t.pattern,
                    regexp: t.regexp,
                    occurrences: t.occurrences,
                    variables: t.variables,
                })
                .collect(),
        }
    }
}

/// Target dialect for rendering a template's [`LogTemplate::regexp`].
///
/// Controls how ASCII literal characters inside the rendered regex are folded
/// for case-insensitive matching. The default [`RegexpDialect::Rust`] matches
/// the historical behavior exactly; [`RegexpDialect::Re2`] trades the
/// Rust-`regex`-private `(?i-u:...)` local-flag group for an engine-agnostic
/// character-class expansion (`G` -> `[Gg]`) that parses in RE2, Python `re`,
/// Java `Pattern`, JavaScript, and PCRE2 (RFC-0025, ADR-0044).
///
/// The dialect only affects the rendered string; the template's `pattern` and
/// other fields are identical across dialects.
///
/// # Example
///
/// ```rust
/// use log_parser::{LogPatternBuilder, RegexpDialect};
///
/// let patterns = LogPatternBuilder::piplup()
///     .regexp_dialect(RegexpDialect::Re2)
///     .extract(["GET /done"])
///     .unwrap();
/// // Re2 output has no `(?i-u:...)` groups.
/// assert!(!patterns.templates()[0].regexp().contains("(?i-u:"));
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
#[non_exhaustive]
pub enum RegexpDialect {
    /// Rust `regex` crate native syntax.
    ///
    /// Uses the `(?i-u:...)` local-flag group for case-insensitive matching
    /// with Unicode folding disabled. Only parseable by the Rust `regex`
    /// engine.
    #[default]
    Rust,

    /// RE2 / Go `regexp` compatible subset.
    ///
    /// ASCII literal characters expand to case character classes
    /// (e.g. `G` -> `[Gg]`) instead of a local-flag group. Parseable by
    /// Python `re`, Java `Pattern`, JavaScript, PCRE2, Lucene, and other
    /// mainstream engines. Non-ASCII letters are emitted verbatim with no
    /// case folding (an intentional narrowing; see ADR-0044).
    Re2,
}

/// One-shot content-level log pattern extraction builder.
///
/// # Example
///
/// ```rust
/// use log_parser::LogPatternBuilder;
///
/// let content = [
///     "worker alpha finished",
///     "worker beta finished",
/// ];
/// let patterns = LogPatternBuilder::piplup().extract(content).unwrap();
/// assert_eq!(patterns.row_count(), 2);
/// ```
#[derive(Debug, Clone)]
#[must_use]
pub struct LogPatternBuilder {
    algorithm: LogPatternAlgorithm,
    common: CommonPatternParams,
    delimiters: Vec<String>,
    separators: Vec<String>,
    labels: Option<Vec<String>>,
    preprocess: bool,
    typed_placeholders: bool,
    regexp_dialect: RegexpDialect,
}

impl LogPatternBuilder {
    /// Build a PIPLUP-backed pattern request.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::LogPatternBuilder;
    ///
    /// let patterns = LogPatternBuilder::piplup().extract(["worker started"]).unwrap();
    /// assert_eq!(patterns.row_count(), 1);
    /// ```
    pub fn piplup() -> Self {
        Self::piplup_with(PiplupPatternParams::default())
    }

    /// Build a PIPLUP-backed pattern request with exact PIPLUP-specific params.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::{LogPatternBuilder, PiplupPatternParams};
    ///
    /// let patterns = LogPatternBuilder::piplup_with(PiplupPatternParams::default())
    ///     .extract(["worker started"])
    ///     .unwrap();
    /// assert_eq!(patterns.row_count(), 1);
    /// ```
    pub fn piplup_with(params: PiplupPatternParams) -> Self {
        Self {
            algorithm: LogPatternAlgorithm::piplup_with(params),
            common: CommonPatternParams::default(),
            delimiters: Vec::new(),
            separators: Vec::new(),
            labels: None,
            preprocess: true,
            typed_placeholders: false,
            regexp_dialect: RegexpDialect::default(),
        }
    }

    /// Build a Drain3-backed pattern request.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::{Drain3PatternParams, LogPatternBuilder};
    ///
    /// let patterns = LogPatternBuilder::drain3(Drain3PatternParams::default())
    ///     .extract(["worker started"])
    ///     .unwrap();
    /// assert_eq!(patterns.row_count(), 1);
    /// ```
    pub fn drain3(params: Drain3PatternParams) -> Self {
        Self {
            algorithm: LogPatternAlgorithm::drain3(params),
            common: CommonPatternParams::default(),
            delimiters: Vec::new(),
            separators: Vec::new(),
            labels: None,
            preprocess: true,
            typed_placeholders: false,
            regexp_dialect: RegexpDialect::default(),
        }
    }

    /// Set raw delimiter strings.
    ///
    /// ADR-0042: non-empty delimiter lists are honored by the Drain3
    /// algorithm; with the PIPLUP algorithm [`extract`](Self::extract)
    /// rejects them with [`LogParseError::Unsupported`].
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::{LogParseError, LogPatternBuilder};
    ///
    /// let err = LogPatternBuilder::piplup()
    ///     .delimiters([","])
    ///     .extract(["a,b"])
    ///     .unwrap_err();
    /// assert!(matches!(err, LogParseError::Unsupported(message) if message.contains("piplup")));
    /// ```
    ///
    /// ```rust
    /// use log_parser::{Drain3PatternParams, LogPatternBuilder};
    ///
    /// let patterns = LogPatternBuilder::drain3(Drain3PatternParams::default())
    ///     .delimiters([","])
    ///     .extract(["a,b"])
    ///     .unwrap();
    /// assert_eq!(patterns.row_count(), 1);
    /// ```
    pub fn delimiters<I, S>(mut self, delimiters: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.delimiters = delimiters.into_iter().map(Into::into).collect();
        self
    }

    /// Set raw separator strings (ADR-0046).
    ///
    /// Separators are caller-declared whitespace-equivalent boundaries for
    /// the PIPLUP algorithm: after preprocessing, each matched separator is
    /// replaced with one space and dropped by tokenization, so
    /// comma-separated (including full-width `，`) logs cluster exactly like
    /// their space-written equivalents, and the generated regexp matches the
    /// original separator-bearing content. The Drain3 algorithm rejects
    /// non-empty separator lists with [`LogParseError::Unsupported`] — use
    /// `delimiters` there.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::LogPatternBuilder;
    ///
    /// let content = [
    ///     "upload failed,retry 3 times,timeout 5000 ms",
    ///     "upload failed,retry 7 times,timeout 3000 ms",
    /// ];
    ///
    /// let patterns = LogPatternBuilder::piplup()
    ///     .separators([","])
    ///     .extract(content)
    ///     .unwrap();
    /// assert_eq!(patterns.template_count(), 1);
    /// assert_eq!(
    ///     patterns.templates()[0].pattern(),
    ///     "upload failed retry <*> times timeout <*>"
    /// );
    /// ```
    pub fn separators<I, S>(mut self, separators: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.separators = separators.into_iter().map(Into::into).collect();
        self
    }

    /// Set labels for label-local parser states.
    ///
    /// Empty labels mean one global parser state; otherwise the label count must
    /// equal the content count at extraction time.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::LogPatternBuilder;
    ///
    /// let patterns = LogPatternBuilder::piplup()
    ///     .labels(["api", "api"])
    ///     .extract(["GET /a", "GET /b"])
    ///     .unwrap();
    /// assert_eq!(patterns.row_count(), 2);
    /// ```
    pub fn labels<I, S>(mut self, labels: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.labels = Some(labels.into_iter().map(Into::into).collect());
        self
    }

    /// Clear labels and use one global parser state.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::LogPatternBuilder;
    ///
    /// let patterns = LogPatternBuilder::piplup()
    ///     .labels(["ignored"])
    ///     .without_labels()
    ///     .extract(["worker started"])
    ///     .unwrap();
    /// assert_eq!(patterns.row_count(), 1);
    /// ```
    pub fn without_labels(mut self) -> Self {
        self.labels = None;
        self
    }

    /// Enable or disable existing parser preprocessing.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::LogPatternBuilder;
    ///
    /// let patterns = LogPatternBuilder::piplup()
    ///     .preprocess(false)
    ///     .extract(["worker started"])
    ///     .unwrap();
    /// assert_eq!(patterns.row_count(), 1);
    /// ```
    pub fn preprocess(mut self, enabled: bool) -> Self {
        self.preprocess = enabled;
        self
    }

    /// Enable or disable existing typed-placeholder mode.
    ///
    /// When enabled, each template's [`LogTemplate::variables`] carries the
    /// derived kind of every `<*>` (ADR-0043); when disabled (the default)
    /// every variable is [`LogPatternVariableKind::Unknown`]. Either way the
    /// length invariant `variables().len() == pattern().matches("<*>").count()`
    /// holds.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::{LogPatternBuilder, LogPatternVariableKind};
    ///
    /// let patterns = LogPatternBuilder::piplup()
    ///     .typed_placeholders(true)
    ///     .extract(["worker 42 done", "worker 43 done"])
    ///     .unwrap();
    /// let template = &patterns.templates()[0];
    /// assert_eq!(template.variables(), &[LogPatternVariableKind::Number]);
    /// ```
    pub fn typed_placeholders(mut self, enabled: bool) -> Self {
        self.typed_placeholders = enabled;
        self
    }

    /// Set the regexp rendering dialect (RFC-0025, ADR-0044).
    ///
    /// The default [`RegexpDialect::Rust`] emits Rust-`regex`-native
    /// `(?i-u:...)` local-flag groups and is unchanged from historical
    /// behavior. [`RegexpDialect::Re2`] emits engine-agnostic character-class
    /// expansion so the rendered [`LogTemplate::regexp`] parses in RE2,
    /// Python `re`, Java `Pattern`, JavaScript, and PCRE2. The dialect only
    /// affects the rendered string; `pattern`, `occurrences`, and `variables`
    /// are identical across dialects.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::{LogPatternBuilder, RegexpDialect};
    ///
    /// let patterns = LogPatternBuilder::piplup()
    ///     .regexp_dialect(RegexpDialect::Re2)
    ///     .extract(["GET /api/users/123 HTTP/1.1"])
    ///     .unwrap();
    /// let regexp = patterns.templates()[0].regexp();
    /// assert!(regexp.contains("[Gg][Ee][Tt]"));
    /// assert!(!regexp.contains("(?i-u:"));
    /// ```
    pub fn regexp_dialect(mut self, dialect: RegexpDialect) -> Self {
        self.regexp_dialect = dialect;
        self
    }

    /// Set common facade params.
    ///
    /// Set common params currently return a field-specific unsupported semantic
    /// error during extraction rather than being approximated to parser knobs.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::{CommonPatternParams, LogParseError, LogPatternBuilder, Ratio};
    ///
    /// let err = LogPatternBuilder::piplup()
    ///     .common_params(CommonPatternParams::default().with_tolerance(Ratio::try_from(0.1).unwrap()))
    ///     .extract(["worker started"])
    ///     .unwrap_err();
    /// assert!(matches!(err, LogParseError::InvalidConfig { field: "tolerance", .. }));
    /// ```
    pub fn common_params(mut self, params: CommonPatternParams) -> Self {
        self.common = params;
        self
    }

    /// Extract template-oriented patterns from content strings.
    ///
    /// Each iterator element is one record, so LF and CRLF inside an element do
    /// not split it. For non-empty content after [`str::trim`], the generated
    /// regexp matches that trimmed value, including internal LF or CRLF.
    /// Leading and trailing whitespace retain the parser's existing trim
    /// behavior. Content that trims to empty retains each algorithm's existing
    /// no-token behavior and is outside the multiline matching guarantee.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::LogPatternBuilder;
    ///
    /// let content = vec!["worker alpha done", "worker beta done"];
    /// let patterns = LogPatternBuilder::piplup().extract(&content).unwrap();
    /// assert_eq!(patterns.row_count(), content.len());
    /// ```
    pub fn extract<I, S>(self, content: I) -> Result<LogPatterns, LogParseError>
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        let content = content
            .into_iter()
            .map(|line| line.as_ref().to_string())
            .collect::<Vec<_>>();
        let resolved = ResolvedPatternRequest::try_from_builder(self, content.len())?;

        if content.is_empty() {
            return Ok(LogPatterns::from_parts(Vec::new(), Vec::new()));
        }

        let groups = group_indices(&content, resolved.labels.as_deref());
        let mut row_patterns: Vec<Option<String>> = vec![None; content.len()];
        let mut slot_evidence = std::collections::BTreeMap::<String, Vec<MaskEvidence>>::new();

        for group in groups {
            let entries = group
                .indices
                .iter()
                .map(|&index| LogEntry {
                    line_number: index + 1,
                    raw: content[index].clone(),
                })
                .collect::<Vec<_>>();
            let training = extract_group(&resolved, &entries)?;

            if resolved.typed_placeholders {
                // ADR-0043 §2: Drain3 requests with custom delimiters must
                // replay the training tokenization; everything else uses
                // whitespace tokenization for both algorithms (PIPLUP
                // requests with separators replay the ADR-0046
                // normalization).
                let group_evidence = build_placeholder_evidence(
                    &training.parse_output,
                    &training.preprocess_plan,
                    resolved.preprocess,
                    |processed| {
                        if !resolved.delimiters.is_empty() {
                            crate::drain3::token::split_tokens(
                                processed,
                                false,
                                &resolved.delimiters,
                            )
                        } else {
                            tokenize_index_content(processed, &resolved.separators)
                        }
                    },
                );
                merge_group_evidence(&mut slot_evidence, group_evidence);
            }

            for line in training.parse_output.line_results {
                let original_index =
                    line.line_number
                        .checked_sub(1)
                        .ok_or_else(|| LogParseError::ParseError {
                            line: line.line_number,
                            message: "parser returned line number 0".to_string(),
                        })?;
                if original_index >= content.len() {
                    return Err(LogParseError::ParseError {
                        line: line.line_number,
                        message: "parser returned a line outside the content input".to_string(),
                    });
                }
                row_patterns[original_index] = Some(line.event_template);
            }
        }

        build_log_patterns(
            row_patterns,
            &resolved.delimiters,
            &resolved.separators,
            resolved.regexp_dialect,
            &slot_evidence,
        )
    }
}

/// Merge one label group's slot evidence into the shared map.
///
/// Keys are normalized template strings so evidence from independent parser
/// states joins under the same dedup key used by [`build_log_patterns`]. The
/// per-slot join is commutative and associative (`Unseen` identity, `Untyped`
/// absorbing), so the result is independent of group order (ADR-0043 §1).
fn merge_group_evidence(
    merged: &mut std::collections::BTreeMap<String, Vec<MaskEvidence>>,
    group: std::collections::BTreeMap<String, Vec<MaskEvidence>>,
) {
    for (template, slots) in group {
        let normalized = normalize_pattern(&template);
        let entry = merged
            .entry(normalized)
            .or_insert_with(|| vec![MaskEvidence::Unseen; slots.len()]);
        if entry.len() < slots.len() {
            entry.resize(slots.len(), MaskEvidence::Unseen);
        }
        for (index, slot) in slots.into_iter().enumerate() {
            entry[index] = join_mask_evidence(entry[index], slot);
        }
    }
}

fn build_log_patterns(
    row_patterns: Vec<Option<String>>,
    delimiters: &[String],
    separators: &[String],
    dialect: RegexpDialect,
    slot_evidence: &std::collections::BTreeMap<String, Vec<MaskEvidence>>,
) -> Result<LogPatterns, LogParseError> {
    let mut template_map: std::collections::HashMap<String, usize> =
        std::collections::HashMap::new();
    let mut templates: Vec<LogTemplate> = Vec::new();
    let mut assignments: Vec<TemplateId> = Vec::with_capacity(row_patterns.len());

    for (index, pattern) in row_patterns.into_iter().enumerate() {
        let pattern = pattern.ok_or_else(|| LogParseError::ParseError {
            line: index + 1,
            message: "pattern extraction did not assign an output row".to_string(),
        })?;

        let normalized = normalize_pattern(&pattern);

        let template_index = if let Some(&existing_index) = template_map.get(&normalized) {
            templates[existing_index].occurrences += 1;
            existing_index
        } else {
            let new_index = templates.len();
            template_map.insert(normalized.clone(), new_index);

            let regexp = pattern_to_regexp(&normalized, delimiters, separators, dialect);
            let variables = derive_variables(
                &normalized,
                slot_evidence.get(&normalized).map(Vec::as_slice),
            );

            templates.push(LogTemplate {
                pattern: normalized,
                regexp,
                occurrences: 1,
                variables,
            });
            new_index
        };

        assignments.push(TemplateId(template_index));
    }

    Ok(LogPatterns::from_parts(templates, assignments))
}

/// Expand per-token slot evidence into one variable kind per `<*>` in the
/// normalized pattern (ADR-0043 §3).
///
/// A `TemplateToken::Wildcard` carries exactly one `<*>` and maps to its slot
/// kind, with `Unseen`/`Untyped` degrading to `Unknown`. A literal token can
/// still render `<*>` markers when one token carries multiple wildcards (a
/// merge artifact); without 1:1 token alignment every marker in such a token
/// degrades to `Unknown`. A missing evidence entry (typed mode disabled or no
/// usable observation) yields `Unknown` for every slot, so the invariant
/// `variables.len() == pattern.matches("<*>").count()` holds regardless.
fn derive_variables(
    normalized_pattern: &str,
    evidence: Option<&[MaskEvidence]>,
) -> Vec<LogPatternVariableKind> {
    let compiled = CompiledTemplate::compile(normalized_pattern);
    let mut variables = Vec::with_capacity(normalized_pattern.matches("<*>").count());
    for (index, token) in compiled.tokens().iter().enumerate() {
        match token {
            TemplateToken::Wildcard { .. } => {
                let kind = evidence.and_then(|slots| slots.get(index)).map_or(
                    LogPatternVariableKind::Unknown,
                    |slot| match slot {
                        MaskEvidence::Seen(kind) => variable_kind(*kind),
                        MaskEvidence::Unseen | MaskEvidence::Untyped => {
                            LogPatternVariableKind::Unknown
                        }
                    },
                );
                variables.push(kind);
            }
            TemplateToken::Literal(literal) => {
                for _ in 0..literal.matches("<*>").count() {
                    variables.push(LogPatternVariableKind::Unknown);
                }
            }
        }
    }
    variables
}

/// Map an internal placeholder kind to the public facade kind (ADR-0043 §4).
///
/// One-to-one over the twelve classifier kinds; the reserved
/// `LogPatternVariableKind::{Ip, Datetime}` variants are never produced.
fn variable_kind(kind: PlaceholderKind) -> LogPatternVariableKind {
    match kind {
        PlaceholderKind::Number => LogPatternVariableKind::Number,
        PlaceholderKind::Hex => LogPatternVariableKind::Hex,
        PlaceholderKind::Id => LogPatternVariableKind::Id,
        PlaceholderKind::Url => LogPatternVariableKind::Url,
        PlaceholderKind::Path => LogPatternVariableKind::Path,
        PlaceholderKind::Ipv4 => LogPatternVariableKind::Ipv4,
        PlaceholderKind::Ipv6 => LogPatternVariableKind::Ipv6,
        PlaceholderKind::Mac => LogPatternVariableKind::Mac,
        PlaceholderKind::Date => LogPatternVariableKind::Date,
        PlaceholderKind::Time => LogPatternVariableKind::Time,
        PlaceholderKind::Duration => LogPatternVariableKind::Duration,
        PlaceholderKind::Size => LogPatternVariableKind::Size,
    }
}

#[derive(Debug)]
struct ResolvedPatternRequest {
    algorithm: LogPatternAlgorithm,
    delimiters: Vec<String>,
    separators: Vec<String>,
    labels: Option<Vec<String>>,
    preprocess: bool,
    typed_placeholders: bool,
    regexp_dialect: RegexpDialect,
}

impl ResolvedPatternRequest {
    fn try_from_builder(
        builder: LogPatternBuilder,
        content_len: usize,
    ) -> Result<Self, LogParseError> {
        let LogPatternBuilder {
            algorithm,
            common,
            delimiters,
            separators,
            labels,
            preprocess,
            typed_placeholders,
            regexp_dialect,
        } = builder;

        validate_label_option(&labels, content_len)?;
        validate_delimiter_strings(&delimiters)?;
        validate_separator_strings(&separators)?;
        reject_common_params(&common)?;
        // ADR-0042: custom delimiters are honored for Drain3 only; PIPLUP
        // keeps rejecting non-empty lists (ADR-0046: the message points
        // callers at `separators`).
        if !delimiters.is_empty() && matches!(algorithm, LogPatternAlgorithm::Piplup(_)) {
            return Err(LogParseError::Unsupported(
                CUSTOM_DELIMITERS_UNSUPPORTED_PIPLUP,
            ));
        }
        // ADR-0046: custom separators are honored for PIPLUP only; Drain3
        // keeps rejecting non-empty lists — it already has `delimiters`.
        if !separators.is_empty() && matches!(algorithm, LogPatternAlgorithm::Drain3(_)) {
            return Err(LogParseError::Unsupported(
                CUSTOM_SEPARATORS_UNSUPPORTED_DRAIN3,
            ));
        }

        let labels = labels.and_then(|labels| (!labels.is_empty()).then_some(labels));
        Ok(Self {
            algorithm,
            delimiters,
            separators,
            labels,
            preprocess,
            typed_placeholders,
            regexp_dialect,
        })
    }
}

#[derive(Debug)]
struct LabelGroup {
    indices: Vec<usize>,
}

fn group_indices(content: &[String], labels: Option<&[String]>) -> Vec<LabelGroup> {
    let Some(labels) = labels else {
        return vec![LabelGroup {
            indices: (0..content.len()).collect(),
        }];
    };

    let mut groups: Vec<(String, Vec<usize>)> = Vec::new();
    for (index, label) in labels.iter().enumerate() {
        if let Some((_, indices)) = groups.iter_mut().find(|(existing, _)| existing == label) {
            indices.push(index);
        } else {
            groups.push((label.clone(), vec![index]));
        }
    }

    groups
        .into_iter()
        .map(|(_, indices)| LabelGroup { indices })
        .collect()
}

fn extract_group(
    resolved: &ResolvedPatternRequest,
    entries: &[LogEntry],
) -> Result<TrainingOutput, LogParseError> {
    match &resolved.algorithm {
        LogPatternAlgorithm::Piplup(params) => {
            let mut config = PiplupConfig {
                log_format: vec![SYNTHETIC_CONTENT_FORMAT.to_string()],
                preprocess: resolved.preprocess,
                typed_placeholders: resolved.typed_placeholders,
                ..PiplupConfig::default()
            };
            if let Some(hit_limit) = params.hit_limit {
                config.hit_limit = hit_limit.get();
            }
            if let Some(br_thresh) = params.br_thresh {
                config.br_thresh = br_thresh.get();
            }
            if let Some(sim_thresh) = params.sim_thresh {
                config.sim_thresh = SimThresh::Fixed(sim_thresh.as_f64());
            }
            if let Some(merge) = params.merge {
                config.merge = merge;
            }
            config.max_token_bytes = params.max_token_bytes;
            config.max_line_bytes = params.max_line_bytes;
            // ADR-0046: separators are caller-declared whitespace-equivalent
            // boundaries honored by PIPLUP.
            config.separators = resolved.separators.clone();
            let mut parser = PiplupParser::new(config)?;
            parser.parse_lines_for_training(entries)
        }
        LogPatternAlgorithm::Drain3(params) => {
            let mut config = Drain3Config {
                log_format: vec![SYNTHETIC_CONTENT_FORMAT.to_string()],
                preprocess: resolved.preprocess,
                typed_placeholders: resolved.typed_placeholders,
                extra_delimiters: resolved.delimiters.clone(),
                ..Drain3Config::default()
            };
            if let Some(depth) = params.depth {
                config.depth = depth.get();
            }
            if let Some(sim_thresh) = params.sim_thresh {
                config.sim_thresh = sim_thresh.as_f64();
            }
            if let Some(max_children) = params.max_children {
                config.max_children = max_children.get();
            }
            config.max_token_bytes = params.max_token_bytes;
            config.max_line_bytes = params.max_line_bytes;
            let mut parser = Drain3Parser::new(config)?;
            parser.parse_lines_for_training(entries)
        }
    }
}

fn normalize_pattern(pattern: &str) -> String {
    normalize_template_wildcards(pattern)
}

/// Render a pattern into an anchored regexp.
///
/// `delimiters` (ADR-0042) and `separators` (ADR-0046) never co-occur: the
/// facade cross-rejects Drain3+separators and PIPLUP+delimiters, so at most
/// one list is non-empty here. The signature carries both so each rule
/// keeps its own rendering path — the Drain3 `\s*` adjacency rule and the
/// PIPLUP separator boundary class are deliberately different semantics.
fn pattern_to_regexp(
    pattern: &str,
    delimiters: &[String],
    separators: &[String],
    dialect: RegexpDialect,
) -> String {
    let normalized = normalize_pattern(pattern);
    let mut regexp = String::from("(?s)^");

    if delimiters.is_empty() {
        // ADR-0046 §5: with separators active, every whitespace run in the
        // literal segments compiles to the separator boundary class so the
        // regexp matches the original separator-bearing content; without
        // separators the output is byte-identical to the historical `\s+`.
        for (index, segment) in normalized.split("<*>").enumerate() {
            if index > 0 {
                regexp.push_str("(.*?)");
            }
            append_literal_segment_regex(segment, dialect, separators, &mut regexp);
        }
    } else {
        // ADR-0042 §5: templates render space-joined, so whitespace adjacent
        // to a token exactly equal to a caller delimiter compiles to `\s*`
        // (delimiter-split tokens introduce whitespace that does not exist in
        // the original content); all other whitespace keeps `\s+`.
        let tokens: Vec<&str> = normalized.split_whitespace().collect();
        for (index, token) in tokens.iter().enumerate() {
            if index > 0 {
                let adjacent_to_delimiter = delimiters
                    .iter()
                    .any(|d| d == tokens[index - 1] || d == token);
                regexp.push_str(if adjacent_to_delimiter {
                    r"\s*"
                } else {
                    r"\s+"
                });
            }
            for (part_index, part) in token.split("<*>").enumerate() {
                if part_index > 0 {
                    regexp.push_str("(.*?)");
                }
                append_literal_segment_regex(part, dialect, &[], &mut regexp);
            }
        }
    }

    regexp.push('$');
    regexp
}

fn append_literal_segment_regex(
    segment: &str,
    dialect: RegexpDialect,
    separators: &[String],
    output: &mut String,
) {
    let mut literal_run = String::new();
    let mut whitespace_run = false;

    for ch in segment.chars() {
        if ch.is_whitespace() {
            flush_literal_run_regex(&mut literal_run, dialect, output);
            if !whitespace_run {
                push_boundary_class(separators, output);
                whitespace_run = true;
            }
        } else {
            whitespace_run = false;
            literal_run.push(ch);
        }
    }

    flush_literal_run_regex(&mut literal_run, dialect, output);
}

/// ADR-0046 §5: with separators active, every whitespace boundary compiles
/// to `(?:\s|esc(s₁)|…|esc(sₙ))+` — each separator is regex-escaped with the
/// same escaping applied to literal characters — instead of the historical
/// `\s+`, so the regexp matches the original separator-bearing content.
/// `+` (not `*`) keeps boundaries mandatory (`User login <*>` cannot match
/// `Userlogin alice`). The class is plain literal alternation, so both
/// dialects (ADR-0044) render it identically.
fn push_boundary_class(separators: &[String], output: &mut String) {
    if separators.is_empty() {
        output.push_str(r"\s+");
        return;
    }
    output.push_str("(?:\\s");
    for separator in separators {
        output.push('|');
        output.push_str(&regex::escape(separator));
    }
    output.push_str(")+");
}

fn flush_literal_run_regex(literal_run: &mut String, dialect: RegexpDialect, output: &mut String) {
    if literal_run.is_empty() {
        return;
    }

    let mut chunk = String::new();
    let mut ascii_chunk = None;
    for ch in literal_run.chars() {
        let is_ascii = ch.is_ascii();
        match ascii_chunk {
            Some(current) if current == is_ascii => chunk.push(ch),
            Some(current) => {
                append_literal_chunk(&chunk, current, dialect, output);
                chunk.clear();
                chunk.push(ch);
                ascii_chunk = Some(is_ascii);
            }
            None => {
                chunk.push(ch);
                ascii_chunk = Some(is_ascii);
            }
        }
    }
    if let Some(is_ascii) = ascii_chunk {
        append_literal_chunk(&chunk, is_ascii, dialect, output);
    }
    literal_run.clear();
}

/// Render one ASCII/non-ASCII literal chunk according to `dialect` (RFC-0025,
/// ADR-0044). `Rust` keeps the historical `(?i-u:...)` group; `Re2` expands
/// ASCII letters to case character classes.
fn append_literal_chunk(chunk: &str, is_ascii: bool, dialect: RegexpDialect, output: &mut String) {
    match dialect {
        RegexpDialect::Rust => append_literal_chunk_regex(chunk, is_ascii, output),
        RegexpDialect::Re2 => append_literal_chunk_re2(chunk, is_ascii, output),
    }
}

fn append_literal_chunk_regex(chunk: &str, is_ascii: bool, output: &mut String) {
    let escaped = regex::escape(chunk);
    if is_ascii {
        output.push_str("(?i-u:");
        output.push_str(&escaped);
        output.push(')');
    } else {
        output.push_str(&escaped);
    }
}

/// RE2-compatible literal chunk renderer (RFC-0025).
///
/// ASCII chunks escape the run, then each ASCII letter expands to a case
/// character class (`G` -> `[Gg]`); other escaped characters pass through
/// verbatim. Non-ASCII chunks escape and emit verbatim with no case folding.
///
/// Assumption on `regex::escape`: for the printable ASCII text that reaches
/// here, its output introduces no letters of its own — it only inserts a `\`
/// before a non-alphabetic metacharacter, so the only ASCII letters in the
/// escaped run are the input's own. This holds because chunks carry no
/// whitespace (segmented to `\s+` upstream) and log content has no embedded
/// control chars; a raw control char would make `regex::escape` emit
/// `\x{HH}`/`\n`-style escapes whose `x`/`n` would be wrongly folded — an
/// accepted limitation, out of scope for ASCII log templates.
fn append_literal_chunk_re2(chunk: &str, is_ascii: bool, output: &mut String) {
    let escaped = regex::escape(chunk);
    if is_ascii {
        for ch in escaped.chars() {
            if ch.is_ascii_alphabetic() {
                output.push('[');
                output.push(ch.to_ascii_uppercase());
                output.push(ch.to_ascii_lowercase());
                output.push(']');
            } else {
                output.push(ch);
            }
        }
    } else {
        output.push_str(&escaped);
    }
}

fn validate_label_option(
    labels: &Option<Vec<String>>,
    content_len: usize,
) -> Result<(), LogParseError> {
    if let Some(labels) = labels {
        validate_label_len(labels.len(), content_len)?;
    }
    Ok(())
}

fn validate_label_len(label_len: usize, content_len: usize) -> Result<(), LogParseError> {
    if label_len == 0 || label_len == content_len {
        Ok(())
    } else {
        Err(LogParseError::InvalidConfig {
            field: "labels",
            message: format!("expected length 0 or content length {content_len}, got {label_len}"),
        })
    }
}

fn validate_delimiter_strings(delimiters: &[String]) -> Result<(), LogParseError> {
    validate_boundary_strings(delimiters, "delimiters", "delimiter")
}

fn validate_separator_strings(separators: &[String]) -> Result<(), LogParseError> {
    validate_boundary_strings(separators, "separators", "separator")
}

/// Shared ADR-0042 validation rules: non-empty strings, no whitespace
/// (whitespace is already a boundary), no `<` or `>` (would collide with
/// the `<*>` wildcard rendering).
fn validate_boundary_strings(
    values: &[String],
    field: &'static str,
    noun: &str,
) -> Result<(), LogParseError> {
    for (index, value) in values.iter().enumerate() {
        if value.is_empty() {
            return Err(LogParseError::InvalidConfig {
                field,
                message: format!("{noun} at index {index} is empty"),
            });
        }
        if value.chars().any(char::is_whitespace) {
            return Err(LogParseError::InvalidConfig {
                field,
                message: format!("{noun} at index {index} contains whitespace"),
            });
        }
        if value.contains('<') || value.contains('>') {
            return Err(LogParseError::InvalidConfig {
                field,
                message: format!("{noun} at index {index} contains '<' or '>'"),
            });
        }
    }
    Ok(())
}

fn reject_common_params(common: &CommonPatternParams) -> Result<(), LogParseError> {
    if common.threshold.is_some() {
        return Err(unsupported_common_param("threshold"));
    }
    if common.tolerance.is_some() {
        return Err(unsupported_common_param("tolerance"));
    }
    if common.max_digit_ratio.is_some() {
        return Err(unsupported_common_param("max_digit_ratio"));
    }
    Ok(())
}

fn unsupported_common_param(field: &'static str) -> LogParseError {
    LogParseError::InvalidConfig {
        field,
        message: "no exact existing parser capability implements this common parameter yet"
            .to_string(),
    }
}

#[derive(Debug, Default)]
struct ParsedDtoParams {
    common: CommonPatternParams,
    piplup: PiplupPatternParams,
    drain3: Drain3PatternParams,
    preprocess: Option<bool>,
    typed_placeholders: Option<bool>,
}

fn parse_legacy_algorithm(
    params: &Map<String, Value>,
) -> Result<Option<LogPatternAlgorithmTag>, LogParseError> {
    params
        .get("algorithm")
        .map(|value| parse_algorithm_value(value, "algorithm"))
        .transpose()
}

fn resolve_algorithm_tag(
    top_level: Option<LogPatternAlgorithmTag>,
    legacy: Option<LogPatternAlgorithmTag>,
) -> Result<LogPatternAlgorithmTag, LogParseError> {
    match (top_level, legacy) {
        (Some(top_level), Some(legacy)) if top_level != legacy => {
            Err(LogParseError::InvalidConfig {
                field: "algorithm",
                message: "top-level algorithm conflicts with params.algorithm".to_string(),
            })
        }
        (Some(top_level), _) => Ok(top_level),
        (None, Some(legacy)) => Ok(legacy),
        (None, None) => Ok(LogPatternAlgorithmTag::Piplup),
    }
}

fn parse_dto_params(
    params: &Map<String, Value>,
    algorithm: LogPatternAlgorithmTag,
) -> Result<ParsedDtoParams, LogParseError> {
    let mut parsed = ParsedDtoParams::default();
    let mut max_digit_ratio_alias: Option<&str> = None;
    let mut typed_placeholders_alias: Option<&str> = None;
    let mut sim_thresh_alias: Option<&str> = None;
    let mut max_children_alias: Option<&str> = None;
    let mut hit_limit_alias: Option<&str> = None;
    let mut br_thresh_alias: Option<&str> = None;
    let mut max_token_bytes_alias: Option<&str> = None;
    let mut max_line_bytes_alias: Option<&str> = None;

    for (key, value) in params {
        match key.as_str() {
            "algorithm" => {}
            "threshold" => {
                parsed.common = parsed
                    .common
                    .with_threshold(parse_nonzero_usize(value, "threshold")?);
            }
            "tolerance" => {
                parsed.common = parsed
                    .common
                    .with_tolerance(parse_ratio(value, "tolerance")?);
            }
            "maxDigitRatio" | "max_digit_ratio" => {
                reject_duplicate_alias(&mut max_digit_ratio_alias, key, "max_digit_ratio")?;
                parsed.common = parsed
                    .common
                    .with_max_digit_ratio(parse_ratio(value, "max_digit_ratio")?);
            }
            "preprocess" => {
                parsed.preprocess = Some(parse_bool(value, "preprocess")?);
            }
            "typedPlaceholders" | "typed_placeholders" => {
                reject_duplicate_alias(&mut typed_placeholders_alias, key, "typed_placeholders")?;
                parsed.typed_placeholders = Some(parse_bool(value, "typed_placeholders")?);
            }
            "hit_limit" | "hitLimit" if algorithm == LogPatternAlgorithmTag::Piplup => {
                reject_duplicate_alias(&mut hit_limit_alias, key, "hit_limit")?;
                parsed.piplup = parsed
                    .piplup
                    .with_hit_limit(parse_nonzero_usize(value, "hit_limit")?);
            }
            "br_thresh" | "brThresh" if algorithm == LogPatternAlgorithmTag::Piplup => {
                reject_duplicate_alias(&mut br_thresh_alias, key, "br_thresh")?;
                parsed.piplup = parsed
                    .piplup
                    .with_br_thresh(parse_nonzero_usize(value, "br_thresh")?);
            }
            "sim_thresh" | "simThresh" if algorithm == LogPatternAlgorithmTag::Piplup => {
                reject_duplicate_alias(&mut sim_thresh_alias, key, "sim_thresh")?;
                parsed.piplup = parsed
                    .piplup
                    .with_sim_thresh(parse_ratio(value, "sim_thresh")?);
            }
            "merge" if algorithm == LogPatternAlgorithmTag::Piplup => {
                parsed.piplup = parsed.piplup.with_merge(parse_bool(value, "merge")?);
            }
            "depth" if algorithm == LogPatternAlgorithmTag::Drain3 => {
                parsed.drain3 = parsed
                    .drain3
                    .with_depth(parse_nonzero_usize(value, "depth")?);
            }
            "sim_thresh" | "simThresh" if algorithm == LogPatternAlgorithmTag::Drain3 => {
                reject_duplicate_alias(&mut sim_thresh_alias, key, "sim_thresh")?;
                parsed.drain3 = parsed
                    .drain3
                    .with_sim_thresh(parse_ratio(value, "sim_thresh")?);
            }
            "max_children" | "maxChildren" if algorithm == LogPatternAlgorithmTag::Drain3 => {
                reject_duplicate_alias(&mut max_children_alias, key, "max_children")?;
                parsed.drain3 = parsed
                    .drain3
                    .with_max_children(parse_nonzero_usize(value, "max_children")?);
            }
            "max_token_bytes" | "maxTokenBytes" => {
                reject_duplicate_alias(&mut max_token_bytes_alias, key, "max_token_bytes")?;
                let raw = parse_usize(value, "max_token_bytes")?;
                parsed.piplup = parsed.piplup.with_max_token_bytes(raw);
                parsed.drain3 = parsed.drain3.with_max_token_bytes(raw);
            }
            "max_line_bytes" | "maxLineBytes" => {
                reject_duplicate_alias(&mut max_line_bytes_alias, key, "max_line_bytes")?;
                let raw = parse_usize(value, "max_line_bytes")?;
                parsed.piplup = parsed.piplup.with_max_line_bytes(raw);
                parsed.drain3 = parsed.drain3.with_max_line_bytes(raw);
            }
            _ => {
                return Err(LogParseError::InvalidConfig {
                    field: "params",
                    message: format!("unknown params key {key:?}"),
                });
            }
        }
    }

    Ok(parsed)
}

fn reject_duplicate_alias<'a>(
    seen: &mut Option<&'a str>,
    key: &'a str,
    field: &'static str,
) -> Result<(), LogParseError> {
    if let Some(previous) = seen {
        return Err(LogParseError::InvalidConfig {
            field,
            message: format!("duplicate aliases {previous:?} and {key:?}"),
        });
    }
    *seen = Some(key);
    Ok(())
}

fn parse_algorithm_value(
    value: &Value,
    field: &'static str,
) -> Result<LogPatternAlgorithmTag, LogParseError> {
    let Some(value) = value.as_str() else {
        return Err(LogParseError::InvalidConfig {
            field,
            message: "must be a string".to_string(),
        });
    };
    match value {
        "piplup" => Ok(LogPatternAlgorithmTag::Piplup),
        "drain3" => Ok(LogPatternAlgorithmTag::Drain3),
        other => Err(LogParseError::InvalidConfig {
            field,
            message: format!("unknown algorithm {other:?}"),
        }),
    }
}

fn parse_nonzero_usize(value: &Value, field: &'static str) -> Result<NonZeroUsize, LogParseError> {
    let Some(raw) = value.as_u64() else {
        return Err(LogParseError::InvalidConfig {
            field,
            message: "must be a positive integer".to_string(),
        });
    };
    let raw = usize::try_from(raw).map_err(|_| LogParseError::InvalidConfig {
        field,
        message: "is too large for this platform".to_string(),
    })?;
    NonZeroUsize::new(raw).ok_or_else(|| LogParseError::InvalidConfig {
        field,
        message: "must be greater than 0".to_string(),
    })
}

fn parse_usize(value: &Value, field: &'static str) -> Result<usize, LogParseError> {
    let Some(raw) = value.as_u64() else {
        return Err(LogParseError::InvalidConfig {
            field,
            message: "must be a non-negative integer".to_string(),
        });
    };
    usize::try_from(raw).map_err(|_| LogParseError::InvalidConfig {
        field,
        message: "is too large for this platform".to_string(),
    })
}

fn parse_ratio(value: &Value, field: &'static str) -> Result<Ratio, LogParseError> {
    let Some(raw) = value.as_f64() else {
        return Err(LogParseError::InvalidConfig {
            field,
            message: "must be a finite number in 0.0..=1.0".to_string(),
        });
    };
    Ratio::try_from(raw).map_err(|_| LogParseError::InvalidConfig {
        field,
        message: format!("must be finite and in 0.0..=1.0, got {raw}"),
    })
}

fn parse_bool(value: &Value, field: &'static str) -> Result<bool, LogParseError> {
    value.as_bool().ok_or_else(|| LogParseError::InvalidConfig {
        field,
        message: "must be a boolean".to_string(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn invalid_field<T>(result: Result<T, LogParseError>) -> &'static str {
        match result {
            Ok(_) => panic!("expected InvalidConfig, got Ok"),
            Err(LogParseError::InvalidConfig { field, .. }) => field,
            Err(other) => panic!("expected InvalidConfig, got {other:?}"),
        }
    }

    #[test]
    fn ratio_accepts_only_finite_unit_interval() {
        assert_eq!(Ratio::try_from(0.0).unwrap().as_f64(), 0.0);
        assert_eq!(Ratio::try_from(1.0).unwrap().as_f64(), 1.0);
        assert!(Ratio::try_from(-0.1).is_err());
        assert!(Ratio::try_from(1.1).is_err());
        assert!(Ratio::try_from(f64::NAN).is_err());
        assert!(Ratio::try_from(f64::INFINITY).is_err());
    }

    #[test]
    fn delimiter_rejects_empty_strings() {
        assert_eq!(Delimiter::try_from("|").unwrap().as_str(), "|");
        assert_eq!(invalid_field(Delimiter::try_from("")), "delimiters");
    }

    #[test]
    fn label_shapes_are_validated() {
        assert!(LogPatternBuilder::piplup().extract(["a", "b"]).is_ok());
        assert!(
            LogPatternBuilder::piplup()
                .labels(Vec::<String>::new())
                .extract(["a"])
                .is_ok()
        );
        assert!(
            LogPatternBuilder::piplup()
                .labels(["x", "y"])
                .extract(["a", "b"])
                .is_ok()
        );
        assert_eq!(
            invalid_field(
                LogPatternBuilder::piplup()
                    .labels(["x"])
                    .extract(["a", "b"])
            ),
            "labels"
        );
    }

    #[test]
    fn dto_parses_strict_params_and_aliases() {
        let request: GetLogPatternsRequest = serde_json::from_value(serde_json::json!({
            "content": ["a"],
            "algorithm": "drain3",
            "params": {
                "preprocess": false,
                "typedPlaceholders": true,
                "depth": 4,
                "simThresh": 0.5,
                "maxChildren": 16
            }
        }))
        .unwrap();
        let (_content, builder) = request.try_into_parts().unwrap();
        match builder.algorithm {
            LogPatternAlgorithm::Drain3(params) => {
                assert_eq!(params.depth().unwrap().get(), 4);
                assert_eq!(params.sim_thresh().unwrap().as_f64(), 0.5);
                assert_eq!(params.max_children().unwrap().get(), 16);
            }
            LogPatternAlgorithm::Piplup(_) => panic!("expected drain3"),
        }
        assert!(!builder.preprocess);
        assert!(builder.typed_placeholders);
    }

    #[test]
    fn piplup_params_default_to_none_and_fluent_setters_round_trip() {
        let params = PiplupPatternParams::default();
        assert!(params.hit_limit().is_none());
        assert!(params.br_thresh().is_none());
        assert!(params.sim_thresh().is_none());
        assert!(params.merge().is_none());

        let params = params
            .with_hit_limit(NonZeroUsize::new(100).unwrap())
            .with_br_thresh(NonZeroUsize::new(2).unwrap())
            .with_sim_thresh(Ratio::try_from(0.5).unwrap())
            .with_merge(false);
        assert_eq!(params.hit_limit().unwrap().get(), 100);
        assert_eq!(params.br_thresh().unwrap().get(), 2);
        assert_eq!(params.sim_thresh().unwrap().as_f64(), 0.5);
        assert_eq!(params.merge(), Some(false));
    }

    #[test]
    fn piplup_params_max_token_bytes_defaults_none_and_setter_round_trip() {
        let params = PiplupPatternParams::default();
        assert!(params.max_token_bytes().is_none());
        assert!(params.max_line_bytes().is_none());

        let params = params.with_max_token_bytes(512).with_max_line_bytes(4096);
        assert_eq!(params.max_token_bytes(), Some(512));
        assert_eq!(params.max_line_bytes(), Some(4096));
    }

    #[test]
    fn drain3_params_default_to_none_and_fluent_setters_round_trip() {
        let params = Drain3PatternParams::default();
        assert!(params.depth().is_none());
        assert!(params.sim_thresh().is_none());
        assert!(params.max_children().is_none());
        assert!(params.max_token_bytes().is_none());
        assert!(params.max_line_bytes().is_none());

        let params = params
            .with_depth(NonZeroUsize::new(4).unwrap())
            .with_sim_thresh(Ratio::try_from(0.5).unwrap())
            .with_max_children(NonZeroUsize::new(16).unwrap())
            .with_max_token_bytes(256)
            .with_max_line_bytes(2048);
        assert_eq!(params.depth().unwrap().get(), 4);
        assert_eq!(params.sim_thresh().unwrap().as_f64(), 0.5);
        assert_eq!(params.max_children().unwrap().get(), 16);
        assert_eq!(params.max_token_bytes(), Some(256));
        assert_eq!(params.max_line_bytes(), Some(2048));
    }

    #[test]
    fn max_line_bytes_filters_long_lines_via_piplup_facade() {
        let patterns =
            LogPatternBuilder::piplup_with(PiplupPatternParams::default().with_max_line_bytes(20))
                .extract(["short", "this line is way too long and should be filtered"])
                .unwrap();
        let assignments = patterns.assignments();
        assert_ne!(
            assignments[0].index(),
            assignments[1].index(),
            "filtered and unfiltered lines must have different template ids"
        );
        assert_eq!(
            patterns.template_for_row(1).unwrap().pattern(),
            "0",
            "overlong line should get sentinel template"
        );
    }

    #[test]
    fn dto_parses_max_token_bytes_and_max_line_bytes() {
        let request: GetLogPatternsRequest = serde_json::from_value(serde_json::json!({
            "content": ["a"],
            "algorithm": "piplup",
            "params": {
                "maxTokenBytes": 512,
                "maxLineBytes": 4096
            }
        }))
        .unwrap();
        let (_content, builder) = request.try_into_parts().unwrap();
        match &builder.algorithm {
            LogPatternAlgorithm::Piplup(params) => {
                assert_eq!(params.max_token_bytes(), Some(512));
                assert_eq!(params.max_line_bytes(), Some(4096));
            }
            LogPatternAlgorithm::Drain3(_) => panic!("expected piplup"),
        }

        let snake: GetLogPatternsRequest = serde_json::from_value(serde_json::json!({
            "content": ["a"],
            "algorithm": "drain3",
            "params": {
                "max_token_bytes": 128,
                "max_line_bytes": 1024
            }
        }))
        .unwrap();
        let (_content, builder) = snake.try_into_parts().unwrap();
        match &builder.algorithm {
            LogPatternAlgorithm::Drain3(params) => {
                assert_eq!(params.max_token_bytes(), Some(128));
                assert_eq!(params.max_line_bytes(), Some(1024));
            }
            LogPatternAlgorithm::Piplup(_) => panic!("expected drain3"),
        }
    }

    #[test]
    fn dto_parses_piplup_params_and_aliases() {
        let snake: GetLogPatternsRequest = serde_json::from_value(serde_json::json!({
            "content": ["a"],
            "algorithm": "piplup",
            "params": {
                "hit_limit": 100,
                "br_thresh": 2,
                "sim_thresh": 0.5,
                "merge": false
            }
        }))
        .unwrap();
        let (_content, builder) = snake.try_into_parts().unwrap();
        match &builder.algorithm {
            LogPatternAlgorithm::Piplup(params) => {
                assert_eq!(params.hit_limit().unwrap().get(), 100);
                assert_eq!(params.br_thresh().unwrap().get(), 2);
                assert_eq!(params.sim_thresh().unwrap().as_f64(), 0.5);
                assert_eq!(params.merge(), Some(false));
            }
            LogPatternAlgorithm::Drain3(_) => panic!("expected piplup"),
        }

        let camel: GetLogPatternsRequest = serde_json::from_value(serde_json::json!({
            "content": ["a"],
            "algorithm": "piplup",
            "params": { "hitLimit": 64, "brThresh": 3, "simThresh": 0.4 }
        }))
        .unwrap();
        let (_content, builder) = camel.try_into_parts().unwrap();
        match &builder.algorithm {
            LogPatternAlgorithm::Piplup(params) => {
                assert_eq!(params.hit_limit().unwrap().get(), 64);
                assert_eq!(params.br_thresh().unwrap().get(), 3);
                assert_eq!(params.sim_thresh().unwrap().as_f64(), 0.4);
                assert!(params.merge().is_none());
            }
            LogPatternAlgorithm::Drain3(_) => panic!("expected piplup"),
        }
    }

    #[test]
    fn dto_rejects_piplup_duplicate_aliases_and_foreign_algorithm_keys() {
        let duplicate: GetLogPatternsRequest = serde_json::from_value(serde_json::json!({
            "content": ["a"],
            "algorithm": "piplup",
            "params": { "hitLimit": 10, "hit_limit": 20 }
        }))
        .unwrap();
        assert_eq!(invalid_field(duplicate.try_into_parts()), "hit_limit");

        let foreign: GetLogPatternsRequest = serde_json::from_value(serde_json::json!({
            "content": ["a"],
            "algorithm": "piplup",
            "params": { "depth": 4 }
        }))
        .unwrap();
        assert_eq!(invalid_field(foreign.try_into_parts()), "params");

        let foreign_drain3: GetLogPatternsRequest = serde_json::from_value(serde_json::json!({
            "content": ["a"],
            "algorithm": "drain3",
            "params": { "hit_limit": 10 }
        }))
        .unwrap();
        assert_eq!(invalid_field(foreign_drain3.try_into_parts()), "params");
    }

    #[test]
    fn dto_piplup_params_validate_value_ranges() {
        let zero: GetLogPatternsRequest = serde_json::from_value(serde_json::json!({
            "content": ["a"],
            "algorithm": "piplup",
            "params": { "br_thresh": 0 }
        }))
        .unwrap();
        assert_eq!(invalid_field(zero.try_into_parts()), "br_thresh");

        let out_of_range: GetLogPatternsRequest = serde_json::from_value(serde_json::json!({
            "content": ["a"],
            "algorithm": "piplup",
            "params": { "sim_thresh": 1.5 }
        }))
        .unwrap();
        assert_eq!(invalid_field(out_of_range.try_into_parts()), "sim_thresh");

        let non_bool: GetLogPatternsRequest = serde_json::from_value(serde_json::json!({
            "content": ["a"],
            "algorithm": "piplup",
            "params": { "merge": "yes" }
        }))
        .unwrap();
        assert_eq!(invalid_field(non_bool.try_into_parts()), "merge");
    }

    #[test]
    fn dto_rejects_unknown_params_algorithm_conflicts_and_duplicate_aliases() {
        let unknown: GetLogPatternsRequest = serde_json::from_value(serde_json::json!({
            "content": ["a"],
            "params": { "bogus": true }
        }))
        .unwrap();
        assert_eq!(invalid_field(unknown.try_into_parts()), "params");

        let conflict: GetLogPatternsRequest = serde_json::from_value(serde_json::json!({
            "content": ["a"],
            "algorithm": "piplup",
            "params": { "algorithm": "drain3" }
        }))
        .unwrap();
        assert_eq!(invalid_field(conflict.try_into_parts()), "algorithm");

        let duplicate: GetLogPatternsRequest = serde_json::from_value(serde_json::json!({
            "content": ["a"],
            "params": { "maxDigitRatio": 0.1, "max_digit_ratio": 0.2 }
        }))
        .unwrap();
        assert_eq!(invalid_field(duplicate.try_into_parts()), "max_digit_ratio");
    }

    #[test]
    fn dto_accepts_legacy_algorithm_and_common_params_but_extract_rejects_common_semantics() {
        let request: GetLogPatternsRequest = serde_json::from_value(serde_json::json!({
            "content": ["a"],
            "params": { "algorithm": "piplup", "threshold": 3 }
        }))
        .unwrap();
        let (content, builder) = request.try_into_parts().unwrap();
        assert_eq!(invalid_field(builder.extract(content)), "threshold");
    }

    #[test]
    fn variable_kind_serde_uses_stable_strings() {
        let value = serde_json::to_value(vec![
            LogPatternVariableKind::Unknown,
            LogPatternVariableKind::Number,
            LogPatternVariableKind::Ipv4,
            LogPatternVariableKind::Url,
        ])
        .unwrap();
        assert_eq!(
            value,
            serde_json::json!(["<*>", "<Number>", "<IPv4>", "<URL>"])
        );
        let parsed: Vec<LogPatternVariableKind> = serde_json::from_value(value).unwrap();
        assert_eq!(
            parsed,
            vec![
                LogPatternVariableKind::Unknown,
                LogPatternVariableKind::Number,
                LogPatternVariableKind::Ipv4,
                LogPatternVariableKind::Url,
            ]
        );
    }

    #[test]
    fn regex_renderer_escapes_literals_and_matches_case_insensitively() {
        let regexp = pattern_to_regexp(
            "GET [<*>] user=(<*>?) done.",
            &[],
            &[],
            RegexpDialect::default(),
        );
        let regex = regex::Regex::new(&regexp).unwrap();
        let captures = regex.captures("get [Alpha] USER=(Beta?) DONE.").unwrap();
        assert_eq!(captures.len() - 1, 2);
        assert_eq!(&captures[1], "Alpha");
        assert_eq!(&captures[2], "Beta");
    }

    #[test]
    fn regex_renderer_collapses_literal_whitespace() {
        let regexp = pattern_to_regexp("A   <*> \t B", &[], &[], RegexpDialect::default());
        let regex = regex::Regex::new(&regexp).unwrap();
        assert!(regex.is_match("a \n value \t b"));
    }

    #[test]
    fn regex_renderer_relaxes_whitespace_adjacent_to_custom_delimiters() {
        // ADR-0042 §5: whitespace adjacent to a token exactly equal to a
        // caller delimiter compiles to `\s*`; other whitespace stays `\s+`.
        let delimiters = vec!["||".to_string()];
        let regexp = pattern_to_regexp("chrome || <*>", &delimiters, &[], RegexpDialect::default());
        let regex = regex::Regex::new(&regexp).unwrap();
        assert!(regex.is_match("chrome||91"));
        assert!(regex.is_match("chrome || 91"));

        // Genuine token boundaries keep `\s+`: `User login <*>` must not
        // match `Userlogin alice`.
        let regexp =
            pattern_to_regexp("User login <*>", &delimiters, &[], RegexpDialect::default());
        let regex = regex::Regex::new(&regexp).unwrap();
        assert!(!regex.is_match("Userlogin alice"));
        assert!(regex.is_match("User login alice"));
    }

    #[test]
    fn regex_renderer_re2_dialect_output() {
        // RFC-0025: the Re2 dialect replaces `(?i-u:...)` local-flag groups
        // with ASCII character-class expansion. NOTE: every ASCII letter in an
        // ASCII chunk expands — including those inside `/api/users/` — so the
        // segment stays case-insensitive (the RFC Guide-level example table
        // omitted those expansions; the algorithm here is authoritative).
        let rust = pattern_to_regexp("GET /api/users/<*> HTTP/1.1", &[], &[], RegexpDialect::Rust);
        let re2 = pattern_to_regexp("GET /api/users/<*> HTTP/1.1", &[], &[], RegexpDialect::Re2);

        assert_eq!(
            rust,
            r"(?s)^(?i-u:GET)\s+(?i-u:/api/users/)(.*?)\s+(?i-u:HTTP/1\.1)$"
        );
        assert_eq!(
            re2,
            r"(?s)^[Gg][Ee][Tt]\s+/[Aa][Pp][Ii]/[Uu][Ss][Ee][Rr][Ss]/(.*?)\s+[Hh][Tt][Tt][Pp]/1\.1$"
        );
    }

    #[test]
    fn regex_renderer_re2_is_semantically_equivalent() {
        let rust = pattern_to_regexp("GET /api/users/<*> HTTP/1.1", &[], &[], RegexpDialect::Rust);
        let re2 = pattern_to_regexp("GET /api/users/<*> HTTP/1.1", &[], &[], RegexpDialect::Re2);

        // The Re2 output parses in the Rust `regex` crate and is case-insensitive.
        let re = regex::Regex::new(&re2).unwrap();
        for candidate in ["get /api/users/7 HTTP/1.1", "Get /api/users/7 Http/1.1"] {
            assert!(re.is_match(candidate), "Re2 failed to match {candidate:?}");
        }

        // Same number of capturing groups (one `(.*?)` per `<*>`). Counting
        // raw `(` would be misleading: the Rust dialect wraps literals in
        // non-capturing `(?i-u:...)` flag groups, the Re2 dialect does not.
        let rust_re = regex::Regex::new(&rust).unwrap();
        assert_eq!(rust_re.captures_len(), re.captures_len());
        assert_eq!(re.captures_len(), 2); // implicit group 0 + one `<*>`
    }

    #[test]
    fn regex_renderer_re2_leaves_non_alphabetic_verbatim() {
        // Defensive (RFC-0025 review): only ASCII letters expand to case
        // classes. Digits and punctuation in the escaped run must pass through
        // untouched — `[Gg]1\.2`, never `[1...]` or `[.]`. Whitespace is
        // already segmented to `\s+` upstream, so it cannot appear in a chunk.
        let re2 = pattern_to_regexp("agent G1.2 done", &[], &[], RegexpDialect::Re2);
        assert_eq!(
            re2,
            r"(?s)^[Aa][Gg][Ee][Nn][Tt]\s+[Gg]1\.2\s+[Dd][Oo][Nn][Ee]$"
        );
        // No character class wraps a digit or the literal dot.
        assert!(!re2.contains("[1"));
        assert!(!re2.contains("[."));
        // The escaped dot is a literal `.`, not "any char": `1\.2` rejects `1x2`.
        let re = regex::Regex::new(&re2).unwrap();
        assert!(re.is_match("agent g1.2 done"));
        assert!(!re.is_match("agent g1x2 done"));
    }

    #[test]
    fn regexp_dialect_default_is_rust() {
        // A builder that never calls `regexp_dialect` renders with the Rust
        // dialect (historical behavior, unchanged).
        let patterns = LogPatternBuilder::piplup().extract(["GET /done"]).unwrap();
        let template = &patterns.templates()[0];
        assert_eq!(
            template.regexp(),
            pattern_to_regexp(template.pattern(), &[], &[], RegexpDialect::Rust)
        );
        assert!(template.regexp().contains("(?i-u:"));
    }

    #[test]
    fn regexp_dialect_re2_builder_end_to_end() {
        let patterns = LogPatternBuilder::piplup()
            .regexp_dialect(RegexpDialect::Re2)
            .extract(["GET /api/users/123 HTTP/1.1"])
            .unwrap();
        let template = &patterns.templates()[0];

        // The stored regexp equals the Re2 rendering of the learned pattern
        // (regardless of which pattern piplup learned) and carries no
        // Rust-only `(?i-u:...)` groups.
        assert_eq!(
            template.regexp(),
            pattern_to_regexp(template.pattern(), &[], &[], RegexpDialect::Re2)
        );
        assert!(!template.regexp().contains("(?i-u:"));

        let re = regex::Regex::new(template.regexp()).unwrap();
        assert!(re.is_match("get /api/users/999 HTTP/1.1"));
    }

    #[test]
    fn get_log_patterns_request_regexp_dialect_json() {
        // Explicit `re2` deserializes to the Re2 variant.
        let request: GetLogPatternsRequest = serde_json::from_value(serde_json::json!({
            "content": ["GET /x"],
            "regexp_dialect": "re2"
        }))
        .unwrap();
        assert_eq!(request.regexp_dialect, Some(RegexpDialect::Re2));

        // `rust` deserializes to the Rust variant.
        let request: GetLogPatternsRequest = serde_json::from_value(serde_json::json!({
            "content": ["GET /x"],
            "regexp_dialect": "rust"
        }))
        .unwrap();
        assert_eq!(request.regexp_dialect, Some(RegexpDialect::Rust));

        // Absent field deserializes to None (which the builder resolves to the
        // Rust default).
        let request: GetLogPatternsRequest =
            serde_json::from_value(serde_json::json!({ "content": ["GET /x"] })).unwrap();
        assert_eq!(request.regexp_dialect, None);

        let (_content, builder) = request.try_into_parts().unwrap();
        assert_eq!(builder.regexp_dialect, RegexpDialect::Rust);
    }

    #[test]
    fn drain3_extract_honors_custom_delimiters_end_to_end() {
        let patterns = LogPatternBuilder::drain3(Drain3PatternParams::default())
            .delimiters(["||"])
            .extract(["chrome||91 stable", "chrome||92 stable"])
            .unwrap();
        assert_eq!(patterns.template_count(), 1);
        let template = &patterns.templates()[0];
        assert_eq!(template.pattern(), "chrome || <*> stable");
        // The regexp must match the original, un-spaced content.
        let regex = regex::Regex::new(template.regexp()).unwrap();
        assert!(regex.is_match("chrome||91 stable"));
        assert!(regex.is_match("chrome||92 stable"));
    }

    #[test]
    fn piplup_extract_rejects_custom_delimiters_with_named_algorithm() {
        let err = LogPatternBuilder::piplup()
            .delimiters(["|"])
            .extract(["a|b"])
            .unwrap_err();
        match err {
            LogParseError::Unsupported(message) => assert!(message.contains("piplup")),
            other => panic!("expected Unsupported, got {other:?}"),
        }
    }

    #[test]
    fn delimiters_reject_whitespace_and_angle_brackets() {
        assert_eq!(
            invalid_field(
                LogPatternBuilder::drain3(Drain3PatternParams::default())
                    .delimiters(["a b"])
                    .extract(["a b c"])
            ),
            "delimiters"
        );
        assert_eq!(
            invalid_field(
                LogPatternBuilder::drain3(Drain3PatternParams::default())
                    .delimiters(["\t"])
                    .extract(["a\tb"])
            ),
            "delimiters"
        );
        assert_eq!(
            invalid_field(
                LogPatternBuilder::drain3(Drain3PatternParams::default())
                    .delimiters(["<"])
                    .extract(["a<b"])
            ),
            "delimiters"
        );
        assert_eq!(
            invalid_field(
                LogPatternBuilder::drain3(Drain3PatternParams::default())
                    .delimiters(["a>"])
                    .extract(["a>b"])
            ),
            "delimiters"
        );
        assert_eq!(
            invalid_field(
                LogPatternBuilder::drain3(Drain3PatternParams::default())
                    .delimiters([""])
                    .extract(["a"])
            ),
            "delimiters"
        );
    }

    #[test]
    fn drain3_extract_with_empty_delimiters_is_unchanged() {
        let patterns = LogPatternBuilder::drain3(Drain3PatternParams::default())
            .delimiters(Vec::<String>::new())
            .extract(["worker alpha done", "worker beta done"])
            .unwrap();
        assert_eq!(patterns.template_count(), 1);
        assert_eq!(patterns.templates()[0].pattern(), "worker <*> done");
    }

    #[test]
    fn piplup_extract_honors_custom_separators_end_to_end() {
        // issue #2 shape: commas replace inter-field spaces, gluing
        // `failed,retry` style literals and fragmenting clusters.
        let content = [
            "upload failed,retry 3 times,timeout 5000 ms",
            "upload failed,retry 7 times,timeout 3000 ms",
        ];
        let patterns = LogPatternBuilder::piplup()
            .separators([","])
            .extract(content)
            .unwrap();
        assert_eq!(patterns.template_count(), 1);
        let template = &patterns.templates()[0];
        assert_eq!(
            template.pattern(),
            "upload failed retry <*> times timeout <*>"
        );
        // The regexp matches the ORIGINAL comma-bearing content and keeps
        // one capture group per `<*>`.
        let regex = regex::Regex::new(template.regexp()).unwrap();
        for line in content {
            assert!(regex.is_match(line));
        }
        let captures = regex.captures(content[0]).unwrap();
        assert_eq!(captures.len() - 1, 2);
    }

    #[test]
    fn piplup_extract_honors_full_width_separator_end_to_end() {
        let content = [
            "任务失败，重试 3 次，耗时 5000 ms",
            "任务失败，重试 5 次，耗时 3000 ms",
        ];
        let patterns = LogPatternBuilder::piplup()
            .separators(["，"])
            .extract(content)
            .unwrap();
        assert_eq!(patterns.template_count(), 1);
        let template = &patterns.templates()[0];
        assert_eq!(template.pattern(), "任务失败 重试 <*> 次 耗时 <*>");
        let regex = regex::Regex::new(template.regexp()).unwrap();
        for line in content {
            assert!(regex.is_match(line));
        }
    }

    #[test]
    fn dto_parses_separators_for_piplup() {
        let request: GetLogPatternsRequest = serde_json::from_value(serde_json::json!({
            "content": ["upload failed,retry 3 times,timeout 5000 ms"],
            "separators": [","],
            "algorithm": "piplup"
        }))
        .unwrap();
        let (content, builder) = request.try_into_parts().unwrap();
        let patterns = builder.extract(content).unwrap();
        assert_eq!(
            patterns.templates()[0].pattern(),
            "upload failed retry <*> times timeout <*>"
        );
    }

    #[test]
    fn drain3_extract_rejects_custom_separators_with_named_algorithm() {
        let err = LogPatternBuilder::drain3(Drain3PatternParams::default())
            .separators([","])
            .extract(["a,b"])
            .unwrap_err();
        match err {
            LogParseError::Unsupported(message) => {
                assert_eq!(message, CUSTOM_SEPARATORS_UNSUPPORTED_DRAIN3);
            }
            other => panic!("expected Unsupported, got {other:?}"),
        }
    }

    #[test]
    fn piplup_delimiters_error_message_points_at_separators() {
        // ADR-0046: the PIPLUP+delimiters rejection keeps its ADR-0042 shape
        // and now cross-points callers at `separators`.
        let err = LogPatternBuilder::piplup()
            .delimiters([","])
            .extract(["a,b"])
            .unwrap_err();
        match err {
            LogParseError::Unsupported(message) => {
                assert_eq!(message, CUSTOM_DELIMITERS_UNSUPPORTED_PIPLUP);
            }
            other => panic!("expected Unsupported, got {other:?}"),
        }
    }

    #[test]
    fn separators_reject_whitespace_and_angle_brackets() {
        for separator in ["a b", "\t", "<", "a>", ""] {
            assert_eq!(
                invalid_field(
                    LogPatternBuilder::piplup()
                        .separators([separator])
                        .extract(["a b c"])
                ),
                "separators",
                "separator {separator:?} must be rejected"
            );
        }
    }

    #[test]
    fn regex_renderer_separator_boundary_class() {
        // ADR-0046 §5: with separators active, every whitespace boundary
        // compiles to `(?:\s|sep)+` so the regexp matches the original
        // separator-bearing content; boundaries stay mandatory.
        let separators = vec![",".to_string()];
        let regexp = pattern_to_regexp("upload failed", &[], &separators, RegexpDialect::default());
        assert!(regexp.contains(r"(?:\s|,)+"));
        let regex = regex::Regex::new(&regexp).unwrap();
        assert!(regex.is_match("upload failed"));
        assert!(regex.is_match("upload,failed"));
        assert!(!regex.is_match("uploadfailed"));

        // Without separators the rendering is byte-identical to `\s+`.
        let plain = pattern_to_regexp("upload failed", &[], &[], RegexpDialect::default());
        assert!(plain.contains(r"\s+"));
        assert!(!plain.contains("(?:\\s|"));
    }

    #[test]
    fn regex_renderer_separator_class_flows_through_re2_dialect() {
        // The boundary class is plain literal alternation; the Re2 dialect
        // (ADR-0044) must render it unchanged and it must still match the
        // original separator-bearing content.
        let separators = vec![",".to_string()];
        let re2 = pattern_to_regexp("upload failed <*>", &[], &separators, RegexpDialect::Re2);
        assert!(re2.contains(r"(?:\s|,)+"));
        let regex = regex::Regex::new(&re2).unwrap();
        assert!(regex.is_match("upload,failed 42"));
        assert!(regex.is_match("upload failed 42"));
    }

    #[test]
    fn template_deduplicates_by_normalized_pattern() {
        let patterns = LogPatternBuilder::piplup()
            .extract(["worker alpha", "worker alpha", "worker alpha"])
            .unwrap();
        assert_eq!(patterns.template_count(), 1);
        assert_eq!(patterns.templates()[0].occurrences(), 3);
        assert_eq!(patterns.row_count(), 3);
    }

    #[test]
    fn templates_ordered_by_first_occurrence() {
        let patterns = LogPatternBuilder::piplup()
            .extract(["alpha done", "beta running", "alpha done", "beta running"])
            .unwrap();
        assert_eq!(patterns.templates()[0].pattern(), "alpha done");
        assert_eq!(patterns.templates()[1].pattern(), "beta running");
    }

    #[test]
    fn rows_for_returns_correct_indices() {
        let patterns = LogPatternBuilder::piplup()
            .extract(["a", "b", "a", "c", "a"])
            .unwrap();
        let id = patterns.assignments()[0];
        let rows: Vec<_> = patterns.rows_for(id).collect();
        assert_eq!(rows, vec![0, 2, 4]);
    }

    #[test]
    fn template_for_row_out_of_bounds_returns_none() {
        let patterns = LogPatternBuilder::piplup().extract(["a"]).unwrap();
        assert!(patterns.template_for_row(0).is_some());
        assert!(patterns.template_for_row(1).is_none());
    }

    #[test]
    fn template_regexp_matches_content() {
        let input = "GET /api/users/123 status=200";
        let patterns = LogPatternBuilder::piplup().extract([input]).unwrap();
        let template = &patterns.templates()[0];
        let re = regex::Regex::new(template.regexp()).unwrap();
        assert!(re.is_match(input));
    }

    #[test]
    fn sum_of_occurrences_equals_row_count() {
        let patterns = LogPatternBuilder::piplup()
            .extract(["a", "b", "a", "b", "a"])
            .unwrap();
        let total: usize = patterns.templates().iter().map(|t| t.occurrences()).sum();
        assert_eq!(total, patterns.row_count());
    }

    #[test]
    fn variable_kind_mapping_is_exhaustive_one_to_one() {
        let mappings = [
            (PlaceholderKind::Number, LogPatternVariableKind::Number),
            (PlaceholderKind::Hex, LogPatternVariableKind::Hex),
            (PlaceholderKind::Id, LogPatternVariableKind::Id),
            (PlaceholderKind::Url, LogPatternVariableKind::Url),
            (PlaceholderKind::Path, LogPatternVariableKind::Path),
            (PlaceholderKind::Ipv4, LogPatternVariableKind::Ipv4),
            (PlaceholderKind::Ipv6, LogPatternVariableKind::Ipv6),
            (PlaceholderKind::Mac, LogPatternVariableKind::Mac),
            (PlaceholderKind::Date, LogPatternVariableKind::Date),
            (PlaceholderKind::Time, LogPatternVariableKind::Time),
            (PlaceholderKind::Duration, LogPatternVariableKind::Duration),
            (PlaceholderKind::Size, LogPatternVariableKind::Size),
        ];
        for (internal, public) in mappings {
            assert_eq!(variable_kind(internal), public, "{internal:?}");
        }
    }

    fn evidence_map(
        entries: &[(&str, Vec<MaskEvidence>)],
    ) -> std::collections::BTreeMap<String, Vec<MaskEvidence>> {
        entries
            .iter()
            .map(|(template, slots)| (template.to_string(), slots.clone()))
            .collect()
    }

    #[test]
    fn dedup_join_keeps_same_kind_across_groups() {
        let mut merged = evidence_map(&[]);
        merge_group_evidence(
            &mut merged,
            evidence_map(&[(
                "worker <*> done",
                vec![
                    MaskEvidence::Unseen,
                    MaskEvidence::Seen(PlaceholderKind::Number),
                    MaskEvidence::Unseen,
                ],
            )]),
        );
        merge_group_evidence(
            &mut merged,
            evidence_map(&[(
                "worker <*> done",
                vec![
                    MaskEvidence::Unseen,
                    MaskEvidence::Seen(PlaceholderKind::Number),
                    MaskEvidence::Unseen,
                ],
            )]),
        );

        let variables = derive_variables(
            "worker <*> done",
            merged.get("worker <*> done").map(Vec::as_slice),
        );
        assert_eq!(variables, vec![LogPatternVariableKind::Number]);
    }

    #[test]
    fn dedup_join_downgrades_conflicting_kinds_across_groups() {
        let mut merged = evidence_map(&[]);
        merge_group_evidence(
            &mut merged,
            evidence_map(&[(
                "worker <*> done",
                vec![
                    MaskEvidence::Unseen,
                    MaskEvidence::Seen(PlaceholderKind::Number),
                    MaskEvidence::Unseen,
                ],
            )]),
        );
        merge_group_evidence(
            &mut merged,
            evidence_map(&[(
                "worker <*> done",
                vec![
                    MaskEvidence::Unseen,
                    MaskEvidence::Seen(PlaceholderKind::Hex),
                    MaskEvidence::Unseen,
                ],
            )]),
        );

        let variables = derive_variables(
            "worker <*> done",
            merged.get("worker <*> done").map(Vec::as_slice),
        );
        assert_eq!(variables, vec![LogPatternVariableKind::Unknown]);
    }

    #[test]
    fn dedup_join_keeps_untyped_sticky_across_groups() {
        let mut merged = evidence_map(&[]);
        merge_group_evidence(
            &mut merged,
            evidence_map(&[(
                "worker <*> done",
                vec![
                    MaskEvidence::Unseen,
                    MaskEvidence::Untyped,
                    MaskEvidence::Unseen,
                ],
            )]),
        );
        merge_group_evidence(
            &mut merged,
            evidence_map(&[(
                "worker <*> done",
                vec![
                    MaskEvidence::Unseen,
                    MaskEvidence::Seen(PlaceholderKind::Number),
                    MaskEvidence::Unseen,
                ],
            )]),
        );
        // Order must not matter: merging the same groups in reverse gives the
        // same sticky `Untyped` result.
        let mut reversed = evidence_map(&[]);
        merge_group_evidence(
            &mut reversed,
            evidence_map(&[(
                "worker <*> done",
                vec![
                    MaskEvidence::Unseen,
                    MaskEvidence::Seen(PlaceholderKind::Number),
                    MaskEvidence::Unseen,
                ],
            )]),
        );
        merge_group_evidence(
            &mut reversed,
            evidence_map(&[(
                "worker <*> done",
                vec![
                    MaskEvidence::Unseen,
                    MaskEvidence::Untyped,
                    MaskEvidence::Unseen,
                ],
            )]),
        );

        for map in [&merged, &reversed] {
            let variables = derive_variables(
                "worker <*> done",
                map.get("worker <*> done").map(Vec::as_slice),
            );
            assert_eq!(variables, vec![LogPatternVariableKind::Unknown]);
        }
    }

    #[test]
    fn multi_wildcard_token_degrades_all_markers_to_unknown() {
        // `<*>------<*>` compiles to a single literal token: without 1:1 token
        // alignment every marker in the token degrades to `Unknown`, even if
        // slot evidence exists at that token index.
        let evidence = evidence_map(&[(
            "a <*>------<*> b",
            vec![
                MaskEvidence::Unseen,
                MaskEvidence::Seen(PlaceholderKind::Number),
                MaskEvidence::Unseen,
            ],
        )]);
        let variables = derive_variables(
            "a <*>------<*> b",
            evidence.get("a <*>------<*> b").map(Vec::as_slice),
        );
        assert_eq!(
            variables,
            vec![
                LogPatternVariableKind::Unknown,
                LogPatternVariableKind::Unknown
            ]
        );
        assert_eq!(variables.len(), "a <*>------<*> b".matches("<*>").count());
    }

    #[test]
    fn embedded_wildcard_slot_maps_kind_and_holds_length_invariant() {
        let evidence = evidence_map(&[(
            "pid=<*> done",
            vec![
                MaskEvidence::Seen(PlaceholderKind::Number),
                MaskEvidence::Unseen,
            ],
        )]);
        let variables = derive_variables(
            "pid=<*> done",
            evidence.get("pid=<*> done").map(Vec::as_slice),
        );
        assert_eq!(variables, vec![LogPatternVariableKind::Number]);
        assert_eq!(variables.len(), "pid=<*> done".matches("<*>").count());
    }

    #[test]
    fn missing_evidence_yields_unknown_with_length_invariant() {
        for pattern in ["worker <*> done", "pid=<*>", "a <*>------<*> b", "plain"] {
            let variables = derive_variables(pattern, None);
            assert_eq!(variables.len(), pattern.matches("<*>").count(), "{pattern}");
            assert!(
                variables
                    .iter()
                    .all(|kind| *kind == LogPatternVariableKind::Unknown),
                "{pattern}"
            );
        }
    }

    #[test]
    fn untyped_extraction_reports_unknown_variables_with_length_invariant() {
        let patterns = LogPatternBuilder::piplup()
            .extract(["worker 42 done", "worker 43 done"])
            .unwrap();
        let template = &patterns.templates()[0];
        assert_eq!(
            template.variables().len(),
            template.pattern().matches("<*>").count()
        );
        assert!(
            template
                .variables()
                .iter()
                .all(|kind| *kind == LogPatternVariableKind::Unknown)
        );
    }

    #[test]
    fn template_dto_serializes_variables_and_defaults_on_absent_field() {
        let patterns = LogPatternBuilder::piplup()
            .typed_placeholders(true)
            .preprocess(false)
            .extract(["worker 10.0.0.1 connected", "worker 10.0.0.2 connected"])
            .unwrap();
        let response = GetLogPatternsResponse::from(patterns);
        let value = serde_json::to_value(&response).unwrap();
        assert_eq!(
            value["templates"][0]["variables"],
            serde_json::json!(["<IPv4>"])
        );

        let json = r#"{"pattern":"worker <*>","regexp":"(?s)^worker\\s+(.*?)$","occurrences":2}"#;
        let dto: LogTemplateDto = serde_json::from_str(json).unwrap();
        assert!(dto.variables.is_empty());
    }
}

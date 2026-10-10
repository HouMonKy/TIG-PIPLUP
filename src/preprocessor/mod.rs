//! Preprocessing rules for normalizing log content before template learning.
//!
//! The preprocessor applies a catalog of regular-expression rules (URLs, IP
//! addresses, paths, numbers, etc.) to replace variable substrings with the
//! `<*>` wildcard. During training, the first [`PREPROCESS_PLAN_ESTIMATE_LIMIT`]
//! lines are used to estimate which rules are active; the resulting
//! [`PreprocessPlan`] is stored in the cache/index so parsing remains stable.

mod catalog;

use regex::Regex;
use serde::{Deserialize, Serialize};
use std::sync::OnceLock;

use crate::error::LogParseError;

pub use catalog::{Rule, RuleId, TYPED_OWNED_RULE_IDS, default_rules};

/// Number of lines used to estimate the active preprocess plan.
pub const PREPROCESS_PLAN_ESTIMATE_LIMIT: usize = 2000;

/// Result of estimating preprocessing for a single line.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EstimateResult {
    /// Preprocessed line text.
    pub output: String,
    /// Ids of the rules that fired for this line.
    pub matched_rule_ids: Vec<RuleId>,
}

/// A normalized set of preprocessing rules enabled for a given input.
///
/// Rule ids are exposed as a public `Vec` for compatibility with existing
/// incremental plan builders, but [`PreprocessPlan::new`] sorts and
/// deduplicates them so that construction through the provided API yields a
/// stable, order-independent representation.
#[derive(Debug, Clone, Serialize)]
pub struct PreprocessPlan {
    /// Sorted, deduplicated ids of enabled preprocessing rules.
    pub enabled_rule_ids: Vec<RuleId>,
}

impl<'de> Deserialize<'de> for PreprocessPlan {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        #[derive(Deserialize)]
        struct Raw {
            enabled_rule_ids: Vec<RuleId>,
        }

        let raw = Raw::deserialize(deserializer)?;
        Ok(Self::new(raw.enabled_rule_ids))
    }
}

impl Default for PreprocessPlan {
    fn default() -> Self {
        Self::new(Vec::new())
    }
}

impl PreprocessPlan {
    /// Build a plan from a list of rule ids.
    ///
    /// The ids are sorted and deduplicated so that two plans describing the
    /// same set of rules compare equal and produce the same content hash.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::{PreprocessPlan, RuleId};
    ///
    /// let plan = PreprocessPlan::new(vec![RuleId::Url, RuleId::Url, RuleId::Path]);
    /// assert_eq!(plan.enabled_rule_ids, vec![RuleId::Url, RuleId::Path]);
    /// ```
    pub fn new(mut enabled_rule_ids: Vec<RuleId>) -> Self {
        enabled_rule_ids.sort_unstable();
        enabled_rule_ids.dedup();
        Self { enabled_rule_ids }
    }

    /// Check whether `rule_id` is enabled in this plan.
    pub fn contains(&self, rule_id: RuleId) -> bool {
        self.enabled_rule_ids.contains(&rule_id)
    }

    /// Compute a SHA-256 over the stable set of enabled rule ids.
    ///
    /// The hash is independent of the order in which ids were supplied.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::{PreprocessPlan, RuleId};
    ///
    /// let plan = PreprocessPlan::new(vec![RuleId::Url]);
    /// let hash = plan.compute_content_hash();
    /// assert_ne!(hash, [0u8; 32]);
    /// ```
    pub fn compute_content_hash(&self) -> [u8; 32] {
        use sha2::{Digest, Sha256};

        let mut ids: Vec<u8> = self.enabled_rule_ids.iter().map(|id| *id as u8).collect();
        ids.sort_unstable();
        ids.dedup();
        Sha256::digest(&ids).into()
    }

    /// Append `rule_id` to `enabled_rule_ids` if not already present.
    ///
    /// Preserves encounter order (absent id → tail; present id → unchanged),
    /// matching the previous free functions verbatim.
    pub(crate) fn push_unique_rule_id(&mut self, rule_id: RuleId) {
        if !self.enabled_rule_ids.contains(&rule_id) {
            self.enabled_rule_ids.push(rule_id);
        }
    }
}

impl PartialEq for PreprocessPlan {
    fn eq(&self, other: &Self) -> bool {
        if self.enabled_rule_ids.len() != other.enabled_rule_ids.len() {
            return false;
        }
        let mut left = self.enabled_rule_ids.clone();
        let mut right = other.enabled_rule_ids.clone();
        left.sort_unstable();
        right.sort_unstable();
        left == right
    }
}

impl Eq for PreprocessPlan {}

/// Preprocessor that applies a catalog of normalization rules.
#[derive(Debug, Clone)]
pub struct Preprocessor {
    rules: Vec<Rule>,
    collapse_wildcards: Regex,
}

impl Default for Preprocessor {
    fn default() -> Self {
        static RULES: OnceLock<Vec<Rule>> = OnceLock::new();
        static COLLAPSE: OnceLock<Regex> = OnceLock::new();

        let rules = RULES
            .get_or_init(|| default_rules().unwrap_or_default())
            .clone();
        let collapse_wildcards = COLLAPSE
            .get_or_init(|| {
                Regex::new(r"(<\*>\s?)+<\*>").unwrap_or_else(|_| {
                    Regex::new(r"<\*>").expect("literal fallback regex should compile")
                })
            })
            .clone();

        Self {
            rules,
            collapse_wildcards,
        }
    }
}

impl Preprocessor {
    /// Apply the placeholder reduction used by TIG-PIPLUP at `3e51f3b`.
    fn finalize(&self, line: &str) -> String {
        self.collapse_wildcards(line)
    }

    /// Create a preprocessor with the default rule catalog.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::Preprocessor;
    ///
    /// let preprocessor = Preprocessor::new().unwrap();
    /// assert!(!preprocessor.rules().is_empty());
    /// ```
    pub fn new() -> Result<Self, LogParseError> {
        Ok(Self {
            rules: default_rules()?,
            collapse_wildcards: Regex::new(r"(<\*>\s?)+<\*>")?,
        })
    }

    /// Return the loaded preprocessing rules.
    pub fn rules(&self) -> &[Rule] {
        &self.rules
    }

    /// Estimate which rules apply to a single line and return the preprocessed text.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::Preprocessor;
    ///
    /// let preprocessor = Preprocessor::default();
    /// let result = preprocessor.estimate_line("connected to https://example.com");
    /// assert!(result.output.contains("<*>"));
    /// assert!(!result.matched_rule_ids.is_empty());
    /// ```
    pub fn estimate_line(&self, line: &str) -> EstimateResult {
        let mut working = line.to_string();
        let mut matched_rule_ids = Vec::new();

        for rule in &self.rules {
            let next = rule
                .pattern
                .replace_all(&working, rule.replacement)
                .into_owned();
            if next != working {
                matched_rule_ids.push(rule.id);
                working = next;
            }
        }

        EstimateResult {
            output: self.finalize(&working),
            matched_rule_ids,
        }
    }

    /// Like [`estimate_line`](Self::estimate_line) but skips the rules in
    /// `excluded`.
    ///
    /// ADR-0033 coexistence: typed mode excludes the rules whose shapes the
    /// typed classifier owns ([`TYPED_OWNED_RULE_IDS`]), so the eager regex
    /// neither runs them (perf) nor erases their shapes before the classifier
    /// sees them (mask correctness).
    ///
    /// [`TYPED_OWNED_RULE_IDS`]: crate::preprocessor::TYPED_OWNED_RULE_IDS
    pub fn estimate_line_excluding(&self, line: &str, excluded: &[RuleId]) -> EstimateResult {
        let mut working = line.to_string();
        let mut matched_rule_ids = Vec::new();

        for rule in &self.rules {
            if excluded.contains(&rule.id) {
                continue;
            }
            let next = rule
                .pattern
                .replace_all(&working, rule.replacement)
                .into_owned();
            if next != working {
                matched_rule_ids.push(rule.id);
                working = next;
            }
        }

        EstimateResult {
            output: self.finalize(&working),
            matched_rule_ids,
        }
    }

    /// Estimate a plan from an iterator of lines.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::Preprocessor;
    ///
    /// let preprocessor = Preprocessor::default();
    /// let plan = preprocessor.estimate(vec!["https://example.com", "10.0.0.1:8080"]);
    /// assert!(!plan.enabled_rule_ids.is_empty());
    /// ```
    pub fn estimate<'a, I>(&self, lines: I) -> PreprocessPlan
    where
        I: IntoIterator<Item = &'a str>,
    {
        let mut enabled_rule_ids = Vec::new();
        for line in lines {
            let result = self.estimate_line(line);
            for rule_id in result.matched_rule_ids {
                if !enabled_rule_ids.contains(&rule_id) {
                    enabled_rule_ids.push(rule_id);
                }
            }
        }
        PreprocessPlan::new(enabled_rule_ids)
    }

    /// Apply the enabled rules from `plan` to a single line.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::{Preprocessor, PreprocessPlan, RuleId};
    ///
    /// let preprocessor = Preprocessor::default();
    /// let plan = PreprocessPlan::new(vec![RuleId::Url]);
    /// let output = preprocessor.apply_with_plan("visit https://example.com now", &plan);
    /// assert!(output.contains("<*>"));
    /// ```
    pub fn apply_with_plan(&self, line: &str, plan: &PreprocessPlan) -> String {
        let mut working = line.to_string();
        for rule in &self.rules {
            if plan.contains(rule.id) {
                working = rule
                    .pattern
                    .replace_all(&working, rule.replacement)
                    .into_owned();
            }
        }
        self.finalize(&self.collapse_wildcards(&working))
    }

    fn collapse_wildcards(&self, line: &str) -> String {
        self.collapse_wildcards
            .replace_all(line, "<*>")
            .into_owned()
    }
}

#[cfg(test)]
mod tier3_tests {
    use super::*;

    #[test]
    fn numerical_sign_and_decimal_semantics() {
        let p = Preprocessor::default();
        // Signs are consumed at word and non-word boundaries alike
        // (Python \b semantics: `(-5` and `abc-5` both mask the sign).
        assert_eq!(
            p.apply_with_plan("err (-5) done", &plan_of(p.rules())),
            "err (-<*>) done"
        );
        assert_eq!(
            p.apply_with_plan("volt abc-5 done", &plan_of(p.rules())),
            "volt abc<*> done"
        );
        // A digit glued to a word character stays (identifier evidence).
        assert_eq!(
            p.apply_with_plan("mode x+-5z done", &plan_of(p.rules())),
            "mode x+-5z done"
        );
        // Decimal point consumed only when followed by digits.
        assert_eq!(
            p.apply_with_plan("loaded 17500.objType now", &plan_of(p.rules())),
            "loaded <*>objType now"
        );
    }

    #[test]
    fn compound_shape_rules_mask_identifiers() {
        let p = Preprocessor::default();
        let plan = plan_of(p.rules());
        assert_eq!(
            p.apply_with_plan("ctx pid:53912,tid:53969 stop", &plan),
            "ctx <*> stop"
        );
        assert_eq!(
            p.apply_with_plan("marker sn(20AB1234CD) registered", &plan),
            "marker sn(<*>) registered"
        );
    }

    #[test]
    fn dme_rules_match_python_shapes() {
        let p = Preprocessor::default();
        let plan = plan_of(p.rules());
        assert_eq!(
            p.apply_with_plan("[ 4247.652837] worker up", &plan),
            "[<*>] worker up"
        );
        assert_eq!(
            p.apply_with_plan("Vcontroller: read_iops changed", &plan),
            "Vcontroller: <*> changed"
        );
        assert_eq!(
            p.apply_with_plan("init module mount cache begin", &plan),
            "init module <*> begin"
        );
    }

    fn plan_of(rules: &[Rule]) -> PreprocessPlan {
        PreprocessPlan::new(rules.iter().map(|r| r.id).collect())
    }
}

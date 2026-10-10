//! Built-in preprocessing rule catalog.
//!
//! Each [`RuleId`] corresponds to a regular expression that replaces common
//! variable substrings (URLs, IP addresses, dates, etc.) with the `<*>` wildcard.

use regex::Regex;
use serde::{Deserialize, Serialize};

use crate::error::LogParseError;

/// Identifier for a built-in preprocessing rule.
#[repr(u8)]
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub enum RuleId {
    /// URL patterns (`http://...`, `https://...`).
    Url,
    /// IPv4 addresses with optional port.
    Ipv4Port,
    /// Hostname with port. The dotted chain must contain at least one
    /// non-digit word character (letter, `_`, or `-`) so `IPv4:port` shapes
    /// stay with the IPv4 rules and, in typed mode, with the placeholder
    /// classifier.
    HostPort,
    /// Package host identifiers. The dotted chain must contain at least one
    /// non-digit word character (letter, `_`, or `-`) so plain IPv4 shapes
    /// stay with the IPv4 rules and, in typed mode, with the placeholder
    /// classifier.
    PackageHost,
    /// IPv6 addresses.
    Ipv6,
    /// MAC addresses.
    MacAddress,
    /// Time-of-day strings.
    Time,
    /// File-system paths.
    Path,
    /// Block identifiers (`blk_...`).
    Block,
    /// Date strings (`YYYY-MM-DD`, `YYYY/MM/DD`).
    Date,
    /// Durations (`10s`, `5 ms`).
    Duration,
    /// Data sizes (`1KB`, `2.5MB`).
    Size,
    /// Generic numbers and hex values.
    Numerical,
    /// Weekday and month names.
    WeekdayMonths,
    /// Syslog level words in content (`WARN`, `ERR`, `EMER`, `DBG`).
    LogLevel,
    /// `pid:123,tid:456` pairs — concurrent execution randomizes both.
    PidTidPair,
    /// Serial numbers in `sn(...)` markers.
    SnParentheses,
    /// Free-form step descriptions inside `Step (...)`.
    StepDesc,
    /// Full `YYYY-MM-DD HH:MM:SS` timestamps.
    DateTimeFull,
    /// Floating-point kernel uptime values inside square brackets.
    KernelUptime,
    /// Metric names following `Vcontroller: `.
    VcontrollerMetric,
    /// Free-form actions between `init module` and `begin`/`end`.
    FsInitAction,
}

/// The regex rules whose variable shapes are owned by the typed placeholder
/// classifier ([`PlaceholderKind`](crate::placeholder::PlaceholderKind)).
///
/// In typed mode these eager regex rules are SKIPPED (coexistence): the typed
/// classifier handles their shapes lazily, so running the regex would both
/// double the work and erase the shape before the classifier sees it.
pub const TYPED_OWNED_RULE_IDS: &[RuleId] = &[
    RuleId::Url,
    RuleId::Ipv4Port,
    RuleId::Ipv6,
    RuleId::MacAddress,
    RuleId::Time,
    RuleId::Path,
    RuleId::Date,
    RuleId::Duration,
    RuleId::Size,
    RuleId::Numerical,
];

impl RuleId {
    /// Whether this rule's variable shape is owned by the typed placeholder
    /// classifier (and thus skipped in typed mode — see [`TYPED_OWNED_RULE_IDS`]).
    pub fn is_typed_owned(self) -> bool {
        TYPED_OWNED_RULE_IDS.contains(&self)
    }
}

/// A single preprocessing rule.
#[derive(Debug, Clone)]
pub struct Rule {
    /// Rule identifier.
    pub id: RuleId,
    /// Human-readable rule name.
    pub name: &'static str,
    /// Regular expression that matches substrings to replace.
    pub pattern: Regex,
    /// Replacement string, typically `"<*>"`.
    pub replacement: &'static str,
}

/// Return the default catalog of preprocessing rules.
///
/// # Example
///
/// ```rust
/// use log_parser::preprocessor::default_rules;
///
/// let rules = default_rules().unwrap();
/// assert!(!rules.is_empty());
/// ```
pub fn default_rules() -> Result<Vec<Rule>, LogParseError> {
    let ordered_rules = [
        // Specific compound shapes first (TIG-PIPLUP ordering principle):
        // generic rules (url / path / numerical) would otherwise truncate
        // them early.
        (
            RuleId::DateTimeFull,
            "datetime_full",
            r"\b\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2}\b",
        ),
        (
            RuleId::KernelUptime,
            "kernel_uptime",
            r"(\[)\s*\d+\.\d+(\])",
        ),
        (
            RuleId::LogLevel,
            "log_level",
            r"\b(WARN|ERR|EMER|INFO|DBG)\b",
        ),
        (RuleId::PidTidPair, "pid_tid_pair", r"pid:\d+,tid:\d+"),
        (
            RuleId::SnParentheses,
            "sn_parentheses",
            r"(sn\()[A-Za-z0-9]+(\))",
        ),
        (RuleId::StepDesc, "step_desc", r"(Step \()[^)]+(\))"),
        (
            RuleId::VcontrollerMetric,
            "vcontroller_metric",
            r"(Vcontroller:\s)[A-Za-z_]+",
        ),
        (
            RuleId::FsInitAction,
            "fs_init_action",
            r"(init module\s).*?(\s(?:begin|end\b))",
        ),
        (
            RuleId::Url,
            "url",
            r"\bhttps?:\/\/(www\.)?[a-zA-Z0-9-]+(\.[a-zA-Z]{2,})+(:[0-9]{1,5})?(\/[^\s]*)?\b",
        ),
        (RuleId::Ipv4Port, "IPv4_port", r"(/|)(\d+\.){3}\d+(:\d+)?"),
        (
            RuleId::HostPort,
            "host_port",
            r"([\w-]+\.)*[\w-]*[A-Za-z_-][\w-]*\.([\w-]+\.)*[\w-]+\:\d+|([\w-]+\.)+[\w-]*[A-Za-z_-][\w-]*\:\d+",
        ),
        (
            RuleId::PackageHost,
            "package_host",
            r"([\w-]+\.){2,}[\w-]*[A-Za-z_-][\w-]*(\$[\w-]+)*(\@[\w-]+)?|([\w-]+\.)*[\w-]*[A-Za-z_-][\w-]*\.([\w-]+\.)+[\w-]+(\$[\w-]+)*(\@[\w-]+)?",
        ),
        (
            RuleId::Ipv6,
            "IPv6",
            r"(([0-9a-fA-F]{1,4}:){7}([0-9a-fA-F]{1,4}|:)|(([0-9a-fA-F]{1,4}:){1,7}|:):((:[0-9a-fA-F]{1,4}){1,7}|:))",
        ),
        (
            RuleId::MacAddress,
            "Mac_address",
            r"^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$",
        ),
        (RuleId::Time, "time", r"\b\d{2}:\d{2}(:\d{2}|:\d{2},\d+)?\b"),
        (
            RuleId::Path,
            "path",
            r"(/|)(([\w.-]+|\<\*\>)/)+([\w.-]+|\<\*\>)",
        ),
        (RuleId::Block, "block", r"blk\_\-?\d+"),
        (RuleId::Date, "date", r"\b\d{4}[-/]\d{2}[-/]\d{2}\b"),
        (RuleId::Duration, "duration", r"\b\d+\s?(sec|s|ms)\b"),
        (
            RuleId::Size,
            "size",
            r"\b\d+\.?\d*\s?([KGTMkgtm]?(B|b)|([KGTMkgtm]))\b",
        ),
        (
            RuleId::Numerical,
            "numerical",
            r"[+-]?\b\d+(?:\.\d+)?\b|\b0[Xx][a-fA-F\d]+\b|\b[a-fA-F\d]{4,}\b",
        ),
        (
            RuleId::WeekdayMonths,
            "weekday_months",
            r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|January|February|March|April|June|July|August|September|October|November|December)\b",
        ),
    ];

    let mut rules = Vec::with_capacity(ordered_rules.len());
    for (id, name, pattern) in ordered_rules {
        // Rules whose pattern captures an anchor character re-emit it via
        // `$1` (the regex crate has no lookbehind).
        let replacement = match id {
            RuleId::KernelUptime
            | RuleId::SnParentheses
            | RuleId::StepDesc
            | RuleId::FsInitAction => "$1<*>$2",
            RuleId::VcontrollerMetric => "$1<*>",
            _ => "<*>",
        };
        rules.push(Rule {
            id,
            name,
            pattern: Regex::new(pattern)?,
            replacement,
        });
    }
    Ok(rules)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rule(id: RuleId) -> Rule {
        default_rules()
            .unwrap()
            .into_iter()
            .find(|rule| rule.id == id)
            .unwrap()
    }

    #[test]
    fn package_host_requires_a_non_digit_in_the_dotted_chain() {
        let rule = rule(RuleId::PackageHost);

        // Plain IPv4 shapes stay with the IPv4 rules / typed classifier.
        assert!(!rule.pattern.is_match("10.0.0.1"));
        assert!(!rule.pattern.is_match("10.0.0.1:8080"));
        assert!(!rule.pattern.is_match("255.255.255.255"));

        // Hostname and package identifiers keep matching, including chains
        // whose only non-digit characters are `-` or `_` (version shapes).
        assert!(rule.pattern.is_match("com.example.host"));
        assert!(rule.pattern.is_match("mail.server.01"));
        assert!(rule.pattern.is_match("10.0.0.x"));
        assert!(rule.pattern.is_match("com.example.host$a@b"));
        assert!(rule.pattern.is_match("3.4.6-1569965"));
        assert!(rule.pattern.is_match("1.8.0_252"));
    }

    #[test]
    fn host_port_requires_a_non_digit_in_the_dotted_chain() {
        let rule = rule(RuleId::HostPort);

        // IPv4:port shapes stay with the IPv4 rules / typed classifier.
        assert!(!rule.pattern.is_match("10.0.0.1:8080"));

        // Real host:port shapes keep matching, wherever the non-digit sits.
        assert!(rule.pattern.is_match("host.example.com:8080"));
        assert!(rule.pattern.is_match("a.1:80"));
        assert!(rule.pattern.is_match("my-host.1:80"));
    }
}

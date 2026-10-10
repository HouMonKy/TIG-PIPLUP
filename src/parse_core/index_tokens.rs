use std::borrow::Cow;

use crate::placeholder::{PlaceholderKind, classify_observed_kind};

/// Replace caller-declared separators with single spaces (ADR-0046).
///
/// Separators are whitespace-equivalent token boundaries: after
/// preprocessing, each matched occurrence is replaced with one space and
/// later dropped by `split_whitespace`. Matching is leftmost,
/// non-overlapping, longest-first (maximal munch), so the result is
/// independent of the caller's array order. The `<*>` token produced by
/// preprocessing is never split, even when a separator would match inside
/// the marker.
///
/// The transform is idempotent: separators become spaces, and spaces are
/// never separators (facade validation rejects whitespace), so a second
/// pass over the output is a no-op.
///
/// An empty separator list returns the input borrowed, with zero cost.
pub(crate) fn normalize_separators<'a>(content: &'a str, separators: &[String]) -> Cow<'a, str> {
    if separators.is_empty() {
        return Cow::Borrowed(content);
    }
    let mut rest = content;
    let mut normalized = String::with_capacity(content.len());
    while let Some(ch) = rest.chars().next() {
        // `<*>` is produced by preprocessing; never split inside it, even
        // when a separator (e.g. `*`) would match within the marker.
        if let Some(after) = rest.strip_prefix("<*>") {
            normalized.push_str("<*>");
            rest = after;
            continue;
        }
        // Maximal munch over the caller-supplied separators; the longest
        // match wins, so the result is independent of caller array order.
        // Empty separators are skipped so a malformed config cannot stall
        // the scan (the facade rejects them; defense in depth here).
        let matched_len = separators
            .iter()
            .filter(|separator| !separator.is_empty() && rest.starts_with(separator.as_str()))
            .map(String::len)
            .max();
        if let Some(len) = matched_len {
            normalized.push(' ');
            rest = &rest[len..];
            continue;
        }
        normalized.push(ch);
        rest = &rest[ch.len_utf8()..];
    }
    Cow::Owned(normalized)
}

/// Tokenize index content by whitespace after separator normalization
/// (ADR-0046). Index-scope naming is intentional: must not be reused by
/// Drain3 training tokenization.
pub(crate) fn tokenize_index_content(content: &str, separators: &[String]) -> Vec<String> {
    normalize_separators(content, separators)
        .split_whitespace()
        .map(|token| token.to_string())
        .collect()
}

/// Classify each token's observed placeholder kind. Returns an empty `Vec`
/// when `typed_placeholders` is disabled.
pub(crate) fn observed_kinds_for_index(
    tokens: &[String],
    typed_placeholders: bool,
) -> Vec<Option<PlaceholderKind>> {
    if !typed_placeholders {
        return Vec::new();
    }
    tokens
        .iter()
        .map(|token| classify_observed_kind(token))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn seps(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| value.to_string()).collect()
    }

    #[test]
    fn empty_separators_return_borrowed_input() {
        let normalized = normalize_separators("a,b，c", &[]);
        assert!(matches!(normalized, Cow::Borrowed("a,b，c")));
        assert_eq!(tokenize_index_content("a,b", &[]), vec!["a,b"]);
    }

    #[test]
    fn separators_replace_each_match_with_one_space() {
        let comma = seps(&[","]);
        assert_eq!(normalize_separators("a,b", &comma), "a b");
        // Full-width comma (U+FF0C, 3 UTF-8 bytes) is a first-class
        // separator.
        let full_width = seps(&["，"]);
        assert_eq!(
            normalize_separators("任务失败，重试", &full_width),
            "任务失败 重试"
        );
        // Multi-character separators match as substrings.
        let multi = seps(&["::"]);
        assert_eq!(normalize_separators("a::b", &multi), "a b");
    }

    #[test]
    fn adjacent_separator_runs_fold_like_spaces() {
        // `a,,b` ≡ `a, b`: each separator becomes one space and
        // `split_whitespace` folds the runs.
        let comma = seps(&[","]);
        assert_eq!(tokenize_index_content("a,,b", &comma), vec!["a", "b"]);
        assert_eq!(tokenize_index_content("a, b", &comma), vec!["a", "b"]);
        assert_eq!(tokenize_index_content("a  b", &[]), vec!["a", "b"]);
    }

    #[test]
    fn separators_at_edges_are_dropped() {
        let comma = seps(&[","]);
        assert_eq!(tokenize_index_content(",lead", &comma), vec!["lead"]);
        assert_eq!(tokenize_index_content("trail,", &comma), vec!["trail"]);
        assert!(tokenize_index_content(",,,", &comma).is_empty());
    }

    #[test]
    fn maximal_munch_is_order_independent() {
        let short_first = seps(&[":", "::"]);
        let long_first = seps(&["::", ":"]);
        assert_eq!(tokenize_index_content("a::b", &short_first), vec!["a", "b"]);
        assert_eq!(tokenize_index_content("a::b", &long_first), vec!["a", "b"]);
    }

    #[test]
    fn wildcard_token_is_never_split() {
        // Even a separator whose characters appear inside `<*>` must not
        // break the wildcard token produced by preprocessing.
        let star = seps(&["*"]);
        assert_eq!(
            tokenize_index_content("worker <*> done", &star),
            vec!["worker", "<*>", "done"]
        );
        assert_eq!(tokenize_index_content("a*b", &star), vec!["a", "b"]);
    }

    #[test]
    fn normalization_is_idempotent() {
        let separators = seps(&[",", "，"]);
        let once = normalize_separators("a,b，c", &separators).into_owned();
        assert_eq!(normalize_separators(&once, &separators), once.as_str());
    }
}

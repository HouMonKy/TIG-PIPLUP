//! Drain3 token classification and normalization.
//!
//! Drain3 operates on plain string tokens (`Vec<String>`), not PIPLUP's
//! `Token` enum. A template slot is either a literal word or the wildcard
//! `<*>`. During routing and similarity scoring we need to classify tokens
//! into three categories: literal, parametrized-numeric (becomes `<*>`), or
//! already-wildcard (`<*>`).

/// Token classification for Drain3's fixed-depth tree.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Drain3TokenKind {
    /// A normal literal token that participates in tree routing.
    Literal,
    /// A token that contains digits and should be treated as `<*>` for
    /// routing and similarity when `parametrize_numeric_tokens` is enabled.
    ParametrizedWildcard,
    /// The literal string `<*>` — always a wildcard.
    AlreadyWildcard,
}

/// Classify a token according to Drain3 rules.
///
/// - `<*>` → `AlreadyWildcard`
/// - `NaN` → `Literal` (IEEE 754 sentinel, not a number)
/// - token contains a digit and `parametrize_numeric` is true → `ParametrizedWildcard`
/// - everything else → `Literal`
pub fn classify(token: &str, parametrize_numeric: bool) -> Drain3TokenKind {
    if token == "<*>" {
        return Drain3TokenKind::AlreadyWildcard;
    }
    if parametrize_numeric && token != "NaN" && token.chars().any(|c| c.is_ascii_digit()) {
        return Drain3TokenKind::ParametrizedWildcard;
    }
    Drain3TokenKind::Literal
}

/// Returns `true` if the token should be treated as `<*>` for routing or
/// similarity scoring.
pub fn is_wildcard(token: &str, parametrize_numeric: bool) -> bool {
    matches!(
        classify(token, parametrize_numeric),
        Drain3TokenKind::AlreadyWildcard | Drain3TokenKind::ParametrizedWildcard
    )
}

/// Normalize a token for tree routing: parametrized-numeric and `<*>` tokens
/// become the literal string `<*>`; everything else passes through unchanged.
pub fn normalize_for_routing(token: &str, parametrize_numeric: bool) -> &str {
    match classify(token, parametrize_numeric) {
        Drain3TokenKind::AlreadyWildcard | Drain3TokenKind::ParametrizedWildcard => "<*>",
        Drain3TokenKind::Literal => token,
    }
}

/// Delimiter characters split into their own tokens when `delimiter_split` is
/// enabled (ADR-0033 改良②). Chosen to break composite tokens (IPv4/IPv6/time/
/// paths/KV/parens) into classifiable pieces while preserving identifiers —
/// `-`/`_` and alphanumerics stay attached.
const DELIMITERS: &[char] = &[':', '.', ',', ';', '"', '/', '=', '(', ')'];

/// Tokenize `content` for Drain3.
///
/// When `delimiter_split` is disabled and `extra_delimiters` is empty, this
/// is plain `split_whitespace` (ADR-0028). When `delimiter_split` is enabled,
/// spaces are inserted around each [`DELIMITERS`] character before splitting,
/// so each delimiter becomes its own token (e.g. `10.0.0.1` →
/// `10 . 0 . 0 . 1`, `pam_unix(sshd:auth):` → `pam_unix ( sshd : auth ) :`).
///
/// ADR-0042: `extra_delimiters` are caller-supplied substrings (multi-character
/// is first-class) matched leftmost, non-overlapping, longest-first — the
/// result is independent of the caller's array order. Each matched delimiter
/// is kept as its own token, exactly like the fixed set. When both sources are
/// active the union applies, with extra delimiters taking precedence at the
/// same position. The `<*>` token produced by preprocessing is never split.
pub fn split_tokens(
    content: &str,
    delimiter_split: bool,
    extra_delimiters: &[String],
) -> Vec<String> {
    if !delimiter_split && extra_delimiters.is_empty() {
        return content.split_whitespace().map(String::from).collect();
    }
    let mut spaced = String::with_capacity(content.len());
    let mut rest = content;
    while let Some(ch) = rest.chars().next() {
        // `<*>` is produced by preprocessing; never split inside it.
        if let Some(after) = rest.strip_prefix("<*>") {
            spaced.push_str("<*>");
            rest = after;
            continue;
        }
        // Maximal munch over caller-supplied delimiters; extras take
        // precedence over the single-character fixed set at this position.
        // Empty delimiters are skipped so a malformed config cannot stall
        // the scan (the facade rejects them; defense in depth here).
        let extra_len = extra_delimiters
            .iter()
            .filter(|delimiter| !delimiter.is_empty() && rest.starts_with(delimiter.as_str()))
            .map(String::len)
            .max();
        if let Some(len) = extra_len {
            spaced.push(' ');
            spaced.push_str(&rest[..len]);
            spaced.push(' ');
            rest = &rest[len..];
            continue;
        }
        if delimiter_split && DELIMITERS.contains(&ch) {
            spaced.push(' ');
            spaced.push(ch);
            spaced.push(' ');
        } else {
            spaced.push(ch);
        }
        rest = &rest[ch.len_utf8()..];
    }
    spaced.split_whitespace().map(String::from).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn already_wildcard_detected() {
        assert_eq!(classify("<*>", true), Drain3TokenKind::AlreadyWildcard);
        assert_eq!(classify("<*>", false), Drain3TokenKind::AlreadyWildcard);
        assert!(is_wildcard("<*>", true));
        assert!(is_wildcard("<*>", false));
    }

    #[test]
    fn nan_is_literal() {
        assert_eq!(classify("NaN", true), Drain3TokenKind::Literal);
        assert!(!is_wildcard("NaN", true));
    }

    #[test]
    fn numeric_parametrized_variants() {
        // v2, 123, 0, a1b, 1e5 all contain digits
        for token in &["v2", "123", "0", "a1b", "1e5"] {
            assert_eq!(
                classify(token, true),
                Drain3TokenKind::ParametrizedWildcard,
                "{token} should be ParametrizedWildcard when enabled"
            );
            assert!(is_wildcard(token, true));
            assert_eq!(
                classify(token, false),
                Drain3TokenKind::Literal,
                "{token} should be Literal when disabled"
            );
            assert!(!is_wildcard(token, false));
        }
    }

    #[test]
    fn pure_alpha_is_literal() {
        assert_eq!(classify("hello", true), Drain3TokenKind::Literal);
        assert!(!is_wildcard("hello", true));
    }

    #[test]
    fn normalize_maps_wildcards() {
        assert_eq!(normalize_for_routing("<*>", true), "<*>");
        assert_eq!(normalize_for_routing("v2", true), "<*>");
        assert_eq!(normalize_for_routing("hello", true), "hello");
        assert_eq!(normalize_for_routing("v2", false), "v2");
    }

    #[test]
    fn empty_string_is_literal() {
        assert_eq!(classify("", true), Drain3TokenKind::Literal);
    }

    #[test]
    fn split_tokens_whitespace_when_disabled() {
        let tokens = split_tokens("worker 42 started", false, &[]);
        assert_eq!(tokens, vec!["worker", "42", "started"]);
    }

    #[test]
    fn split_tokens_breaks_composite_tokens_when_enabled() {
        let tokens = split_tokens("10.0.0.1:8080", true, &[]);
        assert_eq!(
            tokens,
            vec!["10", ".", "0", ".", "0", ".", "1", ":", "8080"]
        );
    }

    #[test]
    fn split_tokens_preserves_identifiers_and_splits_double_colon() {
        // `-`/`_`/alphanumerics stay attached; only DELIMITERS split.
        let tokens = split_tokens("AppleCamIn::wakeEventHandlerThread", true, &[]);
        assert_eq!(
            tokens,
            vec!["AppleCamIn", ":", ":", "wakeEventHandlerThread"]
        );
        let tokens = split_tokens("pam_unix(sshd:auth):", true, &[]);
        assert_eq!(tokens, vec!["pam_unix", "(", "sshd", ":", "auth", ")", ":"]);
    }

    #[test]
    fn split_tokens_extra_delimiters_match_as_substrings() {
        let delimiters = vec!["||".to_string()];
        let tokens = split_tokens("a||b", false, &delimiters);
        assert_eq!(tokens, vec!["a", "||", "b"]);
    }

    #[test]
    fn split_tokens_maximal_munch_is_order_independent() {
        let short_first = vec!["|".to_string(), "||".to_string()];
        let long_first = vec!["||".to_string(), "|".to_string()];
        let expected = vec!["a", "||", "b", "|", "c"];
        assert_eq!(split_tokens("a||b|c", false, &short_first), expected);
        assert_eq!(split_tokens("a||b|c", false, &long_first), expected);
    }

    #[test]
    fn split_tokens_extra_delimiters_adjacent_and_at_edges() {
        let delimiters = vec!["::".to_string(), "=".to_string()];
        assert_eq!(
            split_tokens("::lead", false, &delimiters),
            vec!["::", "lead"]
        );
        assert_eq!(
            split_tokens("trail::", false, &delimiters),
            vec!["trail", "::"]
        );
        assert_eq!(
            split_tokens("a=b::c", false, &delimiters),
            vec!["a", "=", "b", "::", "c"]
        );
        assert_eq!(
            split_tokens("====", false, &delimiters),
            vec!["=", "=", "=", "="]
        );
    }

    #[test]
    fn split_tokens_never_splits_wildcard_token() {
        // Even a delimiter whose characters appear inside `<*>` must not
        // break the wildcard token produced by preprocessing.
        let delimiters = vec!["*".to_string()];
        assert_eq!(
            split_tokens("worker <*> done", false, &delimiters),
            vec!["worker", "<*>", "done"]
        );
        assert_eq!(split_tokens("a*b", false, &delimiters), vec!["a", "*", "b"]);
    }

    #[test]
    fn split_tokens_empty_extras_matches_legacy_behavior() {
        // With an empty extra-delimiter list the output must be identical to
        // the pre-ADR-0042 boolean-only tokenizer.
        assert_eq!(split_tokens("a:b,c", false, &[]), vec!["a:b,c"]);
        assert_eq!(
            split_tokens("a:b,c", true, &[]),
            vec!["a", ":", "b", ",", "c"]
        );
        assert_eq!(
            split_tokens("worker <*> done", true, &[]),
            vec!["worker", "<*>", "done"]
        );
        assert!(split_tokens("", true, &[]).is_empty());
    }

    #[test]
    fn split_tokens_union_of_fixed_set_and_extra_delimiters() {
        // Multi-character extras take precedence over single fixed-set
        // characters at the same position; the union applies elsewhere.
        let delimiters = vec!["::".to_string(), "|".to_string()];
        let tokens = split_tokens("a::b=1|2", true, &delimiters);
        assert_eq!(tokens, vec!["a", "::", "b", "=", "1", "|", "2"]);
    }
}

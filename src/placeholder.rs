//! Internal typed placeholder classification for the lazy placeholder pipeline.
//!
//! This module is an internal implementation detail of
//! [ADR-0033](../docs/adr/0033-typed-lazy-placeholder-pipeline.md). It recognizes
//! generic token shapes (`Number`, `Hex`, `Id`) so that routing and lazy
//! matching can ask "what placeholder kind, if any, does this raw token text
//! carry?" at candidate time, instead of erasing the kind through eager
//! full-line regex replacement.
//!
//! The module is `#[doc(hidden)]` and intentionally not re-exported from
//! [`crate`]: it is unstable internal API. It is exposed at the path level only
//! so the staged ADR-0033 work packages can land clippy-clean while their
//! consumers (WP2 runtime metadata, WP4 lazy matching) are still being wired;
//! it must not be treated as part of the public surface.
//!
//! # Design constraints
//!
//! - Side-effect free and independent from [`crate::model::Token`] and from the
//!   [`crate::preprocessor`] catalog. WP1 intentionally does not wire the
//!   classifier into training or matching.
//! - The kind set is generic: it describes token *shape*, never a log family,
//!   product, host, user, or fixture string.
//! - The predicates do not copy the legacy `Numerical` regex wholesale. They are
//!   hand-written ASCII scanners that split numeric, hex-prefixed, and
//!   long-hex/UUID identifier shapes into separate kinds.
//! - Open decisions deferred by ADR-0033 (scientific `Number`, exact `Id`
//!   length and character set) are resolved conservatively here and documented
//!   per predicate.

/// Internal placeholder kind for a single template position.
///
/// `None` (at the call site, via [`Option<PlaceholderKind>`]) means "untyped
/// wildcard/literal under existing semantics" — not "unknown but probably typed".
/// See the metadata join rule in `design.md`.
#[derive(
    Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, serde::Serialize, serde::Deserialize,
)]
pub enum PlaceholderKind {
    /// Signed ASCII integer or decimal, e.g. `42`, `-7`, `+100`, `1.5`.
    Number,
    /// `0x`/`0X`-prefixed hexadecimal, e.g. `0x1A2b`, `0Xff`.
    Hex,
    /// Sufficiently long hexadecimal/UUID identifier, e.g. `deadbeef`, `a1b2c3d4`.
    Id,
    /// URL: contains `://` or starts with `www.`, e.g. `http://example.com:8080/p`.
    Url,
    /// Filesystem path: starts with `/`, `~/`, `./`, or `../`, e.g. `/usr/local/bin`.
    Path,
    /// IPv4 dotted quad (each octet 0-255), optionally with `:port`, e.g. `10.0.0.1`, `10.0.0.1:8080`.
    Ipv4,
    /// IPv6 address (contains `::` or 8 colon-hex groups), e.g. `fe80::1`.
    Ipv6,
    /// MAC address: 6 colon/dash-separated hex pairs, e.g. `00:1a:2b:3c:4d:5e`.
    Mac,
    /// Date: `YYYY-MM-DD`, `YYYY/MM/DD`, `DD-Mon-YYYY`, `MM/DD/YYYY`, etc.
    Date,
    /// Time: `HH:MM[:SS]` or `HH:MM:SS.mmm`, e.g. `10:00:01`.
    Time,
    /// Duration with time units: `1h30m`, `1.5s`, `500ms`, `1d2h`.
    Duration,
    /// Size with byte unit: `1.5KB`, `1024B`, `1MB`.
    Size,
}

impl PlaceholderKind {
    /// Classify a raw token into a placeholder kind, or `None` for a literal.
    ///
    /// The predicates are disjoint, so the result is independent of evaluation
    /// order. An empty token, the `<*>` wildcard marker, `NaN`, and any token
    /// with a non-hex/non-digit character outside the accepted shapes return
    /// `None`.
    pub fn classify(token: &str) -> Option<Self> {
        if token.is_empty()
            || token.len() > MAX_CLASSIFIABLE_TOKEN_BYTES
            || !has_classifiable_shape(token)
        {
            return None;
        }
        // Priority order (most specific first) so each token classifies to at
        // most one kind; overlaps resolve by specificity (e.g. `10.0.0.1:8080`
        // → Ipv4 not Path; `2026-06-25` → Date not Number; `00:1a:…` → Mac not
        // Ipv6).
        if is_hex_literal(token) {
            return Some(Self::Hex);
        }
        if is_number(token) {
            return Some(Self::Number);
        }
        if is_url(token) {
            return Some(Self::Url);
        }
        if is_ipv4(token) {
            return Some(Self::Ipv4);
        }
        if is_mac(token) {
            return Some(Self::Mac);
        }
        if is_ipv6(token) {
            return Some(Self::Ipv6);
        }
        if is_path(token) {
            return Some(Self::Path);
        }
        if is_date(token) {
            return Some(Self::Date);
        }
        if is_time(token) {
            return Some(Self::Time);
        }
        if is_duration(token) {
            return Some(Self::Duration);
        }
        if is_size(token) {
            return Some(Self::Size);
        }
        if is_uuid(token) || is_long_hex_id(token) {
            return Some(Self::Id);
        }
        None
    }

    /// Run the predicate for this kind against an observed token.
    ///
    /// This is the lazy-match primitive used by the typed matcher: a template
    /// slot that has learned `self` accepts an observed token only when this
    /// returns `true`. A predicate failure rejects the candidate for that slot;
    /// it must not silently degrade into an untyped wildcard match.
    pub fn matches(self, observed: &str) -> bool {
        match self {
            Self::Number => is_number(observed),
            Self::Hex => is_hex_literal(observed),
            Self::Id => is_uuid(observed) || is_long_hex_id(observed),
            Self::Url => is_url(observed),
            Self::Path => is_path(observed),
            Self::Ipv4 => is_ipv4(observed),
            Self::Ipv6 => is_ipv6(observed),
            Self::Mac => is_mac(observed),
            Self::Date => is_date(observed),
            Self::Time => is_time(observed),
            Self::Duration => is_duration(observed),
            Self::Size => is_size(observed),
        }
    }
}

/// Internal per-position placeholder metadata, aligned with a token sequence.
///
/// Each entry corresponds to one token position: `Some(kind)` records that the
/// position has only ever been observed as `kind`, while `None` means "untyped
/// wildcard/literal under existing semantics" — either the position was never
/// typed, or conflicting observations downgraded it (see [`join`]).
///
/// This is internal ADR-0033 metadata. It is never rendered: the public
/// [`crate::model::Token`] surface and template output stay `<*>`.
pub type PlaceholderMask = Vec<Option<PlaceholderKind>>;

/// A variable span embedded inside an otherwise literal token.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct EmbeddedPlaceholder<'a> {
    /// Literal bytes before the variable span.
    pub(crate) head: &'a str,
    /// Classified shape of the variable span, or `None` for an untyped wildcard.
    pub(crate) kind: Option<PlaceholderKind>,
    /// Literal bytes after the variable span.
    pub(crate) tail: &'a str,
}

/// Classify a delimiter-bounded variable span inside `token`.
///
/// PIPLUP keeps whitespace tokenization, so embedded fields such as
/// `value=123` must be represented as `value=<*>` rather than split into more
/// tokens. This stays generic by requiring stable delimiter evidence around the
/// classified span; digit-bearing constants like `http2` and `version=http2`
/// remain literals because the alphanumeric suffix itself does not classify.
/// Compound spans that have no single [`PlaceholderKind`] predicate may still
/// become untyped wildcards, but must record `kind = None`.
pub(crate) fn classify_embedded(token: &str) -> Option<EmbeddedPlaceholder<'_>> {
    if token.len() < 2 || token.len() > MAX_CLASSIFIABLE_TOKEN_BYTES || token == "<*>" {
        return None;
    }
    let shape = EmbeddedShape::scan(token);
    if !shape.has_candidate {
        return None;
    }
    let mut bounds = None;

    if shape.has_hash {
        let (starts, ends) = embedded_bounds(token, &mut bounds);
        if let Some(embedded) = classify_embedded_number_chain(token, starts, ends) {
            return Some(embedded);
        }
    }
    if token.len() >= UUID_LEN
        && shape.has_hyphen
        && let Some(embedded) = classify_embedded_uuid(token)
    {
        return Some(embedded);
    }
    if shape.has_slash && shape.has_start_delimiter && shape.has_end_delimiter {
        let (starts, ends) = embedded_bounds(token, &mut bounds);
        if let Some(embedded) = classify_embedded_slash_compound(token, starts, ends) {
            return Some(embedded);
        }
    }
    if !shape.has_embedded_trigger(token) {
        return None;
    }

    let (starts, ends) = embedded_bounds(token, &mut bounds);
    longest_embedded(token, starts, ends, |span| {
        if span.len() == token.len() {
            None
        } else {
            PlaceholderKind::classify(span).map(Some)
        }
    })
}

/// Join two placeholder-kind observations for the same template position.
///
/// Implements the ADR-0033 metadata join rule:
///
/// - `join(Some(a), Some(b))` is `Some(a)` when `a == b`, otherwise `None`;
/// - any pairing involving `None` yields `None`.
///
/// `None` is therefore sticky: a conflicting or untyped observation downgrades
/// the position to untyped rather than inventing a kind after a destructive
/// rewrite or merge. This is the conservative first-implementation policy; a
/// future policy may keep conflicting kinds in separate clusters instead.
pub fn join(
    existing: Option<PlaceholderKind>,
    observed: Option<PlaceholderKind>,
) -> Option<PlaceholderKind> {
    match (existing, observed) {
        (Some(a), Some(b)) if a == b => Some(a),
        _ => None,
    }
}

/// Classify an observed token for typed matching.
///
/// Whole-token classification wins; delimiter-bounded embedded spans contribute
/// their kind only when the embedded span has one predicate-aligned kind.
pub(crate) fn classify_observed_kind(token: &str) -> Option<PlaceholderKind> {
    PlaceholderKind::classify(token).or_else(|| classify_embedded(token).and_then(|span| span.kind))
}

/// Return whether a template mask slot matches a pre-classified observation.
pub(crate) fn typed_slot_matches(
    mask: &[Option<PlaceholderKind>],
    observed_kinds: &[Option<PlaceholderKind>],
    index: usize,
) -> bool {
    typed_slot_matches_kind(mask, index, observed_kinds.get(index).copied().flatten())
}

/// Return whether a template mask slot matches one observed kind.
pub(crate) fn typed_slot_matches_kind(
    mask: &[Option<PlaceholderKind>],
    index: usize,
    observed_kind: Option<PlaceholderKind>,
) -> bool {
    match mask.get(index).copied().flatten() {
        Some(kind) => observed_kind == Some(kind),
        None => false,
    }
}

/// Maintain a placeholder mask aligned with a template sequence.
///
/// The caller supplies the wildcard predicate for its token representation.
/// Matching wildcard positions join their existing mask entry with the observed
/// kind, downgrading conflicts to `None` via [`join`].
pub(crate) fn maintain_mask_for_template<T>(
    mask: &mut PlaceholderMask,
    template: &[T],
    observed_kinds: &[Option<PlaceholderKind>],
    is_wildcard: impl Fn(&T) -> bool,
) {
    mask.resize(template.len(), None);
    for (i, token) in template.iter().enumerate() {
        if is_wildcard(token) {
            let observed = observed_kinds.get(i).copied().flatten();
            mask[i] = join(mask[i], observed);
        }
    }
}

/// Minimum byte length for an [`PlaceholderKind::Id`] token.
///
/// Eight is a conservative threshold: it covers common 32-bit hex values and
/// longer identifiers while excluding four-letter all-hex English words such as
/// `cafe`, `beef`, `dead`, and `face`. The exact threshold is a deferred open
/// decision in ADR-0033 and is validated by the WP0/WP5 golden survey before
/// default enablement.
const ID_MIN_LEN: usize = 8;

/// Maximum token length (bytes) the typed classifier attempts to classify.
/// Longer tokens always classify as literal (`None`): no shape predicate
/// needs more than this, so the guard avoids wasted O(n) scans on
/// pathological long tokens (e.g. base64 blobs).
const MAX_CLASSIFIABLE_TOKEN_BYTES: usize = 512;

fn has_classifiable_shape(token: &str) -> bool {
    let bytes = token.as_bytes();
    let mut has_digit_or_delimiter_shape = false;
    let mut all_hex = bytes.len() >= ID_MIN_LEN;
    let mut has_hex_letter = false;

    for byte in bytes {
        if byte.is_ascii_digit() || matches!(*byte, b':' | b'/' | b'.' | b'-' | b'+' | b'~') {
            has_digit_or_delimiter_shape = true;
        }
        if all_hex {
            if byte.is_ascii_hexdigit() {
                has_hex_letter |= matches!(*byte, b'a'..=b'f' | b'A'..=b'F');
            } else {
                all_hex = false;
            }
        }
    }

    has_digit_or_delimiter_shape || (all_hex && has_hex_letter)
}

#[derive(Debug, Default)]
struct EmbeddedShape {
    has_candidate: bool,
    has_digit: bool,
    has_hash: bool,
    has_slash: bool,
    has_hyphen: bool,
    has_start_delimiter: bool,
    has_end_delimiter: bool,
    first_can_start: bool,
}

impl EmbeddedShape {
    fn scan(token: &str) -> Self {
        let mut shape = Self::default();
        for (index, ch) in token.char_indices() {
            if index == 0 {
                shape.first_can_start = can_start_placeholder_span(ch);
            }
            if ch.is_ascii_digit() {
                shape.has_digit = true;
                shape.has_candidate = true;
            }
            if ch == '#' {
                shape.has_hash = true;
                shape.has_candidate = true;
            }
            if ch == '/' {
                shape.has_slash = true;
                shape.has_candidate = true;
            }
            if ch == '-' {
                shape.has_hyphen = true;
                shape.has_candidate = true;
            }
            if is_span_start_delimiter(ch) {
                shape.has_start_delimiter = true;
                shape.has_candidate = true;
            }
            if is_span_end_delimiter(ch) {
                shape.has_end_delimiter = true;
                shape.has_candidate = true;
            }
            if matches!(ch, '.' | '+' | '~') {
                shape.has_candidate = true;
            }
        }
        shape
    }

    fn has_embedded_trigger(&self, token: &str) -> bool {
        let has_placeholder_shape =
            self.has_digit || self.has_slash || starts_with_ignore_ascii(token, "www.");
        has_placeholder_shape
            && (self.has_start_delimiter || (self.first_can_start && self.has_end_delimiter))
    }
}

fn embedded_starts(token: &str) -> Vec<usize> {
    let mut starts = Vec::with_capacity(4);
    starts.push(0);
    for (index, ch) in token.char_indices() {
        let next = index + ch.len_utf8();
        if next < token.len() && is_span_start_delimiter(ch) {
            starts.push(next);
        }
    }
    starts.sort_unstable();
    starts.dedup();
    starts
}

fn embedded_ends(token: &str) -> Vec<usize> {
    let mut ends = Vec::with_capacity(4);
    ends.push(token.len());
    for (index, ch) in token.char_indices() {
        if index > 0 && is_span_end_delimiter(ch) {
            ends.push(index);
        }
    }
    ends.sort_unstable();
    ends.dedup();
    ends
}

fn is_span_start_delimiter(ch: char) -> bool {
    matches!(
        ch,
        '=' | ':' | '(' | '[' | '{' | '<' | ',' | ';' | '|' | '"' | '\''
    )
}

fn is_span_end_delimiter(ch: char) -> bool {
    matches!(
        ch,
        ':' | ',' | ';' | ')' | ']' | '}' | '>' | '"' | '\'' | '|'
    )
}

fn can_start_placeholder_span(ch: char) -> bool {
    ch.is_ascii_digit() || matches!(ch, '+' | '-' | '/' | '~' | '.')
}

fn starts_with_ignore_ascii(token: &str, prefix: &str) -> bool {
    token
        .get(..prefix.len())
        .is_some_and(|head| head.eq_ignore_ascii_case(prefix))
}

fn embedded_bounds<'a>(
    token: &str,
    bounds: &'a mut Option<(Vec<usize>, Vec<usize>)>,
) -> (&'a [usize], &'a [usize]) {
    let (starts, ends) =
        bounds.get_or_insert_with(|| (embedded_starts(token), embedded_ends(token)));
    (starts.as_slice(), ends.as_slice())
}

fn longest_embedded<'a>(
    token: &'a str,
    starts: &[usize],
    ends: &[usize],
    accept: impl Fn(&str) -> Option<Option<PlaceholderKind>>,
) -> Option<EmbeddedPlaceholder<'a>> {
    // `accept` is tri-state: reject, accept untyped, or accept with a kind.
    let mut best = None;
    let mut best_len = 0usize;

    for &start in starts {
        for &end in ends {
            if start >= end {
                continue;
            }
            let span = &token[start..end];
            let Some(kind) = accept(span) else { continue };
            let span_len = end - start;
            if span_len > best_len {
                best_len = span_len;
                best = Some(EmbeddedPlaceholder {
                    head: &token[..start],
                    kind,
                    tail: &token[end..],
                });
            }
        }
    }

    best
}

fn classify_embedded_number_chain<'a>(
    token: &'a str,
    starts: &[usize],
    ends: &[usize],
) -> Option<EmbeddedPlaceholder<'a>> {
    longest_embedded(token, starts, ends, |span| {
        is_delimited_number_chain(span).then_some(None)
    })
}

fn is_delimited_number_chain(token: &str) -> bool {
    if token.starts_with('#') || token.ends_with('#') || !token.contains('#') {
        return false;
    }
    let mut parts = 0usize;
    for part in token.split('#').filter(|part| !part.is_empty()) {
        if !is_number(part) {
            return false;
        }
        parts += 1;
    }
    parts >= 2
}

fn classify_embedded_slash_compound<'a>(
    token: &'a str,
    starts: &[usize],
    ends: &[usize],
) -> Option<EmbeddedPlaceholder<'a>> {
    longest_embedded(token, starts, ends, |span| {
        is_classified_slash_sequence(span).then_some(None)
    })
}

fn is_classified_slash_sequence(token: &str) -> bool {
    if token.starts_with('/') || token.ends_with('/') || !token.contains('/') {
        return false;
    }
    let mut parts = 0usize;
    let mut has_non_number_part = false;
    for part in token.split('/') {
        let Some(kind) = PlaceholderKind::classify(part) else {
            return false;
        };
        has_non_number_part |= kind != PlaceholderKind::Number;
        parts += 1;
    }
    parts >= 2 && has_non_number_part
}

fn classify_embedded_uuid(token: &str) -> Option<EmbeddedPlaceholder<'_>> {
    for start in embedded_uuid_starts(token) {
        let end = start + UUID_LEN;
        if end > token.len() || !token.is_char_boundary(end) || (start == 0 && end == token.len()) {
            continue;
        }
        if end < token.len() {
            let Some(tail) = token[end..].chars().next() else {
                continue;
            };
            if !is_span_end_delimiter(tail) {
                continue;
            }
        }
        let span = &token[start..end];
        if is_uuid(span) {
            return Some(EmbeddedPlaceholder {
                head: &token[..start],
                kind: Some(PlaceholderKind::Id),
                tail: &token[end..],
            });
        }
    }

    None
}

fn embedded_uuid_starts(token: &str) -> Vec<usize> {
    let mut starts = embedded_starts(token);
    for (index, ch) in token.char_indices() {
        let next = index + ch.len_utf8();
        if next < token.len() && ch == '-' {
            starts.push(next);
        }
    }
    starts.sort_unstable();
    starts.dedup();
    starts
}

/// `0x`/`0X` followed by one or more ASCII hex digits.
fn is_hex_literal(token: &str) -> bool {
    let Some(rest) = token
        .strip_prefix("0x")
        .or_else(|| token.strip_prefix("0X"))
    else {
        return false;
    };
    !rest.is_empty() && rest.bytes().all(|b| b.is_ascii_hexdigit())
}

/// Optional leading `+`/`-` followed by ASCII digits, optionally with one
/// decimal fraction (`1.5`). Scientific notation and leading-dot/trailing-dot
/// forms stay literal until their stability tradeoff is surveyed separately.
fn is_number(token: &str) -> bool {
    let bytes = token.as_bytes();
    let Some((first, rest)) = bytes.split_first() else {
        return false;
    };
    let digits = match *first {
        b'+' | b'-' => rest,
        _ => bytes,
    };
    if digits.is_empty() {
        return false;
    }

    let mut seen_dot = false;
    let mut whole_digits = 0usize;
    let mut fraction_digits = 0usize;
    for byte in digits {
        match byte {
            b'.' if !seen_dot => seen_dot = true,
            b if b.is_ascii_digit() && seen_dot => fraction_digits += 1,
            b if b.is_ascii_digit() => whole_digits += 1,
            _ => return false,
        }
    }

    whole_digits > 0 && (!seen_dot || fraction_digits > 0)
}

/// All-hex token of length >= [`ID_MIN_LEN`] containing at least one hex letter.
fn is_long_hex_id(token: &str) -> bool {
    if token.len() < ID_MIN_LEN {
        return false;
    }
    let bytes = token.as_bytes();
    if !bytes.iter().all(|b| b.is_ascii_hexdigit()) {
        return false;
    }
    // Require at least one hex letter so a pure decimal number stays `Number`.
    bytes.iter().any(|b| matches!(b, b'a'..=b'f' | b'A'..=b'F'))
}

const UUID_LEN: usize = 36;

/// RFC-4122-style hyphenated hexadecimal identifier.
fn is_uuid(token: &str) -> bool {
    let bytes = token.as_bytes();
    if bytes.len() != UUID_LEN {
        return false;
    }

    for (index, byte) in bytes.iter().enumerate() {
        if matches!(index, 8 | 13 | 18 | 23) {
            if *byte != b'-' {
                return false;
            }
        } else if !byte.is_ascii_hexdigit() {
            return false;
        }
    }
    true
}

/// URL: contains `://` or starts with `www.` (case-insensitive).
fn is_url(token: &str) -> bool {
    if token.contains("://") {
        return true;
    }
    let bytes = token.as_bytes();
    bytes.len() >= 4
        && (bytes[0] == b'w' || bytes[0] == b'W')
        && (bytes[1] == b'w' || bytes[1] == b'W')
        && (bytes[2] == b'w' || bytes[2] == b'W')
        && bytes[3] == b'.'
}

/// Filesystem path: starts with `/`, `~/`, `./`, or `../`.
fn is_path(token: &str) -> bool {
    token.starts_with('/')
        || token.starts_with("~/")
        || token.starts_with("./")
        || token.starts_with("../")
}

/// IPv4 dotted quad (each octet 0-255), optionally with a `:port` suffix.
fn is_ipv4(token: &str) -> bool {
    let addr = match token.split_once(':') {
        None => token,
        Some((a, p)) => {
            if p.is_empty() || !p.bytes().all(|b| b.is_ascii_digit()) {
                return false;
            }
            a
        }
    };
    let mut count = 0;
    let mut all_valid = true;
    for octet in addr.split('.') {
        count += 1;
        if !is_ipv4_octet(octet) {
            all_valid = false;
        }
    }
    count == 4 && all_valid
}

fn is_ipv4_octet(o: &str) -> bool {
    !o.is_empty()
        && o.len() <= 3
        && o.bytes().all(|b| b.is_ascii_digit())
        && o.parse::<u32>().is_ok_and(|n| n <= 255)
}

/// MAC address: exactly 6 colon- or dash-separated hex pairs (each 2 hex chars).
fn is_mac(token: &str) -> bool {
    let mut groups = token.split([':', '-']);
    let mut count = 0;
    let mut all_valid = true;
    for g in groups.by_ref() {
        count += 1;
        if !(g.len() == 2 && g.bytes().all(|b| b.is_ascii_hexdigit())) {
            all_valid = false;
        }
    }
    count == 6 && all_valid
}

/// IPv6 address: contains `::` OR has exactly 8 colon-separated hex groups
/// (1-4 hex chars each). `::` produces empty groups, which are allowed.
fn is_ipv6(token: &str) -> bool {
    let has_double_colon = token.contains("::");
    let mut count = 0;
    let mut all_valid = true;
    for g in token.split(':') {
        count += 1;
        if !(g.is_empty() || (g.len() <= 4 && g.bytes().all(|b| b.is_ascii_hexdigit()))) {
            all_valid = false;
        }
    }
    all_valid && (has_double_colon || count == 8)
}

/// Date: `YYYY-MM-DD`, `YYYY/MM/DD`, `DD-Mon-YYYY`, `MM/DD/YYYY`-style shapes
/// (3 parts split by a single repeated separator `-` or `/`).
fn is_date(token: &str) -> bool {
    let sep = if token.contains('-') {
        '-'
    } else if token.contains('/') {
        '/'
    } else {
        return false;
    };
    if token.matches(sep).count() != 2 {
        return false;
    }
    let parts: Vec<&str> = token.split(sep).collect();
    if parts.len() != 3 {
        return false;
    }
    let (a, b, c) = (parts[0], parts[1], parts[2]);
    let digits = |s: &str| !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit());
    let alpha3 = |s: &str| s.len() == 3 && s.bytes().all(|b| b.is_ascii_alphabetic());
    // YYYY-MM-DD
    if a.len() == 4 && digits(a) && b.len() == 2 && digits(b) && c.len() == 2 && digits(c) {
        return true;
    }
    // DD-Mon-YYYY
    if a.len() == 2 && digits(a) && alpha3(b) && c.len() == 4 && digits(c) {
        return true;
    }
    // MM/DD/YYYY or DD-MM-YYYY
    if a.len() == 2 && digits(a) && b.len() == 2 && digits(b) && c.len() == 4 && digits(c) {
        return true;
    }
    false
}

/// Time: `HH:MM` or `HH:MM:SS`, optionally with a `.mmm` fractional suffix.
fn is_time(token: &str) -> bool {
    let main = token.split_once('.').map(|(m, _)| m).unwrap_or(token);
    let mut count = 0;
    let mut all_valid = true;
    for p in main.split(':') {
        count += 1;
        if !(p.len() <= 2 && !p.is_empty() && p.bytes().all(|b| b.is_ascii_digit())) {
            all_valid = false;
        }
    }
    (count == 2 || count == 3) && all_valid
}

/// Duration: number-led, all alpha chars are time units (h/m/s/d/u/n), at least
/// one digit and one unit, no colon. Matches `1h30m`, `1.5s`, `500ms`, `1d2h`;
/// rejects letter-led (`http2`) and digit-led words with non-unit letters (`3des`).
fn is_duration(token: &str) -> bool {
    let bytes = token.as_bytes();
    let is_time_unit = |b: &u8| {
        matches!(
            b,
            b'h' | b'H' | b'm' | b'M' | b's' | b'S' | b'd' | b'D' | b'u' | b'U' | b'n' | b'N'
        )
    };
    let starts_numeric = bytes
        .first()
        .is_some_and(|b| b.is_ascii_digit() || *b == b'.');
    let has_digit = bytes.iter().any(|b| b.is_ascii_digit());
    let has_time_unit = bytes.iter().any(is_time_unit);
    starts_numeric
        && has_digit
        && has_time_unit
        && !token.contains(':')
        && bytes
            .iter()
            .all(|b| b.is_ascii_digit() || *b == b'.' || is_time_unit(b))
}

/// Size: digits (optionally decimal) followed by a byte-unit suffix
/// (`B`/`KB`/`MB`/`GB`/`TB`/`PB`, case-insensitive).
fn is_size(token: &str) -> bool {
    let has_digit = token.bytes().any(|b| b.is_ascii_digit());
    if !has_digit {
        return false;
    }
    for suf in ["PB", "TB", "GB", "MB", "KB", "B"] {
        if let Some(prefix) = strip_suffix_ignore_ascii(token, suf) {
            return !prefix.is_empty() && prefix.bytes().all(|b| b.is_ascii_digit() || b == b'.');
        }
    }
    false
}

fn strip_suffix_ignore_ascii<'a>(token: &'a str, suf: &str) -> Option<&'a str> {
    let suf_len = suf.len();
    if token.len() < suf_len {
        return None;
    }
    let split = token.len() - suf_len;
    // Guard against slicing at a non-char boundary: a trailing multi-byte
    // UTF-8 sequence (e.g. `1é`) would otherwise panic on `&token[split..]`.
    // Such a token cannot end in an ASCII byte-unit suffix, so return `None`.
    if !token.is_char_boundary(split) {
        return None;
    }
    let tail = &token[split..];
    tail.eq_ignore_ascii_case(suf).then(|| &token[..split])
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Assert `classify` returns `None` for every supplied literal token.
    fn assert_literals(tokens: &[&str]) {
        for token in tokens {
            assert_eq!(
                PlaceholderKind::classify(token),
                None,
                "expected {token:?} to be a literal"
            );
        }
    }

    fn embedded_span<'a>(token: &'a str, span: EmbeddedPlaceholder<'_>) -> &'a str {
        assert!(
            token.starts_with(span.head),
            "{token:?} should start with {:?}",
            span.head
        );
        assert!(
            token.ends_with(span.tail),
            "{token:?} should end with {:?}",
            span.tail
        );
        let end = token.len() - span.tail.len();
        &token[span.head.len()..end]
    }

    #[test]
    fn classifies_signed_integers_as_number() {
        for token in ["0", "7", "42", "007", "100", "-7", "+100", "-0", "+0"] {
            assert_eq!(
                PlaceholderKind::classify(token),
                Some(PlaceholderKind::Number),
                "{token:?}"
            );
        }
    }

    #[test]
    fn classifies_decimal_numbers_as_number() {
        for token in ["1.5", "-3.14", "+2.0", "620701.011328"] {
            assert_eq!(
                PlaceholderKind::classify(token),
                Some(PlaceholderKind::Number),
                "{token:?}"
            );
        }
    }

    #[test]
    fn classifies_hex_prefixed_tokens_as_hex() {
        for token in ["0x0", "0Xff", "0x1A2b", "0xDEADBEEF", "0xabc123"] {
            assert_eq!(
                PlaceholderKind::classify(token),
                Some(PlaceholderKind::Hex),
                "{token:?}"
            );
        }
    }

    #[test]
    fn classifies_long_hex_identifiers_as_id() {
        for token in ["deadbeef", "a1b2c3d4", "ABCDEF12", "cafebabe", "0000dead"] {
            assert_eq!(
                PlaceholderKind::classify(token),
                Some(PlaceholderKind::Id),
                "{token:?}"
            );
        }
    }

    #[test]
    fn classifies_uuid_identifiers_as_id() {
        for token in [
            "38101a0b-2096-447d-96ea-a692162415ae",
            "b9000564-fe1a-409b-b8cc-1e88b294cd1d",
        ] {
            assert_eq!(
                PlaceholderKind::classify(token),
                Some(PlaceholderKind::Id),
                "{token:?}"
            );
        }
    }

    #[test]
    fn rejects_required_stable_constants() {
        // ADR-0033 explicitly requires these stable protocol/encoding/media
        // constants to remain literals. They contain non-hex letters and so
        // fail every predicate.
        assert_literals(&["utf8", "http2", "mp3", "iso8859"]);
    }

    #[test]
    fn rejects_other_digit_bearing_stable_constants() {
        // Generic protocol/encoding/media/format constants that contain digits
        // but are stable literals, not variable placeholders.
        assert_literals(&[
            "h264", "mp4", "aac", "pcie", "sata3", "usb2", "wifi6", "ipv4", "ipv6", "tls1", "tcp3",
            "x86", "arm64",
        ]);
    }

    #[test]
    fn rejects_short_all_hex_words() {
        // Below ID_MIN_LEN, even all-hex tokens stay literal so English words
        // like "cafe", "beef", "dead", "face" are not over-generalized.
        assert_literals(&[
            "cafe", "beef", "dead", "face", "babe", "fade", "abcd", "abcdef",
        ]);
    }

    #[test]
    fn rejects_pure_decimal_above_id_threshold() {
        // A long pure-decimal token is `Number`, not `Id`, because it carries
        // no hex letter.
        for token in ["12345678", "00000000", "99999999"] {
            assert_eq!(
                PlaceholderKind::classify(token),
                Some(PlaceholderKind::Number),
                "{token:?}"
            );
        }
    }

    #[test]
    fn rejects_empty_and_marker_inputs() {
        assert_literals(&["", "<*>", "NaN", "null", "true", "false"]);
    }

    // ── MAX_CLASSIFIABLE_TOKEN_BYTES guard ───────────────────────────────────

    #[test]
    fn rejects_long_pure_number_tokens_as_literal() {
        // A 600-digit decimal string exceeds MAX_CLASSIFIABLE_TOKEN_BYTES and
        // must classify as a literal instead of paying for an O(n) Number scan.
        let token = "9".repeat(600);
        assert_eq!(token.len(), 600);
        assert_eq!(PlaceholderKind::classify(&token), None);
    }

    #[test]
    fn rejects_long_hex_tokens_as_literal() {
        let token = "ab".repeat(300);
        assert_eq!(token.len(), 600);
        assert_eq!(PlaceholderKind::classify(&token), None);
    }

    #[test]
    fn still_classifies_valid_shapes_within_limit() {
        // Regression: tokens under the guard keep classifying as before.
        assert_eq!(
            PlaceholderKind::classify("10.0.0.1"),
            Some(PlaceholderKind::Ipv4)
        );
        assert_eq!(
            PlaceholderKind::classify("2026-06-25"),
            Some(PlaceholderKind::Date)
        );
    }

    #[test]
    fn classifies_token_exactly_at_limit_as_number() {
        // The guard uses strict `>`, so a token of exactly
        // MAX_CLASSIFIABLE_TOKEN_BYTES bytes still classifies.
        let token = "7".repeat(MAX_CLASSIFIABLE_TOKEN_BYTES);
        assert_eq!(token.len(), MAX_CLASSIFIABLE_TOKEN_BYTES);
        assert_eq!(
            PlaceholderKind::classify(&token),
            Some(PlaceholderKind::Number)
        );
    }

    #[test]
    fn rejects_long_embedded_tokens_as_literal() {
        let token = format!("key={}", "9".repeat(600));
        assert!(token.len() > MAX_CLASSIFIABLE_TOKEN_BYTES);
        assert_eq!(classify_embedded(&token), None);
    }

    #[test]
    fn classifies_delimiter_bounded_embedded_spans() {
        let span = classify_embedded("value=123").expect("number span");
        assert_eq!(span.head, "value=");
        assert_eq!(span.kind, Some(PlaceholderKind::Number));
        assert_eq!(span.tail, "");

        let span = classify_embedded("pid[42],").expect("punctuated number span");
        assert_eq!(span.head, "pid[");
        assert_eq!(span.kind, Some(PlaceholderKind::Number));
        assert_eq!(span.tail, "],");

        let span = classify_embedded("path=/tmp/app,").expect("path span");
        assert_eq!(span.head, "path=");
        assert_eq!(span.kind, Some(PlaceholderKind::Path));
        assert_eq!(span.tail, "");
    }

    #[test]
    fn embedded_spans_require_generic_shape_not_digit_bearing_constant() {
        for token in ["http2", "version=http2", "arch=arm64", "abc123xyz", "user1"] {
            assert_eq!(
                classify_embedded(token),
                None,
                "{token:?} should stay literal"
            );
        }
    }

    #[test]
    fn embedded_spans_classify_decimal_numbers_without_dot_splitting() {
        let span = classify_embedded("ts=620701.011328:").expect("decimal timestamp span");
        assert_eq!(span.head, "ts=");
        assert_eq!(span.kind, Some(PlaceholderKind::Number));
        assert_eq!(span.tail, ":");

        let span = classify_embedded("load=1.5").expect("decimal span");
        assert_eq!(span.head, "load=");
        assert_eq!(span.kind, Some(PlaceholderKind::Number));
        assert_eq!(span.tail, "");
    }

    #[test]
    fn embedded_spans_wildcard_dense_number_chains_untyped() {
        let span = classify_embedded("1514038440000##7007##548365").expect("number chain");
        assert_eq!(span.head, "");
        assert_eq!(span.kind, None);
        assert_eq!(span.tail, "");

        let span = classify_embedded("setSteps=1514038440000##7007##548365")
            .expect("anchored number chain");
        assert_eq!(span.head, "setSteps=");
        assert_eq!(span.kind, None);
        assert_eq!(span.tail, "");

        for token in ["abc#123", "#1#2", "1#2#", "1#abc"] {
            assert_eq!(
                classify_embedded(token),
                None,
                "{token:?} should stay literal"
            );
        }
    }

    #[test]
    fn embedded_spans_classify_prefixed_uuid() {
        let span =
            classify_embedded("req-38101a0b-2096-447d-96ea-a692162415ae").expect("uuid span");
        assert_eq!(span.head, "req-");
        assert_eq!(span.kind, Some(PlaceholderKind::Id));
        assert_eq!(span.tail, "");

        let span = classify_embedded("[instance:b9000564-fe1a-409b-b8cc-1e88b294cd1d]")
            .expect("uuid span");
        assert_eq!(span.head, "[instance:");
        assert_eq!(span.kind, Some(PlaceholderKind::Id));
        assert_eq!(span.tail, "]");
    }

    #[test]
    fn embedded_spans_wildcard_delimited_compound_identifiers_untyped() {
        let span = classify_embedded("agent[31702/0x7000002a0000]")
            .expect("slash-separated numeric/hex span");
        assert_eq!(span.head, "agent[");
        assert_eq!(span.kind, None);
        assert_eq!(span.tail, "]");

        let span = classify_embedded("0x7ff1b2d17980>:").expect("angle-delimited hex span");
        assert_eq!(span.head, "");
        assert_eq!(span.kind, Some(PlaceholderKind::Hex));
        assert_eq!(span.tail, ">:");

        for token in ["2017/7/1", "/1/0x2", "1/2", "pid/0xabc"] {
            assert_eq!(
                classify_embedded(token),
                None,
                "{token:?} should stay literal"
            );
        }
    }

    #[test]
    fn embedded_typed_spans_satisfy_matches_predicate() {
        for (token, expected_kind) in [
            ("1514038440000##7007##548365", None),
            ("setSteps=1514038440000##7007##548365", None),
            ("agent[31702/0x7000002a0000]", None),
            (
                "req-38101a0b-2096-447d-96ea-a692162415ae",
                Some(PlaceholderKind::Id),
            ),
            ("value=123", Some(PlaceholderKind::Number)),
            ("ts=620701.011328:", Some(PlaceholderKind::Number)),
        ] {
            let span = classify_embedded(token).expect("embedded span");
            assert_eq!(span.kind, expected_kind, "{token:?}");
            if let Some(kind) = span.kind {
                let observed = embedded_span(token, span);
                assert!(
                    kind.matches(observed),
                    "{token:?} returned typed {kind:?} for non-matching span {observed:?}"
                );
            }
        }
    }

    #[test]
    fn rejects_malformed_numeric_shapes() {
        assert_literals(&["1e10", "1E3", ".5", "1.", "0x", "0xG", "0x-", "-", "+"]);
    }

    #[test]
    fn rejects_mixed_alphanumeric_identifiers() {
        // Tokens with non-hex letters (r, e, q, s, ...) stay literal in the
        // conservative first implementation; catching `req-123` would require
        // allowing arbitrary alphanumerics, which re-introduces the `utf8`
        // over-generalization risk.
        assert_literals(&[
            "req-123",
            "req456",
            "txid_42",
            "blk_42",
            "abc123xyz",
            "user1",
        ]);
    }

    #[test]
    fn predicate_matches_accepts_kind_conforming_tokens() {
        assert!(PlaceholderKind::Number.matches("42"));
        assert!(PlaceholderKind::Number.matches("-7"));
        assert!(PlaceholderKind::Hex.matches("0x1A2b"));
        assert!(PlaceholderKind::Hex.matches("0Xff"));
        assert!(PlaceholderKind::Id.matches("deadbeef"));
        assert!(PlaceholderKind::Id.matches("a1b2c3d4"));
    }

    #[test]
    fn predicate_matches_rejects_non_conforming_tokens() {
        // A typed slot must reject an observed token that does not satisfy its
        // kind, rather than silently degrading to an untyped wildcard match.
        assert!(!PlaceholderKind::Number.matches("0x1a"));
        assert!(!PlaceholderKind::Number.matches("deadbeef"));
        assert!(PlaceholderKind::Number.matches("1.5"));
        assert!(!PlaceholderKind::Number.matches("1e10"));
        assert!(!PlaceholderKind::Hex.matches("deadbeef"));
        assert!(!PlaceholderKind::Hex.matches("0x"));
        assert!(!PlaceholderKind::Id.matches("42"));
        assert!(!PlaceholderKind::Id.matches("cafe"));
        assert!(!PlaceholderKind::Id.matches("utf8"));
    }

    #[test]
    fn predicates_are_disjoint_with_classify() {
        // For every kind, classify and the kind's own predicate agree on the
        // positive cases and the predicates do not overlap on shared inputs.
        for token in [
            "42", "-7", "0x1a", "0XFF", "deadbeef", "a1b2c3d4", "utf8", "1.5", "",
        ] {
            let kind = PlaceholderKind::classify(token);
            for candidate in [
                PlaceholderKind::Number,
                PlaceholderKind::Hex,
                PlaceholderKind::Id,
            ] {
                let predicate_passes = candidate.matches(token);
                if Some(candidate) == kind {
                    assert!(
                        predicate_passes,
                        "{token:?} classified as {candidate:?} but predicate failed"
                    );
                } else if kind.is_some() {
                    // A token with a definite kind must not pass another kind's predicate.
                    assert!(
                        !predicate_passes,
                        "{token:?} passed {candidate:?} but classified as {kind:?}"
                    );
                }
            }
        }
    }

    #[test]
    fn join_keeps_agreeing_kind() {
        assert_eq!(
            join(Some(PlaceholderKind::Number), Some(PlaceholderKind::Number)),
            Some(PlaceholderKind::Number)
        );
        assert_eq!(
            join(Some(PlaceholderKind::Hex), Some(PlaceholderKind::Hex)),
            Some(PlaceholderKind::Hex)
        );
        assert_eq!(
            join(Some(PlaceholderKind::Id), Some(PlaceholderKind::Id)),
            Some(PlaceholderKind::Id)
        );
    }

    #[test]
    fn join_downgrades_conflicting_kinds_to_untyped() {
        assert_eq!(
            join(Some(PlaceholderKind::Number), Some(PlaceholderKind::Hex)),
            None
        );
        assert_eq!(
            join(Some(PlaceholderKind::Id), Some(PlaceholderKind::Number)),
            None
        );
        assert_eq!(
            join(Some(PlaceholderKind::Hex), Some(PlaceholderKind::Id)),
            None
        );
    }

    #[test]
    fn join_is_sticky_on_none() {
        // Any None operand downgrades to None, even when the other side is
        // typed: an untyped or unknown observation must not lock in a kind.
        assert_eq!(join(Some(PlaceholderKind::Number), None), None);
        assert_eq!(join(None, Some(PlaceholderKind::Number)), None);
        assert_eq!(join(None, None), None);
    }

    // ── Extended kinds (Url/Path/Ipv4/Ipv6/Mac/Date/Time/Duration/Size) ──────

    #[test]
    fn classifies_urls() {
        for token in [
            "http://example.com:8080/p",
            "https://a.b/c?x=1",
            "www.example.com",
            "ftp://host",
        ] {
            assert_eq!(
                PlaceholderKind::classify(token),
                Some(PlaceholderKind::Url),
                "{token:?}"
            );
        }
        // Bare host (no scheme/www) is NOT a Url.
        assert_ne!(
            PlaceholderKind::classify("example.com"),
            Some(PlaceholderKind::Url)
        );
    }

    #[test]
    fn classifies_paths() {
        for token in ["/usr/local/bin", "~/file.txt", "./config", "../parent/x"] {
            assert_eq!(
                PlaceholderKind::classify(token),
                Some(PlaceholderKind::Path),
                "{token:?}"
            );
        }
    }

    #[test]
    fn classifies_ipv4() {
        for token in ["10.0.0.1", "10.0.0.1:8080", "255.255.255.255", "0.0.0.0"] {
            assert_eq!(
                PlaceholderKind::classify(token),
                Some(PlaceholderKind::Ipv4),
                "{token:?}"
            );
        }
        assert_eq!(PlaceholderKind::classify("256.0.0.1"), None);
        assert_eq!(PlaceholderKind::classify("1.2.3"), None);
        assert_eq!(PlaceholderKind::classify("10.0.0.1:abc"), None);
    }

    #[test]
    fn classifies_ipv6() {
        for token in ["fe80::1", "2001:db8::1", "::1", "2001:db8:0:0:0:0:0:1"] {
            assert_eq!(
                PlaceholderKind::classify(token),
                Some(PlaceholderKind::Ipv6),
                "{token:?}"
            );
        }
        // 3-segment colon is Time, not Ipv6.
        assert_ne!(
            PlaceholderKind::classify("10:00:01"),
            Some(PlaceholderKind::Ipv6)
        );
    }

    #[test]
    fn classifies_mac_addresses() {
        for token in [
            "00:1a:2b:3c:4d:5e",
            "00-1a-2b-3c-4d-5e",
            "AA:BB:CC:DD:EE:FF",
        ] {
            assert_eq!(
                PlaceholderKind::classify(token),
                Some(PlaceholderKind::Mac),
                "{token:?}"
            );
        }
        assert_ne!(
            PlaceholderKind::classify("00:1a:2b:3c:4d"),
            Some(PlaceholderKind::Mac)
        );
    }

    #[test]
    fn classifies_dates() {
        for token in [
            "2026-06-25",
            "2026/06/25",
            "25-Jun-2026",
            "06/25/2026",
            "25-12-2026",
        ] {
            assert_eq!(
                PlaceholderKind::classify(token),
                Some(PlaceholderKind::Date),
                "{token:?}"
            );
        }
    }

    #[test]
    fn classifies_times() {
        for token in ["10:00:01", "10:00", "10:00:01.123", "23:59:59"] {
            assert_eq!(
                PlaceholderKind::classify(token),
                Some(PlaceholderKind::Time),
                "{token:?}"
            );
        }
        assert_ne!(
            PlaceholderKind::classify("10:00:01:02"),
            Some(PlaceholderKind::Time)
        );
    }

    #[test]
    fn classifies_durations() {
        for token in ["1h30m", "1.5s", "500ms", "1d2h", "100us"] {
            assert_eq!(
                PlaceholderKind::classify(token),
                Some(PlaceholderKind::Duration),
                "{token:?}"
            );
        }
        // Letter-led or non-unit-letter digit-words stay literal (§6).
        assert_eq!(PlaceholderKind::classify("http2"), None);
        assert_eq!(PlaceholderKind::classify("3des"), None);
    }

    #[test]
    fn classifies_sizes() {
        for token in ["1.5KB", "1024B", "1MB", "2.5GB", "512TB"] {
            assert_eq!(
                PlaceholderKind::classify(token),
                Some(PlaceholderKind::Size),
                "{token:?}"
            );
        }
        assert_eq!(
            PlaceholderKind::classify("1.5s"),
            Some(PlaceholderKind::Duration)
        );
        assert_eq!(PlaceholderKind::classify("500K"), None);
    }

    #[test]
    fn classify_priority_resolves_overlaps() {
        assert_eq!(
            PlaceholderKind::classify("10.0.0.1:8080"),
            Some(PlaceholderKind::Ipv4)
        );
        assert_eq!(
            PlaceholderKind::classify("2026-06-25"),
            Some(PlaceholderKind::Date)
        );
        assert_eq!(
            PlaceholderKind::classify("00:1a:2b:3c:4d:5e"),
            Some(PlaceholderKind::Mac)
        );
        assert_eq!(
            PlaceholderKind::classify("fe80::1"),
            Some(PlaceholderKind::Ipv6)
        );
        assert_eq!(
            PlaceholderKind::classify("1.5KB"),
            Some(PlaceholderKind::Size)
        );
        assert_eq!(
            PlaceholderKind::classify("10:00:01"),
            Some(PlaceholderKind::Time)
        );
    }

    #[test]
    fn extended_kinds_keep_stable_constants_literal() {
        // §6: stable constants with digits must remain literals under the
        // extended kind set.
        assert_eq!(PlaceholderKind::classify("utf8"), None);
        assert_eq!(PlaceholderKind::classify("http2"), None);
        assert_eq!(PlaceholderKind::classify("mp3"), None);
        assert_eq!(PlaceholderKind::classify("iso8859"), None);
        assert_eq!(PlaceholderKind::classify("h264"), None);
        assert_eq!(PlaceholderKind::classify("ipv4"), None);
        assert_eq!(PlaceholderKind::classify("ipv6"), None);
        assert_eq!(PlaceholderKind::classify("arm64"), None);
        assert_eq!(PlaceholderKind::classify("3des"), None);
    }

    #[test]
    fn classify_non_ascii_digit_token_does_not_panic() {
        // A digit-bearing token with a trailing multi-byte char must classify
        // to `None` (literal) without panicking on a non-char-boundary slice.
        // Log content is external input; the library parse path must not panic.
        for token in ["1é", "42中", "1.5é", "100℃", "1ño", "42Ｂ"] {
            assert_eq!(
                PlaceholderKind::classify(token),
                None,
                "{token:?} must not panic and must stay a literal"
            );
        }
    }
}

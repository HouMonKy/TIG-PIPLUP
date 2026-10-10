use std::{borrow::Cow, cmp::Ordering};

use crate::placeholder::{PlaceholderKind, classify_embedded};

pub(crate) const WILDCARD_MARKER: &str = "<*>";

/// A token in a log template or observed log line.
///
/// Tokens are either literal strings or wildcards with literal head/tail
/// segments. The wildcard rendering is `head<*>tail`; a pure wildcard is
/// represented as `<*>`. Wildcards generalize variable parts of log messages
/// while preserving surrounding fixed text when possible.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum Token {
    /// A fixed literal token.
    Literal(String),
    /// A wildcard with optional literal prefix and suffix.
    Wildcard { head: String, tail: String },
}

impl Token {
    /// Create a literal token.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::Token;
    ///
    /// let token = Token::literal("worker");
    /// assert!(!token.contains_wildcard());
    /// ```
    pub fn literal(value: impl Into<String>) -> Self {
        Self::Literal(value.into())
    }

    /// Create a wildcard token from head and tail strings.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::Token;
    ///
    /// let token = Token::wildcard("user=", "");
    /// assert_eq!(token.to_rendered_string(), "user=<*>");
    /// ```
    pub fn wildcard(head: impl Into<String>, tail: impl Into<String>) -> Self {
        Self::Wildcard {
            head: head.into(),
            tail: tail.into(),
        }
    }

    /// Return true if this token contains a wildcard marker.
    pub fn contains_wildcard(&self) -> bool {
        match self {
            Self::Literal(value) => value.contains(WILDCARD_MARKER),
            Self::Wildcard { .. } => true,
        }
    }

    /// Return true if the token is a pure wildcard with no alphanumeric text.
    pub fn is_pure_wildcard(&self) -> bool {
        matches!(
            self,
            Self::Wildcard { head, tail } if !head.chars().any(char::is_alphanumeric)
                && !tail.chars().any(char::is_alphanumeric)
        ) || matches!(
            self,
            Self::Literal(value) if value.contains(WILDCARD_MARKER)
                && !value.chars().any(char::is_alphanumeric)
        )
    }

    /// Return true if the token contains at least one alphanumeric character.
    pub fn has_alnum(&self) -> bool {
        match self {
            Self::Literal(value) => value.chars().any(char::is_alphanumeric),
            Self::Wildcard { head, tail } => {
                head.chars().any(char::is_alphanumeric) || tail.chars().any(char::is_alphanumeric)
            }
        }
    }

    /// Length of the rendered token in bytes.
    pub fn rendered_len(&self) -> usize {
        match self {
            Self::Literal(value) => value.len(),
            Self::Wildcard { head, tail } => head.len() + 3 + tail.len(),
        }
    }

    /// Append the rendered token to `output`.
    pub fn append_rendered(&self, output: &mut String) {
        match self {
            Self::Literal(value) => output.push_str(value),
            Self::Wildcard { head, tail } => {
                output.push_str(head);
                output.push_str(WILDCARD_MARKER);
                output.push_str(tail);
            }
        }
    }

    /// Return the rendered token as a new string.
    pub fn to_rendered_string(&self) -> String {
        let mut rendered = String::with_capacity(self.rendered_len());
        self.append_rendered(&mut rendered);
        rendered
    }

    /// Check whether this token renders exactly to `rendered`.
    pub fn matches_rendered(&self, rendered: &str) -> bool {
        match self {
            Self::Literal(value) => value.eq_ignore_ascii_case(rendered),
            Self::Wildcard { head, tail } => {
                rendered.len() == self.rendered_len()
                    && rendered
                        .get(..head.len())
                        .is_some_and(|prefix| prefix.eq_ignore_ascii_case(head))
                    && rendered
                        .get(head.len()..)
                        .is_some_and(|rest| rest.starts_with(WILDCARD_MARKER))
                    && rendered
                        .get(rendered.len().saturating_sub(tail.len())..)
                        .is_some_and(|suffix| suffix.eq_ignore_ascii_case(tail))
            }
        }
    }

    /// Check whether this wildcard token could have produced `observed`.
    pub fn matches_observed(&self, observed: &Token) -> bool {
        match self {
            Self::Literal(value) => value.eq_ignore_ascii_case(observed.render().as_ref()),
            Self::Wildcard { head, tail } => {
                let observed = observed.render();
                let observed = observed.as_ref();
                observed.len() >= head.len() + tail.len()
                    && observed
                        .get(..head.len())
                        .is_some_and(|prefix| prefix.eq_ignore_ascii_case(head))
                    && observed
                        .get(observed.len().saturating_sub(tail.len())..)
                        .is_some_and(|suffix| suffix.eq_ignore_ascii_case(tail))
            }
        }
    }

    /// Compare two tokens by their rendered strings.
    pub fn rendered_cmp(&self, other: &Token) -> Ordering {
        match (self, other) {
            (Self::Literal(left), Self::Literal(right)) => left.cmp(right),
            (Self::Literal(left), Self::Wildcard { head, tail }) => {
                compare_rendered_segments(&[left.as_str()], &[head.as_str(), "<*>", tail.as_str()])
            }
            (Self::Wildcard { head, tail }, Self::Literal(right)) => {
                compare_rendered_segments(&[head.as_str(), "<*>", tail.as_str()], &[right.as_str()])
            }
            (
                Self::Wildcard {
                    head: left_head,
                    tail: left_tail,
                },
                Self::Wildcard {
                    head: right_head,
                    tail: right_tail,
                },
            ) => compare_rendered_segments(
                &[left_head.as_str(), "<*>", left_tail.as_str()],
                &[right_head.as_str(), "<*>", right_tail.as_str()],
            ),
        }
    }

    /// Return the rendered form, borrowing when possible.
    pub fn render(&self) -> Cow<'_, str> {
        match self {
            Self::Literal(value) => Cow::Borrowed(value.as_str()),
            Self::Wildcard { .. } => Cow::Owned(self.to_rendered_string()),
        }
    }

    /// Generalize two tokens into a wildcard that covers both.
    pub fn wildcard_from_pair(left: &Token, right: &Token) -> Self {
        Self::wildcard_from_group(&[left.clone(), right.clone()])
    }

    /// Generalize a group of tokens into a wildcard that covers all of them.
    pub fn wildcard_from_group(tokens: &[Token]) -> Self {
        let rendered = tokens
            .iter()
            .map(Token::to_rendered_string)
            .collect::<Vec<_>>();
        wildcard_from_rendered_group(&rendered)
    }

    /// Build a token from a rendered template token.
    ///
    /// This is crate-internal adapter glue for code paths that historically
    /// stored rendered template tokens (`"<*>"`, `"value=<*>"`) instead of the
    /// shared [`Token`] model.
    pub(crate) fn from_rendered_template(value: impl Into<String>) -> Self {
        let value = value.into();
        if let Some(index) = value.find(WILDCARD_MARKER) {
            let tail_start = index + WILDCARD_MARKER.len();
            return Self::wildcard(&value[..index], &value[tail_start..]);
        }
        Self::literal(value)
    }

    /// Build the template token and optional kind learned from one observed
    /// token in typed-placeholder mode.
    pub(crate) fn from_typed_observed(value: &str) -> (Self, Option<PlaceholderKind>) {
        if value == WILDCARD_MARKER {
            return (Self::wildcard("", ""), None);
        }
        if let Some(kind) = PlaceholderKind::classify(value) {
            return (Self::wildcard("", ""), Some(kind));
        }
        if let Some(embedded) = classify_embedded(value) {
            return (Self::wildcard(embedded.head, embedded.tail), embedded.kind);
        }
        (Self::literal(value), None)
    }
}

pub(crate) fn render_token_list(tokens: &[Token]) -> Vec<String> {
    tokens.iter().map(Token::to_rendered_string).collect()
}

pub(crate) fn render_token_list_joined(tokens: &[Token]) -> String {
    tokens
        .iter()
        .map(Token::to_rendered_string)
        .collect::<Vec<_>>()
        .join(" ")
}

fn wildcard_from_rendered_group(rendered_tokens: &[String]) -> Token {
    let char_sets = rendered_tokens
        .iter()
        .map(|token| token.chars().collect::<Vec<_>>())
        .collect::<Vec<_>>();
    let min_length = char_sets.iter().map(Vec::len).min().unwrap_or(0);

    let mut head = String::new();
    for index in 0..min_length {
        let ch = char_sets[0][index];
        if !is_path_delimiter(ch) || char_sets.iter().skip(1).any(|chars| chars[index] != ch) {
            break;
        }
        head.push(ch);
    }

    let mut tail = String::new();
    let max_tail = min_length.saturating_sub(head.chars().count());
    for index in 0..max_tail {
        let ch = char_sets[0][char_sets[0].len() - index - 1];
        if !is_path_delimiter(ch)
            || char_sets
                .iter()
                .skip(1)
                .any(|chars| chars[chars.len() - index - 1] != ch)
        {
            break;
        }
        tail.insert(0, ch);
    }

    Token::wildcard(head, tail)
}

fn is_path_delimiter(ch: char) -> bool {
    matches!(
        ch,
        ' ' | ','
            | '!'
            | ';'
            | ':'
            | '='
            | '|'
            | '"'
            | '\''
            | '+'
            | '.'
            | '['
            | ']'
            | '('
            | ')'
            | '{'
            | '}'
    )
}

fn compare_rendered_segments(left: &[&str], right: &[&str]) -> Ordering {
    let mut left_index = 0usize;
    let mut right_index = 0usize;
    let mut left_offset = 0usize;
    let mut right_offset = 0usize;

    loop {
        while left_index < left.len() && left_offset == left[left_index].len() {
            left_index += 1;
            left_offset = 0;
        }
        while right_index < right.len() && right_offset == right[right_index].len() {
            right_index += 1;
            right_offset = 0;
        }

        match (left_index == left.len(), right_index == right.len()) {
            (true, true) => return Ordering::Equal,
            (true, false) => return Ordering::Less,
            (false, true) => return Ordering::Greater,
            (false, false) => {}
        }

        let left_bytes = &left[left_index].as_bytes()[left_offset..];
        let right_bytes = &right[right_index].as_bytes()[right_offset..];
        let shared_len = left_bytes.len().min(right_bytes.len());
        let ordering = left_bytes[..shared_len].cmp(&right_bytes[..shared_len]);
        if ordering != Ordering::Equal {
            return ordering;
        }

        left_offset += shared_len;
        right_offset += shared_len;
    }
}

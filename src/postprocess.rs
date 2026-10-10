//! Post-processing helpers for learned templates.
//!
//! Templates discovered by the online learner sometimes contain artifacts such
//! as assignments (`key=value`) that should be generalized. This module applies
//! small, rule-based cleanups that are independent of any specific log dataset.

use std::sync::OnceLock;

use regex::Regex;

const PATH_DELIMITERS: [char; 16] = [
    ' ', ',', '!', ';', ':', '=', '|', '"', '\'', '[', ']', '(', ')', '{', '}', '.',
];
const TOKEN_ONLY_DELIMITERS: [char; 8] = ['.', '-', '+', '@', '#', '$', '%', '&'];

static ASSIGNMENT_RE: OnceLock<Result<Regex, regex::Error>> = OnceLock::new();
static COLLAPSE_ADJACENT_RE: OnceLock<Regex> = OnceLock::new();

/// Normalize a single learned template string.
///
/// The normalization:
/// - collapses whitespace;
/// - generalizes `= value` and `=value` patterns to `= <*>` / `=<*>`;
/// - splits tokens on common delimiters and replaces token-internal `<*>` with
///   standalone wildcards;
/// - collapses adjacent `<*><*>` and `<*>delim<*>` patterns.
///
/// # Example
///
/// ```rust
/// use log_parser::correct_single_template;
///
/// let template = correct_single_template("user=alice  started  task");
/// assert_eq!(template, "user=<*> started task");
/// ```
pub fn correct_single_template(template: &str) -> String {
    let assignment_re = ASSIGNMENT_RE.get_or_init(|| Regex::new(r"=(\s?)([a-zA-Z0-9_\./]+)"));
    let mut template = template.split_whitespace().collect::<Vec<_>>().join(" ");

    if let Ok(re) = assignment_re {
        template = re.replace_all(&template, "=$1<*>").into_owned();
    }

    let mut tokens = Vec::new();
    let mut current = String::new();
    for ch in template.chars() {
        if is_token_delimiter(ch) {
            if !current.is_empty() {
                tokens.push(normalize_token(std::mem::take(&mut current)));
            }
            tokens.push(ch.to_string());
        } else {
            current.push(ch);
        }
    }
    if !current.is_empty() {
        tokens.push(normalize_token(current));
    }

    template = tokens.concat();

    // Parentheses, brackets, braces and equals carry merged structure and
    // are intentionally excluded, matching TIG-PIPLUP `3e51f3b`.
    let collapse = COLLAPSE_ADJACENT_RE
        .get_or_init(|| Regex::new(r#"<\*>[ \t,!:;|"'._\-+#@$%&]<\*>"#).expect("collapse regex"));
    loop {
        let next = collapse.replace_all(&template, "<*>").into_owned();
        if next == template {
            break;
        }
        template = next;
    }

    while template.contains("<*><*>") {
        template = template.replace("<*><*>", "<*>");
    }

    apply_literal_rewrite(&mut template, " #<*># ", " <*> ");
    apply_literal_rewrite(&mut template, " #<*> ", " <*> ");
    apply_literal_rewrite(&mut template, " \"<*>\" ", " <*> ");
    apply_literal_rewrite(&mut template, " '<*>' ", " <*> ");
    apply_literal_rewrite(&mut template, "<*><*>", "<*>");
    apply_literal_rewrite(&mut template, "<*>##<*>", "<*>");
    apply_literal_rewrite(&mut template, "#<*>#", "<*>");
    apply_literal_rewrite(&mut template, " <*> <*> ", " <*> ");

    template
}

fn normalize_token(token: String) -> String {
    if token.contains("<*>") {
        "<*>".to_string()
    } else {
        token
    }
}

fn is_token_delimiter(ch: char) -> bool {
    ch.is_whitespace() || PATH_DELIMITERS.contains(&ch) || TOKEN_ONLY_DELIMITERS.contains(&ch)
}

fn apply_literal_rewrite(template: &mut String, needle: &str, replacement: &str) {
    while template.contains(needle) {
        *template = template.replace(needle, replacement);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn assignment_masks_uppercase_values() {
        assert_eq!(
            correct_single_template("cmd= REPLACE table users"),
            "cmd= <*> table users"
        );
        assert_eq!(
            correct_single_template("level=INFO worker up"),
            "level=<*> worker up"
        );
        assert_eq!(
            correct_single_template("level=info worker up"),
            "level=<*> worker up"
        );
    }

    #[test]
    fn structural_delimiters_are_preserved() {
        assert_eq!(
            correct_single_template("left<*>)<*> right"),
            "<*>)<*> right"
        );
        assert_eq!(correct_single_template("a<*>=<*>"), "<*>=<*>");
    }
}

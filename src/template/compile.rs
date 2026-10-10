use std::sync::OnceLock;

use regex::Regex;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TemplateToken {
    Literal(String),
    Wildcard { head: String, tail: String },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SegmentAtom {
    Literal(String),
    Whitespace,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompiledTemplate {
    template: String,
    normalized: String,
    tokens: Vec<TemplateToken>,
    segments: Vec<Vec<SegmentAtom>>,
    wildcard_count: usize,
}

impl CompiledTemplate {
    pub fn compile(template: &str) -> Self {
        let normalized = normalize_template_wildcards(template);
        let tokens = normalized
            .split_whitespace()
            .map(compile_token)
            .collect::<Vec<_>>();
        let segments = normalized
            .split("<*>")
            .map(compile_segment)
            .collect::<Vec<_>>();
        let wildcard_count = segments.len().saturating_sub(1);

        Self {
            template: template.to_string(),
            normalized,
            tokens,
            segments,
            wildcard_count,
        }
    }

    pub fn normalized(&self) -> &str {
        &self.normalized
    }

    pub fn tokens(&self) -> &[TemplateToken] {
        &self.tokens
    }

    pub fn segments(&self) -> &[Vec<SegmentAtom>] {
        &self.segments
    }

    #[cfg(test)]
    pub fn wildcard_count(&self) -> usize {
        self.wildcard_count
    }
}

pub fn normalize_template_wildcards(template: &str) -> String {
    static TEMPLATE_WILDCARD_RE: OnceLock<Result<Regex, regex::Error>> = OnceLock::new();

    TEMPLATE_WILDCARD_RE
        .get_or_init(|| Regex::new(r"<.{1,5}>"))
        .as_ref()
        .map(|re| re.replace_all(template, "<*>").into_owned())
        .unwrap_or_else(|_| template.to_string())
}

fn compile_token(token: &str) -> TemplateToken {
    let Some(index) = token.find("<*>") else {
        return TemplateToken::Literal(token.to_string());
    };

    let tail_index = index + 3;
    let tail = &token[tail_index..];
    if tail.contains("<*>") {
        return TemplateToken::Literal(token.to_string());
    }

    TemplateToken::Wildcard {
        head: token[..index].to_string(),
        tail: tail.to_string(),
    }
}

fn compile_segment(segment: &str) -> Vec<SegmentAtom> {
    let mut atoms = Vec::new();
    let mut literal = String::new();
    let mut whitespace_run = false;

    for ch in segment.chars() {
        if ch.is_whitespace() {
            if !literal.is_empty() {
                atoms.push(SegmentAtom::Literal(std::mem::take(&mut literal)));
            }
            if !whitespace_run {
                atoms.push(SegmentAtom::Whitespace);
                whitespace_run = true;
            }
            continue;
        }

        whitespace_run = false;
        literal.push(ch);
    }

    if !literal.is_empty() {
        atoms.push(SegmentAtom::Literal(literal));
    }

    atoms
}

#[cfg(test)]
mod tests {
    use super::{CompiledTemplate, SegmentAtom, TemplateToken, normalize_template_wildcards};

    #[test]
    fn compile_preserves_literal_templates() {
        let compiled = CompiledTemplate::compile("INFO worker done");

        assert_eq!(
            compiled.tokens(),
            &[
                TemplateToken::Literal("INFO".into()),
                TemplateToken::Literal("worker".into()),
                TemplateToken::Literal("done".into()),
            ]
        );
        assert_eq!(
            compiled.segments(),
            &[vec![
                SegmentAtom::Literal("INFO".into()),
                SegmentAtom::Whitespace,
                SegmentAtom::Literal("worker".into()),
                SegmentAtom::Whitespace,
                SegmentAtom::Literal("done".into()),
            ]]
        );
        assert_eq!(compiled.wildcard_count(), 0);
    }

    #[test]
    fn compile_supports_contextual_wildcard_tokens() {
        let compiled = CompiledTemplate::compile("setting hostname to \"<*>\"");

        assert_eq!(
            compiled.tokens(),
            &[
                TemplateToken::Literal("setting".into()),
                TemplateToken::Literal("hostname".into()),
                TemplateToken::Literal("to".into()),
                TemplateToken::Wildcard {
                    head: "\"".into(),
                    tail: "\"".into(),
                },
            ]
        );
        assert_eq!(compiled.wildcard_count(), 1);
    }

    #[test]
    fn compile_normalizes_short_wildcard_markers() {
        let compiled = CompiledTemplate::compile("user=<NUM> status=<*>");

        assert_eq!(compiled.normalized(), "user=<*> status=<*>");
        assert_eq!(compiled.wildcard_count(), 2);
    }

    #[test]
    fn compile_collapses_whitespace_runs_inside_segments() {
        let compiled = CompiledTemplate::compile("A   <*> \t B");

        assert_eq!(
            compiled.segments(),
            &[
                vec![SegmentAtom::Literal("A".into()), SegmentAtom::Whitespace],
                vec![SegmentAtom::Whitespace, SegmentAtom::Literal("B".into()),],
            ]
        );
    }

    #[test]
    fn normalize_template_wildcards_only_rewrites_short_markers() {
        assert_eq!(
            normalize_template_wildcards("<ID> <*> <ABCDEF>"),
            "<*> <*> <ABCDEF>"
        );
    }
}

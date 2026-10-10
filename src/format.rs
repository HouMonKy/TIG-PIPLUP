//! Log-format compilation and line parsing.
//!
//! A log format string such as `<Date> <Time> <Content>` is converted into a
//! regular expression that extracts named fields. The `<Content>` field is
//! required and becomes the input to template learning.

use regex::Regex;

use crate::error::LogParseError;
use crate::types::{ParsedField, ParsedLine};

/// A compiled single log format string.
#[derive(Debug, Clone)]
pub struct CompiledLogFormat {
    headers: Vec<String>,
    regex: Regex,
    content_index: usize,
}

impl CompiledLogFormat {
    /// Compile a log format string into a parser.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::CompiledLogFormat;
    ///
    /// let format = CompiledLogFormat::compile("<Date> <Time> <Content>").unwrap();
    /// assert_eq!(format.headers(), &["Date", "Time", "Content"]);
    /// ```
    pub fn compile(log_format: &str) -> Result<Self, LogParseError> {
        let marker = Regex::new(r"(<[^<>]+>)")?;
        let mut headers = Vec::new();
        let mut pattern = String::from("^");
        let mut last = 0;

        for capture in marker.find_iter(log_format) {
            pattern.push_str(&collapse_spaces(&log_format[last..capture.start()]));

            let header = &log_format[capture.start() + 1..capture.end() - 1];
            if header.is_empty() {
                return Err(LogParseError::InvalidLogFormat(
                    "encountered empty field marker".to_string(),
                ));
            }
            headers.push(header.to_string());
            pattern.push_str(&format!("(?P<{header}>.*?)"));
            last = capture.end();
        }

        pattern.push_str(&collapse_spaces(&log_format[last..]));
        pattern.push('$');

        let content_index = headers
            .iter()
            .position(|header| header == "Content")
            .ok_or(LogParseError::MissingContentField)?;

        Ok(Self {
            headers,
            regex: Regex::new(&pattern).map_err(|error| {
                LogParseError::InvalidLogFormat(format!(
                    "failed to compile regex pattern `{pattern}`: {error}. text outside <Field> markers is treated as regex; escape literal `[` as `\\[` and `]` as `\\]`"
                ))
            })?,
            content_index,
        })
    }

    /// Return the ordered list of field names.
    pub fn headers(&self) -> &[String] {
        &self.headers
    }

    /// Parse a single line according to this format.
    ///
    /// Returns `Ok(None)` when the line does not match the format.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::CompiledLogFormat;
    ///
    /// let format = CompiledLogFormat::compile("<Date> <Time> <Content>").unwrap();
    /// let parsed = format.parse_line(1, "2026-06-05 10:00:01 worker started").unwrap();
    /// assert!(parsed.is_some());
    /// assert_eq!(parsed.unwrap().content, "worker started");
    /// ```
    pub fn parse_line(
        &self,
        line_number: usize,
        raw: &str,
    ) -> Result<Option<ParsedLine>, LogParseError> {
        let trimmed = raw.trim();
        let Some(captures) = self.regex.captures(trimmed) else {
            return Ok(None);
        };

        let mut fields = Vec::with_capacity(self.headers.len());
        for header in &self.headers {
            // Python's `match.group(name)` yields `None` for unmatched optional groups.
            // Mirror that by materializing an empty field rather than treating it as a parse error.
            let value = captures
                .name(header)
                .map(|value| value.as_str().to_string())
                .unwrap_or_default();
            fields.push(ParsedField {
                name: header.clone(),
                value,
            });
        }

        let content = fields[self.content_index].value.clone();
        Ok(Some(ParsedLine {
            line_number,
            raw: raw.to_string(),
            fields,
            content,
        }))
    }
}

fn collapse_spaces(input: &str) -> String {
    let mut output = String::new();
    let mut chars = input.chars().peekable();
    while let Some(ch) = chars.next() {
        if ch == ' ' {
            output.push_str(r"\s+");
            while chars.peek().is_some_and(|next| *next == ' ') {
                chars.next();
            }
        } else {
            output.push(ch);
        }
    }
    output
}

/// A compiled set of alternative log formats.
///
/// Input lines are tried against each format in order until one matches. The
/// union of all field names is used for output.
#[derive(Debug, Clone)]
pub struct MultiCompiledLogFormat {
    formats: Vec<CompiledLogFormat>,
    all_headers: Vec<String>,
    content_index: usize,
}

impl MultiCompiledLogFormat {
    /// Compile multiple format strings.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::MultiCompiledLogFormat;
    ///
    /// let formats = vec!["<Date> <Time> <Content>".to_string()];
    /// let multi = MultiCompiledLogFormat::compile(&formats).unwrap();
    /// assert_eq!(multi.headers(), &["Date", "Time", "Content"]);
    /// ```
    pub fn compile(log_formats: &[String]) -> Result<Self, LogParseError> {
        if log_formats.is_empty() {
            return Err(LogParseError::InvalidLogFormat(
                "at least one log format is required".to_string(),
            ));
        }

        let mut formats = Vec::with_capacity(log_formats.len());
        let mut all_headers: Vec<String> = Vec::new();

        for log_format in log_formats {
            let compiled = CompiledLogFormat::compile(log_format)?;
            for header in compiled.headers() {
                if !all_headers.contains(header) {
                    all_headers.push(header.clone());
                }
            }
            formats.push(compiled);
        }

        let content_index = all_headers
            .iter()
            .position(|h| h == "Content")
            .ok_or(LogParseError::MissingContentField)?;

        Ok(Self {
            formats,
            all_headers,
            content_index,
        })
    }

    /// Return the union of field names across all formats.
    pub fn headers(&self) -> &[String] {
        &self.all_headers
    }

    /// Parse a line, trying each alternative format in order.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::MultiCompiledLogFormat;
    ///
    /// let formats = vec!["<Date> <Time> <Content>".to_string()];
    /// let multi = MultiCompiledLogFormat::compile(&formats).unwrap();
    /// let parsed = multi.parse_line(1, "2026-06-05 10:00:01 worker started").unwrap();
    /// assert!(parsed.is_some());
    /// ```
    pub fn parse_line(
        &self,
        line_number: usize,
        raw: &str,
    ) -> Result<Option<ParsedLine>, LogParseError> {
        for format in &self.formats {
            if let Some(parsed) = format.parse_line(line_number, raw)? {
                let mut fields = Vec::with_capacity(self.all_headers.len());
                for header in &self.all_headers {
                    let value = parsed
                        .fields
                        .iter()
                        .find(|f| &f.name == header)
                        .map(|f| f.value.clone())
                        .unwrap_or_default();
                    fields.push(ParsedField {
                        name: header.clone(),
                        value,
                    });
                }
                let content = fields[self.content_index].value.clone();
                return Ok(Some(ParsedLine {
                    line_number,
                    raw: raw.to_string(),
                    fields,
                    content,
                }));
            }
        }
        Ok(None)
    }
}

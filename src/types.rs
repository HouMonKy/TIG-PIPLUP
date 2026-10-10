//! Core data types produced and consumed by the parser pipeline.
//!
//! This module defines the line-oriented inputs, parsed fields, structured
//! outputs, and serialization helpers that are shared across training, index
//! parsing, and CLI output.

use std::time::Duration;
use std::{fs::File, io::BufWriter, io::Write, path::Path};

use csv::Writer;
use serde::{Deserialize, Serialize};

use crate::error::LogParseError;
use rustc_hash::FxHashMap;

/// A single raw log line with its 1-based line number.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LogEntry {
    /// 1-based line number within the input stream.
    pub line_number: usize,
    /// Raw text of the log line.
    pub raw: String,
}

/// A named field parsed from a log line according to the log format.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ParsedField {
    /// Field name, e.g. `Date`, `Time`, or `Content`.
    pub name: String,
    /// Field value as it appears in the line.
    pub value: String,
}

/// A log line split into named fields, plus the extracted `<Content>` value.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ParsedLine {
    /// 1-based line number.
    pub line_number: usize,
    /// Original raw line.
    pub raw: String,
    /// Named fields parsed from the line.
    pub fields: Vec<ParsedField>,
    /// Value of the `<Content>` field, used for template learning.
    pub content: String,
}

/// A fully parsed log line with its assigned event template.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LineResult {
    /// 1-based line number.
    pub line_number: usize,
    /// Original raw line.
    pub raw: String,
    /// Named fields parsed from the line.
    pub fields: Vec<ParsedField>,
    /// Stable event identifier derived from the template.
    pub event_id: String,
    /// Human-readable event template.
    pub event_template: String,
    /// Values that matched wildcard tokens in the template, if any.
    pub parameter_list: Option<Vec<String>>,
}

/// Aggregate information about one discovered event template.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct EventRecord {
    /// Stable event identifier.
    pub event_id: String,
    /// Human-readable event template.
    pub event_template: String,
    /// Number of input lines assigned to this template.
    pub occurrences: usize,
}

/// Result of parsing a batch of log lines.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParseOutput {
    /// One entry per parsed line.
    pub line_results: Vec<LineResult>,
    /// One entry per discovered template.
    pub events: Vec<EventRecord>,
    /// Number of clusters discovered.
    pub cluster_count: usize,
    /// Wall-clock time spent parsing.
    pub duration: Duration,
}

/// Result of training, including parse output and the preprocess plan needed to
/// reproduce it during index parsing.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TrainingOutput {
    /// Parsed output produced by training.
    pub parse_output: ParseOutput,
    /// Preprocess plan captured during training.
    pub preprocess_plan: crate::preprocessor::PreprocessPlan,
}

/// Controls which fields appear in structured output and their order.
///
/// Construct via [`FieldProjection::all`] for the default (all available fields)
/// or [`FieldProjection::subset`] to validate and select specific fields.
#[derive(Debug, Clone)]
pub struct FieldProjection {
    fields: Option<Vec<String>>,
}

impl FieldProjection {
    /// Full projection — all available fields in their default order.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::FieldProjection;
    ///
    /// let proj = FieldProjection::all();
    /// let available = vec!["LineId".to_string(), "EventId".to_string()];
    /// assert_eq!(proj.project(&available), available);
    /// ```
    pub fn all() -> Self {
        FieldProjection { fields: None }
    }

    /// Validate and create a projection for a subset of `available` fields.
    ///
    /// Returns `LogParseError::DuplicateOutputField` if `names` contains duplicates.
    /// Returns `LogParseError::UnknownOutputField` if a name is not in `available`.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::FieldProjection;
    ///
    /// let available = vec!["LineId".to_string(), "EventId".to_string()];
    /// let proj = FieldProjection::subset(vec!["EventId".to_string()], &available).unwrap();
    /// assert_eq!(proj.project(&available), vec!["EventId".to_string()]);
    /// ```
    pub fn subset(names: Vec<String>, available: &[String]) -> Result<Self, LogParseError> {
        let mut seen = rustc_hash::FxHashSet::default();
        for name in &names {
            if !seen.insert(name.as_str()) {
                return Err(LogParseError::DuplicateOutputField {
                    field: name.clone(),
                });
            }
        }
        for name in &names {
            if !available.contains(name) {
                return Err(LogParseError::UnknownOutputField {
                    field: name.clone(),
                    available: available.join(", "),
                });
            }
        }
        Ok(FieldProjection {
            fields: Some(names),
        })
    }

    /// Apply this projection to `available`, returning the ordered list of fields to emit.
    ///
    /// For `all()`, returns `available` unchanged. For `subset(...)`, returns the
    /// user-specified names in the order they were provided.
    pub fn project(&self, available: &[String]) -> Vec<String> {
        match &self.fields {
            None => available.to_vec(),
            Some(fields) => fields.clone(),
        }
    }
}

impl ParseOutput {
    /// Returns the list of available field names for structured output.
    ///
    /// The order is: `LineId`, then log-format fields (from the first line's parsed fields),
    /// then `EventId`, `EventTemplate`, and optionally `ParameterList` if any line has parameters.
    fn available_headers(&self) -> Vec<String> {
        let headers: Vec<String> = self
            .line_results
            .first()
            .map(|line| line.fields.iter().map(|field| field.name.clone()).collect())
            .unwrap_or_default();

        let has_parameter_list = self
            .line_results
            .iter()
            .any(|line| line.parameter_list.is_some());

        let mut available = Vec::with_capacity(headers.len() + 4);
        available.push("LineId".to_string());
        available.extend(headers.iter().cloned());
        available.push("EventId".to_string());
        available.push("EventTemplate".to_string());
        if has_parameter_list {
            available.push("ParameterList".to_string());
        }
        available
    }

    /// Write structured output as CSV using the default (all fields) projection.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::{ParseOutput, LineResult, EventRecord, ParsedField};
    /// use std::time::Duration;
    ///
    /// let output = ParseOutput {
    ///     line_results: vec![LineResult {
    ///         line_number: 1,
    ///         raw: "worker started".into(),
    ///         fields: vec![ParsedField { name: "Content".into(), value: "worker started".into() }],
    ///         event_id: "abc".into(),
    ///         event_template: "worker started".into(),
    ///         parameter_list: None,
    ///     }],
    ///     events: vec![EventRecord { event_id: "abc".into(), event_template: "worker started".into(), occurrences: 1 }],
    ///     cluster_count: 1,
    ///     duration: Duration::default(),
    /// };
    /// let mut buf = Vec::new();
    /// output.write_structured_csv(&mut buf).unwrap();
    /// assert!(String::from_utf8(buf).unwrap().contains("worker started"));
    /// ```
    pub fn write_structured_csv<W: Write>(&self, writer: W) -> Result<(), LogParseError> {
        self.write_structured_csv_projected(writer, &FieldProjection::all())
    }

    /// Write structured output as CSV with a custom field projection.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::{ParseOutput, LineResult, EventRecord, ParsedField, FieldProjection};
    /// use std::time::Duration;
    ///
    /// let output = ParseOutput {
    ///     line_results: vec![LineResult {
    ///         line_number: 1,
    ///         raw: "x".into(),
    ///         fields: vec![ParsedField { name: "Content".into(), value: "x".into() }],
    ///         event_id: "abc".into(),
    ///         event_template: "x".into(),
    ///         parameter_list: None,
    ///     }],
    ///     events: vec![EventRecord { event_id: "abc".into(), event_template: "x".into(), occurrences: 1 }],
    ///     cluster_count: 1,
    ///     duration: Duration::default(),
    /// };
    /// let available = vec!["LineId".into(), "EventId".into()];
    /// let projection = FieldProjection::subset(vec!["LineId".into()], &available).unwrap();
    /// let mut buf = Vec::new();
    /// output.write_structured_csv_projected(&mut buf, &projection).unwrap();
    /// assert_eq!(String::from_utf8(buf).unwrap().trim(), "LineId\n1");
    /// ```
    pub fn write_structured_csv_projected<W: Write>(
        &self,
        writer: W,
        projection: &FieldProjection,
    ) -> Result<(), LogParseError> {
        let available = self.available_headers();
        let projected = projection.project(&available);

        let mut structured = Writer::from_writer(writer);
        structured.write_record(&projected)?;

        let field_index: FxHashMap<&str, usize> = self
            .line_results
            .first()
            .map(|line| {
                line.fields
                    .iter()
                    .enumerate()
                    .map(|(i, f)| (f.name.as_str(), i))
                    .collect()
            })
            .unwrap_or_default();

        for line in &self.line_results {
            for header in &projected {
                let value = match header.as_str() {
                    "LineId" => line.line_number.to_string(),
                    "EventId" => line.event_id.clone(),
                    "EventTemplate" => line.event_template.clone(),
                    "ParameterList" => line
                        .parameter_list
                        .as_ref()
                        .map(|values| python_list_repr(values))
                        .unwrap_or_default(),
                    other => field_index
                        .get(other)
                        .and_then(|&idx| line.fields.get(idx))
                        .map(|f| f.value.as_str())
                        .unwrap_or("")
                        .to_string(),
                };
                structured.write_field(&value)?;
            }
            structured.write_record(std::iter::empty::<&str>())?;
        }
        structured.flush()?;
        Ok(())
    }

    /// Write structured output as newline-delimited JSON with a field projection.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::{ParseOutput, LineResult, EventRecord, ParsedField, FieldProjection};
    /// use std::time::Duration;
    ///
    /// let output = ParseOutput {
    ///     line_results: vec![LineResult {
    ///         line_number: 1,
    ///         raw: "x".into(),
    ///         fields: vec![ParsedField { name: "Content".into(), value: "x".into() }],
    ///         event_id: "abc".into(),
    ///         event_template: "x".into(),
    ///         parameter_list: Some(vec!["p1".into()]),
    ///     }],
    ///     events: vec![],
    ///     cluster_count: 0,
    ///     duration: Duration::default(),
    /// };
    /// let mut buf = Vec::new();
    /// output.write_structured_jsonl(&mut buf, &FieldProjection::all()).unwrap();
    /// let parsed: serde_json::Value = serde_json::from_reader(&buf[..]).unwrap();
    /// assert_eq!(parsed["LineId"], serde_json::json!(1));
    /// ```
    pub fn write_structured_jsonl<W: Write>(
        &self,
        writer: W,
        projection: &FieldProjection,
    ) -> Result<(), LogParseError> {
        let mut writer = BufWriter::new(writer);

        if self.line_results.is_empty() {
            return Ok(());
        }

        let available = self.available_headers();
        let projected = projection.project(&available);

        for line in &self.line_results {
            let field_map: FxHashMap<&str, &str> = line
                .fields
                .iter()
                .map(|f| (f.name.as_str(), f.value.as_str()))
                .collect();

            let mut map = serde_json::Map::new();
            for field_name in &projected {
                let json_value = match field_name.as_str() {
                    "LineId" => {
                        serde_json::Value::Number(serde_json::Number::from(line.line_number as u64))
                    }
                    "EventId" => serde_json::Value::String(line.event_id.clone()),
                    "EventTemplate" => serde_json::Value::String(line.event_template.clone()),
                    "ParameterList" => match &line.parameter_list {
                        Some(vals) => serde_json::Value::Array(
                            vals.iter()
                                .map(|v| serde_json::Value::String(v.clone()))
                                .collect(),
                        ),
                        None => continue,
                    },
                    other => {
                        let value = field_map.get(other).copied().unwrap_or("");
                        serde_json::Value::String(value.to_string())
                    }
                };
                map.insert(field_name.clone(), json_value);
            }

            serde_json::to_writer(&mut writer, &serde_json::Value::Object(map))?;
            writer.write_all(b"\n")?;
        }

        writer.flush()?;
        Ok(())
    }

    /// Write the event template summary as CSV.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::{ParseOutput, EventRecord};
    /// use std::time::Duration;
    ///
    /// let output = ParseOutput {
    ///     line_results: vec![],
    ///     events: vec![EventRecord { event_id: "abc".into(), event_template: "worker started".into(), occurrences: 5 }],
    ///     cluster_count: 1,
    ///     duration: Duration::default(),
    /// };
    /// let mut buf = Vec::new();
    /// output.write_templates_csv(&mut buf).unwrap();
    /// let csv = String::from_utf8(buf).unwrap();
    /// assert!(csv.contains("worker started"));
    /// assert!(csv.contains('5'));
    /// ```
    pub fn write_templates_csv<W: Write>(&self, writer: W) -> Result<(), LogParseError> {
        let mut templates = Writer::from_writer(writer);
        templates.write_record(["EventId", "EventTemplate", "Occurrences"])?;
        for event in &self.events {
            templates.write_record([
                event.event_id.as_str(),
                event.event_template.as_str(),
                &event.occurrences.to_string(),
            ])?;
        }
        templates.flush()?;
        Ok(())
    }

    /// Write both `structured.csv` and `templates.csv` into `dir`.
    ///
    /// # Example
    ///
    /// ```no_run
    /// use log_parser::ParseOutput;
    /// # fn example(output: &ParseOutput) -> Result<(), Box<dyn std::error::Error>> {
    /// output.write_csv(std::path::Path::new("./result"))?;
    /// # Ok(())
    /// # }
    /// ```
    pub fn write_csv(&self, dir: &Path) -> Result<(), LogParseError> {
        std::fs::create_dir_all(dir)?;
        self.write_structured_csv(BufWriter::new(File::create(dir.join("structured.csv"))?))?;
        self.write_templates_csv(BufWriter::new(File::create(dir.join("templates.csv"))?))?;
        Ok(())
    }

    /// Return the number of distinct event templates.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::{ParseOutput, EventRecord};
    /// use std::time::Duration;
    ///
    /// let output = ParseOutput {
    ///     line_results: vec![],
    ///     events: vec![EventRecord { event_id: "a".into(), event_template: "t".into(), occurrences: 1 }],
    ///     cluster_count: 1,
    ///     duration: Duration::default(),
    /// };
    /// assert_eq!(output.template_count(), 1);
    /// ```
    pub fn template_count(&self) -> usize {
        self.events.len()
    }
}

fn python_list_repr(values: &[String]) -> String {
    let rendered = values
        .iter()
        .map(|value| python_string_repr(value))
        .collect::<Vec<_>>()
        .join(", ");
    format!("[{rendered}]")
}

fn python_string_repr(value: &str) -> String {
    let mut rendered = String::from("'");
    for ch in value.chars() {
        match ch {
            '\\' => rendered.push_str("\\\\"),
            '\'' => rendered.push_str("\\'"),
            '\n' => rendered.push_str("\\n"),
            '\r' => rendered.push_str("\\r"),
            '\t' => rendered.push_str("\\t"),
            _ => rendered.push(ch),
        }
    }
    rendered.push('\'');
    rendered
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_line_result(
        line_number: usize,
        fields: Vec<(&str, &str)>,
        event_id: &str,
        event_template: &str,
        parameter_list: Option<Vec<String>>,
    ) -> LineResult {
        LineResult {
            line_number,
            raw: String::new(),
            fields: fields
                .into_iter()
                .map(|(name, value)| ParsedField {
                    name: name.to_string(),
                    value: value.to_string(),
                })
                .collect(),
            event_id: event_id.to_string(),
            event_template: event_template.to_string(),
            parameter_list,
        }
    }

    fn sample_output() -> ParseOutput {
        ParseOutput {
            line_results: vec![
                make_line_result(
                    1,
                    vec![
                        ("Date", "2026-06-05"),
                        ("Time", "10:00:01"),
                        ("Content", "worker started"),
                    ],
                    "abc123",
                    "worker started",
                    None,
                ),
                make_line_result(
                    2,
                    vec![
                        ("Date", "2026-06-05"),
                        ("Time", "10:00:02"),
                        ("Content", "worker stopped"),
                    ],
                    "abc123",
                    "worker started",
                    None,
                ),
            ],
            events: vec![EventRecord {
                event_id: "abc123".to_string(),
                event_template: "worker started".to_string(),
                occurrences: 2,
            }],
            cluster_count: 1,
            duration: Duration::default(),
        }
    }

    // --- FieldProjection tests ---

    #[test]
    fn field_projection_all_returns_available() {
        let proj = FieldProjection::all();
        let available = vec!["LineId".to_string(), "EventId".to_string()];
        assert_eq!(proj.project(&available), available);
    }

    #[test]
    fn field_projection_subset_preserves_user_order() {
        let available = vec![
            "LineId".to_string(),
            "EventId".to_string(),
            "EventTemplate".to_string(),
        ];
        let proj = FieldProjection::subset(
            vec!["EventTemplate".to_string(), "LineId".to_string()],
            &available,
        )
        .unwrap();
        assert_eq!(
            proj.project(&available),
            vec!["EventTemplate".to_string(), "LineId".to_string()]
        );
    }

    #[test]
    fn field_projection_unknown_field_error() {
        let available = vec!["LineId".to_string(), "EventId".to_string()];
        let result = FieldProjection::subset(vec!["Bogus".to_string()], &available);
        assert!(matches!(
            result,
            Err(LogParseError::UnknownOutputField { .. })
        ));
    }

    #[test]
    fn field_projection_duplicate_field_error() {
        let available = vec!["LineId".to_string(), "EventId".to_string()];
        let result =
            FieldProjection::subset(vec!["LineId".to_string(), "LineId".to_string()], &available);
        assert!(matches!(
            result,
            Err(LogParseError::DuplicateOutputField { .. })
        ));
    }

    // --- write_structured_csv_projected tests ---

    #[test]
    fn write_structured_csv_projected_filters_columns() {
        let output = sample_output();
        let available = vec![
            "LineId".to_string(),
            "Date".to_string(),
            "Time".to_string(),
            "Content".to_string(),
            "EventId".to_string(),
            "EventTemplate".to_string(),
        ];
        let projection = FieldProjection::subset(
            vec!["LineId".to_string(), "EventId".to_string()],
            &available,
        )
        .unwrap();

        let mut buf = Vec::new();
        output
            .write_structured_csv_projected(&mut buf, &projection)
            .unwrap();
        let csv_str = String::from_utf8(buf).unwrap();
        let lines: Vec<&str> = csv_str.lines().collect();

        assert_eq!(lines[0], "LineId,EventId");
        assert_eq!(lines[1], "1,abc123");
        assert_eq!(lines[2], "2,abc123");
    }

    // --- write_structured_jsonl tests ---

    #[test]
    fn write_structured_jsonl_line_id_is_number() {
        let output = sample_output();
        let projection = FieldProjection::all();
        let mut buf = Vec::new();
        output
            .write_structured_jsonl(&mut buf, &projection)
            .unwrap();
        let json_str = String::from_utf8(buf).unwrap();
        let first_line = json_str.lines().next().unwrap();
        let parsed: serde_json::Value = serde_json::from_str(first_line).unwrap();

        assert_eq!(parsed["LineId"], serde_json::json!(1));
        assert!(parsed["LineId"].is_number());
    }

    #[test]
    fn write_structured_jsonl_parameter_list_is_array() {
        let output = ParseOutput {
            line_results: vec![make_line_result(
                1,
                vec![("Content", "test")],
                "id1",
                "test <*> <*>",
                Some(vec!["val1".to_string(), "val2".to_string()]),
            )],
            events: vec![],
            cluster_count: 0,
            duration: Duration::default(),
        };
        let projection = FieldProjection::all();
        let mut buf = Vec::new();
        output
            .write_structured_jsonl(&mut buf, &projection)
            .unwrap();
        let json_str = String::from_utf8(buf).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(json_str.trim()).unwrap();

        assert!(parsed["ParameterList"].is_array());
        assert_eq!(parsed["ParameterList"], serde_json::json![["val1", "val2"]]);
    }

    #[test]
    fn write_structured_jsonl_parameter_list_none_absent() {
        let output = sample_output();
        let projection = FieldProjection::all();
        let mut buf = Vec::new();
        output
            .write_structured_jsonl(&mut buf, &projection)
            .unwrap();
        let json_str = String::from_utf8(buf).unwrap();
        let first_line = json_str.lines().next().unwrap();
        let parsed: serde_json::Value = serde_json::from_str(first_line).unwrap();

        // ParameterList should not appear at all when None
        assert!(parsed.get("ParameterList").is_none());
    }

    #[test]
    fn write_structured_jsonl_empty_value_is_empty_string() {
        let output = ParseOutput {
            line_results: vec![make_line_result(
                1,
                vec![("Date", ""), ("Content", "test")],
                "id1",
                "test",
                None,
            )],
            events: vec![],
            cluster_count: 0,
            duration: Duration::default(),
        };
        let projection = FieldProjection::all();
        let mut buf = Vec::new();
        output
            .write_structured_jsonl(&mut buf, &projection)
            .unwrap();
        let json_str = String::from_utf8(buf).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(json_str.trim()).unwrap();

        assert_eq!(parsed["Date"], serde_json::json!(""));
        assert!(parsed["Date"].is_string());
    }

    #[test]
    fn write_structured_jsonl_empty_results_produces_no_output() {
        let output = ParseOutput {
            line_results: vec![],
            events: vec![],
            cluster_count: 0,
            duration: Duration::default(),
        };
        let projection = FieldProjection::all();
        let mut buf = Vec::new();
        output
            .write_structured_jsonl(&mut buf, &projection)
            .unwrap();
        assert!(buf.is_empty());
    }

    #[test]
    fn write_structured_csv_projected_backward_compat_matches_write_structured_csv() {
        let output = sample_output();
        // all() projection should produce same output as write_structured_csv
        let projection = FieldProjection::all();

        let mut projected_buf = Vec::new();
        output
            .write_structured_csv_projected(&mut projected_buf, &projection)
            .unwrap();
        let projected = String::from_utf8(projected_buf).unwrap();

        let mut original_buf = Vec::new();
        output.write_structured_csv(&mut original_buf).unwrap();
        let original = String::from_utf8(original_buf).unwrap();

        assert_eq!(projected, original);
    }

    #[test]
    fn write_structured_jsonl_empty_parameter_list_is_empty_array() {
        let output = ParseOutput {
            line_results: vec![make_line_result(
                1,
                vec![("Content", "test")],
                "id1",
                "test",
                Some(vec![]), // Empty parameter list
            )],
            events: vec![],
            cluster_count: 0,
            duration: Duration::default(),
        };
        let projection = FieldProjection::all();
        let mut buf = Vec::new();
        output
            .write_structured_jsonl(&mut buf, &projection)
            .unwrap();
        let json_str = String::from_utf8(buf).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(json_str.trim()).unwrap();

        // Empty ParameterList should be an empty array, not absent
        assert!(parsed["ParameterList"].is_array());
        assert_eq!(parsed["ParameterList"], serde_json::json!([]));
    }

    #[test]
    fn write_structured_jsonl_projected_subset_preserves_field_order() {
        let output = sample_output();
        let available = vec![
            "LineId".to_string(),
            "Date".to_string(),
            "Time".to_string(),
            "Content".to_string(),
            "EventId".to_string(),
            "EventTemplate".to_string(),
        ];
        // Request fields in a non-default order
        let projection = FieldProjection::subset(
            vec![
                "EventTemplate".to_string(),
                "LineId".to_string(),
                "Date".to_string(),
            ],
            &available,
        )
        .unwrap();

        let mut buf = Vec::new();
        output
            .write_structured_jsonl(&mut buf, &projection)
            .unwrap();
        let json_str = String::from_utf8(buf).unwrap();
        let first_line = json_str.lines().next().unwrap();
        let parsed: serde_json::Value = serde_json::from_str(first_line).unwrap();

        // Verify fields are present in the requested order
        let keys: Vec<&str> = parsed
            .as_object()
            .unwrap()
            .keys()
            .map(|k| k.as_str())
            .collect();
        assert_eq!(keys, vec!["EventTemplate", "LineId", "Date"]);
    }

    #[test]
    fn write_structured_csv_projected_with_parameter_list_backward_compat() {
        let output = ParseOutput {
            line_results: vec![make_line_result(
                1,
                vec![("Content", "test val1 val2")],
                "id1",
                "test <*> <*>",
                Some(vec!["val1".to_string(), "val2".to_string()]),
            )],
            events: vec![],
            cluster_count: 0,
            duration: Duration::default(),
        };

        let projection = FieldProjection::all();
        let mut projected_buf = Vec::new();
        output
            .write_structured_csv_projected(&mut projected_buf, &projection)
            .unwrap();
        let projected = String::from_utf8(projected_buf).unwrap();

        let mut original_buf = Vec::new();
        output.write_structured_csv(&mut original_buf).unwrap();
        let original = String::from_utf8(original_buf).unwrap();

        // With ParameterList present, delegation should still produce identical output
        assert_eq!(projected, original);
    }
}

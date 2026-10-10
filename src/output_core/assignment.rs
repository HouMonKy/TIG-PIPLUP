use std::collections::BTreeMap;
use std::time::Duration;

use crate::error::LogParseError;
use crate::output_core::events::EventAccumulator;
use crate::output_core::parameters::ParameterExtractor;
use crate::parse_core::ensure_unique_line_numbers;
use crate::types::{LineResult, ParseOutput, ParsedLine};

/// A learned template assignment produced by a clustering algorithm.
///
/// Each assignment maps a parsed line to a cluster and its generalized template.
/// The `cluster_id` is used to derive `cluster_count` from unique IDs.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LearnedTemplateAssignment {
    /// Original line number from the parsed input.
    pub line_number: usize,
    /// Cluster identifier. Used to derive `cluster_count`.
    pub cluster_id: usize,
    /// Generalized template string (e.g., `"worker <*> started"`).
    pub template: String,
}

/// Build [`ParseOutput`] from explicit learned assignments.
///
/// This is the shared output path for Drain3 (and future algorithms). It
/// produces the same artifact shape as the PIPLUP [`build_output`] path:
/// - `EventId` is derived from the template via [`event_id_for_template`](crate::event_id_for_template).
/// - Parameters are extracted using the project template matcher when
///   `keep_para` is `true`.
/// - Unmatched assignments (empty template or `"0"`) produce `EventId=0` /
///   `EventTemplate=0` with optional `--warn-miss` stderr output.
/// - `cluster_count` is derived from unique `cluster_id` values.
pub fn build_output_from_assignments(
    parsed_lines: Vec<ParsedLine>,
    assignments: Vec<LearnedTemplateAssignment>,
    keep_para: bool,
    warn_miss: bool,
    duration: Duration,
) -> Result<ParseOutput, LogParseError> {
    ensure_unique_line_numbers(parsed_lines.iter().map(|l| l.line_number))?;

    // Build lookup: line_number -> assignment index
    let assignment_map: BTreeMap<usize, usize> = assignments
        .iter()
        .enumerate()
        .map(|(idx, a)| (a.line_number, idx))
        .collect();

    // Derive cluster_count from unique cluster_ids.
    // Exclude the no-match sentinel (usize::MAX) from the count.
    const NO_MATCH_SENTINEL: usize = usize::MAX;
    let mut cluster_ids_seen = BTreeMap::new();
    for a in &assignments {
        if a.cluster_id != NO_MATCH_SENTINEL {
            cluster_ids_seen.entry(a.cluster_id).or_insert(());
        }
    }
    let cluster_count = cluster_ids_seen.len();

    let mut line_results = Vec::with_capacity(parsed_lines.len());
    let mut accumulator = EventAccumulator::new();
    let mut extractor = ParameterExtractor::new(keep_para);

    for line in &parsed_lines {
        let Some(&assign_idx) = assignment_map.get(&line.line_number) else {
            // No assignment for this line — unmatched.
            if warn_miss {
                eprintln!("[Warning] No template assigned for log: {}", line.content);
            }
            accumulator.record_template("0", 1);
            line_results.push(LineResult {
                line_number: line.line_number,
                raw: line.raw.clone(),
                fields: line.fields.clone(),
                event_id: "0".to_string(),
                event_template: "0".to_string(),
                parameter_list: extractor.unmatched(),
            });
            continue;
        };

        let assignment = &assignments[assign_idx];
        let template = &assignment.template;

        if template.is_empty() || template == "0" {
            // Empty or sentinel template — treat as unmatched.
            if warn_miss {
                eprintln!("[Warning] Empty template for log: {}", line.content);
            }
            accumulator.record_template("0", 1);
            line_results.push(LineResult {
                line_number: line.line_number,
                raw: line.raw.clone(),
                fields: line.fields.clone(),
                event_id: "0".to_string(),
                event_template: "0".to_string(),
                parameter_list: extractor.unmatched(),
            });
            continue;
        }

        let event_id = crate::event_id_for_template(template);
        let parameter_list = extractor.for_template(template, &line.content);

        accumulator.record_template(template, 1);
        line_results.push(LineResult {
            line_number: line.line_number,
            raw: line.raw.clone(),
            fields: line.fields.clone(),
            event_id,
            event_template: template.clone(),
            parameter_list,
        });
    }

    Ok(ParseOutput {
        line_results,
        events: accumulator.into_events(),
        cluster_count,
        duration,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::event_id_for_template;
    use crate::types::{ParsedField, ParsedLine};

    fn make_parsed_line(line_number: usize, content: &str) -> ParsedLine {
        ParsedLine {
            line_number,
            raw: content.to_string(),
            fields: vec![ParsedField {
                name: "Content".to_string(),
                value: content.to_string(),
            }],
            content: content.to_string(),
        }
    }

    #[test]
    fn build_output_assigns_event_id_and_template() {
        let lines = vec![make_parsed_line(1, "worker started")];
        let assignments = vec![LearnedTemplateAssignment {
            line_number: 1,
            cluster_id: 0,
            template: "worker started".to_string(),
        }];

        let output =
            build_output_from_assignments(lines, assignments, false, false, Duration::default())
                .unwrap();

        assert_eq!(output.line_results.len(), 1);
        assert_eq!(
            output.line_results[0].event_id,
            event_id_for_template("worker started")
        );
        assert_eq!(output.line_results[0].event_template, "worker started");
        assert_eq!(output.cluster_count, 1);
    }

    #[test]
    fn build_output_derives_cluster_count_from_unique_ids() {
        let lines = vec![
            make_parsed_line(1, "a"),
            make_parsed_line(2, "b"),
            make_parsed_line(3, "c"),
        ];
        let assignments = vec![
            LearnedTemplateAssignment {
                line_number: 1,
                cluster_id: 0,
                template: "a".to_string(),
            },
            LearnedTemplateAssignment {
                line_number: 2,
                cluster_id: 1,
                template: "b".to_string(),
            },
            LearnedTemplateAssignment {
                line_number: 3,
                cluster_id: 0, // same cluster as line 1
                template: "a".to_string(),
            },
        ];

        let output =
            build_output_from_assignments(lines, assignments, false, false, Duration::default())
                .unwrap();

        assert_eq!(output.cluster_count, 2); // unique cluster_ids: 0, 1
    }

    #[test]
    fn build_output_unmatched_line_produces_zero() {
        let lines = vec![make_parsed_line(1, "no match")];
        let assignments = vec![]; // no assignment for line 1

        let output =
            build_output_from_assignments(lines, assignments, false, false, Duration::default())
                .unwrap();

        assert_eq!(output.line_results[0].event_id, "0");
        assert_eq!(output.line_results[0].event_template, "0");
    }

    #[test]
    fn build_output_empty_template_produces_zero() {
        let lines = vec![make_parsed_line(1, "test")];
        let assignments = vec![LearnedTemplateAssignment {
            line_number: 1,
            cluster_id: 0,
            template: String::new(),
        }];

        let output =
            build_output_from_assignments(lines, assignments, false, false, Duration::default())
                .unwrap();

        assert_eq!(output.line_results[0].event_id, "0");
        assert_eq!(output.line_results[0].event_template, "0");
    }

    #[test]
    fn build_output_sentinel_template_event_record_uses_zero_id() {
        let lines = vec![make_parsed_line(1, "test")];
        let assignments = vec![LearnedTemplateAssignment {
            line_number: 1,
            cluster_id: usize::MAX,
            template: "0".to_string(),
        }];

        let output =
            build_output_from_assignments(lines, assignments, false, false, Duration::default())
                .unwrap();

        assert_eq!(output.line_results[0].event_id, "0");
        assert_eq!(output.events.len(), 1);
        assert_eq!(output.events[0].event_id, "0");
        assert_eq!(output.events[0].event_template, "0");
    }

    #[test]
    fn build_output_keep_para_extracts_parameters() {
        let lines = vec![make_parsed_line(1, "worker 42 started")];
        let assignments = vec![LearnedTemplateAssignment {
            line_number: 1,
            cluster_id: 0,
            template: "worker <*> started".to_string(),
        }];

        let output =
            build_output_from_assignments(lines, assignments, true, false, Duration::default())
                .unwrap();

        assert_eq!(
            output.line_results[0].parameter_list,
            Some(vec!["42".to_string()])
        );
    }

    #[test]
    fn build_output_duplicate_line_numbers_error() {
        let lines = vec![
            make_parsed_line(1, "a"),
            make_parsed_line(1, "b"), // duplicate
        ];
        let assignments = vec![];

        let result =
            build_output_from_assignments(lines, assignments, false, false, Duration::default());

        assert!(matches!(
            result,
            Err(LogParseError::DuplicateLineNumber { line_number: 1 })
        ));
    }

    #[test]
    fn build_output_events_count_occurrences() {
        let lines = vec![
            make_parsed_line(1, "a"),
            make_parsed_line(2, "a"),
            make_parsed_line(3, "b"),
        ];
        let assignments = vec![
            LearnedTemplateAssignment {
                line_number: 1,
                cluster_id: 0,
                template: "a".to_string(),
            },
            LearnedTemplateAssignment {
                line_number: 2,
                cluster_id: 0,
                template: "a".to_string(),
            },
            LearnedTemplateAssignment {
                line_number: 3,
                cluster_id: 1,
                template: "b".to_string(),
            },
        ];

        let output =
            build_output_from_assignments(lines, assignments, false, false, Duration::default())
                .unwrap();

        assert_eq!(output.events.len(), 2);
        assert_eq!(output.events[0].occurrences, 2); // "a" appears twice
        assert_eq!(output.events[1].occurrences, 1); // "b" appears once
    }
}

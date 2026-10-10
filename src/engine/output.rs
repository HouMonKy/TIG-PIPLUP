//! Build [`ParseOutput`] from parser state or session state.
//!
//! Output construction assigns each parsed line to a template variant within
//! its cluster, extracts parameters, and aggregates event records.

use std::{collections::BTreeMap, time::Duration};

use crate::{
    error::LogParseError,
    model::ParseState,
    parse_core::ensure_unique_line_numbers,
    template::{CompiledTemplate as RuntimeTemplate, event_id_for_template, template_matches},
    types::{LineResult, ParseOutput, ParsedLine},
};

#[derive(Debug, Clone, PartialEq, Eq)]
struct LineAssignment {
    event_id: String,
    event_template: String,
    parameter_list: Option<Vec<String>>,
}

#[derive(Debug)]
struct CompiledTemplate {
    template: String,
    event_id: String,
    runtime: RuntimeTemplate,
}

struct TemplateMatch<'a> {
    template: &'a str,
    event_id: &'a str,
    content: &'a str,
}

/// Build [`ParseOutput`] from parsed lines and the final parser state.
///
/// This is the output path used by [`PiplupParser`](crate::engine::parser::PiplupParser).
pub fn build_output(
    parsed_lines: Vec<ParsedLine>,
    state: ParseState,
    keep_para: bool,
    warn_miss: bool,
    duration: Duration,
) -> Result<ParseOutput, LogParseError> {
    ensure_unique_line_numbers(parsed_lines.iter().map(|l| l.line_number))?;

    let mut assignments = vec![None; parsed_lines.len()];
    let line_indexes = parsed_lines
        .iter()
        .enumerate()
        .map(|(index, line)| (line.line_number, index))
        .collect::<BTreeMap<_, _>>();
    let mut accumulator = crate::output_core::EventAccumulator::new();
    let mut extractor = crate::output_core::ParameterExtractor::new(keep_para);

    for cluster in &state.clusters {
        let templates = if cluster.template_list.is_empty() {
            vec![cluster.active_template()]
        } else {
            dedup_templates(&cluster.template_list)
        };

        if templates.len() == 1 {
            let template = templates[0].clone();

            for log_id in &cluster.log_ids {
                let Some(&line_index) = line_indexes.get(log_id) else {
                    continue;
                };
                let line = &parsed_lines[line_index];
                accumulator.record_template(&template, 1);
                assignments[line_index] = Some(LineAssignment {
                    event_id: event_id_for_template(&template),
                    event_template: template.clone(),
                    parameter_list: extractor.for_template(&template, &line.content),
                });
            }
            continue;
        }

        let compiled = compile_templates(&templates)?;
        let active_template = cluster.active_template();
        let active_fallback = if active_template != "<*>"
            || templates
                .iter()
                .any(|template| template == &active_template)
        {
            None
        } else {
            Some(CompiledTemplate {
                event_id: event_id_for_template(&active_template),
                runtime: RuntimeTemplate::compile(&active_template),
                template: active_template,
            })
        };

        for log_id in &cluster.log_ids {
            let Some(&line_index) = line_indexes.get(log_id) else {
                continue;
            };
            let line = &parsed_lines[line_index];
            let Some(assigned) = find_matching_template(&compiled, &line.content).or_else(|| {
                active_fallback.as_ref().and_then(|fallback| {
                    find_matching_template(std::slice::from_ref(fallback), &line.content)
                })
            }) else {
                if warn_miss {
                    eprintln!("[Warning] No template matched for log: {}", line.content);
                    eprintln!(
                        "          -> Candidate Templates in this cluster: {:?}",
                        templates
                    );
                }
                continue;
            };

            accumulator.record_template(assigned.template, 1);
            assignments[line_index] = Some(LineAssignment {
                event_id: assigned.event_id.to_string(),
                event_template: assigned.template.to_string(),
                parameter_list: extractor.for_template(assigned.template, assigned.content),
            });
        }
    }

    let line_results = parsed_lines
        .into_iter()
        .enumerate()
        .map(|(index, line)| {
            let assigned = assignments[index]
                .clone()
                .unwrap_or_else(|| LineAssignment {
                    event_id: "0".to_string(),
                    event_template: "0".to_string(),
                    parameter_list: extractor.unmatched(),
                });
            LineResult {
                line_number: line.line_number,
                raw: line.raw,
                fields: line.fields,
                event_id: assigned.event_id,
                event_template: assigned.event_template,
                parameter_list: assigned.parameter_list,
            }
        })
        .collect();

    Ok(ParseOutput {
        line_results,
        events: accumulator.into_events(),
        cluster_count: state.clusters.len(),
        duration,
    })
}

/// Build [`TrainingOutput`] from parsed lines and a [`SessionState`].
///
/// This is the output path used by [`TrainingSession`](crate::engine::session::TrainingSession).
pub fn build_training_output(
    parsed_lines: Vec<ParsedLine>,
    session_state: crate::engine::session::SessionState,
    keep_para: bool,
    warn_miss: bool,
    duration: Duration,
) -> Result<crate::types::TrainingOutput, LogParseError> {
    let parse_output = build_output(
        parsed_lines,
        session_state.state,
        keep_para,
        warn_miss,
        duration,
    )?;
    Ok(crate::types::TrainingOutput {
        parse_output,
        preprocess_plan: session_state.preprocess_plan,
    })
}

fn compile_templates(templates: &[String]) -> Result<Vec<CompiledTemplate>, LogParseError> {
    Ok(templates
        .iter()
        .map(|template| CompiledTemplate {
            template: template.clone(),
            event_id: event_id_for_template(template),
            runtime: RuntimeTemplate::compile(template),
        })
        .collect())
}

fn find_matching_template<'a>(
    templates: &'a [CompiledTemplate],
    content: &'a str,
) -> Option<TemplateMatch<'a>> {
    templates
        .iter()
        .find_map(|candidate| match_template_content(candidate, content))
}

fn match_template_content<'a>(
    candidate: &'a CompiledTemplate,
    content: &'a str,
) -> Option<TemplateMatch<'a>> {
    let matched_content = if template_matches(&candidate.runtime, content) {
        content
    } else {
        let trimmed = content.trim();
        if trimmed == content || !template_matches(&candidate.runtime, trimmed) {
            return None;
        }
        trimmed
    };

    Some(TemplateMatch {
        template: &candidate.template,
        event_id: &candidate.event_id,
        content: matched_content,
    })
}

fn dedup_templates(templates: &[String]) -> Vec<String> {
    let mut deduped = Vec::with_capacity(templates.len());
    for template in templates {
        if !deduped.iter().any(|existing| existing == template) {
            deduped.push(template.clone());
        }
    }
    deduped
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use super::*;

    fn training_entry(line_number: usize, content: &str) -> crate::LogEntry {
        crate::LogEntry {
            line_number,
            raw: format!("2026-04-11 12:00:{:02} {content}", line_number % 60),
        }
    }

    #[test]
    fn build_training_output_matches_one_shot_training_except_duration() {
        let config = crate::PiplupConfig {
            keep_para: true,
            ..crate::PiplupConfig::default()
        };
        let entries = vec![
            training_entry(1, "INFO worker alpha ready"),
            training_entry(2, "INFO worker beta ready"),
            training_entry(3, "WARN disk sda full"),
            training_entry(4, "INFO worker gamma ready"),
        ];
        let mut parser =
            crate::PiplupParser::new(config.clone()).expect("one-shot parser should build");
        let expected = parser
            .parse_lines_for_training(&entries)
            .expect("one-shot training should succeed");
        let mut session = crate::TrainingSession::builder(config.clone())
            .retain_parsed(true)
            .build()
            .expect("training session should build");

        for chunk in entries.chunks(2) {
            session
                .commit_batch(chunk)
                .expect("session batch should commit");
        }

        let (session_state, parsed_lines) = session
            .try_finish_with_parsed()
            .expect("retained parsed lines should be available");
        let actual = build_training_output(
            parsed_lines,
            session_state,
            config.keep_para,
            config.warn_miss,
            Duration::ZERO,
        )
        .expect("training output should build");

        assert_eq!(
            actual.parse_output.line_results,
            expected.parse_output.line_results
        );
        assert_eq!(actual.parse_output.events, expected.parse_output.events);
        assert_eq!(
            actual.parse_output.cluster_count,
            expected.parse_output.cluster_count
        );
        assert_eq!(actual.preprocess_plan, expected.preprocess_plan);
    }
}

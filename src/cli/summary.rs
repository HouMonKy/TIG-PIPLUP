use std::{path::Path, time::Duration};

use crate::ParseOutput;

use super::{
    STRUCTURED_NAME, TEMPLATES_NAME,
    types::{
        DirectoryIndexBuildStats, DirectoryRunStats, OutputPlan, OutputSink, ResolvedOutputPlan,
    },
};

pub(super) fn print_summary(output: &ParseOutput, plan: &ResolvedOutputPlan) {
    eprintln!(
        "parsed {} lines into {} templates in {}",
        output.line_results.len(),
        output.template_count(),
        format_duration(output.duration)
    );

    match plan {
        ResolvedOutputPlan::Directory(dir) => {
            eprintln!(
                "wrote structured output to {}",
                dir.join(STRUCTURED_NAME).display()
            );
            eprintln!(
                "wrote template summary to {}",
                dir.join(TEMPLATES_NAME).display()
            );
        }
        ResolvedOutputPlan::Separate {
            structured,
            templates,
        } => {
            eprintln!(
                "wrote structured output to {}",
                describe_sink(structured, STRUCTURED_NAME)
            );
            eprintln!(
                "wrote template summary to {}",
                describe_sink(templates, TEMPLATES_NAME)
            );
        }
    }
}

pub(super) fn print_indexed_summary(
    input: &str,
    templates_idx: &Path,
    plan: &ResolvedOutputPlan,
    output: &crate::IndexedParseOutput,
) {
    eprintln!(
        "parsed {} with index {}: {} lines, {} templates, {} misses in {}",
        input,
        templates_idx.display(),
        output.stats.line_count,
        output.parse_output.template_count(),
        output.stats.miss_count,
        format_duration(output.parse_output.duration)
    );
    if let Some(first_miss_line) = output.stats.first_miss_line {
        eprintln!("first miss at line {}", first_miss_line);
    }
    eprintln!(
        "miss breakdown: {} training misses, {} new misses",
        output.stats.training_miss_count, output.stats.new_miss_count
    );
    if let Some(first_new_miss_line) = output.stats.first_new_miss_line {
        eprintln!("first new miss at line {}", first_new_miss_line);
    }
    match plan {
        ResolvedOutputPlan::Directory(dir) => {
            eprintln!(
                "wrote structured output to {}",
                dir.join(STRUCTURED_NAME).display()
            );
            eprintln!(
                "wrote template summary to {}",
                dir.join(TEMPLATES_NAME).display()
            );
        }
        ResolvedOutputPlan::Separate {
            structured,
            templates,
        } => {
            eprintln!(
                "wrote structured output to {}",
                describe_sink(structured, STRUCTURED_NAME)
            );
            eprintln!(
                "wrote template summary to {}",
                describe_sink(templates, TEMPLATES_NAME)
            );
        }
    }
}

pub(super) fn print_directory_summary(stats: &DirectoryRunStats, plan: &OutputPlan) {
    eprintln!(
        "parsed {} lines from {} log files into {} templates across {} leaf directories in {}",
        stats.line_count,
        stats.file_count,
        stats.template_count,
        stats.leaf_dir_count,
        format_duration(stats.duration)
    );

    match plan {
        OutputPlan::MirroredDirectory {
            output_root: Some(root),
            ..
        } => eprintln!(
            "wrote per-file structured outputs and one templates.csv per leaf directory under {}",
            root.display()
        ),
        OutputPlan::MirroredDirectory {
            output_root: None, ..
        } => eprintln!(
            "wrote per-file structured outputs alongside logs and one templates.csv per leaf directory"
        ),
        OutputPlan::Directory(dir) => eprintln!("wrote outputs to {}", dir.display()),
        OutputPlan::Separate { .. } => unreachable!("directory inputs reject separate outputs"),
    }
}

pub(super) fn print_build_index_directory_summary(
    stats: &DirectoryIndexBuildStats,
    input_root: &Path,
    out_dir: Option<&Path>,
) {
    if stats.resume_line_count > 0 {
        let new_lines = stats.line_count.saturating_sub(stats.resume_line_count);
        eprintln!(
            "built {} leaf indexes from {} log files ({} lines: {} restored, {} new, {} templates) in {}",
            stats.leaf_dir_count,
            stats.file_count,
            stats.line_count,
            stats.resume_line_count,
            new_lines,
            stats.template_count,
            format_duration(stats.duration)
        );
    } else {
        eprintln!(
            "built {} leaf indexes from {} log files ({} lines, {} templates) in {}",
            stats.leaf_dir_count,
            stats.file_count,
            stats.line_count,
            stats.template_count,
            format_duration(stats.duration)
        );
    }
    match out_dir {
        Some(root) => eprintln!(
            "wrote one templates.idx and templates.csv per leaf directory under {}",
            root.display()
        ),
        None => eprintln!(
            "wrote one templates.idx and templates.csv into each leaf directory under {}",
            input_root.display()
        ),
    }
}

pub(super) fn describe_sink(sink: &OutputSink, default_name: &str) -> String {
    match sink {
        OutputSink::Stdout => "stdout".to_string(),
        OutputSink::Path(path) => {
            if path.as_os_str().is_empty() {
                default_name.to_string()
            } else {
                path.display().to_string()
            }
        }
    }
}

pub(super) fn format_duration(duration: Duration) -> String {
    if duration.as_secs() > 0 {
        format!("{:.3}s", duration.as_secs_f64())
    } else if duration.as_millis() > 0 {
        format!("{}ms", duration.as_millis())
    } else {
        format!("{}us", duration.as_micros())
    }
}

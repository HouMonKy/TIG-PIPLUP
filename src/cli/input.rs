use std::{
    io::{self, BufRead},
    path::{Path, PathBuf},
};

use anyhow::Context;

use crate::{LogEntry, ParseOutput, PiplupParser, input_plan, progress::ProgressReporter};

use super::{INPUT_STDIN, STDIN_PROGRESS_STEP, args::cli_error, types::InputPlan};

pub(super) fn parse_stdin<W: io::Write>(
    parser: &mut PiplupParser,
    progress: &mut ProgressReporter<W>,
) -> anyhow::Result<ParseOutput> {
    progress.status("reading stdin")?;
    let stdin = io::stdin();
    let mut next_progress = STDIN_PROGRESS_STEP;
    let entries = stdin
        .lock()
        .lines()
        .enumerate()
        .map(|(index, line)| -> Result<LogEntry, io::Error> {
            let entry = LogEntry {
                line_number: index + 1,
                raw: line?,
            };
            if progress.is_enabled() && entry.line_number >= next_progress {
                progress.stdin_read_progress(entry.line_number)?;
                next_progress = next_stdin_progress_step(entry.line_number);
            }
            Ok(entry)
        })
        .collect::<Result<Vec<_>, io::Error>>()?;

    if progress.is_enabled() && !entries.is_empty() {
        progress.stdin_read_progress(entries.len())?;
    }
    progress.status("parsing stdin")?;

    let mut progress_error = None;
    let output = if progress.is_enabled() {
        parser.parse_lines_with_progress(&entries, |update| {
            if progress_error.is_none() {
                progress_error = progress.stdin_parse_progress(update.processed_lines).err();
            }
        })
    } else {
        parser.parse_lines(&entries)
    };
    if let Some(error) = progress_error {
        return Err(error.into());
    }
    Ok(output?)
}

pub(super) fn read_stdin_entries<W: io::Write>(
    progress: &mut ProgressReporter<W>,
) -> anyhow::Result<Vec<LogEntry>> {
    progress.status("reading stdin")?;
    let stdin = io::stdin();
    let mut next_progress = STDIN_PROGRESS_STEP;
    let entries = stdin
        .lock()
        .lines()
        .enumerate()
        .map(|(index, line)| -> Result<LogEntry, io::Error> {
            let entry = LogEntry {
                line_number: index + 1,
                raw: line?,
            };
            if progress.is_enabled() && entry.line_number >= next_progress {
                progress.stdin_read_progress(entry.line_number)?;
                next_progress = next_stdin_progress_step(entry.line_number);
            }
            Ok(entry)
        })
        .collect::<Result<Vec<_>, io::Error>>()?;

    if progress.is_enabled() && !entries.is_empty() {
        progress.stdin_read_progress(entries.len())?;
    }
    Ok(entries)
}

pub(super) fn read_file_entries(path: &Path) -> Result<Vec<LogEntry>, io::Error> {
    let raw = std::fs::read_to_string(path)?;
    Ok(raw
        .lines()
        .enumerate()
        .map(|(index, line)| LogEntry {
            line_number: index + 1,
            raw: line.to_string(),
        })
        .collect())
}

pub(super) fn parse_file<W: io::Write>(
    parser: &mut PiplupParser,
    input: &Path,
    progress: &mut ProgressReporter<W>,
    show_line_progress: bool,
) -> anyhow::Result<ParseOutput> {
    let mut progress_error = None;
    let output = if progress.is_enabled() && show_line_progress {
        parser.parse_file_with_progress(input, |update| {
            if progress_error.is_none() {
                progress_error = progress.file_progress(update).err();
            }
        })
    } else {
        parser.parse_file(input)
    };
    if let Some(error) = progress_error {
        return Err(error.into());
    }

    output.with_context(|| format!("failed to parse {}", input.display()))
}

pub(super) fn build_input_plan(input: &Path) -> Result<InputPlan, clap::Error> {
    if is_stdin_path(input) {
        return Ok(InputPlan::Stdin);
    }

    let metadata = std::fs::metadata(input).map_err(|error| {
        cli_error(&format!(
            "failed to inspect input {}: {error}",
            input.display()
        ))
    })?;

    if metadata.is_file() {
        return Ok(InputPlan::File(input.to_path_buf()));
    }

    if metadata.is_dir() {
        let mut files = input_plan::collect_log_files(input).map_err(|error| {
            cli_error(&format!(
                "failed to scan directory {}: {error}",
                input.display()
            ))
        })?;
        files.sort();

        if files.is_empty() {
            return Err(cli_error(&format!(
                "no .log files found under directory {}",
                input.display()
            )));
        }

        return Ok(InputPlan::Directory {
            root: input.to_path_buf(),
            files,
        });
    }

    Err(cli_error(&format!(
        "input path must be a regular file, directory, or '-' for stdin: {}",
        input.display()
    )))
}

pub(super) use input_plan::group_files_by_leaf;

pub(super) fn input_dir_for(input: &Path) -> PathBuf {
    if is_stdin_path(input) {
        std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."))
    } else {
        input
            .parent()
            .map(Path::to_path_buf)
            .unwrap_or_else(|| PathBuf::from("."))
    }
}

pub(super) fn is_stdin_path(path: &Path) -> bool {
    path == Path::new(INPUT_STDIN)
}

pub(super) fn next_stdin_progress_step(lines: usize) -> usize {
    let steps = (lines / STDIN_PROGRESS_STEP) + 1;
    steps * STDIN_PROGRESS_STEP
}

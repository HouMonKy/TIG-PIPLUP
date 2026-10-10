//! Progress reporting types and helpers.
//!
//! This module provides the public [`ParseProgress`] snapshot and the CLI-only
//! progress reporter implementation.

use crate::error::LogParseError;

#[cfg(any(test, feature = "cli"))]
use std::{
    io::{self, Write},
    path::Path,
};

/// Controls whether and how progress is reported.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProgressMode {
    /// Show progress only when stderr is a terminal and `--quiet` is not set.
    Auto,
    /// Always show progress; use plain output when stderr is not a terminal.
    Always,
    /// Never show progress.
    Never,
}

impl ProgressMode {
    /// Parse a progress mode from a string.
    ///
    /// Accepts `"auto"`, `"always"`, or `"never"` (case-sensitive).
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::progress::ProgressMode;
    ///
    /// assert_eq!(ProgressMode::parse("auto").unwrap(), ProgressMode::Auto);
    /// assert!(ProgressMode::parse("verbose").is_err());
    /// ```
    pub fn parse(value: &str) -> Result<Self, LogParseError> {
        match value {
            "auto" => Ok(Self::Auto),
            "always" => Ok(Self::Always),
            "never" => Ok(Self::Never),
            _ => Err(LogParseError::InvalidConfig {
                field: "progress",
                message: format!(
                    "invalid progress mode '{value}': expected auto, always, or never"
                ),
            }),
        }
    }

    #[cfg(any(test, feature = "cli"))]
    pub(crate) fn resolve(self, quiet: bool, stderr_is_terminal: bool) -> ProgressDrawMode {
        if quiet {
            return ProgressDrawMode::Disabled;
        }

        match self {
            Self::Auto if stderr_is_terminal => ProgressDrawMode::Interactive,
            Self::Auto => ProgressDrawMode::Disabled,
            Self::Always if stderr_is_terminal => ProgressDrawMode::Interactive,
            Self::Always => ProgressDrawMode::Plain,
            Self::Never => ProgressDrawMode::Disabled,
        }
    }
}

/// Snapshot of parser progress for a single input source.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ParseProgress {
    /// Number of lines processed so far.
    pub processed_lines: usize,
    /// Total number of lines known in advance, if available.
    pub total_lines: Option<usize>,
}

impl ParseProgress {
    /// Create a progress snapshot.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::ParseProgress;
    ///
    /// let progress = ParseProgress::new(128, Some(512));
    /// assert_eq!(progress.processed_lines, 128);
    /// assert_eq!(progress.total_lines, Some(512));
    /// ```
    pub const fn new(processed_lines: usize, total_lines: Option<usize>) -> Self {
        Self {
            processed_lines,
            total_lines,
        }
    }
}

#[cfg(any(test, feature = "cli"))]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum ProgressDrawMode {
    Disabled,
    Interactive,
    Plain,
}

#[cfg(any(test, feature = "cli"))]
pub(crate) struct ProgressReporter<W> {
    writer: W,
    mode: ProgressDrawMode,
    transient_width: usize,
    has_transient: bool,
}

#[cfg(any(test, feature = "cli"))]
impl<W: Write> ProgressReporter<W> {
    pub(crate) fn new(writer: W, mode: ProgressDrawMode) -> Self {
        Self {
            writer,
            mode,
            transient_width: 0,
            has_transient: false,
        }
    }

    pub(crate) fn is_enabled(&self) -> bool {
        self.mode != ProgressDrawMode::Disabled
    }

    pub(crate) fn status(&mut self, message: impl AsRef<str>) -> io::Result<()> {
        if self.mode == ProgressDrawMode::Disabled {
            return Ok(());
        }

        self.finish()?;
        writeln!(self.writer, "{}", message.as_ref())?;
        self.writer.flush()
    }

    pub(crate) fn directory_file(
        &mut self,
        current: usize,
        total: usize,
        path: &Path,
    ) -> io::Result<()> {
        self.status(format!("[{current}/{total}] {}", path.display()))
    }

    pub(crate) fn file_progress(&mut self, progress: ParseProgress) -> io::Result<()> {
        let message = match progress.total_lines {
            Some(total) => format!("parsed {}/{total} lines", progress.processed_lines),
            None => format!("parsed {} lines", progress.processed_lines),
        };
        self.render_progress(message)
    }

    pub(crate) fn stdin_read_progress(&mut self, lines: usize) -> io::Result<()> {
        self.render_progress(format!("read {lines} lines from stdin"))
    }

    pub(crate) fn stdin_parse_progress(&mut self, lines: usize) -> io::Result<()> {
        self.render_progress(format!("parsed {lines} lines from stdin"))
    }

    pub(crate) fn finish(&mut self) -> io::Result<()> {
        if self.mode == ProgressDrawMode::Interactive && self.has_transient {
            writeln!(self.writer)?;
            self.writer.flush()?;
        }

        self.transient_width = 0;
        self.has_transient = false;
        Ok(())
    }

    fn render_progress(&mut self, message: String) -> io::Result<()> {
        match self.mode {
            ProgressDrawMode::Disabled => Ok(()),
            ProgressDrawMode::Plain => {
                writeln!(self.writer, "{message}")?;
                self.writer.flush()
            }
            ProgressDrawMode::Interactive => {
                let padding = self.transient_width.saturating_sub(message.len());
                write!(self.writer, "\r{message}")?;
                if padding > 0 {
                    write!(self.writer, "{:padding$}", "", padding = padding)?;
                }
                self.writer.flush()?;
                self.transient_width = message.len();
                self.has_transient = true;
                Ok(())
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn progress_mode_resolves_from_terminal_state() {
        assert_eq!(
            ProgressMode::Auto.resolve(false, true),
            ProgressDrawMode::Interactive
        );
        assert_eq!(
            ProgressMode::Auto.resolve(false, false),
            ProgressDrawMode::Disabled
        );
        assert_eq!(
            ProgressMode::Always.resolve(false, false),
            ProgressDrawMode::Plain
        );
        assert_eq!(
            ProgressMode::Never.resolve(false, true),
            ProgressDrawMode::Disabled
        );
        assert_eq!(
            ProgressMode::Always.resolve(true, true),
            ProgressDrawMode::Disabled
        );
    }

    #[test]
    fn plain_reporter_emits_predictable_lines() {
        let mut output = Vec::new();
        let mut reporter = ProgressReporter::new(&mut output, ProgressDrawMode::Plain);

        assert!(reporter.is_enabled());
        reporter
            .status("processing app.log")
            .expect("status should write");
        reporter
            .stdin_read_progress(1024)
            .expect("stdin read progress should write");
        reporter
            .file_progress(ParseProgress::new(128, Some(512)))
            .expect("file progress should write");
        reporter
            .stdin_parse_progress(2048)
            .expect("stdin progress should write");
        reporter
            .directory_file(2, 5, Path::new("logs/app.log"))
            .expect("directory progress should write");

        let rendered = String::from_utf8(output).expect("output should be utf-8");
        assert!(rendered.contains("processing app.log\n"));
        assert!(rendered.contains("read 1024 lines from stdin\n"));
        assert!(rendered.contains("parsed 128/512 lines\n"));
        assert!(rendered.contains("parsed 2048 lines from stdin\n"));
        assert!(rendered.contains("[2/5] logs/app.log\n"));
    }
}

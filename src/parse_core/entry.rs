use crate::error::LogParseError;
use crate::format::MultiCompiledLogFormat;
use crate::types::{LogEntry, ParsedLine};

/// Parse a single [`LogEntry`] against the configured log formats.
///
/// Returns `Ok(None)` when the line does not match any format and
/// `skip_unmatched` is enabled; otherwise returns the parsed line or an error.
pub(crate) fn parse_entry(
    format: &MultiCompiledLogFormat,
    skip_unmatched: bool,
    entry: &LogEntry,
) -> Result<Option<ParsedLine>, LogParseError> {
    match format.parse_line(entry.line_number, &entry.raw)? {
        Some(line) => Ok(Some(line)),
        None => {
            if skip_unmatched {
                Ok(None)
            } else {
                Err(LogParseError::ParseError {
                    line: entry.line_number,
                    message: "line does not match any configured log format".to_string(),
                })
            }
        }
    }
}

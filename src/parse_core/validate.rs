use std::collections::BTreeSet;

use crate::error::LogParseError;

/// Verify that all line numbers in `line_numbers` are unique.
///
/// Returns `DuplicateLineNumber` on the first repeated line number.
pub(crate) fn ensure_unique_line_numbers<I>(line_numbers: I) -> Result<(), LogParseError>
where
    I: IntoIterator<Item = usize>,
{
    let mut seen = BTreeSet::new();
    for line_number in line_numbers {
        if !seen.insert(line_number) {
            return Err(LogParseError::DuplicateLineNumber { line_number });
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ensure_unique_line_numbers_returns_duplicate_line_number() {
        // Unique line numbers succeed.
        assert!(ensure_unique_line_numbers([1usize, 2, 3]).is_ok());
        // A repeated line number yields DuplicateLineNumber.
        let err = ensure_unique_line_numbers([1usize, 2, 1]).unwrap_err();
        assert!(matches!(
            err,
            LogParseError::DuplicateLineNumber { line_number: 1 }
        ));
    }
}

//! Line numbering adapter.
//!
//! Line numbering is a session-level semantic, not transport behavior. It is
//! applied after ordered assembly and before the parse session receives entries.
//! The adapter is configured from `SessionPlan::line_numbering`.

use super::types::{EntryBatch, LineNumbering};

/// Adapter that applies session-level line numbering policy to ordered batches.
pub struct LineNumberingAdapter {
    policy: LineNumbering,
    /// For `ConcatenateWithinSession`, the next line number to assign.
    next_line_number: usize,
}

impl LineNumberingAdapter {
    /// Create a new adapter with the given numbering policy.
    pub fn new(policy: LineNumbering) -> Self {
        LineNumberingAdapter {
            policy,
            next_line_number: 1,
        }
    }

    /// Apply the numbering policy to a batch of entries.
    ///
    /// For `PreserveSourceLocal`, entries are returned unchanged.
    /// For `ConcatenateWithinSession`, line numbers are rewritten so all
    /// committed batches form one contiguous logical stream starting from 1.
    pub fn rewrite(&mut self, batch: &mut EntryBatch) {
        match self.policy {
            LineNumbering::PreserveSourceLocal => {
                // No rewriting needed.
            }
            LineNumbering::ConcatenateWithinSession => {
                for entry in &mut batch.entries {
                    entry.line_number = self.next_line_number;
                    self.next_line_number += 1;
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::async_api::types::{BatchOrder, SourceContext, SourceName};
    use crate::types::LogEntry;

    fn test_context() -> SourceContext {
        SourceContext {
            source_seq: 0,
            source_name: SourceName::new("test"),
        }
    }

    fn make_batch(orders: &[(usize, &str)]) -> EntryBatch {
        EntryBatch {
            order: BatchOrder(0),
            entries: orders
                .iter()
                .map(|&(line, raw)| LogEntry {
                    line_number: line,
                    raw: raw.to_owned(),
                })
                .collect(),
            context: test_context(),
        }
    }

    #[test]
    fn preserve_source_local_keeps_numbers() {
        let mut adapter = LineNumberingAdapter::new(LineNumbering::PreserveSourceLocal);
        let mut batch = make_batch(&[(1, "a"), (2, "b"), (3, "c")]);

        adapter.rewrite(&mut batch);

        assert_eq!(batch.entries[0].line_number, 1);
        assert_eq!(batch.entries[1].line_number, 2);
        assert_eq!(batch.entries[2].line_number, 3);
    }

    #[test]
    fn concatenate_rewrites_from_1() {
        let mut adapter = LineNumberingAdapter::new(LineNumbering::ConcatenateWithinSession);
        let mut batch = make_batch(&[(5, "a"), (6, "b"), (7, "c")]);

        adapter.rewrite(&mut batch);

        assert_eq!(batch.entries[0].line_number, 1);
        assert_eq!(batch.entries[1].line_number, 2);
        assert_eq!(batch.entries[2].line_number, 3);
    }

    #[test]
    fn concatenate_across_batches() {
        let mut adapter = LineNumberingAdapter::new(LineNumbering::ConcatenateWithinSession);

        let mut batch1 = make_batch(&[(1, "a"), (2, "b")]);
        adapter.rewrite(&mut batch1);
        assert_eq!(batch1.entries[0].line_number, 1);
        assert_eq!(batch1.entries[1].line_number, 2);

        let mut batch2 = make_batch(&[(1, "c"), (2, "d")]);
        adapter.rewrite(&mut batch2);
        assert_eq!(batch2.entries[0].line_number, 3);
        assert_eq!(batch2.entries[1].line_number, 4);
    }

    #[test]
    fn concatenate_empty_batch_does_not_advance() {
        let mut adapter = LineNumberingAdapter::new(LineNumbering::ConcatenateWithinSession);
        let mut empty_batch = EntryBatch {
            order: BatchOrder(0),
            entries: vec![],
            context: test_context(),
        };

        adapter.rewrite(&mut empty_batch);

        // Verify numbering starts at 1 by rewriting a non-empty batch.
        let mut batch = make_batch(&[(10, "a")]);
        adapter.rewrite(&mut batch);
        assert_eq!(batch.entries[0].line_number, 1);
    }
}

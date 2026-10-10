//! Ordered assembler implementation.
//!
//! The ordered assembler owns transport reorder state only. It receives
//! `TransportEvent` values from the source scheduler and produces ordered
//! `EntryBatch` values. It does not apply line-numbering policy and does not
//! understand source grouping semantics.

use std::collections::BTreeMap;

use super::types::{BatchOrder, EntryBatch, TransportEvent};
use crate::error::LogParseError;

/// Result of processing a transport event.
#[derive(Debug)]
pub enum AssemblerOutput {
    /// Ordered batches are ready to consume.
    Batches(Vec<EntryBatch>),
    /// All input has been consumed. No more batches will follow.
    EndOfInput,
    /// A source failure occurred. The session is aborted.
    Failed(LogParseError),
}

/// Ordered assembler state.
///
/// For each incoming `TransportEvent::Batch`, the assembler stores the batch
/// and drains all consecutive orders that are ready. For `EndOfInput`, it
/// records the exclusive end order and finishes after all orders before that
/// value have been drained. For `Failed`, it aborts the session, drops pending
/// batches, and returns the read error with source context.
pub struct OrderedAssembler {
    /// The next `BatchOrder` expected.
    next_order: BatchOrder,
    /// The exclusive end order, set when `EndOfInput` is received.
    end_order: Option<BatchOrder>,
    /// Buffered batches awaiting their turn.
    pending: BTreeMap<BatchOrder, EntryBatch>,
}

impl OrderedAssembler {
    /// Create a new assembler starting at `BatchOrder(0)`.
    pub fn new() -> Self {
        OrderedAssembler {
            next_order: BatchOrder(0),
            end_order: None,
            pending: BTreeMap::new(),
        }
    }

    /// Process a transport event and return the assembler output.
    pub fn handle_event(&mut self, event: TransportEvent) -> AssemblerOutput {
        match event {
            TransportEvent::Batch(batch) => {
                let order = batch.order;
                self.pending.insert(order, batch);
                let batches = self.drain_contiguous();
                AssemblerOutput::Batches(batches)
            }
            TransportEvent::EndOfInput { next_order } => {
                self.end_order = Some(next_order);
                // Drain any remaining contiguous batches.
                let batches = self.drain_contiguous();
                if batches.is_empty() {
                    AssemblerOutput::EndOfInput
                } else {
                    // Return the final batches; the caller should check
                    // `is_complete()` after consuming them.
                    AssemblerOutput::Batches(batches)
                }
            }
            TransportEvent::Failed { error, .. } => {
                // Drop all pending batches.
                self.pending.clear();
                self.end_order = None;
                AssemblerOutput::Failed(error)
            }
        }
    }

    /// Drain all batches that form a contiguous sequence starting from
    /// `next_order`.
    fn drain_contiguous(&mut self) -> Vec<EntryBatch> {
        let mut result = Vec::new();

        while let Some(batch) = self.pending.remove(&self.next_order) {
            self.next_order = self.next_order.next();
            result.push(batch);
        }

        result
    }

    /// Returns `true` if all expected batches have been consumed (i.e., we
    /// have reached the `EndOfInput` order).
    pub fn is_complete(&self) -> bool {
        self.end_order
            .map(|end| self.next_order >= end)
            .unwrap_or(false)
    }
}

impl Default for OrderedAssembler {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::async_api::types::{SourceContext, SourceName};
    use crate::types::LogEntry;

    fn test_context(seq: usize) -> SourceContext {
        SourceContext {
            source_seq: seq,
            source_name: SourceName::new("test"),
        }
    }

    fn make_entry_batch(order: usize, count: usize) -> EntryBatch {
        let entries = (1..=count)
            .map(|i| LogEntry {
                line_number: i,
                raw: format!("batch {order} line {i}"),
            })
            .collect();
        EntryBatch {
            order: BatchOrder(order),
            entries,
            context: test_context(0),
        }
    }

    #[test]
    fn in_order_batches_drain_immediately() {
        let mut asm = OrderedAssembler::new();

        let out = asm.handle_event(TransportEvent::Batch(make_entry_batch(0, 2)));
        match out {
            AssemblerOutput::Batches(b) => {
                assert_eq!(b.len(), 1);
                assert_eq!(b[0].order, BatchOrder(0));
            }
            _ => panic!("expected Batches"),
        }

        let out = asm.handle_event(TransportEvent::Batch(make_entry_batch(1, 3)));
        match out {
            AssemblerOutput::Batches(b) => {
                assert_eq!(b.len(), 1);
                assert_eq!(b[0].order, BatchOrder(1));
            }
            _ => panic!("expected Batches"),
        }
    }

    #[test]
    fn out_of_order_batches_buffered_then_drained() {
        let mut asm = OrderedAssembler::new();

        // Batch 1 arrives before batch 0
        let out = asm.handle_event(TransportEvent::Batch(make_entry_batch(1, 2)));
        match out {
            AssemblerOutput::Batches(b) => assert!(b.is_empty()),
            _ => panic!("expected Batches"),
        }

        // Batch 0 arrives — both drain
        let out = asm.handle_event(TransportEvent::Batch(make_entry_batch(0, 3)));
        match out {
            AssemblerOutput::Batches(b) => {
                assert_eq!(b.len(), 2);
                assert_eq!(b[0].order, BatchOrder(0));
                assert_eq!(b[1].order, BatchOrder(1));
            }
            _ => panic!("expected Batches"),
        }
    }

    #[test]
    fn end_of_input_after_all_batches() {
        let mut asm = OrderedAssembler::new();

        asm.handle_event(TransportEvent::Batch(make_entry_batch(0, 2)));
        let out = asm.handle_event(TransportEvent::EndOfInput {
            next_order: BatchOrder(1),
        });

        match out {
            AssemblerOutput::EndOfInput => {}
            _ => panic!("expected EndOfInput, got {out:?}"),
        }

        assert!(asm.is_complete());
    }

    #[test]
    fn end_of_input_with_pending_buffers() {
        let mut asm = OrderedAssembler::new();

        // Batch 0 arrives
        let out = asm.handle_event(TransportEvent::Batch(make_entry_batch(0, 2)));
        assert!(matches!(out, AssemblerOutput::Batches(_)));

        // Batch 2 arrives (1 is missing)
        let out = asm.handle_event(TransportEvent::Batch(make_entry_batch(2, 1)));
        assert!(matches!(out, AssemblerOutput::Batches(b) if b.is_empty()));

        // EndOfInput at order 3
        let out = asm.handle_event(TransportEvent::EndOfInput {
            next_order: BatchOrder(3),
        });
        // Batch 1 is still missing, so batch 2 is pending.
        // We should get Batches (empty since nothing new is contiguous) but
        // is_complete should be false.
        match out {
            AssemblerOutput::Batches(_) => {}
            AssemblerOutput::EndOfInput => {
                // This is also acceptable if no new contiguous batches
            }
            _ => panic!("expected Batches or EndOfInput"),
        }
        assert!(!asm.is_complete());

        // Now batch 1 arrives — should drain 1 and 2
        let out = asm.handle_event(TransportEvent::Batch(make_entry_batch(1, 1)));
        match out {
            AssemblerOutput::Batches(b) => {
                assert_eq!(b.len(), 2);
                assert_eq!(b[0].order, BatchOrder(1));
                assert_eq!(b[1].order, BatchOrder(2));
            }
            _ => panic!("expected Batches"),
        }
        assert!(asm.is_complete());
    }

    #[test]
    fn failed_drops_pending_batches() {
        let mut asm = OrderedAssembler::new();

        asm.handle_event(TransportEvent::Batch(make_entry_batch(0, 2)));

        let out = asm.handle_event(TransportEvent::Failed {
            context: test_context(0),
            error: std::io::Error::new(std::io::ErrorKind::BrokenPipe, "fail").into(),
        });

        match out {
            AssemblerOutput::Failed(_) => {}
            _ => panic!("expected Failed"),
        }

        assert!(!asm.is_complete());
    }

    #[test]
    fn empty_session_end_of_input() {
        let mut asm = OrderedAssembler::new();

        let out = asm.handle_event(TransportEvent::EndOfInput {
            next_order: BatchOrder(0),
        });
        assert!(matches!(out, AssemblerOutput::EndOfInput));
        assert!(asm.is_complete());
    }

    #[test]
    fn multiple_out_of_order_gaps() {
        let mut asm = OrderedAssembler::new();

        // Deliver 2, 0, 1 — should drain all three when 1 fills the gap
        asm.handle_event(TransportEvent::Batch(make_entry_batch(2, 1)));
        let out0 = asm.handle_event(TransportEvent::Batch(make_entry_batch(0, 1)));
        // 0 drains but 1 is still missing
        match &out0 {
            AssemblerOutput::Batches(b) => {
                assert_eq!(b.len(), 1);
                assert_eq!(b[0].order, BatchOrder(0));
            }
            _ => panic!("expected Batches"),
        }

        // 1 arrives — should drain 1 and 2
        let out1 = asm.handle_event(TransportEvent::Batch(make_entry_batch(1, 1)));
        match &out1 {
            AssemblerOutput::Batches(b) => {
                assert_eq!(b.len(), 2);
                assert_eq!(b[0].order, BatchOrder(1));
                assert_eq!(b[1].order, BatchOrder(2));
            }
            _ => panic!("expected Batches"),
        }
    }
}

//! Source scheduler implementation.
//!
//! The source scheduler owns source activation and source completion. It
//! decides which source tasks are running, buffers source-local batches when
//! needed, and translates source-local order into a contiguous transport order
//! (`BatchOrder`).

use std::collections::BTreeMap;
use std::collections::BTreeSet;

use super::types::{BatchOrder, EntryBatch, SchedulerConfig, SourceTaskEvent, TransportEvent};

/// State for the source scheduler.
///
/// The scheduler maps source-local batches into contiguous `BatchOrder` values
/// and emits `TransportEvent` values through a bounded channel.
pub struct SourceScheduler {
    /// Next source sequence to activate.
    next_source_seq: usize,
    /// Maximum number of source tasks that may run concurrently.
    max_concurrent_sources: usize,
    /// Currently active source sequence numbers.
    active_sources: BTreeSet<usize>,
    /// Completed source extents: source_seq → number of local batches produced.
    completed_extents: BTreeMap<usize, usize>,
    /// Next `BatchOrder` value to assign.
    next_batch_order: BatchOrder,
    /// Per-source local batch counter: source_seq → next local batch seq expected.
    next_local_batch: BTreeMap<usize, usize>,
    /// Bounded prefetch buffer for batches from later sources awaiting
    /// earlier-source completion. Keyed by (source_seq, local_batch_seq).
    prefetch_buffer: BTreeMap<(usize, usize), super::types::SourceBatch>,
    /// Maximum number of batches in the prefetch buffer.
    max_prefetch_batches: usize,
}

impl SourceScheduler {
    /// Create a new scheduler with the given configuration.
    ///
    /// Use [`SchedulerConfig::validate()`] to check configuration before
    /// constructing the scheduler.
    pub fn new(config: &SchedulerConfig) -> Self {
        SourceScheduler {
            next_source_seq: 0,
            max_concurrent_sources: config.max_concurrent_sources_per_session,
            active_sources: BTreeSet::new(),
            completed_extents: BTreeMap::new(),
            next_batch_order: BatchOrder(0),
            next_local_batch: BTreeMap::new(),
            prefetch_buffer: BTreeMap::new(),
            max_prefetch_batches: config.max_scheduler_prefetch_batches,
        }
    }

    /// Process a source task event and return transport events to emit.
    ///
    /// For single-source sessions (`max_concurrent_sources = 1`), batches
    /// arrive in order and are assigned contiguous `BatchOrder` values
    /// directly. For multi-source sessions with prefetch, batches from later
    /// sources are buffered until earlier sources complete.
    pub fn handle_event(&mut self, event: SourceTaskEvent) -> Vec<TransportEvent> {
        match event {
            SourceTaskEvent::Batch(batch) => self.handle_batch(batch),
            SourceTaskEvent::Finished { source_seq } => self.handle_finished(source_seq),
            SourceTaskEvent::Failed {
                source_seq: _,
                local_batch_seq: _,
                context,
                error,
            } => {
                // Source failure aborts the session immediately.
                vec![TransportEvent::Failed { context, error }]
            }
        }
    }

    /// Process a source-local batch.
    ///
    /// For the default single-source case, the batch maps directly to the next
    /// `BatchOrder`. For multi-source with prefetch, the batch may be buffered
    /// if it comes from a later source while an earlier source is still active.
    fn handle_batch(&mut self, batch: super::types::SourceBatch) -> Vec<TransportEvent> {
        let source_seq = batch.source_seq;
        let local_seq = batch.local_batch_seq;

        // Check if this source is the current front (next expected to emit).
        let is_front = self.is_front_source(source_seq);

        if is_front {
            // This batch is from the current front source or can be emitted now.
            let mut events = Vec::new();
            events.push(self.emit_batch(batch));

            // Drain any buffered batches that are now contiguous.
            events.extend(self.drain_contiguous());
            events
        } else {
            // Buffer this batch for later emission.
            debug_assert!(
                self.prefetch_buffer.len() < self.max_prefetch_batches,
                "prefetch buffer overflow: multi-source prefetch is not yet supported"
            );
            self.prefetch_buffer.insert((source_seq, local_seq), batch);
            Vec::new()
        }
    }

    /// Handle source completion.
    ///
    /// When a source finishes, check if we can activate the next source and
    /// drain any buffered batches that are now contiguous.
    fn handle_finished(&mut self, source_seq: usize) -> Vec<TransportEvent> {
        self.active_sources.remove(&source_seq);

        // Record the extent of this completed source.
        let extent = self.next_local_batch.remove(&source_seq).unwrap_or(0);
        self.completed_extents.insert(source_seq, extent);

        // For ordered activation (default), when source N finishes,
        // we can now assign final BatchOrder values to any buffered
        // batches from source N+1, N+2, etc.
        let mut events = self.drain_contiguous();

        // Check if all sources are done.
        if self.active_sources.is_empty() && self.next_source_seq == source_seq + 1 {
            // Single source finished.
            events.push(TransportEvent::EndOfInput {
                next_order: self.next_batch_order,
            });
        } else if self.all_sources_completed() {
            events.push(TransportEvent::EndOfInput {
                next_order: self.next_batch_order,
            });
        }

        events
    }

    /// Assign a `BatchOrder` to a batch and wrap it in a `TransportEvent`.
    fn emit_batch(&mut self, batch: super::types::SourceBatch) -> TransportEvent {
        let source_seq = batch.source_seq;
        let order = self.next_batch_order;
        self.next_batch_order = self.next_batch_order.next();

        // Track local batch progression for this source.
        let current = self.next_local_batch.entry(source_seq).or_insert(0);
        debug_assert_eq!(*current, batch.local_batch_seq);
        *current += 1;

        TransportEvent::Batch(EntryBatch {
            order,
            entries: batch.entries,
            context: batch.context,
        })
    }

    /// Drain buffered batches that are now contiguous with the current
    /// `BatchOrder`.
    ///
    /// For the default ordered-activation mode (one source at a time), this
    /// is a no-op because batches arrive in order. For future multi-source
    /// prefetch mode, this drains buffered batches from completed later sources.
    fn drain_contiguous(&mut self) -> Vec<TransportEvent> {
        let mut events = Vec::new();

        loop {
            // Look for the next buffered batch keyed by source-local order.
            // For ordered activation, after source N completes, source N+1's
            // batches may be buffered.
            let next_local = self
                .next_local_batch
                .get(&self.next_source_seq)
                .copied()
                .unwrap_or(0);
            let key = (self.next_source_seq, next_local);

            if let Some(batch) = self.prefetch_buffer.remove(&key) {
                events.push(self.emit_batch(batch));
            } else {
                break;
            }
        }

        events
    }

    /// Check if the given source is the "front" source — i.e., the next one
    /// whose batches should receive `BatchOrder` values.
    fn is_front_source(&self, source_seq: usize) -> bool {
        // For single-source mode (max_concurrent = 1), only one source is
        // active at a time and it's always the front.
        if self.max_concurrent_sources == 1 {
            return true;
        }
        // For multi-source mode, the front is the lowest active source that
        // hasn't completed yet.
        source_seq == self.next_source_seq
    }

    /// Check if all planned sources have completed.
    fn all_sources_completed(&self) -> bool {
        self.active_sources.is_empty()
            && !self.completed_extents.is_empty()
            && self
                .completed_extents
                .keys()
                .max()
                .map(|&max| max + 1 == self.next_source_seq)
                .unwrap_or(false)
    }

    /// Notify the scheduler that a source task is being started.
    ///
    /// Call this before spawning the source task.
    pub fn activate_source(&mut self, source_seq: usize) {
        self.active_sources.insert(source_seq);
        self.next_local_batch.insert(source_seq, 0);
        // Update next_source_seq to track the highest activated source.
        if source_seq >= self.next_source_seq {
            self.next_source_seq = source_seq + 1;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::async_api::types::{SourceBatch, SourceContext, SourceName};

    fn test_context(seq: usize) -> SourceContext {
        SourceContext {
            source_seq: seq,
            source_name: SourceName::new("test"),
        }
    }

    fn make_batch(source_seq: usize, local_batch_seq: usize, count: usize) -> SourceBatch {
        let entries = (1..=count)
            .map(|i| crate::types::LogEntry {
                line_number: (local_batch_seq * 10) + i,
                raw: format!("source {source_seq} batch {local_batch_seq} line {i}"),
            })
            .collect();
        SourceBatch {
            source_seq,
            local_batch_seq,
            entries,
            context: test_context(source_seq),
        }
    }

    #[test]
    fn single_source_maps_batches_to_contiguous_order() {
        let config = SchedulerConfig::default();
        let mut scheduler = SourceScheduler::new(&config);
        scheduler.activate_source(0);

        let batch0 = make_batch(0, 0, 3);
        let batch1 = make_batch(0, 1, 2);

        let events0 = scheduler.handle_event(SourceTaskEvent::Batch(batch0));
        let events1 = scheduler.handle_event(SourceTaskEvent::Batch(batch1));
        let events2 = scheduler.handle_event(SourceTaskEvent::Finished { source_seq: 0 });

        assert_eq!(events0.len(), 1);
        assert_eq!(events1.len(), 1);
        // Finished produces EndOfInput
        assert_eq!(events2.len(), 1);

        // Verify BatchOrder is contiguous
        match &events0[0] {
            TransportEvent::Batch(b) => assert_eq!(b.order, BatchOrder(0)),
            _ => panic!("expected Batch"),
        }
        match &events1[0] {
            TransportEvent::Batch(b) => assert_eq!(b.order, BatchOrder(1)),
            _ => panic!("expected Batch"),
        }
        match &events2[0] {
            TransportEvent::EndOfInput { next_order } => {
                assert_eq!(*next_order, BatchOrder(2));
            }
            _ => panic!("expected EndOfInput"),
        }
    }

    #[test]
    fn single_source_empty_input_produces_end_of_input() {
        let config = SchedulerConfig::default();
        let mut scheduler = SourceScheduler::new(&config);
        scheduler.activate_source(0);

        let events = scheduler.handle_event(SourceTaskEvent::Finished { source_seq: 0 });
        assert_eq!(events.len(), 1);
        match &events[0] {
            TransportEvent::EndOfInput { next_order } => {
                assert_eq!(*next_order, BatchOrder(0));
            }
            _ => panic!("expected EndOfInput"),
        }
    }

    #[test]
    fn source_failure_aborts_immediately() {
        let config = SchedulerConfig::default();
        let mut scheduler = SourceScheduler::new(&config);
        scheduler.activate_source(0);

        let events = scheduler.handle_event(SourceTaskEvent::Failed {
            source_seq: 0,
            local_batch_seq: 0,
            context: test_context(0),
            error: std::io::Error::new(std::io::ErrorKind::BrokenPipe, "read error").into(),
        });

        assert_eq!(events.len(), 1);
        match &events[0] {
            TransportEvent::Failed { context, .. } => {
                assert_eq!(context.source_seq, 0);
            }
            _ => panic!("expected Failed"),
        }
    }

    #[test]
    fn multi_source_ordered_activation() {
        let config = SchedulerConfig {
            max_concurrent_sources_per_session: 1,
            ..SchedulerConfig::default()
        };
        let mut scheduler = SourceScheduler::new(&config);

        // Activate source 0
        scheduler.activate_source(0);

        // Source 0 batch
        let events = scheduler.handle_event(SourceTaskEvent::Batch(make_batch(0, 0, 2)));
        assert_eq!(events.len(), 1);
        match &events[0] {
            TransportEvent::Batch(b) => assert_eq!(b.order, BatchOrder(0)),
            _ => panic!("expected Batch"),
        }

        // Source 0 finishes
        let events = scheduler.handle_event(SourceTaskEvent::Finished { source_seq: 0 });
        // Should produce EndOfInput since only 1 source was activated
        match &events[0] {
            TransportEvent::EndOfInput { next_order } => {
                assert_eq!(*next_order, BatchOrder(1));
            }
            _ => panic!("expected EndOfInput"),
        }
    }

    #[test]
    fn batch_entries_preserved() {
        let config = SchedulerConfig::default();
        let mut scheduler = SourceScheduler::new(&config);
        scheduler.activate_source(0);

        let batch = make_batch(0, 0, 5);
        let events = scheduler.handle_event(SourceTaskEvent::Batch(batch));

        match &events[0] {
            TransportEvent::Batch(b) => {
                assert_eq!(b.entries.len(), 5);
                assert_eq!(b.entries[0].raw, "source 0 batch 0 line 1");
                assert_eq!(b.entries[4].raw, "source 0 batch 0 line 5");
            }
            _ => panic!("expected Batch"),
        }
    }

    #[test]
    fn multiple_batches_produce_contiguous_orders() {
        let config = SchedulerConfig::default();
        let mut scheduler = SourceScheduler::new(&config);
        scheduler.activate_source(0);

        // Emit 5 batches then finish.
        let mut orders = Vec::new();
        for i in 0..5 {
            let events = scheduler.handle_event(SourceTaskEvent::Batch(make_batch(0, i, 1)));
            for event in events {
                if let TransportEvent::Batch(b) = event {
                    orders.push(b.order);
                }
            }
        }
        let _ = scheduler.handle_event(SourceTaskEvent::Finished { source_seq: 0 });

        // Orders should be 0, 1, 2, 3, 4.
        assert_eq!(orders.len(), 5);
        for (i, order) in orders.iter().enumerate() {
            assert_eq!(
                order.0, i,
                "expected BatchOrder({i}), got BatchOrder({})",
                order.0
            );
        }
    }

    #[test]
    fn two_sources_ordered_activation_sequential_orders() {
        let config = SchedulerConfig {
            max_concurrent_sources_per_session: 1,
            ..SchedulerConfig::default()
        };
        let mut scheduler = SourceScheduler::new(&config);

        // Activate source 0, send its batches, finish it.
        scheduler.activate_source(0);
        let mut all_orders = Vec::new();

        for i in 0..3 {
            let events = scheduler.handle_event(SourceTaskEvent::Batch(make_batch(0, i, 2)));
            for event in events {
                if let TransportEvent::Batch(b) = event {
                    all_orders.push(b.order);
                }
            }
        }

        // Source 0 finishes — in ordered mode with 1 source activated,
        // this produces EndOfInput.
        let finish_events = scheduler.handle_event(SourceTaskEvent::Finished { source_seq: 0 });

        // Verify we got batches 0, 1, 2 from source 0.
        assert_eq!(
            all_orders,
            vec![BatchOrder(0), BatchOrder(1), BatchOrder(2)]
        );

        // EndOfInput should indicate next_order = 3.
        match &finish_events[0] {
            TransportEvent::EndOfInput { next_order } => {
                assert_eq!(*next_order, BatchOrder(3));
            }
            _ => panic!("expected EndOfInput"),
        }
    }

    #[test]
    fn entries_count_preserved_across_batches() {
        let config = SchedulerConfig::default();
        let mut scheduler = SourceScheduler::new(&config);
        scheduler.activate_source(0);

        let mut total_entries = 0;
        for i in 0..4 {
            let count = match i {
                0 => 5,
                1 => 3,
                2 => 7,
                3 => 1,
                _ => unreachable!(),
            };
            let events = scheduler.handle_event(SourceTaskEvent::Batch(make_batch(0, i, count)));
            for event in events {
                if let TransportEvent::Batch(b) = event {
                    total_entries += b.entries.len();
                }
            }
        }

        assert_eq!(total_entries, 5 + 3 + 7 + 1);
    }

    // -----------------------------------------------------------------------
    // Multi-source ordered activation tests
    // -----------------------------------------------------------------------

    #[test]
    fn two_sources_sequential_activation() {
        let config = SchedulerConfig {
            max_concurrent_sources_per_session: 1,
            ..SchedulerConfig::default()
        };
        let mut scheduler = SourceScheduler::new(&config);

        // Activate both sources up front so the scheduler knows the total extent.
        scheduler.activate_source(0);
        scheduler.activate_source(1);

        // Source 0 sends 2 batches.
        let events0 = scheduler.handle_event(SourceTaskEvent::Batch(make_batch(0, 0, 2)));
        let events1 = scheduler.handle_event(SourceTaskEvent::Batch(make_batch(0, 1, 3)));

        let mut orders = Vec::new();
        for e in events0.iter().chain(events1.iter()) {
            if let TransportEvent::Batch(b) = e {
                orders.push(b.order);
            }
        }
        assert_eq!(orders, vec![BatchOrder(0), BatchOrder(1)]);

        // Source 0 finishes — no EndOfInput yet because source 1 is still active.
        let finish_events = scheduler.handle_event(SourceTaskEvent::Finished { source_seq: 0 });
        // No EndOfInput should be emitted.
        assert!(
            finish_events
                .iter()
                .all(|e| !matches!(e, TransportEvent::EndOfInput { .. })),
            "should not emit EndOfInput when source 1 is still active"
        );

        // Source 1 sends its batch.
        let events2 = scheduler.handle_event(SourceTaskEvent::Batch(make_batch(1, 0, 4)));
        for e in &events2 {
            if let TransportEvent::Batch(b) = e {
                orders.push(b.order);
            }
        }
        assert_eq!(orders, vec![BatchOrder(0), BatchOrder(1), BatchOrder(2)]);

        // Source 1 finishes — should emit EndOfInput now.
        let finish_events = scheduler.handle_event(SourceTaskEvent::Finished { source_seq: 1 });
        assert_eq!(finish_events.len(), 1);
        match &finish_events[0] {
            TransportEvent::EndOfInput { next_order } => {
                assert_eq!(*next_order, BatchOrder(3));
            }
            _ => panic!("expected EndOfInput, got {:?}", finish_events[0]),
        }
    }

    #[test]
    fn three_sources_sequential_activation() {
        let config = SchedulerConfig {
            max_concurrent_sources_per_session: 1,
            ..SchedulerConfig::default()
        };
        let mut scheduler = SourceScheduler::new(&config);

        // Activate 3 sources up front.
        scheduler.activate_source(0);
        scheduler.activate_source(1);
        scheduler.activate_source(2);

        // Source 0: 1 batch.
        let e0 = scheduler.handle_event(SourceTaskEvent::Batch(make_batch(0, 0, 1)));
        let f0 = scheduler.handle_event(SourceTaskEvent::Finished { source_seq: 0 });
        let batch_count_0 = e0
            .iter()
            .chain(f0.iter())
            .filter(|e| matches!(e, TransportEvent::Batch(_)))
            .count();
        assert_eq!(batch_count_0, 1);
        assert!(
            f0.iter()
                .all(|e| !matches!(e, TransportEvent::EndOfInput { .. }))
        );

        // Source 1: 1 batch.
        let e1 = scheduler.handle_event(SourceTaskEvent::Batch(make_batch(1, 0, 2)));
        let f1 = scheduler.handle_event(SourceTaskEvent::Finished { source_seq: 1 });
        let batch_count_1 = e1
            .iter()
            .chain(f1.iter())
            .filter(|e| matches!(e, TransportEvent::Batch(_)))
            .count();
        assert_eq!(batch_count_1, 1);
        assert!(
            f1.iter()
                .all(|e| !matches!(e, TransportEvent::EndOfInput { .. }))
        );

        // Source 2: 1 batch.
        let e2 = scheduler.handle_event(SourceTaskEvent::Batch(make_batch(2, 0, 3)));
        let f2 = scheduler.handle_event(SourceTaskEvent::Finished { source_seq: 2 });
        let batch_count_2 = e2
            .iter()
            .chain(f2.iter())
            .filter(|e| matches!(e, TransportEvent::Batch(_)))
            .count();
        assert_eq!(batch_count_2, 1);

        // Now EndOfInput should be emitted.
        let has_end = f2
            .iter()
            .any(|e| matches!(e, TransportEvent::EndOfInput { .. }));
        assert!(has_end, "should emit EndOfInput after last source finishes");
    }

    #[test]
    fn validate_rejects_zero_concurrent() {
        let config = SchedulerConfig {
            max_concurrent_sources_per_session: 0,
            ..SchedulerConfig::default()
        };
        assert!(config.validate().is_err());
    }

    #[test]
    fn validate_rejects_multi_source() {
        let config = SchedulerConfig {
            max_concurrent_sources_per_session: 2,
            max_scheduler_prefetch_batches: 4,
            ..SchedulerConfig::default()
        };
        assert!(config.validate().is_err());
    }

    #[test]
    fn validate_accepts_single_source_without_prefetch() {
        let config = SchedulerConfig {
            max_concurrent_sources_per_session: 1,
            max_scheduler_prefetch_batches: 0,
            ..SchedulerConfig::default()
        };
        assert!(config.validate().is_ok());
    }
}

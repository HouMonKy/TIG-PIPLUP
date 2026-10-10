use std::collections::BTreeMap;
use std::time::Duration;

use super::parser::{IndexedBatchResult, IndexedParseOutput, IndexedParseStats};
use crate::error::LogParseError;
use crate::types::{EventRecord, ParseOutput};

fn min_option(a: Option<usize>, b: Option<usize>) -> Option<usize> {
    match (a, b) {
        (Some(a), Some(b)) => Some(a.min(b)),
        (Some(a), None) => Some(a),
        (None, Some(b)) => Some(b),
        (None, None) => None,
    }
}

/// Accumulates batched indexed parse results into a single output.
///
/// Use [`IndexedParser::accumulator`](crate::index::IndexedParser::accumulator) to
/// create one, then call [`try_merge`](Self::try_merge) for each batch and
/// [`finish_with_duration`](Self::finish_with_duration) at the end.
///
/// # Example
///
/// ```rust
/// use log_parser::{IndexedParser, IndexedResultAccumulator, PiplupConfig, PiplupParser, LogEntry, IndexBuilder};
///
/// let mut parser = PiplupParser::new(PiplupConfig::default()).unwrap();
/// let entries: Vec<_> = (1..=100).map(|i| LogEntry {
///     line_number: i,
///     raw: format!("2026-06-05 10:00:00 worker {i}"),
/// }).collect();
/// let training = parser.parse_lines_for_training(&entries).unwrap();
/// let index = IndexBuilder::build(&training, &PiplupConfig::default()).unwrap();
/// let indexed = IndexedParser::from_index(index, PiplupConfig::default()).unwrap();
///
/// let mut acc = indexed.accumulator();
/// for chunk in entries.chunks(25) {
///     let batch = indexed.parse_batch(chunk).unwrap();
///     acc.try_merge(batch).unwrap();
/// }
/// let output = acc.finish_with_duration(std::time::Duration::ZERO);
/// assert_eq!(output.stats.line_count, 100);
/// ```
pub struct IndexedResultAccumulator {
    line_results: Vec<crate::types::LineResult>,
    events: Vec<EventRecord>,
    event_indexes: BTreeMap<String, usize>,
    stats: IndexedParseStats,
    cluster_count: usize,
    max_input_line_number: Option<usize>,
}

impl IndexedResultAccumulator {
    /// Create a new accumulator expecting `cluster_count` clusters.
    pub fn new(cluster_count: usize) -> Self {
        Self {
            line_results: Vec::new(),
            events: Vec::new(),
            event_indexes: BTreeMap::new(),
            stats: IndexedParseStats {
                line_count: 0,
                miss_count: 0,
                first_miss_line: None,
                training_miss_count: 0,
                new_miss_count: 0,
                first_new_miss_line: None,
            },
            cluster_count,
            max_input_line_number: None,
        }
    }

    /// Return the expected cluster count.
    pub fn cluster_count(&self) -> usize {
        self.cluster_count
    }

    /// Merge a batch result into the accumulator.
    ///
    /// Returns an error if the batch's input line range overlaps with a
    /// previously merged batch.
    pub fn try_merge(&mut self, batch: IndexedBatchResult) -> Result<(), LogParseError> {
        if let (Some(batch_min), Some(max_seen)) =
            (batch.input_line_range.0, self.max_input_line_number)
            && batch_min <= max_seen
        {
            return Err(LogParseError::InvalidAlgorithmConfig {
                algorithm: "IndexedResultAccumulator",
                field: "input_line_range",
                message: format!(
                    "batch line range overlaps with previously merged lines: \
                     batch min {} <= accumulated max {}",
                    batch_min, max_seen
                ),
            });
        }

        if let Some(max) = batch.input_line_range.1 {
            self.max_input_line_number = Some(max);
        }

        self.line_results.extend(batch.line_results);

        for event in batch.events {
            if let Some(&idx) = self.event_indexes.get(&event.event_template) {
                self.events[idx].occurrences += event.occurrences;
            } else {
                let idx = self.events.len();
                self.event_indexes.insert(event.event_template.clone(), idx);
                self.events.push(event);
            }
        }

        self.stats.line_count += batch.stats.line_count;
        self.stats.miss_count += batch.stats.miss_count;
        self.stats.first_miss_line =
            min_option(self.stats.first_miss_line, batch.stats.first_miss_line);
        self.stats.training_miss_count += batch.stats.training_miss_count;
        self.stats.new_miss_count += batch.stats.new_miss_count;
        self.stats.first_new_miss_line = min_option(
            self.stats.first_new_miss_line,
            batch.stats.first_new_miss_line,
        );

        Ok(())
    }

    /// Finish accumulation and produce the final output.
    pub fn finish_with_duration(self, elapsed: Duration) -> IndexedParseOutput {
        IndexedParseOutput {
            parse_output: ParseOutput {
                line_results: self.line_results,
                events: self.events,
                cluster_count: self.cluster_count,
                duration: elapsed,
            },
            stats: self.stats,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn stats(
        line_count: usize,
        miss_count: usize,
        first_miss_line: Option<usize>,
        training_miss_count: usize,
        new_miss_count: usize,
        first_new_miss_line: Option<usize>,
    ) -> IndexedParseStats {
        IndexedParseStats {
            line_count,
            miss_count,
            first_miss_line,
            training_miss_count,
            new_miss_count,
            first_new_miss_line,
        }
    }

    fn event(template: &str, occurrences: usize) -> EventRecord {
        EventRecord {
            event_id: crate::event_id_for_template(template),
            event_template: template.to_string(),
            occurrences,
        }
    }

    fn batch(
        input_line_range: (Option<usize>, Option<usize>),
        events: Vec<EventRecord>,
        stats: IndexedParseStats,
    ) -> IndexedBatchResult {
        IndexedBatchResult {
            line_results: Vec::new(),
            events,
            stats,
            input_line_range,
        }
    }

    #[test]
    fn merges_events_and_stats_across_batches() {
        let mut accumulator = IndexedResultAccumulator::new(7);

        accumulator
            .try_merge(batch(
                (Some(1), Some(2)),
                vec![event("INFO worker ready", 2), event("WARN disk full", 1)],
                stats(2, 1, Some(2), 0, 1, Some(2)),
            ))
            .expect("first batch should merge");
        accumulator
            .try_merge(batch(
                (Some(3), Some(5)),
                vec![
                    event("INFO worker ready", 3),
                    event("ERROR database down", 1),
                ],
                stats(3, 2, Some(4), 1, 1, Some(5)),
            ))
            .expect("second batch should merge");

        let output = accumulator.finish_with_duration(Duration::ZERO);

        assert_eq!(output.parse_output.cluster_count, 7);
        assert_eq!(
            output.parse_output.events,
            vec![
                event("INFO worker ready", 5),
                event("WARN disk full", 1),
                event("ERROR database down", 1),
            ]
        );
        assert_eq!(output.stats, stats(5, 3, Some(2), 1, 2, Some(2)));
    }

    #[test]
    fn rejects_overlapping_input_ranges() {
        let mut accumulator = IndexedResultAccumulator::new(1);

        accumulator
            .try_merge(batch(
                (Some(10), Some(12)),
                Vec::new(),
                stats(3, 0, None, 0, 0, None),
            ))
            .expect("first batch should merge");

        let error = accumulator
            .try_merge(batch(
                (Some(12), Some(14)),
                Vec::new(),
                stats(3, 0, None, 0, 0, None),
            ))
            .expect_err("overlapping range should fail");

        assert!(matches!(
            error,
            LogParseError::InvalidAlgorithmConfig {
                algorithm: "IndexedResultAccumulator",
                field: "input_line_range",
                ..
            }
        ));
    }

    #[test]
    fn empty_batches_keep_output_empty_and_allow_following_ranges() {
        let mut accumulator = IndexedResultAccumulator::new(3);

        accumulator
            .try_merge(batch(
                (None, None),
                Vec::new(),
                stats(0, 0, None, 0, 0, None),
            ))
            .expect("empty batch should merge");
        accumulator
            .try_merge(batch(
                (Some(1), Some(1)),
                vec![event("INFO worker ready", 1)],
                stats(1, 0, None, 0, 0, None),
            ))
            .expect("non-empty batch after empty should merge");

        let output = accumulator.finish_with_duration(Duration::ZERO);

        assert_eq!(output.parse_output.cluster_count, 3);
        assert_eq!(output.parse_output.line_results, Vec::new());
        assert_eq!(
            output.parse_output.events,
            vec![event("INFO worker ready", 1)]
        );
        assert_eq!(output.stats, stats(1, 0, None, 0, 0, None));
    }
}

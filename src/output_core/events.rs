use std::collections::BTreeMap;

use crate::template::event_id_for_template;
use crate::types::EventRecord;

/// Aggregate event records keyed by `event_template`, preserving first-seen order.
pub(crate) struct EventAccumulator {
    events: Vec<EventRecord>,
    event_indexes: BTreeMap<String, usize>,
}

impl EventAccumulator {
    pub(crate) fn new() -> Self {
        Self {
            events: Vec::new(),
            event_indexes: BTreeMap::new(),
        }
    }

    /// Derive `event_id` from `template` (with `"0"` sentinel short-circuit),
    /// then aggregate. Used by PIPLUP build_output and assignment output.
    pub(crate) fn record_template(&mut self, template: &str, occurrences: usize) {
        let event_id = if template == "0" {
            "0".to_string()
        } else {
            event_id_for_template(template)
        };
        self.accumulate(template, event_id, occurrences);
    }

    /// Trust the caller-supplied `event_id`, then aggregate. Used by indexed parse
    /// where event_id already comes from the index or training assignment.
    pub(crate) fn record_event(&mut self, event_id: &str, template: &str, occurrences: usize) {
        self.accumulate(template, event_id.to_string(), occurrences);
    }

    fn accumulate(&mut self, template: &str, event_id: String, occurrences: usize) {
        if let Some(&index) = self.event_indexes.get(template) {
            self.events[index].occurrences += occurrences;
            return;
        }
        let index = self.events.len();
        self.events.push(EventRecord {
            event_id,
            event_template: template.to_string(),
            occurrences,
        });
        self.event_indexes.insert(template.to_string(), index);
    }

    pub(crate) fn into_events(self) -> Vec<EventRecord> {
        self.events
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn record_template_accumulates_occurrences_for_same_template() {
        // Repeated templates accumulate occurrences into a single record;
        // distinct templates produce separate records.
        let mut acc = EventAccumulator::new();
        acc.record_template("worker started", 1);
        acc.record_template("worker started", 2);
        acc.record_template("node joined", 1);

        let events = acc.into_events();
        assert_eq!(events.len(), 2);
        assert_eq!(events[0].event_template, "worker started");
        assert_eq!(events[0].occurrences, 3);
        assert_eq!(events[1].event_template, "node joined");
        assert_eq!(events[1].occurrences, 1);
    }

    #[test]
    fn record_template_preserves_first_seen_order() {
        // Event order follows first encounter, not lexical/template order;
        // re-recording a template does not reorder the vector.
        let mut acc = EventAccumulator::new();
        acc.record_template("beta", 1);
        acc.record_template("alpha", 1);
        acc.record_template("beta", 1);

        let events = acc.into_events();
        assert_eq!(
            events
                .iter()
                .map(|e| e.event_template.as_str())
                .collect::<Vec<_>>(),
            vec!["beta", "alpha"]
        );
        assert_eq!(events[0].occurrences, 2);
        assert_eq!(events[1].occurrences, 1);
    }

    #[test]
    fn record_template_zero_sentinel_keeps_zero_event_id() {
        // The "0" sentinel must keep event_id "0" verbatim, NOT the derived MD5
        // prefix (event_id_for_template("0") == "cfcd2084").
        let mut acc = EventAccumulator::new();
        acc.record_template("0", 5);

        let events = acc.into_events();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].event_id, "0");
        assert_eq!(events[0].event_template, "0");
        assert_eq!(events[0].occurrences, 5);
        assert_ne!(events[0].event_id, event_id_for_template("0"));
    }

    #[test]
    fn record_event_keeps_explicit_event_id() {
        // record_event trusts the caller-supplied event_id (indexed-parse path)
        // and does not derive it from the template.
        let mut acc = EventAccumulator::new();
        acc.record_event("deadbeef", "worker started", 1);

        let events = acc.into_events();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].event_id, "deadbeef");
        assert_eq!(events[0].event_template, "worker started");
        assert_ne!(events[0].event_id, event_id_for_template("worker started"));
    }

    #[test]
    fn record_event_aggregates_by_template_not_event_id() {
        // Aggregation is keyed by event_template: a second call with the same
        // template but a different event_id accumulates occurrences and keeps
        // the first-seen event_id.
        let mut acc = EventAccumulator::new();
        acc.record_event("id1", "shared template", 1);
        acc.record_event("id2", "shared template", 1);

        let events = acc.into_events();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].event_id, "id1");
        assert_eq!(events[0].occurrences, 2);
    }
}

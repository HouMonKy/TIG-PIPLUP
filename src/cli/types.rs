use std::{collections::BTreeMap, path::PathBuf, time::Duration};

use crate::EventRecord;

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) enum OutputSink {
    Stdout,
    Path(PathBuf),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) enum OutputPlan {
    Directory(PathBuf),
    Separate {
        structured: OutputSink,
        templates: OutputSink,
    },
    MirroredDirectory {
        input_root: PathBuf,
        output_root: Option<PathBuf>,
        suffix: String,
    },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) enum ResolvedOutputPlan {
    Directory(PathBuf),
    Separate {
        structured: OutputSink,
        templates: OutputSink,
    },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) enum InputPlan {
    Stdin,
    File(PathBuf),
    Directory { root: PathBuf, files: Vec<PathBuf> },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct DirectoryRunStats {
    pub(super) file_count: usize,
    pub(super) leaf_dir_count: usize,
    pub(super) line_count: usize,
    pub(super) template_count: usize,
    pub(super) duration: Duration,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct DirectoryIndexBuildStats {
    pub(super) leaf_dir_count: usize,
    pub(super) file_count: usize,
    pub(super) line_count: usize,
    pub(super) template_count: usize,
    pub(super) duration: Duration,
    /// Total lines restored from cache across all leaf directories.
    pub(super) resume_line_count: usize,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct DirectoryOutputPaths {
    pub(super) structured_dir: PathBuf,
    pub(super) templates_path: PathBuf,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct LeafTemplateAccumulator {
    pub(super) events: Vec<EventRecord>,
    pub(super) template_indexes: BTreeMap<String, usize>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct BuildIndexResult {
    pub(super) line_count: usize,
    pub(super) template_count: usize,
    pub(super) duration: Duration,
    /// Number of lines restored from cache during resume.  Zero for cold
    /// starts; set by `train_for_entries` when a valid cache was loaded
    /// (either algorithm, via the `CliTrainingRunner` holder).
    pub(super) resume_line_count: usize,
}

pub(super) fn merge_leaf_templates(
    aggregate: &mut LeafTemplateAccumulator,
    events: &[EventRecord],
) {
    for event in events {
        if let Some(&index) = aggregate.template_indexes.get(&event.event_template) {
            aggregate.events[index].occurrences += event.occurrences;
            continue;
        }

        let index = aggregate.events.len();
        aggregate.events.push(event.clone());
        aggregate
            .template_indexes
            .insert(event.event_template.clone(), index);
    }
}

impl LeafTemplateAccumulator {
    pub(super) fn new() -> Self {
        Self {
            events: Vec::new(),
            template_indexes: BTreeMap::new(),
        }
    }
}

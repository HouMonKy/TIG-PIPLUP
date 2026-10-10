//! Template merging across clusters.
//!
//! After a cluster's template list changes, `merge_templates` compares the new
//! variants against sibling clusters and replaces templates when one template
//! is a strict generalization of another and their Jaccard word similarity is
//! high enough.

use regex::Regex;
use rustc_hash::{FxHashMap, FxHashSet};

use crate::{
    engine::matcher::{jaccard_similarity, template_matches},
    model::{ClusterId, ConstantKey, LogCluster, ParseState},
};

const MERGE_JACCARD_THRESHOLD: f64 = 0.6;

/// Merge a cluster's updated template variants with its siblings in `constant` bucket.
///
/// `old_template_list` is the cluster's previous template list; only new
/// variants are considered for merging.
pub fn merge_templates(
    state: &mut ParseState,
    cluster_id: ClusterId,
    constant: &ConstantKey,
    old_template_list: &[String],
) {
    let Some(bucket) = state.bucket(constant) else {
        return;
    };

    let sibling_ids = bucket.cluster_ids.clone();
    let old_templates = old_template_list
        .iter()
        .map(|template| template.as_str())
        .collect::<FxHashSet<_>>();
    let updated_template_indices = state
        .cluster(cluster_id)
        .template_list
        .iter()
        .enumerate()
        .filter_map(|(index, template)| {
            (!old_templates.contains(template.as_str())).then_some(index)
        })
        .collect::<Vec<_>>();
    let mut regex_cache = FxHashMap::<String, Regex>::default();

    for template_index in updated_template_indices {
        let Some(template) = state
            .cluster(cluster_id)
            .template_list
            .get(template_index)
            .cloned()
        else {
            continue;
        };

        for sibling_id in &sibling_ids {
            if cluster_id == *sibling_id {
                merge_with_self(
                    state,
                    cluster_id,
                    template_index,
                    &template,
                    &mut regex_cache,
                );
            } else {
                merge_with_sibling(
                    state,
                    cluster_id,
                    template_index,
                    &template,
                    *sibling_id,
                    &mut regex_cache,
                );
            }
        }
    }
}

enum MergeAction {
    ReplaceClusterTemplate(String),
    ReplaceSiblingTemplate(String),
    None,
}

fn evaluate_merge(
    template: &str,
    template_word_count: usize,
    sibling_template: &str,
    sibling_word_count: usize,
    regex_cache: &mut FxHashMap<String, Regex>,
) -> MergeAction {
    if jaccard_similarity(template, sibling_template) < MERGE_JACCARD_THRESHOLD {
        return MergeAction::None;
    }

    if template_word_count >= sibling_word_count
        && template_matches(sibling_template, template, regex_cache)
    {
        return MergeAction::ReplaceClusterTemplate(sibling_template.to_string());
    }

    if template_word_count <= sibling_word_count
        && template_matches(template, sibling_template, regex_cache)
    {
        return MergeAction::ReplaceSiblingTemplate(template.to_string());
    }

    MergeAction::None
}

fn merge_with_self(
    state: &mut ParseState,
    cluster_id: ClusterId,
    template_index: usize,
    template: &str,
    regex_cache: &mut FxHashMap<String, Regex>,
) {
    let sibling_template_count = state.cluster(cluster_id).template_list.len();
    let template_word_count = word_count(template);

    for sibling_index in 0..sibling_template_count {
        if sibling_index == template_index {
            continue;
        }
        let sibling_template = state.cluster(cluster_id).template_list[sibling_index].clone();
        let sibling_word_count = word_count(&sibling_template);
        let action = evaluate_merge(
            template,
            template_word_count,
            &sibling_template,
            sibling_word_count,
            regex_cache,
        );
        let cluster = state.cluster_mut(cluster_id);
        match action {
            MergeAction::ReplaceClusterTemplate(replacement) => {
                cluster.template_list[template_index] = replacement;
                break;
            }
            MergeAction::ReplaceSiblingTemplate(replacement) => {
                cluster.template_list[sibling_index] = replacement;
            }
            MergeAction::None => {}
        }
    }
}

fn merge_with_sibling(
    state: &mut ParseState,
    cluster_id: ClusterId,
    template_index: usize,
    template: &str,
    sibling_id: ClusterId,
    regex_cache: &mut FxHashMap<String, Regex>,
) {
    let sibling_template_count = state.cluster(sibling_id).template_list.len();
    let template_word_count = word_count(template);

    for sibling_index in 0..sibling_template_count {
        let sibling_template = state.cluster(sibling_id).template_list[sibling_index].clone();
        let sibling_word_count = word_count(&sibling_template);
        let action = evaluate_merge(
            template,
            template_word_count,
            &sibling_template,
            sibling_word_count,
            regex_cache,
        );
        let (cluster, sibling) = cluster_pair_mut(&mut state.clusters, cluster_id, sibling_id);
        match action {
            MergeAction::ReplaceClusterTemplate(replacement) => {
                cluster.template_list[template_index] = replacement;
                break;
            }
            MergeAction::ReplaceSiblingTemplate(replacement) => {
                sibling.template_list[sibling_index] = replacement;
            }
            MergeAction::None => {}
        }
    }
}

fn cluster_pair_mut(
    clusters: &mut [LogCluster],
    left_id: ClusterId,
    right_id: ClusterId,
) -> (&mut LogCluster, &mut LogCluster) {
    if left_id < right_id {
        let (left, right) = clusters.split_at_mut(right_id);
        (&mut left[left_id], &mut right[0])
    } else {
        let (left, right) = clusters.split_at_mut(left_id);
        (&mut right[0], &mut left[right_id])
    }
}

fn word_count(template: &str) -> usize {
    template.split_whitespace().count()
}

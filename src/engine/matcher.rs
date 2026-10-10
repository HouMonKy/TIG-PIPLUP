//! Sequence similarity, template matching, and prefix-tree search.
//!
//! This module implements the low-level matching primitives used by the piplup
//! online learner and by output construction.

use std::{
    collections::VecDeque,
    sync::{Mutex, OnceLock},
};

use regex::{Regex, RegexBuilder};
use rustc_hash::{FxHashMap, FxHashSet};

use crate::{
    config::SimThresholds,
    model::{ClusterId, ConstantKey, ParseState, Token},
    placeholder::{PlaceholderKind, typed_slot_matches},
};

/// Compute token-sequence distance and parameter count.
///
/// Returns a tuple `(similarity, num_of_parameters)`. Sequences must have the
/// same length; this is asserted in debug builds.
///
/// TIG-PIPLUP `3e51f3b` uses binary, case-sensitive scoring: pure wildcards
/// count as parameters without scoring; equal non-wildcard tokens score 1;
/// all other positions score 0. Every position remains in the denominator.
///
/// ADR-0033 §4 typed lazy matching: when `mask[i]` is `Some(kind)` (the
/// template slot has learned a placeholder kind), the slot counts as a
/// similarity match iff `observed_kinds[i]` is the same kind — the observed
/// token classified identically. A kind mismatch lowers similarity (soft)
/// rather than hard-rejecting, mirroring `drain3::state::typed_similarity`.
/// Both slices default to all-`None` (empty in typed-off mode), which is
/// byte-identical to the legacy behavior.
///
/// # Example
///
/// ```rust
/// use log_parser::{seq_dist, Token};
///
/// let seq1 = vec![Token::literal("worker"), Token::wildcard("", "")];
/// let seq2 = vec![Token::literal("worker"), Token::literal("42")];
/// let (sim, params) = seq_dist(&seq1, &seq2, &[], &[]);
/// // TIG-PIPLUP 3e51f3b keeps the wildcard in the denominator.
/// assert!((sim - 0.5).abs() < 1e-9);
/// assert_eq!(params, 1);
/// ```
pub fn seq_dist(
    seq1: &[Token],
    seq2: &[Token],
    mask: &[Option<PlaceholderKind>],
    observed_kinds: &[Option<PlaceholderKind>],
) -> (f64, usize) {
    assert_eq!(seq1.len(), seq2.len());
    if seq1.is_empty() {
        return (0.0, 0);
    }

    let mut sim_tokens = 0usize;
    let mut num_of_parameters = 0usize;

    for (i, (token1, token2)) in seq1.iter().zip(seq2).enumerate() {
        if token1.is_pure_wildcard() {
            num_of_parameters += 1;
            if typed_slot_matches(mask, observed_kinds, i) {
                sim_tokens += 1;
            }
            continue;
        }
        if token1 == token2 {
            sim_tokens += 1;
        }
    }

    (sim_tokens as f64 / seq1.len() as f64, num_of_parameters)
}

/// Generalize two equal-length token sequences into a template.
///
/// Equal positions keep the right-hand token; differing positions become a
/// pure wildcard. This is the *scoring-surface* generalization
/// (`log_template`): it must stay attractor-neutral — structure-preserving
/// wildcards here would score 0.9/0.5 in later `seq_dist` comparisons and
/// funnel dissimilar lines into high-context clusters (observed as the
/// Hadoop transition-collapse regression). Structure evidence is preserved
/// on the *output* surface instead: `LogCluster::update_sequence` merges
/// paths with structure awareness.
///
/// # Example
///
/// ```rust
/// use log_parser::{get_template, Token};
///
/// let seq1 = vec![Token::literal("worker"), Token::literal("42")];
/// let seq2 = vec![Token::literal("worker"), Token::literal("7")];
/// let template = get_template(&seq1, &seq2);
/// assert_eq!(template[1], Token::wildcard("", ""));
/// ```
pub fn get_template(seq1: &[Token], seq2: &[Token]) -> Vec<Token> {
    assert_eq!(seq1.len(), seq2.len());
    seq1.iter()
        .zip(seq2)
        .map(|(left, right)| {
            if left == right {
                right.clone()
            } else {
                Token::wildcard("", "")
            }
        })
        .collect()
}

/// Fast matching against a candidate list of clusters.
///
/// Returns the best matching cluster id, the generalized template, and whether
/// the match was exact.
///
/// Candidates are scored in list order with [`seq_dist`]; ties prefer the
/// candidate containing more pure wildcard parameters, matching Python.
pub fn fast_match(
    state: &ParseState,
    candidate_ids: &[ClusterId],
    seq: &[Token],
    observed_kinds: &[Option<PlaceholderKind>],
    thresholds: &SimThresholds,
) -> (Option<ClusterId>, Vec<Token>, bool) {
    let mut max_sim = -1.0_f64;
    let mut max_num_of_para = -1_isize;
    let mut matched_cluster = None;

    for &cluster_id in candidate_ids {
        let cluster = state.cluster(cluster_id);
        if cluster.log_template == seq {
            return (Some(cluster_id), cluster.log_template.clone(), true);
        }
        let (cur_sim, cur_num_of_para) = seq_dist(
            &cluster.log_template,
            seq,
            &cluster.placeholder_mask,
            observed_kinds,
        );

        if cur_sim > max_sim || (cur_sim == max_sim && (cur_num_of_para as isize) > max_num_of_para)
        {
            max_sim = cur_sim;
            max_num_of_para = cur_num_of_para as isize;
            matched_cluster = Some(cluster_id);
        }
    }

    if let Some(cluster_id) = matched_cluster
        && max_sim >= thresholds.get(seq.len())
    {
        return (
            Some(cluster_id),
            get_template(seq, &state.cluster(cluster_id).log_template),
            false,
        );
    }

    (None, seq.to_vec(), false)
}

/// Search the parse state for the best cluster matching `seq`.
///
/// Selects a constant bucket by token content, then runs [`fast_match`] against
/// length-filtered candidates. Identical repeated lines short-circuit through
/// the exact-template index before bucket routing (verified on hit, so stale
/// entries fall through to the normal search).
pub fn tree_search(
    state: &mut ParseState,
    seq: &[Token],
    observed_kinds: &[Option<PlaceholderKind>],
    thresholds: &SimThresholds,
) -> (Option<ClusterId>, ConstantKey, Vec<Token>, bool) {
    if let Some(cluster_id) = state.exact_lookup(seq) {
        let constant = state.select_constant_for_tokens(seq);
        let template = state.cluster(cluster_id).log_template.clone();
        return (Some(cluster_id), constant, template, true);
    }
    let constant = state.select_constant_for_tokens(seq);
    let Some(bucket) = state.bucket(&constant) else {
        return (None, constant, seq.to_vec(), false);
    };
    let Some(candidate_ids) = bucket.by_len.get(&seq.len()) else {
        return (None, constant, seq.to_vec(), false);
    };

    let (cluster_id, new_template, exact) =
        fast_match(state, candidate_ids, seq, observed_kinds, thresholds);
    (cluster_id, constant, new_template, exact)
}

/// Check whether `template1` (which may contain `<*>` wildcards) matches
/// `template2` case-sensitively.
///
/// A mutable regex cache may be supplied to amortize compilation cost.
///
/// # Example
///
/// ```rust
/// use log_parser::template_matches;
/// use rustc_hash::FxHashMap;
///
/// let mut cache = FxHashMap::default();
/// assert!(template_matches("worker <*> started", "worker 42 started", &mut cache));
/// assert!(!template_matches("worker <*> started", "server 42 started", &mut cache));
/// ```
pub fn template_matches(
    template1: &str,
    template2: &str,
    _cache: &mut FxHashMap<String, Regex>,
) -> bool {
    COMPILED_TEMPLATE_CACHE
        .get_or_init(|| Mutex::new(CompiledTemplateLru::default()))
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .is_match(template1, template2)
}

const COMPILED_TEMPLATE_CACHE_CAPACITY: usize = 4096;

#[derive(Default)]
struct CompiledTemplateLru {
    entries: FxHashMap<String, Regex>,
    order: VecDeque<String>,
}

impl CompiledTemplateLru {
    fn is_match(&mut self, template: &str, content: &str) -> bool {
        if let Some(regex) = self.entries.get(template).cloned() {
            self.touch(template);
            return regex.is_match(content);
        }

        let pattern = template
            .split("<*>")
            .map(regex::escape)
            .collect::<Vec<_>>()
            .join(".*?");
        let Ok(regex) = RegexBuilder::new(&format!(r"\A(?:{pattern})\z"))
            .dot_matches_new_line(true)
            .build()
        else {
            return false;
        };
        if self.entries.len() == COMPILED_TEMPLATE_CACHE_CAPACITY
            && let Some(oldest) = self.order.pop_front()
        {
            self.entries.remove(&oldest);
        }
        self.order.push_back(template.to_string());
        self.entries.insert(template.to_string(), regex.clone());
        regex.is_match(content)
    }

    fn touch(&mut self, template: &str) {
        if let Some(position) = self.order.iter().position(|key| key == template) {
            self.order.remove(position);
        }
        self.order.push_back(template.to_string());
    }
}

static COMPILED_TEMPLATE_CACHE: OnceLock<Mutex<CompiledTemplateLru>> = OnceLock::new();

/// Jaccard similarity of the alphanumeric word sets in two templates.
///
/// Word sets are memoized in a bounded thread-local cache: template merging
/// recomputes the same template strings' sets many times per merge pass.
///
/// # Example
///
/// ```rust
/// use log_parser::jaccard_similarity;
///
/// let sim = jaccard_similarity("worker started done", "worker stopped done");
/// assert!((sim - 0.5).abs() < 1e-9);
/// ```
pub fn jaccard_similarity(template1: &str, template2: &str) -> f64 {
    WORD_SET_CACHE.with(|cache| {
        let mut cache = cache.borrow_mut();
        // Evict before insertion and never evict the two keys about to be
        // used. The previous FIFO evicted the oldest entry on insert —
        // which could be `template1`, freshly *accessed* but long-since
        // inserted, making the `sets[template1]` lookups below panic.
        let missing =
            !cache.contains_key(template1) as usize + !cache.contains_key(template2) as usize;
        while missing > 0 && cache.len() + missing > WORD_SET_CACHE_MAX_SIZE {
            let Some(victim) = cache
                .keys()
                .find(|key| key.as_str() != template1 && key.as_str() != template2)
                .cloned()
            else {
                break;
            };
            cache.remove(&victim);
        }
        cache
            .entry(template1.to_string())
            .or_insert_with(|| word_set(template1));
        cache
            .entry(template2.to_string())
            .or_insert_with(|| word_set(template2));
        let set1 = &cache[template1];
        let set2 = &cache[template2];

        let union_len = set1.union(set2).count();
        if union_len == 0 {
            return 0.0;
        }
        set1.intersection(set2).count() as f64 / union_len as f64
    })
}

const WORD_SET_CACHE_MAX_SIZE: usize = 10_000;

thread_local! {
    static WORD_SET_CACHE: std::cell::RefCell<FxHashMap<String, FxHashSet<String>>> =
        std::cell::RefCell::new(FxHashMap::default());
}

fn word_set(template: &str) -> FxHashSet<String> {
    template
        .split_whitespace()
        .filter(|item| item.chars().any(char::is_alphanumeric))
        .map(|item| item.to_string())
        .collect()
}

#[cfg(test)]
mod panic_regression_tests {
    use super::*;

    #[test]
    fn jaccard_eviction_never_removes_the_keys_in_use() {
        // Regression: with the cache exactly full, the FIFO could evict
        // `template1` (accessed but long-since inserted) when
        // `template2` was inserted, panicking the lookups below.
        let old = "alpha beta gamma";
        let _ = jaccard_similarity(old, "seed words one");
        for i in 0..WORD_SET_CACHE_MAX_SIZE - 1 {
            let filler = format!("filler-{i}-entry");
            let _ = jaccard_similarity(old, &filler);
        }
        let sim = jaccard_similarity(old, "brand-new-template-xyz");
        assert!((0.0..=1.0).contains(&sim));
    }

    #[test]
    fn wildcard_suffix_check_survives_multibyte_content() {
        // Regression: suffix_start = len - last.len() landed inside a
        // multibyte character and the byte-index slice panicked.
        let mut cache = FxHashMap::default();
        assert!(!template_matches("X<*>ab", "X中", &mut cache));
        assert!(template_matches("X<*>中", "X中", &mut cache));
        // Anchored, boundary-aligned suffix still matches.
        assert!(template_matches("X<*>b", "X中b", &mut cache));
    }

    #[test]
    fn compiled_template_cache_is_lru_bounded_to_python_capacity() {
        let mut cache = CompiledTemplateLru::default();
        assert!(cache.is_match("oldest<*>", "oldest-value"));
        for index in 0..COMPILED_TEMPLATE_CACHE_CAPACITY - 1 {
            let template = format!("entry-{index}-<*>");
            let content = format!("entry-{index}-value");
            assert!(cache.is_match(&template, &content));
        }
        // Refresh the oldest entry, then force one eviction.
        assert!(cache.is_match("oldest<*>", "oldest-new"));
        assert!(cache.is_match("overflow<*>", "overflow-value"));
        assert_eq!(cache.entries.len(), COMPILED_TEMPLATE_CACHE_CAPACITY);
        assert!(cache.entries.contains_key("oldest<*>"));
        assert!(!cache.entries.contains_key("entry-0-<*>"));
    }
}

#[cfg(test)]
mod denominator_tests {
    use super::*;
    use crate::Token;

    #[test]
    fn seq_dist_keeps_pure_wildcards_in_the_denominator() {
        let template = vec![Token::literal("worker"), Token::wildcard("", "")];
        let seq = vec![Token::literal("worker"), Token::literal("42")];
        let (sim, params) = seq_dist(&template, &seq, &[], &[]);
        assert!((sim - 0.5).abs() < 1e-9, "got {sim}");
        assert_eq!(params, 1);
    }

    #[test]
    fn seq_dist_all_pure_wildcard_returns_zero() {
        let template = vec![Token::wildcard("", ""), Token::wildcard("", "")];
        let seq = vec![Token::literal("a"), Token::literal("b")];
        let (sim, params) = seq_dist(&template, &seq, &[], &[]);
        assert!(sim.abs() < 1e-9);
        assert_eq!(params, 2);
    }

    #[test]
    fn seq_dist_literal_mismatches_use_full_denominator() {
        let template = vec![
            Token::literal("a"),
            Token::wildcard("", ""),
            Token::literal("x"),
        ];
        let seq = vec![
            Token::literal("a"),
            Token::literal("1"),
            Token::literal("y"),
        ];
        let (sim, _) = seq_dist(&template, &seq, &[], &[]);
        assert!((sim - 1.0 / 3.0).abs() < 1e-9, "got {sim}");
    }
}

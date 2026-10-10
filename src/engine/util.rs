//! Small internal helpers shared by the parser and session engines.

use crate::config::{PiplupConfig, SimThresholds};
use crate::engine::matcher::tree_search;
use crate::engine::merge::merge_templates;
use crate::engine::session::PREPROCESS_PLAN_ESTIMATE_LIMIT;
use crate::model::{ConstantKey, LogCluster, ParseState, Token};
use crate::parse_core::normalize_separators;
use crate::placeholder::PlaceholderKind;
use crate::preprocessor::{PreprocessPlan, Preprocessor, TYPED_OWNED_RULE_IDS};
use crate::types::ParsedLine;

/// Return the first token that contains alphanumeric characters, rendered as a string.
pub(crate) fn first_promotable_constant(tokens: &[Token]) -> Option<String> {
    tokens
        .iter()
        .find(|token| token.has_alnum())
        .map(Token::to_rendered_string)
}

/// Tokenize a parsed line, applying preprocessing and updating the plan when
/// still in the estimation window.
///
/// Returns the token sequence plus a per-position `PlaceholderKind` classification
/// (non-empty only when `config.typed_placeholders` is enabled). In typed mode
/// the eager regex rules whose shapes the classifier owns are skipped
/// (`estimate_line_excluding`) and classified tokens become pure wildcards;
/// their kind is recorded in the returned kinds for placeholder-mask maintenance.
///
/// The third element is `true` when the line was excluded from clustering by a
/// configured length limit (`max_line_bytes`, or `max_token_bytes` filtering
/// every token of the line). An empty token sequence that is *not*
/// length-filtered is natural empty content and must retain the legacy
/// empty-template clustering behavior.
pub(crate) fn tokenize_line(
    config: &PiplupConfig,
    preprocessor: &Preprocessor,
    preprocess_plan: &mut PreprocessPlan,
    index: usize,
    line: &ParsedLine,
    plan_frozen: bool,
    plan_restored_from_cache: bool,
) -> (Vec<Token>, Vec<Option<PlaceholderKind>>, bool) {
    let processed = if !config.preprocess {
        line.content.clone()
    } else if plan_frozen || plan_restored_from_cache || index >= PREPROCESS_PLAN_ESTIMATE_LIMIT {
        preprocessor.apply_with_plan(&line.content, preprocess_plan)
    } else {
        // ADR-0033 coexistence: in typed mode, skip the eager regex rules whose
        // shapes the typed classifier owns — neither run them (perf) nor let
        // them erase shapes before classification (mask correctness).
        let estimated = if config.typed_placeholders {
            preprocessor.estimate_line_excluding(&line.content, TYPED_OWNED_RULE_IDS)
        } else {
            preprocessor.estimate_line(&line.content)
        };
        for rule_id in estimated.matched_rule_ids {
            preprocess_plan.push_unique_rule_id(rule_id);
        }
        estimated.output
    };

    // ADR-0046: separator normalization runs after preprocessing (catalog
    // rules depend on the original punctuation adjacency) and before
    // `split_whitespace`; the length limits below apply to the normalized
    // content.
    let processed = normalize_separators(&processed, &config.separators);

    if let Some(max) = config.max_line_bytes
        && processed.len() > max
    {
        return (Vec::new(), Vec::new(), true);
    }

    let mut filtered_any = false;
    let kept_tokens: Vec<&str> = processed
        .split_whitespace()
        .filter(|token| {
            let keep = config.max_token_bytes.is_none_or(|max| token.len() <= max);
            if !keep {
                filtered_any = true;
            }
            keep
        })
        .collect();
    let length_filtered = filtered_any && kept_tokens.is_empty();

    if config.typed_placeholders {
        // ADR-0033: classify each kept token once with the shared token adapter.
        // Whole-token placeholders become pure wildcards; embedded spans keep
        // their head/tail context (`value=123` -> `value=<*>`).
        let mut tokens = Vec::new();
        let mut line_kinds = Vec::new();
        for token in kept_tokens {
            let (template_token, kind) = Token::from_typed_observed(token);
            tokens.push(template_token);
            line_kinds.push(kind);
        }
        (tokens, line_kinds, length_filtered)
    } else {
        let mut tokens = Vec::new();
        for token in kept_tokens {
            let template_token = if token == "<*>" {
                Token::wildcard("", "")
            } else {
                Token::literal(token)
            };
            tokens.push(template_token);
        }
        (tokens, Vec::new(), length_filtered)
    }
}

/// Process one tokenized line through the PIPLUP match/update/promotion/merge
/// pipeline. Returns the cluster id that the line was assigned to.
///
/// `line_kinds` carries the per-token `PlaceholderKind` classification (non-empty
/// only in typed mode) used to maintain the cluster's placeholder mask.
pub(crate) fn process_line(
    state: &mut ParseState,
    tokens: &[Token],
    line_kinds: &[Option<PlaceholderKind>],
    thresholds: &SimThresholds,
    config: &PiplupConfig,
    line_number: usize,
) -> usize {
    let (matched_cluster, first_constant, new_template, _exact_match) =
        tree_search(state, tokens, line_kinds, thresholds);

    match matched_cluster {
        Some(cluster_id) => {
            let cluster = state.cluster(cluster_id);
            let template_changed = cluster.log_template != new_template;
            let needs_promotion = (template_changed || cluster.hit_time + 1 == config.hit_limit)
                && matches!(first_constant, ConstantKey::None);

            if cluster.path_known(tokens) && !needs_promotion {
                let cluster = state.cluster_mut(cluster_id);
                cluster.record_covered_match(line_number, new_template);
                if config.typed_placeholders {
                    cluster.maintain_placeholder_mask(line_kinds);
                }
            } else {
                let (old_template_list, promoted_constant) = {
                    let cluster = state.cluster_mut(cluster_id);
                    let old_template_list =
                        cluster.record_match(line_number, new_template.clone(), tokens.to_vec());
                    if config.typed_placeholders {
                        cluster.maintain_placeholder_mask(line_kinds);
                    }
                    let promoted_constant = if (template_changed
                        || cluster.hit_time == config.hit_limit)
                        && matches!(first_constant, ConstantKey::None)
                    {
                        first_promotable_constant(&new_template)
                    } else {
                        None
                    };
                    (old_template_list, promoted_constant)
                };

                let merge_constant = promoted_constant
                    .as_deref()
                    .map(|constant| state.promote_none_bucket(constant))
                    .unwrap_or_else(|| first_constant.clone());

                if config.merge
                    && let Some(old_template_list) = old_template_list
                {
                    merge_templates(
                        state,
                        cluster_id,
                        &merge_constant,
                        old_template_list.as_slice(),
                    );
                }
            }
            cluster_id
        }
        None => {
            let mut cluster = LogCluster::new(tokens.to_vec(), line_number, config.br_thresh);
            if config.typed_placeholders {
                cluster.set_placeholder_mask_from_kinds(line_kinds);
            }
            let (cluster_id, constant) = state.add_cluster(cluster);
            if config.merge {
                merge_templates(state, cluster_id, &constant, &[]);
            }
            cluster_id
        }
    }
}

/// CRC32 used by Python's `zlib.crc32` cache keys.
fn crc32(bytes: &[u8]) -> u32 {
    let mut crc = !0_u32;
    for &byte in bytes {
        crc ^= u32::from(byte);
        for _ in 0..8 {
            crc = (crc >> 1) ^ (0xedb8_8320_u32 & (0_u32.wrapping_sub(crc & 1)));
        }
    }
    !crc
}

/// CRC32 of content after replacing every Unicode digit run with one `0`.
fn light_crc(content: &str) -> u32 {
    use std::sync::OnceLock;
    static DIGIT_RE: OnceLock<regex::Regex> = OnceLock::new();
    let regex = DIGIT_RE.get_or_init(|| regex::Regex::new(r"\d+").expect("valid digit regex"));
    crc32(regex.replace_all(content, "0").as_bytes())
}

/// TIG-PIPLUP level-1 cache lookup, before preprocessing.
pub(crate) fn try_light_cache(
    state: &mut ParseState,
    config: &PiplupConfig,
    content: &str,
    line_number: usize,
) -> Option<usize> {
    if !config.use_template_cache || config.typed_placeholders || state.light_cache.is_empty() {
        return None;
    }
    let candidates = state.light_cache.get(&light_crc(content))?.clone();
    let raw_content = content.trim();
    let cluster_id = candidates.into_iter().find(|&cluster_id| {
        let templates = &state.cluster(cluster_id).template_list;
        if templates.len() == 1 {
            templates[0]
                .split("<*>")
                .filter(|part| !part.is_empty())
                .all(|part| raw_content.contains(part))
        } else {
            templates
                .iter()
                .any(|template| output_template_matches(template, raw_content))
        }
    })?;
    let cluster = state.cluster_mut(cluster_id);
    cluster.hit_time += 1;
    cluster.log_ids.push(line_number);
    Some(cluster_id)
}

/// TIG-PIPLUP level-2 cache lookup after preprocessing. Python deliberately
/// trusts this CRC32 mapping and backfills the level-1 candidate list.
pub(crate) fn try_template_cache(
    state: &mut ParseState,
    config: &PiplupConfig,
    content: &str,
    tokens: &[Token],
    line_number: usize,
) -> Option<usize> {
    if !config.use_template_cache || config.typed_placeholders {
        return None;
    }
    let rendered = tokens
        .iter()
        .map(Token::to_rendered_string)
        .collect::<Vec<_>>()
        .join(" ");
    let cluster_id = *state.template_cache.get(&crc32(rendered.as_bytes()))?;
    let bucket = state.light_cache.entry(light_crc(content)).or_default();
    if !bucket.contains(&cluster_id) {
        bucket.push(cluster_id);
    }
    let cluster = state.cluster_mut(cluster_id);
    cluster.hit_time += 1;
    cluster.log_ids.push(line_number);
    Some(cluster_id)
}

/// Insert into both Python-compatible cache levels after the full pipeline.
pub(crate) fn insert_template_caches(
    state: &mut ParseState,
    config: &PiplupConfig,
    content: &str,
    tokens: &[Token],
    cluster_id: usize,
) {
    if !config.use_template_cache || config.typed_placeholders {
        return;
    }
    let rendered = tokens
        .iter()
        .map(Token::to_rendered_string)
        .collect::<Vec<_>>()
        .join(" ");
    state
        .template_cache
        .insert(crc32(rendered.as_bytes()), cluster_id);
    let bucket = state.light_cache.entry(light_crc(content)).or_default();
    if !bucket.contains(&cluster_id) {
        bucket.push(cluster_id);
    }
}

/// Python `_compile_regex_patterns` + `_regex_match_log` semantics.
fn output_template_matches(template: &str, content: &str) -> bool {
    let pattern = template
        .split(' ')
        .map(regex::escape)
        .collect::<Vec<_>>()
        .join(r"\s*")
        .replace(&regex::escape("<*>"), ".*?");
    regex::RegexBuilder::new(&format!("^(?:{pattern})$"))
        .case_insensitive(true)
        .dot_matches_new_line(true)
        .build()
        .is_ok_and(|regex| regex.is_match(content))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::preprocessor::Preprocessor;
    use crate::types::ParsedLine;

    fn tokenize_content(config: &PiplupConfig, content: &str) -> Vec<Token> {
        let preprocessor = Preprocessor::default();
        let mut plan = PreprocessPlan::default();
        let line = ParsedLine {
            line_number: 1,
            raw: content.to_string(),
            fields: Vec::new(),
            content: content.to_string(),
        };
        let (tokens, _kinds, _length_filtered) =
            tokenize_line(config, &preprocessor, &mut plan, 0, &line, false, false);
        tokens
    }

    #[test]
    fn crc32_matches_zlib_reference_vector() {
        assert_eq!(crc32(b"123456789"), 0xcbf4_3926);
    }

    #[test]
    fn light_crc_replaces_each_digit_run_with_one_zero() {
        assert_eq!(light_crc("node-123-port-45"), crc32(b"node-0-port-0"));
    }

    #[test]
    fn multi_template_cache_match_uses_python_optional_whitespace_and_ignore_case() {
        assert!(output_template_matches(
            "INFO worker <*> done",
            "info   worker 42 done"
        ));
    }

    #[test]
    fn separators_tokenize_like_pre_spaced_content() {
        // ADR-0046 core invariant: for the same config, tokenizing
        // separator-bearing content equals tokenizing the same content with
        // the separators already written as spaces.
        let cases: &[(&[&str], &str, &str)] = &[
            // issue #2 shape: commas replace inter-field spaces
            (
                &[","],
                "upload failed,retry 3 times,timeout 5000 ms",
                "upload failed retry 3 times timeout 5000 ms",
            ),
            // full-width comma (U+FF0C) is a first-class separator
            (&["，"], "任务失败，重试 3 次", "任务失败 重试 3 次"),
            // multi-character separator
            (
                &["::"],
                "worker::idle worker::busy",
                "worker idle worker busy",
            ),
            // adjacent runs fold: `a,,b` ≡ `a  b`
            (&[","], "a,,b", "a  b"),
            // separators at both edges are dropped
            (&[","], ",lead trail,", " lead trail "),
            // a `*` separator must not split the `<*>` wildcard marker
            (&["*"], "worker <*> done a*b", "worker <*> done a b"),
        ];

        for (preprocess, typed) in [(false, false), (false, true), (true, false), (true, true)] {
            let base = PiplupConfig {
                preprocess,
                typed_placeholders: typed,
                ..PiplupConfig::default()
            };
            for (separators, original, pre_spaced) in cases {
                let with_separators = PiplupConfig {
                    separators: separators.iter().map(|sep| (*sep).to_string()).collect(),
                    ..base.clone()
                };
                assert_eq!(
                    tokenize_content(&with_separators, original),
                    tokenize_content(&base, pre_spaced),
                    "preprocess={preprocess} typed={typed} original={original:?}"
                );
            }
        }
    }

    #[test]
    fn separators_normalize_after_preprocessing() {
        // ADR-0046 §3: catalog rules run on the original punctuation before
        // separator normalization, so the Time rule consumes `12:34:56`
        // (its `,789` tail falls to the Numerical rule) and the Ipv6 rule
        // consumes a whole address instead of it being shattered on colons.
        let config = PiplupConfig {
            preprocess: true,
            separators: vec![",".to_string(), ":".to_string()],
            ..PiplupConfig::default()
        };

        let tokens = tokenize_content(&config, "job at 12:34:56,789 done");
        // Time matched `12:34:56` and Numerical matched `789`; only the
        // surviving comma splits: `job at <*> <*> done`.
        assert_eq!(tokens.len(), 5);
        assert!(
            tokens.iter().all(|token| !token
                .to_rendered_string()
                .contains(|c: char| c.is_ascii_digit())),
            "the timestamp must not survive preprocessing as literal digits: {tokens:?}"
        );

        let tokens = tokenize_content(&config, "connect to fe80:0:0:0:0:0:0:1 failed");
        // Ipv6 matched the whole address; no colon survives to normalize.
        assert_eq!(tokens.len(), 4);
        assert_eq!(tokens[2], Token::wildcard("", ""));
    }
}

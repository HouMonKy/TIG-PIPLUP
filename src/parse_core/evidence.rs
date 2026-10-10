//! Shared per-template placeholder evidence derivation (ADR-0043 §1).
//!
//! Extracted from `index::builder` so the index builder and the patterns
//! facade replay observed lines against final templates with the same
//! `Unseen/Seen(kind)/Untyped` join semantics, instead of duplicating the
//! state machine. Evidence is keyed by the final rendered template string, so
//! it is immune to cluster-mask staleness after template merges.

use std::collections::BTreeMap;

use crate::{
    PreprocessPlan,
    placeholder::{PlaceholderKind, classify_observed_kind, join},
    preprocessor::Preprocessor,
    template::{CompiledTemplate, TemplateToken},
    types::ParseOutput,
};

/// Evidence state for one template slot while replaying observed lines.
///
/// `Unseen` means no usable observation reached the slot yet; `Seen(kind)`
/// means every observation classified as `kind`; `Untyped` is sticky and means
/// a conflicting or unclassifiable observation downgraded the slot (ADR-0033
/// conservative join).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum MaskEvidence {
    /// No observation reached this slot yet.
    Unseen,
    /// All observations classified as this kind.
    Seen(PlaceholderKind),
    /// A conflicting or unclassifiable observation downgraded the slot.
    Untyped,
}

/// Fold one observed kind into a slot's evidence state.
pub(crate) fn update_mask_evidence(
    current: MaskEvidence,
    observed: Option<PlaceholderKind>,
) -> MaskEvidence {
    match (current, observed) {
        (MaskEvidence::Untyped, _) | (_, None) => MaskEvidence::Untyped,
        (MaskEvidence::Unseen, Some(kind)) => MaskEvidence::Seen(kind),
        (MaskEvidence::Seen(existing), Some(kind)) => match join(Some(existing), Some(kind)) {
            Some(joined) => MaskEvidence::Seen(joined),
            None => MaskEvidence::Untyped,
        },
    }
}

/// Merge two evidence states for the same slot.
///
/// `Unseen` is the identity and `Untyped` is absorbing, so the merge is
/// commutative and associative: evidence collected by independent parser
/// states (e.g. facade label groups) can be combined in any order.
pub(crate) fn join_mask_evidence(left: MaskEvidence, right: MaskEvidence) -> MaskEvidence {
    match (left, right) {
        (MaskEvidence::Untyped, _) | (_, MaskEvidence::Untyped) => MaskEvidence::Untyped,
        (MaskEvidence::Unseen, other) | (other, MaskEvidence::Unseen) => other,
        (MaskEvidence::Seen(left), MaskEvidence::Seen(right)) => {
            match join(Some(left), Some(right)) {
                Some(kind) => MaskEvidence::Seen(kind),
                None => MaskEvidence::Untyped,
            }
        }
    }
}

/// Project evidence slots into a placeholder mask: `Seen(kind)` keeps the
/// kind, while `Unseen`/`Untyped` become `None`.
pub(crate) fn finalize_mask_evidence(slots: &[MaskEvidence]) -> Vec<Option<PlaceholderKind>> {
    slots
        .iter()
        .map(|slot| match slot {
            MaskEvidence::Seen(kind) => Some(*kind),
            MaskEvidence::Unseen | MaskEvidence::Untyped => None,
        })
        .collect()
}

/// Derive per-template wildcard evidence by replaying each assigned line
/// against its final template.
///
/// For every line, the preprocessing captured at training time is replayed
/// with `preprocess_plan` (unless `preprocess` is disabled), the result is
/// split with `tokenize`, and the observed tokens are zipped against the
/// compiled template's tokens. Lines whose observed token count differs from
/// the template token count are skipped (conservative alignment). Only
/// wildcard template positions contribute evidence, classified with
/// [`classify_observed_kind`].
pub(crate) fn build_placeholder_evidence(
    parse_output: &ParseOutput,
    preprocess_plan: &PreprocessPlan,
    preprocess: bool,
    tokenize: impl Fn(&str) -> Vec<String>,
) -> BTreeMap<String, Vec<MaskEvidence>> {
    let preprocessor = Preprocessor::default();
    let mut templates = BTreeMap::<String, CompiledTemplate>::new();
    let mut evidence = BTreeMap::<String, Vec<MaskEvidence>>::new();

    for line in &parse_output.line_results {
        if line.event_id == "0" || line.event_template == "0" {
            continue;
        }
        let Some(content) = line
            .fields
            .iter()
            .find(|field| field.name == "Content")
            .map(|field| field.value.as_str())
        else {
            continue;
        };

        let compiled = templates
            .entry(line.event_template.clone())
            .or_insert_with(|| CompiledTemplate::compile(&line.event_template));
        let processed = if preprocess {
            preprocessor.apply_with_plan(content, preprocess_plan)
        } else {
            content.to_string()
        };
        let observed_tokens = tokenize(&processed);
        if observed_tokens.len() != compiled.tokens().len() {
            continue;
        }

        let slots = evidence
            .entry(line.event_template.clone())
            .or_insert_with(|| vec![MaskEvidence::Unseen; compiled.tokens().len()]);
        for (index, (template_token, observed)) in compiled
            .tokens()
            .iter()
            .zip(observed_tokens.iter())
            .enumerate()
        {
            if !matches!(template_token, TemplateToken::Wildcard { .. }) {
                continue;
            }
            slots[index] = update_mask_evidence(slots[index], classify_observed_kind(observed));
        }
    }

    evidence
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn update_mask_evidence_follows_conservative_join() {
        assert_eq!(
            update_mask_evidence(MaskEvidence::Unseen, Some(PlaceholderKind::Number)),
            MaskEvidence::Seen(PlaceholderKind::Number)
        );
        assert_eq!(
            update_mask_evidence(
                MaskEvidence::Seen(PlaceholderKind::Number),
                Some(PlaceholderKind::Number)
            ),
            MaskEvidence::Seen(PlaceholderKind::Number)
        );
        // Conflict downgrades to untyped.
        assert_eq!(
            update_mask_evidence(
                MaskEvidence::Seen(PlaceholderKind::Number),
                Some(PlaceholderKind::Hex)
            ),
            MaskEvidence::Untyped
        );
        // Unclassifiable observations downgrade to untyped.
        assert_eq!(
            update_mask_evidence(MaskEvidence::Seen(PlaceholderKind::Number), None),
            MaskEvidence::Untyped
        );
        assert_eq!(
            update_mask_evidence(MaskEvidence::Unseen, None),
            MaskEvidence::Untyped
        );
        // Untyped is sticky.
        assert_eq!(
            update_mask_evidence(MaskEvidence::Untyped, Some(PlaceholderKind::Number)),
            MaskEvidence::Untyped
        );
    }

    #[test]
    fn join_mask_evidence_is_order_independent_with_sticky_untyped() {
        // Same kind is kept, regardless of order.
        assert_eq!(
            join_mask_evidence(
                MaskEvidence::Seen(PlaceholderKind::Ipv4),
                MaskEvidence::Seen(PlaceholderKind::Ipv4)
            ),
            MaskEvidence::Seen(PlaceholderKind::Ipv4)
        );
        // Unseen is the identity on both sides.
        assert_eq!(
            join_mask_evidence(
                MaskEvidence::Unseen,
                MaskEvidence::Seen(PlaceholderKind::Hex)
            ),
            MaskEvidence::Seen(PlaceholderKind::Hex)
        );
        assert_eq!(
            join_mask_evidence(
                MaskEvidence::Seen(PlaceholderKind::Hex),
                MaskEvidence::Unseen
            ),
            MaskEvidence::Seen(PlaceholderKind::Hex)
        );
        // Conflicting kinds downgrade, on both orders.
        for (left, right) in [
            (
                MaskEvidence::Seen(PlaceholderKind::Number),
                MaskEvidence::Seen(PlaceholderKind::Hex),
            ),
            (
                MaskEvidence::Seen(PlaceholderKind::Hex),
                MaskEvidence::Seen(PlaceholderKind::Number),
            ),
        ] {
            assert_eq!(join_mask_evidence(left, right), MaskEvidence::Untyped);
        }
        // Untyped is absorbing.
        assert_eq!(
            join_mask_evidence(
                MaskEvidence::Untyped,
                MaskEvidence::Seen(PlaceholderKind::Number)
            ),
            MaskEvidence::Untyped
        );
        assert_eq!(
            join_mask_evidence(
                MaskEvidence::Seen(PlaceholderKind::Number),
                MaskEvidence::Untyped
            ),
            MaskEvidence::Untyped
        );
    }

    #[test]
    fn finalize_mask_evidence_keeps_only_seen_kinds() {
        let slots = [
            MaskEvidence::Seen(PlaceholderKind::Path),
            MaskEvidence::Unseen,
            MaskEvidence::Untyped,
        ];
        assert_eq!(
            finalize_mask_evidence(&slots),
            vec![Some(PlaceholderKind::Path), None, None]
        );
    }
}

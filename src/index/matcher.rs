use std::collections::{BTreeSet, HashMap, HashSet};

use crate::index::types::{AnchorPosting, LengthBucket, TemplateEntry, TokenAtom};
use crate::placeholder::PlaceholderKind;

/// Internal candidate index for selecting template ids to evaluate against a
/// tokenized line.
#[derive(Debug, Clone, Default)]
pub(crate) struct CandidateIndex {
    template_match_orders: Vec<u32>,
    length_buckets: HashMap<u16, Vec<u32>>,
    anchor_postings: HashMap<String, Vec<u32>>,
    no_anchor_template_ids: HashSet<u32>,
}

/// Ordered list of candidate template ids returned by
/// [`CandidateIndex::select_candidates`].
pub(crate) struct CandidateSet {
    pub(crate) template_ids: Vec<u32>,
}

impl CandidateIndex {
    pub(crate) fn new(
        templates: &[TemplateEntry],
        length_buckets: &[LengthBucket],
        anchor_postings: &[AnchorPosting],
    ) -> Self {
        let template_match_orders = templates
            .iter()
            .map(|template| template.match_order)
            .collect();
        let mut index = Self {
            template_match_orders,
            length_buckets: HashMap::with_capacity(length_buckets.len()),
            anchor_postings: HashMap::with_capacity(anchor_postings.len()),
            no_anchor_template_ids: HashSet::with_capacity(templates.len()),
        };

        for template in templates {
            if template.anchors.is_empty() {
                index.no_anchor_template_ids.insert(template.template_id);
            }
        }

        for bucket in length_buckets {
            let mut template_ids = bucket.template_ids.clone();
            index.sort_template_ids(&mut template_ids);
            index
                .length_buckets
                .insert(bucket.token_count, template_ids);
        }

        for posting in anchor_postings {
            let mut template_ids = posting.template_ids.clone();
            index.sort_template_ids(&mut template_ids);
            index
                .anchor_postings
                .insert(posting.anchor.to_ascii_lowercase(), template_ids);
        }

        index
    }

    pub(crate) fn select_candidates(&self, tokens: &[String]) -> CandidateSet {
        let Some(bucket_template_ids) = self.length_buckets.get(&(tokens.len() as u16)) else {
            return CandidateSet {
                template_ids: Vec::new(),
            };
        };

        let literal_tokens = tokens
            .iter()
            .filter(|token| token.chars().any(char::is_alphanumeric))
            .map(|token| token.to_ascii_lowercase())
            .collect::<HashSet<_>>();
        let mut posting_hits = BTreeSet::<u32>::new();
        for token in literal_tokens {
            if let Some(template_ids) = self.anchor_postings.get(token.as_str()) {
                posting_hits.extend(template_ids.iter().copied());
            }
        }

        let template_ids = if posting_hits.is_empty() {
            bucket_template_ids.clone()
        } else {
            bucket_template_ids
                .iter()
                .copied()
                .filter(|template_id| {
                    posting_hits.contains(template_id)
                        || self.no_anchor_template_ids.contains(template_id)
                })
                .collect()
        };

        CandidateSet { template_ids }
    }

    fn sort_template_ids(&self, template_ids: &mut [u32]) {
        template_ids.sort_by_key(|template_id| self.template_match_orders[*template_id as usize]);
    }
}

pub(crate) fn template_matches(
    entry: &TemplateEntry,
    tokens: &[String],
    observed_kinds: &[Option<PlaceholderKind>],
) -> bool {
    if entry.token_atoms.len() != tokens.len() {
        return false;
    }

    entry
        .token_atoms
        .iter()
        .zip(tokens)
        .enumerate()
        .all(|(index, (atom, token))| match atom {
            TokenAtom::Literal { value } => token.eq_ignore_ascii_case(value),
            TokenAtom::Wildcard { head, tail } => {
                token.len() >= head.len() + tail.len()
                    && token
                        .get(..head.len())
                        .is_some_and(|prefix| prefix.eq_ignore_ascii_case(head))
                    && token
                        .get(token.len().saturating_sub(tail.len())..)
                        .is_some_and(|suffix| suffix.eq_ignore_ascii_case(tail))
                    && typed_mask_allows(entry, index, head, tail, observed_kinds)
            }
        })
}

fn typed_mask_allows(
    entry: &TemplateEntry,
    index: usize,
    head: &str,
    tail: &str,
    observed_kinds: &[Option<PlaceholderKind>],
) -> bool {
    let Some(kind) = entry.placeholder_mask.get(index).copied().flatten() else {
        return true;
    };
    if !head.is_empty() || !tail.is_empty() {
        return true;
    }
    observed_kinds.get(index).copied().flatten() == Some(kind)
}

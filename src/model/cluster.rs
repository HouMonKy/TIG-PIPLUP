use crate::{
    engine::structure_merge::{structure_aware_merge, token_covers},
    model::Token,
    placeholder::{PlaceholderKind, PlaceholderMask, maintain_mask_for_template},
    postprocess::correct_single_template,
};

/// A cluster of log lines that share a common event template.
///
/// A `LogCluster` stores the current template (`log_template`), all line ids
/// assigned to it, the set of observed token paths (`path_list`), and the
/// rendered template variants (`template_list`). It mutates during online
/// learning as new lines are observed.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct LogCluster {
    /// Current canonical template as a token sequence.
    pub log_template: Vec<Token>,
    /// Line numbers (or ids) of every line assigned to this cluster.
    pub log_ids: Vec<usize>,
    /// Observed token paths used to refine wildcards.
    pub path_list: Vec<Vec<Token>>,
    /// Rendered template variants; a line is covered if it matches any variant.
    pub template_list: Vec<String>,
    /// Number of times this cluster has been matched.
    pub hit_time: usize,
    /// Branching threshold controlling when new paths are added.
    pub br_thresh: usize,
    /// ADR-0033 internal placeholder metadata, aligned with `log_template` by
    /// position. `Some(kind)` records that every observation merged into that
    /// template slot carried `kind`; `None` means untyped (wildcard/literal
    /// under existing semantics). Parallel metadata — never rendered; preserved
    /// through `maintain_placeholder_mask` via [`placeholder::join`]. Cluster-
    /// level because PIPLUP generalizes wildcards across all paths in
    /// `path_list`. Unused when `typed_placeholders` is disabled.
    pub placeholder_mask: PlaceholderMask,
}

impl LogCluster {
    /// Create a new cluster from its first observed token sequence.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::{LogCluster, Token};
    ///
    /// let cluster = LogCluster::new(
    ///     vec![Token::literal("worker"), Token::literal("started")],
    ///     1,
    ///     5,
    /// );
    /// assert_eq!(cluster.hit_time, 1);
    /// ```
    pub fn new(log_template: Vec<Token>, log_id: usize, br_thresh: usize) -> Self {
        let template = render_template(&log_template);
        let placeholder_mask = vec![None; log_template.len()];
        Self {
            path_list: vec![log_template.clone()],
            log_template,
            log_ids: vec![log_id],
            template_list: vec![template],
            hit_time: 1,
            br_thresh,
            placeholder_mask,
        }
    }

    /// Return the human-readable template that should be used for output.
    ///
    /// If only one variant exists, that variant is returned; otherwise the
    /// canonical `log_template` is rendered.
    pub fn active_template(&self) -> String {
        if self.template_list.len() == 1 {
            return self.template_list[0].clone();
        }
        render_template(&self.log_template)
    }

    /// Check whether `seq` is identical to the canonical template.
    pub fn matches_exactly(&self, seq: &[Token]) -> bool {
        self.log_template == seq
    }

    /// Compute token-sequence similarity between `seq1` and `seq2`.
    ///
    /// A position matches if the tokens are equal or the token in `seq1` is a
    /// wildcard. The result is the fraction of matching positions.
    pub fn seq_similarity(&self, seq1: &[Token], seq2: &[Token]) -> f64 {
        if seq1.is_empty() {
            return 0.0;
        }
        let mut sim = 0usize;
        for (left, right) in seq1.iter().zip(seq2) {
            if left == right || left.contains_wildcard() {
                sim += 1;
            }
        }
        sim as f64 / seq1.len() as f64
    }

    /// Check whether `seq` is covered by the runtime template list without mutating state.
    /// Returns true when `update_sequence` would return false.
    pub fn path_known(&self, seq: &[Token]) -> bool {
        self.template_covers(seq)
    }

    pub fn update_sequence(&mut self, seq: &[Token]) -> bool {
        if self.template_covers(seq) {
            return false;
        }

        if self.path_list.len() < self.br_thresh {
            self.path_list.push(seq.to_vec());
        } else if let Some(target_index) = self.closest_path_index(seq) {
            if self.path_list[target_index].len() != seq.len() {
                return false;
            }
            for (position, token) in seq.iter().enumerate() {
                let target = self.path_list[target_index][position].clone();
                if target == *token {
                    continue;
                }
                let mut merged = structure_aware_merge(&target, token);
                for path in &self.path_list {
                    merged = structure_aware_merge(&merged, &path[position]);
                }
                for path in &mut self.path_list {
                    path[position] = merged.clone();
                }
            }
        }

        dedup_paths(&mut self.path_list);
        self.template_list = self
            .path_list
            .iter()
            .map(|path| render_template(path))
            .collect();
        remove_covered_templates(&mut self.template_list);
        true
    }

    /// Record a match, update templates, and return the previous template list
    /// if it changed.
    pub fn record_match(
        &mut self,
        log_id: usize,
        new_template: Vec<Token>,
        observed_path: Vec<Token>,
    ) -> Option<Vec<String>> {
        self.hit_time += 1;
        self.log_ids.push(log_id);
        self.log_template = new_template;
        let old_template_list = self.template_list.clone();

        if !self.update_sequence(&observed_path) {
            return None;
        }

        if self.template_list == old_template_list {
            return None;
        }

        Some(old_template_list)
    }

    /// Record a match that is already covered by the current templates.
    pub fn record_covered_match(&mut self, log_id: usize, new_template: Vec<Token>) {
        self.hit_time += 1;
        self.log_ids.push(log_id);
        self.log_template = new_template;
    }

    /// ADR-0033: (re)build the [`placeholder_mask`](Self::placeholder_mask) from
    /// the real observed token kinds. Used at cluster creation in typed mode:
    /// the stored template already wildcarded classified tokens, but the mask
    /// must record each slot's kind. `kinds.len()` must equal the template
    /// length.
    pub(crate) fn set_placeholder_mask_from_kinds(&mut self, kinds: &[Option<PlaceholderKind>]) {
        self.placeholder_mask = kinds.to_vec();
    }

    /// ADR-0033: keep [`placeholder_mask`](Self::placeholder_mask) aligned with
    /// `log_template` after a match. Resize to the template length (new slots
    /// untyped), then join each wildcard slot with the observed token's kind.
    /// [`placeholder::join`] downgrades conflicting kinds to `None` rather than
    /// inventing a kind after a merge. Called on every match (template changed
    /// or not) so a soft-similarity cross-kind match downgrades the slot.
    pub(crate) fn maintain_placeholder_mask(&mut self, line_kinds: &[Option<PlaceholderKind>]) {
        maintain_mask_for_template(
            &mut self.placeholder_mask,
            &self.log_template,
            line_kinds,
            |t| t.contains_wildcard(),
        );
    }

    fn closest_path_index(&self, seq: &[Token]) -> Option<usize> {
        let mut max_similarity = -1.0_f64;
        let mut target_index = None;

        for (index, path) in self.path_list.iter().enumerate() {
            let similarity = self.seq_similarity(seq, path);
            if similarity > max_similarity {
                max_similarity = similarity;
                target_index = Some(index);
            }
        }

        target_index
    }

    fn template_covers(&self, seq: &[Token]) -> bool {
        let seq_str = render_template_without_postprocess(seq);
        let seq_str = seq_str.trim();
        self.template_list
            .iter()
            .any(|template| token_covers(template.trim(), seq_str))
    }
}

fn render_template(tokens: &[Token]) -> String {
    correct_single_template(&render_template_without_postprocess(tokens))
}

fn render_template_without_postprocess(tokens: &[Token]) -> String {
    let capacity = tokens
        .iter()
        .map(Token::rendered_len)
        .sum::<usize>()
        .saturating_add(tokens.len().saturating_sub(1));
    let mut rendered = String::with_capacity(capacity);

    for (index, token) in tokens.iter().enumerate() {
        if index > 0 {
            rendered.push(' ');
        }
        token.append_rendered(&mut rendered);
    }

    rendered
}

fn dedup_paths(paths: &mut Vec<Vec<Token>>) {
    paths.sort_by(|left, right| compare_paths(left, right));
    paths.dedup();
}

fn compare_paths(left: &[Token], right: &[Token]) -> std::cmp::Ordering {
    for (left_token, right_token) in left.iter().zip(right.iter()) {
        let ordering = left_token.rendered_cmp(right_token);
        if ordering != std::cmp::Ordering::Equal {
            return ordering;
        }
    }
    left.len().cmp(&right.len())
}

fn remove_covered_templates(templates: &mut Vec<String>) {
    let mut keep = vec![true; templates.len()];
    for i in 0..templates.len() {
        if !keep[i] {
            continue;
        }
        for j in i + 1..templates.len() {
            if token_covers(&templates[j], &templates[i]) {
                keep[i] = false;
                break;
            }
            if token_covers(&templates[i], &templates[j]) {
                keep[j] = false;
            }
        }
    }
    let mut index = 0usize;
    templates.retain(|_| {
        let retain = keep[index];
        index += 1;
        retain
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tokens(items: &[&str]) -> Vec<Token> {
        items.iter().copied().map(Token::literal).collect()
    }

    #[test]
    fn under_capacity_appends_a_new_path() {
        let mut cluster = LogCluster::new(tokens(&["job", "VER.--", "start"]), 1, 5);
        cluster.log_template = tokens(&["job", "<*>", "start"]);
        assert!(cluster.update_sequence(&tokens(&["job", "VER.A", "start"])));
        assert_eq!(cluster.path_list.len(), 2);
    }

    #[test]
    fn capacity_merge_updates_every_path() {
        let mut cluster = LogCluster::new(tokens(&["job", "node.alpha.done"]), 1, 1);
        cluster.log_template = tokens(&["job", "<*>"]);
        assert!(cluster.update_sequence(&tokens(&["job", "node.beta.done"])));
        assert_eq!(
            cluster.path_list[0][1].to_rendered_string(),
            "node.<*>.done"
        );
    }
}

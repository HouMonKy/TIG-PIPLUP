use rustc_hash::FxHashMap;

use crate::template::compile::{CompiledTemplate, SegmentAtom};

/// Memoization map for segment capture ranges: (segment_index, content_index) → optional ranges.
type CaptureMemo = FxHashMap<(usize, usize), Option<Vec<(usize, usize)>>>;

pub fn template_matches(compiled: &CompiledTemplate, content: &str) -> bool {
    matches_segments_fast(compiled.segments(), content)
}

pub fn extract_parameters(compiled: &CompiledTemplate, content: &str) -> Vec<String> {
    captures_for(compiled, content).unwrap_or_default()
}

fn captures_for(compiled: &CompiledTemplate, content: &str) -> Option<Vec<String>> {
    let mut memo = FxHashMap::<(usize, usize), Option<Vec<(usize, usize)>>>::default();
    let capture_ranges =
        capture_ranges_for_segments(compiled.segments(), content, &mut memo, 0, 0)?;

    Some(
        capture_ranges
            .into_iter()
            .map(|(start, end)| content[start..end].to_string())
            .collect(),
    )
}

fn matches_segments_fast(segments: &[Vec<SegmentAtom>], content: &str) -> bool {
    let leading_wildcard = segments.first().is_some_and(|segment| segment.is_empty());
    let trailing_wildcard = segments.last().is_some_and(|segment| segment.is_empty());

    let Some(first_index) = segments.iter().position(|segment| !segment.is_empty()) else {
        return true;
    };
    let last_index = segments
        .iter()
        .rposition(|segment| !segment.is_empty())
        .unwrap_or(first_index);

    if first_index == last_index {
        let segment = &segments[first_index];
        return match (leading_wildcard, trailing_wildcard) {
            (false, false) => match_segment_at(segment, content, 0) == Some(content.len()),
            (false, true) => match_segment_at(segment, content, 0).is_some(),
            (true, false) => segment_matches_suffix_after(segment, content, 0),
            (true, true) => find_segment_end(segment, content, 0).is_some(),
        };
    }

    let mut pos = if leading_wildcard {
        let Some(end) = find_segment_end(&segments[first_index], content, 0) else {
            return false;
        };
        end
    } else {
        let Some(end) = match_segment_at(&segments[first_index], content, 0) else {
            return false;
        };
        end
    };

    for segment in &segments[first_index + 1..last_index] {
        if segment.is_empty() {
            continue;
        }
        let Some(end) = find_segment_end(segment, content, pos) else {
            return false;
        };
        pos = end;
    }

    if trailing_wildcard {
        find_segment_end(&segments[last_index], content, pos).is_some()
    } else {
        segment_matches_suffix_after(&segments[last_index], content, pos)
    }
}

fn find_segment_end(segment: &[SegmentAtom], content: &str, start: usize) -> Option<usize> {
    for next_start in char_boundaries_from(content, start) {
        if let Some(end) = match_segment_at(segment, content, next_start) {
            return Some(end);
        }
    }
    None
}

fn segment_matches_suffix_after(segment: &[SegmentAtom], content: &str, start: usize) -> bool {
    for next_start in char_boundaries_from(content, start) {
        if match_segment_at(segment, content, next_start) == Some(content.len()) {
            return true;
        }
    }
    false
}

fn capture_ranges_for_segments(
    segments: &[Vec<SegmentAtom>],
    content: &str,
    memo: &mut CaptureMemo,
    segment_index: usize,
    content_index: usize,
) -> Option<Vec<(usize, usize)>> {
    if let Some(cached) = memo.get(&(segment_index, content_index)) {
        return cached.clone();
    }

    let result = (|| {
        let segment_end = match_segment_at(&segments[segment_index], content, content_index)?;
        if segment_index + 1 == segments.len() {
            if segment_end == content.len() {
                return Some(Vec::new());
            }
            return None;
        }

        for next_start in char_boundaries_from(content, segment_end) {
            if let Some(mut ranges) =
                capture_ranges_for_segments(segments, content, memo, segment_index + 1, next_start)
            {
                ranges.insert(0, (segment_end, next_start));
                return Some(ranges);
            }
        }

        None
    })();

    memo.insert((segment_index, content_index), result.clone());
    result
}

fn match_segment_at(segment: &[SegmentAtom], content: &str, start: usize) -> Option<usize> {
    let mut index = start;

    for atom in segment {
        match atom {
            SegmentAtom::Literal(literal) => {
                index = match_literal_ignore_ascii_case(content, index, literal)?;
            }
            SegmentAtom::Whitespace => {
                let consumed = content[index..]
                    .chars()
                    .take_while(|ch| ch.is_whitespace())
                    .count();
                if consumed == 0 {
                    return None;
                }
                index = nth_char_boundary(content, index, consumed);
            }
        }
    }

    Some(index)
}

fn match_literal_ignore_ascii_case(content: &str, start: usize, literal: &str) -> Option<usize> {
    let mut content_chars = content[start..].char_indices();
    let mut consumed = 0usize;

    for expected in literal.chars() {
        let (offset, observed) = content_chars.next()?;
        if !observed.eq_ignore_ascii_case(&expected) {
            return None;
        }
        consumed = offset + observed.len_utf8();
    }

    Some(start + consumed)
}

fn char_boundaries_from(content: &str, start: usize) -> impl Iterator<Item = usize> + '_ {
    std::iter::once(start)
        .chain(
            content[start..]
                .char_indices()
                .skip(1)
                .map(move |(offset, _)| start + offset),
        )
        .chain(std::iter::once(content.len()))
}

fn nth_char_boundary(content: &str, start: usize, count: usize) -> usize {
    if count == 0 {
        return start;
    }

    let mut index = start;
    for _ in 0..count {
        let ch = content[index..]
            .chars()
            .next()
            .expect("character boundary traversal requires valid char");
        index += ch.len_utf8();
    }
    index
}

#[cfg(test)]
mod tests {
    use regex::Regex;

    use crate::template::{CompiledTemplate, extract_parameters, template_matches};

    fn regex_extract(template: &str, content: &str) -> Vec<String> {
        let normalized = Regex::new(r"<.{1,5}>")
            .expect("wildcard regex should compile")
            .replace_all(template, "<*>")
            .into_owned();

        if !normalized.contains("<*>") {
            return Vec::new();
        }

        let mut pattern = String::from("(?s)^");
        for (index, segment) in normalized.split("<*>").enumerate() {
            if index > 0 {
                pattern.push_str("(.*?)");
            }
            pattern.push_str(&escape_segment(segment));
        }
        pattern.push('$');

        let regex = Regex::new(&pattern).expect("parameter regex should compile");
        let Some(captures) = regex.captures(content) else {
            return Vec::new();
        };

        captures
            .iter()
            .skip(1)
            .map(|capture| {
                capture
                    .map(|value| value.as_str().to_string())
                    .unwrap_or_default()
            })
            .collect()
    }

    fn escape_segment(segment: &str) -> String {
        let mut escaped = String::new();
        let mut whitespace_run = false;

        for ch in segment.chars() {
            if ch.is_whitespace() {
                if !whitespace_run {
                    escaped.push_str(r"\s+");
                    whitespace_run = true;
                }
                continue;
            }

            whitespace_run = false;
            escaped.push_str(&regex::escape(&ch.to_string()));
        }

        escaped
    }

    fn assert_matches_regex(template: &str, content: &str) {
        let compiled = CompiledTemplate::compile(template);
        let expected_parameters = regex_extract(template, content);

        assert_eq!(extract_parameters(&compiled, content), expected_parameters);
        assert_eq!(
            template_matches(&compiled, content),
            !expected_parameters.is_empty()
        );
    }

    #[test]
    fn extractor_returns_empty_for_literal_templates() {
        let compiled = CompiledTemplate::compile("INFO worker done");
        assert!(template_matches(&compiled, "INFO worker done"));
        assert_eq!(
            extract_parameters(&compiled, "INFO worker done"),
            Vec::<String>::new()
        );
        assert!(!template_matches(&compiled, "INFO worker wait"));
    }

    #[test]
    fn extractor_matches_regex_for_common_cases() {
        let cases = [
            ("user=<*>", "user=alice"),
            ("user=<*> status=<*>", "user=alice status=ok"),
            ("<*> worker done", "INFO worker done"),
            (
                "setting hostname to \"<*>\"",
                "setting hostname to \"authorMacBook-Pro.local\"",
            ),
            ("A <*> B <*> C", "A one B two C"),
            ("A <*> A <*>", "A one A two"),
            ("A <*> B", "A \n\tvalue\t B"),
            (
                "<NUM>: scheduler <*> seconds. Ignoring.",
                "123: scheduler 2450 seconds.\t Ignoring.",
            ),
        ];

        for (template, content) in cases {
            assert_matches_regex(template, content);
        }
    }

    #[test]
    fn matcher_treats_literal_whitespace_runs_as_flexible_whitespace() {
        let compiled = CompiledTemplate::compile("Dop Io type: read , IOPS <*>");
        let content = "Dop Io type: read\u{a0}   ,\u{a0} IOPS 34 MB/s.";

        assert!(template_matches(&compiled, content));
        assert_eq!(
            extract_parameters(&compiled, content),
            vec!["34 MB/s.".to_string()]
        );
        assert!(!template_matches(
            &compiled,
            "Dop Io type: read,IOPS 34 MB/s."
        ));
    }

    #[test]
    fn matcher_ignores_ascii_case_for_literal_segments() {
        let compiled = CompiledTemplate::compile("Init succeed, subModIndex <*> diff time <*>.");
        let content = "init SUCCEED, subModIndex 3 diff TIME 15.";

        assert!(template_matches(&compiled, content));
        assert_eq!(
            extract_parameters(&compiled, content),
            vec!["3".to_string(), "15".to_string()]
        );
    }

    #[test]
    fn extractor_uses_non_greedy_matching_for_repeated_segments() {
        let compiled = CompiledTemplate::compile("A <*> B <*> B");
        assert_eq!(
            extract_parameters(&compiled, "A one B two B"),
            vec!["one".to_string(), "two".to_string()]
        );
    }

    #[test]
    fn extractor_returns_empty_on_mismatch() {
        let compiled = CompiledTemplate::compile("INFO <*> done");
        assert_eq!(
            extract_parameters(&compiled, "WARN worker done"),
            Vec::<String>::new()
        );
        assert!(!template_matches(&compiled, "WARN worker done"));
    }

    #[test]
    fn matcher_allows_flexible_literal_whitespace() {
        let compiled = CompiledTemplate::compile("<*>: scheduler <*> seconds. Ignoring.");
        assert!(template_matches(
            &compiled,
            "jobd: scheduler 2450 seconds.\t Ignoring."
        ));
        assert!(template_matches(
            &compiled,
            "jobd: scheduler 2450 seconds. Ignoring."
        ));
        assert_eq!(
            extract_parameters(&compiled, "jobd: scheduler 2450 seconds.\t Ignoring."),
            vec!["jobd".to_string(), "2450".to_string()]
        );
    }
}

//! Structure-aware token merging used by TIG-PIPLUP `3e51f3b`.

use crate::model::Token;

const PATH_DELIMITERS: [char; 18] = [
    ' ', ',', '!', ';', ':', '=', '|', '"', '\'', '+', '.', '[', ']', '(', ')', '{', '}', '_',
];

pub(crate) fn structure_aware_merge(left: &Token, right: &Token) -> Token {
    let left = left.render();
    let right = right.render();
    Token::from_rendered_template(structure_aware_merge_str(left.as_ref(), right.as_ref()))
}

pub(crate) fn structure_aware_merge_str(left: &str, right: &str) -> String {
    if left == right {
        return left.to_string();
    }
    for (general, specific) in [(left, right), (right, left)] {
        if general.contains("<*>") && token_covers(general, specific) {
            return general.to_string();
        }
    }

    let left_chars: Vec<char> = left.chars().collect();
    let right_chars: Vec<char> = right.chars().collect();
    let common_prefix = left_chars
        .iter()
        .zip(&right_chars)
        .take_while(|(a, b)| a == b)
        .count();

    let mut head_end = None;
    for index in (0..common_prefix).rev() {
        if PATH_DELIMITERS.contains(&left_chars[index]) && left_chars[index] != '_' {
            head_end = Some(index);
            break;
        }
    }
    if let Some(non_underscore_end) = head_end {
        for (index, ch) in left_chars
            .iter()
            .enumerate()
            .take(common_prefix)
            .skip(non_underscore_end + 1)
        {
            if *ch == '_' && index < common_prefix - 1 {
                head_end = Some(index);
                break;
            }
        }
    } else {
        head_end = (0..common_prefix).find(|&index| PATH_DELIMITERS.contains(&left_chars[index]));
    }
    let head: String = head_end
        .map(|index| left_chars[..=index].iter().collect())
        .unwrap_or_default();

    let common_suffix = left_chars
        .iter()
        .rev()
        .zip(right_chars.iter().rev())
        .take_while(|(a, b)| a == b)
        .count();
    let mut tail = String::new();
    if common_suffix > 0 {
        let start = left_chars.len() - common_suffix;
        tail = left_chars[start..].iter().collect();
        if let Some(index) =
            (start..left_chars.len()).find(|&index| PATH_DELIMITERS.contains(&left_chars[index]))
        {
            tail = left_chars[index..].iter().collect();
        }
    }

    let mut middle = String::new();
    if tail.is_empty() {
        let mut index = common_prefix;
        let limit = left_chars.len().min(right_chars.len());
        while index < limit {
            if left_chars[index] != '_'
                && PATH_DELIMITERS.contains(&left_chars[index])
                && left_chars[index] == right_chars[index]
            {
                let mut end = index + 1;
                while end < limit && left_chars[end] == right_chars[end] {
                    end += 1;
                }
                if end - index > middle.chars().count().max(1) {
                    middle = left_chars[index..end].iter().collect();
                }
                index = end;
            } else {
                index += 1;
            }
        }
    }

    if head.chars().count() + middle.chars().count() + tail.chars().count()
        >= left_chars.len().min(right_chars.len())
    {
        return "<*>".to_string();
    }
    let result = if middle.is_empty() {
        format!("{head}<*>{tail}")
    } else {
        format!("{head}<*>{middle}<*>{tail}")
    };
    if token_covers(&result, left) && token_covers(&result, right) {
        result
    } else {
        "<*>".to_string()
    }
}

pub(crate) fn token_covers(general: &str, specific: &str) -> bool {
    let parts: Vec<&str> = general.split("<*>").collect();
    if parts.len() == 1 {
        return general == specific;
    }
    let mut cursor = 0usize;
    if let Some(head) = parts.first().filter(|head| !head.is_empty()) {
        if !specific.starts_with(head) {
            return false;
        }
        cursor = head.len();
    }
    for part in parts.iter().skip(1).take(parts.len().saturating_sub(2)) {
        if part.is_empty() {
            continue;
        }
        let Some(offset) = specific[cursor..].find(part) else {
            return false;
        };
        cursor += offset + part.len();
    }
    let tail = parts.last().copied().unwrap_or_default();
    tail.is_empty()
        || specific
            .strip_suffix(tail)
            .is_some_and(|prefix| prefix.len() >= cursor)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn covering_wildcard_is_preserved() {
        assert_eq!(structure_aware_merge_str("<*>", "0,"), "<*>");
        assert_eq!(structure_aware_merge_str("name=<*>", "name=42"), "name=<*>");
    }

    #[test]
    fn delimiter_bounded_affixes_are_preserved() {
        assert_eq!(
            structure_aware_merge_str("node.alpha.done", "node.beta.done"),
            "node.<*>.done"
        );
    }

    #[test]
    fn matching_is_case_sensitive_like_python() {
        assert!(token_covers("worker <*> done", "worker 42 done"));
        assert!(!token_covers("Worker <*> done", "worker 42 done"));
    }
}

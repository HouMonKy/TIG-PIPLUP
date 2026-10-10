use rustc_hash::FxHashMap;

use crate::template::{CompiledTemplate as RuntimeTemplate, extract_parameters};

/// Extract parameter values for output `LineResult.parameter_list`.
///
/// `keep_para = false` ⇒ all methods return `None`. Unmatched/sentinel lines
/// return `Some(Vec::new())` when `keep_para = true`.
pub(crate) struct ParameterExtractor {
    keep_para: bool,
    cache: FxHashMap<String, RuntimeTemplate>,
}

impl ParameterExtractor {
    pub(crate) fn new(keep_para: bool) -> Self {
        Self {
            keep_para,
            cache: FxHashMap::default(),
        }
    }

    /// `keep_para = true` ⇒ `Some(Vec::new())`; `keep_para = false` ⇒ `None`.
    pub(crate) fn unmatched(&self) -> Option<Vec<String>> {
        if self.keep_para {
            Some(Vec::new())
        } else {
            None
        }
    }

    /// Compile (cached) and extract. `keep_para = false` ⇒ `None`.
    pub(crate) fn for_template(&mut self, template: &str, content: &str) -> Option<Vec<String>> {
        if !self.keep_para {
            return None;
        }
        let compiled = self
            .cache
            .entry(template.to_string())
            .or_insert_with(|| RuntimeTemplate::compile(template));
        Some(extract_parameters(compiled, content))
    }

    /// Extract using a precompiled runtime template. `keep_para = false` ⇒ `None`.
    pub(crate) fn for_runtime(
        &self,
        runtime: &RuntimeTemplate,
        content: &str,
    ) -> Option<Vec<String>> {
        if !self.keep_para {
            return None;
        }
        Some(extract_parameters(runtime, content))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keep_para_false_returns_none() {
        // keep_para = false disables every extraction path, including unmatched.
        let mut extractor = ParameterExtractor::new(false);
        let compiled = RuntimeTemplate::compile("worker <*> started");

        assert_eq!(extractor.unmatched(), None);
        assert_eq!(
            extractor.for_template("worker <*> started", "worker 42 started"),
            None
        );
        assert_eq!(extractor.for_runtime(&compiled, "worker 42 started"), None);
    }

    #[test]
    fn unmatched_returns_empty_vec_when_keep_para() {
        // Unmatched/sentinel lines emit Some(Vec::new()) when keep_para = true,
        // preserving the None-vs-Some(Vec::new()) distinction.
        let extractor = ParameterExtractor::new(true);
        assert_eq!(extractor.unmatched(), Some(Vec::<String>::new()));
    }

    #[test]
    fn for_template_extracts_parameters_and_caches() {
        // for_template compiles (cached) and extracts, matching the legacy
        // build_parameter_list output for the same template/content. The second
        // call exercises the cache entry and must yield identical output.
        let mut extractor = ParameterExtractor::new(true);
        let template = "worker <*> started";
        let content = "worker 42 started";
        let expected = extract_parameters(&RuntimeTemplate::compile(template), content);

        assert_eq!(
            extractor.for_template(template, content),
            Some(expected.clone())
        );
        assert_eq!(extractor.for_template(template, content), Some(expected));
    }

    #[test]
    fn for_runtime_extracts_using_precompiled_template() {
        // for_runtime uses a caller-supplied compiled template (indexed-parse
        // path) and must agree with for_template for the same template/content.
        let mut extractor = ParameterExtractor::new(true);
        let template = "user=<*> status=<*>";
        let content = "user=alice status=ok";
        let compiled = RuntimeTemplate::compile(template);

        let from_runtime = extractor.for_runtime(&compiled, content);
        let from_template = extractor.for_template(template, content);

        assert_eq!(
            from_runtime,
            Some(vec!["alice".to_string(), "ok".to_string()])
        );
        assert_eq!(from_runtime, from_template);
    }
}

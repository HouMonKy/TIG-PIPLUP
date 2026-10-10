use std::io::Write;

use crate::{
    error::LogParseError,
    index::{
        builder::build_header,
        types::{IndexHeader, TemplateIndexFile, TemplateIndexPayload},
    },
};

/// Serializes a [`TemplateIndexFile`] to bytes.
///
/// # Example
///
/// ```rust
/// use log_parser::{IndexWriter, IndexBuilder, PiplupConfig, PiplupParser, LogEntry};
///
/// let mut parser = PiplupParser::new(PiplupConfig::default()).unwrap();
/// let entries = vec![
///     LogEntry { line_number: 1, raw: "2026-06-05 10:00:00 worker started".into() },
/// ];
/// let training = parser.parse_lines_for_training(&entries).unwrap();
/// let index = IndexBuilder::build(&training, &PiplupConfig::default()).unwrap();
/// let bytes = IndexWriter::write_to_vec(&index).unwrap();
/// assert!(!bytes.is_empty());
/// ```
pub struct IndexWriter;

impl IndexWriter {
    /// Serialize the index to a byte vector.
    pub fn write_to_vec(index: &TemplateIndexFile) -> Result<Vec<u8>, LogParseError> {
        let mut bytes = Vec::new();
        Self::write(&mut bytes, index)?;
        Ok(bytes)
    }

    /// Serialize the index to any `std::io::Write` destination.
    ///
    /// The preprocess plan is canonicalized (sorted, deduplicated) before
    /// hashing and serialization: the reader's `Deserialize` impl rebuilds the
    /// plan through `PreprocessPlan::new`, which sorts the rule ids. Hashing
    /// the plan in encounter order would make every index with two or more
    /// out-of-order rules fail its own `preprocess_plan_hash` validation.
    /// Rule application order is unaffected — `apply_with_plan` always walks
    /// the catalog order, so this is a pure serialization fix.
    pub fn write<W: Write>(mut writer: W, index: &TemplateIndexFile) -> Result<(), LogParseError> {
        let mut index = index.clone();
        index.preprocess_plan = crate::PreprocessPlan::new(index.preprocess_plan.enabled_rule_ids);
        index.header = build_header(&index)?;
        let payload = serde_json::to_vec(&TemplateIndexPayload::from(&index))?;

        write_header(&mut writer, &index.header)?;
        writer.write_all(&payload)?;
        Ok(())
    }
}

fn write_header<W: Write>(writer: &mut W, header: &IndexHeader) -> Result<(), LogParseError> {
    writer.write_all(&header.magic)?;
    writer.write_all(&header.format_version.to_le_bytes())?;
    writer.write_all(&header.header_len.to_le_bytes())?;
    writer.write_all(&header.payload_len.to_le_bytes())?;
    writer.write_all(&header.runtime_config_hash)?;
    writer.write_all(&header.template_set_hash)?;
    writer.write_all(&header.preprocess_plan_hash)?;
    writer.write_all(&header.checksum)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::index::IndexReader;
    use crate::{LogEntry, PiplupConfig, PiplupParser, RuleId};

    #[test]
    fn write_canonicalizes_out_of_order_preprocess_plan() {
        // Regression: the reader's `Deserialize` rebuilds the plan through
        // `PreprocessPlan::new` (sorted). Before canonicalization on the
        // writer side, any plan whose ids were recorded in encounter order
        // failed its own hash validation on read-back.
        let config = PiplupConfig::default();
        let entries: Vec<LogEntry> = vec![
            LogEntry {
                line_number: 1,
                raw: "2026-04-11 12:00:00 connected to 10.0.0.1:8080".into(),
            },
            LogEntry {
                line_number: 2,
                raw: "2026-04-11 12:00:01 fetched https://example.com/x".into(),
            },
        ];
        let mut parser = PiplupParser::new(config.clone()).unwrap();
        let training = parser.parse_lines_for_training(&entries).unwrap();

        let mut index = crate::index::IndexBuilder::build(&training, &config).unwrap();
        // Force the pathological order: Numerical sorts after Url, so store
        // them reversed relative to canonical (declaration) order.
        index.preprocess_plan = crate::PreprocessPlan {
            enabled_rule_ids: vec![RuleId::Numerical, RuleId::Ipv4Port, RuleId::Url],
        };

        let bytes = IndexWriter::write_to_vec(&index).unwrap();
        let restored = IndexReader::read_from_bytes(&bytes, &config)
            .expect("round-trip must pass hash validation");
        assert_eq!(
            restored.preprocess_plan.enabled_rule_ids,
            vec![RuleId::Url, RuleId::Ipv4Port, RuleId::Numerical],
            "written plan must be canonical (sorted) so reader hash matches"
        );
    }
}

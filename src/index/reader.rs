use std::io::Read;

use crate::{
    PiplupConfig,
    error::LogParseError,
    index::{
        builder::{payload_checksum, preprocess_plan_hash, runtime_config_hash, template_set_hash},
        types::*,
    },
    preprocessor::default_rules,
};

/// Validates and deserializes a piplup index file.
///
/// # Example
///
/// ```rust
/// use log_parser::{IndexReader, IndexWriter, PiplupConfig, PiplupParser, LogEntry, IndexBuilder};
///
/// let mut parser = PiplupParser::new(PiplupConfig::default()).unwrap();
/// let entries = vec![
///     LogEntry { line_number: 1, raw: "2026-06-05 10:00:00 worker started".into() },
/// ];
/// let training = parser.parse_lines_for_training(&entries).unwrap();
/// let index = IndexBuilder::build(&training, &PiplupConfig::default()).unwrap();
/// let bytes = IndexWriter::write_to_vec(&index).unwrap();
/// let loaded = IndexReader::read_from_bytes(&bytes, &PiplupConfig::default()).unwrap();
/// assert_eq!(loaded.template_count(), 1);
/// ```
pub struct IndexReader;

impl IndexReader {
    /// Read an index from a byte slice.
    pub fn read_from_bytes(
        bytes: &[u8],
        config: &PiplupConfig,
    ) -> Result<TemplateIndexFile, LogParseError> {
        Self::read(std::io::Cursor::new(bytes), config)
    }

    /// Read an index from any `std::io::Read` source.
    pub fn read<R: Read>(
        mut reader: R,
        config: &PiplupConfig,
    ) -> Result<TemplateIndexFile, LogParseError> {
        let header = read_header(&mut reader)?;
        validate_header(&header)?;

        let mut payload = Vec::new();
        reader.read_to_end(&mut payload)?;
        if payload.len() != header.payload_len as usize {
            return Err(LogParseError::InvalidIndex {
                message: format!(
                    "payload length mismatch: header says {}, actual {}",
                    header.payload_len,
                    payload.len()
                ),
            });
        }
        if payload_checksum(&payload) != header.checksum {
            return Err(LogParseError::InvalidIndex {
                message: "payload checksum mismatch".to_string(),
            });
        }

        let payload_data: TemplateIndexPayload = serde_json::from_slice(&payload)?;
        let index = TemplateIndexFile {
            header,
            runtime: payload_data.runtime,
            training: payload_data.training,
            preprocess_plan: payload_data.preprocess_plan,
            templates: payload_data.templates,
            length_buckets: payload_data.length_buckets,
            anchor_postings: payload_data.anchor_postings,
            training_assignments: payload_data.training_assignments,
            training_misses: payload_data.training_misses,
        };

        validate_payload_runtime_hash(&index)?;
        validate_runtime_hash(&index, config)?;
        validate_payload_hashes(&index)?;
        validate_index_structure(&index)?;
        Ok(index)
    }
}

fn read_header<R: Read>(reader: &mut R) -> Result<IndexHeader, LogParseError> {
    let mut magic = [0u8; 7];
    reader.read_exact(&mut magic)?;
    let format_version = read_u32(reader)?;
    let header_len = read_u32(reader)?;
    let payload_len = read_u64(reader)?;
    let runtime_config_hash = read_hash(reader)?;
    let template_set_hash = read_hash(reader)?;
    let preprocess_plan_hash = read_hash(reader)?;
    let checksum = read_hash(reader)?;

    Ok(IndexHeader {
        magic,
        format_version,
        header_len,
        payload_len,
        runtime_config_hash,
        template_set_hash,
        preprocess_plan_hash,
        checksum,
    })
}

fn validate_header(header: &IndexHeader) -> Result<(), LogParseError> {
    if &header.magic != INDEX_MAGIC {
        return Err(LogParseError::InvalidIndex {
            message: "invalid index magic".to_string(),
        });
    }
    if header.format_version != INDEX_FORMAT_VERSION {
        return Err(LogParseError::InvalidIndex {
            message: format!("unsupported index format version {}", header.format_version),
        });
    }
    if header.header_len != IndexHeader::SERIALIZED_LEN {
        return Err(LogParseError::InvalidIndex {
            message: format!("unexpected header length {}", header.header_len),
        });
    }
    if header.payload_len == 0 {
        return Err(LogParseError::InvalidIndex {
            message: "index payload is empty".to_string(),
        });
    }
    Ok(())
}

fn validate_runtime_hash(
    index: &TemplateIndexFile,
    config: &PiplupConfig,
) -> Result<(), LogParseError> {
    let runtime = RuntimeCompatibility::for_piplup_config(config);
    if runtime_config_hash(&runtime)? != index.header.runtime_config_hash {
        return Err(LogParseError::IncompatibleIndexRuntime);
    }
    Ok(())
}

pub(crate) fn validate_payload_runtime_hash(
    index: &TemplateIndexFile,
) -> Result<(), LogParseError> {
    if runtime_config_hash(&index.runtime)? != index.header.runtime_config_hash {
        return Err(LogParseError::InvalidIndex {
            message: "payload runtime metadata does not match header runtime hash".to_string(),
        });
    }
    Ok(())
}

pub(crate) fn validate_payload_hashes(index: &TemplateIndexFile) -> Result<(), LogParseError> {
    if template_set_hash(&index.templates)? != index.header.template_set_hash {
        return Err(LogParseError::InvalidIndex {
            message: "template set hash mismatch".to_string(),
        });
    }
    if preprocess_plan_hash(&index.preprocess_plan)? != index.header.preprocess_plan_hash {
        return Err(LogParseError::InvalidIndex {
            message: "preprocess plan hash mismatch".to_string(),
        });
    }
    Ok(())
}

pub(crate) fn validate_index_structure(index: &TemplateIndexFile) -> Result<(), LogParseError> {
    let valid_template_ids = index
        .templates
        .iter()
        .map(|template| template.template_id)
        .collect::<std::collections::BTreeSet<_>>();
    let valid_rule_ids = default_rules()?
        .into_iter()
        .map(|rule| rule.id)
        .collect::<std::collections::BTreeSet<_>>();

    if valid_template_ids.len() != index.templates.len() {
        return Err(LogParseError::InvalidIndex {
            message: "template_id values must be unique".to_string(),
        });
    }

    let expected_template_ids =
        (0..index.templates.len() as u32).collect::<std::collections::BTreeSet<_>>();
    if valid_template_ids != expected_template_ids {
        return Err(LogParseError::InvalidIndex {
            message: "template_id values must form a dense 0..N-1 sequence".to_string(),
        });
    }

    for rule_id in &index.preprocess_plan.enabled_rule_ids {
        if !valid_rule_ids.contains(rule_id) {
            return Err(LogParseError::InvalidIndex {
                message: format!("unknown preprocess rule id {:?}", rule_id),
            });
        }
    }

    for template in &index.templates {
        if template.token_count as usize != template.token_atoms.len() {
            return Err(LogParseError::InvalidIndex {
                message: format!(
                    "template {} token_count does not match token_atoms length",
                    template.template_id
                ),
            });
        }
        if template.literal_count as usize + template.wildcard_count as usize
            != template.token_atoms.len()
        {
            return Err(LogParseError::InvalidIndex {
                message: format!(
                    "template {} literal/wildcard counts do not match token_atoms length",
                    template.template_id
                ),
            });
        }
        if template.placeholder_mask.len() != template.token_atoms.len() {
            return Err(LogParseError::InvalidIndex {
                message: format!(
                    "template {} placeholder_mask does not match token_atoms length",
                    template.template_id
                ),
            });
        }
        for (index, (atom, kind)) in template
            .token_atoms
            .iter()
            .zip(&template.placeholder_mask)
            .enumerate()
        {
            if matches!(atom, TokenAtom::Literal { .. }) && kind.is_some() {
                return Err(LogParseError::InvalidIndex {
                    message: format!(
                        "template {} literal token {} has typed placeholder metadata",
                        template.template_id, index
                    ),
                });
            }
        }
        if template.segments.len() != template.segment_atoms.len() {
            return Err(LogParseError::InvalidIndex {
                message: format!(
                    "template {} segments and segment_atoms length mismatch",
                    template.template_id
                ),
            });
        }
    }

    for bucket in &index.length_buckets {
        for template_id in &bucket.template_ids {
            if !valid_template_ids.contains(template_id) {
                return Err(LogParseError::InvalidIndex {
                    message: format!(
                        "length bucket {} references unknown template {}",
                        bucket.token_count, template_id
                    ),
                });
            }
        }
    }

    for posting in &index.anchor_postings {
        for template_id in &posting.template_ids {
            if !valid_template_ids.contains(template_id) {
                return Err(LogParseError::InvalidIndex {
                    message: format!(
                        "anchor posting {} references unknown template {}",
                        posting.anchor, template_id
                    ),
                });
            }
        }
    }

    Ok(())
}

fn read_u32<R: Read>(reader: &mut R) -> Result<u32, LogParseError> {
    let mut bytes = [0u8; 4];
    reader.read_exact(&mut bytes)?;
    Ok(u32::from_le_bytes(bytes))
}

fn read_u64<R: Read>(reader: &mut R) -> Result<u64, LogParseError> {
    let mut bytes = [0u8; 8];
    reader.read_exact(&mut bytes)?;
    Ok(u64::from_le_bytes(bytes))
}

fn read_hash<R: Read>(reader: &mut R) -> Result<[u8; 32], LogParseError> {
    let mut bytes = [0u8; 32];
    reader.read_exact(&mut bytes)?;
    Ok(bytes)
}

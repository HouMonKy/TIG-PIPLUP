//! Cache lifecycle: load, save, save_checkpoint.
//!
//! `state_hash` is always over the **uncompressed** payload — the same state
//! produces the same hash regardless of compression, so a cache file can be
//! re-saved with or without LZ4 without invalidating it. Compression itself
//! is applied to the bincode bytes as a whole, gated behind
//! `#[cfg(feature = "cache-compress")]`.

use std::fs;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};

use sha2::{Digest, Sha256};
use tempfile::NamedTempFile;

use crate::cache::dto::{CACHE_STATE_VERSION, CacheState};
use crate::cache::format::{CacheHeader, FLAG_LZ4_COMPRESSED};
use crate::config::PiplupConfig;
use crate::error::LogParseError;
use crate::preprocessor::PreprocessPlan;

const MAX_PAYLOAD_BYTES: usize = 256 * 1024 * 1024;

/// Manages loading and saving of piplup cache files.
#[derive(Debug, Clone, Default)]
pub struct CacheManager;

impl CacheManager {
    /// Create a new cache manager.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::CacheManager;
    ///
    /// let manager = CacheManager::new();
    /// ```
    pub fn new() -> Self {
        Self
    }

    /// Read and validate a cache from `reader`. The returned [`CacheState`]
    /// carries `input_identity` and `processed_line_count` — the caller is
    /// responsible for deciding whether to honor the cursor.
    pub fn load<R: Read>(reader: &mut R) -> Result<CacheState, LogParseError> {
        let header = CacheHeader::read(reader)?;
        let payload_len =
            usize::try_from(header.payload_len).map_err(|_| LogParseError::InvalidCache {
                message: "cache payload_len exceeds usize".to_string(),
            })?;
        if payload_len > MAX_PAYLOAD_BYTES {
            return Err(LogParseError::InvalidCache {
                message: "cache payload too large".to_string(),
            });
        }
        let mut payload = vec![0u8; payload_len];
        match reader.read_exact(&mut payload) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::UnexpectedEof => {
                return Err(LogParseError::InvalidCache {
                    message: "cache file is truncated (payload)".to_string(),
                });
            }
            Err(error) => return Err(LogParseError::Io(error)),
        }

        let uncompressed = decompress_payload(&payload, header.is_compressed())?;

        let mut hasher = Sha256::new();
        hasher.update(&uncompressed);
        let computed_state_hash: [u8; 32] = hasher.finalize().into();
        if computed_state_hash != header.state_hash {
            return Err(LogParseError::InvalidCache {
                message: "cache state hash mismatch".to_string(),
            });
        }

        let mut cache: CacheState =
            bincode::serde::decode_from_slice(&uncompressed, bincode::config::standard())
                .map_err(|error| LogParseError::InvalidCache {
                    message: format!("cache bincode decode failed: {error}"),
                })?
                .0;
        if cache.version != CACHE_STATE_VERSION {
            return Err(LogParseError::InvalidCache {
                message: format!(
                    "cache state version {} does not match expected {CACHE_STATE_VERSION}",
                    cache.version
                ),
            });
        }

        if cache.runtime_config_hash != header.runtime_config_hash {
            return Err(LogParseError::IncompatibleCacheRuntime);
        }
        if cache.preprocess_plan_hash != header.preprocess_plan_hash {
            return Err(LogParseError::IncompatibleCachePlan);
        }

        // Sync header-stamped hashes into the DTO so callers can inspect
        // them without going through the header.
        cache.runtime_config_hash = header.runtime_config_hash;
        cache.preprocess_plan_hash = header.preprocess_plan_hash;

        Ok(cache)
    }

    /// Save the cache to `writer`.
    /// The caller is responsible for skipping this call when the cache state
    /// is unchanged from the last save.
    pub fn save<W: Write>(
        &self,
        writer: &mut W,
        cache: &CacheState,
        compress: bool,
    ) -> Result<(), LogParseError> {
        write_payload(writer, cache, compress)
    }

    /// Save a checkpoint. The `cache` should already have the cursor and
    /// input identity stamped.
    pub fn save_checkpoint<W: Write>(
        &self,
        writer: &mut W,
        cache: &CacheState,
        compress: bool,
    ) -> Result<(), LogParseError> {
        write_payload(writer, cache, compress)?;
        Ok(())
    }

    /// File-system convenience: load from `path`.
    pub fn load_from_path(path: &Path) -> Result<CacheState, LogParseError> {
        let mut file = fs::File::open(path).map_err(|error| LogParseError::InvalidCache {
            message: format!("failed to open cache {}: {error}", path.display()),
        })?;
        Self::load(&mut file)
    }

    /// File-system convenience: save to `path` atomically.
    pub fn save_to_path(
        path: &Path,
        cache: &CacheState,
        compress: bool,
    ) -> Result<(), LogParseError> {
        let parent = path
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .map(Path::to_path_buf)
            .unwrap_or_else(|| PathBuf::from("."));
        fs::create_dir_all(&parent)?;
        let mut temp = NamedTempFile::new_in(&parent)?;
        CacheManager::new().save(temp.as_file_mut(), cache, compress)?;
        match temp.persist(path) {
            Ok(_) => Ok(()),
            Err(error) => Err(LogParseError::Io(error.error)),
        }
    }

    /// File-system convenience: save a checkpoint to `path` atomically.
    pub fn save_checkpoint_to_path(
        path: &Path,
        cache: &CacheState,
        compress: bool,
    ) -> Result<(), LogParseError> {
        let parent = path
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .map(Path::to_path_buf)
            .unwrap_or_else(|| PathBuf::from("."));
        fs::create_dir_all(&parent)?;
        let mut temp = NamedTempFile::new_in(&parent)?;
        CacheManager::new().save_checkpoint(temp.as_file_mut(), cache, compress)?;
        match temp.persist(path) {
            Ok(_) => Ok(()),
            Err(error) => Err(LogParseError::Io(error.error)),
        }
    }
}

fn write_payload<W: Write>(
    writer: &mut W,
    cache: &CacheState,
    compress: bool,
) -> Result<(), LogParseError> {
    // state_hash is always computed over the uncompressed bincode bytes so
    // that hash and compression are orthogonal.
    let uncompressed =
        bincode::serde::encode_to_vec(cache, bincode::config::standard()).map_err(|error| {
            LogParseError::InvalidCache {
                message: format!("cache bincode encode failed: {error}"),
            }
        })?;
    let state_hash: [u8; 32] = {
        let mut hasher = Sha256::new();
        hasher.update(&uncompressed);
        hasher.finalize().into()
    };

    let payload = encode_payload(&uncompressed, compress)?;
    let flags = if compress { FLAG_LZ4_COMPRESSED } else { 0 };
    let header = CacheHeader::new(
        flags,
        payload.len() as u64,
        cache.runtime_config_hash,
        cache.preprocess_plan_hash,
        state_hash,
    );
    header.write(writer)?;
    writer.write_all(&payload)?;
    Ok(())
}

fn encode_payload(uncompressed: &[u8], compress: bool) -> Result<Vec<u8>, LogParseError> {
    if compress {
        #[cfg(feature = "cache-compress")]
        {
            Ok(lz4_flex::compress_prepend_size(uncompressed))
        }
        #[cfg(not(feature = "cache-compress"))]
        {
            let _ = uncompressed;
            Err(LogParseError::InvalidCache {
                message: "cache-compress feature is not enabled".to_string(),
            })
        }
    } else {
        Ok(uncompressed.to_vec())
    }
}

fn decompress_payload(payload: &[u8], is_compressed: bool) -> Result<Vec<u8>, LogParseError> {
    if !is_compressed {
        return Ok(payload.to_vec());
    }
    #[cfg(feature = "cache-compress")]
    {
        lz4_flex::decompress_size_prepended(payload).map_err(|error| LogParseError::InvalidCache {
            message: format!("cache lz4 decompress failed: {error}"),
        })
    }
    #[cfg(not(feature = "cache-compress"))]
    {
        let _ = payload;
        Err(LogParseError::InvalidCache {
            message: "cache is compressed but cache-compress feature is disabled".to_string(),
        })
    }
}

impl CacheState {
    /// SHA-256 of the bincode-encoded `CacheState`. Used as the
    /// `state_hash` field of the on-disk header.
    pub fn state_hash(&self) -> Result<[u8; 32], LogParseError> {
        let bytes =
            bincode::serde::encode_to_vec(self, bincode::config::standard()).map_err(|error| {
                LogParseError::InvalidCache {
                    message: format!("cache state hash encode failed: {error}"),
                }
            })?;
        let mut hasher = Sha256::new();
        hasher.update(&bytes);
        Ok(hasher.finalize().into())
    }

    /// Stamp `processed_line_count` and `input_identity` on a clone.
    pub fn with_cursor(
        mut self,
        processed_line_count: u64,
        input_identity: Option<[u8; 32]>,
    ) -> Self {
        self.processed_line_count = processed_line_count;
        self.input_identity = input_identity;
        self
    }

    /// Stamp the runtime config hash.
    pub fn with_runtime_hash(mut self, hash: [u8; 32]) -> Self {
        self.runtime_config_hash = hash;
        self
    }

    /// Stamp the preprocess plan hash.
    pub fn with_preprocess_hash(mut self, hash: [u8; 32]) -> Self {
        self.preprocess_plan_hash = hash;
        self
    }
}

/// Compute SHA-256 over the path bytes — used to identify the input file.
pub fn path_identity(path: &Path) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(path.to_string_lossy().as_bytes());
    hasher.finalize().into()
}

/// Compute a `PiplupConfig`'s runtime hash.
pub fn compute_runtime_config_hash(config: &PiplupConfig) -> [u8; 32] {
    Sha256::digest(config.runtime_hash_seed().as_bytes()).into()
}

/// Compute a `PreprocessPlan`'s content hash.
pub fn compute_preprocess_plan_hash(plan: &PreprocessPlan) -> [u8; 32] {
    plan.compute_content_hash()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cache::dto::{CacheCluster, CacheConstantBucket, CachePreprocessPlan};
    use crate::cache::format::SERIALIZED_LEN;
    use crate::config::PiplupConfig;
    use crate::preprocessor::{PreprocessPlan, RuleId};
    use std::collections::BTreeMap;

    fn sample_cache() -> CacheState {
        let config = PiplupConfig::default();
        let plan = PreprocessPlan::new(vec![RuleId::Url]);
        let runtime_hash = compute_runtime_config_hash(&config);
        let plan_hash = compute_preprocess_plan_hash(&plan);
        CacheState::new(
            vec![CacheCluster {
                log_template: vec![],
                log_ids: vec![1],
                path_list: vec![vec![]],
                template_list: vec!["<*>".into()],
                hit_time: 1,
                br_thresh: 5,
                placeholder_mask: vec![],
            }],
            BTreeMap::from([(
                crate::cache::dto::CacheConstantKey::None,
                CacheConstantBucket {
                    cluster_ids: vec![0],
                    by_len: BTreeMap::new(),
                },
            )]),
            vec![crate::cache::dto::CacheConstantKey::None],
            CachePreprocessPlan {
                enabled_rule_ids: vec![RuleId::Url],
            },
        )
        .with_cursor(0, None)
        .with_runtime_hash(runtime_hash)
        .with_preprocess_hash(plan_hash)
    }

    #[test]
    fn save_load_round_trip_via_bytes() {
        let cache = sample_cache();
        let mut buffer = Vec::new();
        CacheManager::new()
            .save(&mut buffer, &cache, false)
            .unwrap();
        let restored = CacheManager::load(&mut buffer.as_slice()).unwrap();
        assert_eq!(restored.clusters.len(), cache.clusters.len());
        assert_eq!(restored.runtime_config_hash, cache.runtime_config_hash);
    }

    #[test]
    fn corrupted_magic_is_invalid_cache() {
        let mut buffer = vec![0u8; SERIALIZED_LEN + 4];
        buffer[..8].copy_from_slice(b"WRONG!!!");
        let error = CacheManager::load(&mut buffer.as_slice()).unwrap_err();
        assert!(
            matches!(error, LogParseError::InvalidCache { .. }),
            "got {error:?}"
        );
    }

    #[test]
    fn truncated_payload_is_invalid_cache() {
        let cache = sample_cache();
        let mut buffer = Vec::new();
        CacheManager::new()
            .save(&mut buffer, &cache, false)
            .unwrap();
        buffer.truncate(SERIALIZED_LEN);
        let error = CacheManager::load(&mut buffer.as_slice()).unwrap_err();
        assert!(
            matches!(error, LogParseError::InvalidCache { .. }),
            "got {error:?}"
        );
    }

    #[test]
    fn save_load_via_temp_file_survives() {
        let temp = tempfile::tempdir().expect("tempdir");
        let path: PathBuf = temp.path().join("cache.bin");
        let cache = sample_cache();
        CacheManager::save_to_path(&path, &cache, false).expect("save should succeed");
        let restored = CacheManager::load_from_path(&path).expect("load should succeed");
        assert_eq!(restored.clusters.len(), cache.clusters.len());
    }

    #[test]
    fn state_hash_independent_of_hash_field_itself() {
        // state_hash is computed from the bincode-encoded DTO which
        // includes the hash field. Stamping the field with a different
        // value will change the state hash. The test is here as a
        // smoke test for the API.
        let cache = sample_cache();
        let hash1 = cache.state_hash().unwrap();
        let mutated = cache.with_runtime_hash([0xAB; 32]);
        let hash2 = mutated.state_hash().unwrap();
        assert_ne!(hash1, hash2);
    }

    #[test]
    fn wrong_runtime_hash_yields_incompatible() {
        let cache = sample_cache();
        let mut buffer = Vec::new();
        CacheManager::new()
            .save(&mut buffer, &cache, false)
            .unwrap();
        // Tamper with the runtime hash byte in the header.
        let offset = 25; // runtime_config_hash starts at byte 25
        buffer[offset] ^= 0x01;
        let error = CacheManager::load(&mut buffer.as_slice()).unwrap_err();
        assert!(matches!(error, LogParseError::IncompatibleCacheRuntime));
    }

    #[test]
    fn wrong_preprocess_hash_yields_incompatible() {
        let cache = sample_cache();
        let mut buffer = Vec::new();
        CacheManager::new()
            .save(&mut buffer, &cache, false)
            .unwrap();
        let offset = 57; // preprocess_plan_hash starts at byte 57
        buffer[offset] ^= 0x01;
        let error = CacheManager::load(&mut buffer.as_slice()).unwrap_err();
        assert!(matches!(error, LogParseError::IncompatibleCachePlan));
    }
}

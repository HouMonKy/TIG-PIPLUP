//! Drain3 checkpoint cache.
//!
//! This format is deliberately independent from PIPLUP's `CacheState`: it has
//! its own magic bytes and persists Drain3 clusters plus their immutable route
//! paths so restore can rebuild the tree without rerouting final templates.

use std::fs;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tempfile::NamedTempFile;

use crate::cache::FLAG_LZ4_COMPRESSED;
use crate::drain3::config::Drain3Config;
use crate::drain3::state::{DrainCluster, DrainKey, DrainState};
use crate::error::LogParseError;
use crate::model::{Token, token::render_token_list};
use crate::placeholder::{PlaceholderKind, PlaceholderMask};
use crate::preprocessor::{PreprocessPlan, RuleId};

pub const DRAIN3_CACHE_MAGIC: [u8; 8] = *b"DRNCCH01";
pub const DRAIN3_CACHE_FORMAT_VERSION: u32 = 2;
pub const DRAIN3_CACHE_HEADER_LEN: usize = 8 + 4 + 4 + 1 + 8 + 32 + 32 + 32;

const KNOWN_FLAGS: u8 = FLAG_LZ4_COMPRESSED;
const MAX_PAYLOAD_BYTES: usize = 256 * 1024 * 1024;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Drain3CacheHeader {
    pub format_version: u32,
    pub flags: u8,
    pub payload_len: u64,
    pub runtime_config_hash: [u8; 32],
    pub preprocess_plan_hash: [u8; 32],
    pub state_hash: [u8; 32],
}

impl Drain3CacheHeader {
    pub fn new(
        flags: u8,
        payload_len: u64,
        runtime_config_hash: [u8; 32],
        preprocess_plan_hash: [u8; 32],
        state_hash: [u8; 32],
    ) -> Self {
        Self {
            format_version: DRAIN3_CACHE_FORMAT_VERSION,
            flags,
            payload_len,
            runtime_config_hash,
            preprocess_plan_hash,
            state_hash,
        }
    }

    pub fn is_compressed(&self) -> bool {
        self.flags & FLAG_LZ4_COMPRESSED != 0
    }

    pub fn write<W: Write>(&self, writer: &mut W) -> Result<usize, LogParseError> {
        if self.payload_len == 0 {
            return Err(LogParseError::InvalidCache {
                message: "payload_len must be non-zero".to_string(),
            });
        }

        let mut buffer = [0u8; DRAIN3_CACHE_HEADER_LEN];
        buffer[..8].copy_from_slice(&DRAIN3_CACHE_MAGIC);
        buffer[8..12].copy_from_slice(&self.format_version.to_le_bytes());
        buffer[12..16].copy_from_slice(&(DRAIN3_CACHE_HEADER_LEN as u32).to_le_bytes());
        buffer[16] = self.flags;
        buffer[17..25].copy_from_slice(&self.payload_len.to_le_bytes());
        buffer[25..57].copy_from_slice(&self.runtime_config_hash);
        buffer[57..89].copy_from_slice(&self.preprocess_plan_hash);
        buffer[89..121].copy_from_slice(&self.state_hash);

        writer.write_all(&buffer)?;
        Ok(DRAIN3_CACHE_HEADER_LEN)
    }

    pub fn read<R: Read>(reader: &mut R) -> Result<Self, LogParseError> {
        let mut buffer = [0u8; DRAIN3_CACHE_HEADER_LEN];
        match reader.read_exact(&mut buffer) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::UnexpectedEof => {
                return Err(LogParseError::InvalidCache {
                    message: "drain3 cache file is truncated (header)".to_string(),
                });
            }
            Err(error) => return Err(LogParseError::Io(error)),
        }

        if buffer[..8] != DRAIN3_CACHE_MAGIC {
            return Err(LogParseError::InvalidCache {
                message: "drain3 cache magic mismatch".to_string(),
            });
        }

        let format_version = u32::from_le_bytes(buffer[8..12].try_into().map_err(|_| {
            LogParseError::InvalidCache {
                message: "drain3 cache header format_version field is truncated".to_string(),
            }
        })?);
        if format_version != DRAIN3_CACHE_FORMAT_VERSION {
            return Err(LogParseError::InvalidCache {
                message: format!(
                    "drain3 cache format version {format_version} does not match expected {DRAIN3_CACHE_FORMAT_VERSION}",
                ),
            });
        }

        let header_len = u32::from_le_bytes(buffer[12..16].try_into().map_err(|_| {
            LogParseError::InvalidCache {
                message: "drain3 cache header header_len field is truncated".to_string(),
            }
        })?);
        if header_len as usize != DRAIN3_CACHE_HEADER_LEN {
            return Err(LogParseError::InvalidCache {
                message: format!(
                    "drain3 cache header_len {header_len} does not match expected {DRAIN3_CACHE_HEADER_LEN}",
                ),
            });
        }

        let flags = buffer[16];
        if flags & !KNOWN_FLAGS != 0 {
            return Err(LogParseError::InvalidCache {
                message: format!("drain3 cache header has unknown flags set: 0x{flags:02x}"),
            });
        }

        let payload_len = u64::from_le_bytes(buffer[17..25].try_into().map_err(|_| {
            LogParseError::InvalidCache {
                message: "drain3 cache header payload_len field is truncated".to_string(),
            }
        })?);
        if payload_len == 0 {
            return Err(LogParseError::InvalidCache {
                message: "drain3 cache payload_len is zero".to_string(),
            });
        }

        let mut runtime_config_hash = [0u8; 32];
        runtime_config_hash.copy_from_slice(&buffer[25..57]);
        let mut preprocess_plan_hash = [0u8; 32];
        preprocess_plan_hash.copy_from_slice(&buffer[57..89]);
        let mut state_hash = [0u8; 32];
        state_hash.copy_from_slice(&buffer[89..121]);

        Ok(Self {
            format_version,
            flags,
            payload_len,
            runtime_config_hash,
            preprocess_plan_hash,
            state_hash,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Drain3CachePreprocessPlan {
    pub enabled_rule_ids: Vec<RuleId>,
}

impl From<&PreprocessPlan> for Drain3CachePreprocessPlan {
    fn from(plan: &PreprocessPlan) -> Self {
        Self {
            enabled_rule_ids: plan.enabled_rule_ids.clone(),
        }
    }
}

impl From<Drain3CachePreprocessPlan> for PreprocessPlan {
    fn from(plan: Drain3CachePreprocessPlan) -> Self {
        Self::new(plan.enabled_rule_ids)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub enum Drain3CacheRouteKey {
    TokenCount(u64),
    Literal(String),
    Wildcard,
    Placeholder(PlaceholderKind),
}

impl From<&DrainKey> for Drain3CacheRouteKey {
    fn from(key: &DrainKey) -> Self {
        match key {
            DrainKey::TokenCount(count) => Self::TokenCount(*count as u64),
            DrainKey::Literal(value) => Self::Literal(value.clone()),
            DrainKey::Wildcard => Self::Wildcard,
            DrainKey::Placeholder(kind) => Self::Placeholder(*kind),
        }
    }
}

impl TryFrom<Drain3CacheRouteKey> for DrainKey {
    type Error = LogParseError;

    fn try_from(key: Drain3CacheRouteKey) -> Result<Self, Self::Error> {
        Ok(match key {
            Drain3CacheRouteKey::TokenCount(count) => {
                DrainKey::TokenCount(usize::try_from(count).map_err(|_| {
                    LogParseError::InvalidCache {
                        message: format!("drain3 cache route token count {count} overflows usize"),
                    }
                })?)
            }
            Drain3CacheRouteKey::Literal(value) => DrainKey::Literal(value),
            Drain3CacheRouteKey::Wildcard => DrainKey::Wildcard,
            Drain3CacheRouteKey::Placeholder(kind) => DrainKey::Placeholder(kind),
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Drain3CacheCluster {
    pub id: u64,
    pub route_path: Vec<Drain3CacheRouteKey>,
    pub template_tokens: Vec<String>,
    pub placeholder_mask: PlaceholderMask,
    pub log_ids: Vec<u64>,
    pub size: u64,
}

impl From<&DrainCluster> for Drain3CacheCluster {
    fn from(cluster: &DrainCluster) -> Self {
        Self {
            id: cluster.id as u64,
            route_path: cluster
                .route_path
                .iter()
                .map(Drain3CacheRouteKey::from)
                .collect(),
            template_tokens: cluster.log_template.clone(),
            placeholder_mask: cluster.placeholder_mask.clone(),
            log_ids: cluster.log_ids.iter().map(|id| *id as u64).collect(),
            size: cluster.size as u64,
        }
    }
}

impl TryFrom<Drain3CacheCluster> for DrainCluster {
    type Error = LogParseError;

    fn try_from(cluster: Drain3CacheCluster) -> Result<Self, Self::Error> {
        let id = usize::try_from(cluster.id).map_err(|_| LogParseError::InvalidCache {
            message: format!("drain3 cache cluster id {} overflows usize", cluster.id),
        })?;
        let route_path = cluster
            .route_path
            .into_iter()
            .map(DrainKey::try_from)
            .collect::<Result<Vec<_>, _>>()?;
        let log_ids = cluster
            .log_ids
            .into_iter()
            .map(|id| {
                usize::try_from(id).map_err(|_| LogParseError::InvalidCache {
                    message: format!("drain3 cache log id {id} overflows usize"),
                })
            })
            .collect::<Result<Vec<_>, _>>()?;
        let size = usize::try_from(cluster.size).map_err(|_| LogParseError::InvalidCache {
            message: format!("drain3 cache cluster size {} overflows usize", cluster.size),
        })?;

        let template_tokens = cluster
            .template_tokens
            .iter()
            .map(Token::from_rendered_template)
            .collect::<Vec<_>>();
        validate_placeholder_mask(&template_tokens, &cluster.placeholder_mask)?;
        let log_template = render_token_list(&template_tokens);
        Ok(DrainCluster {
            id,
            log_template,
            template_tokens,
            route_path,
            log_ids,
            size,
            placeholder_mask: cluster.placeholder_mask,
            minhash_sig: Vec::new(),
        })
    }
}

fn validate_placeholder_mask(
    template_tokens: &[Token],
    placeholder_mask: &PlaceholderMask,
) -> Result<(), LogParseError> {
    if placeholder_mask.len() != template_tokens.len() {
        return Err(LogParseError::InvalidCache {
            message: format!(
                "drain3 cache placeholder mask len {} does not match template len {}",
                placeholder_mask.len(),
                template_tokens.len()
            ),
        });
    }

    for (index, (token, kind)) in template_tokens.iter().zip(placeholder_mask).enumerate() {
        if kind.is_some() && !matches!(token, Token::Wildcard { .. }) {
            return Err(LogParseError::InvalidCache {
                message: format!(
                    "drain3 cache placeholder mask position {index} is typed but template token is literal"
                ),
            });
        }
    }

    Ok(())
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[non_exhaustive]
pub struct Drain3CacheState {
    pub version: u32,
    pub clusters: Vec<Drain3CacheCluster>,
    pub preprocess_plan: Drain3CachePreprocessPlan,
    pub processed_line_count: u64,
    pub input_identity: [u8; 32],
    pub runtime_config_hash: [u8; 32],
    pub preprocess_plan_hash: [u8; 32],
}

impl Drain3CacheState {
    pub fn from_state(
        state: &DrainState,
        preprocess_plan: &PreprocessPlan,
        processed_line_count: u64,
        input_identity: [u8; 32],
        config: &Drain3Config,
    ) -> Self {
        Self {
            version: DRAIN3_CACHE_FORMAT_VERSION,
            clusters: state
                .clusters()
                .iter()
                .map(Drain3CacheCluster::from)
                .collect(),
            preprocess_plan: Drain3CachePreprocessPlan::from(preprocess_plan),
            processed_line_count,
            input_identity,
            runtime_config_hash: config.compute_runtime_hash(),
            preprocess_plan_hash: preprocess_plan.compute_content_hash(),
        }
    }

    pub fn with_runtime_hash(mut self, hash: [u8; 32]) -> Self {
        self.runtime_config_hash = hash;
        self
    }

    pub fn with_preprocess_hash(mut self, hash: [u8; 32]) -> Self {
        self.preprocess_plan_hash = hash;
        self
    }

    /// SHA-256 of the bincode-encoded cache state.
    pub fn state_hash(&self) -> Result<[u8; 32], LogParseError> {
        let bytes = encode_cache_state(self)?;
        Ok(Sha256::digest(&bytes).into())
    }

    pub fn into_restored_state(
        self,
        config: &Drain3Config,
        expected_input_identity: [u8; 32],
    ) -> Result<Drain3RestoredState, LogParseError> {
        if self.version != DRAIN3_CACHE_FORMAT_VERSION {
            return Err(LogParseError::InvalidCache {
                message: format!(
                    "drain3 cache state version {} does not match expected {DRAIN3_CACHE_FORMAT_VERSION}",
                    self.version
                ),
            });
        }

        if self.input_identity != expected_input_identity {
            return Err(LogParseError::InvalidCache {
                message: "drain3 cache input identity mismatch".to_string(),
            });
        }

        // These checks compare the cached state against the current runtime.
        // `load()` separately checks that header metadata matches the payload.
        if self.runtime_config_hash != config.compute_runtime_hash() {
            return Err(LogParseError::IncompatibleCacheRuntime);
        }

        let preprocess_plan: PreprocessPlan = self.preprocess_plan.into();
        if self.preprocess_plan_hash != preprocess_plan.compute_content_hash() {
            return Err(LogParseError::IncompatibleCachePlan);
        }

        let clusters = self
            .clusters
            .into_iter()
            .map(DrainCluster::try_from)
            .collect::<Result<Vec<_>, _>>()?;
        let state =
            DrainState::from_cached_clusters(clusters, config.depth).map_err(|message| {
                LogParseError::InvalidCache {
                    message: format!("drain3 cache route restore failed: {message}"),
                }
            })?;

        let processed_line_count = usize::try_from(self.processed_line_count).map_err(|_| {
            LogParseError::InvalidCache {
                message: format!(
                    "drain3 cache processed_line_count {} overflows usize",
                    self.processed_line_count
                ),
            }
        })?;

        Ok(Drain3RestoredState {
            state,
            preprocess_plan,
            processed_line_count,
            input_identity: expected_input_identity,
        })
    }
}

#[derive(Debug, Clone)]
pub struct Drain3RestoredState {
    pub state: DrainState,
    pub preprocess_plan: PreprocessPlan,
    pub processed_line_count: usize,
    pub input_identity: [u8; 32],
}

#[derive(Debug, Clone, Default)]
pub struct Drain3CacheManager;

impl Drain3CacheManager {
    pub fn new() -> Self {
        Self
    }

    pub fn load<R: Read>(reader: &mut R) -> Result<Drain3CacheState, LogParseError> {
        let header = Drain3CacheHeader::read(reader)?;
        let payload_len =
            usize::try_from(header.payload_len).map_err(|_| LogParseError::InvalidCache {
                message: "drain3 cache payload_len exceeds usize".to_string(),
            })?;
        if payload_len > MAX_PAYLOAD_BYTES {
            return Err(LogParseError::InvalidCache {
                message: "drain3 cache payload too large".to_string(),
            });
        }
        let mut payload = vec![0u8; payload_len];
        match reader.read_exact(&mut payload) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::UnexpectedEof => {
                return Err(LogParseError::InvalidCache {
                    message: "drain3 cache file is truncated (payload)".to_string(),
                });
            }
            Err(error) => return Err(LogParseError::Io(error)),
        }

        let uncompressed = decompress_payload(&payload, header.is_compressed())?;
        let computed_state_hash: [u8; 32] = Sha256::digest(&uncompressed).into();
        if computed_state_hash != header.state_hash {
            return Err(LogParseError::InvalidCache {
                message: "drain3 cache state hash mismatch".to_string(),
            });
        }

        let cache: Drain3CacheState =
            bincode::serde::decode_from_slice(&uncompressed, bincode::config::standard())
                .map_err(|error| LogParseError::InvalidCache {
                    message: format!("drain3 cache bincode decode failed: {error}"),
                })?
                .0;

        // Header-vs-payload checks are intentional: they validate file
        // self-consistency before `into_restored_state()` validates the cache
        // against the caller's current config and preprocess plan.
        if cache.runtime_config_hash != header.runtime_config_hash {
            return Err(LogParseError::IncompatibleCacheRuntime);
        }
        if cache.preprocess_plan_hash != header.preprocess_plan_hash {
            return Err(LogParseError::IncompatibleCachePlan);
        }
        if cache.version != DRAIN3_CACHE_FORMAT_VERSION {
            return Err(LogParseError::InvalidCache {
                message: format!(
                    "drain3 cache state version {} does not match expected {DRAIN3_CACHE_FORMAT_VERSION}",
                    cache.version
                ),
            });
        }

        Ok(cache)
    }

    pub fn save<W: Write>(
        &self,
        writer: &mut W,
        cache: &Drain3CacheState,
        compress: bool,
    ) -> Result<bool, LogParseError> {
        // Keep this API symmetric with PIPLUP's `CacheManager::save`: callers
        // can skip calling it when unchanged, and `true` means bytes were
        // actually written. Checkpoints use the narrower `Result<()>` below.
        write_payload(writer, cache, compress)?;
        Ok(true)
    }

    /// Save a checkpoint. The `cache` should already include the resume
    /// cursor and input identity.
    pub fn save_checkpoint<W: Write>(
        &self,
        writer: &mut W,
        cache: &Drain3CacheState,
        compress: bool,
    ) -> Result<(), LogParseError> {
        write_payload(writer, cache, compress)
    }

    pub fn load_from_path(path: &Path) -> Result<Drain3CacheState, LogParseError> {
        let mut file = fs::File::open(path).map_err(|error| LogParseError::InvalidCache {
            message: format!("failed to open drain3 cache {}: {error}", path.display()),
        })?;
        Self::load(&mut file)
    }

    pub fn save_to_path(
        path: &Path,
        cache: &Drain3CacheState,
        compress: bool,
    ) -> Result<bool, LogParseError> {
        let parent = path
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .map(Path::to_path_buf)
            .unwrap_or_else(|| PathBuf::from("."));
        fs::create_dir_all(&parent)?;
        let mut temp = NamedTempFile::new_in(&parent)?;
        Self::new().save(temp.as_file_mut(), cache, compress)?;
        match temp.persist(path) {
            Ok(_) => Ok(true),
            Err(error) => Err(LogParseError::Io(error.error)),
        }
    }

    pub fn save_checkpoint_to_path(
        path: &Path,
        cache: &Drain3CacheState,
        compress: bool,
    ) -> Result<(), LogParseError> {
        let parent = path
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .map(Path::to_path_buf)
            .unwrap_or_else(|| PathBuf::from("."));
        fs::create_dir_all(&parent)?;
        let mut temp = NamedTempFile::new_in(&parent)?;
        Self::new().save_checkpoint(temp.as_file_mut(), cache, compress)?;
        match temp.persist(path) {
            Ok(_) => Ok(()),
            Err(error) => Err(LogParseError::Io(error.error)),
        }
    }

    pub fn restore_from_path(
        path: &Path,
        config: &Drain3Config,
        expected_input_identity: [u8; 32],
        force_rebuild: bool,
    ) -> Result<Option<Drain3RestoredState>, LogParseError> {
        if force_rebuild || !path.exists() {
            return Ok(None);
        }

        let cache = Self::load_from_path(path)?;
        cache
            .into_restored_state(config, expected_input_identity)
            .map(Some)
    }
}

fn write_payload<W: Write>(
    writer: &mut W,
    cache: &Drain3CacheState,
    compress: bool,
) -> Result<(), LogParseError> {
    let uncompressed = encode_cache_state(cache)?;
    let state_hash: [u8; 32] = Sha256::digest(&uncompressed).into();
    let payload = encode_payload(&uncompressed, compress)?;
    let flags = if compress { FLAG_LZ4_COMPRESSED } else { 0 };
    let header = Drain3CacheHeader::new(
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

fn encode_cache_state(cache: &Drain3CacheState) -> Result<Vec<u8>, LogParseError> {
    bincode::serde::encode_to_vec(cache, bincode::config::standard()).map_err(|error| {
        LogParseError::InvalidCache {
            message: format!("drain3 cache bincode encode failed: {error}"),
        }
    })
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
            message: format!("drain3 cache lz4 decompress failed: {error}"),
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

pub fn drain3_path_identity(path: &Path) -> [u8; 32] {
    let canonical = path.canonicalize().unwrap_or_else(|_| path.to_path_buf());
    let mut hasher = Sha256::new();
    hasher.update(b"drain3-input-v1\0");
    hasher.update(canonical.to_string_lossy().as_bytes());
    hasher.finalize().into()
}

/// Compute the hash-based cache file ID for a single Drain3 file cache entry.
///
/// ADR-0028 specifies:
/// ```text
/// cache_file_id = SHA-256(
///   "drain3-cache-file-v1\0" +
///   canonical_input_root_bytes + "\0" +
///   canonical_input_file_path_bytes
/// )
/// ```
///
/// Returns the lowercase hex encoding of the hash.
pub fn drain3_cache_file_id(input_root: &Path, input_file: &Path) -> String {
    let canonical_root = input_root
        .canonicalize()
        .unwrap_or_else(|_| input_root.to_path_buf());
    let canonical_file = input_file
        .canonicalize()
        .unwrap_or_else(|_| input_file.to_path_buf());
    let mut hasher = Sha256::new();
    hasher.update(b"drain3-cache-file-v1\0");
    hasher.update(canonical_root.to_string_lossy().as_bytes());
    hasher.update(b"\0");
    hasher.update(canonical_file.to_string_lossy().as_bytes());
    let hash: [u8; 32] = hasher.finalize().into();
    encode_hex(hash)
}

/// Compute the hash-based cache file ID for a Drain3 leaf directory cache.
///
/// For directory input, training is per-leaf (all `.log` files in a leaf
/// directory are concatenated).  The cache identity covers the entire leaf,
/// not a single representative file.  All file paths must be sorted before
/// calling this function so the hash is stable regardless of filesystem
/// enumeration order.
///
/// ```text
/// cache_file_id = SHA-256(
///   "drain3-leaf-cache-v1\0" +
///   canonical_input_root_bytes +
///   ("\0" + canonical_file_path_bytes)*
/// )
/// ```
pub fn drain3_leaf_cache_file_id(input_root: &Path, leaf_files: &[&Path]) -> String {
    let canonical_root = input_root
        .canonicalize()
        .unwrap_or_else(|_| input_root.to_path_buf());
    let mut hasher = Sha256::new();
    hasher.update(b"drain3-leaf-cache-v1\0");
    hasher.update(canonical_root.to_string_lossy().as_bytes());
    for file in leaf_files {
        let canonical_file = file.canonicalize().unwrap_or_else(|_| file.to_path_buf());
        hasher.update(b"\0");
        hasher.update(canonical_file.to_string_lossy().as_bytes());
    }
    let hash: [u8; 32] = hasher.finalize().into();
    encode_hex(hash)
}

fn encode_hex(hash: [u8; 32]) -> String {
    let mut hex = String::with_capacity(64);
    for byte in hash {
        hex.push_str(&format!("{byte:02x}"));
    }
    hex
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::drain3::state::DrainKey;
    use crate::placeholder::PlaceholderKind;

    fn config() -> Drain3Config {
        Drain3Config {
            log_format: vec!["<Date> <Time> <Level> <Content>".to_string()],
            preprocess: false,
            ..Drain3Config::default()
        }
    }

    fn identity() -> [u8; 32] {
        [0x42; 32]
    }

    fn sample_state() -> (DrainState, PreprocessPlan) {
        let mut state = DrainState::new();
        let tokens = vec!["worker".to_string(), "started".to_string()];
        let path = DrainKey::route_path_from_tokens(&tokens, 4, true, false);
        state.add_cluster_for_line(tokens, path, 1);
        (state, PreprocessPlan::default())
    }

    fn sample_cache() -> Drain3CacheState {
        let (state, plan) = sample_state();
        Drain3CacheState::from_state(&state, &plan, 1, identity(), &config())
    }

    #[test]
    fn header_uses_drain3_magic() {
        let header = Drain3CacheHeader::new(0, 10, [1; 32], [2; 32], [3; 32]);
        let mut bytes = Vec::new();
        header.write(&mut bytes).unwrap();
        assert_eq!(&bytes[..8], b"DRNCCH01");

        let read = Drain3CacheHeader::read(&mut bytes.as_slice()).unwrap();
        assert_eq!(read, header);
    }

    #[test]
    fn wrong_magic_is_invalid_cache() {
        let mut bytes = vec![0u8; DRAIN3_CACHE_HEADER_LEN + 1];
        bytes[..8].copy_from_slice(b"PPLCCH01");
        let error = Drain3CacheManager::load(&mut bytes.as_slice()).unwrap_err();
        assert!(matches!(error, LogParseError::InvalidCache { .. }));
    }

    #[test]
    fn wrong_version_is_invalid_cache() {
        let mut bytes = Vec::new();
        Drain3CacheHeader::new(0, 1, [1; 32], [2; 32], [3; 32])
            .write(&mut bytes)
            .unwrap();
        bytes[8..12].copy_from_slice(&99u32.to_le_bytes());

        let error = Drain3CacheHeader::read(&mut bytes.as_slice()).unwrap_err();
        assert!(matches!(error, LogParseError::InvalidCache { .. }));
    }

    #[test]
    fn save_load_round_trip_restores_tree_from_route_path() {
        let cache = sample_cache();
        let mut bytes = Vec::new();
        Drain3CacheManager::new()
            .save(&mut bytes, &cache, false)
            .unwrap();

        let restored_cache = Drain3CacheManager::load(&mut bytes.as_slice()).unwrap();
        let restored = restored_cache
            .into_restored_state(&config(), identity())
            .unwrap();

        assert_eq!(restored.processed_line_count, 1);
        assert_eq!(restored.state.cluster_count(), 1);
        assert_eq!(
            restored.state.cluster(0).route_path,
            vec![DrainKey::TokenCount(2), DrainKey::Literal("worker".into())]
        );
    }

    #[test]
    fn route_key_round_trips_placeholder_kind() {
        let key = DrainKey::Placeholder(PlaceholderKind::Number);
        let cached = Drain3CacheRouteKey::from(&key);
        assert_eq!(
            cached,
            Drain3CacheRouteKey::Placeholder(PlaceholderKind::Number)
        );
        let restored = DrainKey::try_from(cached).unwrap();
        assert_eq!(restored, key);
    }

    #[test]
    fn old_state_version_is_invalid_cache() {
        let mut cache = sample_cache();
        cache.version = DRAIN3_CACHE_FORMAT_VERSION - 1;
        let mut bytes = Vec::new();
        Drain3CacheManager::new()
            .save(&mut bytes, &cache, false)
            .unwrap();

        let error = Drain3CacheManager::load(&mut bytes.as_slice()).unwrap_err();
        assert!(matches!(error, LogParseError::InvalidCache { .. }));
    }

    #[test]
    fn placeholder_mask_length_mismatch_is_invalid_cache() {
        let mut cache = sample_cache();
        cache.clusters[0]
            .placeholder_mask
            .push(Some(PlaceholderKind::Number));
        let error = cache
            .into_restored_state(&config(), identity())
            .unwrap_err();
        assert!(matches!(error, LogParseError::InvalidCache { .. }));
    }

    #[test]
    fn typed_mask_on_literal_token_is_invalid_cache() {
        let mut cache = sample_cache();
        cache.clusters[0].placeholder_mask[0] = Some(PlaceholderKind::Number);
        let error = cache
            .into_restored_state(&config(), identity())
            .unwrap_err();
        assert!(matches!(error, LogParseError::InvalidCache { .. }));
    }

    #[test]
    fn state_hash_mismatch_is_invalid_cache() {
        let cache = sample_cache();
        let mut bytes = Vec::new();
        Drain3CacheManager::new()
            .save(&mut bytes, &cache, false)
            .unwrap();
        let last = bytes.last_mut().unwrap();
        *last ^= 0x01;

        let error = Drain3CacheManager::load(&mut bytes.as_slice()).unwrap_err();
        assert!(matches!(error, LogParseError::InvalidCache { .. }));
    }

    #[test]
    fn runtime_hash_mismatch_is_incompatible_runtime() {
        let cache = sample_cache();
        let mut bytes = Vec::new();
        Drain3CacheManager::new()
            .save(&mut bytes, &cache, false)
            .unwrap();
        bytes[25] ^= 0x01;

        let error = Drain3CacheManager::load(&mut bytes.as_slice()).unwrap_err();
        assert!(matches!(error, LogParseError::IncompatibleCacheRuntime));
    }

    #[test]
    fn runtime_config_mismatch_is_incompatible_runtime() {
        let cache = sample_cache();
        let mut mismatched = config();
        mismatched.max_children += 1;

        let error = cache
            .into_restored_state(&mismatched, identity())
            .unwrap_err();
        assert!(matches!(error, LogParseError::IncompatibleCacheRuntime));
    }

    #[test]
    fn preprocess_hash_mismatch_is_incompatible_plan() {
        let cache = sample_cache();
        let mut bytes = Vec::new();
        Drain3CacheManager::new()
            .save(&mut bytes, &cache, false)
            .unwrap();
        bytes[57] ^= 0x01;

        let error = Drain3CacheManager::load(&mut bytes.as_slice()).unwrap_err();
        assert!(matches!(error, LogParseError::IncompatibleCachePlan));
    }

    #[test]
    fn preprocess_plan_content_mismatch_is_incompatible_plan() {
        let mut cache = sample_cache();
        cache
            .preprocess_plan
            .enabled_rule_ids
            .push(crate::preprocessor::RuleId::Url);

        let error = cache
            .into_restored_state(&config(), identity())
            .unwrap_err();
        assert!(matches!(error, LogParseError::IncompatibleCachePlan));
    }

    #[test]
    fn input_identity_mismatch_is_invalid_cache() {
        let cache = sample_cache();
        let error = cache
            .into_restored_state(&config(), [0x99; 32])
            .unwrap_err();
        assert!(matches!(error, LogParseError::InvalidCache { .. }));
    }

    #[test]
    fn invalid_route_path_is_invalid_cache() {
        let mut cache = sample_cache();
        cache.clusters[0].route_path = vec![Drain3CacheRouteKey::Literal("worker".into())];
        let error = cache
            .into_restored_state(&config(), identity())
            .unwrap_err();
        assert!(matches!(error, LogParseError::InvalidCache { .. }));
    }

    #[test]
    fn preprocess_plan_hash_uses_only_plan_content() {
        let (state, plan) = sample_state();
        let mut cfg_a = config();
        let mut cfg_b = config();
        cfg_b.depth = 6;

        let cache_a = Drain3CacheState::from_state(&state, &plan, 1, identity(), &cfg_a);
        let cache_b = Drain3CacheState::from_state(&state, &plan, 1, identity(), &cfg_b);

        assert_ne!(cache_a.runtime_config_hash, cache_b.runtime_config_hash);
        assert_eq!(cache_a.preprocess_plan_hash, cache_b.preprocess_plan_hash);
        assert_eq!(cache_a.preprocess_plan_hash, plan.compute_content_hash());

        cfg_a.parametrize_numeric_tokens = false;
        let cache_c = Drain3CacheState::from_state(&state, &plan, 1, identity(), &cfg_a);
        assert_eq!(cache_a.preprocess_plan_hash, cache_c.preprocess_plan_hash);
    }

    #[test]
    fn force_rebuild_does_not_read_corrupt_payload() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("drain3.cache");
        fs::write(&path, b"not a cache").unwrap();

        let restored =
            Drain3CacheManager::restore_from_path(&path, &config(), identity(), true).unwrap();
        assert!(restored.is_none());
    }

    #[cfg(feature = "cache-compress")]
    #[test]
    fn compressed_cache_round_trips() {
        let cache = sample_cache();
        let mut bytes = Vec::new();
        Drain3CacheManager::new()
            .save(&mut bytes, &cache, true)
            .unwrap();
        assert_ne!(bytes[DRAIN3_CACHE_HEADER_LEN], 0);

        let restored = Drain3CacheManager::load(&mut bytes.as_slice()).unwrap();
        assert_eq!(restored.clusters.len(), 1);
    }

    #[cfg(not(feature = "cache-compress"))]
    #[test]
    fn compressed_save_requires_feature() {
        let cache = sample_cache();
        let mut bytes = Vec::new();
        let error = Drain3CacheManager::new()
            .save(&mut bytes, &cache, true)
            .unwrap_err();
        assert!(matches!(error, LogParseError::InvalidCache { .. }));
    }
}

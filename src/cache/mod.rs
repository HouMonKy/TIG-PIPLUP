//! piplup online cache.
//!
//! See [`dto`](self::dto) for the on-disk DTO layout, [`format`](self::format)
//! for the binary header, and [`manager`](self::manager) for the I/O API.

mod dto;
mod format;
mod manager;

pub use dto::{
    CACHE_STATE_VERSION, CacheCluster, CacheConstantBucket, CacheConstantKey, CachePreprocessPlan,
    CacheState, CacheStateError, CacheToken, RestoredState,
};
pub use format::{
    CACHE_FORMAT_VERSION, CACHE_MAGIC, CacheHeader, FLAG_LZ4_COMPRESSED, SERIALIZED_LEN,
};
pub use manager::{
    CacheManager, compute_preprocess_plan_hash, compute_runtime_config_hash, path_identity,
};

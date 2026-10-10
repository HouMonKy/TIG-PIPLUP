//! Drain3 fixed-depth tree clustering algorithm.
//!
//! This module implements the Drain3 log template learning algorithm as a peer
//! to the PIPLUP algorithm. Drain3 uses a fixed-depth parse tree to route log
//! lines to candidate clusters, then scores candidates by sequence similarity.
//!
//! See [ADR-0028](../../docs/adr/0028-drain3-learning-algorithm-and-checkpoint-cache.md)
//! for design decisions and parameter documentation.

/// Cache serialization, identity, and restore for Drain3 checkpoints.
pub mod cache;
/// Configuration for the Drain3 algorithm.
pub mod config;
/// Minhash signature for ADR-0033 改良③ variable-length clustering.
pub mod minhash;
/// Stateful Drain3 parser and read-only assignment pass.
pub mod parser;
/// Drain3 tree state, routing keys, clusters, and similarity functions.
pub mod state;
/// Token classification and normalization used by Drain3 routing.
pub mod token;

pub use cache::{
    DRAIN3_CACHE_FORMAT_VERSION, DRAIN3_CACHE_MAGIC, Drain3CacheCluster, Drain3CacheManager,
    Drain3CacheRouteKey, Drain3CacheState, Drain3RestoredState, drain3_cache_file_id,
    drain3_leaf_cache_file_id, drain3_path_identity,
};
pub use config::Drain3Config;
pub use parser::{Drain3InitialCacheState, Drain3Parser};
pub use state::{DrainCluster, DrainKey, DrainState};

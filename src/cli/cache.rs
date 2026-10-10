use std::{fs, path::Path};

use crate::{
    Drain3Config, Drain3Parser, PiplupConfig, PiplupParser,
    cache::{CacheManager, CacheState, path_identity},
    drain3::{Drain3CacheManager, Drain3CacheState, Drain3InitialCacheState, drain3_path_identity},
    engine::parser::InitialCacheState,
};

pub(super) fn finalize_cache(
    parser: &mut PiplupParser,
    cache_path: &Path,
    force_rebuild: &bool,
) -> anyhow::Result<()> {
    let Some(cache) = parser.take_cache_state() else {
        return Ok(());
    };
    save_online_cache(parser.config(), cache, cache_path)?;
    let _ = force_rebuild; // handled in apply_cache_config; accepted here for API symmetry
    Ok(())
}

/// Online warm-start save: keep learned templates but reset cursor to 0 and
/// clear input identity so the next online run re-processes all input.
/// Mirrors `save_drain3_online_cache` semantics.
pub(super) fn save_online_cache(
    config: &PiplupConfig,
    cache: CacheState,
    cache_path: &Path,
) -> anyhow::Result<()> {
    save_cache_from_parser_state(config, cache, cache_path, 0, None)?;
    eprintln!("wrote cache to {}", cache_path.display());
    Ok(())
}

pub(super) fn save_cache_from_parser_state(
    config: &PiplupConfig,
    cache: CacheState,
    cache_path: &Path,
    processed_line_count: u64,
    input_identity: Option<[u8; 32]>,
) -> anyhow::Result<()> {
    let restored = cache
        .into_parse_state()
        .map_err(crate::LogParseError::from)?;
    let final_cache = CacheState::from_parse_state(
        &restored.state,
        &restored.preprocess_plan,
        processed_line_count,
        input_identity,
    )
    .with_runtime_hash(config.compute_runtime_hash())
    .with_preprocess_hash(restored.preprocess_plan.compute_content_hash());
    CacheManager::save_to_path(cache_path, &final_cache, config.cache_compress)?;
    Ok(())
}

/// Train resume save: persist input identity and processed-line cursor so a
/// future train run resumes only if the input matches. Mirrors
/// `save_drain3_train_cache` semantics.
pub(super) fn save_train_cache(
    config: &PiplupConfig,
    input: &Path,
    processed_line_count: usize,
    cache: CacheState,
    cache_path: &Path,
) -> anyhow::Result<()> {
    save_cache_from_parser_state(
        config,
        cache,
        cache_path,
        processed_line_count as u64,
        Some(path_identity(input)),
    )?;
    eprintln!("wrote cache to {}", cache_path.display());
    Ok(())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum CacheInputPolicy {
    WarmStartOnMismatch,
    RequireMatch,
}

pub(super) struct CacheConfigArgs<'a> {
    pub(super) current_input: &'a Path,
    pub(super) cache_path: Option<&'a Path>,
    pub(super) segment_lines: Option<usize>,
    pub(super) checkpoint_lines: Option<usize>,
    pub(super) cache_compress: bool,
    pub(super) force_rebuild: bool,
    pub(super) input_policy: CacheInputPolicy,
}

/// Validate the cache-related CLI arguments shared by PIPLUP and Drain3.
/// Algorithm-neutral; field assignment stays in each `apply_*_cache_config`.
fn validate_cache_args(args: &CacheConfigArgs<'_>) -> Result<(), crate::LogParseError> {
    if args.segment_lines == Some(0) {
        return Err(crate::LogParseError::Unsupported(
            "--segment-lines must be > 0",
        ));
    }
    if args.checkpoint_lines == Some(0) {
        return Err(crate::LogParseError::Unsupported(
            "--checkpoint-lines must be > 0",
        ));
    }
    if args.cache_path.is_none() && args.checkpoint_lines.is_some() {
        return Err(crate::LogParseError::CacheCheckpointRequiresCache);
    }
    Ok(())
}

/// Why an existing cache file is being removed for a cold rebuild. Drives the
/// `eprintln` label in [`remove_cache_for_rebuild`]. Only covers the PIPLUP
/// reason-specific removal sites; the Drain3 force-rebuild fast path stays
/// inline (it does not classify a reason and prints no message).
#[derive(Debug, Clone, Copy)]
enum CacheRebuildReason {
    IncompatibleRuntime,
    InputIdentityMismatch,
    CorruptPayload,
}

impl CacheRebuildReason {
    fn label(self) -> &'static str {
        match self {
            CacheRebuildReason::IncompatibleRuntime => "incompatible",
            CacheRebuildReason::InputIdentityMismatch => "input mismatch",
            CacheRebuildReason::CorruptPayload => "corrupt",
        }
    }
}

/// Print the reason-specific rebuild message and remove the cache file.
/// Returns `io::Result` so strict callers propagate via `?` (converted to
/// `LogParseError` through `Io(#[from])`) and best-effort callers write
/// `let _ = remove_cache_for_rebuild(...)`.
fn remove_cache_for_rebuild(path: &Path, reason: CacheRebuildReason) -> std::io::Result<()> {
    eprintln!("cache {}, rebuilding: {}", reason.label(), path.display());
    fs::remove_file(path)
}

/// Shared user-facing error for "cache input identity does not match current
/// input" — used by both PIPLUP (`apply_cache_config`) and Drain3
/// (`apply_drain3_cache_config`) RequireMatch branches.
fn cache_identity_mismatch_error() -> crate::LogParseError {
    crate::LogParseError::Unsupported(
        "cache input identity does not match current input; use --force-rebuild-cache",
    )
}

pub(super) fn apply_cache_config(
    mut config: PiplupConfig,
    args: CacheConfigArgs<'_>,
) -> Result<(PiplupConfig, Option<InitialCacheState>), crate::LogParseError> {
    validate_cache_args(&args)?;
    // Segment_lines is independent of cache and may be set unconditionally.
    config.segment_lines = args.segment_lines;
    if let Some(path) = args.cache_path {
        if args.checkpoint_lines.is_some() {
            // cache_checkpoint_lines already validated > 0 above
            config.cache_checkpoint_lines = args.checkpoint_lines;
        }
        config.cache_path = Some(path.to_path_buf());
        config.cache_compress = args.cache_compress;
        if path.exists() {
            let cache = CacheManager::load_from_path(path);
            match cache {
                Ok(cache) => {
                    let runtime_hash = config.compute_runtime_hash();
                    if cache.runtime_config_hash != runtime_hash {
                        if args.force_rebuild {
                            remove_cache_for_rebuild(
                                path,
                                CacheRebuildReason::IncompatibleRuntime,
                            )?;
                            return Ok((config, None));
                        }
                        return Err(crate::LogParseError::IncompatibleCacheRuntime);
                    }
                    let restored = cache
                        .into_parse_state()
                        .map_err(crate::LogParseError::from)?;
                    let current_identity = path_identity(args.current_input);
                    let cursor = match restored.input_identity {
                        Some(identity) if identity == current_identity => {
                            restored.processed_line_count as usize
                        }
                        Some(_) | None if args.input_policy == CacheInputPolicy::RequireMatch => {
                            if args.force_rebuild {
                                remove_cache_for_rebuild(
                                    path,
                                    CacheRebuildReason::InputIdentityMismatch,
                                )?;
                                return Ok((config, None));
                            }
                            return Err(cache_identity_mismatch_error());
                        }
                        _ => 0,
                    };
                    return Ok((
                        config,
                        Some(InitialCacheState {
                            state: restored.state,
                            preprocess_plan: restored.preprocess_plan,
                            processed_line_count: cursor,
                            input_identity: Some(current_identity),
                        }),
                    ));
                }
                Err(err) => {
                    if args.force_rebuild {
                        let _ = remove_cache_for_rebuild(path, CacheRebuildReason::CorruptPayload);
                        return Ok((config, None));
                    }
                    return Err(err);
                }
            }
        }
    }
    Ok((config, None))
}

// ── Drain3 cache helpers ─────────────────────────────────────────────────

pub(super) fn finalize_drain3_cache(
    parser: &mut Drain3Parser,
    cache_path: &Path,
    force_rebuild: &bool,
) -> anyhow::Result<()> {
    let Some(cache) = parser.take_cache_state() else {
        return Ok(());
    };
    save_drain3_online_cache(parser.config(), cache, cache_path)?;
    let _ = force_rebuild; // handled in apply_drain3_cache_config
    Ok(())
}

pub(super) fn save_drain3_online_cache(
    config: &Drain3Config,
    cache: Drain3CacheState,
    cache_path: &Path,
) -> anyhow::Result<()> {
    // Online save uses warm-start semantics: keep clusters and tree
    // structure, but reset cursor to 0 and clear input identity so the
    // next run re-processes all lines while benefiting from learned
    // templates.  Mirrors PIPLUP's save_online_cache behavior.
    let preprocess_plan = crate::PreprocessPlan::from(cache.preprocess_plan.clone());
    let warm = Drain3CacheState {
        processed_line_count: 0,
        input_identity: [0u8; 32],
        runtime_config_hash: config.compute_runtime_hash(),
        preprocess_plan_hash: preprocess_plan.compute_content_hash(),
        ..cache
    };
    Drain3CacheManager::save_to_path(cache_path, &warm, config.cache_compress)?;
    eprintln!("wrote cache to {}", cache_path.display());
    Ok(())
}

pub(super) fn save_drain3_train_cache(
    config: &Drain3Config,
    input: &Path,
    cache: Drain3CacheState,
    cache_path: &Path,
) -> anyhow::Result<()> {
    // Stamp the input identity so restore can validate the cache matches the
    // current input file.  The `Drain3CacheState` passed in may already have
    // an identity from `from_state()`, but the caller might have overridden
    // the input path (e.g. for directory training where the display_input is
    // the leaf directory).  Always recompute from the canonical path.
    let identity = drain3_path_identity(input);
    let final_cache = Drain3CacheState {
        input_identity: identity,
        runtime_config_hash: config.compute_runtime_hash(),
        preprocess_plan_hash: cache.preprocess_plan_hash,
        ..cache
    };
    Drain3CacheManager::save_to_path(cache_path, &final_cache, config.cache_compress)?;
    eprintln!("wrote cache to {}", cache_path.display());
    Ok(())
}

pub(super) fn apply_drain3_cache_config(
    mut config: Drain3Config,
    args: CacheConfigArgs<'_>,
) -> Result<(Drain3Config, Option<Drain3InitialCacheState>), crate::LogParseError> {
    validate_cache_args(&args)?;
    config.segment_lines = args.segment_lines;
    if let Some(path) = args.cache_path {
        if args.checkpoint_lines.is_some() {
            config.cache_checkpoint_lines = args.checkpoint_lines;
        }
        config.cache_path = Some(path.to_path_buf());
        config.cache_compress = args.cache_compress;
        if path.exists() {
            let identity = drain3_path_identity(args.current_input);
            // Fast path: force_rebuild skips the cache entirely.
            if args.force_rebuild {
                let _ = fs::remove_file(path);
                return Ok((config, None));
            }
            let cache = Drain3CacheManager::load_from_path(path)?;
            // Try restoring with the current input identity.
            match cache.clone().into_restored_state(&config, identity) {
                Ok(restored) => {
                    return Ok((config, Some(restored.into())));
                }
                Err(crate::LogParseError::InvalidCache { ref message })
                    if message.contains("input identity mismatch") =>
                {
                    match args.input_policy {
                        CacheInputPolicy::WarmStartOnMismatch => {
                            // Warm-start: restore tree structure from the
                            // cache's own identity but reset cursor to 0.
                            // set_input_path() on the parser will overwrite
                            // the identity with the current input.
                            let cache_identity = cache.input_identity;
                            let restored = cache.into_restored_state(&config, cache_identity)?;
                            let mut initial: Drain3InitialCacheState = restored.into();
                            initial.processed_line_count = 0;
                            return Ok((config, Some(initial)));
                        }
                        CacheInputPolicy::RequireMatch => {
                            return Err(cache_identity_mismatch_error());
                        }
                    }
                }
                Err(crate::LogParseError::IncompatibleCacheRuntime) => {
                    return Err(crate::LogParseError::IncompatibleCacheRuntime);
                }
                Err(err) => {
                    return Err(err);
                }
            }
        }
    }
    Ok((config, None))
}

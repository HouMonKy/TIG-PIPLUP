use std::{
    io::{self, IsTerminal},
    path::{Path, PathBuf},
    time::{Duration, Instant},
};

use anyhow::{Context, Result};

use crate::{
    Drain3Config, Drain3Parser, LogEntry, PiplupConfig, PiplupParser,
    cache::CacheState,
    index::{IndexBuilder, TemplateIndexFile},
    progress::ProgressReporter,
};

use super::{
    INDEX_NAME, TEMPLATES_NAME,
    args::{Algorithm, TrainArgs, cli_error},
    cache::{
        CacheConfigArgs, CacheInputPolicy, apply_cache_config, apply_drain3_cache_config,
        save_drain3_train_cache, save_train_cache,
    },
    config::{
        Drain3ConfigOverrides, build_drain3_config, build_training_config,
        piplup_overrides_from_train, shared_config_flags_from_train,
    },
    input::{build_input_plan, group_files_by_leaf, read_file_entries},
    output::{
        DirectoryTrainingArtifactWriter, algorithm_suffix, output_dir_name_with_suffix,
        resolve_cache_leaf_path, resolve_drain3_leaf_cache_path, write_template_records,
    },
    summary::{format_duration, print_build_index_directory_summary},
    types::{BuildIndexResult, DirectoryIndexBuildStats, InputPlan},
};

pub(super) fn run_train(args: TrainArgs) -> anyhow::Result<()> {
    let stderr = io::stderr();
    let mode = args.progress.resolve(args.quiet, stderr.is_terminal());
    let mut progress = ProgressReporter::new(stderr, mode);
    run_train_with_progress(args, &mut progress)
}

pub(super) fn run_train_with_progress<W: io::Write>(
    args: TrainArgs,
    progress: &mut ProgressReporter<W>,
) -> anyhow::Result<()> {
    let input_plan = build_input_plan(&args.input)?;
    match input_plan {
        InputPlan::Stdin => Err(cli_error("train does not support stdin input").into()),
        InputPlan::File(input) => {
            if args.cache_dir.is_some() {
                return Err(cli_error("--cache-dir is only supported for directory input").into());
            }
            let output_dir = args.out_dir.clone().unwrap_or_else(|| {
                let suffix = algorithm_suffix(args.algorithm);
                let parent = input.parent().unwrap_or_else(|| Path::new("."));
                parent.join(output_dir_name_with_suffix(&input, suffix))
            });
            let templates_idx = args
                .index_out
                .clone()
                .unwrap_or_else(|| output_dir.join(INDEX_NAME));
            let templates_csv = if args.index_out.is_some() {
                templates_idx
                    .parent()
                    .map(|parent| parent.join(TEMPLATES_NAME))
                    .unwrap_or_else(|| PathBuf::from(TEMPLATES_NAME))
            } else {
                output_dir.join(TEMPLATES_NAME)
            };

            let result = train_for_entries(
                &args,
                &input,
                &read_file_entries(&input)?,
                args.emit_index.then_some(templates_idx.as_path()),
                &templates_csv,
                args.cache.clone(),
                progress,
            )?;

            if !args.quiet {
                if result.resume_line_count > 0 {
                    let new_lines = result.line_count.saturating_sub(result.resume_line_count);
                    eprintln!(
                        "trained {} templates from {} lines ({} restored, {} new) in {}",
                        result.template_count,
                        result.line_count,
                        result.resume_line_count,
                        new_lines,
                        format_duration(result.duration)
                    );
                } else {
                    eprintln!(
                        "trained {} templates from {} lines in {}",
                        result.template_count,
                        result.line_count,
                        format_duration(result.duration)
                    );
                }
                if args.emit_index {
                    eprintln!("wrote template index to {}", templates_idx.display());
                }
                eprintln!("wrote template summary to {}", templates_csv.display());
            }

            Ok(())
        }
        InputPlan::Directory { root, files } => {
            if args.index_out.is_some() {
                return Err(cli_error(
                    "train directory input does not support --index-out; use --out-dir to mirror leaf indexes",
                )
                .into());
            }
            if args.cache.is_some() {
                return Err(cli_error(
                    "train directory input does not support --cache; use --cache-dir",
                )
                .into());
            }

            let stats = run_train_directory_loop(&args, &root, &files, progress)?;
            if !args.quiet {
                print_build_index_directory_summary(&stats, &root, args.out_dir.as_deref());
            }
            Ok(())
        }
    }
}

pub(super) fn train_for_entries<W: io::Write>(
    args: &TrainArgs,
    input: &Path,
    entries: &[LogEntry],
    templates_idx: Option<&Path>,
    templates_csv: &Path,
    cache_path: Option<PathBuf>,
    progress: &mut ProgressReporter<W>,
) -> anyhow::Result<BuildIndexResult> {
    let config_output_dir = templates_idx
        .and_then(Path::parent)
        .map(Path::to_path_buf)
        .or_else(|| templates_csv.parent().map(Path::to_path_buf))
        .unwrap_or_else(|| PathBuf::from("."));

    let (training_output, index_opt, resume_cursor) = train_for_entries_core(
        args,
        input,
        entries,
        config_output_dir,
        cache_path,
        progress,
    )?;

    if let Some(templates_idx) = templates_idx
        && let Some(index) = &index_opt
    {
        write_index_file(templates_idx, index, progress)?;
    }
    write_template_records(&training_output.parse_output.events, templates_csv)?;

    Ok(BuildIndexResult {
        line_count: training_output.parse_output.line_results.len(),
        template_count: training_output.parse_output.template_count(),
        duration: training_output.parse_output.duration,
        resume_line_count: resume_cursor,
    })
}

/// Shared leaf training skeleton: config + cache apply + parser construction,
/// the training pass, cache rebuild/persist, and (when `args.emit_index`) index
/// construction. Excludes artifact writes — the caller (single-file
/// [`train_for_entries`] or the directory writer's `record_leaf`) materializes
/// the index file and `templates.csv`. This keeps a single training+build path
/// for both single-file and directory train (no shotgun surgery between two
/// artifact-write sites).
///
/// Returns the training output, the caller-built index (only when
/// `args.emit_index`), and the resume cursor for stat accumulation.
fn train_for_entries_core<W: io::Write>(
    args: &TrainArgs,
    input: &Path,
    entries: &[LogEntry],
    config_output_dir: PathBuf,
    cache_path: Option<PathBuf>,
    progress: &mut ProgressReporter<W>,
) -> anyhow::Result<(crate::TrainingOutput, Option<TemplateIndexFile>, usize)> {
    // Algorithm-specific config + cache apply + parser construction. The two
    // arms keep the type-specific builders (config struct, cache-apply fn,
    // parser type) fully visible; everything below is the shared leaf skeleton.
    progress.status(format!("training on {}", input.display()))?;
    let (mut runner, resume_cursor) = match args.algorithm {
        Algorithm::Piplup => {
            let config = build_training_config(
                &args.log_format,
                input,
                config_output_dir.clone(),
                piplup_overrides_from_train(args),
                shared_config_flags_from_train(args),
            );
            let (config, initial_state) = apply_cache_config(
                config,
                cache_config_args(args, input, cache_path.as_deref()),
            )?;
            let resume_cursor = initial_state
                .as_ref()
                .map(|state| state.processed_line_count)
                .unwrap_or(0);
            let mut parser = if let Some(initial) = initial_state {
                PiplupParser::with_cache(config.clone(), Some(initial))?
            } else {
                PiplupParser::new(config.clone())?
            };
            parser.set_input_path(input);
            (CliTrainingRunner::Piplup { config, parser }, resume_cursor)
        }
        Algorithm::Drain3 => {
            let config = build_drain3_config(
                &args.log_format,
                input,
                config_output_dir.clone(),
                Drain3ConfigOverrides {
                    depth: args.depth,
                    max_children: args.max_children,
                    sim_thresh: args.drain3_sim_thresh,
                    parametrize_numeric_tokens: !args.no_parametrize_numeric_tokens,
                },
                shared_config_flags_from_train(args),
            );
            let (config, initial_state) = apply_drain3_cache_config(
                config,
                cache_config_args(args, input, cache_path.as_deref()),
            )?;
            let resume_cursor = initial_state
                .as_ref()
                .map(|state| state.processed_line_count)
                .unwrap_or(0);
            let mut parser = if let Some(initial) = initial_state {
                Drain3Parser::with_cache(config.clone(), Some(initial))?
            } else {
                Drain3Parser::new(config.clone())?
            };
            parser.set_input_path(input);
            (CliTrainingRunner::Drain3 { config, parser }, resume_cursor)
        }
    };

    let training_output = runner.parse_lines_for_training(entries)?;
    let training_output = runner.rebuild_and_save_cache(
        resume_cursor,
        entries,
        training_output,
        input,
        entries.len(),
        cache_path.as_deref(),
    )?;

    let index_opt = if args.emit_index {
        progress.status("building template index")?;
        Some(runner.build_index(&training_output)?)
    } else {
        None
    };

    Ok((training_output, index_opt, resume_cursor))
}

/// Holds the algorithm-specific `(config, parser)` pair so the shared
/// `train_for_entries` leaf skeleton can drive both algorithms through one
/// code path. Mirrors the online-side `CliOnlineParser`: a closed enum with
/// match-based methods, NOT a trait — every branch stays visible in one place
/// (no dynamic dispatch, no hidden hooks). The cache-state types
/// (`CacheState` vs `Drain3CacheState`) genuinely differ, so resume-rebuild
/// and cache-save live together in [`rebuild_and_save_cache`] rather than
/// behind a uniform cache type.
enum CliTrainingRunner {
    Piplup {
        config: PiplupConfig,
        parser: PiplupParser,
    },
    Drain3 {
        config: Drain3Config,
        parser: Drain3Parser,
    },
}

impl CliTrainingRunner {
    fn parse_lines_for_training(
        &mut self,
        entries: &[LogEntry],
    ) -> anyhow::Result<crate::TrainingOutput> {
        Ok(match self {
            CliTrainingRunner::Piplup { parser, .. } => parser.parse_lines_for_training(entries)?,
            CliTrainingRunner::Drain3 { parser, .. } => parser.parse_lines_for_training(entries)?,
        })
    }

    /// Take the parser cache, optionally rebuild full artifacts on resume, then
    /// persist the cache. The arms differ in cache-state type and resume strategy
    /// (PIPLUP rebuilds from the cache via `build_output`; Drain3 re-parses
    /// read-only), so each arm finalizes its own cache type locally.
    fn rebuild_and_save_cache(
        &mut self,
        resume_cursor: usize,
        entries: &[LogEntry],
        training_output: crate::TrainingOutput,
        input: &Path,
        entries_len: usize,
        cache_path: Option<&Path>,
    ) -> anyhow::Result<crate::TrainingOutput> {
        match self {
            CliTrainingRunner::Piplup { config, parser } => {
                let final_cache = parser.take_cache_state();
                // Resumed training only returns line results for the unprocessed suffix.
                // Rebuilding from the final state and full input keeps artifacts complete.
                let training_output = if resume_cursor > 0 {
                    if let Some(cache) = final_cache.clone() {
                        rebuild_training_output_from_cache(
                            cache,
                            entries,
                            config,
                            training_output.parse_output.duration,
                        )?
                    } else {
                        training_output
                    }
                } else {
                    training_output
                };
                if let (Some(cache_path), Some(cache)) = (cache_path, final_cache) {
                    save_train_cache(config, input, entries_len, cache, cache_path)?;
                }
                Ok(training_output)
            }
            CliTrainingRunner::Drain3 { config, parser } => {
                let final_cache = parser.take_cache_state();
                // Drain3's parse_lines_for_training handles resume internally.
                // If resumed, rebuild full artifacts via a read-only pass.
                let training_output = if resume_cursor > 0 {
                    let rebuilt_parse_output =
                        parser.parse_lines_read_only(entries, &training_output.preprocess_plan)?;
                    crate::TrainingOutput {
                        parse_output: rebuilt_parse_output,
                        preprocess_plan: training_output.preprocess_plan,
                    }
                } else {
                    training_output
                };
                if let (Some(cache_path), Some(cache)) = (cache_path, final_cache) {
                    save_drain3_train_cache(config, input, cache, cache_path)?;
                }
                Ok(training_output)
            }
        }
    }

    fn build_index(
        &self,
        training_output: &crate::TrainingOutput,
    ) -> anyhow::Result<TemplateIndexFile> {
        Ok(match self {
            CliTrainingRunner::Piplup { config, .. } => {
                IndexBuilder::build(training_output, config)?
            }
            CliTrainingRunner::Drain3 { config, .. } => {
                IndexBuilder::build_drain3(training_output, config)?
            }
        })
    }
}

fn cache_config_args<'a>(
    args: &'a TrainArgs,
    input: &'a Path,
    cache_path: Option<&'a Path>,
) -> CacheConfigArgs<'a> {
    CacheConfigArgs {
        current_input: input,
        cache_path,
        segment_lines: args.segment_lines,
        checkpoint_lines: args.checkpoint_lines,
        cache_compress: args.cache_compress,
        force_rebuild: args.force_rebuild_cache,
        input_policy: CacheInputPolicy::RequireMatch,
    }
}

fn write_index_file<W: io::Write>(
    templates_idx: &Path,
    index: &TemplateIndexFile,
    progress: &mut ProgressReporter<W>,
) -> anyhow::Result<()> {
    progress.status("writing template index")?;
    super::output::write_template_index_file(templates_idx, index)
}

fn rebuild_training_output_from_cache(
    cache: CacheState,
    entries: &[LogEntry],
    config: &PiplupConfig,
    duration: Duration,
) -> anyhow::Result<crate::TrainingOutput> {
    let restored = cache
        .into_parse_state()
        .map_err(crate::LogParseError::from)?;
    let format = crate::MultiCompiledLogFormat::compile(&config.log_format)?;
    let parsed_lines = entries
        .iter()
        .filter_map(|entry| {
            crate::parse_core::parse_entry(&format, config.skip_unmatched, entry).transpose()
        })
        .collect::<Result<Vec<_>, _>>()?;
    let parse_output = crate::engine::output::build_output(
        parsed_lines,
        restored.state,
        config.keep_para,
        config.warn_miss,
        duration,
    )?;
    Ok(crate::TrainingOutput {
        parse_output,
        preprocess_plan: restored.preprocess_plan,
    })
}

fn run_train_directory_loop<W: io::Write>(
    args: &TrainArgs,
    root: &Path,
    files: &[PathBuf],
    progress: &mut ProgressReporter<W>,
) -> anyhow::Result<DirectoryIndexBuildStats> {
    // Start the clock BEFORE grouping so the reported duration covers grouping
    // + materialization (matches the coverage of the async path, which times
    // the whole async run).
    let started = Instant::now();
    let files_by_leaf = group_files_by_leaf(files);
    let leaf_dir_count = files_by_leaf.len();
    let mut writer = DirectoryTrainingArtifactWriter::new(started, leaf_dir_count, files.len());

    for (leaf_index, (leaf_dir, leaf_files)) in files_by_leaf.into_iter().enumerate() {
        let display_input = if leaf_files.len() == 1 {
            leaf_files[0].clone()
        } else {
            leaf_dir.clone()
        };
        progress.directory_file(leaf_index + 1, leaf_dir_count, &display_input)?;

        let mut entries = Vec::new();
        for file in &leaf_files {
            let mut file_entries = read_file_entries(file)?;
            let next_line = entries.len();
            for entry in &mut file_entries {
                entry.line_number += next_line;
            }
            entries.extend(file_entries);
        }

        // `config_output_dir` is the leaf's output base dir, used only to seed
        // the (currently vestigial, never-read) `output_dir` config field; it
        // does not affect index bytes, cache bytes, or templates.csv. Computed
        // here rather than inside the writer so the writer owns only artifact
        // path resolution + writes (single artifact-write path).
        let config_output_dir = match args.out_dir.as_ref() {
            Some(out) => {
                let relative = leaf_dir.strip_prefix(root).with_context(|| {
                    format!(
                        "failed to derive relative leaf path for {}",
                        leaf_dir.display()
                    )
                })?;
                out.join(relative)
            }
            None => leaf_dir.to_path_buf(),
        };
        // Algorithm differences are confined to cache path resolution
        // (PIPLUP: <rel>/train.cache, Result; Drain3: <hash>.drain3.cache, PathBuf),
        // expressed via inline match. The leaf training call is a single
        // train_for_entries_core (algorithm dispatch lives inside it via
        // CliTrainingRunner), so the rest of the loop skeleton is fully shared.
        let cache_path = match args.algorithm {
            Algorithm::Piplup => args
                .cache_dir
                .as_ref()
                .map(|cache_root| resolve_cache_leaf_path(root, &leaf_dir, cache_root))
                .transpose()?,
            Algorithm::Drain3 => args
                .cache_dir
                .as_ref()
                .map(|cache_root| resolve_drain3_leaf_cache_path(root, &leaf_files, cache_root)),
        };
        let (training, index_opt, resume_cursor) = train_for_entries_core(
            args,
            &display_input,
            &entries,
            config_output_dir,
            cache_path,
            progress,
        )?;
        // The "training on {display_input}" and "building template index"
        // statuses are printed inside core (preserving the old sequence). This
        // "writing template index" status mirrors what write_index_file printed
        // in the old single-call path; it must stay here (after build, before
        // write) so the per-leaf status sequence is byte-identical.
        if args.emit_index {
            progress.status("writing template index")?;
        }
        writer.record_leaf(
            root,
            &leaf_dir,
            args.out_dir.as_deref(),
            &training,
            index_opt.as_ref(),
            resume_cursor,
        )?;
    }

    Ok(writer.finish())
}

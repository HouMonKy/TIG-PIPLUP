use std::{
    io::{self, IsTerminal},
    path::{Path, PathBuf},
};

use anyhow::anyhow;

use crate::{Drain3Parser, ParseOutput, PiplupParser, progress::ProgressReporter};

use super::{
    INDEX_NAME,
    args::{Algorithm, OnlineArgs, OutputFormat, cli_error},
    cache::{
        CacheConfigArgs, CacheInputPolicy, apply_cache_config, apply_drain3_cache_config,
        finalize_cache, finalize_drain3_cache, save_drain3_online_cache, save_online_cache,
    },
    config::{
        Drain3ConfigOverrides, build_drain3_config, build_training_config,
        piplup_overrides_from_online, shared_config_flags_from_online,
    },
    input::{build_input_plan, parse_file, parse_stdin, read_stdin_entries},
    output::{
        DirectoryParseOutputWriter, build_field_projection, build_output_plan, output_dir_for_plan,
        resolve_directory_output_paths, resolve_output_plan, write_index_from_drain3_output,
        write_index_from_parse_output, write_output, write_structured_output,
    },
    summary::{print_directory_summary, print_summary},
    types::{DirectoryRunStats, InputPlan, OutputPlan, ResolvedOutputPlan},
};

pub(super) fn run_online(args: OnlineArgs) -> anyhow::Result<()> {
    let stderr = io::stderr();
    let mode = args
        .io
        .progress
        .resolve(args.io.quiet, stderr.is_terminal());
    let mut progress = ProgressReporter::new(stderr, mode);
    run_online_with_progress(args, &mut progress)
}

pub(super) fn run_online_with_progress<W: io::Write>(
    args: OnlineArgs,
    progress: &mut ProgressReporter<W>,
) -> anyhow::Result<()> {
    let input_plan = build_input_plan(&args.io.input)?;
    let output_plan = build_output_plan(&args.io, &input_plan, args.algorithm)?;

    // Reject --output-format both with stdin input.
    if args.io.output_format == OutputFormat::Both && matches!(input_plan, InputPlan::Stdin) {
        return Err(anyhow!(
            "--output-format both cannot be used with stdin input; \
             use --out-dir to specify an output directory"
        ));
    }

    let projection = build_field_projection(
        &args.io.output_fields,
        &args.log_format,
        args.keep_params,
        args.io.quiet,
    )?;

    // Cache is not applicable to stdin input — reject early.
    if matches!(input_plan, InputPlan::Stdin) && args.cache.is_some() {
        return Err(crate::LogParseError::CacheNotApplicableToStdin.into());
    }
    if matches!(input_plan, InputPlan::Stdin) && args.checkpoint_lines.is_some() {
        return Err(crate::LogParseError::CacheNotApplicableToStdin.into());
    }
    if args.segment_lines == Some(0) {
        return Err(cli_error("--segment-lines must be > 0").into());
    }
    if args.checkpoint_lines == Some(0) {
        return Err(cli_error("--checkpoint-lines must be > 0").into());
    }

    match input_plan {
        InputPlan::Stdin => {
            if args.emit_index {
                return Err(cli_error(
                    "online --emit-index does not support stdin input; write input to a file and use train --emit-index",
                )
                .into());
            }
            let resolved_output = resolve_output_plan(&output_plan, None)?;
            let output = match args.algorithm {
                Algorithm::Piplup => {
                    let mut parser = build_parser(&args, &args.io.input, &resolved_output)?;
                    parse_stdin(&mut parser, progress)?
                }
                Algorithm::Drain3 => {
                    let mut parser = build_drain3_parser(&args, &args.io.input, &resolved_output)?;
                    let entries = read_stdin_entries(progress)?;
                    progress.status("parsing stdin")?;
                    parser.parse_lines(&entries)?
                }
            };
            progress.status("writing outputs")?;
            write_output(
                &output,
                &resolved_output,
                args.io.output_format,
                &projection,
            )?;

            if !args.io.quiet {
                print_summary(&output, &resolved_output);
            }
        }
        InputPlan::File(input) => {
            let resolved_output = resolve_output_plan(&output_plan, Some(&input))?;
            let mut parser = cli_build_online_parser(&args, &input, &resolved_output)?;
            progress.status(format!("processing {}", input.display()))?;
            let output = cli_online_parse_file(&mut parser, &input, progress, true)?;
            progress.status("writing outputs")?;
            write_output(
                &output,
                &resolved_output,
                args.io.output_format,
                &projection,
            )?;
            if args.emit_index {
                let index_path = args
                    .index_out
                    .clone()
                    .unwrap_or_else(|| output_dir_for_plan(&resolved_output).join(INDEX_NAME));
                cli_online_emit_index(
                    &mut parser,
                    &output,
                    &index_path,
                    args.cache.as_deref(),
                    progress,
                )?;
            } else if let Some(cache_path) = args.cache.as_ref() {
                cli_online_finalize_cache(&mut parser, cache_path, &args.force_rebuild_cache)?;
            }

            if !args.io.quiet {
                print_summary(&output, &resolved_output);
            }
        }
        InputPlan::Directory { files, .. } => {
            if args.emit_index {
                return Err(cli_error(
                    "online directory input does not support --emit-index; use train --emit-index",
                )
                .into());
            }
            let stats = run_online_directory_loop(
                &args,
                &output_plan,
                &files,
                args.io.output_format,
                &projection,
                progress,
            )?;
            if !args.io.quiet {
                print_directory_summary(&stats, &output_plan);
            }
        }
    }

    Ok(())
}

pub(super) fn build_parser(
    args: &OnlineArgs,
    input: &Path,
    output_plan: &ResolvedOutputPlan,
) -> Result<PiplupParser, crate::LogParseError> {
    let config = build_training_config(
        &args.log_format,
        input,
        output_dir_for_plan(output_plan),
        piplup_overrides_from_online(args),
        shared_config_flags_from_online(args),
    );
    let (config, initial_state) = apply_cache_config(config, cache_config_args(args, input))?;

    let mut parser = if let Some(initial) = initial_state {
        PiplupParser::with_cache(config, Some(initial))?
    } else {
        PiplupParser::new(config)?
    };
    parser.set_input_path(input);
    Ok(parser)
}

fn build_drain3_parser(
    args: &OnlineArgs,
    input: &Path,
    output_plan: &ResolvedOutputPlan,
) -> Result<Drain3Parser, crate::LogParseError> {
    let config = build_drain3_config(
        &args.log_format,
        input,
        output_dir_for_plan(output_plan),
        Drain3ConfigOverrides {
            depth: args.depth,
            max_children: args.max_children,
            sim_thresh: args.drain3_sim_thresh,
            parametrize_numeric_tokens: !args.no_parametrize_numeric_tokens,
        },
        shared_config_flags_from_online(args),
    );
    let (config, initial_state) =
        apply_drain3_cache_config(config, cache_config_args(args, input))?;

    let mut parser = if let Some(initial) = initial_state {
        Drain3Parser::with_cache(config, Some(initial))?
    } else {
        Drain3Parser::new(config)?
    };
    parser.set_input_path(input);
    Ok(parser)
}

/// Build `CacheConfigArgs` for an online command (always warm-start on
/// mismatch). Mirrors `train.rs::cache_config_args` (which uses RequireMatch).
fn cache_config_args<'a>(args: &'a OnlineArgs, input: &'a Path) -> CacheConfigArgs<'a> {
    CacheConfigArgs {
        current_input: input,
        cache_path: args.cache.as_deref(),
        segment_lines: args.segment_lines,
        checkpoint_lines: args.checkpoint_lines,
        cache_compress: args.cache_compress,
        force_rebuild: args.force_rebuild_cache,
        input_policy: CacheInputPolicy::WarmStartOnMismatch,
    }
}

pub(crate) enum CliOnlineParser {
    Piplup(PiplupParser),
    Drain3(Drain3Parser),
}

pub(crate) fn cli_build_online_parser(
    args: &OnlineArgs,
    input: &Path,
    output_plan: &ResolvedOutputPlan,
) -> Result<CliOnlineParser, crate::LogParseError> {
    match args.algorithm {
        Algorithm::Piplup => Ok(CliOnlineParser::Piplup(build_parser(
            args,
            input,
            output_plan,
        )?)),
        Algorithm::Drain3 => Ok(CliOnlineParser::Drain3(build_drain3_parser(
            args,
            input,
            output_plan,
        )?)),
    }
}

pub(crate) fn cli_online_parse_file<W: io::Write>(
    parser: &mut CliOnlineParser,
    input: &Path,
    progress: &mut ProgressReporter<W>,
    report_progress: bool,
) -> anyhow::Result<ParseOutput> {
    match parser {
        CliOnlineParser::Piplup(p) => parse_file(p, input, progress, report_progress),
        // Drain3's parse_file has no progress hook; report_progress is ignored
        // in this arm, matching the original behavior.
        CliOnlineParser::Drain3(p) => Ok(p.parse_file(input)?),
    }
}

pub(crate) fn cli_online_finalize_cache(
    parser: &mut CliOnlineParser,
    cache_path: &Path,
    force_rebuild: &bool,
) -> anyhow::Result<()> {
    match parser {
        CliOnlineParser::Piplup(p) => finalize_cache(p, cache_path, force_rebuild),
        CliOnlineParser::Drain3(p) => finalize_drain3_cache(p, cache_path, force_rebuild),
    }
}

/// Replicates the original single-file `--emit-index` take-once / use-twice
/// sequence: take the cache once, then use it both for the index write
/// (`cache.as_ref()`) and an optional cache save (move).
pub(crate) fn cli_online_emit_index<W: io::Write>(
    parser: &mut CliOnlineParser,
    output: &ParseOutput,
    index_path: &Path,
    cache_path: Option<&Path>,
    progress: &mut ProgressReporter<W>,
) -> anyhow::Result<()> {
    match parser {
        CliOnlineParser::Piplup(p) => {
            let cache = p.take_cache_state();
            write_index_from_parse_output(
                output,
                cache.as_ref(),
                p.config(),
                index_path,
                progress,
            )?;
            if let (Some(cache_path), Some(cache)) = (cache_path, cache) {
                save_online_cache(p.config(), cache, cache_path)?;
            }
        }
        CliOnlineParser::Drain3(p) => {
            let cache = p.take_cache_state();
            write_index_from_drain3_output(
                output,
                cache.as_ref(),
                p.config(),
                index_path,
                progress,
            )?;
            if let (Some(cache_path), Some(cache)) = (cache_path, cache) {
                save_drain3_online_cache(p.config(), cache, cache_path)?;
            }
        }
    }
    Ok(())
}

fn run_online_directory_loop<W: io::Write>(
    args: &OnlineArgs,
    output_plan: &OutputPlan,
    files: &[PathBuf],
    format: OutputFormat,
    projection: &crate::FieldProjection,
    progress: &mut ProgressReporter<W>,
) -> anyhow::Result<DirectoryRunStats> {
    let mut writer = DirectoryParseOutputWriter::new(output_plan, files.len());

    for (index, input) in files.iter().enumerate() {
        progress.directory_file(index + 1, files.len(), input)?;
        let output_paths = resolve_directory_output_paths(output_plan, input)?;
        let resolved_output = ResolvedOutputPlan::Directory(output_paths.structured_dir.clone());
        let mut parser = cli_build_online_parser(args, input, &resolved_output)?;
        let output = cli_online_parse_file(&mut parser, input, progress, false)?;
        write_structured_output(&output, &output_paths.structured_dir, format, projection)?;
        if let Some(cache_path) = args.cache.as_ref() {
            cli_online_finalize_cache(&mut parser, cache_path, &args.force_rebuild_cache)?;
        }
        writer.record_file(input, &output)?;
    }

    writer.finish()
}

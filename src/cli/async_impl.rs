use std::{collections::BTreeMap, io, path::PathBuf, time::Instant};

use anyhow::anyhow;

use crate::{
    PiplupConfig,
    async_api::{
        directory::{run_online_directory, run_training_directory},
        types::{DirectoryConfig, SchedulerConfig, SessionPlan},
    },
    index::IndexBuilder,
    progress::ProgressReporter,
};

use super::{
    args::{Algorithm, OnlineArgs, ParseArgs, TrainArgs, cli_error, resolved_max_concurrent},
    config::{
        build_training_config, piplup_overrides_from_online, piplup_overrides_from_train,
        shared_config_flags_from_online, shared_config_flags_from_train,
    },
    input::build_input_plan,
    output::{
        DirectoryParseOutputWriter, DirectoryTrainingArtifactWriter, build_field_projection,
        build_output_plan, resolve_directory_index_path, resolve_directory_output_paths,
        resolve_output_plan, write_structured_output,
    },
    summary::{print_build_index_directory_summary, print_directory_summary},
    types::{InputPlan, OutputPlan, ResolvedOutputPlan},
};

pub(super) async fn run_online_async<W: io::Write>(
    args: OnlineArgs,
    progress: &mut ProgressReporter<W>,
) -> anyhow::Result<()> {
    if args.algorithm == Algorithm::Drain3 {
        return Err(anyhow!(
            "--async is currently only supported with the piplup algorithm"
        ));
    }

    let input_plan = build_input_plan(&args.io.input)?;
    let output_plan = build_output_plan(&args.io, &input_plan, args.algorithm)?;

    let projection = build_field_projection(
        &args.io.output_fields,
        &args.log_format,
        args.keep_params,
        args.io.quiet,
    )?;

    match input_plan {
        InputPlan::Directory { root, files } => {
            let plans = SessionPlan::online_plans_for_directory(&root)?;
            let config = build_piplup_config_for_async(&args, &root, &output_plan);
            let dir_config = directory_config(resolved_max_concurrent(args.max_concurrent));

            progress.status("starting async online parse")?;
            let started = Instant::now();
            let results = run_online_directory(config, dir_config, plans).await?;
            let elapsed = started.elapsed();

            let mut writer = DirectoryParseOutputWriter::new(&output_plan, files.len());

            for (key, output) in &results {
                let file_path = key.path.as_deref().unwrap_or(&root);
                let output_paths = resolve_directory_output_paths(&output_plan, file_path)?;
                write_structured_output(
                    output,
                    &output_paths.structured_dir,
                    args.io.output_format,
                    &projection,
                )?;
                writer.record_file(file_path, output)?;
            }

            let stats = writer.finish_with_duration(elapsed)?;

            if !args.io.quiet {
                print_directory_summary(&stats, &output_plan);
            }
        }
        _ => {
            return Err(anyhow!("--async is only supported for directory input"));
        }
    }

    Ok(())
}

pub(super) async fn run_train_async<W: io::Write>(
    args: TrainArgs,
    progress: &mut ProgressReporter<W>,
) -> anyhow::Result<()> {
    if args.algorithm == Algorithm::Drain3 {
        return Err(anyhow!(
            "--async is currently only supported with the piplup algorithm"
        ));
    }

    let input_plan = build_input_plan(&args.input)?;

    match input_plan {
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

            let plans = SessionPlan::training_plans_for_directory(&root)?;
            let config = build_piplup_config_from_train(&args, &root);
            let dir_config = directory_config(resolved_max_concurrent(args.max_concurrent));

            progress.status("starting async training")?;
            let started = Instant::now();
            let results = run_training_directory(config.clone(), dir_config, plans).await?;

            let mut writer =
                DirectoryTrainingArtifactWriter::new(started, results.len(), files.len());

            for (i, (key, training_output)) in results.iter().enumerate() {
                let leaf_dir = key.path.as_deref().unwrap_or(&root);

                let index = if args.emit_index {
                    progress.status(format!(
                        "building index for leaf {}/{}",
                        i + 1,
                        results.len()
                    ))?;
                    Some(IndexBuilder::build(training_output, &config)?)
                } else {
                    None
                };

                writer.record_leaf(
                    &root,
                    leaf_dir,
                    args.out_dir.as_deref(),
                    training_output,
                    index.as_ref(),
                    0,
                )?;
            }

            let stats = writer.finish();

            if !args.quiet {
                print_build_index_directory_summary(&stats, &root, args.out_dir.as_deref());
            }
        }
        _ => {
            return Err(anyhow!("--async is only supported for directory input"));
        }
    }

    Ok(())
}

pub(super) async fn run_parse_async<W: io::Write>(
    args: ParseArgs,
    progress: &mut ProgressReporter<W>,
) -> anyhow::Result<()> {
    let input_plan = build_input_plan(&args.io.input)?;
    let output_plan = build_output_plan(&args.io, &input_plan, Algorithm::Piplup)?;

    let projection = build_field_projection(
        &args.io.output_fields,
        &args.log_format,
        args.keep_params,
        args.io.quiet,
    )?;

    match input_plan {
        InputPlan::Directory { root, files } => {
            let mut writer = DirectoryParseOutputWriter::new(&output_plan, files.len());
            let mut parser_cache = BTreeMap::<PathBuf, crate::IndexedParser>::new();

            let started = Instant::now();

            for (index, input) in files.iter().enumerate() {
                progress.directory_file(index + 1, files.len(), input)?;
                let output_paths = resolve_directory_output_paths(&output_plan, input)?;
                let resolved_output =
                    ResolvedOutputPlan::Directory(output_paths.structured_dir.clone());
                let templates_idx =
                    resolve_directory_index_path(&root, input, args.index.as_deref())?;

                let parser = match parser_cache.entry(templates_idx.clone()) {
                    std::collections::btree_map::Entry::Vacant(entry) => {
                        progress.status(format!("loading index {}", templates_idx.display()))?;
                        let config = super::config::build_indexed_config_for_input(
                            &args,
                            input,
                            &resolved_output,
                        );
                        let parser = crate::IndexedParser::from_bytes(
                            &std::fs::read(&templates_idx)?,
                            config,
                        )?;
                        entry.insert(parser)
                    }
                    std::collections::btree_map::Entry::Occupied(entry) => entry.into_mut(),
                };

                progress.status(format!("processing {} (async)", input.display()))?;
                let scheduler_config = SchedulerConfig::default();
                let plan = SessionPlan::for_file(input.clone());
                let output = crate::async_api::session::run_indexed_plan_session(
                    parser.clone(),
                    scheduler_config,
                    plan,
                )
                .await
                .map_err(|e| anyhow!("failed to parse {} with index: {}", input.display(), e))?;

                super::parse::handle_indexed_output_for_input(
                    &args,
                    input,
                    &output,
                    &resolved_output,
                    args.io.output_format,
                    &projection,
                )?;
                writer.record_file(input, &output.parse_output)?;
            }

            let elapsed = started.elapsed();
            let stats = writer.finish_with_duration(elapsed)?;

            if !args.io.quiet {
                print_directory_summary(&stats, &output_plan);
            }
        }
        _ => {
            return Err(anyhow!("--async is only supported for directory input"));
        }
    }

    Ok(())
}

fn directory_config(max_concurrent: usize) -> DirectoryConfig {
    DirectoryConfig {
        max_concurrent_sessions: max_concurrent,
        scheduler_config: SchedulerConfig::default(),
    }
}

fn build_piplup_config_for_async(
    args: &OnlineArgs,
    input: &std::path::Path,
    output_plan: &OutputPlan,
) -> PiplupConfig {
    let resolved_output =
        resolve_output_plan(output_plan, Some(input)).unwrap_or(ResolvedOutputPlan::Directory(
            std::env::current_dir().unwrap_or_else(|_| PathBuf::from(".")),
        ));
    build_training_config(
        &args.log_format,
        input,
        super::output::output_dir_for_plan(&resolved_output),
        piplup_overrides_from_online(args),
        shared_config_flags_from_online(args),
    )
}

fn build_piplup_config_from_train(args: &TrainArgs, root: &std::path::Path) -> PiplupConfig {
    build_training_config(
        &args.log_format,
        root,
        args.out_dir.clone().unwrap_or_else(|| PathBuf::from(".")),
        piplup_overrides_from_train(args),
        shared_config_flags_from_train(args),
    )
}

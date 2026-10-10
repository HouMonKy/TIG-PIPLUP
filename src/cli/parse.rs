use std::{
    collections::BTreeMap,
    fs,
    io::{self, IsTerminal},
    path::{Path, PathBuf},
};

use anyhow::{Context, anyhow};

use crate::{IndexedParser, progress::ProgressReporter};

use super::{
    args::{Algorithm, OutputFormat, ParseArgs, cli_error},
    config::{build_indexed_config, build_indexed_config_for_input},
    input::{build_input_plan, read_file_entries, read_stdin_entries},
    output::{
        DirectoryParseOutputWriter, build_field_projection, build_output_plan,
        resolve_directory_index_path, resolve_directory_output_paths, resolve_output_plan,
        write_output,
    },
    summary::{print_directory_summary, print_indexed_summary},
    types::{DirectoryRunStats, InputPlan, OutputPlan, ResolvedOutputPlan},
};

pub(super) fn run_parse(args: ParseArgs) -> anyhow::Result<()> {
    let stderr = io::stderr();
    let mode = args
        .io
        .progress
        .resolve(args.io.quiet, stderr.is_terminal());
    let mut progress = ProgressReporter::new(stderr, mode);
    run_parse_with_progress(args, &mut progress)
}

pub(super) fn run_parse_with_progress<W: io::Write>(
    args: ParseArgs,
    progress: &mut ProgressReporter<W>,
) -> anyhow::Result<()> {
    let input_plan = build_input_plan(&args.io.input)?;
    let output_plan = build_output_plan(&args.io, &input_plan, Algorithm::Piplup)?;

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

    match input_plan {
        InputPlan::Stdin => {
            let templates_idx = required_index(&args, false)?;
            let config = build_indexed_config(&args);
            let resolved_output = resolve_output_plan(&output_plan, None)?;
            progress.status(format!("loading index {}", templates_idx.display()))?;
            let index_bytes = fs::read(&templates_idx)?;
            let parser = IndexedParser::from_bytes(&index_bytes, config)?;
            let entries = read_stdin_entries(progress)?;
            progress.status("parsing stdin with index")?;
            let output = parser.parse_lines_detailed(&entries)?;
            handle_indexed_output(
                &args,
                &output,
                &resolved_output,
                args.io.output_format,
                &projection,
            )?;
            if !args.io.quiet {
                print_indexed_summary("stdin", &templates_idx, &resolved_output, &output);
            }
        }
        InputPlan::File(input) => {
            let templates_idx = required_index(&args, false)?;
            let config = build_indexed_config(&args);
            let resolved_output = resolve_output_plan(&output_plan, Some(&input))?;
            progress.status(format!("loading index {}", templates_idx.display()))?;
            let index_bytes = fs::read(&templates_idx)?;
            let parser = IndexedParser::from_bytes(&index_bytes, config)?;
            progress.status(format!("processing {}", input.display()))?;
            let entries = read_file_entries(&input)?;
            let output = parser
                .parse_lines_detailed(&entries)
                .with_context(|| format!("failed to parse {} with index", input.display()))?;
            handle_indexed_output(
                &args,
                &output,
                &resolved_output,
                args.io.output_format,
                &projection,
            )?;
            if !args.io.quiet {
                print_indexed_summary(
                    &input.display().to_string(),
                    &templates_idx,
                    &resolved_output,
                    &output,
                );
            }
        }
        InputPlan::Directory { root, files } => {
            let stats = run_parse_directory(
                &args,
                &root,
                &files,
                &output_plan,
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

pub(super) fn run_parse_directory<W: io::Write>(
    args: &ParseArgs,
    root: &Path,
    files: &[PathBuf],
    output_plan: &OutputPlan,
    format: OutputFormat,
    projection: &crate::FieldProjection,
    progress: &mut ProgressReporter<W>,
) -> anyhow::Result<DirectoryRunStats> {
    let mut writer = DirectoryParseOutputWriter::new(output_plan, files.len());
    let mut parser_cache = BTreeMap::<PathBuf, IndexedParser>::new();

    for (index, input) in files.iter().enumerate() {
        progress.directory_file(index + 1, files.len(), input)?;
        let output_paths = resolve_directory_output_paths(output_plan, input)?;
        let resolved_output = ResolvedOutputPlan::Directory(output_paths.structured_dir.clone());
        let templates_idx = resolve_directory_index_path(root, input, args.index.as_deref())?;

        let parser = if let Some(parser) = parser_cache.get(&templates_idx) {
            parser
        } else {
            progress.status(format!("loading index {}", templates_idx.display()))?;
            let config = build_indexed_config_for_input(args, input, &resolved_output);
            let parser = IndexedParser::from_bytes(&fs::read(&templates_idx)?, config)?;
            parser_cache.insert(templates_idx.clone(), parser);
            parser_cache
                .get(&templates_idx)
                .expect("cached parser should be available")
        };

        progress.status(format!("processing {}", input.display()))?;
        let entries = read_file_entries(input)?;
        let output = parser.parse_lines_detailed(&entries).with_context(|| {
            format!(
                "failed to parse {} with index {}",
                input.display(),
                templates_idx.display()
            )
        })?;
        handle_indexed_output_for_input(
            args,
            input,
            &output,
            &resolved_output,
            format,
            projection,
        )?;
        writer.record_file(input, &output.parse_output)?;
    }

    writer.finish()
}

pub(super) fn required_index(
    args: &ParseArgs,
    directory_input: bool,
) -> Result<PathBuf, clap::Error> {
    match (&args.index, directory_input) {
        (Some(path), _) => Ok(path.clone()),
        (None, true) => Ok(args.io.input.clone()),
        (None, false) => Err(cli_error(
            "parse requires --index for single-file or stdin input",
        )),
    }
}

pub(super) fn handle_indexed_output(
    args: &ParseArgs,
    output: &crate::IndexedParseOutput,
    plan: &ResolvedOutputPlan,
    format: OutputFormat,
    projection: &crate::FieldProjection,
) -> anyhow::Result<()> {
    handle_indexed_output_for_input(args, Path::new("stdin"), output, plan, format, projection)
}

pub(super) fn handle_indexed_output_for_input(
    args: &ParseArgs,
    input: &Path,
    output: &crate::IndexedParseOutput,
    plan: &ResolvedOutputPlan,
    format: OutputFormat,
    projection: &crate::FieldProjection,
) -> anyhow::Result<()> {
    if args.fail_on_miss && output.stats.new_miss_count > 0 {
        let first = output
            .stats
            .first_new_miss_line
            .map(|line| line.to_string())
            .unwrap_or_else(|| "unknown".to_string());
        return Err(anyhow!(
            "indexed parse introduced new misses for {} with new_miss_count={} first_new_miss_line={} total_miss_count={} training_miss_count={}",
            input.display(),
            output.stats.new_miss_count,
            first,
            output.stats.miss_count,
            output.stats.training_miss_count
        ));
    }
    write_output(&output.parse_output, plan, format, projection)?;
    Ok(())
}

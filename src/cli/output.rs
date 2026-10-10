use std::{
    collections::BTreeMap,
    ffi::OsStr,
    fs::{self, File},
    io::{self, BufWriter},
    path::{Path, PathBuf},
    time::{Duration, Instant},
};

use anyhow::{Context, anyhow};

use crate::{
    Drain3Config, EventRecord, ParseOutput, PiplupConfig,
    cache::CacheState,
    drain3::{Drain3CacheState, drain3_leaf_cache_file_id},
    index::{IndexBuilder, IndexWriter, TemplateIndexFile},
    progress::ProgressReporter,
};

use super::{
    INDEX_NAME, STRUCTURED_JSONL_NAME, STRUCTURED_NAME, TEMPLATES_NAME,
    args::{IoArgs, OutputFormat, cli_error},
    input::is_stdin_path,
    types::{
        DirectoryIndexBuildStats, DirectoryOutputPaths, DirectoryRunStats, InputPlan,
        LeafTemplateAccumulator, OutputPlan, OutputSink, ResolvedOutputPlan, merge_leaf_templates,
    },
};

pub(super) fn write_output(
    output: &ParseOutput,
    plan: &ResolvedOutputPlan,
    format: OutputFormat,
    projection: &crate::FieldProjection,
) -> anyhow::Result<()> {
    match plan {
        ResolvedOutputPlan::Directory(dir) => {
            fs::create_dir_all(dir)?;
            match format {
                OutputFormat::Csv | OutputFormat::Both => {
                    output.write_structured_csv_projected(
                        BufWriter::new(File::create(dir.join(STRUCTURED_NAME))?),
                        projection,
                    )?;
                }
                OutputFormat::Jsonl => {}
            }
            match format {
                OutputFormat::Jsonl | OutputFormat::Both => {
                    output.write_structured_jsonl(
                        BufWriter::new(File::create(dir.join(STRUCTURED_JSONL_NAME))?),
                        projection,
                    )?;
                }
                OutputFormat::Csv => {}
            }
            output.write_templates_csv(BufWriter::new(File::create(dir.join(TEMPLATES_NAME))?))?;
        }
        ResolvedOutputPlan::Separate {
            structured,
            templates,
        } => {
            match format {
                OutputFormat::Csv => {
                    write_sink(structured, |writer| {
                        output.write_structured_csv_projected(writer, projection)
                    })?;
                }
                OutputFormat::Jsonl => {
                    write_sink(structured, |writer| {
                        output.write_structured_jsonl(writer, projection)
                    })?;
                }
                OutputFormat::Both => {
                    // Check for stdout first - can't write both formats to stdout.
                    if matches!(structured, OutputSink::Stdout) {
                        return Err(anyhow!(
                            "--output-format both cannot write both CSV and JSONL to stdout"
                        ));
                    }

                    let OutputSink::Path(path) = structured else {
                        unreachable!("already checked Stdout");
                    };

                    // Check for path collision - if path already has .jsonl extension,
                    // with_extension("jsonl") returns the same path, causing overwrites.
                    let jsonl_path = path.with_extension("jsonl");
                    if jsonl_path == *path {
                        return Err(anyhow!(
                            "--structured path '{}' already has .jsonl extension; \
                             use --out-dir or specify a path with a different extension \
                             for --output-format both",
                            path.display()
                        ));
                    }

                    write_sink(structured, |writer| {
                        output.write_structured_csv_projected(writer, projection)
                    })?;
                    let jsonl_sink = OutputSink::Path(jsonl_path);
                    write_sink(&jsonl_sink, |writer| {
                        output.write_structured_jsonl(writer, projection)
                    })?;
                }
            }
            write_sink(templates, |writer| output.write_templates_csv(writer))?;
        }
    }
    Ok(())
}

pub(super) fn write_structured_output(
    output: &ParseOutput,
    dir: &Path,
    format: OutputFormat,
    projection: &crate::FieldProjection,
) -> anyhow::Result<()> {
    fs::create_dir_all(dir)?;
    match format {
        OutputFormat::Csv | OutputFormat::Both => {
            output.write_structured_csv_projected(
                BufWriter::new(File::create(dir.join(STRUCTURED_NAME))?),
                projection,
            )?;
        }
        OutputFormat::Jsonl => {}
    }
    match format {
        OutputFormat::Jsonl | OutputFormat::Both => {
            output.write_structured_jsonl(
                BufWriter::new(File::create(dir.join(STRUCTURED_JSONL_NAME))?),
                projection,
            )?;
        }
        OutputFormat::Csv => {}
    }
    Ok(())
}

pub(super) fn write_template_records(events: &[EventRecord], path: &Path) -> anyhow::Result<()> {
    if let Some(parent) = path.parent()
        && !parent.as_os_str().is_empty()
    {
        fs::create_dir_all(parent)?;
    }

    ParseOutput {
        line_results: Vec::new(),
        events: events.to_vec(),
        cluster_count: 0,
        duration: Duration::default(),
    }
    .write_templates_csv(BufWriter::new(File::create(path)?))?;

    Ok(())
}

pub(super) fn write_index_from_parse_output<W: io::Write>(
    output: &ParseOutput,
    cache: Option<&CacheState>,
    config: &PiplupConfig,
    path: &Path,
    progress: &mut ProgressReporter<W>,
) -> anyhow::Result<()> {
    let preprocess_plan = if let Some(cache) = cache {
        cache
            .clone()
            .into_parse_state()
            .map_err(crate::LogParseError::from)?
            .preprocess_plan
    } else {
        crate::PreprocessPlan::default()
    };
    progress.status("building template index")?;
    let ctx = crate::IndexBuildContext {
        log_format: config.log_format.clone(),
        preprocess: config.preprocess,
        typed_placeholders: config.typed_placeholders,
        training: crate::AlgorithmTrainingMetadata::Piplup {
            merge: config.merge,
            keep_para: config.keep_para,
            hit_limit: config.hit_limit,
            br_thresh: config.br_thresh,
            sim_thresh: config.sim_thresh.clone(),
        },
    };
    let index = IndexBuilder::build_from_parts(output, &preprocess_plan, &ctx, &config.separators)?;
    if let Some(parent) = path.parent()
        && !parent.as_os_str().is_empty()
    {
        fs::create_dir_all(parent)?;
    }
    progress.status("writing template index")?;
    IndexWriter::write(BufWriter::new(File::create(path)?), &index)?;
    Ok(())
}

pub(super) fn write_index_from_drain3_output<W: io::Write>(
    output: &ParseOutput,
    cache: Option<&Drain3CacheState>,
    config: &Drain3Config,
    path: &Path,
    progress: &mut ProgressReporter<W>,
) -> anyhow::Result<()> {
    let preprocess_plan = if let Some(cache) = cache {
        crate::PreprocessPlan::from(cache.preprocess_plan.clone())
    } else {
        crate::PreprocessPlan::default()
    };
    progress.status("building template index")?;
    let index = IndexBuilder::build_drain3(
        &crate::TrainingOutput {
            parse_output: output.clone(),
            preprocess_plan,
        },
        config,
    )?;
    if let Some(parent) = path.parent()
        && !parent.as_os_str().is_empty()
    {
        fs::create_dir_all(parent)?;
    }
    progress.status("writing template index")?;
    IndexWriter::write(BufWriter::new(File::create(path)?), &index)?;
    Ok(())
}

pub(super) fn write_sink<F>(sink: &OutputSink, write: F) -> anyhow::Result<()>
where
    F: FnOnce(Box<dyn io::Write>) -> Result<(), crate::LogParseError>,
{
    match sink {
        OutputSink::Stdout => {
            let stdout = io::stdout();
            write(Box::new(stdout.lock()))?;
        }
        OutputSink::Path(path) => {
            if let Some(parent) = path.parent()
                && !parent.as_os_str().is_empty()
            {
                std::fs::create_dir_all(parent)?;
            }
            let file = BufWriter::new(File::create(path)?);
            write(Box::new(file))?;
        }
    }
    Ok(())
}

pub(super) fn build_field_projection(
    output_fields: &Option<String>,
    log_formats: &[String],
    keep_params: bool,
    quiet: bool,
) -> anyhow::Result<crate::FieldProjection> {
    let Some(fields_str) = output_fields else {
        return Ok(crate::FieldProjection::all());
    };

    let names: Vec<String> = fields_str
        .split(',')
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .collect();

    if names.is_empty() {
        return Err(anyhow!("--output-fields cannot be empty"));
    }

    let formats = if log_formats.is_empty() {
        vec!["<Date> <Time> <Content>".to_string()]
    } else {
        log_formats.to_vec()
    };
    let compiled = crate::MultiCompiledLogFormat::compile(&formats).map_err(|e| anyhow!("{e}"))?;

    let mut available = Vec::new();
    available.push("LineId".to_string());
    available.extend(compiled.headers().iter().cloned());
    available.push("EventId".to_string());
    available.push("EventTemplate".to_string());
    if keep_params {
        available.push("ParameterList".to_string());
    }

    if names.contains(&"ParameterList".to_string()) && !keep_params {
        return Err(anyhow!(
            "--output-fields includes 'ParameterList' but --keep-params is not enabled; \
             add --keep-params to include ParameterList in the output"
        ));
    }

    let has_content_field = compiled.headers().iter().any(|h| h == "Content");
    if has_content_field && !names.contains(&"Content".to_string()) && !quiet {
        eprintln!(
            "warning: --output-fields excludes 'Content'; \
             consumers will only see EventTemplate, not original log line content"
        );
    }

    Ok(crate::FieldProjection::subset(names, &available)?)
}

pub(super) fn build_output_plan(
    io: &IoArgs,
    input: &InputPlan,
    algorithm: super::args::Algorithm,
) -> Result<OutputPlan, clap::Error> {
    if let Some(out_dir) = &io.out_dir {
        return Ok(match input {
            InputPlan::Directory { root, .. } => OutputPlan::MirroredDirectory {
                input_root: root.clone(),
                output_root: Some(out_dir.clone()),
                suffix: algorithm_suffix(algorithm).to_string(),
            },
            InputPlan::Stdin | InputPlan::File(_) => OutputPlan::Directory(out_dir.clone()),
        });
    }

    match (&io.structured, &io.templates) {
        (Some(structured), Some(templates)) => {
            if matches!(input, InputPlan::Directory { .. }) {
                return Err(cli_error(
                    "directory input does not support --structured/--templates; use --out-dir",
                ));
            }
            let structured = parse_sink(structured);
            let templates = parse_sink(templates);
            if matches!(structured, OutputSink::Stdout) && matches!(templates, OutputSink::Stdout) {
                return Err(cli_error(
                    "--structured and --templates cannot both write to stdout",
                ));
            }
            Ok(OutputPlan::Separate {
                structured,
                templates,
            })
        }
        (Some(_), None) | (None, Some(_)) => Err(cli_error(
            "--structured and --templates must be provided together",
        )),
        (None, None) => match input {
            InputPlan::Stdin => Err(cli_error(
                "reading from stdin requires --out-dir, --structured, and --templates",
            )),
            InputPlan::File(path) => Ok(OutputPlan::Directory(default_output_dir(
                path,
                algorithm_suffix(algorithm),
            ))),
            InputPlan::Directory { root, .. } => Ok(OutputPlan::MirroredDirectory {
                input_root: root.clone(),
                output_root: None,
                suffix: algorithm_suffix(algorithm).to_string(),
            }),
        },
    }
}

pub(super) fn parse_sink(path: &Path) -> OutputSink {
    if is_stdin_path(path) {
        OutputSink::Stdout
    } else {
        OutputSink::Path(path.to_path_buf())
    }
}

pub(super) fn resolve_output_plan(
    plan: &OutputPlan,
    input: Option<&Path>,
) -> anyhow::Result<ResolvedOutputPlan> {
    Ok(match plan {
        OutputPlan::Directory(path) => ResolvedOutputPlan::Directory(path.clone()),
        OutputPlan::Separate {
            structured,
            templates,
        } => ResolvedOutputPlan::Separate {
            structured: structured.clone(),
            templates: templates.clone(),
        },
        OutputPlan::MirroredDirectory {
            input_root,
            output_root,
            suffix,
        } => {
            let input = input
                .ok_or_else(|| anyhow!("directory output plan requires a concrete input path"))?;
            let dir = match output_root {
                Some(root) => {
                    let relative = input.strip_prefix(input_root).with_context(|| {
                        format!("failed to derive relative path for {}", input.display())
                    })?;
                    mirrored_output_dir(relative, root, suffix)
                }
                None => default_output_dir(input, suffix),
            };
            ResolvedOutputPlan::Directory(dir)
        }
    })
}

pub(super) fn resolve_directory_output_paths(
    plan: &OutputPlan,
    input: &Path,
) -> anyhow::Result<DirectoryOutputPaths> {
    match plan {
        OutputPlan::MirroredDirectory {
            input_root,
            output_root,
            suffix,
        } => {
            let relative = input.strip_prefix(input_root).with_context(|| {
                format!("failed to derive relative path for {}", input.display())
            })?;

            let leaf_dir = match output_root {
                Some(root) => mirrored_leaf_output_dir(relative, root),
                None => input
                    .parent()
                    .map(Path::to_path_buf)
                    .unwrap_or_else(|| PathBuf::from(".")),
            };

            Ok(DirectoryOutputPaths {
                structured_dir: leaf_dir.join(output_dir_name_with_suffix(input, suffix)),
                templates_path: leaf_dir.join(TEMPLATES_NAME),
            })
        }
        OutputPlan::Directory(path) => Ok(DirectoryOutputPaths {
            structured_dir: path.clone(),
            templates_path: path.join(TEMPLATES_NAME),
        }),
        OutputPlan::Separate { .. } => Err(anyhow!(
            "directory input does not support explicit structured/template paths",
        )),
    }
}

pub(super) fn resolve_build_index_leaf_paths(
    input_root: &Path,
    leaf_dir: &Path,
    out_dir: Option<&Path>,
) -> anyhow::Result<(PathBuf, PathBuf)> {
    let base_dir = match out_dir {
        Some(root) => {
            let relative = leaf_dir.strip_prefix(input_root).with_context(|| {
                format!(
                    "failed to derive relative leaf path for {}",
                    leaf_dir.display()
                )
            })?;
            root.join(relative)
        }
        None => leaf_dir.to_path_buf(),
    };

    Ok((base_dir.join(INDEX_NAME), base_dir.join(TEMPLATES_NAME)))
}

pub(super) fn resolve_cache_leaf_path(
    input_root: &Path,
    leaf_dir: &Path,
    cache_root: &Path,
) -> anyhow::Result<PathBuf> {
    let relative = leaf_dir.strip_prefix(input_root).with_context(|| {
        format!(
            "failed to derive relative leaf path for {}",
            leaf_dir.display()
        )
    })?;
    Ok(cache_root.join(relative).join("train.cache"))
}

/// Resolve the hash-based cache file path for a Drain3 leaf directory cache.
///
/// For directory input, training is per-leaf (all `.log` files in a leaf are
/// concatenated).  The cache identity covers the entire leaf: all file paths
/// are sorted and hashed together so the result is stable regardless of
/// filesystem enumeration order.
pub(super) fn resolve_drain3_leaf_cache_path(
    input_root: &Path,
    leaf_files: &[PathBuf],
    cache_root: &Path,
) -> PathBuf {
    let sorted: Vec<&Path> = {
        let mut v: Vec<&Path> = leaf_files.iter().map(PathBuf::as_path).collect();
        v.sort();
        v
    };
    let file_id = drain3_leaf_cache_file_id(input_root, &sorted);
    cache_root.join(format!("{file_id}.drain3.cache"))
}

pub(super) fn resolve_directory_index_path(
    input_root: &Path,
    input: &Path,
    templates_idx: Option<&Path>,
) -> anyhow::Result<PathBuf> {
    let leaf_dir = input
        .parent()
        .ok_or_else(|| anyhow!("missing parent directory for {}", input.display()))?;
    let index_root = templates_idx.unwrap_or(input_root);

    let path = if index_root
        .extension()
        .and_then(OsStr::to_str)
        .map(|ext| ext.eq_ignore_ascii_case("idx"))
        .unwrap_or(false)
    {
        return Err(anyhow!(
            "--index must be a directory when parsing directory input (got {})",
            index_root.display()
        ));
    } else {
        let relative = leaf_dir.strip_prefix(input_root).with_context(|| {
            format!(
                "failed to derive relative leaf path for {}",
                input.display()
            )
        })?;
        index_root.join(relative).join(INDEX_NAME)
    };

    if !path.is_file() {
        return Err(anyhow!(
            "missing leaf index for {}: expected {}",
            input.display(),
            path.display()
        ));
    }

    Ok(path)
}

pub(super) fn output_dir_for_plan(plan: &ResolvedOutputPlan) -> PathBuf {
    match plan {
        ResolvedOutputPlan::Directory(path) => path.clone(),
        ResolvedOutputPlan::Separate { .. } => {
            std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."))
        }
    }
}

pub(super) fn default_output_dir(input: &Path, suffix: &str) -> PathBuf {
    input
        .parent()
        .unwrap_or_else(|| Path::new("."))
        .join(output_dir_name_with_suffix(input, suffix))
}

pub(super) fn mirrored_output_dir(
    relative_input: &Path,
    output_root: &Path,
    suffix: &str,
) -> PathBuf {
    let parent = relative_input
        .parent()
        .filter(|path| !path.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new(""));
    output_root
        .join(parent)
        .join(output_dir_name_with_suffix(relative_input, suffix))
}

pub(super) fn mirrored_leaf_output_dir(relative_input: &Path, output_root: &Path) -> PathBuf {
    let parent = relative_input
        .parent()
        .filter(|path| !path.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new(""));
    output_root.join(parent)
}

pub(super) fn output_dir_name_with_suffix(input: &Path, suffix: &str) -> String {
    let stem = input
        .file_stem()
        .or_else(|| input.file_name())
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_else(|| "out".to_string());
    format!("{stem}.{suffix}")
}

pub(super) fn algorithm_suffix(algorithm: super::args::Algorithm) -> &'static str {
    match algorithm {
        super::args::Algorithm::Piplup => "piplup",
        super::args::Algorithm::Drain3 => "drain3",
    }
}

// ---------------------------------------------------------------------------
// Directory parse-output writer
// ---------------------------------------------------------------------------

/// Converges directory parse-output materialization across sync online, sync
/// indexed parse, async online, and async indexed parse paths.
///
/// Owns output-path resolution, per-leaf template aggregation, per-leaf
/// `templates.csv` write, and `DirectoryRunStats` accumulation. The caller
/// retains parser execution, indexed miss-policy handling, structured-output
/// writing, progress rendering, and command-specific error context.
///
/// The caller writes structured output (CSV/JSONL) **before** calling
/// [`record_file`](Self::record_file): online paths use
/// [`write_structured_output`]; indexed paths use
/// [`handle_indexed_output_for_input`](super::parse::handle_indexed_output_for_input)
/// / [`write_output`] (which also writes the per-file `templates.csv` inside
/// each `structured_dir` — an asymmetry that is preserved verbatim).
pub(super) struct DirectoryParseOutputWriter<'a> {
    output_plan: &'a OutputPlan,
    templates_by_leaf: BTreeMap<PathBuf, LeafTemplateAccumulator>,
    stats: DirectoryRunStats,
    started: Instant,
}

impl<'a> DirectoryParseOutputWriter<'a> {
    /// Create a writer for a directory run with the given file count.
    pub(super) fn new(output_plan: &'a OutputPlan, file_count: usize) -> Self {
        Self {
            output_plan,
            templates_by_leaf: BTreeMap::new(),
            stats: DirectoryRunStats {
                file_count,
                leaf_dir_count: 0,
                line_count: 0,
                template_count: 0,
                duration: Duration::default(),
            },
            started: Instant::now(),
        }
    }

    /// Per-file: resolve output paths, aggregate templates, count lines.
    ///
    /// The caller writes structured output (online via `write_structured_output`;
    /// indexed via `write_output` / `handle_indexed_output_for_input`) **before**
    /// calling this method.
    pub(super) fn record_file(&mut self, input: &Path, output: &ParseOutput) -> anyhow::Result<()> {
        let output_paths = resolve_directory_output_paths(self.output_plan, input)?;
        merge_leaf_templates(
            self.templates_by_leaf
                .entry(output_paths.templates_path.clone())
                .or_insert_with(LeafTemplateAccumulator::new),
            &output.events,
        );
        self.stats.line_count += output.line_results.len();
        Ok(())
    }

    /// Write per-leaf aggregated `templates.csv`, self-time the duration, and
    /// return the accumulated stats. Used by synchronous paths.
    pub(super) fn finish(mut self) -> anyhow::Result<DirectoryRunStats> {
        self.stats.leaf_dir_count = self.templates_by_leaf.len();
        for (templates_path, aggregate) in &self.templates_by_leaf {
            self.stats.template_count += aggregate.events.len();
            write_template_records(&aggregate.events, templates_path)?;
        }
        self.stats.duration = self.started.elapsed();
        Ok(self.stats)
    }

    /// Like [`finish`](Self::finish), but the duration is injected by the
    /// caller. Async online/parse paths cover the whole async run (not just
    /// materialization), so the duration must be preserved verbatim.
    #[cfg(feature = "async")]
    pub(super) fn finish_with_duration(
        mut self,
        elapsed: Duration,
    ) -> anyhow::Result<DirectoryRunStats> {
        self.stats.leaf_dir_count = self.templates_by_leaf.len();
        for (templates_path, aggregate) in &self.templates_by_leaf {
            self.stats.template_count += aggregate.events.len();
            write_template_records(&aggregate.events, templates_path)?;
        }
        self.stats.duration = elapsed;
        Ok(self.stats)
    }
}

// ---------------------------------------------------------------------------
// Shared index-file write primitive
// ---------------------------------------------------------------------------

/// Write a [`TemplateIndexFile`] to disk: create parent dirs, open a
/// `BufWriter`, and call [`IndexWriter::write`].
///
/// Deliberately excludes `progress.status` — callers retain their own wording
/// (sync `train.rs` prints "writing template index"; async `async_impl.rs`
/// prints "building index for leaf i/n") so status ordering and text are
/// unchanged. Algorithm-specific `IndexBuilder::build` / `build_drain3` stays
/// at the caller; this primitive only writes a ready index.
pub(super) fn write_template_index_file(
    path: &Path,
    index: &TemplateIndexFile,
) -> anyhow::Result<()> {
    if let Some(parent) = path.parent()
        && !parent.as_os_str().is_empty()
    {
        fs::create_dir_all(parent)?;
    }
    let idx_file = BufWriter::new(File::create(path)?);
    IndexWriter::write(idx_file, index)?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Directory training-artifact writer
// ---------------------------------------------------------------------------

/// Converges directory training-artifact materialization across sync and async
/// PIPLUP/Drain3 train paths.
///
/// Both sync and async directory train materialize per-leaf artifacts through
/// [`record_leaf`]: the writer resolves the leaf `(templates.idx,
/// templates.csv)` paths, writes the optional caller-built index, writes
/// `templates.csv`, and accumulates `DirectoryIndexBuildStats`. The caller
/// retains algorithm-specific index construction (`IndexBuilder::build` vs
/// `build_drain3`), cache-path resolution, progress wording, and the training
/// pass itself.
///
/// `resume_line_count` is accumulated verbatim from each `record_leaf` call:
/// sync passes the leaf's restored line count; async passes 0 (async train
/// rejects `--cache`, so resume is always 0) — preserving the existing
/// sync-accumulates / async-stays-0 asymmetry (ADR-0040 §6.10).
pub(super) struct DirectoryTrainingArtifactWriter {
    stats: DirectoryIndexBuildStats,
    started: Instant,
}

impl DirectoryTrainingArtifactWriter {
    /// `started` is captured by the caller BEFORE `group_files_by_leaf` (sync)
    /// or at the async run start, so the reported duration covers grouping +
    /// materialization (sync) or the whole async run (async).
    pub(super) fn new(started: Instant, leaf_dir_count: usize, file_count: usize) -> Self {
        Self {
            stats: DirectoryIndexBuildStats {
                leaf_dir_count,
                file_count,
                line_count: 0,
                template_count: 0,
                duration: Duration::default(),
                resume_line_count: 0,
            },
            started,
        }
    }

    /// Per-leaf: resolve `(templates_idx, templates_csv)` paths, optionally
    /// write the caller-built index, write `templates.csv`, and accumulate
    /// stats. `resume_line_count` is accumulated verbatim: sync passes the
    /// leaf's restored line count; async passes 0 (async train rejects
    /// `--cache`).
    pub(super) fn record_leaf(
        &mut self,
        input_root: &Path,
        leaf_dir: &Path,
        out_dir: Option<&Path>,
        training: &crate::TrainingOutput,
        index: Option<&TemplateIndexFile>,
        resume_line_count: usize,
    ) -> anyhow::Result<()> {
        let (templates_idx, templates_csv) =
            resolve_build_index_leaf_paths(input_root, leaf_dir, out_dir)?;
        if let Some(index) = index {
            write_template_index_file(&templates_idx, index)?;
        }
        write_template_records(&training.parse_output.events, &templates_csv)?;
        self.stats.line_count += training.parse_output.line_results.len();
        self.stats.template_count += training.parse_output.template_count();
        self.stats.resume_line_count += resume_line_count;
        Ok(())
    }

    /// Duration from the `started` instant passed to [`new`](Self::new): covers
    /// grouping + materialization for sync, the whole async run for async.
    pub(super) fn finish(self) -> DirectoryIndexBuildStats {
        let Self { mut stats, started } = self;
        stats.duration = started.elapsed();
        stats
    }
}

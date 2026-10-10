use std::path::PathBuf;

use clap::{Args, CommandFactory, Parser, Subcommand, ValueEnum};

use crate::{SimThresh, progress::ProgressMode};

#[derive(Debug, Parser)]
#[command(
    name = "log-parser",
    bin_name = "log-parser",
    version,
    about = "Parse logs with one of several parser algorithms.",
    long_about = None
)]
pub(super) struct Cli {
    #[command(subcommand)]
    pub(super) command: Commands,
}

#[derive(Debug, Subcommand)]
pub(super) enum Commands {
    #[command(
        about = "Online learn templates and parse logs.",
        long_about = None,
        after_long_help = "Examples:\n  log-parser online HDFS_2k.log\n  log-parser online organized_logs\n  log-parser online organized_logs -o parsed_logs\n  log-parser online -f \"<Date> <Time> <Pid> <Content>\" app.log -o out\n  log-parser online --emit-index app.log\n  cat app.log | log-parser online - --structured structured.csv --templates templates.csv"
    )]
    Online(OnlineArgs),
    #[command(
        about = "Train templates from a log file or directory.",
        long_about = None
    )]
    Train(TrainArgs),
    #[command(
        about = "Parse logs with a prebuilt template index.",
        long_about = None
    )]
    Parse(ParseArgs),
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, ValueEnum)]
pub(super) enum Algorithm {
    #[default]
    Piplup,
    Drain3,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, ValueEnum)]
pub(super) enum PreprocessMode {
    #[default]
    Eager,
    Typed,
    Off,
}

#[derive(Debug, Args)]
pub(super) struct IoArgs {
    #[arg(
        value_name = "INPUT",
        help = "Path to a log file or directory. Directory inputs recurse over all .log files. Use '-' to read from stdin."
    )]
    pub(super) input: PathBuf,

    #[arg(
        short = 'o',
        long = "out-dir",
        value_name = "DIR",
        conflicts_with_all = ["structured", "templates"],
        help = "Write structured.csv and templates.csv to DIR."
    )]
    pub(super) out_dir: Option<PathBuf>,

    #[arg(
        long,
        value_name = "PATH",
        help = "Write structured output to PATH. Use '-' for stdout."
    )]
    pub(super) structured: Option<PathBuf>,

    #[arg(
        long,
        value_name = "PATH",
        help = "Write template summary to PATH. Use '-' for stdout."
    )]
    pub(super) templates: Option<PathBuf>,

    #[arg(
        long = "progress",
        value_name = "MODE",
        default_value = "auto",
        value_parser = parse_progress_mode,
        help = "Progress reporting on stderr: auto, always, or never."
    )]
    pub(super) progress: ProgressMode,

    #[arg(
        short = 'q',
        long,
        help = "Suppress progress output and final summaries."
    )]
    pub(super) quiet: bool,

    #[arg(
        long = "output-format",
        value_name = "FORMAT",
        default_value = "csv",
        value_parser = parse_output_format,
        help = "Structured output format: csv, jsonl, or both."
    )]
    pub(super) output_format: OutputFormat,

    #[arg(
        long = "output-fields",
        value_name = "FIELDS",
        help = "Comma-separated list of fields to include in structured output. \
                Available: LineId, <log format fields>, EventId, EventTemplate, ParameterList. \
                Default: all fields."
    )]
    pub(super) output_fields: Option<String>,
}

#[derive(Debug, Args)]
pub(super) struct OnlineArgs {
    #[command(flatten)]
    pub(super) io: IoArgs,

    #[cfg(feature = "async")]
    #[arg(
        long = "async",
        help = "Use async I/O for directory input. Requires the `async` feature."
    )]
    pub(super) r#async: bool,

    #[cfg(feature = "async")]
    #[arg(
        long = "max-concurrent",
        value_name = "N",
        help = "Maximum independent directory sessions to run concurrently with --async. [default: 1]"
    )]
    pub(super) max_concurrent: Option<usize>,

    #[arg(
        long = "algorithm",
        value_enum,
        default_value_t = Algorithm::Piplup,
        help = "Learning algorithm to use."
    )]
    pub(super) algorithm: Algorithm,

    #[arg(
        short = 'f',
        long = "log-format",
        value_name = "FORMAT",
        action = clap::ArgAction::Append,
        help = "Log format pattern. Repeatable for multi-format (tried in order, first match wins). Text outside <Field> markers is treated as regex."
    )]
    pub(super) log_format: Vec<String>,

    #[arg(
        long = "hit-limit",
        value_name = "N",
        default_value_t = 385,
        help = "Promote from the None bucket after N hits."
    )]
    pub(super) hit_limit: usize,

    #[arg(
        long = "branch-threshold",
        value_name = "N",
        default_value_t = 5,
        help = "Maximum path branching threshold."
    )]
    pub(super) branch_threshold: usize,

    #[arg(
        long = "sim-thresh",
        value_name = "VALUE",
        default_value = "dynamic",
        value_parser = parse_sim_thresh,
        help = "Similarity threshold. Use 'dynamic' or a float like '0.6'."
    )]
    pub(super) sim_thresh: SimThresh,

    #[arg(
        long = "no-preprocess",
        conflicts_with = "preprocess_mode",
        help = "Disable preprocessing before tokenization."
    )]
    pub(super) no_preprocess: bool,

    #[arg(
        long = "preprocess-mode",
        value_enum,
        value_name = "MODE",
        help = "Variable normalization mode: eager, typed, or off. [default: eager]"
    )]
    pub(super) preprocess_mode: Option<PreprocessMode>,

    #[arg(long = "no-merge", help = "Disable cross-cluster template merging.")]
    pub(super) no_merge: bool,

    #[arg(
        long = "keep-params",
        help = "Include ParameterList in structured.csv."
    )]
    pub(super) keep_params: bool,

    #[arg(
        long = "warn-miss",
        help = "Print a warning to stderr for each log line that fails to match a template."
    )]
    pub(super) warn_miss: bool,

    #[arg(
        long = "skip-unmatched",
        help = "Skip lines that don't match any configured log format instead of erroring."
    )]
    pub(super) skip_unmatched: bool,

    #[arg(
        long = "cache",
        value_name = "PATH",
        help = "Enable piplup cache. Persists ParseState and PreprocessPlan across runs."
    )]
    pub(super) cache: Option<PathBuf>,

    #[arg(
        long = "segment-lines",
        value_name = "N",
        help = "Segment input into N-entry processing windows. Independent of cache: works with stdin and without --cache. No I/O overhead."
    )]
    pub(super) segment_lines: Option<usize>,

    #[arg(
        long = "checkpoint-lines",
        value_name = "M",
        help = "Write a checkpoint every M entries during training. Requires --cache. Mutually exclusive with stdin."
    )]
    pub(super) checkpoint_lines: Option<usize>,

    #[arg(
        long = "cache-compress",
        help = "Compress cache payload with LZ4. Requires --cache and the cache-compress feature."
    )]
    pub(super) cache_compress: bool,

    #[arg(
        long = "force-rebuild-cache",
        help = "Overwrite an incompatible cache instead of erroring out."
    )]
    pub(super) force_rebuild_cache: bool,

    // ── Drain3-specific args (incompatible with PIPLUP) ──────────────
    #[arg(
        long = "depth",
        value_name = "N",
        help = "Drain3 parse tree depth. Minimum 3. (default: 4)"
    )]
    pub(super) depth: Option<usize>,

    #[arg(
        long = "max-children",
        value_name = "N",
        help = "Drain3 max literal children per node. (default: 100)"
    )]
    pub(super) max_children: Option<usize>,

    #[arg(
        long = "drain3-sim-thresh",
        value_name = "FLOAT",
        help = "Drain3 similarity threshold, 0.0..=1.0. (default: 0.4)"
    )]
    pub(super) drain3_sim_thresh: Option<f64>,

    #[arg(
        long = "no-parametrize-numeric-tokens",
        help = "Disable Drain3 numeric token parametrization."
    )]
    pub(super) no_parametrize_numeric_tokens: bool,

    // ── Index output ─────────────────────────────────────────────────
    #[arg(
        long = "emit-index",
        help = "Also write a compiled templates.idx index."
    )]
    pub(super) emit_index: bool,

    #[arg(
        long = "index-out",
        value_name = "PATH",
        requires = "emit_index",
        help = "Write the compiled template index to PATH for single-file input."
    )]
    pub(super) index_out: Option<PathBuf>,
}

#[derive(Debug, Args)]
pub(super) struct TrainArgs {
    #[arg(
        value_name = "INPUT",
        help = "Path to a log file or directory. Directory inputs recurse over all .log files and build one index per leaf directory."
    )]
    pub(super) input: PathBuf,

    #[cfg(feature = "async")]
    #[arg(
        long = "async",
        help = "Use async I/O for directory input. Requires the `async` feature."
    )]
    pub(super) r#async: bool,

    #[cfg(feature = "async")]
    #[arg(
        long = "max-concurrent",
        value_name = "N",
        help = "Maximum independent leaf-directory sessions to run concurrently with --async. [default: 1]"
    )]
    pub(super) max_concurrent: Option<usize>,

    #[arg(
        long = "index-out",
        value_name = "PATH",
        requires = "emit_index",
        help = "Write the compiled template index to PATH for single-file input."
    )]
    pub(super) index_out: Option<PathBuf>,

    #[arg(
        long = "emit-index",
        help = "Also write a compiled templates.idx index."
    )]
    pub(super) emit_index: bool,

    #[arg(
        short = 'o',
        long = "out-dir",
        value_name = "DIR",
        conflicts_with = "index_out",
        help = "Write directory indexes under DIR, mirroring each input leaf directory."
    )]
    pub(super) out_dir: Option<PathBuf>,

    #[arg(
        long = "algorithm",
        value_enum,
        default_value_t = Algorithm::Piplup,
        help = "Learning algorithm to use."
    )]
    pub(super) algorithm: Algorithm,

    #[arg(
        long = "progress",
        value_name = "MODE",
        default_value = "auto",
        value_parser = parse_progress_mode,
        help = "Progress reporting on stderr: auto, always, or never."
    )]
    pub(super) progress: ProgressMode,

    #[arg(
        short = 'q',
        long,
        help = "Suppress progress output and final summaries."
    )]
    pub(super) quiet: bool,

    #[arg(
        short = 'f',
        long = "log-format",
        value_name = "FORMAT",
        action = clap::ArgAction::Append,
        help = "Log format pattern. Repeatable for multi-format (tried in order, first match wins). Text outside <Field> markers is treated as regex."
    )]
    pub(super) log_format: Vec<String>,

    #[arg(long = "hit-limit", value_name = "N", default_value_t = 385)]
    pub(super) hit_limit: usize,

    #[arg(long = "branch-threshold", value_name = "N", default_value_t = 5)]
    pub(super) branch_threshold: usize,

    #[arg(
        long = "sim-thresh",
        value_name = "VALUE",
        default_value = "dynamic",
        value_parser = parse_sim_thresh
    )]
    pub(super) sim_thresh: SimThresh,

    #[arg(long = "no-preprocess", conflicts_with = "preprocess_mode")]
    pub(super) no_preprocess: bool,

    #[arg(
        long = "preprocess-mode",
        value_enum,
        value_name = "MODE",
        help = "Variable normalization mode: eager, typed, or off. [default: eager]"
    )]
    pub(super) preprocess_mode: Option<PreprocessMode>,

    #[arg(long = "no-merge")]
    pub(super) no_merge: bool,

    #[arg(long = "keep-params")]
    pub(super) keep_params: bool,

    #[arg(
        long = "warn-miss",
        help = "Print a warning to stderr for each log line that fails to match a template, and for clusters whose template degrades to <*>."
    )]
    pub(super) warn_miss: bool,

    #[arg(
        long = "skip-unmatched",
        help = "Skip lines that don't match any configured log format instead of erroring."
    )]
    pub(super) skip_unmatched: bool,

    #[arg(
        long = "cache",
        value_name = "PATH",
        help = "Enable piplup cache. Persists ParseState and PreprocessPlan across runs."
    )]
    pub(super) cache: Option<PathBuf>,

    #[arg(
        long = "segment-lines",
        value_name = "N",
        help = "Segment input into N-entry processing windows. Independent of cache: works with stdin and without --cache. No I/O overhead."
    )]
    pub(super) segment_lines: Option<usize>,

    #[arg(
        long = "checkpoint-lines",
        value_name = "M",
        help = "Write a checkpoint every M entries during training. Requires --cache. Mutually exclusive with stdin."
    )]
    pub(super) checkpoint_lines: Option<usize>,

    #[arg(
        long = "cache-dir",
        value_name = "DIR",
        conflicts_with = "cache",
        help = "Directory input cache root. Mirrors leaf directories and writes one train.cache per leaf."
    )]
    pub(super) cache_dir: Option<PathBuf>,

    #[arg(
        long = "cache-compress",
        help = "Compress cache payload with LZ4. Requires --cache and the cache-compress feature."
    )]
    pub(super) cache_compress: bool,

    #[arg(
        long = "force-rebuild-cache",
        help = "Overwrite an incompatible cache instead of erroring out."
    )]
    pub(super) force_rebuild_cache: bool,

    // ── Drain3-specific args (incompatible with PIPLUP) ──────────────
    #[arg(
        long = "depth",
        value_name = "N",
        help = "Drain3 parse tree depth. Minimum 3. (default: 4)"
    )]
    pub(super) depth: Option<usize>,

    #[arg(
        long = "max-children",
        value_name = "N",
        help = "Drain3 max literal children per node. (default: 100)"
    )]
    pub(super) max_children: Option<usize>,

    #[arg(
        long = "drain3-sim-thresh",
        value_name = "FLOAT",
        help = "Drain3 similarity threshold, 0.0..=1.0. (default: 0.4)"
    )]
    pub(super) drain3_sim_thresh: Option<f64>,

    #[arg(
        long = "no-parametrize-numeric-tokens",
        help = "Disable Drain3 numeric token parametrization."
    )]
    pub(super) no_parametrize_numeric_tokens: bool,
}

#[derive(Debug, Args)]
pub(super) struct ParseArgs {
    #[command(flatten)]
    pub(super) io: IoArgs,

    #[cfg(feature = "async")]
    #[arg(
        long = "async",
        help = "Use async I/O for directory input. Requires the `async` feature."
    )]
    pub(super) r#async: bool,

    #[arg(
        long = "index",
        value_name = "PATH",
        help = "Path to a templates.idx file for single-file/stdin input, or a root directory containing per-leaf templates.idx files for directory input."
    )]
    pub(super) index: Option<PathBuf>,

    #[arg(
        short = 'f',
        long = "log-format",
        value_name = "FORMAT",
        action = clap::ArgAction::Append,
        help = "Log format pattern. Repeatable for multi-format (tried in order, first match wins). Text outside <Field> markers is treated as regex."
    )]
    pub(super) log_format: Vec<String>,

    #[arg(long = "no-preprocess", conflicts_with = "preprocess_mode")]
    pub(super) no_preprocess: bool,

    #[arg(
        long = "preprocess-mode",
        value_enum,
        value_name = "MODE",
        help = "Variable normalization mode: eager, typed, or off. [default: eager]"
    )]
    pub(super) preprocess_mode: Option<PreprocessMode>,

    #[arg(long = "keep-params")]
    pub(super) keep_params: bool,

    #[arg(
        long = "warn-miss",
        help = "Print a warning to stderr for each log line that fails to match a template."
    )]
    pub(super) warn_miss: bool,

    #[arg(
        long = "skip-unmatched",
        help = "Skip lines that don't match any configured log format instead of erroring."
    )]
    pub(super) skip_unmatched: bool,

    #[arg(
        long = "fail-on-miss",
        help = "Fail if indexed parsing introduces misses that were not present in the training output."
    )]
    pub(super) fail_on_miss: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum OutputFormat {
    Csv,
    Jsonl,
    Both,
}

fn parse_sim_thresh(value: &str) -> Result<SimThresh, String> {
    if value.eq_ignore_ascii_case("dynamic") {
        return Ok(SimThresh::Dynamic);
    }

    let parsed = value.parse::<f64>().map_err(|_| {
        format!("invalid similarity threshold '{value}': expected 'dynamic' or a float")
    })?;
    Ok(SimThresh::Fixed(parsed))
}

fn parse_progress_mode(value: &str) -> Result<ProgressMode, String> {
    ProgressMode::parse(value).map_err(|err| err.to_string())
}

fn parse_output_format(value: &str) -> Result<OutputFormat, String> {
    match value.to_ascii_lowercase().as_str() {
        "csv" => Ok(OutputFormat::Csv),
        "jsonl" => Ok(OutputFormat::Jsonl),
        "both" => Ok(OutputFormat::Both),
        _ => Err(format!(
            "invalid output format '{value}': expected csv, jsonl, or both"
        )),
    }
}

pub(super) fn cli_error(message: &str) -> clap::Error {
    let mut command = Cli::command();
    command.error(clap::error::ErrorKind::ValueValidation, message)
}

/// PIPLUP-only CLI flags that are incompatible with `--algorithm drain3`.
/// Each entry is `(clap_arg_id, cli_flag_name)`.
const PIPLUP_ONLY_FLAGS: &[(&str, &str)] = &[
    ("hit_limit", "--hit-limit"),
    ("branch_threshold", "--branch-threshold"),
    ("sim_thresh", "--sim-thresh"),
    ("no_merge", "--no-merge"),
];

/// Drain3-only CLI flags that are incompatible with PIPLUP (the default
/// algorithm).  When any of these are present without an explicit
/// `--algorithm drain3`, validation fails.
/// Each entry is `(clap_arg_id, cli_flag_name)`.
const DRAIN3_ONLY_FLAGS: &[(&str, &str)] = &[
    ("depth", "--depth"),
    ("max_children", "--max-children"),
    ("drain3_sim_thresh", "--drain3-sim-thresh"),
    (
        "no_parametrize_numeric_tokens",
        "--no-parametrize-numeric-tokens",
    ),
];

/// Validate that algorithm-specific flags are consistent with the selected
/// algorithm.  Call this after clap parsing but before dispatch.
///
/// `matches` should be the subcommand-level `ArgMatches` (i.e., the matches
/// for the `online` or `train` subcommand, not the root command).
pub(super) fn validate_algorithm_args(
    matches: &clap::ArgMatches,
    algorithm: Algorithm,
) -> Result<(), clap::Error> {
    let algorithm_explicit =
        matches.value_source("algorithm") == Some(clap::parser::ValueSource::CommandLine);

    match algorithm {
        Algorithm::Drain3 => {
            for &(id, flag) in PIPLUP_ONLY_FLAGS {
                if matches.value_source(id) == Some(clap::parser::ValueSource::CommandLine) {
                    return Err(cli_error(&format!(
                        "{flag} is not compatible with --algorithm drain3"
                    )));
                }
            }
        }
        Algorithm::Piplup => {
            for &(id, flag) in DRAIN3_ONLY_FLAGS {
                if matches.value_source(id) == Some(clap::parser::ValueSource::CommandLine) {
                    if algorithm_explicit {
                        return Err(cli_error(&format!(
                            "{flag} is not compatible with --algorithm piplup"
                        )));
                    } else {
                        return Err(cli_error(&format!("{flag} requires --algorithm drain3")));
                    }
                }
            }
        }
    }
    Ok(())
}

#[cfg(feature = "async")]
pub(super) fn resolved_max_concurrent(value: Option<usize>) -> usize {
    value.unwrap_or(1)
}

#[cfg(feature = "async")]
pub(super) fn validate_async_concurrency_args(
    matches: &clap::ArgMatches,
    async_enabled: bool,
    max_concurrent: Option<usize>,
) -> Result<(), clap::Error> {
    if max_concurrent == Some(0) {
        return Err(cli_error("--max-concurrent must be greater than 0"));
    }

    let max_concurrent_explicit =
        matches.value_source("max_concurrent") == Some(clap::parser::ValueSource::CommandLine);
    if max_concurrent_explicit && !async_enabled {
        return Err(cli_error("--max-concurrent requires --async"));
    }

    Ok(())
}

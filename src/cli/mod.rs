//! Command-line interface for `log-parser`.
//!
//! The CLI is feature-gated behind the `cli` Cargo feature. It provides the
//! `online`, `train`, and `parse` subcommands and wires them to the
//! synchronous or async parser implementations.

use std::ffi::OsString;

#[cfg(feature = "async")]
use crate::progress::ProgressReporter;
use clap::{CommandFactory, Parser};
#[cfg(feature = "async")]
use std::io::{self, IsTerminal};

mod args;
#[cfg(feature = "async")]
mod async_impl;
mod cache;
mod config;
mod input;
mod online;
mod output;
mod parse;
mod summary;
mod train;
mod types;

/// Sentinel value for stdin input.
const INPUT_STDIN: &str = "-";
/// Default structured CSV output filename.
const STRUCTURED_NAME: &str = "structured.csv";
/// Default structured JSONL output filename.
const STRUCTURED_JSONL_NAME: &str = "structured.jsonl";
/// Default templates CSV output filename.
const TEMPLATES_NAME: &str = "templates.csv";
/// Default index output filename.
const INDEX_NAME: &str = "templates.idx";
/// Progress reporting interval for stdin parsing.
const STDIN_PROGRESS_STEP: usize = 1_024;

use args::{Cli, Commands};

/// Run the CLI using arguments from the process environment.
///
/// This is the binary entry point for `log-parser`.
pub fn run() -> anyhow::Result<()> {
    run_from(std::env::args_os())
}

fn run_from<I, T>(args: I) -> anyhow::Result<()>
where
    I: IntoIterator<Item = T>,
    T: Into<OsString> + Clone,
{
    // Collect args for two-pass parsing: first raw matches for validation,
    // then typed struct for dispatch.
    let args_vec: Vec<T> = args.into_iter().collect();
    let os_args: Vec<OsString> = args_vec.iter().map(|a| a.clone().into()).collect();
    let matches = match Cli::command().try_get_matches_from(&os_args) {
        Ok(m) => m,
        Err(error)
            if matches!(
                error.kind(),
                clap::error::ErrorKind::DisplayHelp | clap::error::ErrorKind::DisplayVersion
            ) =>
        {
            error.print()?;
            return Ok(());
        }
        Err(error) => return Err(error.into()),
    };

    let cli = Cli::try_parse_from(&os_args)?;

    // Validate algorithm-specific flag compatibility.
    match &cli.command {
        Commands::Online(a) => {
            if let Some(sub) = matches.subcommand_matches("online") {
                args::validate_algorithm_args(sub, a.algorithm)?;
                #[cfg(feature = "async")]
                args::validate_async_concurrency_args(sub, a.r#async, a.max_concurrent)?;
            }
        }
        Commands::Train(a) => {
            if let Some(sub) = matches.subcommand_matches("train") {
                args::validate_algorithm_args(sub, a.algorithm)?;
                #[cfg(feature = "async")]
                args::validate_async_concurrency_args(sub, a.r#async, a.max_concurrent)?;
            }
        }
        Commands::Parse(_) => {}
    }

    match cli.command {
        Commands::Online(args) => {
            #[cfg(feature = "async")]
            if args.r#async {
                let rt = tokio::runtime::Runtime::new()?;
                let stderr = io::stderr();
                let mode = args
                    .io
                    .progress
                    .resolve(args.io.quiet, stderr.is_terminal());
                let mut progress = ProgressReporter::new(stderr, mode);
                rt.block_on(async_impl::run_online_async(args, &mut progress))?;
                return Ok(());
            }
            online::run_online(args)?
        }
        Commands::Train(args) => {
            #[cfg(feature = "async")]
            if args.r#async {
                let rt = tokio::runtime::Runtime::new()?;
                let stderr = io::stderr();
                let mode = args.progress.resolve(args.quiet, stderr.is_terminal());
                let mut progress = ProgressReporter::new(stderr, mode);
                rt.block_on(async_impl::run_train_async(args, &mut progress))?;
                return Ok(());
            }
            train::run_train(args)?
        }
        Commands::Parse(args) => {
            #[cfg(feature = "async")]
            if args.r#async {
                let rt = tokio::runtime::Runtime::new()?;
                let stderr = io::stderr();
                let mode = args
                    .io
                    .progress
                    .resolve(args.io.quiet, stderr.is_terminal());
                let mut progress = ProgressReporter::new(stderr, mode);
                rt.block_on(async_impl::run_parse_async(args, &mut progress))?;
                return Ok(());
            }
            parse::run_parse(args)?
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests;

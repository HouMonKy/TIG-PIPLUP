use std::path::{Path, PathBuf};

use crate::{Drain3Config, PiplupConfig, SimThresh};

use super::{
    args::{OnlineArgs, ParseArgs, PreprocessMode, TrainArgs},
    input::input_dir_for,
    output::output_dir_for_plan,
    types::ResolvedOutputPlan,
};

pub(super) fn build_indexed_config(args: &ParseArgs) -> PiplupConfig {
    build_indexed_config_for_input(
        args,
        &args.io.input,
        &ResolvedOutputPlan::Directory(
            args.io
                .out_dir
                .clone()
                .unwrap_or_else(|| std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."))),
        ),
    )
}

pub(super) fn build_indexed_config_for_input(
    args: &ParseArgs,
    input: &Path,
    output_plan: &ResolvedOutputPlan,
) -> PiplupConfig {
    build_base_config(
        &args.log_format,
        input,
        output_dir_for_plan(output_plan),
        shared_config_flags(
            args.preprocess_mode,
            args.no_preprocess,
            args.keep_params,
            args.warn_miss,
            args.skip_unmatched,
        ),
    )
}

/// Boolean flags shared by both PIPLUP and Drain3 configurations.
#[derive(Debug, Clone, Copy)]
pub(super) struct SharedConfigFlags {
    pub(super) preprocess: bool,
    pub(super) typed_placeholders: bool,
    pub(super) keep_para: bool,
    pub(super) warn_miss: bool,
    pub(super) skip_unmatched: bool,
}

#[derive(Debug, Clone)]
pub(super) struct TrainingConfigOverrides {
    pub(super) hit_limit: usize,
    pub(super) branch_threshold: usize,
    pub(super) sim_thresh: SimThresh,
    pub(super) merge: bool,
}

#[derive(Debug, Clone, Copy)]
pub(super) struct Drain3ConfigOverrides {
    pub(super) depth: Option<usize>,
    pub(super) max_children: Option<usize>,
    pub(super) sim_thresh: Option<f64>,
    pub(super) parametrize_numeric_tokens: bool,
}

pub(super) fn build_base_config(
    log_formats: &[String],
    input: &Path,
    output_dir: PathBuf,
    flags: SharedConfigFlags,
) -> PiplupConfig {
    let formats = if log_formats.is_empty() {
        vec!["<Date> <Time> <Content>".to_string()]
    } else {
        log_formats.to_vec()
    };
    PiplupConfig {
        log_format: formats,
        input_dir: input_dir_for(input),
        output_dir,
        preprocess: flags.preprocess,
        typed_placeholders: flags.typed_placeholders,
        keep_para: flags.keep_para,
        warn_miss: flags.warn_miss,
        skip_unmatched: flags.skip_unmatched,
        ..PiplupConfig::default()
    }
}

pub(super) fn build_training_config(
    log_formats: &[String],
    input: &Path,
    output_dir: PathBuf,
    overrides: TrainingConfigOverrides,
    flags: SharedConfigFlags,
) -> PiplupConfig {
    let mut config = build_base_config(log_formats, input, output_dir, flags);
    config.hit_limit = overrides.hit_limit;
    config.br_thresh = overrides.branch_threshold;
    config.sim_thresh = overrides.sim_thresh;
    config.merge = overrides.merge;
    config
}

pub(super) fn build_drain3_config(
    log_formats: &[String],
    input: &Path,
    output_dir: PathBuf,
    overrides: Drain3ConfigOverrides,
    flags: SharedConfigFlags,
) -> Drain3Config {
    let formats = if log_formats.is_empty() {
        vec!["<Date> <Time> <Content>".to_string()]
    } else {
        log_formats.to_vec()
    };
    Drain3Config {
        log_format: formats,
        input_dir: input_dir_for(input),
        output_dir,
        depth: overrides.depth.unwrap_or(4),
        sim_thresh: overrides.sim_thresh.unwrap_or(0.4),
        max_children: overrides.max_children.unwrap_or(100),
        parametrize_numeric_tokens: overrides.parametrize_numeric_tokens,
        preprocess: flags.preprocess,
        typed_placeholders: flags.typed_placeholders,
        keep_para: flags.keep_para,
        warn_miss: flags.warn_miss,
        skip_unmatched: flags.skip_unmatched,
        ..Drain3Config::default()
    }
}

/// Map online command flags into the shared PIPLUP/Drain3 boolean flag set.
/// Pure data mapping after clap has validated conflicting arguments.
pub(super) fn shared_config_flags_from_online(args: &OnlineArgs) -> SharedConfigFlags {
    shared_config_flags(
        args.preprocess_mode,
        args.no_preprocess,
        args.keep_params,
        args.warn_miss,
        args.skip_unmatched,
    )
}

/// Map train command flags into the shared PIPLUP/Drain3 boolean flag set.
/// Pure data mapping after clap has validated conflicting arguments.
pub(super) fn shared_config_flags_from_train(args: &TrainArgs) -> SharedConfigFlags {
    shared_config_flags(
        args.preprocess_mode,
        args.no_preprocess,
        args.keep_params,
        args.warn_miss,
        args.skip_unmatched,
    )
}

fn shared_config_flags(
    preprocess_mode: Option<PreprocessMode>,
    no_preprocess: bool,
    keep_para: bool,
    warn_miss: bool,
    skip_unmatched: bool,
) -> SharedConfigFlags {
    let mode = if no_preprocess {
        PreprocessMode::Off
    } else {
        preprocess_mode.unwrap_or_default()
    };
    let (preprocess, typed_placeholders) = match mode {
        PreprocessMode::Eager => (true, false),
        PreprocessMode::Typed => (true, true),
        PreprocessMode::Off => (false, false),
    };
    SharedConfigFlags {
        preprocess,
        typed_placeholders,
        keep_para,
        warn_miss,
        skip_unmatched,
    }
}

/// Map online command flags into PIPLUP training overrides.
/// Pure data mapping — no defaults, no validation.
pub(super) fn piplup_overrides_from_online(args: &OnlineArgs) -> TrainingConfigOverrides {
    TrainingConfigOverrides {
        hit_limit: args.hit_limit,
        branch_threshold: args.branch_threshold,
        sim_thresh: args.sim_thresh.clone(),
        merge: !args.no_merge,
    }
}

/// Map train command flags into PIPLUP training overrides.
/// Pure data mapping — no defaults, no validation.
pub(super) fn piplup_overrides_from_train(args: &TrainArgs) -> TrainingConfigOverrides {
    TrainingConfigOverrides {
        hit_limit: args.hit_limit,
        branch_threshold: args.branch_threshold,
        sim_thresh: args.sim_thresh.clone(),
        merge: !args.no_merge,
    }
}

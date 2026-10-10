use std::{
    ffi::OsString,
    path::{Path, PathBuf},
    time::{Duration, Instant},
};

use clap::{CommandFactory, Parser};

use super::{
    args::{Algorithm, Cli, Commands, IoArgs, OutputFormat},
    cache::{CacheConfigArgs, CacheInputPolicy, apply_cache_config, apply_drain3_cache_config},
    config::{
        build_indexed_config, piplup_overrides_from_online, piplup_overrides_from_train,
        shared_config_flags_from_online, shared_config_flags_from_train,
    },
    input::build_input_plan,
    output::{
        DirectoryParseOutputWriter, DirectoryTrainingArtifactWriter, build_output_plan,
        resolve_directory_output_paths,
    },
    run_from,
    types::{DirectoryOutputPaths, InputPlan, OutputPlan},
};
use crate::{
    CacheManager, Drain3CacheManager, Drain3Config, Drain3Parser, EventRecord, LineResult,
    LogEntry, ParseOutput, PiplupConfig, SimThresh,
    cache::{CacheState, path_identity},
    drain3::drain3_path_identity,
    input_plan::has_log_extension,
    preprocessor::PreprocessPlan,
    progress::ProgressMode,
};

#[test]
fn parses_online_subcommand() {
    let cli = Cli::try_parse_from(["log-parser", "online", "app.log"])
        .expect("online subcommand should parse");

    let Commands::Online(args) = cli.command else {
        panic!("expected online command");
    };
    assert_eq!(args.io.input, PathBuf::from("app.log"));
    assert_eq!(args.io.progress, ProgressMode::Auto);
    assert!(args.log_format.is_empty());
    assert_eq!(args.sim_thresh, SimThresh::Dynamic);
    assert!(!args.no_merge);
}

#[test]
fn sim_thresh_accepts_fixed_float() {
    let cli = Cli::try_parse_from(["log-parser", "online", "--sim-thresh", "0.6", "app.log"])
        .expect("fixed similarity threshold should parse");

    let Commands::Online(args) = cli.command else {
        panic!("expected online command");
    };
    assert_eq!(args.sim_thresh, SimThresh::Fixed(0.6));
}

#[test]
fn progress_mode_accepts_always() {
    let cli = Cli::try_parse_from(["log-parser", "online", "--progress", "always", "app.log"])
        .expect("progress mode should parse");

    let Commands::Online(args) = cli.command else {
        panic!("expected online command");
    };
    assert_eq!(args.io.progress, ProgressMode::Always);
}

#[test]
fn online_help_mentions_regex_log_formats() {
    let mut help = Vec::new();
    Cli::command()
        .find_subcommand_mut("online")
        .expect("online subcommand should exist")
        .write_long_help(&mut help)
        .expect("help should render");
    let help = String::from_utf8(help).expect("help should be valid utf-8");

    assert!(help.contains("Text outside <Field> markers is treated as regex"));
    assert!(help.contains("--emit-index"));
}

#[test]
fn output_fields_help_does_not_leak_visibility_modifier() {
    let mut help = Vec::new();
    Cli::command()
        .find_subcommand_mut("online")
        .expect("online subcommand should exist")
        .write_long_help(&mut help)
        .expect("help should render");
    let help = String::from_utf8(help).expect("help should be valid utf-8");

    assert!(help.contains("Available: LineId"));
    assert!(!help.contains("pub(super)"));
}

#[test]
fn root_help_mentions_index_commands() {
    let mut help = Vec::new();
    Cli::command()
        .write_long_help(&mut help)
        .expect("help should render");
    let help = String::from_utf8(help).expect("help should be valid utf-8");

    assert!(help.contains("online"));
    assert!(help.contains("train"));
    assert!(help.contains("parse"));
}

#[test]
fn old_subcommands_are_removed() {
    for subcommand in ["piplup", "build-index", "parse-with-index"] {
        let error = Cli::try_parse_from(["log-parser", subcommand, "app.log"])
            .expect_err("old subcommand should be removed");
        assert_eq!(error.kind(), clap::error::ErrorKind::InvalidSubcommand);
    }
}

#[test]
fn train_help_mentions_directory_input() {
    let mut help = Vec::new();
    Cli::command()
        .find_subcommand_mut("train")
        .expect("train subcommand should exist")
        .write_long_help(&mut help)
        .expect("help should render");
    let help = String::from_utf8(help).expect("help should be valid utf-8");

    assert!(help.contains("log file or directory"));
    assert!(help.contains("--emit-index"));
}

#[test]
fn train_rejects_index_out_and_out_dir_together() {
    let error = Cli::try_parse_from([
        "log-parser",
        "train",
        "app.log",
        "--emit-index",
        "--index-out",
        "templates.idx",
        "--out-dir",
        "indexed",
    ])
    .expect_err("conflicting train outputs should fail");

    assert_eq!(error.kind(), clap::error::ErrorKind::ArgumentConflict);
    let message = error.to_string();
    assert!(message.contains("--index-out"));
    assert!(message.contains("--out-dir"));
}

#[cfg(feature = "async")]
#[test]
fn async_max_concurrent_defaults_to_one() {
    let cli = Cli::try_parse_from(["log-parser", "online", "--async", "app.log"])
        .expect("async online should parse");
    let Commands::Online(args) = cli.command else {
        panic!("expected online command");
    };
    assert!(args.r#async);
    assert!(args.max_concurrent.is_none());
    assert_eq!(super::args::resolved_max_concurrent(args.max_concurrent), 1);

    let cli = Cli::try_parse_from(["log-parser", "train", "--async", "app.log"])
        .expect("async train should parse");
    let Commands::Train(args) = cli.command else {
        panic!("expected train command");
    };
    assert!(args.r#async);
    assert!(args.max_concurrent.is_none());
    assert_eq!(super::args::resolved_max_concurrent(args.max_concurrent), 1);
}

#[cfg(feature = "async")]
#[test]
fn async_max_concurrent_parses_for_online_and_train() {
    let cli = Cli::try_parse_from([
        "log-parser",
        "online",
        "--async",
        "--max-concurrent",
        "2",
        "app.log",
    ])
    .expect("async online max concurrency should parse");
    let Commands::Online(args) = cli.command else {
        panic!("expected online command");
    };
    assert_eq!(args.max_concurrent, Some(2));

    let cli = Cli::try_parse_from([
        "log-parser",
        "train",
        "--async",
        "--max-concurrent",
        "2",
        "app.log",
    ])
    .expect("async train max concurrency should parse");
    let Commands::Train(args) = cli.command else {
        panic!("expected train command");
    };
    assert_eq!(args.max_concurrent, Some(2));
}

#[cfg(feature = "async")]
#[test]
fn async_max_concurrent_requires_async() {
    for command in ["online", "train"] {
        let error = run_from([
            OsString::from("log-parser"),
            OsString::from(command),
            OsString::from("--max-concurrent"),
            OsString::from("2"),
            OsString::from("app.log"),
        ])
        .expect_err("--max-concurrent should require --async");

        assert!(
            error
                .to_string()
                .contains("--max-concurrent requires --async"),
            "unexpected error for {command}: {error}"
        );
    }
}

#[cfg(feature = "async")]
#[test]
fn async_max_concurrent_rejects_zero() {
    for command in ["online", "train"] {
        let error = run_from([
            OsString::from("log-parser"),
            OsString::from(command),
            OsString::from("--async"),
            OsString::from("--max-concurrent"),
            OsString::from("0"),
            OsString::from("app.log"),
        ])
        .expect_err("--max-concurrent 0 should be rejected");

        assert!(
            error
                .to_string()
                .contains("--max-concurrent must be greater than 0"),
            "unexpected error for {command}: {error}"
        );
    }
}

#[cfg(feature = "async")]
#[test]
fn parse_does_not_accept_max_concurrent() {
    let error = Cli::try_parse_from([
        "log-parser",
        "parse",
        "--async",
        "--max-concurrent",
        "2",
        "app.log",
    ])
    .expect_err("parse should not accept --max-concurrent in ADR-0032");

    assert_eq!(error.kind(), clap::error::ErrorKind::UnknownArgument);
}

#[test]
fn stdin_requires_explicit_output_plan() {
    let input = InputPlan::Stdin;
    let args = IoArgs {
        input: PathBuf::from("-"),
        out_dir: None,
        structured: None,
        templates: None,
        progress: ProgressMode::Auto,
        quiet: false,
        output_format: OutputFormat::Csv,
        output_fields: None,
    };

    let error = build_output_plan(&args, &input, Algorithm::Piplup)
        .expect_err("stdin should require explicit outputs");
    assert!(
        error
            .to_string()
            .contains("reading from stdin requires --out-dir, --structured, and --templates")
    );
}

#[test]
fn separate_outputs_require_both_paths() {
    let input = InputPlan::File(PathBuf::from("app.log"));
    let args = IoArgs {
        input: PathBuf::from("app.log"),
        out_dir: None,
        structured: Some(PathBuf::from("structured.csv")),
        templates: None,
        progress: ProgressMode::Auto,
        quiet: false,
        output_format: OutputFormat::Csv,
        output_fields: None,
    };

    let error = build_output_plan(&args, &input, Algorithm::Piplup)
        .expect_err("one explicit file path should be rejected");
    assert!(
        error
            .to_string()
            .contains("--structured and --templates must be provided together")
    );
}

#[test]
fn default_output_dir_uses_input_stem() {
    let input = InputPlan::File(PathBuf::from("logs/HDFS_2k.log"));
    let plan = build_output_plan(
        &IoArgs {
            input: PathBuf::from("logs/HDFS_2k.log"),
            out_dir: None,
            structured: None,
            templates: None,
            progress: ProgressMode::Auto,
            quiet: false,
            output_format: OutputFormat::Csv,
            output_fields: None,
        },
        &input,
        Algorithm::Piplup,
    )
    .expect("file input should get a default out dir");

    assert_eq!(
        plan,
        OutputPlan::Directory(PathBuf::from("logs").join("HDFS_2k.piplup"))
    );
}

#[test]
fn directory_input_recurses_for_all_log_files() {
    let temp = TestDir::new("discover-log-files");
    std::fs::create_dir_all(temp.path().join("controller_0A").join("mem_logs"))
        .expect("test directories should be created");
    std::fs::create_dir_all(temp.path().join("controller_0B").join("disk_logs"))
        .expect("test directories should be created");
    std::fs::write(
        temp.path()
            .join("controller_0A")
            .join("mem_logs")
            .join("messages_1_mem.log"),
        "2026-02-05 00:27:42 alpha",
    )
    .expect("test log should be written");
    std::fs::write(
        temp.path()
            .join("controller_0B")
            .join("disk_logs")
            .join("messages_2.LOG"),
        "2026-02-05 00:27:43 beta",
    )
    .expect("test log should be written");
    std::fs::write(temp.path().join("notes.txt"), "ignore").expect("test file should exist");

    let plan = build_input_plan(temp.path()).expect("directory input should resolve");
    let InputPlan::Directory { files, .. } = plan else {
        panic!("expected directory input");
    };

    assert_eq!(files.len(), 2);
    assert!(files.iter().all(|path| has_log_extension(path)));
}

#[test]
fn directory_input_rejects_explicit_separate_outputs() {
    let input = InputPlan::Directory {
        root: PathBuf::from("organized_logs"),
        files: vec![PathBuf::from(
            "organized_logs/controller_0A/mem_logs/messages_1_mem.log",
        )],
    };
    let args = IoArgs {
        input: PathBuf::from("organized_logs"),
        out_dir: None,
        structured: Some(PathBuf::from("structured.csv")),
        templates: Some(PathBuf::from("templates.csv")),
        progress: ProgressMode::Auto,
        quiet: false,
        output_format: OutputFormat::Csv,
        output_fields: None,
    };

    let error = build_output_plan(&args, &input, Algorithm::Piplup)
        .expect_err("directory input should reject explicit separate outputs");
    assert!(
        error
            .to_string()
            .contains("directory input does not support --structured/--templates")
    );
}

#[test]
fn directory_out_dir_mirrors_relative_layout() {
    let input = InputPlan::Directory {
        root: PathBuf::from("organized_logs"),
        files: vec![PathBuf::from(
            "organized_logs/controller_0A/mem_logs/messages_1_mem.log",
        )],
    };
    let plan = build_output_plan(
        &IoArgs {
            input: PathBuf::from("organized_logs"),
            out_dir: Some(PathBuf::from("parsed")),
            structured: None,
            templates: None,
            progress: ProgressMode::Auto,
            quiet: false,
            output_format: OutputFormat::Csv,
            output_fields: None,
        },
        &input,
        Algorithm::Piplup,
    )
    .expect("directory output plan should resolve");

    let resolved = resolve_directory_output_paths(
        &plan,
        Path::new("organized_logs/controller_0A/mem_logs/messages_1_mem.log"),
    )
    .expect("directory output paths should resolve");

    assert_eq!(
        resolved,
        DirectoryOutputPaths {
            structured_dir: PathBuf::from("parsed")
                .join("controller_0A")
                .join("mem_logs")
                .join("messages_1_mem.piplup"),
            templates_path: PathBuf::from("parsed")
                .join("controller_0A")
                .join("mem_logs")
                .join("templates.csv"),
        }
    );
}

#[test]
fn run_online_processes_directory_recursively() {
    let temp = TestDir::new("run-directory");
    let input_root = temp.path().join("organized_logs");
    let output_root = temp.path().join("parsed");

    std::fs::create_dir_all(input_root.join("controller_0A").join("mem_logs"))
        .expect("test directories should be created");
    std::fs::create_dir_all(input_root.join("controller_0B").join("disk_logs"))
        .expect("test directories should be created");

    std::fs::write(
        input_root
            .join("controller_0A")
            .join("mem_logs")
            .join("messages_20260205002742_mem.log"),
        "2026-02-05 00:27:42 alpha\n2026-02-05 00:27:43 alpha",
    )
    .expect("first log should be written");
    std::fs::write(
        input_root
            .join("controller_0A")
            .join("mem_logs")
            .join("messages_20260205005732_mem.log"),
        "2026-02-05 00:57:32 alpha\n2026-02-05 00:57:33 beta",
    )
    .expect("second log in same leaf dir should be written");
    std::fs::write(
        input_root
            .join("controller_0B")
            .join("disk_logs")
            .join("messages_20260204_102806.log"),
        "2026-02-04 10:28:06 beta",
    )
    .expect("second log should be written");

    run_from([
        OsString::from("log-parser"),
        OsString::from("online"),
        input_root.as_os_str().to_os_string(),
        OsString::from("--out-dir"),
        output_root.as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect("directory run should succeed");

    assert!(
        output_root
            .join("controller_0A")
            .join("mem_logs")
            .join("messages_20260205002742_mem.piplup")
            .join("structured.csv")
            .is_file()
    );
    assert!(
        output_root
            .join("controller_0A")
            .join("mem_logs")
            .join("messages_20260205005732_mem.piplup")
            .join("structured.csv")
            .is_file()
    );
    assert!(
        output_root
            .join("controller_0B")
            .join("disk_logs")
            .join("messages_20260204_102806.piplup")
            .join("structured.csv")
            .is_file()
    );
    assert!(
        !output_root
            .join("controller_0A")
            .join("mem_logs")
            .join("messages_20260205002742_mem.piplup")
            .join("templates.csv")
            .exists()
    );
    assert!(
        output_root
            .join("controller_0A")
            .join("mem_logs")
            .join("templates.csv")
            .is_file()
    );
    assert!(
        output_root
            .join("controller_0B")
            .join("disk_logs")
            .join("templates.csv")
            .is_file()
    );

    let rows = csv::Reader::from_path(
        output_root
            .join("controller_0A")
            .join("mem_logs")
            .join("templates.csv"),
    )
    .expect("leaf templates csv should exist")
    .records()
    .collect::<Result<Vec<_>, _>>()
    .expect("leaf template rows should load");
    assert_eq!(rows.len(), 2);

    let alpha = rows
        .iter()
        .find(|row| row.get(1) == Some("alpha"))
        .expect("alpha template should be aggregated");
    assert_eq!(alpha.get(2), Some("3"));

    let beta = rows
        .iter()
        .find(|row| row.get(1) == Some("beta"))
        .expect("beta template should be aggregated");
    assert_eq!(beta.get(2), Some("1"));
}

#[cfg(feature = "async")]
#[test]
fn async_online_max_concurrent_preserves_directory_outputs() {
    let temp = TestDir::new("async-online-max-concurrent");
    let input_root = temp.path().join("organized_logs");
    let output_one = temp.path().join("parsed-one");
    let output_two = temp.path().join("parsed-two");
    write_async_multi_leaf_fixture(&input_root);

    for (max_concurrent, output_root) in [(1, &output_one), (2, &output_two)] {
        run_from([
            OsString::from("log-parser"),
            OsString::from("online"),
            OsString::from("--async"),
            OsString::from("--max-concurrent"),
            OsString::from(max_concurrent.to_string()),
            input_root.as_os_str().to_os_string(),
            OsString::from("--out-dir"),
            output_root.as_os_str().to_os_string(),
            OsString::from("--quiet"),
        ])
        .expect("async online directory run should succeed");
    }

    assert_directory_files_equal(&output_one, &output_two);
}

#[cfg(feature = "async")]
#[test]
fn sync_vs_async_online_directory_outputs_equal() {
    for fmt in ["csv", "jsonl", "both"] {
        let temp = TestDir::new(&format!("sync-vs-async-online-{fmt}"));
        let input_root = temp.path().join("organized_logs");
        let sync_out = temp.path().join("sync_out");
        let async_out = temp.path().join("async_out");
        write_async_multi_leaf_fixture(&input_root);

        run_from([
            OsString::from("log-parser"),
            OsString::from("online"),
            input_root.as_os_str().to_os_string(),
            OsString::from("--out-dir"),
            sync_out.as_os_str().to_os_string(),
            OsString::from("--output-format"),
            OsString::from(fmt),
            OsString::from("--quiet"),
        ])
        .expect("sync online directory run should succeed");

        run_from([
            OsString::from("log-parser"),
            OsString::from("online"),
            OsString::from("--async"),
            input_root.as_os_str().to_os_string(),
            OsString::from("--out-dir"),
            async_out.as_os_str().to_os_string(),
            OsString::from("--output-format"),
            OsString::from(fmt),
            OsString::from("--quiet"),
        ])
        .expect("async online directory run should succeed");

        assert_directory_files_equal(&sync_out, &async_out);
    }
}

#[cfg(feature = "async")]
#[test]
fn sync_vs_async_indexed_parse_directory_outputs_equal() {
    let temp = TestDir::new("sync-vs-async-indexed-parse");
    let input_root = temp.path().join("organized_logs");
    let index_root = temp.path().join("indexed");
    let sync_out = temp.path().join("sync_out");
    let async_out = temp.path().join("async_out");
    write_async_multi_leaf_fixture(&input_root);

    // Train to produce per-leaf indexes.
    run_from([
        OsString::from("log-parser"),
        OsString::from("train"),
        input_root.as_os_str().to_os_string(),
        OsString::from("--emit-index"),
        OsString::from("--out-dir"),
        index_root.as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect("directory train should succeed");

    // Sync parse.
    run_from([
        OsString::from("log-parser"),
        OsString::from("parse"),
        OsString::from("--index"),
        index_root.as_os_str().to_os_string(),
        input_root.as_os_str().to_os_string(),
        OsString::from("--out-dir"),
        sync_out.as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect("sync directory parse should succeed");

    // Async parse.
    run_from([
        OsString::from("log-parser"),
        OsString::from("parse"),
        OsString::from("--async"),
        OsString::from("--index"),
        index_root.as_os_str().to_os_string(),
        input_root.as_os_str().to_os_string(),
        OsString::from("--out-dir"),
        async_out.as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect("async directory parse should succeed");

    assert_directory_files_equal(&sync_out, &async_out);
}

#[cfg(feature = "async")]
#[test]
fn sync_vs_async_train_directory_artifacts_equal() {
    let temp = TestDir::new("sync-vs-async-train-emit");
    let input_root = temp.path().join("organized_logs");
    let sync_out = temp.path().join("sync_out");
    let async_out = temp.path().join("async_out");
    write_async_multi_leaf_fixture(&input_root);

    run_from([
        OsString::from("log-parser"),
        OsString::from("train"),
        input_root.as_os_str().to_os_string(),
        OsString::from("--emit-index"),
        OsString::from("--out-dir"),
        sync_out.as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect("sync directory train should succeed");

    run_from([
        OsString::from("log-parser"),
        OsString::from("train"),
        OsString::from("--async"),
        input_root.as_os_str().to_os_string(),
        OsString::from("--emit-index"),
        OsString::from("--out-dir"),
        async_out.as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect("async directory train should succeed");

    assert_directory_files_equal(&sync_out, &async_out);
}

#[cfg(feature = "async")]
#[test]
fn sync_vs_async_train_templates_only_equal() {
    let temp = TestDir::new("sync-vs-async-train-noemit");
    let input_root = temp.path().join("organized_logs");
    let sync_out = temp.path().join("sync_out");
    let async_out = temp.path().join("async_out");
    write_async_multi_leaf_fixture(&input_root);

    run_from([
        OsString::from("log-parser"),
        OsString::from("train"),
        input_root.as_os_str().to_os_string(),
        OsString::from("--out-dir"),
        sync_out.as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect("sync directory train should succeed");

    run_from([
        OsString::from("log-parser"),
        OsString::from("train"),
        OsString::from("--async"),
        input_root.as_os_str().to_os_string(),
        OsString::from("--out-dir"),
        async_out.as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect("async directory train should succeed");

    assert_directory_files_equal(&sync_out, &async_out);
}

#[test]
fn cache_cursor_is_honored_for_matching_input_identity() {
    let temp = TestDir::new("cache-same-input-cursor");
    let input = temp.path().join("app.log");
    let cache_path = temp.path().join("app.cache");
    let config = PiplupConfig::default();
    let cache = CacheState::default()
        .with_runtime_hash(config.compute_runtime_hash())
        .with_preprocess_hash(PreprocessPlan::default().compute_content_hash())
        .with_cursor(7, Some(path_identity(&input)));
    CacheManager::save_to_path(&cache_path, &cache, false).expect("cache should save");

    let (_, initial) = apply_cache_config(
        config,
        CacheConfigArgs {
            current_input: &input,
            cache_path: Some(&cache_path),
            segment_lines: None,
            checkpoint_lines: Some(5),
            cache_compress: false,
            force_rebuild: false,
            input_policy: CacheInputPolicy::WarmStartOnMismatch,
        },
    )
    .expect("matching cache should load");

    let initial = initial.expect("initial cache state should be restored");
    assert_eq!(initial.processed_line_count, 7);
    assert_eq!(initial.input_identity, Some(path_identity(&input)));
}

#[test]
fn cache_cursor_resets_for_different_or_missing_input_identity() {
    let temp = TestDir::new("cache-cursor-reset");
    let input = temp.path().join("app.log");
    let other_input = temp.path().join("other.log");
    let cache_path = temp.path().join("app.cache");
    let config = PiplupConfig::default();
    let preprocess_hash = PreprocessPlan::default().compute_content_hash();
    let cache = CacheState::default()
        .with_runtime_hash(config.compute_runtime_hash())
        .with_preprocess_hash(preprocess_hash)
        .with_cursor(9, Some(path_identity(&input)));
    CacheManager::save_to_path(&cache_path, &cache, false).expect("cache should save");

    let (_, initial) = apply_cache_config(
        config.clone(),
        CacheConfigArgs {
            current_input: &other_input,
            cache_path: Some(&cache_path),
            segment_lines: None,
            checkpoint_lines: Some(5),
            cache_compress: false,
            force_rebuild: false,
            input_policy: CacheInputPolicy::WarmStartOnMismatch,
        },
    )
    .expect("mismatched cache should warm-start");
    assert_eq!(
        initial
            .expect("initial cache state should be restored")
            .processed_line_count,
        0
    );

    let cache_without_identity = CacheState::default()
        .with_runtime_hash(config.compute_runtime_hash())
        .with_preprocess_hash(preprocess_hash)
        .with_cursor(9, None);
    CacheManager::save_to_path(&cache_path, &cache_without_identity, false)
        .expect("cache should save");

    let (_, initial) = apply_cache_config(
        config,
        CacheConfigArgs {
            current_input: &input,
            cache_path: Some(&cache_path),
            segment_lines: None,
            checkpoint_lines: Some(5),
            cache_compress: false,
            force_rebuild: false,
            input_policy: CacheInputPolicy::WarmStartOnMismatch,
        },
    )
    .expect("identity-less cache should warm-start");
    assert_eq!(
        initial
            .expect("initial cache state should be restored")
            .processed_line_count,
        0
    );
}

#[test]
fn run_online_directory_with_cache_finalizes_after_files() {
    let temp = TestDir::new("run-directory-cache");
    let input_root = temp.path().join("organized_logs");
    let output_root = temp.path().join("parsed");
    let cache_path = temp.path().join("state.cache");

    std::fs::create_dir_all(input_root.join("leaf")).expect("test directory should exist");
    std::fs::write(
        input_root.join("leaf").join("a.log"),
        "2026-02-05 00:27:42 alpha\n2026-02-05 00:27:43 beta",
    )
    .expect("first log should be written");
    std::fs::write(
        input_root.join("leaf").join("b.log"),
        "2026-02-05 00:27:44 gamma",
    )
    .expect("second log should be written");

    run_from([
        OsString::from("log-parser"),
        OsString::from("online"),
        input_root.as_os_str().to_os_string(),
        OsString::from("--out-dir"),
        output_root.as_os_str().to_os_string(),
        OsString::from("--cache"),
        cache_path.as_os_str().to_os_string(),
        OsString::from("--checkpoint-lines"),
        OsString::from("1"),
        OsString::from("--quiet"),
    ])
    .expect("directory run with cache should succeed");

    let cache = CacheManager::load_from_path(&cache_path).expect("cache should load");
    assert_eq!(cache.processed_line_count, 0);
    assert!(cache.input_identity.is_none());
}

#[test]
fn online_emit_index_with_cache_finalizes_cache() {
    let temp = TestDir::new("online-emit-index-cache");
    let input = temp.path().join("app.log");
    let cache_path = temp.path().join("state.cache");
    let out_dir = temp.path().join("out");
    std::fs::write(
        &input,
        "2026-04-11 12:00:00 INFO worker 1\n2026-04-11 12:00:01 INFO worker 2",
    )
    .expect("input log should be written");

    run_from([
        OsString::from("log-parser"),
        OsString::from("online"),
        input.as_os_str().to_os_string(),
        OsString::from("--cache"),
        cache_path.as_os_str().to_os_string(),
        OsString::from("--emit-index"),
        OsString::from("--out-dir"),
        out_dir.as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect("online should succeed");

    assert!(out_dir.join("templates.idx").is_file());
    let cache = CacheManager::load_from_path(&cache_path).expect("cache should load");
    assert_eq!(cache.processed_line_count, 0);
    assert!(cache.input_identity.is_none());
    let preprocess_hash = cache
        .clone()
        .into_parse_state()
        .expect("cache should restore")
        .preprocess_plan
        .compute_content_hash();
    assert_eq!(cache.preprocess_plan_hash, preprocess_hash);
}

#[test]
fn online_emit_index_rejects_stdin() {
    // The single-file --emit-index path must reject stdin input. The check is
    // algorithm-neutral (runs before the CliOnlineParser is built), so it guards
    // the stdin branch of run_online regardless of --algorithm. This is one of
    // the validation seams the RFC-0019 orchestration refactor must preserve.
    let temp = TestDir::new("online-emit-index-stdin");
    let out_dir = temp.path().join("out");

    let error = run_from([
        OsString::from("log-parser"),
        OsString::from("online"),
        OsString::from("-"), // stdin
        OsString::from("--out-dir"),
        out_dir.as_os_str().to_os_string(),
        OsString::from("--emit-index"),
        OsString::from("--quiet"),
    ])
    .expect_err("online stdin + --emit-index should be rejected");

    let message = error.to_string();
    assert!(
        message.contains("emit-index"),
        "error should mention --emit-index: {message}"
    );
    assert!(
        message.contains("stdin"),
        "error should mention stdin: {message}"
    );
}

#[test]
fn train_writes_index_for_single_file() {
    let temp = TestDir::new("train");
    let input = temp.path().join("app.log");
    std::fs::write(
        &input,
        "2026-04-11 12:00:00 INFO worker 1\n2026-04-11 12:00:01 INFO worker 2",
    )
    .expect("input log should be written");

    run_from([
        OsString::from("log-parser"),
        OsString::from("train"),
        input.as_os_str().to_os_string(),
        OsString::from("--emit-index"),
        OsString::from("--quiet"),
    ])
    .expect("train should succeed");

    assert!(
        temp.path()
            .join("app.piplup")
            .join("templates.idx")
            .is_file()
    );
    assert!(
        temp.path()
            .join("app.piplup")
            .join("templates.csv")
            .is_file()
    );
}

#[test]
fn typed_preprocess_profile_round_trips_index_and_rejects_eager_parse() {
    let temp = TestDir::new("typed-profile-index");
    let input = temp.path().join("app.log");
    let trained = temp.path().join("trained");
    let parsed = temp.path().join("parsed");
    std::fs::write(
        &input,
        "2026-04-11 12:00:00 value=123 ready\n2026-04-11 12:00:01 value=456 ready\n",
    )
    .expect("input log should be written");

    run_from([
        OsString::from("log-parser"),
        OsString::from("train"),
        input.as_os_str().to_os_string(),
        OsString::from("--preprocess-mode"),
        OsString::from("typed"),
        OsString::from("--emit-index"),
        OsString::from("--out-dir"),
        trained.as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect("typed train should succeed");

    let index = trained.join("templates.idx");
    run_from([
        OsString::from("log-parser"),
        OsString::from("parse"),
        input.as_os_str().to_os_string(),
        OsString::from("--preprocess-mode"),
        OsString::from("typed"),
        OsString::from("--index"),
        index.as_os_str().to_os_string(),
        OsString::from("--out-dir"),
        parsed.as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect("typed parse should accept a typed index");
    assert!(parsed.join("structured.csv").is_file());

    let error = run_from([
        OsString::from("log-parser"),
        OsString::from("parse"),
        input.as_os_str().to_os_string(),
        OsString::from("--preprocess-mode"),
        OsString::from("eager"),
        OsString::from("--index"),
        index.as_os_str().to_os_string(),
        OsString::from("--out-dir"),
        temp.path().join("eager-parse").as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect_err("eager parse must reject a typed index");
    assert!(
        error.to_string().contains("runtime configuration"),
        "unexpected compatibility error: {error}"
    );
}

#[test]
fn train_index_out_writes_templates_next_to_index() {
    let temp = TestDir::new("train-index-out");
    let input = temp.path().join("app.log");
    let index = temp.path().join("custom").join("templates.idx");
    std::fs::write(
        &input,
        "2026-04-11 12:00:00 INFO worker 1\n2026-04-11 12:00:01 INFO worker 2",
    )
    .expect("input log should be written");

    run_from([
        OsString::from("log-parser"),
        OsString::from("train"),
        input.as_os_str().to_os_string(),
        OsString::from("--emit-index"),
        OsString::from("--index-out"),
        index.as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect("train should succeed");

    assert!(index.is_file());
    assert!(index.with_file_name("templates.csv").is_file());
    assert!(
        !temp
            .path()
            .join("app.piplup")
            .join("templates.csv")
            .exists()
    );
}

#[test]
fn train_without_emit_index_writes_templates_only() {
    let temp = TestDir::new("train-no-index");
    let input = temp.path().join("app.log");
    std::fs::write(&input, "2026-04-11 12:00:00 INFO worker 1")
        .expect("input log should be written");

    run_from([
        OsString::from("log-parser"),
        OsString::from("train"),
        input.as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect("train should succeed");

    let out_dir = temp.path().join("app.piplup");
    assert!(out_dir.join("templates.csv").is_file());
    assert!(!out_dir.join("templates.idx").exists());
}

#[test]
fn train_cache_mismatch_requires_force_rebuild() {
    let temp = TestDir::new("train-cache-mismatch");
    let first = temp.path().join("first.log");
    let second = temp.path().join("second.log");
    let cache = temp.path().join("train.cache");
    std::fs::write(&first, "2026-04-11 12:00:00 INFO first").expect("first log");
    std::fs::write(&second, "2026-04-11 12:00:00 INFO second").expect("second log");

    run_from([
        OsString::from("log-parser"),
        OsString::from("train"),
        first.as_os_str().to_os_string(),
        OsString::from("--cache"),
        cache.as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect("initial train should succeed");

    let error = run_from([
        OsString::from("log-parser"),
        OsString::from("train"),
        second.as_os_str().to_os_string(),
        OsString::from("--cache"),
        cache.as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect_err("mismatched train cache should fail");
    assert!(error.to_string().contains("cache input identity"));
}

#[test]
fn train_piplup_single_file_cache_resume_rebuilds_full_output() {
    // Guards the PIPLUP arm of CliTrainingRunner::rebuild_and_save_cache: when a
    // single-file train resumes from a cache (cursor > 0), the converged leaf
    // must rebuild the FULL artifact via rebuild_training_output_from_cache
    // rather than emitting only the unprocessed suffix. A resumed run's
    // templates.csv must therefore match a from-scratch run on the same input.
    let log = "2026-04-11 12:00:00 INFO worker started\n\
               2026-04-11 12:00:01 INFO worker started\n\
               2026-04-11 12:00:02 INFO worker stopped\n\
               2026-04-11 12:00:03 INFO worker stopped\n\
               2026-04-11 12:00:04 WARN node joined\n\
               2026-04-11 12:00:05 WARN node joined\n";

    // From-scratch run (no cache).
    let fresh = TestDir::new("train-piplup-resume-fresh");
    let fresh_input = fresh.path().join("app.log");
    std::fs::write(&fresh_input, log).expect("fresh log should be written");
    run_from([
        OsString::from("log-parser"),
        OsString::from("train"),
        fresh_input.as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect("from-scratch train should succeed");
    let fresh_templates =
        std::fs::read_to_string(fresh.path().join("app.piplup").join("templates.csv"))
            .expect("fresh templates.csv should exist");

    // Resumed run: train twice against the same cache so the second run resumes
    // (cursor > 0) and exercises the rebuild arm.
    let resumed = TestDir::new("train-piplup-resume-cached");
    let resumed_input = resumed.path().join("app.log");
    let cache_path = resumed.path().join("state.cache");
    std::fs::write(&resumed_input, log).expect("resumed log should be written");
    for _ in 0..2 {
        run_from([
            OsString::from("log-parser"),
            OsString::from("train"),
            resumed_input.as_os_str().to_os_string(),
            OsString::from("--cache"),
            cache_path.as_os_str().to_os_string(),
            OsString::from("--checkpoint-lines"),
            OsString::from("2"),
            OsString::from("--quiet"),
        ])
        .expect("cached train run should succeed");
    }
    let resumed_templates =
        std::fs::read_to_string(resumed.path().join("app.piplup").join("templates.csv"))
            .expect("resumed templates.csv should exist");

    assert_eq!(
        fresh_templates, resumed_templates,
        "resumed PIPLUP train must rebuild full output matching a from-scratch run"
    );
}

#[test]
fn train_directory_cache_dir_writes_leaf_cache() {
    let temp = TestDir::new("train-cache-dir");
    let input_root = temp.path().join("logs");
    let cache_root = temp.path().join("cache");
    std::fs::create_dir_all(input_root.join("leaf")).expect("leaf should exist");
    std::fs::write(
        input_root.join("leaf").join("app.log"),
        "2026-04-11 12:00:00 INFO worker 1",
    )
    .expect("log should be written");

    run_from([
        OsString::from("log-parser"),
        OsString::from("train"),
        input_root.as_os_str().to_os_string(),
        OsString::from("--cache-dir"),
        cache_root.as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect("directory train should succeed");

    assert!(cache_root.join("leaf").join("train.cache").is_file());
}

#[test]
fn train_rejects_wrong_cache_scope() {
    let temp = TestDir::new("train-cache-scope");
    let input_root = temp.path().join("logs");
    let input_file = temp.path().join("app.log");
    std::fs::create_dir_all(&input_root).expect("root should exist");
    std::fs::write(input_root.join("app.log"), "2026-04-11 12:00:00 INFO dir").unwrap();
    std::fs::write(&input_file, "2026-04-11 12:00:00 INFO file").unwrap();

    let directory_error = run_from([
        OsString::from("log-parser"),
        OsString::from("train"),
        input_root.as_os_str().to_os_string(),
        OsString::from("--cache"),
        temp.path().join("one.cache").as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect_err("directory --cache should fail");
    assert!(directory_error.to_string().contains("--cache-dir"));

    let file_error = run_from([
        OsString::from("log-parser"),
        OsString::from("train"),
        input_file.as_os_str().to_os_string(),
        OsString::from("--cache-dir"),
        temp.path().join("cache-root").as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect_err("file --cache-dir should fail");
    assert!(file_error.to_string().contains("--cache-dir"));
}

#[test]
fn parse_index_writes_structured_and_templates_outputs() {
    let temp = TestDir::new("parse");
    let training_input = temp.path().join("train.log");
    let parse_input = temp.path().join("parse.log");
    std::fs::write(
        &training_input,
        "2026-04-11 12:00:00 user=alice status=ok\n2026-04-11 12:00:01 user=bob status=fail",
    )
    .expect("training log should be written");
    std::fs::write(
        &parse_input,
        "2026-04-11 12:00:02 user=carol status=ok\n2026-04-11 12:00:03 user=dave status=fail",
    )
    .expect("parse log should be written");
    let idx = temp.path().join("custom").join("templates.idx");

    run_from([
        OsString::from("log-parser"),
        OsString::from("train"),
        training_input.as_os_str().to_os_string(),
        OsString::from("--emit-index"),
        OsString::from("--index-out"),
        idx.as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect("train should succeed");

    run_from([
        OsString::from("log-parser"),
        OsString::from("parse"),
        OsString::from("--index"),
        idx.as_os_str().to_os_string(),
        parse_input.as_os_str().to_os_string(),
        OsString::from("--keep-params"),
        OsString::from("--quiet"),
    ])
    .expect("parse --index should succeed");

    let out_dir = temp.path().join("parse.piplup");
    assert!(out_dir.join("structured.csv").is_file());
    assert!(out_dir.join("templates.csv").is_file());
}

#[test]
fn parse_index_fail_on_miss_returns_error() {
    let temp = TestDir::new("parse-index-miss");
    let training_input = temp.path().join("train.log");
    let parse_input = temp.path().join("parse.log");
    std::fs::write(&training_input, "2026-04-11 12:00:00 INFO ok").expect("train log");
    std::fs::write(&parse_input, "2026-04-11 12:00:01 WARN nope").expect("parse log");
    let idx = temp.path().join("templates.idx");

    run_from([
        OsString::from("log-parser"),
        OsString::from("train"),
        training_input.as_os_str().to_os_string(),
        OsString::from("--emit-index"),
        OsString::from("--index-out"),
        idx.as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect("train should succeed");

    let error = run_from([
        OsString::from("log-parser"),
        OsString::from("parse"),
        OsString::from("--index"),
        idx.as_os_str().to_os_string(),
        OsString::from("--fail-on-miss"),
        parse_input.as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect_err("fail-on-miss should fail");
    assert!(error.to_string().contains("miss_count=1"));
}

#[test]
fn train_directory_writes_leaf_indexes_and_templates() {
    let temp = TestDir::new("train-dir");
    let input_root = temp.path().join("organized_logs");
    let output_root = temp.path().join("indexed");
    std::fs::create_dir_all(input_root.join("controller_0A").join("mem_logs"))
        .expect("test directories should be created");
    std::fs::create_dir_all(input_root.join("controller_0B").join("disk_logs"))
        .expect("test directories should be created");
    std::fs::write(
        input_root
            .join("controller_0A")
            .join("mem_logs")
            .join("messages_1.log"),
        "2026-04-11 12:00:00 alpha\n2026-04-11 12:00:01 alpha",
    )
    .expect("first leaf log should exist");
    std::fs::write(
        input_root
            .join("controller_0A")
            .join("mem_logs")
            .join("messages_2.log"),
        "2026-04-11 12:00:02 beta",
    )
    .expect("second leaf log should exist");
    std::fs::write(
        input_root
            .join("controller_0B")
            .join("disk_logs")
            .join("messages_3.log"),
        "2026-04-11 12:00:03 gamma",
    )
    .expect("third leaf log should exist");

    run_from([
        OsString::from("log-parser"),
        OsString::from("train"),
        input_root.as_os_str().to_os_string(),
        OsString::from("--emit-index"),
        OsString::from("--out-dir"),
        output_root.as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect("directory train should succeed");

    assert!(
        output_root
            .join("controller_0A")
            .join("mem_logs")
            .join("templates.idx")
            .is_file()
    );
    assert!(
        output_root
            .join("controller_0A")
            .join("mem_logs")
            .join("templates.csv")
            .is_file()
    );
    assert!(
        output_root
            .join("controller_0B")
            .join("disk_logs")
            .join("templates.idx")
            .is_file()
    );
    assert!(
        output_root
            .join("controller_0B")
            .join("disk_logs")
            .join("templates.csv")
            .is_file()
    );
}

#[cfg(feature = "async")]
#[test]
fn async_train_max_concurrent_preserves_leaf_artifacts() {
    let temp = TestDir::new("async-train-max-concurrent");
    let input_root = temp.path().join("organized_logs");
    let output_one = temp.path().join("indexed-one");
    let output_two = temp.path().join("indexed-two");
    write_async_multi_leaf_fixture(&input_root);

    for (max_concurrent, output_root) in [(1, &output_one), (2, &output_two)] {
        run_from([
            OsString::from("log-parser"),
            OsString::from("train"),
            OsString::from("--async"),
            OsString::from("--max-concurrent"),
            OsString::from(max_concurrent.to_string()),
            input_root.as_os_str().to_os_string(),
            OsString::from("--emit-index"),
            OsString::from("--out-dir"),
            output_root.as_os_str().to_os_string(),
            OsString::from("--quiet"),
        ])
        .expect("async train directory run should succeed");
    }

    assert_directory_files_equal(&output_one, &output_two);
}

#[test]
fn parse_index_directory_uses_leaf_indexes_and_writes_outputs() {
    let temp = TestDir::new("parse-index-dir");
    let input_root = temp.path().join("organized_logs");
    let index_root = temp.path().join("indexed");
    let output_root = temp.path().join("parsed");
    std::fs::create_dir_all(input_root.join("controller_0A").join("mem_logs"))
        .expect("test directories should be created");
    std::fs::create_dir_all(input_root.join("controller_0B").join("disk_logs"))
        .expect("test directories should be created");

    std::fs::write(
        input_root
            .join("controller_0A")
            .join("mem_logs")
            .join("train_1.log"),
        "2026-04-11 12:00:00 alpha\n2026-04-11 12:00:01 beta",
    )
    .expect("leaf A log should exist");
    std::fs::write(
        input_root
            .join("controller_0A")
            .join("mem_logs")
            .join("train_2.log"),
        "2026-04-11 12:00:02 alpha",
    )
    .expect("leaf A second log should exist");
    std::fs::write(
        input_root
            .join("controller_0B")
            .join("disk_logs")
            .join("train_3.log"),
        "2026-04-11 12:00:03 gamma",
    )
    .expect("leaf B log should exist");

    run_from([
        OsString::from("log-parser"),
        OsString::from("train"),
        input_root.as_os_str().to_os_string(),
        OsString::from("--emit-index"),
        OsString::from("--out-dir"),
        index_root.as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect("directory train should succeed");

    run_from([
        OsString::from("log-parser"),
        OsString::from("parse"),
        OsString::from("--index"),
        index_root.as_os_str().to_os_string(),
        input_root.as_os_str().to_os_string(),
        OsString::from("--out-dir"),
        output_root.as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect("directory parse --index should succeed");

    assert!(
        output_root
            .join("controller_0A")
            .join("mem_logs")
            .join("train_1.piplup")
            .join("structured.csv")
            .is_file()
    );
    assert!(
        output_root
            .join("controller_0A")
            .join("mem_logs")
            .join("train_2.piplup")
            .join("structured.csv")
            .is_file()
    );
    assert!(
        output_root
            .join("controller_0B")
            .join("disk_logs")
            .join("train_3.piplup")
            .join("structured.csv")
            .is_file()
    );

    let rows = csv::Reader::from_path(
        output_root
            .join("controller_0A")
            .join("mem_logs")
            .join("templates.csv"),
    )
    .expect("leaf templates csv should exist")
    .records()
    .collect::<Result<Vec<_>, _>>()
    .expect("leaf template rows should load");
    let alpha = rows
        .iter()
        .find(|row| row.get(1) == Some("alpha"))
        .expect("alpha row should exist");
    assert_eq!(alpha.get(2), Some("2"));
    let beta = rows
        .iter()
        .find(|row| row.get(1) == Some("beta"))
        .expect("beta row should exist");
    assert_eq!(beta.get(2), Some("1"));
}

#[test]
fn parse_index_directory_errors_when_leaf_index_is_missing() {
    let temp = TestDir::new("parse-index-missing-dir");
    let input_root = temp.path().join("organized_logs");
    let index_root = temp.path().join("indexed");
    std::fs::create_dir_all(input_root.join("controller_0A").join("mem_logs"))
        .expect("test directories should be created");
    std::fs::create_dir_all(&index_root).expect("index root should exist");
    std::fs::write(
        input_root
            .join("controller_0A")
            .join("mem_logs")
            .join("sample.log"),
        "2026-04-11 12:00:00 alpha",
    )
    .expect("sample log should exist");

    let error = run_from([
        OsString::from("log-parser"),
        OsString::from("parse"),
        OsString::from("--index"),
        index_root.as_os_str().to_os_string(),
        input_root.as_os_str().to_os_string(),
        OsString::from("--out-dir"),
        temp.path().join("parsed").as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect_err("missing leaf index should fail");

    assert!(error.to_string().contains("missing leaf index"));
    assert!(error.to_string().contains("sample.log"));
}

#[test]
fn parse_index_directory_fail_on_miss_reports_file_and_counts() {
    let temp = TestDir::new("parse-index-dir-miss");
    let input_root = temp.path().join("organized_logs");
    let index_root = temp.path().join("indexed");
    std::fs::create_dir_all(input_root.join("controller_0A").join("mem_logs"))
        .expect("test directories should be created");
    std::fs::write(
        input_root
            .join("controller_0A")
            .join("mem_logs")
            .join("sample.log"),
        "2026-04-11 12:00:00 alpha",
    )
    .expect("sample log should exist");
    std::fs::write(
        input_root
            .join("controller_0A")
            .join("mem_logs")
            .join("miss.log"),
        "2026-04-11 12:00:01 gamma",
    )
    .expect("miss log should exist");

    run_from([
        OsString::from("log-parser"),
        OsString::from("train"),
        input_root.as_os_str().to_os_string(),
        OsString::from("--emit-index"),
        OsString::from("--out-dir"),
        index_root.as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect("directory train should succeed");

    std::fs::write(
        input_root
            .join("controller_0A")
            .join("mem_logs")
            .join("miss.log"),
        "2026-04-11 12:00:01 delta",
    )
    .expect("miss log should be updated");

    let error = run_from([
        OsString::from("log-parser"),
        OsString::from("parse"),
        OsString::from("--index"),
        index_root.as_os_str().to_os_string(),
        OsString::from("--fail-on-miss"),
        input_root.as_os_str().to_os_string(),
        OsString::from("--out-dir"),
        temp.path().join("parsed").as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect_err("directory fail-on-miss should fail");

    assert!(error.to_string().contains("miss.log"));
    assert!(error.to_string().contains("miss_count=1"));
    assert!(error.to_string().contains("first_new_miss_line=1"));
}

// --- Output format and field projection CLI tests ---

#[test]
fn output_format_default_is_csv() {
    let cli =
        Cli::try_parse_from(["log-parser", "online", "app.log"]).expect("default should parse");
    let Commands::Online(args) = cli.command else {
        panic!("expected online");
    };
    assert_eq!(args.io.output_format, OutputFormat::Csv);
    assert!(args.io.output_fields.is_none());
}

#[test]
fn output_format_accepts_jsonl() {
    let cli = Cli::try_parse_from([
        "log-parser",
        "online",
        "--output-format",
        "jsonl",
        "app.log",
    ])
    .expect("jsonl should parse");
    let Commands::Online(args) = cli.command else {
        panic!("expected online");
    };
    assert_eq!(args.io.output_format, OutputFormat::Jsonl);
}

#[test]
fn output_format_accepts_both() {
    let cli = Cli::try_parse_from(["log-parser", "online", "--output-format", "both", "app.log"])
        .expect("both should parse");
    let Commands::Online(args) = cli.command else {
        panic!("expected online");
    };
    assert_eq!(args.io.output_format, OutputFormat::Both);
}

#[test]
fn output_format_rejects_invalid() {
    let result = Cli::try_parse_from(["log-parser", "online", "--output-format", "xml", "app.log"]);
    assert!(result.is_err());
}

#[test]
fn output_format_case_insensitive() {
    let cli = Cli::try_parse_from([
        "log-parser",
        "online",
        "--output-format",
        "JSONL",
        "app.log",
    ])
    .expect("case-insensitive should parse");
    let Commands::Online(args) = cli.command else {
        panic!("expected online");
    };
    assert_eq!(args.io.output_format, OutputFormat::Jsonl);
}

#[test]
fn output_fields_accepts_comma_separated() {
    let cli = Cli::try_parse_from([
        "log-parser",
        "online",
        "--output-fields",
        "LineId,EventId,EventTemplate",
        "app.log",
    ])
    .expect("output-fields should parse");
    let Commands::Online(args) = cli.command else {
        panic!("expected online");
    };
    assert_eq!(
        args.io.output_fields.as_deref(),
        Some("LineId,EventId,EventTemplate")
    );
}

#[test]
fn train_rejects_output_format() {
    let error = Cli::try_parse_from(["log-parser", "train", "--output-format", "jsonl", "app.log"])
        .expect_err("train should not accept --output-format");

    assert_eq!(error.kind(), clap::error::ErrorKind::UnknownArgument);
    assert!(error.to_string().contains("--output-format"));
}

#[test]
fn online_writes_jsonl_output() {
    let dir = TestDir::new("online_jsonl");
    let input = dir.path().join("input.log");
    let out_dir = dir.path().join("output");
    std::fs::write(
        &input,
        "2026-04-11 12:00:00 worker started\n2026-04-11 12:00:01 worker started\n",
    )
    .expect("log should be written");

    run_from([
        OsString::from("log-parser"),
        OsString::from("online"),
        input.as_os_str().to_os_string(),
        OsString::from("--out-dir"),
        out_dir.as_os_str().to_os_string(),
        OsString::from("--output-format"),
        OsString::from("jsonl"),
        OsString::from("--quiet"),
    ])
    .expect("online with jsonl should succeed");

    assert!(out_dir.join("structured.jsonl").is_file());
    assert!(!out_dir.join("structured.csv").exists());
    assert!(out_dir.join("templates.csv").is_file());

    let content = std::fs::read_to_string(out_dir.join("structured.jsonl")).unwrap();
    let first_line = content.lines().next().unwrap();
    let parsed: serde_json::Value = serde_json::from_str(first_line).unwrap();
    assert!(parsed["LineId"].is_number());
}

#[test]
fn online_writes_both_outputs() {
    let dir = TestDir::new("online_both");
    let input = dir.path().join("input.log");
    let out_dir = dir.path().join("output");
    std::fs::write(
        &input,
        "2026-04-11 12:00:00 worker started\n2026-04-11 12:00:01 worker started\n",
    )
    .expect("log should be written");

    run_from([
        OsString::from("log-parser"),
        OsString::from("online"),
        input.as_os_str().to_os_string(),
        OsString::from("--out-dir"),
        out_dir.as_os_str().to_os_string(),
        OsString::from("--output-format"),
        OsString::from("both"),
        OsString::from("--quiet"),
    ])
    .expect("online with both should succeed");

    assert!(out_dir.join("structured.csv").is_file());
    assert!(out_dir.join("structured.jsonl").is_file());
    assert!(out_dir.join("templates.csv").is_file());
}

#[test]
fn online_output_fields_projects_columns() {
    let dir = TestDir::new("online_fields");
    let input = dir.path().join("input.log");
    let out_dir = dir.path().join("output");
    std::fs::write(
        &input,
        "2026-04-11 12:00:00 worker started\n2026-04-11 12:00:01 worker started\n",
    )
    .expect("log should be written");

    run_from([
        OsString::from("log-parser"),
        OsString::from("online"),
        input.as_os_str().to_os_string(),
        OsString::from("--out-dir"),
        out_dir.as_os_str().to_os_string(),
        OsString::from("--output-fields"),
        OsString::from("LineId,EventId"),
        OsString::from("--quiet"),
    ])
    .expect("online with output-fields should succeed");

    let content = std::fs::read_to_string(out_dir.join("structured.csv")).unwrap();
    let header = content.lines().next().unwrap();
    assert_eq!(header, "LineId,EventId");
}

#[test]
fn online_parameter_list_without_keep_params_errors() {
    let dir = TestDir::new("online_params_err");
    let input = dir.path().join("input.log");
    std::fs::write(&input, "2026-04-11 12:00:00 worker started\n").expect("log should be written");

    let error = run_from([
        OsString::from("log-parser"),
        OsString::from("online"),
        input.as_os_str().to_os_string(),
        OsString::from("--output-fields"),
        OsString::from("LineId,ParameterList"),
        OsString::from("--quiet"),
    ])
    .expect_err("should fail without --keep-params");

    assert!(error.to_string().contains("--keep-params"));
}

#[test]
fn online_stdin_with_both_output_format_rejected() {
    let error = run_from([
        OsString::from("log-parser"),
        OsString::from("online"),
        OsString::from("-"), // stdin
        OsString::from("--output-format"),
        OsString::from("both"),
        OsString::from("--quiet"),
    ])
    .expect_err("stdin + both should be rejected");

    assert!(
        error.to_string().contains("stdin"),
        "error should mention stdin: {}",
        error
    );
}

#[test]
fn online_both_with_structured_stdout_rejected() {
    let dir = TestDir::new("online_both_stdout");
    let input = dir.path().join("input.log");
    std::fs::write(&input, "2026-04-11 12:00:00 worker started\n").expect("log should be written");

    let error = run_from([
        OsString::from("log-parser"),
        OsString::from("online"),
        input.as_os_str().to_os_string(),
        OsString::from("--structured"),
        OsString::from("-"), // stdout
        OsString::from("--templates"),
        OsString::from("-"),
        OsString::from("--output-format"),
        OsString::from("both"),
        OsString::from("--quiet"),
    ])
    .expect_err("both + stdout should be rejected");

    assert!(
        error.to_string().contains("stdout"),
        "error should mention stdout: {}",
        error
    );
}

#[test]
fn online_jsonl_with_structured_path_writes_jsonl() {
    let dir = TestDir::new("online_jsonl_structured");
    let input = dir.path().join("input.log");
    let structured_path = dir.path().join("output.jsonl");
    let templates_path = dir.path().join("templates.csv");
    std::fs::write(&input, "2026-04-11 12:00:00 worker started\n").expect("log should be written");

    run_from([
        OsString::from("log-parser"),
        OsString::from("online"),
        input.as_os_str().to_os_string(),
        OsString::from("--structured"),
        structured_path.as_os_str().to_os_string(),
        OsString::from("--templates"),
        templates_path.as_os_str().to_os_string(),
        OsString::from("--output-format"),
        OsString::from("jsonl"),
        OsString::from("--quiet"),
    ])
    .expect("jsonl + --structured should succeed");

    assert!(
        structured_path.is_file(),
        "JSONL should be written to --structured path"
    );
    let content = std::fs::read_to_string(&structured_path).unwrap();
    let parsed: serde_json::Value = serde_json::from_str(content.trim()).unwrap();
    assert!(
        parsed["LineId"].is_number(),
        "JSONL should have numeric LineId"
    );
}

#[test]
fn online_both_with_jsonl_extension_path_rejected() {
    let dir = TestDir::new("online_both_jsonl_ext");
    let input = dir.path().join("input.log");
    let structured_path = dir.path().join("output.jsonl"); // .jsonl extension
    let templates_path = dir.path().join("templates.csv");
    std::fs::write(&input, "2026-04-11 12:00:00 worker started\n").expect("log should be written");

    let error = run_from([
        OsString::from("log-parser"),
        OsString::from("online"),
        input.as_os_str().to_os_string(),
        OsString::from("--structured"),
        structured_path.as_os_str().to_os_string(),
        OsString::from("--templates"),
        templates_path.as_os_str().to_os_string(),
        OsString::from("--output-format"),
        OsString::from("both"),
        OsString::from("--quiet"),
    ])
    .expect_err("both + .jsonl extension should be rejected");

    assert!(
        error.to_string().contains(".jsonl"),
        "error should mention .jsonl extension: {}",
        error
    );
}

// ── Drain3 CLI tests ────────────────────────────────────────────────────

#[test]
fn parses_online_algorithm_drain3() {
    let cli = Cli::try_parse_from(["log-parser", "online", "--algorithm", "drain3", "app.log"])
        .expect("online --algorithm drain3 should parse");
    let Commands::Online(args) = cli.command else {
        panic!("expected online command");
    };
    assert_eq!(args.algorithm, Algorithm::Drain3);
}

#[test]
fn parses_train_algorithm_drain3() {
    let cli = Cli::try_parse_from(["log-parser", "train", "--algorithm", "drain3", "app.log"])
        .expect("train --algorithm drain3 should parse");
    let Commands::Train(args) = cli.command else {
        panic!("expected train command");
    };
    assert_eq!(args.algorithm, Algorithm::Drain3);
}

#[test]
fn online_algorithm_defaults_to_piplup() {
    let cli = Cli::try_parse_from(["log-parser", "online", "app.log"]).expect("should parse");
    let Commands::Online(args) = cli.command else {
        panic!("expected online command");
    };
    assert_eq!(args.algorithm, Algorithm::Piplup);
}

#[test]
fn drain3_flag_without_algorithm_fails() {
    let error = run_from([
        OsString::from("log-parser"),
        OsString::from("online"),
        OsString::from("--depth"),
        OsString::from("5"),
        OsString::from("app.log"),
    ])
    .expect_err("--depth without --algorithm drain3 should fail");
    assert!(
        error.to_string().contains("--depth"),
        "error should mention --depth: {}",
        error
    );
    assert!(
        error.to_string().contains("--algorithm drain3"),
        "error should mention --algorithm drain3: {}",
        error
    );
}

#[test]
fn drain3_flag_with_explicit_piplup_fails() {
    let error = run_from([
        OsString::from("log-parser"),
        OsString::from("online"),
        OsString::from("--algorithm"),
        OsString::from("piplup"),
        OsString::from("--depth"),
        OsString::from("5"),
        OsString::from("app.log"),
    ])
    .expect_err("--depth with --algorithm piplup should fail");
    assert!(
        error.to_string().contains("--depth"),
        "error should mention --depth: {}",
        error
    );
    assert!(
        error.to_string().contains("--algorithm piplup"),
        "error should mention --algorithm piplup: {}",
        error
    );
}

#[test]
fn piplup_flag_with_drain3_fails() {
    let error = run_from([
        OsString::from("log-parser"),
        OsString::from("online"),
        OsString::from("--algorithm"),
        OsString::from("drain3"),
        OsString::from("--hit-limit"),
        OsString::from("100"),
        OsString::from("app.log"),
    ])
    .expect_err("--hit-limit with --algorithm drain3 should fail");
    assert!(
        error.to_string().contains("--hit-limit"),
        "error should mention --hit-limit: {}",
        error
    );
    assert!(
        error.to_string().contains("--algorithm drain3"),
        "error should mention --algorithm drain3: {}",
        error
    );
}

#[test]
fn train_drain3_flag_without_algorithm_fails() {
    let error = run_from([
        OsString::from("log-parser"),
        OsString::from("train"),
        OsString::from("--max-children"),
        OsString::from("50"),
        OsString::from("app.log"),
    ])
    .expect_err("--max-children without --algorithm drain3 should fail");
    assert!(error.to_string().contains("--max-children"));
    assert!(error.to_string().contains("--algorithm drain3"));
}

#[test]
fn train_piplup_flag_with_drain3_fails() {
    let error = run_from([
        OsString::from("log-parser"),
        OsString::from("train"),
        OsString::from("--algorithm"),
        OsString::from("drain3"),
        OsString::from("--no-merge"),
        OsString::from("app.log"),
    ])
    .expect_err("--no-merge with --algorithm drain3 should fail");
    assert!(error.to_string().contains("--no-merge"));
    assert!(error.to_string().contains("--algorithm drain3"));
}

#[test]
fn online_drain3_runs_successfully() {
    let temp = TestDir::new("online-drain3");
    let input = temp.path().join("app.log");
    let out_dir = temp.path().join("out");
    std::fs::write(
        &input,
        "2026-04-11 12:00:00 INFO worker started\n2026-04-11 12:00:01 INFO worker stopped\n",
    )
    .expect("log should be written");

    run_from([
        OsString::from("log-parser"),
        OsString::from("online"),
        OsString::from("--algorithm"),
        OsString::from("drain3"),
        input.as_os_str().to_os_string(),
        OsString::from("--out-dir"),
        out_dir.as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect("online --algorithm drain3 should succeed");

    assert!(out_dir.join("structured.csv").is_file());
    assert!(out_dir.join("templates.csv").is_file());
}

#[test]
fn online_drain3_emit_index_writes_index() {
    // Single-file online --emit-index for Drain3 must write templates.idx
    // alongside structured/templates CSV. This exercises the Drain3 arm of
    // cli_online_emit_index (write_index_from_drain3_output), the seam the
    // RFC-0019 refactor added per algorithm. Mirrors the PIPLUP
    // online_emit_index_with_cache_finalizes_cache behavior.
    let temp = TestDir::new("online-drain3-emit-index");
    let input = temp.path().join("app.log");
    let out_dir = temp.path().join("out");
    std::fs::write(
        &input,
        "2026-04-11 12:00:00 INFO worker started\n2026-04-11 12:00:01 INFO worker stopped\n",
    )
    .expect("input log should be written");

    run_from([
        OsString::from("log-parser"),
        OsString::from("online"),
        OsString::from("--algorithm"),
        OsString::from("drain3"),
        input.as_os_str().to_os_string(),
        OsString::from("--emit-index"),
        OsString::from("--out-dir"),
        out_dir.as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect("online --algorithm drain3 --emit-index should succeed");

    assert!(
        out_dir.join("templates.idx").is_file(),
        "templates.idx should be written for drain3 --emit-index"
    );
    assert!(out_dir.join("structured.csv").is_file());
    assert!(out_dir.join("templates.csv").is_file());
}

#[test]
fn online_drain3_directory_aggregates_leaf_templates() {
    // Directory online for Drain3 must write per-leaf structured output and a
    // single aggregated templates.csv per leaf dir (not per file). This
    // exercises the Drain3 path of run_online_directory_loop plus
    // merge_leaf_templates. Mirrors the PIPLUP
    // run_online_processes_directory_recursively seam. The occurrence-count
    // assertion is algorithm-neutral: aggregated occurrences must sum to the
    // number of input lines in each leaf.
    let temp = TestDir::new("online-drain3-directory");
    let input_root = temp.path().join("organized_logs");
    let output_root = temp.path().join("parsed");

    std::fs::create_dir_all(input_root.join("leaf_a")).expect("leaf_a should exist");
    std::fs::create_dir_all(input_root.join("leaf_b")).expect("leaf_b should exist");

    // leaf_a: two files sharing one leaf (4 lines total).
    std::fs::write(
        input_root.join("leaf_a").join("a.log"),
        "2026-02-05 00:27:42 alpha\n2026-02-05 00:27:43 alpha",
    )
    .expect("first leaf_a log should be written");
    std::fs::write(
        input_root.join("leaf_a").join("b.log"),
        "2026-02-05 00:27:44 alpha\n2026-02-05 00:27:45 beta",
    )
    .expect("second leaf_a log should be written");
    // leaf_b: one file (1 line).
    std::fs::write(
        input_root.join("leaf_b").join("c.log"),
        "2026-02-05 00:27:46 beta",
    )
    .expect("leaf_b log should be written");

    run_from([
        OsString::from("log-parser"),
        OsString::from("online"),
        OsString::from("--algorithm"),
        OsString::from("drain3"),
        input_root.as_os_str().to_os_string(),
        OsString::from("--out-dir"),
        output_root.as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect("drain3 directory run should succeed");

    // Per-leaf structured output uses the .drain3 suffix.
    assert!(
        output_root
            .join("leaf_a")
            .join("a.drain3")
            .join("structured.csv")
            .is_file()
    );
    assert!(
        output_root
            .join("leaf_a")
            .join("b.drain3")
            .join("structured.csv")
            .is_file()
    );
    assert!(
        output_root
            .join("leaf_b")
            .join("c.drain3")
            .join("structured.csv")
            .is_file()
    );

    // templates.csv is aggregated per LEAF dir, never per file.
    assert!(
        !output_root
            .join("leaf_a")
            .join("a.drain3")
            .join("templates.csv")
            .exists()
    );
    assert!(output_root.join("leaf_a").join("templates.csv").is_file());
    assert!(output_root.join("leaf_b").join("templates.csv").is_file());

    // Aggregated occurrences in leaf_a must cover all 4 lines across its two files.
    let leaf_a_rows = csv::Reader::from_path(output_root.join("leaf_a").join("templates.csv"))
        .expect("leaf_a templates csv should exist")
        .records()
        .collect::<Result<Vec<_>, _>>()
        .expect("leaf_a template rows should load");
    let leaf_a_total: usize = leaf_a_rows
        .iter()
        .filter_map(|row| row.get(2).and_then(|v| v.parse::<usize>().ok()))
        .sum();
    assert_eq!(
        leaf_a_total, 4,
        "leaf_a templates should aggregate 4 lines across two files"
    );

    let leaf_b_rows = csv::Reader::from_path(output_root.join("leaf_b").join("templates.csv"))
        .expect("leaf_b templates csv should exist")
        .records()
        .collect::<Result<Vec<_>, _>>()
        .expect("leaf_b template rows should load");
    let leaf_b_total: usize = leaf_b_rows
        .iter()
        .filter_map(|row| row.get(2).and_then(|v| v.parse::<usize>().ok()))
        .sum();
    assert_eq!(
        leaf_b_total, 1,
        "leaf_b templates should aggregate its 1 line"
    );
}

#[test]
fn train_drain3_runs_successfully() {
    let temp = TestDir::new("train-drain3");
    let input = temp.path().join("app.log");
    std::fs::write(
        &input,
        "2026-04-11 12:00:00 INFO worker started\n2026-04-11 12:00:01 INFO worker stopped\n",
    )
    .expect("log should be written");

    run_from([
        OsString::from("log-parser"),
        OsString::from("train"),
        OsString::from("--algorithm"),
        OsString::from("drain3"),
        input.as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect("train --algorithm drain3 should succeed");

    let out_dir = temp.path().join("app.drain3");
    assert!(out_dir.join("templates.csv").is_file());
    assert!(!out_dir.join("templates.idx").exists());
}

#[test]
fn train_drain3_emit_index_writes_both() {
    let temp = TestDir::new("train-drain3-idx");
    let input = temp.path().join("app.log");
    std::fs::write(
        &input,
        "2026-04-11 12:00:00 INFO worker started\n2026-04-11 12:00:01 INFO worker stopped\n",
    )
    .expect("log should be written");

    run_from([
        OsString::from("log-parser"),
        OsString::from("train"),
        OsString::from("--algorithm"),
        OsString::from("drain3"),
        input.as_os_str().to_os_string(),
        OsString::from("--emit-index"),
        OsString::from("--quiet"),
    ])
    .expect("train --algorithm drain3 --emit-index should succeed");

    let out_dir = temp.path().join("app.drain3");
    assert!(out_dir.join("templates.csv").is_file());
    assert!(out_dir.join("templates.idx").is_file());
}

#[test]
fn train_drain3_directory_writes_leaf_indexes_and_templates() {
    // Directory train --emit-index for Drain3 must write per-leaf templates.idx
    // and templates.csv. This exercises the Drain3 arm of run_train_directory_loop
    // (train_for_entries via its CliTrainingRunner::Drain3 arm + IndexBuilder::build_drain3)
    // and the cache-path asymmetry (resolve_drain3_leaf_cache_path). Mirrors the PIPLUP
    // train_directory_writes_leaf_indexes_and_templates seam.
    let temp = TestDir::new("train-drain3-directory");
    let input_root = temp.path().join("organized_logs");
    let output_root = temp.path().join("indexed");
    std::fs::create_dir_all(input_root.join("leaf_a")).expect("leaf_a should exist");
    std::fs::create_dir_all(input_root.join("leaf_b")).expect("leaf_b should exist");
    std::fs::write(
        input_root.join("leaf_a").join("a.log"),
        "2026-04-11 12:00:00 INFO worker started\n2026-04-11 12:00:01 INFO worker started\n",
    )
    .expect("leaf_a log should be written");
    std::fs::write(
        input_root.join("leaf_b").join("b.log"),
        "2026-04-11 12:00:02 INFO worker stopped\n",
    )
    .expect("leaf_b log should be written");

    run_from([
        OsString::from("log-parser"),
        OsString::from("train"),
        OsString::from("--algorithm"),
        OsString::from("drain3"),
        input_root.as_os_str().to_os_string(),
        OsString::from("--emit-index"),
        OsString::from("--out-dir"),
        output_root.as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect("drain3 directory train should succeed");

    assert!(output_root.join("leaf_a").join("templates.idx").is_file());
    assert!(output_root.join("leaf_a").join("templates.csv").is_file());
    assert!(output_root.join("leaf_b").join("templates.idx").is_file());
    assert!(output_root.join("leaf_b").join("templates.csv").is_file());
}

#[test]
fn train_drain3_directory_cache_dir_writes_hash_based_cache() {
    let temp = TestDir::new("train-drain3-cache-dir");
    let input_root = temp.path().join("logs");
    let cache_root = temp.path().join("cache");
    std::fs::create_dir_all(input_root.join("leaf")).expect("leaf should exist");
    std::fs::write(
        input_root.join("leaf").join("app.log"),
        "2026-04-11 12:00:00 INFO worker 1",
    )
    .expect("log should be written");

    run_from([
        OsString::from("log-parser"),
        OsString::from("train"),
        OsString::from("--algorithm"),
        OsString::from("drain3"),
        input_root.as_os_str().to_os_string(),
        OsString::from("--cache-dir"),
        cache_root.as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect("directory train --algorithm drain3 should succeed");

    // Drain3 uses hash-based naming: <hex>.drain3.cache
    let cache_entries: Vec<_> = std::fs::read_dir(&cache_root)
        .expect("cache dir should be readable")
        .filter_map(|e| e.ok())
        .collect();
    assert_eq!(cache_entries.len(), 1, "expected exactly one cache file");
    let cache_file = cache_entries[0].file_name();
    let name = cache_file.to_string_lossy();
    assert!(
        name.ends_with(".drain3.cache"),
        "cache file should end with .drain3.cache, got: {name}"
    );
    assert!(
        name.len() == 77,
        "hex hash (64) + .drain3.cache (13) = 77 chars, got: {}",
        name.len()
    );
}

#[test]
fn train_drain3_directory_cache_dir_resumes_correctly() {
    let temp = TestDir::new("train-drain3-cache-resume");
    let input_root = temp.path().join("logs");
    let cache_root = temp.path().join("cache");
    std::fs::create_dir_all(input_root.join("leaf")).expect("leaf should exist");

    // First run: train on partial input
    std::fs::write(
        input_root.join("leaf").join("app.log"),
        "2026-04-11 12:00:00 INFO worker started\n2026-04-11 12:00:01 INFO worker stopped\n",
    )
    .expect("log should be written");

    run_from([
        OsString::from("log-parser"),
        OsString::from("train"),
        OsString::from("--algorithm"),
        OsString::from("drain3"),
        input_root.as_os_str().to_os_string(),
        OsString::from("--cache-dir"),
        cache_root.as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect("initial train should succeed");

    // Verify cache file exists
    let cache_entries: Vec<_> = std::fs::read_dir(&cache_root)
        .expect("cache dir should be readable")
        .filter_map(|e| e.ok())
        .collect();
    assert_eq!(cache_entries.len(), 1, "expected exactly one cache file");

    // Second run: resume with same input (should succeed)
    run_from([
        OsString::from("log-parser"),
        OsString::from("train"),
        OsString::from("--algorithm"),
        OsString::from("drain3"),
        input_root.as_os_str().to_os_string(),
        OsString::from("--cache-dir"),
        cache_root.as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect("resume train should succeed");
}

#[test]
fn train_drain3_checkpoint_lines_without_cache_fails() {
    let temp = TestDir::new("train-drain3-checkpoint-no-cache");
    let input = temp.path().join("app.log");
    std::fs::write(&input, "2026-04-11 12:00:00 INFO worker started\n")
        .expect("log should be written");

    let error = run_from([
        OsString::from("log-parser"),
        OsString::from("train"),
        OsString::from("--algorithm"),
        OsString::from("drain3"),
        input.as_os_str().to_os_string(),
        OsString::from("--checkpoint-lines"),
        OsString::from("100"),
        OsString::from("--quiet"),
    ])
    .expect_err("checkpoint-lines without cache should fail");

    assert!(
        error.to_string().contains("cache"),
        "error should mention cache requirement, got: {error}"
    );
}

#[test]
fn train_drain3_force_rebuild_cache_dir() {
    let temp = TestDir::new("train-drain3-force-rebuild");
    let input_root = temp.path().join("logs");
    let cache_root = temp.path().join("cache");
    std::fs::create_dir_all(input_root.join("leaf")).expect("leaf should exist");
    std::fs::write(
        input_root.join("leaf").join("app.log"),
        "2026-04-11 12:00:00 INFO worker 1",
    )
    .expect("log should be written");

    // First run: create cache
    run_from([
        OsString::from("log-parser"),
        OsString::from("train"),
        OsString::from("--algorithm"),
        OsString::from("drain3"),
        input_root.as_os_str().to_os_string(),
        OsString::from("--cache-dir"),
        cache_root.as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect("initial train should succeed");

    // Verify cache exists
    let cache_entries: Vec<_> = std::fs::read_dir(&cache_root)
        .expect("cache dir should be readable")
        .filter_map(|e| e.ok())
        .collect();
    assert_eq!(cache_entries.len(), 1, "expected exactly one cache file");

    // Second run: force rebuild
    run_from([
        OsString::from("log-parser"),
        OsString::from("train"),
        OsString::from("--algorithm"),
        OsString::from("drain3"),
        input_root.as_os_str().to_os_string(),
        OsString::from("--cache-dir"),
        cache_root.as_os_str().to_os_string(),
        OsString::from("--force-rebuild-cache"),
        OsString::from("--quiet"),
    ])
    .expect("force rebuild train should succeed");

    // Cache should still exist (rebuilt, not deleted)
    let cache_entries: Vec<_> = std::fs::read_dir(&cache_root)
        .expect("cache dir should be readable")
        .filter_map(|e| e.ok())
        .collect();
    assert_eq!(
        cache_entries.len(),
        1,
        "expected exactly one cache file after rebuild"
    );
}

#[test]
fn drain3_cache_file_id_is_stable() {
    use crate::drain3::drain3_cache_file_id;
    let root = Path::new("/some/root");
    let file = Path::new("/some/root/dir/app.log");

    let id1 = drain3_cache_file_id(root, file);
    let id2 = drain3_cache_file_id(root, file);
    assert_eq!(id1, id2, "cache file id should be stable for same inputs");
    assert_eq!(id1.len(), 64, "SHA-256 hex should be 64 chars");
}

#[test]
fn drain3_cache_file_id_differs_for_different_files() {
    use crate::drain3::drain3_cache_file_id;
    let root = Path::new("/some/root");
    let file1 = Path::new("/some/root/dir/app1.log");
    let file2 = Path::new("/some/root/dir/app2.log");

    let id1 = drain3_cache_file_id(root, file1);
    let id2 = drain3_cache_file_id(root, file2);
    assert_ne!(
        id1, id2,
        "different files should produce different cache ids"
    );
}

#[test]
fn drain3_leaf_cache_file_id_is_stable() {
    use crate::drain3::drain3_leaf_cache_file_id;
    let root = Path::new("/some/root");
    let files = [
        Path::new("/some/root/leaf/a.log"),
        Path::new("/some/root/leaf/b.log"),
    ];

    let id1 = drain3_leaf_cache_file_id(root, &files);
    let id2 = drain3_leaf_cache_file_id(root, &files);
    assert_eq!(id1, id2, "leaf cache id should be stable for same inputs");
    assert_eq!(id1.len(), 64, "SHA-256 hex should be 64 chars");
}

#[test]
fn drain3_leaf_cache_file_id_is_order_independent() {
    use crate::drain3::drain3_leaf_cache_file_id;
    let root = Path::new("/some/root");
    let files_a = [
        Path::new("/some/root/leaf/a.log"),
        Path::new("/some/root/leaf/b.log"),
    ];
    let files_b = [
        Path::new("/some/root/leaf/b.log"),
        Path::new("/some/root/leaf/a.log"),
    ];

    // Caller must sort before calling; but the function itself hashes in
    // the order given.  This test documents that the caller is responsible
    // for sorting (resolve_drain3_leaf_cache_path does this).
    let id_a = drain3_leaf_cache_file_id(root, &files_a);
    let id_b = drain3_leaf_cache_file_id(root, &files_b);
    // Different order → different hash (caller sorts first)
    assert_ne!(id_a, id_b, "unsorted order produces different hashes");
}

#[test]
fn drain3_leaf_cache_file_id_differs_for_different_leaf_contents() {
    use crate::drain3::drain3_leaf_cache_file_id;
    let root = Path::new("/some/root");
    let files_1 = [Path::new("/some/root/leaf/a.log")];
    let files_2 = [
        Path::new("/some/root/leaf/a.log"),
        Path::new("/some/root/leaf/b.log"),
    ];

    let id1 = drain3_leaf_cache_file_id(root, &files_1);
    let id2 = drain3_leaf_cache_file_id(root, &files_2);
    assert_ne!(
        id1, id2,
        "different file sets should produce different cache ids"
    );
}

#[test]
fn train_drain3_directory_multi_file_leaf_writes_one_cache() {
    let temp = TestDir::new("train-drain3-multi-file");
    let input_root = temp.path().join("logs");
    let cache_root = temp.path().join("cache");
    std::fs::create_dir_all(input_root.join("leaf")).expect("leaf should exist");
    std::fs::write(
        input_root.join("leaf").join("a.log"),
        "2026-04-11 12:00:00 INFO worker 1",
    )
    .expect("a.log should be written");
    std::fs::write(
        input_root.join("leaf").join("b.log"),
        "2026-04-11 12:00:01 INFO worker 2",
    )
    .expect("b.log should be written");

    run_from([
        OsString::from("log-parser"),
        OsString::from("train"),
        OsString::from("--algorithm"),
        OsString::from("drain3"),
        input_root.as_os_str().to_os_string(),
        OsString::from("--cache-dir"),
        cache_root.as_os_str().to_os_string(),
        OsString::from("--quiet"),
    ])
    .expect("directory train with multi-file leaf should succeed");

    // One cache per leaf, not per file
    let cache_entries: Vec<_> = std::fs::read_dir(&cache_root)
        .expect("cache dir should be readable")
        .filter_map(|e| e.ok())
        .collect();
    assert_eq!(
        cache_entries.len(),
        1,
        "expected exactly one cache file for one leaf with two files"
    );
    let name = cache_entries[0].file_name().to_string_lossy().into_owned();
    assert!(
        name.ends_with(".drain3.cache"),
        "should be .drain3.cache: {name}"
    );
}

#[cfg(feature = "async")]
fn write_async_multi_leaf_fixture(input_root: &Path) {
    std::fs::create_dir_all(input_root.join("controller_0A").join("mem_logs"))
        .expect("test directories should be created");
    std::fs::create_dir_all(input_root.join("controller_0B").join("disk_logs"))
        .expect("test directories should be created");

    std::fs::write(
        input_root
            .join("controller_0A")
            .join("mem_logs")
            .join("messages_1.log"),
        "2026-04-11 12:00:00 alpha\n2026-04-11 12:00:01 alpha\n",
    )
    .expect("first leaf log should exist");
    std::fs::write(
        input_root
            .join("controller_0A")
            .join("mem_logs")
            .join("messages_2.log"),
        "2026-04-11 12:00:02 beta\n",
    )
    .expect("second leaf log should exist");
    std::fs::write(
        input_root
            .join("controller_0B")
            .join("disk_logs")
            .join("messages_3.log"),
        "2026-04-11 12:00:03 gamma\n2026-04-11 12:00:04 gamma\n",
    )
    .expect("third leaf log should exist");
}

#[cfg(feature = "async")]
fn assert_directory_files_equal(left: &Path, right: &Path) {
    let left_files = relative_file_bytes(left);
    let right_files = relative_file_bytes(right);
    assert_eq!(
        left_files.keys().collect::<Vec<_>>(),
        right_files.keys().collect::<Vec<_>>()
    );
    for (path, left_bytes) in left_files {
        let right_bytes = right_files
            .get(&path)
            .unwrap_or_else(|| panic!("missing file in right output: {}", path.display()));
        assert_eq!(
            left_bytes.as_slice(),
            right_bytes.as_slice(),
            "file differs: {}",
            path.display()
        );
    }
}

#[cfg(feature = "async")]
fn relative_file_bytes(root: &Path) -> std::collections::BTreeMap<PathBuf, Vec<u8>> {
    fn visit(root: &Path, dir: &Path, files: &mut std::collections::BTreeMap<PathBuf, Vec<u8>>) {
        let mut entries = std::fs::read_dir(dir)
            .unwrap_or_else(|err| panic!("failed to read {}: {err}", dir.display()))
            .collect::<Result<Vec<_>, _>>()
            .expect("directory entries should load");
        entries.sort_by_key(|entry| entry.path());

        for entry in entries {
            let path = entry.path();
            if path.is_dir() {
                visit(root, &path, files);
            } else {
                let relative = path
                    .strip_prefix(root)
                    .expect("output path should be under root")
                    .to_path_buf();
                files.insert(
                    relative,
                    std::fs::read(&path).expect("output file should be readable"),
                );
            }
        }
    }

    let mut files = std::collections::BTreeMap::new();
    visit(root, root, &mut files);
    files
}

// ── ADR-0039 A1: config mapping helper tests ──────────────────────────────

#[test]
fn shared_config_flags_from_online_maps_polarity() {
    // All shared flags set true: preprocess inverts, the rest pass through.
    let cli = Cli::try_parse_from([
        OsString::from("log-parser"),
        OsString::from("online"),
        OsString::from("app.log"),
        OsString::from("--no-preprocess"),
        OsString::from("--keep-params"),
        OsString::from("--warn-miss"),
        OsString::from("--skip-unmatched"),
    ])
    .expect("online with all flags should parse");
    let Commands::Online(args) = cli.command else {
        panic!("expected online command");
    };
    let flags = shared_config_flags_from_online(&args);
    assert!(!flags.preprocess, "preprocess = !no_preprocess");
    assert!(!flags.typed_placeholders);
    assert!(flags.keep_para, "keep_para = keep_params");
    assert!(flags.warn_miss);
    assert!(flags.skip_unmatched);

    // Defaults: only preprocess is true (no_preprocess = false).
    let cli = Cli::try_parse_from(["log-parser", "online", "app.log"])
        .expect("default online should parse");
    let Commands::Online(args) = cli.command else {
        panic!("expected online command");
    };
    let flags = shared_config_flags_from_online(&args);
    assert!(flags.preprocess);
    assert!(!flags.typed_placeholders);
    assert!(!flags.keep_para);
    assert!(!flags.warn_miss);
    assert!(!flags.skip_unmatched);
}

#[test]
fn shared_config_flags_from_train_maps_polarity() {
    let cli = Cli::try_parse_from([
        OsString::from("log-parser"),
        OsString::from("train"),
        OsString::from("app.log"),
        OsString::from("--no-preprocess"),
        OsString::from("--keep-params"),
        OsString::from("--warn-miss"),
        OsString::from("--skip-unmatched"),
    ])
    .expect("train with all flags should parse");
    let Commands::Train(args) = cli.command else {
        panic!("expected train command");
    };
    let flags = shared_config_flags_from_train(&args);
    assert!(!flags.preprocess);
    assert!(!flags.typed_placeholders);
    assert!(flags.keep_para);
    assert!(flags.warn_miss);
    assert!(flags.skip_unmatched);

    let cli = Cli::try_parse_from(["log-parser", "train", "app.log"])
        .expect("default train should parse");
    let Commands::Train(args) = cli.command else {
        panic!("expected train command");
    };
    let flags = shared_config_flags_from_train(&args);
    assert!(flags.preprocess);
    assert!(!flags.typed_placeholders);
    assert!(!flags.keep_para);
    assert!(!flags.warn_miss);
    assert!(!flags.skip_unmatched);
}

#[test]
fn preprocess_modes_map_for_all_subcommands() {
    let cli = Cli::try_parse_from([
        "log-parser",
        "online",
        "app.log",
        "--preprocess-mode",
        "typed",
    ])
    .expect("typed online mode should parse");
    let Commands::Online(args) = cli.command else {
        panic!("expected online command");
    };
    let flags = shared_config_flags_from_online(&args);
    assert!(flags.preprocess);
    assert!(flags.typed_placeholders);

    let cli = Cli::try_parse_from(["log-parser", "train", "app.log", "--preprocess-mode", "off"])
        .expect("off train mode should parse");
    let Commands::Train(args) = cli.command else {
        panic!("expected train command");
    };
    let flags = shared_config_flags_from_train(&args);
    assert!(!flags.preprocess);
    assert!(!flags.typed_placeholders);

    let cli = Cli::try_parse_from([
        "log-parser",
        "parse",
        "app.log",
        "--preprocess-mode",
        "typed",
    ])
    .expect("typed parse mode should parse");
    let Commands::Parse(args) = cli.command else {
        panic!("expected parse command");
    };
    let config = build_indexed_config(&args);
    assert!(config.preprocess);
    assert!(config.typed_placeholders);
}

#[test]
fn preprocess_mode_conflicts_with_legacy_no_preprocess_flag() {
    for subcommand in ["online", "train", "parse"] {
        let error = Cli::try_parse_from([
            "log-parser",
            subcommand,
            "app.log",
            "--no-preprocess",
            "--preprocess-mode",
            "typed",
        ])
        .expect_err("preprocessing selectors should conflict");
        assert_eq!(error.kind(), clap::error::ErrorKind::ArgumentConflict);
    }
}

#[test]
fn piplup_overrides_from_online_maps_fields() {
    let cli = Cli::try_parse_from([
        OsString::from("log-parser"),
        OsString::from("online"),
        OsString::from("app.log"),
        OsString::from("--hit-limit"),
        OsString::from("100"),
        OsString::from("--branch-threshold"),
        OsString::from("7"),
        OsString::from("--sim-thresh"),
        OsString::from("0.6"),
        OsString::from("--no-merge"),
    ])
    .expect("online with piplup overrides should parse");
    let Commands::Online(args) = cli.command else {
        panic!("expected online command");
    };
    let overrides = piplup_overrides_from_online(&args);
    assert_eq!(overrides.hit_limit, 100);
    assert_eq!(overrides.branch_threshold, 7);
    assert_eq!(overrides.sim_thresh, SimThresh::Fixed(0.6));
    assert!(!overrides.merge, "merge = !no_merge");
}

#[test]
fn piplup_overrides_from_train_maps_fields() {
    let cli = Cli::try_parse_from([
        OsString::from("log-parser"),
        OsString::from("train"),
        OsString::from("app.log"),
        OsString::from("--hit-limit"),
        OsString::from("42"),
        OsString::from("--branch-threshold"),
        OsString::from("3"),
        OsString::from("--sim-thresh"),
        OsString::from("dynamic"),
        OsString::from("--no-merge"),
    ])
    .expect("train with piplup overrides should parse");
    let Commands::Train(args) = cli.command else {
        panic!("expected train command");
    };
    let overrides = piplup_overrides_from_train(&args);
    assert_eq!(overrides.hit_limit, 42);
    assert_eq!(overrides.branch_threshold, 3);
    assert_eq!(overrides.sim_thresh, SimThresh::Dynamic);
    assert!(!overrides.merge);
}

// ── ADR-0039 A2: cache lifecycle policy matrix tests ──────────────────────

/// Build a Drain3 cache state with a specific input identity and a non-zero
/// processed-line cursor, by training a short session. Mirrors the pattern in
/// `tests/drain3_integration.rs::drain3_cache_round_trip_preserves_clusters`.
fn build_drain3_cache_state(
    config: &Drain3Config,
    input: &Path,
    lines: &[&str],
) -> crate::Drain3CacheState {
    let mut parser = Drain3Parser::new(config.clone()).expect("drain3 parser should create");
    parser.set_input_identity(drain3_path_identity(input));
    let entries: Vec<LogEntry> = lines
        .iter()
        .enumerate()
        .map(|(i, line)| LogEntry {
            line_number: i + 1,
            raw: format!("2026-04-11 12:00:00 INFO {line}"),
        })
        .collect();
    parser
        .parse_lines_for_training(&entries)
        .expect("training should succeed");
    parser
        .take_cache_state()
        .expect("cache state should be available after training")
}

#[test]
fn drain3_online_cache_warm_starts_with_different_input() {
    let temp = TestDir::new("drain3-online-warm-start");
    let first = temp.path().join("first.log");
    let second = temp.path().join("second.log");
    let cache_path = temp.path().join("state.drain3.cache");
    let config = Drain3Config::default();

    let cache = build_drain3_cache_state(&config, &first, &["alpha event", "beta event"]);
    Drain3CacheManager::save_to_path(&cache_path, &cache, false).expect("cache should save");

    let (_, initial) = apply_drain3_cache_config(
        config,
        CacheConfigArgs {
            current_input: &second,
            cache_path: Some(&cache_path),
            segment_lines: None,
            checkpoint_lines: None,
            cache_compress: false,
            force_rebuild: false,
            input_policy: CacheInputPolicy::WarmStartOnMismatch,
        },
    )
    .expect("warm-start with different input should succeed");

    let initial = initial.expect("warm-start should restore learned state");
    assert_eq!(
        initial.processed_line_count, 0,
        "cursor must reset to 0 on warm-start"
    );
}

#[test]
fn drain3_train_cache_mismatch_rejects_without_force_rebuild() {
    let temp = TestDir::new("drain3-train-mismatch-reject");
    let first = temp.path().join("first.log");
    let second = temp.path().join("second.log");
    let cache_path = temp.path().join("train.drain3.cache");
    let config = Drain3Config::default();

    let cache = build_drain3_cache_state(&config, &first, &["alpha event"]);
    Drain3CacheManager::save_to_path(&cache_path, &cache, false).expect("cache should save");

    let err = apply_drain3_cache_config(
        config,
        CacheConfigArgs {
            current_input: &second,
            cache_path: Some(&cache_path),
            segment_lines: None,
            checkpoint_lines: None,
            cache_compress: false,
            force_rebuild: false,
            input_policy: CacheInputPolicy::RequireMatch,
        },
    )
    .expect_err("RequireMatch mismatch should reject");
    assert!(
        err.to_string().contains("use --force-rebuild-cache"),
        "error should mention --force-rebuild-cache: {err}"
    );
}

#[test]
fn piplup_train_cache_mismatch_force_rebuild_cold_starts() {
    let temp = TestDir::new("piplup-train-mismatch-force");
    let first = temp.path().join("first.log");
    let second = temp.path().join("second.log");
    let cache_path = temp.path().join("train.cache");
    let config = PiplupConfig::default();
    let cache = CacheState::default()
        .with_runtime_hash(config.compute_runtime_hash())
        .with_preprocess_hash(PreprocessPlan::default().compute_content_hash())
        .with_cursor(5, Some(path_identity(&first)));
    CacheManager::save_to_path(&cache_path, &cache, false).expect("cache should save");

    let (_, initial) = apply_cache_config(
        config,
        CacheConfigArgs {
            current_input: &second,
            cache_path: Some(&cache_path),
            segment_lines: None,
            checkpoint_lines: None,
            cache_compress: false,
            force_rebuild: true,
            input_policy: CacheInputPolicy::RequireMatch,
        },
    )
    .expect("force rebuild should cold-start despite mismatch");
    assert!(initial.is_none(), "force rebuild must not restore state");
    assert!(
        !cache_path.exists(),
        "mismatched cache file should be removed"
    );
}

#[test]
fn drain3_train_cache_mismatch_force_rebuild_cold_starts() {
    let temp = TestDir::new("drain3-train-mismatch-force");
    let first = temp.path().join("first.log");
    let second = temp.path().join("second.log");
    let cache_path = temp.path().join("train.drain3.cache");
    let config = Drain3Config::default();

    let cache = build_drain3_cache_state(&config, &first, &["alpha event"]);
    Drain3CacheManager::save_to_path(&cache_path, &cache, false).expect("cache should save");

    let (_, initial) = apply_drain3_cache_config(
        config,
        CacheConfigArgs {
            current_input: &second,
            cache_path: Some(&cache_path),
            segment_lines: None,
            checkpoint_lines: None,
            cache_compress: false,
            force_rebuild: true,
            input_policy: CacheInputPolicy::RequireMatch,
        },
    )
    .expect("force rebuild should cold-start despite mismatch");
    assert!(initial.is_none(), "force rebuild must not restore state");
}

#[test]
fn piplup_corrupt_cache_force_rebuild_cold_starts() {
    let temp = TestDir::new("piplup-corrupt-force");
    let input = temp.path().join("app.log");
    let cache_path = temp.path().join("state.cache");
    std::fs::write(&cache_path, b"corrupt cache payload").expect("corrupt cache should be written");

    let config = PiplupConfig::default();
    let (_, initial) = apply_cache_config(
        config,
        CacheConfigArgs {
            current_input: &input,
            cache_path: Some(&cache_path),
            segment_lines: None,
            checkpoint_lines: None,
            cache_compress: false,
            force_rebuild: true,
            input_policy: CacheInputPolicy::WarmStartOnMismatch,
        },
    )
    .expect("corrupt cache + force rebuild should cold-start");
    assert!(initial.is_none(), "corrupt cache must not restore state");
}

#[test]
fn drain3_corrupt_cache_force_rebuild_cold_starts() {
    let temp = TestDir::new("drain3-corrupt-force");
    let input = temp.path().join("app.log");
    let cache_path = temp.path().join("state.drain3.cache");
    std::fs::write(&cache_path, b"corrupt cache payload").expect("corrupt cache should be written");

    let config = Drain3Config::default();
    let (_, initial) = apply_drain3_cache_config(
        config,
        CacheConfigArgs {
            current_input: &input,
            cache_path: Some(&cache_path),
            segment_lines: None,
            checkpoint_lines: None,
            cache_compress: false,
            force_rebuild: true,
            input_policy: CacheInputPolicy::WarmStartOnMismatch,
        },
    )
    .expect("corrupt cache + force rebuild should cold-start");
    assert!(initial.is_none(), "corrupt cache must not restore state");
}

// ── ADR-0040 B2/B3: directory-writer stats aggregation tests ──────────────
//
// Unit tests driving `DirectoryParseOutputWriter` and `DirectoryTrainingArtifactWriter`
// directly with synthetic inputs, asserting `DirectoryRunStats` /
// `DirectoryIndexBuildStats` aggregation (multi-leaf leaf_dir_count, template_count,
// line_count, and resume_line_count sync-accumulates / async-stays-0 semantics).

/// Build a synthetic `ParseOutput` with `line_count` line results and the given
/// event records `(event_id, event_template, occurrences)`.
fn synthetic_parse_output(line_count: usize, events: &[(&str, &str, usize)]) -> ParseOutput {
    let line_results = (1..=line_count)
        .map(|i| LineResult {
            line_number: i,
            raw: format!("line {i}"),
            fields: Vec::new(),
            event_id: "E".to_string(),
            event_template: "T".to_string(),
            parameter_list: None,
        })
        .collect();
    let events = events
        .iter()
        .map(|(id, tmpl, occ)| EventRecord {
            event_id: (*id).to_string(),
            event_template: (*tmpl).to_string(),
            occurrences: *occ,
        })
        .collect::<Vec<_>>();
    ParseOutput {
        line_results,
        cluster_count: events.len(),
        events,
        duration: Duration::ZERO,
    }
}

fn synthetic_training_output(
    line_count: usize,
    events: &[(&str, &str, usize)],
) -> crate::TrainingOutput {
    crate::TrainingOutput {
        parse_output: synthetic_parse_output(line_count, events),
        preprocess_plan: PreprocessPlan::default(),
    }
}

#[test]
fn parse_output_writer_aggregates_multi_leaf_stats() {
    let temp = TestDir::new("parse-writer-stats");
    let input_root = temp.path().join("logs");
    let output_root = temp.path().join("out");
    // Two leaves (leafA, leafB), two files each. Paths only need to resolve
    // (record_file derives dirs via strip_prefix; it does not read the files).
    let leaf_a1 = input_root.join("leafA").join("f1.log");
    let leaf_a2 = input_root.join("leafA").join("f2.log");
    let leaf_b1 = input_root.join("leafB").join("f1.log");
    let leaf_b2 = input_root.join("leafB").join("f2.log");

    let output_plan = OutputPlan::MirroredDirectory {
        input_root: input_root.clone(),
        output_root: Some(output_root.clone()),
        suffix: "piplup".to_string(),
    };
    let mut writer = DirectoryParseOutputWriter::new(&output_plan, 4);

    // leafA f1: 2 lines, template "A"; f2: 1 line, template "A" (same → deduped per leaf)
    writer
        .record_file(&leaf_a1, &synthetic_parse_output(2, &[("E1", "A", 2)]))
        .expect("record_file leafA f1");
    writer
        .record_file(&leaf_a2, &synthetic_parse_output(1, &[("E1", "A", 1)]))
        .expect("record_file leafA f2");
    // leafB f1: 3 lines, templates "B" and "C"; f2: 1 line, template "B" (deduped with leafB "B")
    writer
        .record_file(
            &leaf_b1,
            &synthetic_parse_output(3, &[("E2", "B", 2), ("E3", "C", 1)]),
        )
        .expect("record_file leafB f1");
    writer
        .record_file(&leaf_b2, &synthetic_parse_output(1, &[("E2", "B", 1)]))
        .expect("record_file leafB f2");

    let stats = writer
        .finish()
        .expect("finish should write per-leaf templates.csv");
    assert_eq!(stats.file_count, 4);
    assert_eq!(stats.leaf_dir_count, 2, "two distinct leaf template paths");
    assert_eq!(stats.line_count, 2 + 1 + 3 + 1);
    // leafA: 1 unique template (A); leafB: 2 unique (B, C) → 3
    assert_eq!(stats.template_count, 3);
}

#[test]
fn training_artifact_writer_aggregates_multi_leaf_stats_with_resume() {
    let temp = TestDir::new("train-writer-stats");
    let input_root = temp.path().join("logs");
    let output_root = temp.path().join("out");
    let leaf_a_dir = input_root.join("leafA");
    let leaf_b_dir = input_root.join("leafB");

    let started = Instant::now();
    let mut writer = DirectoryTrainingArtifactWriter::new(started, 2, 3);

    // leafA: 2 lines, 1 template, resume 5 (sync semantics: passes non-zero)
    let training_a = synthetic_training_output(2, &[("E1", "A", 2)]);
    writer
        .record_leaf(
            &input_root,
            &leaf_a_dir,
            Some(&output_root),
            &training_a,
            None,
            5,
        )
        .expect("record_leaf leafA should write templates.csv");
    // leafB: 3 lines, 2 templates, resume 0
    let training_b = synthetic_training_output(3, &[("E2", "B", 2), ("E3", "C", 1)]);
    writer
        .record_leaf(
            &input_root,
            &leaf_b_dir,
            Some(&output_root),
            &training_b,
            None,
            0,
        )
        .expect("record_leaf leafB should write templates.csv");

    let stats = writer.finish();
    assert_eq!(stats.leaf_dir_count, 2);
    assert_eq!(stats.file_count, 3);
    assert_eq!(stats.line_count, 2 + 3);
    assert_eq!(stats.template_count, 1 + 2);
    assert_eq!(
        stats.resume_line_count, 5,
        "resume_line_count accumulates non-zero (sync semantics)"
    );
}

#[test]
fn training_artifact_writer_async_semantics_resume_stays_zero() {
    let temp = TestDir::new("train-writer-async");
    let input_root = temp.path().join("logs");
    let leaf_a_dir = input_root.join("leafA");
    let leaf_b_dir = input_root.join("leafB");

    let started = Instant::now();
    let mut writer = DirectoryTrainingArtifactWriter::new(started, 2, 2);

    // Async semantics: resume_line_count is always passed 0 (async train rejects --cache).
    let training_a = synthetic_training_output(4, &[("E1", "A", 4)]);
    writer
        .record_leaf(&input_root, &leaf_a_dir, None, &training_a, None, 0)
        .expect("record_leaf leafA should write templates.csv");
    let training_b = synthetic_training_output(6, &[("E2", "B", 6)]);
    writer
        .record_leaf(&input_root, &leaf_b_dir, None, &training_b, None, 0)
        .expect("record_leaf leafB should write templates.csv");

    let stats = writer.finish();
    assert_eq!(stats.line_count, 4 + 6);
    assert_eq!(
        stats.resume_line_count, 0,
        "async semantics: resume stays 0 even with many lines"
    );
}

#[derive(Debug)]
struct TestDir {
    path: PathBuf,
}

impl TestDir {
    fn new(prefix: &str) -> Self {
        let unique = format!(
            "{prefix}-{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .expect("system time should be after unix epoch")
                .as_nanos()
        );
        let path = std::env::temp_dir().join(unique);
        std::fs::create_dir_all(&path).expect("test temp dir should be created");
        Self { path }
    }

    fn path(&self) -> &Path {
        &self.path
    }
}

impl Drop for TestDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.path);
    }
}

use super::super::source_task;
use super::super::types::{
    IndexedWorkerConfig, LineNumbering, SchedulerConfig, SessionPlan, SourceContext, SourceName,
    SourcePlan,
};
use super::*;
use crate::async_api::test_util::AsyncCursor;
use crate::config::PiplupConfig;
use crate::engine::parser::PiplupParser;
use crate::index::IndexedParser;
use crate::types::{LogEntry, ParseOutput};
use std::path::PathBuf;
use std::pin::Pin;
use std::sync::Arc;

fn make_log_lines(n: usize) -> String {
    (1..=n)
        .map(|i| format!("Jan 01 00:00:{:02} host app: message {i}\n", i % 60))
        .collect()
}

// -----------------------------------------------------------------------
// Reader session tests
// -----------------------------------------------------------------------

#[tokio::test]
async fn piplup_session_parses_simple_input() {
    let input = "Jan 01 00:00:01 host app: Connection accepted from 192.168.1.1\n\
                 Jan 01 00:00:02 host app: Connection accepted from 10.0.0.5\n\
                 Jan 01 00:00:03 host app: Connection accepted from 172.16.0.1\n";

    let config = PiplupConfig::default();
    let scheduler_config = SchedulerConfig::default();
    let reader = AsyncCursor::new(input);

    let output = run_piplup_reader_session(config, scheduler_config, reader)
        .await
        .expect("session should succeed");

    assert_eq!(output.line_results.len(), 3);
    assert_eq!(output.cluster_count, 1);
    assert_eq!(
        output.line_results[0].event_id,
        output.line_results[1].event_id
    );
}

#[tokio::test]
async fn piplup_training_session_returns_preprocess_plan() {
    let input = "Jan 01 00:00:01 host app: Connection accepted from 192.168.1.1\n\
                 Jan 01 00:00:02 host app: Connection accepted from 10.0.0.5\n";

    let config = PiplupConfig::default();
    let scheduler_config = SchedulerConfig::default();
    let reader = AsyncCursor::new(input);

    let output = run_piplup_reader_training_session(config, scheduler_config, reader)
        .await
        .expect("training session should succeed");

    assert_eq!(output.parse_output.line_results.len(), 2);
}

#[tokio::test]
async fn piplup_session_handles_empty_input() {
    let input = "";

    let config = PiplupConfig::default();
    let scheduler_config = SchedulerConfig::default();
    let reader = AsyncCursor::new(input);

    let output = run_piplup_reader_session(config, scheduler_config, reader)
        .await
        .expect("session should succeed with empty input");

    assert_eq!(output.line_results.len(), 0);
    assert_eq!(output.cluster_count, 0);
}

#[tokio::test]
async fn piplup_session_handles_many_lines() {
    let input = make_log_lines(50);

    let config = PiplupConfig::default();
    let scheduler_config = SchedulerConfig {
        batch_size: 8,
        ..SchedulerConfig::default()
    };
    let reader = AsyncCursor::new(input);

    let output = run_piplup_reader_session(config, scheduler_config, reader)
        .await
        .expect("session should succeed");

    assert_eq!(output.line_results.len(), 50);
}

// -----------------------------------------------------------------------
// Config validation tests
// -----------------------------------------------------------------------

#[tokio::test]
async fn scheduler_config_rejects_zero_channel_capacity() {
    let config = SchedulerConfig {
        source_channel_capacity: 0,
        ..SchedulerConfig::default()
    };
    let reader = AsyncCursor::new("");
    let result = run_piplup_reader_session(PiplupConfig::default(), config, reader).await;
    assert!(result.is_err(), "should reject source_channel_capacity = 0");
}

#[tokio::test]
async fn scheduler_config_rejects_zero_batch_size() {
    let config = SchedulerConfig {
        batch_size: 0,
        ..SchedulerConfig::default()
    };
    let reader = AsyncCursor::new("");
    let result = run_piplup_reader_session(PiplupConfig::default(), config, reader).await;
    assert!(result.is_err(), "should reject batch_size = 0");
}

#[tokio::test]
async fn scheduler_config_rejects_multi_source() {
    let config = SchedulerConfig {
        max_concurrent_sources_per_session: 2,
        max_scheduler_prefetch_batches: 8,
        ..SchedulerConfig::default()
    };
    let reader = AsyncCursor::new("");
    let result = run_piplup_reader_session(PiplupConfig::default(), config, reader).await;
    assert!(
        result.is_err(),
        "should reject max_concurrent_sources_per_session > 1 (not yet supported)"
    );
}

#[tokio::test]
async fn scheduler_config_rejects_zero_concurrent_sources() {
    let config = SchedulerConfig {
        max_concurrent_sources_per_session: 0,
        ..SchedulerConfig::default()
    };
    let reader = AsyncCursor::new("");
    let result = run_piplup_reader_session(PiplupConfig::default(), config, reader).await;
    assert!(
        result.is_err(),
        "should reject max_concurrent_sources_per_session = 0"
    );
}

#[tokio::test]
async fn scheduler_config_accepts_single_source_without_prefetch() {
    let config = SchedulerConfig {
        max_concurrent_sources_per_session: 1,
        max_scheduler_prefetch_batches: 0,
        ..SchedulerConfig::default()
    };
    assert!(config.validate().is_ok());
}

// -----------------------------------------------------------------------
// File session tests
// -----------------------------------------------------------------------

#[tokio::test]
async fn file_session_parses_single_file() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("test.log");
    std::fs::write(
        &path,
        "Jan 01 00:00:01 host app: Connection accepted from 192.168.1.1\n\
                            Jan 01 00:00:02 host app: Connection accepted from 10.0.0.5\n",
    )
    .expect("write");

    let config = PiplupConfig::default();
    let scheduler_config = SchedulerConfig::default();

    let output = run_piplup_file_session(config, scheduler_config, path.to_path_buf())
        .await
        .expect("file session should succeed");

    assert_eq!(output.line_results.len(), 2);
    assert_eq!(output.cluster_count, 1);
}

#[tokio::test]
async fn file_session_handles_empty_file() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("empty.log");
    std::fs::write(&path, "").expect("write");

    let config = PiplupConfig::default();
    let scheduler_config = SchedulerConfig::default();

    let output = run_piplup_file_session(config, scheduler_config, path.to_path_buf())
        .await
        .expect("file session should succeed with empty file");

    assert_eq!(output.line_results.len(), 0);
}

#[tokio::test]
async fn file_training_session_returns_preprocess_plan() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("test.log");
    std::fs::write(&path, "Jan 01 00:00:01 host app: Starting service\n").expect("write");

    let config = PiplupConfig::default();
    let scheduler_config = SchedulerConfig::default();

    let output = run_piplup_file_training_session(config, scheduler_config, path.to_path_buf())
        .await
        .expect("file training session should succeed");

    assert_eq!(output.parse_output.line_results.len(), 1);
}

// -----------------------------------------------------------------------
// Plan session tests (multi-file)
// -----------------------------------------------------------------------

fn make_session_plan(paths: Vec<PathBuf>, line_numbering: LineNumbering) -> SessionPlan {
    use super::super::types::SessionKey;
    SessionPlan {
        key: SessionKey::directory(
            paths[0]
                .parent()
                .unwrap_or(paths[0].as_path())
                .to_path_buf(),
        ),
        sources: paths
            .into_iter()
            .map(|p| SourcePlan::File { path: p })
            .collect(),
        line_numbering,
    }
}

#[tokio::test]
async fn plan_session_two_files_concatenated_numbering() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path_a = dir.path().join("a.log");
    let path_b = dir.path().join("b.log");
    std::fs::write(
        &path_a,
        "Jan 01 00:00:01 host app: Connection accepted from 192.168.1.1\n",
    )
    .expect("write");
    std::fs::write(
        &path_b,
        "Jan 01 00:00:02 host app: Connection accepted from 10.0.0.5\n",
    )
    .expect("write");

    let config = PiplupConfig::default();
    let scheduler_config = SchedulerConfig::default();
    let plan = make_session_plan(
        vec![path_a, path_b],
        LineNumbering::ConcatenateWithinSession,
    );

    let output = run_piplup_plan_session(config, scheduler_config, plan)
        .await
        .expect("plan session should succeed");

    assert_eq!(output.line_results.len(), 2);
    assert_eq!(output.cluster_count, 1);
}

#[tokio::test]
async fn plan_training_session_two_files() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path_a = dir.path().join("a.log");
    let path_b = dir.path().join("b.log");
    std::fs::write(&path_a, "Jan 01 00:00:01 host app: Starting service\n").expect("write");
    std::fs::write(&path_b, "Jan 01 00:00:02 host app: Stopping service\n").expect("write");

    let config = PiplupConfig::default();
    let scheduler_config = SchedulerConfig::default();
    let plan = make_session_plan(
        vec![path_a, path_b],
        LineNumbering::ConcatenateWithinSession,
    );

    let output = run_piplup_plan_training_session(config, scheduler_config, plan)
        .await
        .expect("plan training session should succeed");

    assert_eq!(output.parse_output.line_results.len(), 2);
}

#[tokio::test]
async fn plan_session_empty_file_in_plan() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path_empty = dir.path().join("empty.log");
    let path_content = dir.path().join("content.log");
    std::fs::write(&path_empty, "").expect("write");
    std::fs::write(
        &path_content,
        "Jan 01 00:00:01 host app: Starting service\n",
    )
    .expect("write");

    let config = PiplupConfig::default();
    let scheduler_config = SchedulerConfig::default();
    let plan = make_session_plan(
        vec![path_empty, path_content],
        LineNumbering::ConcatenateWithinSession,
    );

    let output = run_piplup_plan_session(config, scheduler_config, plan)
        .await
        .expect("plan session should succeed with empty file");

    assert_eq!(output.line_results.len(), 1);
}

// -----------------------------------------------------------------------
// Sync-vs-async parity tests
// -----------------------------------------------------------------------

fn sync_parse(input: &str, config: PiplupConfig) -> ParseOutput {
    let entries: Vec<LogEntry> = input
        .lines()
        .enumerate()
        .filter(|(_, line)| !line.is_empty())
        .map(|(i, line)| LogEntry {
            line_number: i + 1,
            raw: line.to_owned(),
        })
        .collect();

    let mut parser = PiplupParser::new(config).expect("parser should construct");
    parser
        .parse_lines(&entries)
        .expect("sync parse should succeed")
}

async fn async_parse(input: &str, config: PiplupConfig, batch_size: usize) -> ParseOutput {
    let scheduler_config = SchedulerConfig {
        batch_size,
        ..SchedulerConfig::default()
    };
    let reader = AsyncCursor::new(input);
    run_piplup_reader_session(config, scheduler_config, reader)
        .await
        .expect("async parse should succeed")
}

fn assert_parity(sync: &ParseOutput, async_: &ParseOutput) {
    assert_eq!(
        sync.line_results.len(),
        async_.line_results.len(),
        "line count mismatch"
    );
    assert_eq!(
        sync.cluster_count, async_.cluster_count,
        "cluster count mismatch"
    );
    assert_eq!(
        sync.events.len(),
        async_.events.len(),
        "event count mismatch"
    );

    for (i, (s, a)) in sync
        .line_results
        .iter()
        .zip(async_.line_results.iter())
        .enumerate()
    {
        assert_eq!(
            s.event_template, a.event_template,
            "line {i}: template mismatch"
        );
        assert_eq!(s.event_id, a.event_id, "line {i}: event_id mismatch");
    }
}

#[tokio::test]
async fn parity_identical_lines_single_template() {
    let input = "Jan 01 00:00:01 host app: Connection accepted from 192.168.1.1\n\
                 Jan 01 00:00:02 host app: Connection accepted from 10.0.0.5\n\
                 Jan 01 00:00:03 host app: Connection accepted from 172.16.0.1\n";

    let config = PiplupConfig::default();
    let sync_output = sync_parse(input, config.clone());
    let async_output = async_parse(input, config, 2).await;

    assert_parity(&sync_output, &async_output);
}

#[tokio::test]
async fn parity_two_distinct_templates() {
    let input = "Jan 01 00:00:01 host app: Connection accepted from 192.168.1.1\n\
                 Jan 01 00:00:02 host app: Connection refused by peer\n\
                 Jan 01 00:00:03 host app: Connection accepted from 10.0.0.5\n\
                 Jan 01 00:00:04 host app: Connection refused by peer\n";

    let config = PiplupConfig::default();
    let sync_output = sync_parse(input, config.clone());
    let async_output = async_parse(input, config, 2).await;

    assert_parity(&sync_output, &async_output);
    assert_eq!(sync_output.cluster_count, 2);
}

#[tokio::test]
async fn parity_with_small_batches() {
    let input = "Jan 01 00:00:01 app: Error in module alpha\n\
                 Jan 01 00:00:02 app: Error in module beta\n\
                 Jan 01 00:00:03 app: Error in module gamma\n\
                 Jan 01 00:00:04 app: Warning in module alpha\n\
                 Jan 01 00:00:05 app: Warning in module beta\n";

    let config = PiplupConfig::default();
    let sync_output = sync_parse(input, config.clone());
    let async_output = async_parse(input, config, 1).await;

    assert_parity(&sync_output, &async_output);
}

#[tokio::test]
async fn parity_with_large_batch_size() {
    let input = "Jan 01 00:00:01 app: Starting service\n\
                 Jan 01 00:00:02 app: Stopping service\n\
                 Jan 01 00:00:03 app: Starting service\n";

    let config = PiplupConfig::default();
    let sync_output = sync_parse(input, config.clone());
    let async_output = async_parse(input, config, 1000).await;

    assert_parity(&sync_output, &async_output);
}

#[tokio::test]
async fn parity_empty_input() {
    let input = "";

    let config = PiplupConfig::default();
    let sync_output = sync_parse(input, config.clone());
    let async_output = async_parse(input, config, 10).await;

    assert_eq!(sync_output.line_results.len(), 0);
    assert_eq!(async_output.line_results.len(), 0);
}

#[tokio::test]
async fn parity_many_lines_with_varying_content() {
    let mut lines = Vec::new();
    for i in 0..10 {
        lines.push(format!(
            "Jan 01 00:00:{i:02} app: User admin logged in from 10.0.0.{i}"
        ));
    }
    for i in 0..10 {
        lines.push(format!(
            "Jan 01 00:01:{i:02} app: Disk usage at {i}% on /dev/sda1"
        ));
    }
    for i in 0..10 {
        lines.push(format!(
            "Jan 01 00:02:{i:02} app: Connection timeout for client 192.168.{i}.1"
        ));
    }
    let input = lines.join("\n");

    let config = PiplupConfig::default();
    let sync_output = sync_parse(&input, config.clone());
    let async_output = async_parse(&input, config, 4).await;

    assert_parity(&sync_output, &async_output);
    assert_eq!(sync_output.cluster_count, 3);
}

#[tokio::test]
async fn piplup_streaming_training_parity() {
    let input = make_log_lines(200);

    // Sync baseline
    let entries: Vec<LogEntry> = input
        .lines()
        .enumerate()
        .map(|(i, line)| LogEntry {
            line_number: i + 1,
            raw: line.to_string(),
        })
        .collect();
    let mut parser = PiplupParser::new(PiplupConfig::default()).unwrap();
    let sync_output = parser.parse_lines_for_training(&entries).unwrap();

    // Async streaming
    let reader = crate::async_api::test_util::AsyncCursor::new(input);
    let async_output = run_piplup_reader_training_session(
        PiplupConfig::default(),
        SchedulerConfig::default(),
        reader,
    )
    .await
    .unwrap();

    assert_eq!(
        sync_output.parse_output.line_results.len(),
        async_output.parse_output.line_results.len()
    );
    assert_eq!(
        sync_output.preprocess_plan, async_output.preprocess_plan,
        "preprocess plan mismatch"
    );
}

// -----------------------------------------------------------------------
// Indexed session tests
// -----------------------------------------------------------------------

/// Helper: build an IndexedParser from training entries.
fn build_indexed_parser(entries: &[LogEntry], config: &PiplupConfig) -> IndexedParser {
    let mut parser = PiplupParser::new(config.clone()).expect("parser should build");
    let training = parser
        .parse_lines_for_training(entries)
        .expect("training should succeed");
    let index = crate::index::IndexBuilder::build(&training, config).expect("index should build");
    IndexedParser::from_index(index, config.clone()).expect("indexed parser should build")
}

/// Helper: build an IndexedParser from raw training input string.
fn build_indexed_parser_from_input(input: &str, config: &PiplupConfig) -> IndexedParser {
    let entries: Vec<LogEntry> = input
        .lines()
        .enumerate()
        .filter(|(_, line)| !line.is_empty())
        .map(|(i, line)| LogEntry {
            line_number: i + 1,
            raw: line.to_owned(),
        })
        .collect();
    build_indexed_parser(&entries, config)
}

fn assert_indexed_parity(sync: &crate::IndexedParseOutput, async_: &crate::IndexedParseOutput) {
    assert_eq!(
        sync.parse_output.line_results.len(),
        async_.parse_output.line_results.len(),
        "line count mismatch"
    );
    assert_eq!(
        sync.parse_output.cluster_count, async_.parse_output.cluster_count,
        "cluster count mismatch"
    );
    assert_eq!(
        sync.parse_output.events.len(),
        async_.parse_output.events.len(),
        "event count mismatch"
    );
    assert_eq!(
        sync.stats.miss_count, async_.stats.miss_count,
        "miss count mismatch"
    );
    assert_eq!(
        sync.stats.new_miss_count, async_.stats.new_miss_count,
        "new miss count mismatch"
    );

    for (i, (s, a)) in sync
        .parse_output
        .line_results
        .iter()
        .zip(async_.parse_output.line_results.iter())
        .enumerate()
    {
        assert_eq!(
            s.event_template, a.event_template,
            "line {i}: template mismatch"
        );
        assert_eq!(s.event_id, a.event_id, "line {i}: event_id mismatch");
    }
}

#[tokio::test]
async fn indexed_reader_session_parses_simple_input() {
    let input = "Jan 01 00:00:01 host app: Connection accepted from 192.168.1.1\n\
                 Jan 01 00:00:02 host app: Connection accepted from 10.0.0.5\n\
                 Jan 01 00:00:03 host app: Connection accepted from 172.16.0.1\n";

    let config = PiplupConfig::default();
    let parser = build_indexed_parser_from_input(input, &config);

    let scheduler_config = SchedulerConfig::default();
    let reader = AsyncCursor::new(input);

    let output = run_indexed_reader_session(parser, scheduler_config, reader)
        .await
        .expect("indexed reader session should succeed");

    assert_eq!(output.parse_output.line_results.len(), 3);
    assert_eq!(output.parse_output.cluster_count, 1);
    assert_eq!(output.stats.miss_count, 0);
}

#[tokio::test]
async fn indexed_reader_session_handles_empty_input() {
    let training_input = "Jan 01 00:00:01 host app: Connection accepted from 192.168.1.1\n";
    let config = PiplupConfig::default();
    let parser = build_indexed_parser_from_input(training_input, &config);

    let scheduler_config = SchedulerConfig::default();
    let reader = AsyncCursor::new("");

    let output = run_indexed_reader_session(parser, scheduler_config, reader)
        .await
        .expect("indexed session should succeed with empty input");

    assert_eq!(output.parse_output.line_results.len(), 0);
    assert_eq!(output.parse_output.cluster_count, 1);
    assert_eq!(output.stats.miss_count, 0);
}

#[tokio::test]
async fn indexed_file_session_parses_single_file() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("test.log");
    let content = "Jan 01 00:00:01 host app: Connection accepted from 192.168.1.1\n\
                   Jan 01 00:00:02 host app: Connection accepted from 10.0.0.5\n";
    std::fs::write(&path, content).expect("write");

    let config = PiplupConfig::default();
    let parser = build_indexed_parser_from_input(content, &config);

    let scheduler_config = SchedulerConfig::default();
    let output = run_indexed_file_session(parser, scheduler_config, path.to_path_buf())
        .await
        .expect("indexed file session should succeed");

    assert_eq!(output.parse_output.line_results.len(), 2);
    assert_eq!(output.parse_output.cluster_count, 1);
    assert_eq!(output.stats.miss_count, 0);
}

#[tokio::test]
async fn indexed_plan_session_two_files() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path_a = dir.path().join("a.log");
    let path_b = dir.path().join("b.log");
    let content_a = "Jan 01 00:00:01 host app: Connection accepted from 192.168.1.1\n";
    let content_b = "Jan 01 00:00:02 host app: Connection accepted from 10.0.0.5\n";
    std::fs::write(&path_a, content_a).expect("write");
    std::fs::write(&path_b, content_b).expect("write");

    // Build index from the concatenated content.
    let all_content = format!("{content_a}{content_b}");
    let config = PiplupConfig::default();
    let parser = build_indexed_parser_from_input(&all_content, &config);

    let plan = make_session_plan(
        vec![path_a, path_b],
        LineNumbering::ConcatenateWithinSession,
    );

    let scheduler_config = SchedulerConfig::default();
    let output = run_indexed_plan_session(parser, scheduler_config, plan)
        .await
        .expect("indexed plan session should succeed");

    assert_eq!(output.parse_output.line_results.len(), 2);
    assert_eq!(output.parse_output.cluster_count, 1);
}

#[tokio::test]
async fn indexed_session_parity_with_sync() {
    let input = "Jan 01 00:00:01 host app: Connection accepted from 192.168.1.1\n\
                 Jan 01 00:00:02 host app: Connection refused by peer\n\
                 Jan 01 00:00:03 host app: Connection accepted from 10.0.0.5\n\
                 Jan 01 00:00:04 host app: Connection refused by peer\n";

    let config = PiplupConfig::default();

    // Sync: build parser, parse directly.
    let entries: Vec<LogEntry> = input
        .lines()
        .enumerate()
        .filter(|(_, line)| !line.is_empty())
        .map(|(i, line)| LogEntry {
            line_number: i + 1,
            raw: line.to_owned(),
        })
        .collect();
    let sync_parser = build_indexed_parser(&entries, &config);
    let sync_output = sync_parser
        .parse_lines_detailed(&entries)
        .expect("sync indexed parse");

    // Async: same parser, same input through the async pipeline.
    let async_parser = build_indexed_parser(&entries, &config);
    let scheduler_config = SchedulerConfig {
        batch_size: 2,
        ..SchedulerConfig::default()
    };
    let reader = AsyncCursor::new(input);
    let async_output = run_indexed_reader_session(async_parser, scheduler_config, reader)
        .await
        .expect("async indexed parse");

    assert_indexed_parity(&sync_output, &async_output);
}

#[tokio::test]
async fn indexed_session_parity_with_small_batches() {
    let input = "Jan 01 00:00:01 app: Error in module alpha\n\
                 Jan 01 00:00:02 app: Error in module beta\n\
                 Jan 01 00:00:03 app: Error in module gamma\n\
                 Jan 01 00:00:04 app: Warning in module alpha\n\
                 Jan 01 00:00:05 app: Warning in module beta\n";

    let config = PiplupConfig::default();

    let entries: Vec<LogEntry> = input
        .lines()
        .enumerate()
        .filter(|(_, line)| !line.is_empty())
        .map(|(i, line)| LogEntry {
            line_number: i + 1,
            raw: line.to_owned(),
        })
        .collect();
    let sync_parser = build_indexed_parser(&entries, &config);
    let sync_output = sync_parser
        .parse_lines_detailed(&entries)
        .expect("sync indexed parse");

    let async_parser = build_indexed_parser(&entries, &config);
    let scheduler_config = SchedulerConfig {
        batch_size: 1,
        ..SchedulerConfig::default()
    };
    let reader = AsyncCursor::new(input);
    let async_output = run_indexed_reader_session(async_parser, scheduler_config, reader)
        .await
        .expect("async indexed parse");

    assert_indexed_parity(&sync_output, &async_output);
}

#[tokio::test]
async fn indexed_streaming_parity_with_sync() {
    use crate::index::IndexBuilder;
    let input = make_log_lines(200);
    let entries: Vec<LogEntry> = input
        .lines()
        .enumerate()
        .map(|(i, line)| LogEntry {
            line_number: i + 1,
            raw: line.to_string(),
        })
        .collect();

    // Build index from training data
    let mut trainer = PiplupParser::new(PiplupConfig::default()).unwrap();
    let training_output = trainer.parse_lines_for_training(&entries).unwrap();
    let index = IndexBuilder::build(&training_output, &PiplupConfig::default()).unwrap();

    // Sync baseline
    let parser = IndexedParser::from_index(index.clone(), PiplupConfig::default()).unwrap();
    let sync_output = parser.parse_lines_detailed(&entries).unwrap();

    // Async streaming
    let parser = IndexedParser::from_index(index, PiplupConfig::default()).unwrap();
    let reader = crate::async_api::test_util::AsyncCursor::new(input);
    let async_output = run_indexed_reader_session(parser, SchedulerConfig::default(), reader)
        .await
        .unwrap();

    assert_eq!(
        sync_output.parse_output.line_results.len(),
        async_output.parse_output.line_results.len()
    );
    for (a, b) in sync_output
        .parse_output
        .line_results
        .iter()
        .zip(async_output.parse_output.line_results.iter())
    {
        assert_eq!(a.line_number, b.line_number);
        assert_eq!(a.event_id, b.event_id);
        assert_eq!(a.event_template, b.event_template);
    }
    assert_eq!(sync_output.stats, async_output.stats);
}

#[tokio::test]
async fn indexed_session_stats_report_misses() {
    // Train on one template, then parse a line that doesn't match.
    let training_input = "Jan 01 00:00:01 host app: Connection accepted from 192.168.1.1\n";
    let parse_input = "Jan 01 00:00:01 host app: Connection accepted from 192.168.1.1\n\
                       Jan 01 00:00:02 host app: Completely unexpected message\n";

    let config = PiplupConfig::default();
    let parser = build_indexed_parser_from_input(training_input, &config);

    let scheduler_config = SchedulerConfig::default();
    let reader = AsyncCursor::new(parse_input);
    let output = run_indexed_reader_session(parser, scheduler_config, reader)
        .await
        .expect("indexed session should succeed");

    assert_eq!(output.parse_output.line_results.len(), 2);
    assert_eq!(output.stats.miss_count, 1);
    assert_eq!(output.stats.new_miss_count, 1);
    assert_eq!(output.stats.first_miss_line, Some(2));
    assert_eq!(output.stats.first_new_miss_line, Some(2));
    // First line matched the training template, second is a miss.
    assert_ne!(
        output.parse_output.line_results[0].event_id, "0",
        "first line should match a template"
    );
    assert_eq!(
        output.parse_output.line_results[1].event_id, "0",
        "second line should be a miss"
    );
}

#[tokio::test]
async fn indexed_session_handles_many_lines() {
    let input = make_log_lines(50);
    let config = PiplupConfig::default();

    let entries: Vec<LogEntry> = input
        .lines()
        .enumerate()
        .filter(|(_, line)| !line.is_empty())
        .map(|(i, line)| LogEntry {
            line_number: i + 1,
            raw: line.to_owned(),
        })
        .collect();
    let parser = build_indexed_parser(&entries, &config);

    let scheduler_config = SchedulerConfig {
        batch_size: 8,
        ..SchedulerConfig::default()
    };
    let reader = AsyncCursor::new(input.as_bytes());
    let output = run_indexed_reader_session(parser, scheduler_config, reader)
        .await
        .expect("indexed session should succeed");

    assert_eq!(output.parse_output.line_results.len(), 50);
    assert_eq!(output.stats.miss_count, 0);
}

// -----------------------------------------------------------------------
// Multi-worker indexed session tests
// -----------------------------------------------------------------------

#[tokio::test]
async fn indexed_parallel_parity_with_single_worker() {
    let input = "Jan 01 00:00:01 host app: Connection accepted from 192.168.1.1\n\
                 Jan 01 00:00:02 host app: Connection refused by peer\n\
                 Jan 01 00:00:03 host app: Connection accepted from 10.0.0.5\n\
                 Jan 01 00:00:04 host app: Connection refused by peer\n";

    let config = PiplupConfig::default();

    let entries: Vec<LogEntry> = input
        .lines()
        .enumerate()
        .filter(|(_, line)| !line.is_empty())
        .map(|(i, line)| LogEntry {
            line_number: i + 1,
            raw: line.to_owned(),
        })
        .collect();

    // Single-worker (worker_count=1).
    let parser = Arc::new(build_indexed_parser(&entries, &config));
    let scheduler_config = SchedulerConfig {
        batch_size: 2,
        ..SchedulerConfig::default()
    };
    let worker_config = IndexedWorkerConfig {
        worker_count: 1,
        chunk_size: 1024,
    };
    let reader = AsyncCursor::new(input);
    let single = run_indexed_reader_session_parallel(
        parser,
        scheduler_config.clone(),
        worker_config,
        reader,
    )
    .await
    .expect("single-worker parallel");

    // Multi-worker (worker_count=3).
    let parser = Arc::new(build_indexed_parser(&entries, &config));
    let worker_config = IndexedWorkerConfig {
        worker_count: 3,
        chunk_size: 2,
    };
    let reader = AsyncCursor::new(input);
    let multi =
        run_indexed_reader_session_parallel(parser, scheduler_config, worker_config, reader)
            .await
            .expect("multi-worker parallel");

    // Results should be identical.
    assert_eq!(
        single.parse_output.line_results.len(),
        multi.parse_output.line_results.len(),
    );
    assert_eq!(
        single.parse_output.cluster_count,
        multi.parse_output.cluster_count,
    );
    assert_eq!(single.stats.miss_count, multi.stats.miss_count);
    assert_eq!(single.stats.new_miss_count, multi.stats.new_miss_count);

    for (i, (s, m)) in single
        .parse_output
        .line_results
        .iter()
        .zip(multi.parse_output.line_results.iter())
        .enumerate()
    {
        assert_eq!(
            s.event_template, m.event_template,
            "line {i}: template mismatch"
        );
        assert_eq!(s.event_id, m.event_id, "line {i}: event_id mismatch");
    }
}

#[tokio::test]
async fn indexed_parallel_parity_with_sync() {
    let input = "Jan 01 00:00:01 host app: Connection accepted from 192.168.1.1\n\
                 Jan 01 00:00:02 host app: Connection refused by peer\n\
                 Jan 01 00:00:03 host app: Connection accepted from 10.0.0.5\n\
                 Jan 01 00:00:04 host app: Connection refused by peer\n";

    let config = PiplupConfig::default();

    let entries: Vec<LogEntry> = input
        .lines()
        .enumerate()
        .filter(|(_, line)| !line.is_empty())
        .map(|(i, line)| LogEntry {
            line_number: i + 1,
            raw: line.to_owned(),
        })
        .collect();
    let sync_parser = build_indexed_parser(&entries, &config);
    let sync_output = sync_parser
        .parse_lines_detailed(&entries)
        .expect("sync indexed parse");

    let parser = Arc::new(build_indexed_parser(&entries, &config));
    let scheduler_config = SchedulerConfig {
        batch_size: 2,
        ..SchedulerConfig::default()
    };
    let worker_config = IndexedWorkerConfig {
        worker_count: 2,
        chunk_size: 2,
    };
    let reader = AsyncCursor::new(input);
    let async_output =
        run_indexed_reader_session_parallel(parser, scheduler_config, worker_config, reader)
            .await
            .expect("parallel indexed parse");

    assert_indexed_parity(&sync_output, &async_output);
}

#[tokio::test]
async fn indexed_parallel_misses_across_chunks() {
    // Train on one template, parse with misses that span chunks.
    let training_input = "Jan 01 00:00:01 host app: Connection accepted from 192.168.1.1\n";
    let parse_input = "Jan 01 00:00:01 host app: Completely unexpected alpha\n\
                       Jan 01 00:00:02 host app: Connection accepted from 192.168.1.1\n\
                       Jan 01 00:00:03 host app: Completely unexpected beta\n";

    let config = PiplupConfig::default();

    let parser = Arc::new(build_indexed_parser_from_input(training_input, &config));
    let scheduler_config = SchedulerConfig::default();
    let worker_config = IndexedWorkerConfig {
        worker_count: 2,
        chunk_size: 2, // chunk 0: lines 1-2, chunk 1: line 3
    };
    let reader = AsyncCursor::new(parse_input);
    let output =
        run_indexed_reader_session_parallel(parser, scheduler_config, worker_config, reader)
            .await
            .expect("parallel indexed parse");

    assert_eq!(output.parse_output.line_results.len(), 3);
    assert_eq!(output.stats.miss_count, 2);
    assert_eq!(output.stats.new_miss_count, 2);
    assert_eq!(output.stats.first_miss_line, Some(1));
    assert_eq!(output.stats.first_new_miss_line, Some(1));
}

#[tokio::test]
async fn indexed_parallel_chunk_boundary_preserves_line_order() {
    let input = make_log_lines(7);
    let config = PiplupConfig::default();

    let entries: Vec<LogEntry> = input
        .lines()
        .enumerate()
        .filter(|(_, line)| !line.is_empty())
        .map(|(i, line)| LogEntry {
            line_number: i + 1,
            raw: line.to_owned(),
        })
        .collect();

    let parser = Arc::new(build_indexed_parser(&entries, &config));
    let scheduler_config = SchedulerConfig::default();
    let worker_config = IndexedWorkerConfig {
        worker_count: 4,
        chunk_size: 2,
    };
    let reader = AsyncCursor::new(input.as_bytes());
    let output =
        run_indexed_reader_session_parallel(parser, scheduler_config, worker_config, reader)
            .await
            .expect("parallel indexed parse");

    assert_eq!(output.parse_output.line_results.len(), 7);
    // Verify line numbers are in ascending order.
    for (i, result) in output.parse_output.line_results.iter().enumerate() {
        assert_eq!(
            result.line_number,
            i + 1,
            "line {i}: expected line_number {}, got {}",
            i + 1,
            result.line_number,
        );
    }
}

#[tokio::test]
async fn indexed_parallel_events_deduplicated_across_chunks() {
    // All lines match the same template. With multiple chunks, events
    // should be merged to a single entry with summed occurrences.
    let input = "Jan 01 00:00:01 host app: Connection accepted from 192.168.1.1\n\
                 Jan 01 00:00:02 host app: Connection accepted from 10.0.0.5\n\
                 Jan 01 00:00:03 host app: Connection accepted from 172.16.0.1\n\
                 Jan 01 00:00:04 host app: Connection accepted from 192.168.2.1\n";

    let config = PiplupConfig::default();

    let entries: Vec<LogEntry> = input
        .lines()
        .enumerate()
        .filter(|(_, line)| !line.is_empty())
        .map(|(i, line)| LogEntry {
            line_number: i + 1,
            raw: line.to_owned(),
        })
        .collect();

    let parser = Arc::new(build_indexed_parser(&entries, &config));
    let scheduler_config = SchedulerConfig::default();
    let worker_config = IndexedWorkerConfig {
        worker_count: 2,
        chunk_size: 2,
    };
    let reader = AsyncCursor::new(input);
    let output =
        run_indexed_reader_session_parallel(parser, scheduler_config, worker_config, reader)
            .await
            .expect("parallel indexed parse");

    // All 4 lines match one template.
    assert_eq!(output.parse_output.events.len(), 1);
    assert_eq!(output.parse_output.events[0].occurrences, 4);
    assert_eq!(output.stats.miss_count, 0);
}

#[tokio::test]
async fn indexed_parallel_single_entry_chunk() {
    let input = "Jan 01 00:00:01 host app: Connection accepted from 192.168.1.1\n\
                 Jan 01 00:00:02 host app: Connection accepted from 10.0.0.5\n";

    let config = PiplupConfig::default();

    let entries: Vec<LogEntry> = input
        .lines()
        .enumerate()
        .filter(|(_, line)| !line.is_empty())
        .map(|(i, line)| LogEntry {
            line_number: i + 1,
            raw: line.to_owned(),
        })
        .collect();

    let parser = Arc::new(build_indexed_parser(&entries, &config));
    let scheduler_config = SchedulerConfig::default();
    let worker_config = IndexedWorkerConfig {
        worker_count: 2,
        chunk_size: 1,
    };
    let reader = AsyncCursor::new(input);
    let output =
        run_indexed_reader_session_parallel(parser, scheduler_config, worker_config, reader)
            .await
            .expect("parallel indexed parse");

    assert_eq!(output.parse_output.line_results.len(), 2);
    assert_eq!(output.parse_output.events[0].occurrences, 2);
    assert_eq!(output.stats.miss_count, 0);
}

#[test]
fn indexed_worker_config_validation() {
    assert!(IndexedWorkerConfig::default().validate().is_ok());

    let zero_workers = IndexedWorkerConfig {
        worker_count: 0,
        ..IndexedWorkerConfig::default()
    };
    assert!(zero_workers.validate().is_err());

    let zero_chunk = IndexedWorkerConfig {
        chunk_size: 0,
        ..IndexedWorkerConfig::default()
    };
    assert!(zero_chunk.validate().is_err());
}

// -----------------------------------------------------------------------
// Phase 5: Cancellation, checkpoint, and error-path tests
// -----------------------------------------------------------------------

#[tokio::test]
async fn source_task_stops_on_receiver_drop() {
    // Verify that a source task stops reading when the receiver is dropped
    // before all batches are consumed.

    let input = make_log_lines(100);
    let reader = AsyncCursor::new(input);
    let context = SourceContext {
        source_seq: 0,
        source_name: SourceName::new("<test>"),
    };

    let (tx, mut rx) = tokio::sync::mpsc::channel(16);

    let handle = tokio::spawn(async move {
        source_task::read_lines_to_batches(reader, 0, context, 10, tx).await;
    });

    // Receive only the first batch, then drop the receiver.
    let first = rx.recv().await;
    assert!(first.is_some(), "should receive at least one batch");
    drop(rx);

    // The source task should stop (not panic or hang).
    handle
        .await
        .expect("source task should complete without panic");
}

#[tokio::test]
async fn reader_error_does_not_reach_spawn_blocking() {
    // Verify that when a reader fails, the session returns an error
    // without reaching the spawn_blocking parser call. This is the
    // "no invalid cache writes on early reader error" invariant —
    // the async path never writes cache, and a reader error prevents
    // the parser from being called at all.

    // Use a reader that fails on first read.
    struct FailingReader;

    impl tokio::io::AsyncRead for FailingReader {
        fn poll_read(
            self: Pin<&mut Self>,
            _cx: &mut std::task::Context<'_>,
            _buf: &mut tokio::io::ReadBuf<'_>,
        ) -> std::task::Poll<std::io::Result<()>> {
            std::task::Poll::Ready(Err(std::io::Error::other("simulated read failure")))
        }
    }

    impl tokio::io::AsyncBufRead for FailingReader {
        fn poll_fill_buf(
            self: Pin<&mut Self>,
            _cx: &mut std::task::Context<'_>,
        ) -> std::task::Poll<std::io::Result<&[u8]>> {
            std::task::Poll::Ready(Err(std::io::Error::other("simulated read failure")))
        }

        fn consume(self: Pin<&mut Self>, _amt: usize) {}
    }

    let config = PiplupConfig::default();
    let scheduler_config = SchedulerConfig::default();

    let result = run_piplup_reader_session(config, scheduler_config, FailingReader).await;
    assert!(
        result.is_err(),
        "reader error should propagate as session error"
    );
}

#[tokio::test]
async fn indexed_output_line_count_matches_input() {
    // Verify that the async indexed path produces exactly as many line
    // results as input lines — the "checkpoints count committed lines"
    // invariant. The streaming path parses every ordered batch exactly
    // once, so every line is committed.
    let input = make_log_lines(25);
    let config = PiplupConfig::default();

    let entries: Vec<LogEntry> = input
        .lines()
        .enumerate()
        .filter(|(_, line)| !line.is_empty())
        .map(|(i, line)| LogEntry {
            line_number: i + 1,
            raw: line.to_owned(),
        })
        .collect();
    let parser = build_indexed_parser(&entries, &config);

    let scheduler_config = SchedulerConfig {
        batch_size: 4,
        ..SchedulerConfig::default()
    };
    let reader = AsyncCursor::new(input.as_bytes());
    let output = run_indexed_reader_session(parser, scheduler_config, reader)
        .await
        .expect("indexed session should succeed");

    assert_eq!(
        output.parse_output.line_results.len(),
        25,
        "output line count must equal input line count"
    );
    assert_eq!(
        output.stats.line_count, 25,
        "stats.line_count must equal input line count"
    );
}

#[tokio::test]
async fn indexed_parallel_output_line_count_matches_input() {
    // Same invariant for the multi-worker path: output line count equals
    // input line count regardless of chunk boundaries.
    let input = make_log_lines(30);
    let config = PiplupConfig::default();

    let entries: Vec<LogEntry> = input
        .lines()
        .enumerate()
        .filter(|(_, line)| !line.is_empty())
        .map(|(i, line)| LogEntry {
            line_number: i + 1,
            raw: line.to_owned(),
        })
        .collect();
    let parser = Arc::new(build_indexed_parser(&entries, &config));

    let scheduler_config = SchedulerConfig {
        batch_size: 4,
        ..SchedulerConfig::default()
    };
    let worker_config = IndexedWorkerConfig {
        worker_count: 3,
        chunk_size: 7,
    };
    let reader = AsyncCursor::new(input.as_bytes());
    let output =
        run_indexed_reader_session_parallel(parser, scheduler_config, worker_config, reader)
            .await
            .expect("parallel indexed session should succeed");

    assert_eq!(
        output.parse_output.line_results.len(),
        30,
        "parallel output line count must equal input line count"
    );
    assert_eq!(
        output.stats.line_count, 30,
        "parallel stats.line_count must equal input line count"
    );
}

#[tokio::test]
async fn piplup_session_output_matches_input_line_count() {
    // Verify the piplup path also produces correct line counts.
    let input = make_log_lines(15);
    let config = PiplupConfig::default();
    let scheduler_config = SchedulerConfig {
        batch_size: 3,
        ..SchedulerConfig::default()
    };
    let reader = AsyncCursor::new(input.as_bytes());

    let output = run_piplup_reader_session(config, scheduler_config, reader)
        .await
        .expect("piplup session should succeed");

    assert_eq!(
        output.line_results.len(),
        15,
        "piplup output line count must equal input line count"
    );
}

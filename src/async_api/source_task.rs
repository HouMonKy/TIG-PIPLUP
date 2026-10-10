//! Source task implementation.
//!
//! A source task reads raw lines from an `AsyncBufRead` or a file path and
//! emits source-local `SourceTaskEvent` values. It does not parse log formats
//! or run parser algorithms.

use std::path::PathBuf;

use tokio::io::{AsyncBufRead, AsyncBufReadExt};

use super::types::{SourceBatch, SourceContext, SourceTaskEvent};
use crate::types::LogEntry;

/// Read all lines from an `AsyncBufRead` and emit source-local batches.
///
/// Lines are read until EOF or an error. Each batch contains up to
/// `batch_size` entries. The function emits `SourceTaskEvent::Finished`
/// on clean EOF and `SourceTaskEvent::Failed` on read errors.
///
/// Blank lines are preserved as `LogEntry` values with an empty `raw`
/// string, matching the synchronous stdin/file reader behavior. The parser
/// is responsible for deciding how to handle them.
///
/// # Arguments
///
/// * `reader` — The async buffered reader to read from.
/// * `source_seq` — The sequence number assigned by the session planner.
/// * `context` — Diagnostic context for this source.
/// * `batch_size` — Maximum number of entries per batch.
/// * `tx` — Channel sender for source task events.
pub async fn read_lines_to_batches<S>(
    mut reader: S,
    source_seq: usize,
    context: SourceContext,
    batch_size: usize,
    tx: tokio::sync::mpsc::Sender<SourceTaskEvent>,
) where
    S: AsyncBufRead + Unpin,
{
    let mut local_batch_seq = 0usize;
    let mut line_number = 1usize;
    let mut buffer = String::new();

    loop {
        let mut entries = Vec::with_capacity(batch_size);

        for _ in 0..batch_size {
            buffer.clear();
            match reader.read_line(&mut buffer).await {
                Ok(0) => {
                    // EOF
                    break;
                }
                Ok(_n) => {
                    let line = trim_line_ending(&buffer);
                    entries.push(LogEntry {
                        line_number,
                        raw: line.to_owned(),
                    });
                    line_number += 1;
                }
                Err(e) => {
                    let _ = tx
                        .send(SourceTaskEvent::Failed {
                            source_seq,
                            local_batch_seq,
                            context,
                            error: e.into(),
                        })
                        .await;
                    return;
                }
            }
        }

        if entries.is_empty() {
            break;
        }

        let batch = SourceBatch {
            source_seq,
            local_batch_seq,
            entries,
            context: context.clone(),
        };
        local_batch_seq += 1;

        if tx.send(SourceTaskEvent::Batch(batch)).await.is_err() {
            // Receiver dropped — session cancelled.
            return;
        }
    }

    let _ = tx.send(SourceTaskEvent::Finished { source_seq }).await;
}

/// Read all lines from a file and emit source-local batches.
///
/// Opens the file at `path` using [`tokio::fs::File`], wraps it in a
/// `BufReader`, and delegates to [`read_lines_to_batches`]. On file-open
/// failure, emits `SourceTaskEvent::Failed` before returning.
///
/// # Arguments
///
/// * `path` — Path to the file to read.
/// * `source_seq` — The sequence number assigned by the session planner.
/// * `context` — Diagnostic context for this source.
/// * `batch_size` — Maximum number of entries per batch.
/// * `tx` — Channel sender for source task events.
pub(crate) async fn read_file_to_batches(
    path: PathBuf,
    source_seq: usize,
    context: SourceContext,
    batch_size: usize,
    tx: tokio::sync::mpsc::Sender<SourceTaskEvent>,
) {
    let file = match tokio::fs::File::open(&path).await {
        Ok(f) => f,
        Err(e) => {
            let _ = tx
                .send(SourceTaskEvent::Failed {
                    source_seq,
                    local_batch_seq: 0,
                    context,
                    error: e.into(),
                })
                .await;
            return;
        }
    };
    let reader = tokio::io::BufReader::new(file);
    read_lines_to_batches(reader, source_seq, context, batch_size, tx).await;
}

/// Strip `\n` or `\r\n` from the end of `line`.
fn trim_line_ending(line: &str) -> &str {
    line.strip_suffix("\r\n")
        .or_else(|| line.strip_suffix('\n'))
        .unwrap_or(line)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::pin::Pin;

    /// A simple async wrapper around `std::io::Cursor<Vec<u8>>` that
    /// implements `tokio::io::AsyncBufRead` for tests.
    struct AsyncCursor(std::io::Cursor<Vec<u8>>);

    impl AsyncCursor {
        fn new(data: impl Into<Vec<u8>>) -> Self {
            AsyncCursor(std::io::Cursor::new(data.into()))
        }
    }

    impl tokio::io::AsyncRead for AsyncCursor {
        fn poll_read(
            self: Pin<&mut Self>,
            _cx: &mut std::task::Context<'_>,
            buf: &mut tokio::io::ReadBuf<'_>,
        ) -> std::task::Poll<std::io::Result<()>> {
            let this = self.get_mut();
            let unfilled = buf.initialize_unfilled();
            let n = std::io::Read::read(&mut this.0, unfilled)?;
            buf.advance(n);
            std::task::Poll::Ready(Ok(()))
        }
    }

    impl tokio::io::AsyncBufRead for AsyncCursor {
        fn poll_fill_buf(
            self: Pin<&mut Self>,
            _cx: &mut std::task::Context<'_>,
        ) -> std::task::Poll<std::io::Result<&[u8]>> {
            let this = self.get_mut();
            std::task::Poll::Ready(std::io::BufRead::fill_buf(&mut this.0))
        }

        fn consume(self: Pin<&mut Self>, amt: usize) {
            self.get_mut().0.consume(amt);
        }
    }

    fn test_context() -> SourceContext {
        SourceContext {
            source_seq: 0,
            source_name: super::super::types::SourceName::new("test"),
        }
    }

    #[tokio::test]
    async fn read_lines_produces_ordered_batches() {
        let input = "line 1\nline 2\nline 3\nline 4\nline 5\n";
        let reader = AsyncCursor::new(input);
        let (tx, mut rx) = tokio::sync::mpsc::channel(16);

        read_lines_to_batches(reader, 0, test_context(), 2, tx).await;

        let mut events = Vec::new();
        while let Some(event) = rx.recv().await {
            events.push(event);
        }

        // 5 lines / 2 per batch = 3 batches + 1 Finished
        assert_eq!(events.len(), 4);

        // Batch 0: lines 1-2
        match &events[0] {
            SourceTaskEvent::Batch(b) => {
                assert_eq!(b.local_batch_seq, 0);
                assert_eq!(b.entries.len(), 2);
                assert_eq!(b.entries[0].raw, "line 1");
                assert_eq!(b.entries[1].raw, "line 2");
            }
            _ => panic!("expected Batch event"),
        }

        // Batch 1: lines 3-4
        match &events[1] {
            SourceTaskEvent::Batch(b) => {
                assert_eq!(b.local_batch_seq, 1);
                assert_eq!(b.entries.len(), 2);
            }
            _ => panic!("expected Batch event"),
        }

        // Batch 2: line 5
        match &events[2] {
            SourceTaskEvent::Batch(b) => {
                assert_eq!(b.local_batch_seq, 2);
                assert_eq!(b.entries.len(), 1);
                assert_eq!(b.entries[0].raw, "line 5");
            }
            _ => panic!("expected Batch event"),
        }

        // Finished
        match &events[3] {
            SourceTaskEvent::Finished { source_seq } => {
                assert_eq!(*source_seq, 0);
            }
            _ => panic!("expected Finished event"),
        }
    }

    #[tokio::test]
    async fn read_lines_preserves_blank_lines() {
        let input = "line 1\n\nline 3\n";
        let reader = AsyncCursor::new(input);
        let (tx, mut rx) = tokio::sync::mpsc::channel(16);

        read_lines_to_batches(reader, 0, test_context(), 10, tx).await;

        let mut events = Vec::new();
        while let Some(event) = rx.recv().await {
            events.push(event);
        }

        assert_eq!(events.len(), 2); // 1 batch + Finished

        match &events[0] {
            SourceTaskEvent::Batch(b) => {
                // Blank line is preserved with empty raw, matching sync behavior.
                assert_eq!(b.entries.len(), 3);
                assert_eq!(b.entries[0].line_number, 1);
                assert_eq!(b.entries[0].raw, "line 1");
                assert_eq!(b.entries[1].line_number, 2);
                assert_eq!(b.entries[1].raw, "");
                assert_eq!(b.entries[2].line_number, 3);
                assert_eq!(b.entries[2].raw, "line 3");
            }
            _ => panic!("expected Batch event"),
        }
    }

    #[tokio::test]
    async fn read_lines_handles_empty_input() {
        let input = "";
        let reader = AsyncCursor::new(input);
        let (tx, mut rx) = tokio::sync::mpsc::channel(16);

        read_lines_to_batches(reader, 0, test_context(), 10, tx).await;

        let mut events = Vec::new();
        while let Some(event) = rx.recv().await {
            events.push(event);
        }

        assert_eq!(events.len(), 1); // Finished only
        match &events[0] {
            SourceTaskEvent::Finished { source_seq } => assert_eq!(*source_seq, 0),
            _ => panic!("expected Finished event"),
        }
    }

    #[tokio::test]
    async fn read_lines_handles_crlf() {
        let input = "line 1\r\nline 2\r\n";
        let reader = AsyncCursor::new(input);
        let (tx, mut rx) = tokio::sync::mpsc::channel(16);

        read_lines_to_batches(reader, 0, test_context(), 10, tx).await;

        let mut events = Vec::new();
        while let Some(event) = rx.recv().await {
            events.push(event);
        }

        match &events[0] {
            SourceTaskEvent::Batch(b) => {
                assert_eq!(b.entries[0].raw, "line 1");
                assert_eq!(b.entries[1].raw, "line 2");
            }
            _ => panic!("expected Batch event"),
        }
    }

    #[tokio::test]
    async fn read_lines_stops_on_dropped_receiver() {
        let input = "line 1\nline 2\nline 3\n";
        let reader = AsyncCursor::new(input);
        let (tx, rx) = tokio::sync::mpsc::channel(1);

        // Drop the receiver immediately so sends will fail.
        drop(rx);

        // Should not panic, just return cleanly.
        read_lines_to_batches(reader, 0, test_context(), 10, tx).await;
    }

    #[tokio::test]
    async fn read_lines_handles_no_trailing_newline() {
        let input = "line 1\nline 2";
        let reader = AsyncCursor::new(input);
        let (tx, mut rx) = tokio::sync::mpsc::channel(16);

        read_lines_to_batches(reader, 0, test_context(), 10, tx).await;

        let mut events = Vec::new();
        while let Some(event) = rx.recv().await {
            events.push(event);
        }

        match &events[0] {
            SourceTaskEvent::Batch(b) => {
                assert_eq!(b.entries.len(), 2);
                assert_eq!(b.entries[0].raw, "line 1");
                assert_eq!(b.entries[1].raw, "line 2");
            }
            _ => panic!("expected Batch event"),
        }
    }

    #[tokio::test]
    async fn read_lines_handles_single_line() {
        let input = "only line\n";
        let reader = AsyncCursor::new(input);
        let (tx, mut rx) = tokio::sync::mpsc::channel(16);

        read_lines_to_batches(reader, 0, test_context(), 10, tx).await;

        let mut events = Vec::new();
        while let Some(event) = rx.recv().await {
            events.push(event);
        }

        assert_eq!(events.len(), 2); // 1 batch + Finished
        match &events[0] {
            SourceTaskEvent::Batch(b) => {
                assert_eq!(b.entries.len(), 1);
                assert_eq!(b.entries[0].raw, "only line");
                assert_eq!(b.entries[0].line_number, 1);
            }
            _ => panic!("expected Batch event"),
        }
    }

    // -----------------------------------------------------------------------
    // File source tests
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn read_file_produces_ordered_batches() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("test.log");
        std::fs::write(&path, "line 1\nline 2\nline 3\nline 4\nline 5\n").expect("write");

        let (tx, mut rx) = tokio::sync::mpsc::channel(16);
        read_file_to_batches(path.to_path_buf(), 0, test_context(), 2, tx).await;

        let mut events = Vec::new();
        while let Some(event) = rx.recv().await {
            events.push(event);
        }

        // 5 lines / 2 per batch = 3 batches + 1 Finished
        assert_eq!(events.len(), 4);

        // Batch 0: lines 1-2
        match &events[0] {
            SourceTaskEvent::Batch(b) => {
                assert_eq!(b.entries.len(), 2);
                assert_eq!(b.entries[0].raw, "line 1");
                assert_eq!(b.entries[0].line_number, 1);
            }
            _ => panic!("expected Batch"),
        }

        // Finished
        match &events[3] {
            SourceTaskEvent::Finished { source_seq } => assert_eq!(*source_seq, 0),
            _ => panic!("expected Finished"),
        }
    }

    #[tokio::test]
    async fn read_file_not_found_emits_failed() {
        let (tx, mut rx) = tokio::sync::mpsc::channel(16);
        read_file_to_batches(
            PathBuf::from("/nonexistent/path/file.log"),
            0,
            test_context(),
            10,
            tx,
        )
        .await;

        let mut events = Vec::new();
        while let Some(event) = rx.recv().await {
            events.push(event);
        }

        assert_eq!(events.len(), 1);
        match &events[0] {
            SourceTaskEvent::Failed {
                source_seq, error, ..
            } => {
                assert_eq!(*source_seq, 0);
                // Just verify it's an IO error — the exact message is locale-dependent.
                assert!(
                    error.to_string().contains("os error") || error.to_string().contains("error"),
                    "error should describe the failure: {}",
                    error
                );
            }
            _ => panic!("expected Failed, got {:?}", events[0]),
        }
    }

    #[tokio::test]
    async fn read_file_empty_produces_finished_only() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("empty.log");
        std::fs::write(&path, "").expect("write");

        let (tx, mut rx) = tokio::sync::mpsc::channel(16);
        read_file_to_batches(path.to_path_buf(), 0, test_context(), 10, tx).await;

        let mut events = Vec::new();
        while let Some(event) = rx.recv().await {
            events.push(event);
        }

        assert_eq!(events.len(), 1);
        match &events[0] {
            SourceTaskEvent::Finished { source_seq } => assert_eq!(*source_seq, 0),
            _ => panic!("expected Finished"),
        }
    }

    #[tokio::test]
    async fn read_file_preserves_blank_lines() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("blanks.log");
        std::fs::write(&path, "line 1\n\nline 3\n").expect("write");

        let (tx, mut rx) = tokio::sync::mpsc::channel(16);
        read_file_to_batches(path.to_path_buf(), 0, test_context(), 10, tx).await;

        let mut events = Vec::new();
        while let Some(event) = rx.recv().await {
            events.push(event);
        }

        assert_eq!(events.len(), 2); // 1 batch + Finished
        match &events[0] {
            SourceTaskEvent::Batch(b) => {
                assert_eq!(b.entries.len(), 3);
                assert_eq!(b.entries[1].raw, "");
                assert_eq!(b.entries[1].line_number, 2);
            }
            _ => panic!("expected Batch"),
        }
    }
}

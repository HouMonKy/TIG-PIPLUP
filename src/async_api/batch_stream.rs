use std::collections::VecDeque;

use tokio::sync::mpsc;
use tokio::task::JoinHandle;

use super::assembler::{AssemblerOutput, OrderedAssembler};
use super::numbering::LineNumberingAdapter;
use super::scheduler::SourceScheduler;
use super::types::{LineNumbering, SchedulerConfig, SourceTaskEvent, TransportEvent};
use crate::error::LogParseError;
use crate::types::LogEntry;

pub(crate) struct BatchStream {
    transport_rx: mpsc::Receiver<TransportEvent>,
    assembler: OrderedAssembler,
    numbering: LineNumberingAdapter,
    ready_queue: VecDeque<Vec<LogEntry>>,
    source_handle: Option<JoinHandle<()>>,
    scheduler_handle: Option<JoinHandle<()>>,
    done: bool,
}

impl BatchStream {
    pub(crate) async fn new(
        scheduler_config: SchedulerConfig,
        line_numbering: LineNumbering,
        source_count: usize,
        produce: impl FnOnce(mpsc::Sender<SourceTaskEvent>) -> JoinHandle<()>,
    ) -> Result<Self, LogParseError> {
        scheduler_config.validate()?;

        let (source_tx, mut source_rx) = mpsc::channel(scheduler_config.source_channel_capacity);
        let (transport_tx, transport_rx) =
            mpsc::channel(scheduler_config.transport_channel_capacity);

        let source_handle = produce(source_tx);

        let mut scheduler = SourceScheduler::new(&scheduler_config);
        for seq in 0..source_count {
            scheduler.activate_source(seq);
        }

        let scheduler_handle = tokio::spawn(async move {
            while let Some(event) = source_rx.recv().await {
                let transport_events = scheduler.handle_event(event);
                for te in transport_events {
                    if transport_tx.send(te).await.is_err() {
                        return;
                    }
                }
            }
        });

        Ok(Self {
            transport_rx,
            assembler: OrderedAssembler::new(),
            numbering: LineNumberingAdapter::new(line_numbering),
            ready_queue: VecDeque::new(),
            source_handle: Some(source_handle),
            scheduler_handle: Some(scheduler_handle),
            done: false,
        })
    }

    pub(crate) async fn next_batch(&mut self) -> Option<Result<Vec<LogEntry>, LogParseError>> {
        if self.done {
            return None;
        }

        if let Some(batch) = self.ready_queue.pop_front() {
            return Some(Ok(batch));
        }

        loop {
            let Some(event) = self.transport_rx.recv().await else {
                self.done = true;
                if self.assembler.is_complete() {
                    return None;
                }
                return Some(Err(LogParseError::Io(std::io::Error::other(
                    "transport channel closed before end-of-input; internal pipeline shutdown",
                ))));
            };
            match self.assembler.handle_event(event) {
                AssemblerOutput::Batches(batches) => {
                    let mut first_batch = None;
                    for mut batch in batches {
                        self.numbering.rewrite(&mut batch);
                        if first_batch.is_none() {
                            first_batch = Some(batch.entries);
                        } else {
                            self.ready_queue.push_back(batch.entries);
                        }
                    }
                    if let Some(batch) = first_batch {
                        return Some(Ok(batch));
                    }
                }
                AssemblerOutput::EndOfInput => {
                    self.done = true;
                    if self.assembler.is_complete() {
                        return None;
                    }
                    return Some(Err(LogParseError::Io(std::io::Error::other(
                        "session ended with unreceived batches — internal order gap",
                    ))));
                }
                AssemblerOutput::Failed(error) => {
                    self.done = true;
                    return Some(Err(error));
                }
            }
        }
    }
}

impl Drop for BatchStream {
    fn drop(&mut self) {
        self.transport_rx.close();
        if let Some(handle) = self.source_handle.take() {
            handle.abort();
        }
        if let Some(handle) = self.scheduler_handle.take() {
            handle.abort();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::async_api::test_util::AsyncCursor;

    fn make_log_lines(n: usize) -> String {
        (1..=n)
            .map(|i| format!("Jan 01 00:00:{:02} host app: message {i}\n", i % 60))
            .collect()
    }

    #[tokio::test]
    async fn batch_stream_yields_all_entries() {
        let input = make_log_lines(100);
        let reader = AsyncCursor::new(input);
        let config = SchedulerConfig::default();
        let batch_size = config.batch_size;

        let mut stream =
            BatchStream::new(config, LineNumbering::PreserveSourceLocal, 1, |source_tx| {
                tokio::spawn(async move {
                    let context = super::super::types::SourceContext {
                        source_seq: 0,
                        source_name: super::super::types::SourceName::new("<test>"),
                    };
                    super::super::source_task::read_lines_to_batches(
                        reader, 0, context, batch_size, source_tx,
                    )
                    .await;
                })
            })
            .await
            .unwrap();

        let mut total = 0;
        while let Some(result) = stream.next_batch().await {
            let entries = result.unwrap();
            total += entries.len();
        }
        assert_eq!(total, 100);
    }

    #[tokio::test]
    async fn batch_stream_drop_cleans_up() {
        let input = make_log_lines(1000);
        let reader = crate::async_api::test_util::AsyncCursor::new(input);
        let config = SchedulerConfig::default();
        let batch_size = config.batch_size;

        let mut stream =
            BatchStream::new(config, LineNumbering::PreserveSourceLocal, 1, |source_tx| {
                tokio::spawn(async move {
                    let context = super::super::types::SourceContext {
                        source_seq: 0,
                        source_name: super::super::types::SourceName::new("<test>"),
                    };
                    super::super::source_task::read_lines_to_batches(
                        reader, 0, context, batch_size, source_tx,
                    )
                    .await;
                })
            })
            .await
            .unwrap();

        // Consume only one batch, then drop
        let _first = stream.next_batch().await.unwrap().unwrap();
        drop(stream);
        // If Drop impl panics or deadlocks, this test will fail.
    }
}

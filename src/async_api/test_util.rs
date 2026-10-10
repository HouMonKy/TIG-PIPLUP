//! Shared async test utilities for the `async_api` crate module.

use std::pin::Pin;

/// A simple async wrapper around `std::io::Cursor<Vec<u8>>`.
pub(crate) struct AsyncCursor(std::io::Cursor<Vec<u8>>);

impl AsyncCursor {
    pub(crate) fn new(data: impl Into<Vec<u8>>) -> Self {
        AsyncCursor(std::io::Cursor::new(data.into()))
    }
}

impl tokio::io::AsyncRead for AsyncCursor {
    fn poll_read(
        self: Pin<&mut Self>,
        _cx: &mut std::task::Context<'_>,
        buf: &mut tokio::io::ReadBuf<'_>,
    ) -> std::task::Poll<std::io::Result<()>> {
        let inner = self.get_mut();
        let n = std::io::Read::read(&mut inner.0, buf.initialize_unfilled())?;
        buf.advance(n);
        std::task::Poll::Ready(Ok(()))
    }
}

impl tokio::io::AsyncBufRead for AsyncCursor {
    fn poll_fill_buf(
        self: Pin<&mut Self>,
        _cx: &mut std::task::Context<'_>,
    ) -> std::task::Poll<std::io::Result<&[u8]>> {
        let inner = self.get_mut();
        let pos = inner.0.position() as usize;
        let buf = inner.0.get_ref();
        std::task::Poll::Ready(Ok(&buf[pos..]))
    }

    fn consume(self: Pin<&mut Self>, amount: usize) {
        let inner = self.get_mut();
        inner.0.set_position(inner.0.position() + amount as u64);
    }
}

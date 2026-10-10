//! Cache file binary header layout.
//!
//! The on-disk layout is:
//!
//! ```text
//! +-----------------+-------+-----------------+
//! | field           | bytes | encoding        |
//! +-----------------+-------+-----------------+
//! | magic           |     8 | ASCII "PPLCCH01"|
//! | format_version  |     4 | u32 LE          |
//! | header_len      |     4 | u32 LE (self)   |
//! | flags           |     1 | bitmask         |
//! | payload_len     |     8 | u64 LE          |
//! | runtime_hash    |    32 | SHA-256         |
//! | preprocess_hash |    32 | SHA-256         |
//! | state_hash      |    32 | SHA-256         |
//! +-----------------+-------+-----------------+
//! | payload         |  *    | bincode (lz4?)  |
//! +-----------------+-------+-----------------+
//! ```
//!
//! Bit 0 of `flags` is reserved for LZ4 compression. `state_hash` is always
//! computed over the **uncompressed** payload, so hash and compression are
//! orthogonal.

use std::io::{Read, Write};

use crate::error::LogParseError;

/// Magic bytes that identify a piplup cache file.
pub const CACHE_MAGIC: [u8; 8] = *b"PPLCCH01";

/// Current cache file format version.
pub const CACHE_FORMAT_VERSION: u32 = 1;

/// Bit in `flags` that, when set, indicates the payload is LZ4-compressed.
pub const FLAG_LZ4_COMPRESSED: u8 = 1 << 0;
const KNOWN_FLAGS: u8 = FLAG_LZ4_COMPRESSED;

/// Fixed header size in bytes: 8 (magic) + 4 + 4 + 1 + 8 + 32 + 32 + 32.
pub const SERIALIZED_LEN: usize = 8 + 4 + 4 + 1 + 8 + 32 + 32 + 32;

/// Fixed-size binary header for a piplup cache file.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CacheHeader {
    /// Cache file format version.
    pub format_version: u32,
    /// Header flags bitmask.
    pub flags: u8,
    /// Length of the payload in bytes.
    pub payload_len: u64,
    /// SHA-256 of the runtime configuration.
    pub runtime_config_hash: [u8; 32],
    /// SHA-256 of the preprocess plan.
    pub preprocess_plan_hash: [u8; 32],
    /// SHA-256 of the uncompressed payload.
    pub state_hash: [u8; 32],
}

impl CacheHeader {
    /// Create a new header.
    ///
    /// # Example
    ///
    /// ```rust
    /// use log_parser::cache::{CacheHeader, FLAG_LZ4_COMPRESSED};
    ///
    /// let header = CacheHeader::new(FLAG_LZ4_COMPRESSED, 1024, [0u8; 32], [0u8; 32], [0u8; 32]);
    /// assert!(header.is_compressed());
    /// ```
    pub fn new(
        flags: u8,
        payload_len: u64,
        runtime_config_hash: [u8; 32],
        preprocess_plan_hash: [u8; 32],
        state_hash: [u8; 32],
    ) -> Self {
        Self {
            format_version: CACHE_FORMAT_VERSION,
            flags,
            payload_len,
            runtime_config_hash,
            preprocess_plan_hash,
            state_hash,
        }
    }

    /// Return true if the payload is LZ4-compressed.
    pub fn is_compressed(&self) -> bool {
        self.flags & FLAG_LZ4_COMPRESSED != 0
    }

    /// Write the fixed-size header to `writer`. Returns the number of bytes
    /// written; the function writes exactly `SERIALIZED_LEN` bytes on success.
    pub fn write<W: Write>(&self, writer: &mut W) -> Result<usize, LogParseError> {
        if self.payload_len == 0 {
            return Err(LogParseError::InvalidCache {
                message: "payload_len must be non-zero".to_string(),
            });
        }

        let mut buffer = [0u8; SERIALIZED_LEN];
        buffer[..8].copy_from_slice(&CACHE_MAGIC);
        buffer[8..12].copy_from_slice(&self.format_version.to_le_bytes());
        buffer[12..16].copy_from_slice(&(SERIALIZED_LEN as u32).to_le_bytes());
        buffer[16] = self.flags;
        buffer[17..25].copy_from_slice(&self.payload_len.to_le_bytes());
        buffer[25..57].copy_from_slice(&self.runtime_config_hash);
        buffer[57..89].copy_from_slice(&self.preprocess_plan_hash);
        buffer[89..121].copy_from_slice(&self.state_hash);

        writer.write_all(&buffer)?;
        Ok(SERIALIZED_LEN)
    }

    /// Read and validate a header from `reader`.
    ///
    /// The reader's underlying source must contain at least `SERIALIZED_LEN`
    /// bytes; anything shorter is reported as `InvalidCache`. The reader is
    /// advanced past the header.
    pub fn read<R: Read>(reader: &mut R) -> Result<Self, LogParseError> {
        let mut buffer = [0u8; SERIALIZED_LEN];
        match reader.read_exact(&mut buffer) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::UnexpectedEof => {
                return Err(LogParseError::InvalidCache {
                    message: "cache file is truncated (header)".to_string(),
                });
            }
            Err(error) => return Err(LogParseError::Io(error)),
        }

        if buffer[..8] != CACHE_MAGIC {
            return Err(LogParseError::InvalidCache {
                message: "cache magic mismatch".to_string(),
            });
        }

        let format_version = u32::from_le_bytes(buffer[8..12].try_into().map_err(|_| {
            LogParseError::InvalidCache {
                message: "cache header format_version field is truncated".to_string(),
            }
        })?);
        if format_version != CACHE_FORMAT_VERSION {
            return Err(LogParseError::CacheVersionMismatch {
                file: format_version,
                expected: CACHE_FORMAT_VERSION,
            });
        }

        let header_len = u32::from_le_bytes(buffer[12..16].try_into().map_err(|_| {
            LogParseError::InvalidCache {
                message: "cache header header_len field is truncated".to_string(),
            }
        })?);
        if header_len as usize != SERIALIZED_LEN {
            return Err(LogParseError::InvalidCache {
                message: format!(
                    "cache header_len {header_len} does not match expected {}",
                    SERIALIZED_LEN
                ),
            });
        }

        let flags = buffer[16];
        if flags & !KNOWN_FLAGS != 0 {
            return Err(LogParseError::InvalidCache {
                message: format!("cache header has unknown flags set: 0x{flags:02x}"),
            });
        }
        let payload_len = u64::from_le_bytes(buffer[17..25].try_into().map_err(|_| {
            LogParseError::InvalidCache {
                message: "cache header payload_len field is truncated".to_string(),
            }
        })?);
        if payload_len == 0 {
            return Err(LogParseError::InvalidCache {
                message: "cache payload_len is zero".to_string(),
            });
        }

        let mut runtime_config_hash = [0u8; 32];
        runtime_config_hash.copy_from_slice(&buffer[25..57]);
        let mut preprocess_plan_hash = [0u8; 32];
        preprocess_plan_hash.copy_from_slice(&buffer[57..89]);
        let mut state_hash = [0u8; 32];
        state_hash.copy_from_slice(&buffer[89..121]);

        Ok(Self {
            format_version,
            flags,
            payload_len,
            runtime_config_hash,
            preprocess_plan_hash,
            state_hash,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    fn sample_header() -> CacheHeader {
        CacheHeader::new(
            FLAG_LZ4_COMPRESSED,
            1234,
            [0x11; 32],
            [0x22; 32],
            [0x33; 32],
        )
    }

    #[test]
    fn serialized_len_matches_layout() {
        assert_eq!(SERIALIZED_LEN, 8 + 4 + 4 + 1 + 8 + 32 + 32 + 32);
    }

    #[test]
    fn header_round_trip_via_bytes() {
        let header = sample_header();
        let mut buffer = Vec::new();
        let written = header.write(&mut buffer).expect("write should succeed");
        assert_eq!(written, SERIALIZED_LEN);
        assert_eq!(buffer.len(), SERIALIZED_LEN);

        let read_back = CacheHeader::read(&mut Cursor::new(&buffer)).expect("read should succeed");
        assert_eq!(read_back, header);
    }

    #[test]
    fn flags_byte_round_trips_preserving_compression_bit() {
        let header = CacheHeader::new(FLAG_LZ4_COMPRESSED, 100, [0; 32], [0; 32], [0; 32]);
        assert!(header.is_compressed());

        let mut buffer = Vec::new();
        header.write(&mut buffer).unwrap();
        let read_back = CacheHeader::read(&mut Cursor::new(&buffer)).unwrap();
        assert_eq!(read_back.flags, FLAG_LZ4_COMPRESSED);
        assert!(read_back.is_compressed());
    }

    #[test]
    fn unknown_header_flags_are_invalid_cache() {
        let header = CacheHeader::new(FLAG_LZ4_COMPRESSED, 100, [0; 32], [0; 32], [0; 32]);
        let mut buffer = Vec::new();
        header.write(&mut buffer).unwrap();
        buffer[16] |= 1 << 7;

        let error = CacheHeader::read(&mut Cursor::new(&buffer)).unwrap_err();
        assert!(
            matches!(error, LogParseError::InvalidCache { .. }),
            "got {error:?}"
        );
    }

    #[test]
    fn wrong_magic_is_invalid_cache() {
        let mut buffer = vec![0u8; SERIALIZED_LEN];
        buffer[..8].copy_from_slice(b"WRONG!! ");
        let error = CacheHeader::read(&mut Cursor::new(&buffer)).unwrap_err();
        assert!(
            matches!(error, LogParseError::InvalidCache { .. }),
            "got {error:?}"
        );
    }

    #[test]
    fn wrong_format_version_is_version_mismatch() {
        let mut buffer = Vec::new();
        let header = sample_header();
        header.write(&mut buffer).unwrap();

        // Patch the version field to a different value.
        let new_version: u32 = 99;
        buffer[8..12].copy_from_slice(&new_version.to_le_bytes());

        let error = CacheHeader::read(&mut Cursor::new(&buffer)).unwrap_err();
        match error {
            LogParseError::CacheVersionMismatch { file, expected } => {
                assert_eq!(file, 99);
                assert_eq!(expected, CACHE_FORMAT_VERSION);
            }
            other => panic!("expected version mismatch, got {other:?}"),
        }
    }

    #[test]
    fn wrong_header_len_is_invalid_cache() {
        let mut buffer = Vec::new();
        sample_header().write(&mut buffer).unwrap();

        // Patch header_len to a different value.
        let bad_len: u32 = 64;
        buffer[12..16].copy_from_slice(&bad_len.to_le_bytes());

        let error = CacheHeader::read(&mut Cursor::new(&buffer)).unwrap_err();
        assert!(
            matches!(error, LogParseError::InvalidCache { .. }),
            "got {error:?}"
        );
    }

    #[test]
    fn zero_payload_len_is_invalid_cache() {
        let mut buffer = Vec::new();
        CacheHeader::new(0, 0, [0; 32], [0; 32], [0; 32])
            .write(&mut buffer)
            .expect_err("zero payload must be rejected on write");
    }

    #[test]
    fn truncated_input_is_invalid_cache() {
        let buffer = vec![0u8; 16];
        let error = CacheHeader::read(&mut Cursor::new(&buffer)).unwrap_err();
        assert!(
            matches!(error, LogParseError::InvalidCache { .. }),
            "got {error:?}"
        );
    }
}

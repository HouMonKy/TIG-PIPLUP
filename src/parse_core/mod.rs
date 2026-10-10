//! 行解析与 index runtime 共享的算法中性 helper。
//!
//! 本模块为 `pub(crate)`，不进 `lib.rs` re-export。仅承载算法中性原语，
//! 不接收 parser state、cache、CLI、output serialization 或 algorithm dispatch。

mod entry;
mod evidence;
mod index_tokens;
mod validate;

pub(crate) use entry::parse_entry;
pub(crate) use evidence::{
    MaskEvidence, build_placeholder_evidence, finalize_mask_evidence, join_mask_evidence,
};
pub(crate) use index_tokens::{
    normalize_separators, observed_kinds_for_index, tokenize_index_content,
};
pub(crate) use validate::ensure_unique_line_numbers;

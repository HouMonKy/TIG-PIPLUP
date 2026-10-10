//! 输出构造的算法中性 helper。
//!
//! 本模块为 `pub(crate)`，不进 `lib.rs` re-export。仅承载算法中性输出原语
//! （事件聚合、参数提取、assignment 输出构造），不接收 parser state、cache
//! manager、CLI、algorithm dispatch、index candidate selection 或 Drain3 routing。

pub(crate) mod assignment;
mod events;
mod parameters;

pub(crate) use events::EventAccumulator;
pub(crate) use parameters::ParameterExtractor;

//! Minhash signature for ADR-0033 改良③ (variable-length clustering).
//!
//! When same-length positional similarity fails, Drain3 falls back to minhash
//! Jaccard over clusters in a token-count neighborhood (per the reference
//! article). Each cluster keeps a memoized signature over its (normalized)
//! template tokens; the estimated Jaccard between two signatures approximates
//! the token-set Jaccard, which is length- and order-independent — so it can
//! match variable-length logs the fixed-depth tree would otherwise isolate.
//!
//! The signature is a pure function of the (deduplicated) token set, so it can
//! be recomputed on cache restore without persistence (no DTO field, no cache
//! version bump).

use rustc_hash::FxHasher;
use std::hash::{Hash, Hasher};

/// Compute a minhash signature of `size` permutations over the token set.
///
/// Each token gets one base hash ([`FxHasher`]); each signature element is the
/// minimum of `a_i · base + c_i  (mod 2⁶⁴)` over the (deduplicated) token set,
/// a cheap 2-universal affine family whose min-wise agreement estimates Jaccard
/// similarity. Order- and duplicate-independent.
pub fn signature(tokens: &[String], size: usize) -> Vec<u64> {
    let mut base: Vec<u64> = tokens
        .iter()
        .map(|t| {
            let mut hasher = FxHasher::default();
            t.as_str().hash(&mut hasher);
            hasher.finish()
        })
        .collect();
    base.sort_unstable();
    base.dedup();
    (0..size)
        .map(|i| {
            // Odd multiplier (bijection on u64) + additive term ⇒ 2-universal.
            let a = (i as u64).wrapping_mul(0x9e37_79b9_7f4a_7c15) | 1;
            let c = (i as u64).wrapping_mul(0x6a09_e667_f3bc_c909);
            base.iter()
                .map(|&x: &u64| x.wrapping_mul(a).wrapping_add(c))
                .min()
                .unwrap_or(u64::MAX)
        })
        .collect()
}

/// Estimate Jaccard similarity from two equal-length signatures: the fraction
/// of permutation positions that agree. Empty or mismatched-length signatures
/// return `0.0` (incomparable).
pub fn estimated_jaccard(sig_a: &[u64], sig_b: &[u64]) -> f64 {
    if sig_a.is_empty() || sig_a.len() != sig_b.len() {
        return 0.0;
    }
    let agree = sig_a.iter().zip(sig_b).filter(|(a, b)| a == b).count();
    agree as f64 / sig_a.len() as f64
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identical_token_sets_have_jaccard_one() {
        let a = signature(&["worker".into(), "42".into(), "started".into()], 64);
        let b = signature(&["worker".into(), "42".into(), "started".into()], 64);
        assert!((estimated_jaccard(&a, &b) - 1.0).abs() < 1e-9);
    }

    #[test]
    fn signature_is_order_and_duplicate_independent() {
        let a = signature(&["a".into(), "b".into()], 32);
        let b = signature(&["b".into(), "a".into(), "a".into()], 32);
        assert_eq!(a, b);
    }

    #[test]
    fn disjoint_token_sets_have_low_jaccard() {
        let a = signature(&["worker".into(), "started".into()], 128);
        let b = signature(&["server".into(), "stopped".into()], 128);
        assert!(estimated_jaccard(&a, &b) < 0.2);
    }

    #[test]
    fn half_overlapping_token_sets_have_moderate_jaccard() {
        // 2 shared of 4 union → exact Jaccard 0.5; minhash estimate nearby.
        let a = signature(&["worker".into(), "job".into(), "started".into()], 256);
        let b = signature(&["worker".into(), "job".into(), "stopped".into()], 256);
        let j = estimated_jaccard(&a, &b);
        assert!(j > 0.3 && j < 0.7, "expected ~0.5, got {j}");
    }

    #[test]
    fn mismatched_lengths_return_zero() {
        let a = signature(&["a".into()], 32);
        let b = signature(&["a".into()], 64);
        assert_eq!(estimated_jaccard(&a, &b), 0.0);
    }
}

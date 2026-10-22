"""E3: confidence gating and body protection on top of E2-D boundaries.

The gate NEVER moves a boundary.  For every line where the E2-D detector
already produced a candidate strip (pred > 0), the gate decides:

    ACCEPT -> keep the E2-D boundary
    KEEP   -> refuse to strip, return the original line (pred = 0)

Lines the detector itself already abstains on stay KEEP.

Two complementary gates:

- E3-B (rule reliability): per (dataset, 4-span signature) family,
  offline statistics of the E2-D predictions on the VALIDATION split:
  support, accepted accuracy, over-strip rate, boundary stability.
  A family is accepted only if its composite reliability >= tau_B.
  Online cost: one dict lookup.  No M2/M3 statistics are recomputed
  online.

- E3-C (body risk): per-line structural evidence at the candidate
  boundary, learned as a small risk table over pooled validation strips:
      f_value_end       last consumed span is value-like (ID/NUM/HEX/
                        HOST/IP/DOTTED) -- the classic "value-like body
                        first word swallowed into the header" signature
      f_plain_ctail_end last consumed span is a BARE "word:" colon tail
                        without digits/brackets (component-style tails
                        like "sshd[19939]:" are structurally anchored)
      f_value_start     first body span is value-like
      f_weak_end        last consumed span is bare word-like content
                        (WORD/OTHER/FCALL) -- deep-overshoot signature
  A (f_value_end, f_plain_ctail_end, f_value_start, f_weak_end) combo whose
  validation over-rate exceeds tau_C (with support >= 5) is rejected.
  No dataset names, words, IPs or host patterns are hardcoded; the
  signals are structural and the thresholds are learned on validation.

- E3-D: E3-B AND E3-C (either gate saying KEEP rejects the strip).
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass

from .methods import M3_SIG_LEN
from .tokentypes import classify, signature_from_tokens, type_family

VALUE_LIKE_FAMS = {
    "ID", "NUM", "HEX", "HOST", "IP", "IP_PORT", "HOST_PORT", "DOTTED",
    "NUM_EPOCH",
}
#: headers rarely END in bare word-like spans; a boundary whose last
#: consumed span is plain body-ish content (WORD / OTHER / FCALL) is a
#: deep-overshoot signature (e.g. body fragments like `"GET` after a
#: swallowed constant IP)
WEAK_END_FAMS = {"WORD", "OTHER", "FCALL"}
RISK_MIN_SUPPORT = 5


# ---------------------------------------------------------------------------
# E3-B: rule reliability
# ---------------------------------------------------------------------------

@dataclass
class FamilyRule:
    dataset: str
    sig: tuple
    n_val_stripped: int = 0
    val_correct: int = 0
    val_over: int = 0
    val_under: int = 0
    modal_boundary: int = 0
    stability: float = 0.0
    reliability: float = 0.0

    @property
    def val_acc(self) -> float:
        return self.val_correct / self.n_val_stripped if self.n_val_stripped else 0.0

    @property
    def val_over_rate(self) -> float:
        return self.val_over / self.n_val_stripped if self.n_val_stripped else 1.0


def build_rules(dataset: str, det, val_items: list[dict]) -> dict[tuple, FamilyRule]:
    """Offline: reliability of E2-D strips per (signature, boundary-combo)
    rule, computed on the validation split.

    The combo dimension is the key design point: within one signature
    family, correct strips and over-strips often fall into DIFFERENT
    structural combos (e.g. OpenStack correct lines end at the client IP
    -> value_start=True, while over lines swallow an [instance: uuid]
    body bracket -> all-False combo).  Keying rules by (sig, combo)
    separates them instead of killing the whole family.
    """
    rules: dict[tuple, FamilyRule] = {}
    bnd: dict[tuple, Counter] = defaultdict(Counter)

    for it in val_items:
        pred = det.predict(it["line"])
        if pred <= 0:
            continue  # detector abstain: nothing to gate
        toks = det._tok(it["line"])
        sig = signature_from_tokens(toks, M3_SIG_LEN)
        feats = boundary_features(det, it["line"])
        combo = feats["combo"] if feats else (False, False, False, False)
        key = (sig, combo)
        if key not in rules:
            rules[key] = FamilyRule(dataset, sig)
        r = rules[key]
        r.n_val_stripped += 1
        bnd[key][feats["span_boundary"] if feats else 0] += 1
        gt = it["body_start"]
        if pred == gt:
            r.val_correct += 1
        elif pred > gt:
            r.val_over += 1
        else:
            r.val_under += 1

    for key, r in rules.items():
        c = bnd.get(key)
        if c and r.n_val_stripped:
            r.modal_boundary, cnt = c.most_common(1)[0]
            r.stability = cnt / r.n_val_stripped
        # composite reliability (E1-C style): accepted accuracy x support
        # factor x boundary stability x over-strip penalty
        sup = min(1.0, r.n_val_stripped / 10.0)
        r.reliability = round(
            max(0.0, r.val_acc * sup * r.stability * max(0.0, 1 - 4 * r.val_over_rate)),
            4)
    return rules


def gate_b(rule: FamilyRule | None, tau_b: float, min_support: int = 2) -> bool:
    """ACCEPT decision for E3-B.  Unknown rule (no val evidence) -> KEEP."""
    if rule is None:
        return False
    if rule.n_val_stripped < min_support:
        return False
    return rule.reliability >= tau_b


# ---------------------------------------------------------------------------
# E3-C: body risk
# ---------------------------------------------------------------------------

def boundary_features(det, line: str):
    """Structural features at the candidate boundary (span granularity)."""
    toks = det._tok(line)
    b = det.predict_token_boundary(line)
    if b <= 0 or b > len(toks):
        return None
    last = toks[b - 1].text
    first = toks[b].text if b < len(toks) else ""
    last_fam = type_family(classify(last))
    first_fam = type_family(classify(first)) if first else "END"
    f_value_end = last_fam in VALUE_LIKE_FAMS
    f_plain_ctail_end = (
        last_fam == "CTAIL"
        and not any(c.isdigit() for c in last)
        and "[" not in last and "(" not in last
    )
    f_value_start = first_fam in VALUE_LIKE_FAMS
    f_weak_end = last_fam in WEAK_END_FAMS
    return {
        "span_boundary": b,
        "last_fam": last_fam,
        "first_fam": first_fam,
        "combo": (f_value_end, f_plain_ctail_end, f_value_start, f_weak_end),
    }


class BodyRiskModel:
    """Risk table over boundary feature combos, fit on pooled val strips."""

    def __init__(self, table: dict[tuple, dict]):
        self.table = table

    @classmethod
    def fit(cls, records: list[dict]) -> "BodyRiskModel":
        """records: [{"combo": ..., "is_over": bool, "is_correct": bool}]"""
        agg: dict[tuple, Counter] = defaultdict(Counter)
        for rec in records:
            agg[rec["combo"]]["n"] += 1
            if rec["is_over"]:
                agg[rec["combo"]]["over"] += 1
        table = {
            k: {"n": c["n"], "over_rate": c["over"] / c["n"]}
            for k, c in agg.items()
        }
        return cls(table)

    def keep(self, combo: tuple, tau_c: float,
             min_support: int = RISK_MIN_SUPPORT) -> bool:
        """True -> KEEP (reject the strip)."""
        entry = self.table.get(combo)
        if entry is None or entry["n"] < min_support:
            return False
        return entry["over_rate"] > tau_c

    def describe(self):
        return {
            "(value_end, plain_ctail_end, value_start)": {
                str(k): {"n": v["n"], "val_over_rate": round(v["over_rate"], 4)}
                for k, v in sorted(self.table.items())
            }
        }

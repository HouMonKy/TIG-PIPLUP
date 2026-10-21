"""E1: M2/M3 offline adaptive rule learning (per-source families).

Architecture (experiment plan section 5):

    per SOURCE (= production log module; experiment: dataset) and per
    format family (4-token structural signature):
        learn split --M2/M3--> candidate rules
        val split   ----------> rule selection / filtering / confidence
    test split  ----------> per-dataset evaluation

Design decisions (see e1-adaptive-report.md for rationale):
- Rules are keyed by (source, signature).  A source is the log module a
  line came from -- available in production from the collector, and in the
  experiment from the dataset.  Selection between M2/M3 is NEVER made by
  dataset name: it is made per structural family from val performance.
- M2-rule  = flat positional M2 scan WITHIN the family's learn lines.
- M3-rule  = modal M3 boundary over the family's learn lines (M3 fitted
             per source on the source's learn stream).
- E1-A: per family select M2 or M3 (prefer lower over-strip risk; tie ->
  higher accuracy; tie -> simpler M2; both unreliable -> keep).
- E1-B: + filtering (support / val accuracy / val over-strip gates).
- E1-C: + confidence threshold tau (sweepable).

Leave-one-dataset-out (LODO) evaluates transfer: the held-out source has
no rules of its own; test lines match OTHER sources' rules by signature
alone (highest confidence wins), measuring whether unknown modules can be
handled safely by structural transfer.
"""
from __future__ import annotations

import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .methods import M2Detector, M3Detector, M3_SIG_LEN
from .metrics import evaluate
from .tokentypes import signature, token_boundary_to_char


@dataclass
class E1Config:
    seed: int = 42
    learn_frac: float = 0.30
    val_frac: float = 0.20   # test = 1 - learn - val
    # E1-A selection
    over_margin: float = 0.02
    min_val_acc: float = 0.50
    max_val_over: float = 0.02
    # E1-B filtering
    min_learn_support: int = 3
    min_val_support: int = 3
    filter_min_acc: float = 0.60
    filter_max_over: float = 0.02
    # E1-C confidence
    tau: float = 0.75
    support_saturation: int = 20


@dataclass
class Rule:
    rule_id: str
    source: str
    prefix_signature: tuple
    method: str                 # "M2" | "M3" | "KEEP"
    boundary_tokens: int        # 0 = keep original
    learn_support: int = 0
    val_support: int = 0
    val_correct: int = 0
    val_over: int = 0
    val_under: int = 0
    val_abstain: int = 0

    @property
    def val_acc(self) -> float:
        return self.val_correct / self.val_support if self.val_support else 0.0

    @property
    def val_over_rate(self) -> float:
        return self.val_over / self.val_support if self.val_support else 1.0

    @property
    def confidence(self) -> float:
        if self.boundary_tokens == 0 or self.val_support == 0:
            return 0.0
        accepted = self.val_correct / self.val_support
        sup = min(1.0, self.val_support / E1Config.support_saturation)
        penalty = 1.0 - self.val_over_rate * 4.0
        return round(max(0.0, accepted * sup * max(0.0, penalty)), 4)

    def predict(self, line: str) -> int:
        return token_boundary_to_char(line, self.boundary_tokens)

    def to_json(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "source": self.source,
            "prefix_signature": list(self.prefix_signature),
            "method": self.method,
            "body_boundary_tokens": self.boundary_tokens,
            "learn_support": self.learn_support,
            "val_support": self.val_support,
            "val_accuracy": round(self.val_acc, 4),
            "val_over_strip": round(self.val_over_rate, 4),
            "confidence": self.confidence,
            "version": "e1-v1.1",
        }


def _keep_of(rule: Rule) -> Rule:
    return Rule(rule_id=rule.rule_id + "-KEEP", source=rule.source,
                prefix_signature=rule.prefix_signature, method="KEEP",
                boundary_tokens=0, learn_support=rule.learn_support,
                val_support=rule.val_support, val_correct=rule.val_correct,
                val_over=rule.val_over, val_under=rule.val_under,
                val_abstain=rule.val_abstain)


# ---------------------------------------------------------------------------
# Candidate generation (per source)
# ---------------------------------------------------------------------------

def build_candidates(learn_items: list[dict], cfg: E1Config):
    """Per source, per signature family: M2-rule and M3-rule candidates.

    learn_items carry {"line", "source"}.  Returns
    {(source, sig): {"M2": Rule, "M3": Rule}}.
    """
    by_source: dict[str, list[dict]] = defaultdict(list)
    for it in learn_items:
        by_source[it["source"]].append(it)

    candidates: dict[tuple, dict[str, Rule]] = {}
    for source, items in by_source.items():
        m3 = M3Detector().fit([it["line"] for it in items])
        fams: dict[tuple, list[dict]] = defaultdict(list)
        for it in items:
            fams[signature(it["line"], M3_SIG_LEN)].append(it)
        for sig, fitems in fams.items():
            lines = [it["line"] for it in fitems]
            m2 = M2Detector().fit(lines)
            m3_bounds = Counter(m3.predict_token_boundary(l) for l in lines)
            m3_b = m3_bounds.most_common(1)[0][0]
            rid = f"{source}::{','.join(sig[:2])}…"
            candidates[(source, sig)] = {
                "M2": Rule(rid, source, sig, "M2", m2.boundary,
                           learn_support=len(fitems)),
                "M3": Rule(rid, source, sig, "M3", m3_b,
                           learn_support=len(fitems)),
            }
    return candidates


def evaluate_rule_on(rule: Rule, items: list[dict]) -> None:
    for it in items:
        pred = rule.predict(it["line"])
        gt = it["body_start"]
        rule.val_support += 1
        if pred == gt:
            rule.val_correct += 1
        elif pred > gt:
            rule.val_over += 1
        elif pred == 0:
            rule.val_abstain += 1
        else:
            rule.val_under += 1


# ---------------------------------------------------------------------------
# Selection variants
# ---------------------------------------------------------------------------

def select_e1a(candidates: dict, cfg: E1Config) -> dict[tuple, Rule]:
    chosen: dict[tuple, Rule] = {}
    for key, cand in candidates.items():
        m2r, m3r = cand["M2"], cand["M3"]
        if m2r.boundary_tokens == 0 and m3r.boundary_tokens == 0:
            chosen[key] = _keep_of(m2r)
            continue
        if abs(m2r.val_over_rate - m3r.val_over_rate) > cfg.over_margin:
            pick = m2r if m2r.val_over_rate < m3r.val_over_rate else m3r
        elif abs(m2r.val_acc - m3r.val_acc) > 0.02:
            pick = m2r if m2r.val_acc > m3r.val_acc else m3r
        else:
            pick = m2r  # simpler
        if pick.val_support == 0:
            chosen[key] = _keep_of(m2r)
        elif pick.val_over_rate > cfg.max_val_over or pick.val_acc < cfg.min_val_acc:
            chosen[key] = _keep_of(pick)
        else:
            chosen[key] = pick
    return chosen


def filter_e1b(chosen: dict[tuple, Rule], cfg: E1Config) -> dict[tuple, Rule]:
    """E1-B: support / accuracy / over-strip gates on the selected rules."""
    out: dict[tuple, Rule] = {}
    for key, rule in chosen.items():
        if rule.method == "KEEP":
            out[key] = rule
        elif (rule.learn_support < cfg.min_learn_support
                or rule.val_support < cfg.min_val_support
                or rule.val_acc < cfg.filter_min_acc
                or rule.val_over_rate > cfg.filter_max_over):
            out[key] = _keep_of(rule)
        else:
            out[key] = rule
    return out


def gate_e1c(filtered: dict[tuple, Rule], cfg: E1Config,
             tau: float | None = None) -> dict[tuple, Rule]:
    tau = cfg.tau if tau is None else tau
    return {
        key: (rule if rule.method == "KEEP" or rule.confidence >= tau
              else _keep_of(rule))
        for key, rule in filtered.items()
    }


# ---------------------------------------------------------------------------
# Rule-set predictors
# ---------------------------------------------------------------------------

class RuleSetDetector:
    """Deployed rules keyed by (source, signature); miss -> keep."""

    def __init__(self, rules: dict[tuple, Rule], name: str):
        self.rules = rules
        self.name = name

    def predict(self, line: str, source: str) -> int:
        rule = self.rules.get((source, signature(line, M3_SIG_LEN)))
        if rule is None:
            return 0
        return rule.predict(line)

    def stats(self) -> dict:
        by_method = Counter(r.method for r in self.rules.values())
        n = len(self.rules)
        return {
            "n_rules": n,
            "n_keep": by_method.get("KEEP", 0),
            "n_strip": n - by_method.get("KEEP", 0),
            "by_method": dict(by_method),
        }


class TransferDetector:
    """LODO predictor: no rules for the held-out source; match OTHER
    sources' rules by signature alone (highest confidence wins)."""

    def __init__(self, rules: dict[tuple, Rule]):
        self.by_sig: dict[tuple, Rule] = {}
        for (source, sig), rule in rules.items():
            if rule.boundary_tokens == 0:
                continue
            cur = self.by_sig.get(sig)
            if cur is None or rule.confidence > cur.confidence:
                self.by_sig[sig] = rule

    def predict(self, line: str, source: str) -> int:
        rule = self.by_sig.get(signature(line, M3_SIG_LEN))
        if rule is None:
            return 0
        return rule.predict(line)

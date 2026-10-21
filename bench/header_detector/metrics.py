"""Character-level evaluation metrics for header boundary detection.

Outcome categories per line (V1.1 unified standard, section 9.1 of the
experiment plan):

- correct      pred == gt (for header-less lines gt == 0, keeping is correct)
- over_strip   pred > gt  (message content deleted)
- under_strip  pred > 0 and pred < gt (stripped but residual header)
- abstain      pred == 0 and gt > 0  (original kept, header not removed)

Reported metrics:
- Boundary Accuracy          correct / total
- Over-strip Rate            over_strip / total
- Under-strip Rate           under_strip / total
- Abstain Rate               abstain / total
- Auto-strip Coverage        stripped (pred > 0) / total
- Accepted Boundary Accuracy correct & stripped / stripped
- Body Integrity (char)      preserved GT body chars / total GT body chars
- Body Integrity (line)      lines without any body-char loss / total
"""
from __future__ import annotations


def classify_outcome(pred: int, gt: int) -> str:
    if pred == gt:
        return "correct"
    if pred > gt:
        return "over_strip"
    if pred == 0:
        return "abstain"
    return "under_strip"


def evaluate(items: list[dict], preds: list[int]) -> dict:
    assert len(items) == len(preds)
    n = len(items)
    counts = {"correct": 0, "over_strip": 0, "under_strip": 0, "abstain": 0}
    stripped = correct_stripped = 0
    body_chars_total = 0
    body_chars_preserved = 0
    lines_body_intact = 0
    examples = {"over_strip": [], "under_strip": [], "abstain": []}

    for it, pred in zip(items, preds):
        gt = it["body_start"]
        line = it["line"]
        out = classify_outcome(pred, gt)
        counts[out] += 1
        gt_body_len = len(line) - gt
        body_chars_total += gt_body_len
        preserved = gt_body_len - max(0, pred - gt)
        if preserved < 0:
            preserved = 0
        body_chars_preserved += preserved
        if pred <= gt:
            lines_body_intact += 1
        if pred > 0:
            stripped += 1
            if out == "correct":
                correct_stripped += 1
        if out != "correct" and len(examples.get(out, [])) < 10:
            examples[out].append({
                "line": line[:200],
                "gt_body_start": gt,
                "pred_body_start": pred,
                "gt_header": line[:gt][:120],
                "pred_header": line[:pred][:120],
                "format": it.get("format", ""),
            })

    d = {
        "n": n,
        "counts": counts,
        "boundary_accuracy": round(counts["correct"] / n, 4) if n else 0.0,
        "over_strip_rate": round(counts["over_strip"] / n, 4) if n else 0.0,
        "under_strip_rate": round(counts["under_strip"] / n, 4) if n else 0.0,
        "abstain_rate": round(counts["abstain"] / n, 4) if n else 0.0,
        "auto_strip_coverage": round(stripped / n, 4) if n else 0.0,
        "accepted_boundary_accuracy": (
            round(correct_stripped / stripped, 4) if stripped else None
        ),
        "body_integrity_char": (
            round(body_chars_preserved / body_chars_total, 4) if body_chars_total else None
        ),
        "body_integrity_line": round(lines_body_intact / n, 4) if n else 0.0,
    }
    d["examples"] = examples
    return d


def macro_average(results: dict[str, dict]) -> dict:
    """Macro-average metric dict over datasets (excluding examples)."""
    keys = [
        "boundary_accuracy", "over_strip_rate", "under_strip_rate",
        "abstain_rate", "auto_strip_coverage", "accepted_boundary_accuracy",
        "body_integrity_char", "body_integrity_line",
    ]
    out = {}
    for k in keys:
        vals = [r[k] for r in results.values() if r.get(k) is not None]
        out[k] = round(sum(vals) / len(vals), 4) if vals else None
    return out

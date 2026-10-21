"""Ground-truth header boundaries for the 14 Loghub 2k datasets.

GT convention: a header regex consumes the entire header INCLUDING the
trailing separator (space / pipe / bracket) so that ``match.end()`` equals
the char offset where the message content starts ("body_start").

The definitions follow the standard logparser benchmark format
specifications for each dataset.  Lines that do not match are excluded
from evaluation and their count is reported (they are mostly malformed /
truncated lines); this is documented in the E0 report.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

LOGHUB_GT = {
    "Apache": r"^\[[^\]]+\] \[[^\]]+\] ",
    "BGL": r"^(\S+) \d+ \d{4}\.\d{2}\.\d{2} \S+ \d{4}-\d{2}-\d{2}[-.][\d.]+ \S+ \S+ \S+ \S+ ",
    "HDFS": r"^\d{6} \d{6} \d+ \S+ \S+: ",
    "HPC": r"^\d+ \S+ \S+ \S+ \d+ -?\d+ ",
    "Hadoop": r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} \S+ \[[^\]]*\] \S+: ",
    "HealthApp": r"^[^|]+\|[^|]+\|[^|]+\|",
    "Linux": r"^[A-Z][a-z]{2} +\d{1,2} \d{2}:\d{2}:\d{2} \S+ [^:]+?: ",
    "Mac": r"^[A-Z][a-z]{2} +\d{1,2} \d{2}:\d{2}:\d{2} \S+ [^:]+?: ",
    "OpenSSH": r"^[A-Z][a-z]{2} +\d{1,2} \d{2}:\d{2}:\d{2} \S+ [^\s:]+(\[\d+\])?: ",
    "OpenStack": r"^\S+ \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+ \d+ [A-Z]+ \S+ \[[^\]]*\] ",
    "Proxifier": r"^\[[^\]]+\] \S+(?: \*\d+)? - \S+:\d+ ",
    "Spark": r"^\d{2}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} \S+ \S+: ",
    "Thunderbird": r"^(\S+) \d+ \d{4}\.\d{2}\.\d{2} \S+ [A-Z][a-z]{2} +\d{1,2} \d{2}:\d{2}:\d{2} \S+ \S+: ",
    "Zookeeper": r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} - \S+ +\[.*?\] - ",
}


def load_loghub_gt(log_dir: Path, name: str):
    """Load one Loghub dataset; returns (items, n_excluded).

    items: list of {"line", "body_start"} dicts.
    """
    rx = re.compile(LOGHUB_GT[name])
    path = log_dir / f"{name}_2k.log"
    items, excluded = [], 0
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.rstrip("\n")
        if not line.strip():
            excluded += 1
            continue
        m = rx.match(line)
        if m:
            items.append({"line": line, "body_start": m.end()})
        else:
            excluded += 1
    return items, excluded


def load_stress_gt(stress_dir: Path, name: str):
    """Stress sets are JSONL with body_start recorded at generation time."""
    path = stress_dir / f"{name}.jsonl"
    items = []
    with path.open(encoding="utf-8") as f:
        for raw in f:
            rec = json.loads(raw)
            items.append({
                "line": rec["line"],
                "body_start": rec["body_start"],
                "format": rec.get("format", ""),
                "has_header": rec.get("has_header", True),
            })
    return items, 0


def validate_loghub_coverage(log_dir: Path) -> dict:
    """Report GT regex coverage for every Loghub dataset."""
    report = {}
    for name in LOGHUB_GT:
        items, excluded = load_loghub_gt(log_dir, name)
        total = len(items) + excluded
        report[name] = {
            "total": total,
            "matched": len(items),
            "excluded": excluded,
            "coverage": round(len(items) / total, 4) if total else 0.0,
        }
    return report

#!/usr/bin/env python3
"""E2: structure-aware tokenization and character-level boundary experiment.

Usage:
    python3 bench/header_span.py

Baseline: E0 commit 8fb2c985 (E2-A is the unmodified whitespace M3,
verified bit-identical to the E0 results after the tokenizer-injection
refactor).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.header_detector.e2_runner import print_report, run_e2  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / "bench" / "header_data"
OUT_DIR = ROOT / "docs" / "rep" / "header-detector-v11" / "e2-results"


def main():
    payload = run_e2(DATA_ROOT, OUT_DIR)
    print_report(payload)
    print(f"\nresults written to {OUT_DIR / 'e2_results.json'}")


if __name__ == "__main__":
    main()

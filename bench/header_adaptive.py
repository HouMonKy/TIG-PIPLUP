#!/usr/bin/env python3
"""E1: M2/M3 offline adaptive rule learning experiment.

Usage:
    python3 bench/header_adaptive.py

Baseline reference: E0 (branch exp/header-v11-repro, commit 8fb2c985).
Protocol and design decisions are documented in
docs/rep/header-detector-v11/e1-adaptive-report.md.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.header_detector.e1_runner import print_report, run_e1  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / "bench" / "header_data"
OUT_DIR = ROOT / "docs" / "rep" / "header-detector-v11" / "e1-results"


def main():
    payload = run_e1(DATA_ROOT, OUT_DIR)
    print_report(payload)
    print(f"\nresults written to {OUT_DIR / 'e1_results.json'}")


if __name__ == "__main__":
    main()

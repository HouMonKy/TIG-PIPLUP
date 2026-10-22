#!/usr/bin/env python3
"""E4: high-performance rule compilation experiment.

Usage:
    python3 bench/header_e4.py

Baseline: E3 commit 63eb7063 (E3-B operating point chosen on val only).
Compiles tau=0.4 and tau=0.2 rule sets; rules are NOT re-selected or
modified based on test results.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.header_detector.e4_runner import print_report, run_e4  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / "bench" / "header_data"
OUT_DIR = ROOT / "docs" / "rep" / "header-detector-v11" / "e4-results"


def main():
    payload = run_e4(DATA_ROOT, OUT_DIR)
    print_report(payload)
    print(f"\nresults written to {OUT_DIR / 'e4_results.json'}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""E3: confidence gating and body protection experiment.

Usage:
    python3 bench/header_gate.py

Baseline: E2 commit 811914c6 (E3-A reproduces E2-D exactly).
Operating points are selected on the validation split only.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.header_detector.e3_runner import print_report, run_e3  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / "bench" / "header_data"
OUT_DIR = ROOT / "docs" / "rep" / "header-detector-v11" / "e3-results"


def main():
    payload = run_e3(DATA_ROOT, OUT_DIR)
    print_report(payload)
    print(f"\nresults written to {OUT_DIR / 'e3_results.json'}")


if __name__ == "__main__":
    main()

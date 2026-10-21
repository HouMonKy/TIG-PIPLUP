#!/usr/bin/env python3
"""E0: rebuilt V1 baseline ablation (M1-M4) over the 22 rebuilt datasets.

Usage:
    python3 bench/header_ablation.py                # run evaluation
    python3 bench/header_ablation.py --validate-gt  # GT regex coverage only

Historical note: the original bench/header_ablation.py (commit ac30623d,
branch `final`) is missing from the repository.  This script re-establishes
the V1 baseline; results are NOT numerically comparable with the historical
report -- see docs/rep/header-detector-v11/e0-baseline-report.md.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.header_detector.gt import validate_loghub_coverage  # noqa: E402
from bench.header_detector.runner import print_table, run_all  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / "bench" / "header_data"
OUT_DIR = ROOT / "docs" / "rep" / "header-detector-v11" / "e0-results"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--validate-gt", action="store_true")
    args = ap.parse_args()

    if args.validate_gt:
        cov = validate_loghub_coverage(DATA_ROOT / "loghub")
        print(json.dumps(cov, indent=1))
        return

    payload = run_all(DATA_ROOT, OUT_DIR)
    print_table(payload)
    print(f"\nresults written to {OUT_DIR / 'e0_results.json'}")


if __name__ == "__main__":
    main()

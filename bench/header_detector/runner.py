"""Evaluation harness: runs M1-M4 over the 22 rebuilt datasets.

Protocol (fixed, seed 42):
- per dataset, indices are shuffled with random.Random(42);
  train = first 500 lines (Loghub) / 300 lines (stress), test = the rest;
- detectors are fitted on train lines ONLY (no test leakage: no positional
  statistics, grouping or thresholds ever see test lines);
- evaluation is character-level against GT body_start.

Summary numbers are macro-averages within each suite (loghub / stress) and
over all 22 datasets.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from .gt import LOGHUB_GT, load_loghub_gt, load_stress_gt
from .methods import ALL_METHODS
from .metrics import evaluate, macro_average

SEED = 42
TRAIN_N = {"loghub": 500, "stress": 300}


def load_all_datasets(data_root: Path) -> dict[str, list[dict]]:
    datasets = {}
    loghub_dir = data_root / "loghub"
    for name in LOGHUB_GT:
        items, excluded = load_loghub_gt(loghub_dir, name)
        datasets[name] = items
    stress_dir = data_root / "stress"
    for f in sorted(stress_dir.glob("*.jsonl")):
        items, _ = load_stress_gt(stress_dir, f.stem)
        datasets[f.name] = items
    return datasets


def split_dataset(items: list[dict], train_n: int, seed: int = SEED):
    idx = list(range(len(items)))
    rng = random.Random(seed)
    rng.shuffle(idx)
    train_idx = set(idx[:train_n])
    train = [items[i] for i in sorted(train_idx)]
    test = [items[i] for i in idx[train_n:]]
    return train, test


def run_all(data_root: Path, out_dir: Path) -> dict:
    datasets = load_all_datasets(data_root)
    results = {}   # dataset -> method -> metrics
    meta = {}
    for name, items in datasets.items():
        suite = "stress" if name.startswith("stress-") else "loghub"
        train, test = split_dataset(items, TRAIN_N[suite])
        meta[name] = {
            "suite": suite, "n_total": len(items),
            "n_train": len(train), "n_test": len(test),
        }
        results[name] = {}
        for method_cls in ALL_METHODS:
            det = method_cls().fit([it["line"] for it in train])
            preds = [det.predict(it["line"]) for it in test]
            m = evaluate(test, preds)
            if hasattr(det, "n_splits"):
                m["_diag"] = {
                    "n_leaves": det.n_leaves,
                    "n_splits": det.n_splits,
                }
            results[name][det.name] = m

    # summaries
    summary = {}
    for suite in ("loghub", "stress"):
        ds_names = [n for n in results if meta[n]["suite"] == suite]
        summary[suite] = {
            m: macro_average({n: results[n][m] for n in ds_names})
            for m in ("M1", "M2", "M3", "M4")
        }
    summary["all22"] = {
        m: macro_average({n: results[n][m] for n in results}) for m in ("M1", "M2", "M3", "M4")
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {"meta": meta, "results": results, "summary": summary}
    (out_dir / "e0_results.json").write_text(
        json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    return payload


def print_table(payload: dict):
    meta, results, summary = payload["meta"], payload["results"], payload["summary"]
    for suite in ("loghub", "stress", "all22"):
        print(f"\n=== {suite} (macro avg) ===")
        hdr = f"{'method':6s} {'bndry_acc':>9s} {'ovr_strp':>8s} {'undr_strp':>9s} {'abstain':>7s} {'coverage':>8s} {'acc_bndry':>9s} {'integ_chr':>9s} {'integ_ln':>8s}"
        print(hdr)
        for m in ("M1", "M2", "M3", "M4"):
            s = summary[suite][m]
            print(f"{m:6s} {s['boundary_accuracy']:>9.4f} {s['over_strip_rate']:>8.4f} "
                  f"{s['under_strip_rate']:>9.4f} {s['abstain_rate']:>7.4f} "
                  f"{s['auto_strip_coverage']:>8.4f} "
                  f"{(s['accepted_boundary_accuracy'] or 0):>9.4f} "
                  f"{(s['body_integrity_char'] or 0):>9.4f} {s['body_integrity_line']:>8.4f}")
    print("\n=== per-dataset boundary accuracy ===")
    names = list(results)
    print(f"{'dataset':22s}" + "".join(f"{m:>8s}" for m in ("M1", "M2", "M3", "M4")))
    for n in names:
        row = "".join(f"{results[n][m]['boundary_accuracy']:>8.3f}" for m in ("M1", "M2", "M3", "M4"))
        print(f"{n:22s}{row}")

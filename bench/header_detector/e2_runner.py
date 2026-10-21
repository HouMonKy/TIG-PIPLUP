"""E2 experiment runner: span-aware tokenization quality + cost evaluation."""
from __future__ import annotations

import json
import random
import resource
import time
from collections import defaultdict
from pathlib import Path

from .gt import LOGHUB_GT, load_loghub_gt, load_stress_gt
from .methods import M2Detector, M3Detector
from .metrics import evaluate, macro_average
from .span_methods import SpanM3Detector, SpanM3Prefix, SpanM3Refined

LEARN_FRAC = 0.30
VAL_FRAC = 0.20
SEED = 42
N_REPEAT = 5


def load_datasets(data_root: Path) -> dict[str, list[dict]]:
    datasets = {}
    for name in LOGHUB_GT:
        items, _ = load_loghub_gt(data_root / "loghub", name)
        datasets[name] = items
    for f in sorted((data_root / "stress").glob("*.jsonl")):
        items, _ = load_stress_gt(data_root / "stress", f.stem)
        datasets[f.name] = items
    ch = data_root / "challenge" / "challenge-brackets.jsonl"
    if ch.exists():
        items = []
        for raw in ch.read_text(encoding="utf-8").splitlines():
            rec = json.loads(raw)
            items.append({"line": rec["line"], "body_start": rec["body_start"],
                          "format": rec["format"], "has_header": rec["has_header"]})
        datasets["challenge-brackets"] = items
    return datasets


def split_dataset(items: list[dict], seed: int = SEED):
    idx = list(range(len(items)))
    random.Random(seed).shuffle(idx)
    n_learn = int(len(items) * LEARN_FRAC)
    n_val = int(len(items) * VAL_FRAC)
    learn = [items[i] for i in idx[:n_learn]]
    val = [items[i] for i in idx[n_learn:n_learn + n_val]]
    test = [items[i] for i in idx[n_learn + n_val:]]
    return learn, val, test


def suite_of(name: str) -> str:
    if name.startswith("stress-"):
        return "stress"
    if name.startswith("challenge"):
        return "challenge"
    return "loghub"


def run_e2(data_root: Path, out_dir: Path) -> dict:
    datasets = load_datasets(data_root)
    methods = {
        "E2-A": lambda: M3Detector(),          # original whitespace M3
        "E2-B": lambda: SpanM3Detector(),
        "E2-C": lambda: SpanM3Refined(),
        "E2-D": lambda: SpanM3Prefix(),
    }

    rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    results: dict[str, dict[str, dict]] = {}
    meta = {}
    fitted = {}          # method -> list[(dataset, det, test_items)]
    fit_times = defaultdict(float)
    for name, items in datasets.items():
        learn, val, test = split_dataset(items)
        meta[name] = {"suite": suite_of(name), "n_learn": len(learn),
                      "n_val": len(val), "n_test": len(test)}
        results[name] = {}
        fitted[name] = {}
        for mname, factory in methods.items():
            t0 = time.perf_counter()
            det = factory().fit([it["line"] for it in learn])
            fit_times[mname] += time.perf_counter() - t0
            fitted[name][mname] = det
            preds = [det.predict(it["line"]) for it in test]
            results[name][mname] = evaluate(test, preds)

    # ---- summaries -------------------------------------------------------
    summary = {}
    for suite in ("loghub", "stress", "challenge"):
        names = [n for n in results if meta[n]["suite"] == suite]
        if not names:
            continue
        summary[suite] = {
            m: macro_average({n: results[n][m] for n in names})
            for m in methods
        }
    summary["all"] = {
        m: macro_average({n: results[n][m] for n in results})
        for m in methods
    }

    # ---- pooled per-category results on the challenge set ----------------
    challenge_cats = {}
    ch_items = datasets.get("challenge-brackets", [])
    if ch_items:
        _, _, ch_test = split_dataset(ch_items)
        by_cat = defaultdict(list)
        for it in ch_test:
            by_cat[it["format"]].append(it)
        for mname in methods:
            # refit the same way (learn split), evaluate per category
            learn_ch, _, _ = split_dataset(ch_items)
            det = methods[mname]().fit([it["line"] for it in learn_ch])
            challenge_cats[mname] = {}
            for cat, its in sorted(by_cat.items()):
                preds = [det.predict(it["line"]) for it in its]
                m = evaluate(its, preds)
                challenge_cats[mname][cat] = {
                    k: m[k] for k in (
                        "n", "boundary_accuracy", "over_strip_rate",
                        "under_strip_rate", "abstain_rate",
                        "auto_strip_coverage", "accepted_boundary_accuracy")}

    # ---- performance -----------------------------------------------------
    all_test = [(name, it) for name in datasets
                for it in split_dataset(datasets[name])[2]]
    perf = {}
    for mname, factory in methods.items():
        # representative fit: pooled learn lines of the three suites
        pooled_learn = []
        for name in datasets:
            learn, _, _ = split_dataset(datasets[name])
            pooled_learn.extend(it["line"] for it in learn)
        det = factory().fit(pooled_learn)
        lines = [it["line"] for _, it in all_test]
        run_stats = []
        for _ in range(N_REPEAT):
            t0 = time.perf_counter()
            durations = []
            for ln in lines:
                t1 = time.perf_counter_ns()
                det.predict(ln)
                durations.append(time.perf_counter_ns() - t1)
            total = time.perf_counter() - t0
            durations.sort()
            run_stats.append({
                "total_s": total,
                "per_line_ns_avg": sum(durations) / len(durations),
                "per_line_ns_p50": durations[len(durations) // 2],
                "per_line_ns_p95": durations[int(len(durations) * 0.95)],
                "lines_per_s": len(lines) / total,
            })
        run_stats.sort(key=lambda r: r["total_s"])
        med = run_stats[N_REPEAT // 2]
        perf[mname] = {
            "n_lines": len(lines),
            "n_repeat": N_REPEAT,
            "fit_total_s_by_dataset_sum": round(fit_times[mname], 4),
            "per_line_ns_avg": round(med["per_line_ns_avg"], 1),
            "per_line_ns_p50": round(med["per_line_ns_p50"], 1),
            "per_line_ns_p95": round(med["per_line_ns_p95"], 1),
            "lines_per_s": round(med["lines_per_s"], 1),
            "min_lines_per_s": round(min(r["lines_per_s"] for r in run_stats), 1),
            "max_lines_per_s": round(max(r["lines_per_s"] for r in run_stats), 1),
        }
    rss1 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    perf["_rss"] = {
        "ru_maxrss_start_kb": rss0,
        "ru_maxrss_peak_kb": rss1,
        "note": "peak RSS of the whole E2 run (all methods, datasets loaded)",
    }

    payload = {
        "config": {
            "seed": SEED, "learn_frac": LEARN_FRAC, "val_frac": VAL_FRAC,
            "methods": {
                "E2-A": "original whitespace M3 (E0, unchanged)",
                "E2-B": "M3 on span tokens (no guard/refinement)",
                "E2-C": "E2-B + content-bracket guard + char refinement",
                "E2-D": "E2-C with prefix-limited tokenizer (17 spans/256 chars)",
            },
            "content_guard": {"word_frac": 0.6, "distinct_ratio": 0.5},
            "n_repeat_perf": N_REPEAT,
        },
        "meta": meta,
        "results": results,
        "summary": summary,
        "challenge_by_category": challenge_cats,
        "performance": perf,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "e2_results.json").write_text(
        json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    return payload


def print_report(payload: dict) -> None:
    keys = ["boundary_accuracy", "over_strip_rate", "under_strip_rate",
            "abstain_rate", "auto_strip_coverage",
            "accepted_boundary_accuracy", "body_integrity_char"]
    for suite in ("loghub", "stress", "challenge", "all"):
        if suite not in payload["summary"]:
            continue
        print(f"\n=== {suite} (macro avg) ===")
        print(f"{'method':8s}" + "".join(f"{k[:11]:>12s}" for k in keys))
        for m, s in payload["summary"][suite].items():
            row = "".join(
                f"{(s[k] if s[k] is not None else 0):>12.4f}" for k in keys)
            print(f"{m:8s}{row}")
    if payload["challenge_by_category"]:
        print("\n=== challenge set by category (boundary accuracy) ===")
        cats = sorted(next(iter(payload["challenge_by_category"].values())).keys())
        print(f"{'method':8s}" + "".join(f"{c:>8s}" for c in cats))
        for m, bycat in payload["challenge_by_category"].items():
            print(f"{m:8s}" + "".join(f"{bycat[c]['boundary_accuracy']:>8.3f}" for c in cats))
        print("\n=== challenge set by category (over-strip) ===")
        print(f"{'method':8s}" + "".join(f"{c:>8s}" for c in cats))
        for m, bycat in payload["challenge_by_category"].items():
            print(f"{m:8s}" + "".join(f"{bycat[c]['over_strip_rate']:>8.3f}" for c in cats))
    print("\n=== per-dataset boundary accuracy ===")
    names = list(payload["results"])
    vns = list(next(iter(payload["results"].values())).keys())
    print(f"{'dataset':28s}" + "".join(f"{v:>8s}" for v in vns))
    for n in names:
        row = "".join(
            f"{payload['results'][n][v]['boundary_accuracy']:>8.3f}" for v in vns)
        print(f"{n:28s}{row}")
    print("\n=== performance (predict, pooled test lines, median of 5 runs) ===")
    print(f"{'method':8s} {'avg us':>9s} {'p50 us':>9s} {'p95 us':>9s} {'lines/s':>10s} {'fit s':>8s}")
    for m, p in payload["performance"].items():
        if m == "_rss":
            continue
        print(f"{m:8s} {p['per_line_ns_avg']/1000:>9.2f} {p['per_line_ns_p50']/1000:>9.2f} "
              f"{p['per_line_ns_p95']/1000:>9.2f} {p['lines_per_s']:>10.1f} "
              f"{p['fit_total_s_by_dataset_sum']:>8.2f}")
    print("rss:", payload["performance"]["_rss"])

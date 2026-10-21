"""E1 experiment runner: per-source rule learning + variant comparison."""
from __future__ import annotations

import json
import random
from collections import defaultdict
from pathlib import Path

from .adaptive import (
    E1Config, RuleSetDetector, TransferDetector, build_candidates,
    evaluate_rule_on, filter_e1b, gate_e1c, select_e1a,
)
from .gt import LOGHUB_GT, load_loghub_gt, load_stress_gt
from .methods import M2Detector, M3Detector, M3_SIG_LEN
from .metrics import evaluate, macro_average
from .tokentypes import signature

TAU_SWEEP = [0.0, 0.3, 0.5, 0.6, 0.7, 0.75, 0.8, 0.9, 0.95]


def load_datasets(data_root: Path) -> dict[str, list[dict]]:
    datasets = {}
    for name in LOGHUB_GT:
        items, _ = load_loghub_gt(data_root / "loghub", name)
        datasets[name] = items
    for f in sorted((data_root / "stress").glob("*.jsonl")):
        items, _ = load_stress_gt(data_root / "stress", f.stem)
        datasets[f.name] = items
    return datasets


def three_way_split(items: list[dict], cfg: E1Config, source: str):
    idx = list(range(len(items)))
    random.Random(cfg.seed).shuffle(idx)
    n_learn = int(len(items) * cfg.learn_frac)
    n_val = int(len(items) * cfg.val_frac)
    out = {"learn": [], "val": [], "test": []}
    for k, i in enumerate(idx):
        it = dict(items[i])
        it["source"] = source
        if k < n_learn:
            out["learn"].append(it)
        elif k < n_learn + n_val:
            out["val"].append(it)
        else:
            out["test"].append(it)
    return out


def run_e1(data_root: Path, out_dir: Path) -> dict:
    cfg = E1Config()
    datasets = load_datasets(data_root)
    splits = {name: three_way_split(items, cfg, name) for name, items in datasets.items()}

    learn_pool = [it for s in splits.values() for it in s["learn"]]
    val_pool = [it for s in splits.values() for it in s["val"]]

    # ---- candidate generation + validation -------------------------------
    candidates = build_candidates(learn_pool, cfg)
    val_by_key: dict[tuple, list[dict]] = defaultdict(list)
    for it in val_pool:
        val_by_key[(it["source"], signature(it["line"], M3_SIG_LEN))].append(it)
    for key, cand in candidates.items():
        vitems = val_by_key.get(key, [])
        for rule in cand.values():
            evaluate_rule_on(rule, vitems)

    # ---- variants --------------------------------------------------------
    m2_rules = {k: c["M2"] for k, c in candidates.items()}
    m3_rules = {k: c["M3"] for k, c in candidates.items()}
    e1a = select_e1a(candidates, cfg)
    e1b = filter_e1b(e1a, cfg)
    e1c_default = gate_e1c(e1b, cfg, cfg.tau)

    variants = {
        "M2-RULE": RuleSetDetector(m2_rules, "M2-RULE"),
        "M3-RULE": RuleSetDetector(m3_rules, "M3-RULE"),
        "E1-A": RuleSetDetector(e1a, "E1-A"),
        "E1-B": RuleSetDetector(e1b, "E1-B"),
        "E1-C": RuleSetDetector(e1c_default, "E1-C"),
    }

    # reference detectors: per-source M2 / M3 fitted on learn+val
    learnval_by_source: dict[str, list[str]] = defaultdict(list)
    for it in learn_pool + val_pool:
        learnval_by_source[it["source"]].append(it["line"])
    m2_refs = {s: M2Detector().fit(ls) for s, ls in learnval_by_source.items()}
    m3_refs = {s: M3Detector().fit(ls) for s, ls in learnval_by_source.items()}

    # ---- evaluation on test ----------------------------------------------
    results: dict[str, dict[str, dict]] = {}
    meta = {}
    for name, sp in splits.items():
        suite = "stress" if name.startswith("stress-") else "loghub"
        test = sp["test"]
        meta[name] = {"suite": suite, "n_learn": len(sp["learn"]),
                      "n_val": len(sp["val"]), "n_test": len(test)}
        results[name] = {}
        for vname, det in variants.items():
            preds = [det.predict(it["line"], it["source"]) for it in test]
            results[name][vname] = evaluate(test, preds)
        results[name]["M2-raw"] = evaluate(
            test, [m2_refs[name].predict(it["line"]) for it in test])
        results[name]["M3-raw"] = evaluate(
            test, [m3_refs[name].predict(it["line"]) for it in test])

    variant_names = list(variants) + ["M2-raw", "M3-raw"]
    summary = {}
    for suite in ("loghub", "stress"):
        names = [n for n in results if meta[n]["suite"] == suite]
        summary[suite] = {
            v: macro_average({n: results[n][v] for n in names})
            for v in variant_names
        }
    summary["all22"] = {
        v: macro_average({n: results[n][v] for n in results})
        for v in variant_names
    }

    # ---- tau sweep --------------------------------------------------------
    tau_curve = []
    for tau in TAU_SWEEP:
        det = RuleSetDetector(gate_e1c(e1b, cfg, tau), f"E1-C@{tau}")
        all_test = [it for sp in splits.values() for it in sp["test"]]
        preds = [det.predict(it["line"], it["source"]) for it in all_test]
        m = evaluate(all_test, preds)
        tau_curve.append({
            "tau": tau,
            "boundary_accuracy": m["boundary_accuracy"],
            "over_strip_rate": m["over_strip_rate"],
            "auto_strip_coverage": m["auto_strip_coverage"],
            "accepted_boundary_accuracy": m["accepted_boundary_accuracy"],
        })

    # ---- LODO transfer ----------------------------------------------------
    lodo = {}
    for holdout in datasets:
        lp = [it for n, sp in splits.items() if n != holdout for it in sp["learn"]]
        vp = [it for n, sp in splits.items() if n != holdout for it in sp["val"]]
        cands = build_candidates(lp, cfg)
        v_by_key = defaultdict(list)
        for it in vp:
            v_by_key[(it["source"], signature(it["line"], M3_SIG_LEN))].append(it)
        for key, cand in cands.items():
            for rule in cand.values():
                evaluate_rule_on(rule, v_by_key.get(key, []))
        rules = filter_e1b(select_e1a(cands, cfg), cfg)
        det = TransferDetector(rules)
        test = splits[holdout]["test"]
        m = evaluate(test, [det.predict(it["line"], it["source"]) for it in test])
        lodo[holdout] = {
            "boundary_accuracy": m["boundary_accuracy"],
            "over_strip_rate": m["over_strip_rate"],
            "auto_strip_coverage": m["auto_strip_coverage"],
            "accepted_boundary_accuracy": m["accepted_boundary_accuracy"],
        }

    # ---- analysis ----------------------------------------------------------
    method_choice = []
    for key, cand in sorted(candidates.items(),
                            key=lambda kv: -kv[1]["M2"].learn_support):
        source, sig = key
        chosen = e1a.get(key)
        if chosen is None:
            continue
        method_choice.append({
            "source": source,
            "signature": list(sig),
            "learn_support": cand["M2"].learn_support,
            "m2_boundary": cand["M2"].boundary_tokens,
            "m3_boundary": cand["M3"].boundary_tokens,
            "m2_val": [cand["M2"].val_acc, cand["M2"].val_over_rate,
                       cand["M2"].val_support],
            "m3_val": [cand["M3"].val_acc, cand["M3"].val_over_rate,
                       cand["M3"].val_support],
            "chosen": chosen.method,
            "chosen_boundary": chosen.boundary_tokens,
        })

    payload = {
        "config": {
            "seed": cfg.seed, "learn_frac": cfg.learn_frac,
            "val_frac": cfg.val_frac, "over_margin": cfg.over_margin,
            "min_val_acc": cfg.min_val_acc, "max_val_over": cfg.max_val_over,
            "filter_min_acc": cfg.filter_min_acc,
            "filter_max_over": cfg.filter_max_over, "tau": cfg.tau,
            "note": "rules keyed by (source, signature); selection per "
                    "structural family on val; test never used before eval",
        },
        "meta": meta,
        "results": results,
        "summary": summary,
        "tau_curve": tau_curve,
        "lodo": lodo,
        "method_choice": method_choice,
        "rule_set_stats": {v: variants[v].stats() for v in variants},
        "rules_e1c": [r.to_json() for r in
                      sorted(e1c_default.values(),
                             key=lambda r: -r.learn_support)],
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "e1_results.json").write_text(
        json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    return payload


def print_report(payload: dict) -> None:
    keys = ["boundary_accuracy", "over_strip_rate", "abstain_rate",
            "auto_strip_coverage", "accepted_boundary_accuracy",
            "body_integrity_char"]
    for suite in ("loghub", "stress", "all22"):
        print(f"\n=== {suite} (macro avg) ===")
        print(f"{'variant':10s}" + "".join(f"{k[:11]:>12s}" for k in keys))
        for v, s in payload["summary"][suite].items():
            row = "".join(
                f"{(s[k] if s[k] is not None else 0):>12.4f}" for k in keys)
            print(f"{v:10s}{row}")
    print("\n=== E1-C tau sweep (pooled test) ===")
    print(f"{'tau':>5s} {'acc':>8s} {'over':>8s} {'coverage':>9s} {'accB':>8s}")
    for t in payload["tau_curve"]:
        print(f"{t['tau']:>5.2f} {t['boundary_accuracy']:>8.4f} "
              f"{t['over_strip_rate']:>8.4f} {t['auto_strip_coverage']:>9.4f} "
              f"{(t['accepted_boundary_accuracy'] or 0):>8.4f}")
    print("\n=== LODO transfer (held-out source, signature-only matching) ===")
    print(f"{'dataset':28s} {'acc':>7s} {'over':>7s} {'cov':>7s} {'accB':>7s}")
    for ds, m in payload["lodo"].items():
        ab = m["accepted_boundary_accuracy"]
        print(f"{ds:28s} {m['boundary_accuracy']:>7.3f} {m['over_strip_rate']:>7.3f} "
              f"{m['auto_strip_coverage']:>7.3f} {(ab if ab is not None else 0):>7.3f}")
    print("\n=== rule set stats ===")
    for v, st in payload["rule_set_stats"].items():
        print(f"{v:8s} {st}")
    print("\n=== per-dataset boundary accuracy ===")
    names = list(payload["results"])
    vns = list(next(iter(payload["results"].values())).keys())
    print(f"{'dataset':28s}" + "".join(f"{v[:9]:>10s}" for v in vns))
    for n in names:
        row = "".join(
            f"{payload['results'][n][v]['boundary_accuracy']:>10.3f}" for v in vns)
        print(f"{n:28s}{row}")

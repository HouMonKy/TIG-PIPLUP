"""E3 experiment runner: gate ablation, threshold sweeps, transitions, LODO."""
from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from pathlib import Path

from .e2_runner import load_datasets, split_dataset, suite_of
from .gating import (
    BodyRiskModel, boundary_features, build_rules, gate_b,
)
from .metrics import evaluate, macro_average
from .span_methods import SpanM3Prefix

TAU_B_GRID = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
TAU_C_GRID = [0.05, 0.08, 0.10, 0.15, 0.20, 0.30, 0.50, 1.01]  # 1.01 = off
VAL_OVER_TARGET = 0.005  # <=0.5% exploration target (validation)


def _suite_key(name: str) -> str:
    return suite_of(name)


def run_e3(data_root: Path, out_dir: Path) -> dict:
    datasets = load_datasets(data_root)
    splits = {n: split_dataset(items) for n, items in datasets.items()}

    # ---- fit E2-D per dataset on learn (identical to E2) ----------------
    dets = {}
    for name, (learn, _val, _test) in splits.items():
        dets[name] = SpanM3Prefix().fit([it["line"] for it in learn])

    # ---- E3-A baseline predictions on test -------------------------------
    preds_a = {
        name: [dets[name].predict(it["line"]) for it in splits[name][2]]
        for name in datasets
    }

    # ---- offline gate statistics on VAL -----------------------------------
    rules_by_ds = {}
    for name in datasets:
        rules_by_ds[name] = build_rules(name, dets[name], splits[name][1])

    risk_records = []
    for name in datasets:
        det = dets[name]
        for it in splits[name][1]:
            pred = det.predict(it["line"])
            if pred <= 0:
                continue
            feats = boundary_features(det, it["line"])
            if feats is None:
                continue
            risk_records.append({
                "combo": feats["combo"],
                "is_over": pred > it["body_start"],
                "is_correct": pred == it["body_start"],
            })
    risk_model = BodyRiskModel.fit(risk_records)

    # ---- gated predictions ------------------------------------------------
    def apply_gate(name_items_preds, tau_b, tau_c):
        """name_items_preds: list of (name, item, pred). Returns final preds."""
        from .tokentypes import signature_from_tokens
        from .methods import M3_SIG_LEN
        out = []
        for name, it, pred in name_items_preds:
            if pred <= 0:
                out.append(0)
                continue
            det = dets[name]
            rules = rules_by_ds[name]
            feats = boundary_features(det, it["line"])
            combo = feats["combo"] if feats else (False, False, False, False)
            sig = signature_from_tokens(det._tok(it["line"]), M3_SIG_LEN)
            rule = rules.get((sig, combo))
            if not gate_b(rule, tau_b) or risk_model.keep(combo, tau_c):
                out.append(0)
            else:
                out.append(pred)
        return out

    def pooled_eval(items, preds) -> dict:
        return evaluate(items, preds)

    # val pool
    val_pool = []
    for name in datasets:
        det = dets[name]
        for it in splits[name][1]:
            val_pool.append((name, it, det.predict(it["line"])))

    def val_metrics(tau_b, tau_c):
        preds = apply_gate(val_pool, tau_b, tau_c)
        items = [it for _n, it, _p in val_pool]
        return pooled_eval(items, preds)

    # sweep on val: B alone (tau_c=off), C alone (tau_b=0), D grid
    val_curve_b = []
    for tb in TAU_B_GRID:
        m = val_metrics(tb, 1.01)
        val_curve_b.append({"tau_b": tb, **_pick(m)})
    val_curve_c = []
    for tc in TAU_C_GRID:
        m = val_metrics(0.0, tc)
        val_curve_c.append({"tau_c": tc, **_pick(m)})
    # D: for each tau_b, best tau_c; choose jointly
    best = None
    for tb in TAU_B_GRID:
        for tc in TAU_C_GRID:
            m = val_metrics(tb, tc)
            if m["over_strip_rate"] <= VAL_OVER_TARGET:
                cov = m["auto_strip_coverage"]
                if best is None or cov > best[0]:
                    best = (cov, tb, tc)
    if best is None:
        # no operating point meets the target: pick min over-strip
        allm = [(val_metrics(tb, tc), tb, tc) for tb in TAU_B_GRID for tc in TAU_C_GRID]
        m, tb, tc = min(allm, key=lambda x: (x[0]["over_strip_rate"], -x[0]["auto_strip_coverage"]))
        best = (m["auto_strip_coverage"], tb, tc)
    _, tau_b_star, tau_c_star = best

    # B / C individual operating points (each alone meets target, else min over)
    def _pick_point(curve, key):
        ok = [c for c in curve if c["over_strip_rate"] <= VAL_OVER_TARGET]
        if ok:
            return max(ok, key=lambda c: c["auto_strip_coverage"])[key]
        return min(curve, key=lambda c: (c["over_strip_rate"], -c["auto_strip_coverage"]))[key]

    tau_b_only = _pick_point(val_curve_b, "tau_b")
    tau_c_only = _pick_point(val_curve_c, "tau_c")

    # ---- TEST evaluation ---------------------------------------------------
    variants = {
        "E3-A": (None, None),
        "E3-B": (tau_b_only, 1.01),
        "E3-C": (0.0, tau_c_only),
        "E3-D": (tau_b_star, tau_c_star),
    }
    results = {}
    final_preds = {}
    for name in datasets:
        test = splits[name][2]
        pool = [(name, it, preds_a[name][k]) for k, it in enumerate(test)]
        results[name] = {}
        final_preds[name] = {}
        for vname, (tb, tc) in variants.items():
            if vname == "E3-A":
                preds = preds_a[name]
            else:
                preds = apply_gate(pool, tb, tc)
            final_preds[name][vname] = preds
            results[name][vname] = evaluate(test, preds)

    variant_names = list(variants)
    summary = {}
    for suite in ("loghub", "stress", "challenge"):
        names = [n for n in results if _suite_key(n) == suite]
        summary[suite] = {
            v: macro_average({n: results[n][v] for n in names})
            for v in variant_names
        }
    summary["all"] = {
        v: macro_average({n: results[n][v] for n in results})
        for v in variant_names
    }
    # pooled micro (for intercept rates) + content-level over
    pooled = {}
    all_test = [it for n in datasets for it in splits[n][2]]
    for vname in variant_names:
        preds = [p for n in datasets for p in final_preds[n][vname]]
        m = evaluate(all_test, preds)
        # content over: over-strips that deleted NON-WHITESPACE body chars
        n_content = 0
        n_over = 0
        for it, pv in zip(all_test, preds):
            if pv > it["body_start"]:
                n_over += 1
                if it["line"][it["body_start"]:pv].strip():
                    n_content += 1
        m["content_over_rate"] = round(n_content / len(all_test), 4) if all_test else 0.0
        m["whitespace_only_over"] = n_over - n_content
        pooled[vname] = m

    # ---- transitions at E3-D (and B, C) operating points -------------------
    transitions = {}
    for vname in ("E3-B", "E3-C", "E3-D"):
        t = Counter()
        for name in datasets:
            test = splits[name][2]
            for k, it in enumerate(test):
                pa = preds_a[name][k]
                pv = final_preds[name][vname][k]
                gt = it["body_start"]
                if pa <= 0:
                    continue  # detector abstain: gate has no say
                was = ("correct" if pa == gt else ("over" if pa > gt else "under"))
                now_keep = pv == 0
                t[(was, "keep" if now_keep else "strip")] += 1
        total_over = sum(c for (w, _), c in t.items() if w == "over")
        total_correct = sum(c for (w, _), c in t.items() if w == "correct")
        kept_over = t[("over", "keep")]
        kept_correct = t[("correct", "keep")]
        transitions[vname] = {
            "matrix": {f"{w}->{n}": c for (w, n), c in sorted(t.items())},
            "intercept_over_rate": round(kept_over / total_over, 4) if total_over else None,
            "correct_reject_loss_rate": round(kept_correct / total_correct, 4) if total_correct else None,
            "total_over_strips": total_over,
            "total_correct_strips": total_correct,
        }

    # ---- test curves (transparency; operating points were chosen on val) ---
    test_curve_b, test_curve_c, test_curve_d = [], [], []
    for tb in TAU_B_GRID:
        m = _pooled_at(tb, 1.01, datasets, splits, preds_a, dets, rules_by_ds, risk_model)
        test_curve_b.append({"tau_b": tb, **_pick(m)})
    for tc in TAU_C_GRID:
        m = _pooled_at(0.0, tc, datasets, splits, preds_a, dets, rules_by_ds, risk_model)
        test_curve_c.append({"tau_c": tc, **_pick(m)})
    for tc in TAU_C_GRID:
        m = _pooled_at(tau_b_star, tc, datasets, splits, preds_a, dets, rules_by_ds, risk_model)
        test_curve_d.append({"tau_c": tc, **_pick(m)})

    # ---- LODO transfer (independent experiment, unknown source) ------------
    lodo = {}
    for holdout in datasets:
        others_learn = [it for n in datasets if n != holdout for it in splits[n][0]]
        others_val = [(n, it) for n in datasets if n != holdout for it in splits[n][1]]
        det = SpanM3Prefix().fit([it["line"] for it in others_learn])
        # risk table from OTHER sources' val strips (structural, source-free)
        recs = []
        for n, it in others_val:
            pred = det.predict(it["line"])
            if pred <= 0:
                continue
            feats = boundary_features(det, it["line"])
            if feats is None:
                continue
            recs.append({"combo": feats["combo"],
                         "is_over": pred > it["body_start"],
                         "is_correct": pred == it["body_start"]})
        rm = BodyRiskModel.fit(recs)
        test = splits[holdout][2]
        preds_raw, preds_gated = [], []
        for it in test:
            p = det.predict(it["line"])
            preds_raw.append(p)
            if p <= 0:
                preds_gated.append(0)
                continue
            feats = boundary_features(det, it["line"])
            combo = feats["combo"] if feats else (False, False, False, False)
            preds_gated.append(0 if rm.keep(combo, tau_c_only) else p)
        m_raw = evaluate(test, preds_raw)
        m_gated = evaluate(test, preds_gated)
        lodo[holdout] = {
            "ungated": _pick(m_raw),
            "gated_C": _pick(m_gated),
        }

    # ---- gate overhead micro-bench -----------------------------------------
    # measure: (1) full predict; (2) predict + gate.  The delta isolates
    # the gate cost (rule lookup + features + decisions).
    import random as _random
    sample = [it["line"] for n in list(datasets)[:6] for it in splits[n][2][:200]]
    det0 = dets[list(datasets)[0]]
    rules0 = rules_by_ds[list(datasets)[0]]
    from .tokentypes import signature_from_tokens
    from .methods import M3_SIG_LEN

    def _timed(fn, reps=3):
        best = None
        for _ in range(reps):
            t0 = time.perf_counter_ns()
            for ln in sample:
                fn(ln)
            dt = time.perf_counter_ns() - t0
            best = dt if best is None else min(best, dt)
        return best / len(sample)

    ns_pred = _timed(lambda ln: det0.predict(ln))

    def _pred_plus_gate(ln):
        p = det0.predict(ln)
        if p <= 0:
            return
        feats = boundary_features(det0, ln)
        combo = feats["combo"] if feats else (False, False, False, False)
        sig = signature_from_tokens(det0._tok(ln), M3_SIG_LEN)
        gate_b(rules0.get((sig, combo)), tau_b_star)
        risk_model.keep(combo, tau_c_star)

    ns_pred_gate = _timed(_pred_plus_gate)
    overhead = {
        "n_lines": len(sample),
        "predict_ns_per_line": round(ns_pred, 1),
        "predict_plus_gate_ns_per_line": round(ns_pred_gate, 1),
        "gate_overhead_ns_per_line": round(ns_pred_gate - ns_pred, 1),
    }

    payload = {
        "config": {
            "split": "learn/val/test = 30/20/50, seed 42 (identical to E2)",
            "tau_b_grid": TAU_B_GRID, "tau_c_grid": TAU_C_GRID,
            "val_over_target": VAL_OVER_TARGET,
            "chosen_operating_points": {
                "E3-B": {"tau_b": tau_b_only},
                "E3-C": {"tau_c": tau_c_only},
                "E3-D": {"tau_b": tau_b_star, "tau_c": tau_c_star},
            },
            "note": "operating points selected on validation only",
        },
        "risk_table": risk_model.describe(),
        "summary": summary,
        "pooled_micro": {v: _pick(pooled[v]) for v in variant_names},
        "results": results,
        "transitions": transitions,
        "val_curves": {"E3-B": val_curve_b, "E3-C": val_curve_c},
        "test_curves": {"E3-B": test_curve_b, "E3-C": test_curve_c,
                        "E3-D@tau_b*": test_curve_d},
        "lodo_transfer": lodo,
        "gate_overhead": overhead,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "e3_results.json").write_text(
        json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    return payload


def _pick(m: dict) -> dict:
    out = {k: m[k] for k in (
        "n", "boundary_accuracy", "over_strip_rate", "under_strip_rate",
        "abstain_rate", "auto_strip_coverage", "accepted_boundary_accuracy",
        "body_integrity_char", "body_integrity_line")}
    if "content_over_rate" in m:
        out["content_over_rate"] = m["content_over_rate"]
        out["whitespace_only_over"] = m["whitespace_only_over"]
    return out


def _pooled_at(tau_b, tau_c, datasets, splits, preds_a, dets, rules_by_ds, risk_model):
    from .tokentypes import signature_from_tokens
    from .methods import M3_SIG_LEN
    items, preds = [], []
    for name in datasets:
        test = splits[name][2]
        det = dets[name]
        rules = rules_by_ds[name]
        for k, it in enumerate(test):
            p = preds_a[name][k]
            items.append(it)
            if p <= 0:
                preds.append(0)
                continue
            feats = boundary_features(det, it["line"])
            combo = feats["combo"] if feats else (False, False, False, False)
            sig = signature_from_tokens(det._tok(it["line"]), M3_SIG_LEN)
            rule = rules.get((sig, combo))
            if not gate_b(rule, tau_b) or risk_model.keep(combo, tau_c):
                preds.append(0)
            else:
                preds.append(p)
    return evaluate(items, preds)


def print_report(payload: dict) -> None:
    keys = ["boundary_accuracy", "over_strip_rate", "auto_strip_coverage",
            "accepted_boundary_accuracy", "body_integrity_char", "abstain_rate"]
    print("\n=== macro summaries ===")
    for suite in ("loghub", "stress", "challenge", "all"):
        print(f"\n-- {suite} --")
        print(f"{'method':8s}" + "".join(f"{k[:12]:>13s}" for k in keys))
        for v, s in payload["summary"][suite].items():
            row = "".join(
                f"{(s[k] if s[k] is not None else 0):>13.4f}" for k in keys)
            print(f"{v:8s}{row}")
    print("\n=== pooled micro ===")
    print(f"{'method':8s}" + "".join(f"{k[:12]:>13s}" for k in keys))
    for v, s in payload["pooled_micro"].items():
        row = "".join(f"{(s[k] if s[k] is not None else 0):>13.4f}" for k in keys)
        print(f"{v:8s}{row}")
    print("\n=== risk table (pooled val) ===")
    for k, v in payload["risk_table"]["(value_end, plain_ctail_end, value_start)"].items():
        print(f"  {k:32s} n={v['n']:5d} val_over={v['val_over_rate']:.4f}")
    print("\n=== transitions ===")
    for v, t in payload["transitions"].items():
        print(f"{v}: {t['matrix']}")
        print(f"   intercept_over={t['intercept_over_rate']} "
              f"(kept {t['matrix'].get('over->keep', 0)}/{t['total_over_strips']})  "
              f"correct_loss={t['correct_reject_loss_rate']} "
              f"(kept {t['matrix'].get('correct->keep', 0)}/{t['total_correct_strips']})")
    print("\n=== test curves ===")
    for cname, curve in payload["test_curves"].items():
        print(f"-- {cname} --")
        knob = "tau_b" if "tau_b" in curve[0] else "tau_c"
        print(f"{'tau':>6s} {'acc':>8s} {'over':>8s} {'coverage':>9s} {'accB':>8s}")
        for c in curve:
            ab = c["accepted_boundary_accuracy"]
            print(f"{c[knob]:>6.2f} {c['boundary_accuracy']:>8.4f} "
                  f"{c['over_strip_rate']:>8.4f} {c['auto_strip_coverage']:>9.4f} "
                  f"{(ab if ab is not None else 0):>8.4f}")
    print("\n=== LODO transfer (independent, unknown source, C-gate only) ===")
    print(f"{'dataset':28s} {'raw acc/over/cov':>22s} {'gated acc/over/cov':>22s}")
    for ds, m in payload["lodo_transfer"].items():
        r, g = m["ungated"], m["gated_C"]
        print(f"{ds:28s} {r['boundary_accuracy']:.3f}/{r['over_strip_rate']:.3f}/{r['auto_strip_coverage']:.3f}"
              f"   {g['boundary_accuracy']:.3f}/{g['over_strip_rate']:.3f}/{g['auto_strip_coverage']:.3f}")
    print("\n=== per-dataset (acc / over / coverage) ===")
    for n in payload["results"]:
        row = "  ".join(
            f"{v}={payload['results'][n][v]['boundary_accuracy']:.3f}/"
            f"{payload['results'][n][v]['over_strip_rate']:.3f}/"
            f"{payload['results'][n][v]['auto_strip_coverage']:.3f}"
            for v in ("E3-A", "E3-D"))
        print(f"{n:28s} {row}")
    print("\ngate overhead:", payload["gate_overhead"])
    print("\nchosen points:", payload["config"]["chosen_operating_points"])

"""E4 experiment runner: rule compilation equivalence, runtime ablation,
unknown-format safety.

Sections:
  E4-C  line-by-line equivalence of the compiled runtimes (P1/P2/P3)
        against the E3-B full path (P0), at tau=0.4 and tau=0.2.
  E4-D  performance ablation P0..P3 across 8 scenarios with timing
        controls (warmup, GC off, 5 repeats, median), rule-load cost,
        memory and rule-count scaling.
  E4-E  unknown-format safety: unknown source / unknown signature /
        unknown boundary combo, cross-source routing (dsware focus),
        LODO isolation.
"""
from __future__ import annotations

import gc
import json
import random
import resource
import time
from collections import Counter, defaultdict
from pathlib import Path

from .e2_runner import SEED, load_datasets, split_dataset, suite_of
from .e4_compiler import compile_ruleset
from .e4_matcher import (
    HeaderRuleMatcher, HeaderRuleMatcherFused, HeaderRuleMatcherSequential,
    P0Baseline, save_ruleset)
from .gating import build_rules
from .metrics import evaluate, macro_average
from .span_methods import SpanM3Prefix

TAUS = (0.4, 0.2)
N_REPEAT = 5
WARMUP = 300
E3_RESULTS = Path(__file__).resolve().parents[2] / \
    "docs" / "rep" / "header-detector-v11" / "e3-results" / "e3_results.json"


# ---------------------------------------------------------------------------
# shared offline state
# ---------------------------------------------------------------------------

def _fit_all(datasets, splits):
    dets, rules3 = {}, {}
    for name, (learn, val, _t) in splits.items():
        det = SpanM3Prefix().fit([it["line"] for it in learn])
        dets[name] = det
        rules3[name] = build_rules(name, det, val)
    return dets, rules3


def _test_pairs(splits):
    """[(source, line, gt)] over all test splits, dataset order."""
    out = []
    for name, (_l, _v, test) in splits.items():
        for it in test:
            out.append((name, it["line"], it["body_start"]))
    return out


# ---------------------------------------------------------------------------
# E4-C: equivalence
# ---------------------------------------------------------------------------

def run_equivalence(splits, dets, rules3, rulesets) -> dict:
    pairs = _test_pairs(splits)
    report = {}
    for tau in TAUS:
        rs = rulesets[tau]
        matchers = {
            "P0": P0Baseline(tau, dets, rules3),
            "P1": HeaderRuleMatcherSequential(rs),
            "P2": HeaderRuleMatcher(rs),
            "P3": HeaderRuleMatcherFused(rs),
        }
        per_variant = {}
        for vname, m in matchers.items():
            if vname == "P0":
                continue
            mism = []
            counts = Counter()
            for src, line, gt in pairs:
                r0 = matchers["P0"].match(line, src)
                rx = m.match(line, src)
                same_action = (r0["action"] == rx["action"])
                same_off = (r0["body_start_offset"]
                            == rx["body_start_offset"])
                if same_action and same_off:
                    counts["agree"] += 1
                    continue
                kind = ("action" if not same_action else "offset")
                # new over-strip: runtime ACCEPTs where P0 kept, past gt
                new_over = (rx["action"] == "ACCEPT"
                            and r0["action"] == "KEEP"
                            and rx["body_start_offset"] > gt)
                counts[kind] += 1
                counts["new_over_strip"] += int(new_over)
                if len(mism) < 20:
                    mism.append({
                        "source": src, "kind": kind,
                        "line": line[:160],
                        "p0": (r0["action"], r0["body_start_offset"],
                               r0["reason"]),
                        "px": (rx["action"], rx["body_start_offset"],
                               rx["reason"]),
                        "gt": gt,
                    })
            per_variant[vname] = {
                "n": len(pairs),
                "agree": counts["agree"],
                "action_mismatch": counts["action"],
                "offset_mismatch": counts["offset"],
                "new_over_strip": counts["new_over_strip"],
                "examples": mism,
            }
        # pooled metrics P0 vs P3 (must be identical when agree == n)
        items = [{"line": ln, "body_start": gt} for _s, ln, gt in pairs]
        preds_p0 = [
            matchers["P0"].match(ln, s)["body_start_offset"]
            for s, ln, _g in pairs]
        preds_p3 = [
            matchers["P3"].match(ln, s)["body_start_offset"]
            for s, ln, _g in pairs]
        per_variant["metrics"] = {
            "P0": _pick_metrics(evaluate(items, preds_p0)),
            "P3": _pick_metrics(evaluate(items, preds_p3)),
        }
        # cross-check against the committed E3 results (tau chosen on val)
        e3_check = None
        if E3_RESULTS.exists():
            e3 = json.loads(E3_RESULTS.read_text(encoding="utf-8"))
            tau_e3 = e3["config"]["chosen_operating_points"]["E3-B"]["tau_b"]
            if abs(tau_e3 - tau) < 1e-9:
                e3b = _pick_metrics(e3["pooled_micro"]["E3-B"])
                same = all(
                    e3b[k] == per_variant["metrics"]["P0"][k]
                    for k in e3b)
                e3_check = {"tau_b": tau_e3, "p0_equals_e3b_pooled": same,
                            "e3b": e3b}
        report[f"tau={tau}"] = {"variants": per_variant, "e3_check": e3_check}
    return report


def _pick_metrics(m: dict) -> dict:
    return {k: m[k] for k in (
        "n", "boundary_accuracy", "over_strip_rate", "under_strip_rate",
        "abstain_rate", "auto_strip_coverage", "accepted_boundary_accuracy",
        "body_integrity_char", "body_integrity_line")}


# ---------------------------------------------------------------------------
# E4-D: performance ablation
# ---------------------------------------------------------------------------

def _bench(fn, pairs, warmup=WARMUP, repeats=N_REPEAT) -> dict:
    for src, ln in pairs[:warmup]:
        fn(ln, src)
    runs = []
    gc_was = gc.isenabled()
    gc.disable()
    try:
        for _ in range(repeats):
            durs = []
            c0 = time.process_time_ns()
            t0 = time.perf_counter()
            for src, ln in pairs:
                t1 = time.perf_counter_ns()
                fn(ln, src)
                durs.append(time.perf_counter_ns() - t1)
            wall = time.perf_counter() - t0
            cpu = time.process_time_ns() - c0
            durs.sort()
            n = len(durs)
            runs.append({
                "wall_s": wall,
                "ns_avg": sum(durs) / n,
                "ns_p50": durs[n // 2],
                "ns_p95": durs[int(n * 0.95)],
                "ns_p99": durs[min(n - 1, int(n * 0.99))],
                "lines_per_s": n / wall,
                "cpu_ns_per_line": cpu / n,
            })
    finally:
        if gc_was:
            gc.enable()
    runs.sort(key=lambda r: r["wall_s"])
    med = runs[len(runs) // 2]
    med = {k: (round(v, 1) if isinstance(v, float) else v)
           for k, v in med.items()}
    med["lines_per_s_min"] = round(min(r["lines_per_s"] for r in runs), 1)
    med["lines_per_s_max"] = round(max(r["lines_per_s"] for r in runs), 1)
    med["n_repeat"] = repeats
    return med


def _scenarios(splits, matchers_p3):
    """Build the 8 required scenarios as [(source, line)] lists."""
    pairs = [(s, ln) for s, ln, _g in _test_pairs(splits)]
    p3 = matchers_p3
    acc_pairs = [(s, ln) for s, ln in pairs
                 if p3.match(ln, s)["action"] == "ACCEPT"]
    keep_pairs = [(s, ln) for s, ln in pairs
                  if p3.match(ln, s)["action"] == "KEEP"]
    long_pairs = [(s, ln) for s, ln in pairs if len(ln) > 256]
    if len(long_pairs) < 300:               # pad bodies (perf-only scenario)
        need = 300 - len(long_pairs)
        pad = []
        for s, ln in pairs[:need]:
            pad.append((s, ln + " tail " + "x" * 200))
        long_pairs = long_pairs + pad
    bracket = [(s, ln) for s, ln in pairs
               if s in ("challenge-brackets", "stress-bracket-space")]
    # round-robin interleave across sources (mixed formats)
    by_src = defaultdict(list)
    for s, ln in pairs:
        by_src[s].append((s, ln))
    mixed = []
    iters = {s: iter(v) for s, v in by_src.items()}
    while iters:
        for s in list(iters):
            try:
                mixed.append(next(iters[s]))
            except StopIteration:
                del iters[s]
    top_line = Counter(ln for _s, ln in pairs).most_common(1)[0][0]
    top_src = next(s for s, ln in pairs if ln == top_line)
    return {
        "high_hit_rate": acc_pairs,
        "low_hit_rate": keep_pairs,
        "unknown_source": [(f"__unknown__{i % 3}__", ln)
                           for i, (s, ln) in enumerate(pairs)],
        "repeated_line": [(top_src, top_line)] * 2000,
        "many_distinct": pairs,
        "long_lines": long_pairs,
        "mixed_formats": mixed,
        "bracket_prefix": bracket,
    }


def run_performance(splits, dets, rules3, rulesets, out_dir) -> dict:
    tau = TAUS[0]
    rs = rulesets[tau]
    matchers = {
        "P0": P0Baseline(tau, dets, rules3).match,
        "P1": HeaderRuleMatcherSequential(rs).match,
        "P2": HeaderRuleMatcher(rs).match,
        "P3": HeaderRuleMatcherFused(rs).match,
    }
    p3m = HeaderRuleMatcherFused(rs)
    scen = _scenarios(splits, p3m)
    perf = {}
    for sname, spairs in scen.items():
        perf[sname] = {"n": len(spairs)}
        for vname, fn in matchers.items():
            perf[sname][vname] = _bench(fn, spairs)

    # ---- rule load cost -------------------------------------------------
    f = out_dir / f"rules_tau{tau}.json"
    save_ruleset(rs, f)
    t0 = time.perf_counter()
    loaded = json.loads(f.read_text(encoding="utf-8"))
    t_parse = time.perf_counter() - t0
    t0 = time.perf_counter()
    HeaderRuleMatcherFused(loaded)
    t_build = time.perf_counter() - t0
    perf["rule_load"] = {
        "rule_file_bytes": f.stat().st_size,
        "json_parse_s": round(t_parse, 4),
        "index_build_s": round(t_build, 4),
        "n_sources": len(rs["sources"]),
        "totals": rs["_compile_meta"]["totals"],
    }

    # ---- rule-count scaling ----------------------------------------------
    scaling = {}
    base_pairs = scen["mixed_formats"][:2000]
    for k in (1, 8, 32):
        scaled = json.loads(json.dumps(rs))     # deep copy
        for rep in range(1, k):
            for name, src in rs["sources"].items():
                scaled["sources"][f"{name}#rep{rep}"] = src
        m2 = HeaderRuleMatcher(scaled).match
        m3 = HeaderRuleMatcherFused(scaled).match
        # k==1 probes original names; k>1 probes the LAST replica
        # (worst-case index probe with the largest source count)
        probe = [(s if k == 1 else f"{s}#rep{k - 1}", ln)
                 for s, ln in base_pairs]
        scaling[f"sources_x{k}"] = {
            "n_sources": len(scaled["sources"]),
            "P2": _bench(m2, probe, warmup=200, repeats=3),
            "P3": _bench(m3, probe, warmup=200, repeats=3),
        }
    # P1 scales with per-source flat rule count: measure on the largest
    # source vs a small one (same runtime, different rule-list length)
    sizes = {n: rs["sources"][n]["meta"]["n_flat_rules"]
             for n in rs["sources"]}
    big = max(sizes, key=sizes.get)
    small = min(sizes, key=sizes.get)
    p1 = HeaderRuleMatcherSequential(rs).match
    big_pairs = [(big, ln) for s, ln in base_pairs if s == big][:500] \
        or [(big, base_pairs[0][1])] * 500
    small_pairs = [(small, ln) for s, ln in base_pairs if s == small][:500] \
        or [(small, base_pairs[0][1])] * 500
    scaling["p1_rule_list"] = {
        "largest_source": {"name": big, "n_flat_rules": sizes[big],
                           "P1": _bench(p1, big_pairs, warmup=100, repeats=3)},
        "smallest_source": {"name": small, "n_flat_rules": sizes[small],
                            "P1": _bench(p1, small_pairs, warmup=100,
                                         repeats=3)},
    }
    perf["scaling"] = scaling
    return perf


# ---------------------------------------------------------------------------
# E4-E: unknown formats and safety fallback
# ---------------------------------------------------------------------------

def run_safety(splits, rulesets) -> dict:
    tau = TAUS[0]
    rs = rulesets[tau]
    p3 = HeaderRuleMatcherFused(rs)
    pairs = _test_pairs(splits)

    # 1. unknown source -> KEEP, offset 0
    bad = [(s, ln) for s, ln, _g in pairs
           if p3.match(ln, "__no_such_source__")["action"] != "KEEP"
           or p3.match(ln, "__no_such_source__")["body_start_offset"] != 0]

    # 2. known source, foreign lines (cross-source structural collisions)
    cross = defaultdict(lambda: Counter())
    cross_over_examples = []
    by_src = defaultdict(list)
    for s, ln, gt in pairs:
        by_src[s].append((ln, gt))
    for target in rs["sources"]:
        for src, items in by_src.items():
            if src == target:
                continue
            for ln, gt in items:
                r = p3.match(ln, target)
                if r["action"] == "ACCEPT":
                    cross[target]["accept"] += 1
                    if r["body_start_offset"] > gt:
                        cross[target]["over_strip"] += 1
                        if len(cross_over_examples) < 10:
                            cross_over_examples.append(
                                {"target": target, "from": src,
                                 "line": ln[:140], "off": r["body_start_offset"],
                                 "gt": gt})
                else:
                    cross[target]["keep"] += 1

    # 3. dsware focus: every other source routed INTO stress-dsware
    dsw = cross.get("stress-dsware.jsonl", Counter())

    # 4. known signature but unknown boundary combo (no val evidence)
    no_evidence = 0
    gated = 0
    from .e4_matcher import _PREFIX_TOK, _combo_str, _combo_of
    from .tokentypes import classify, type_family
    for s, ln, _gt in pairs:
        src = p3.sources.get(s)
        if src is None:
            continue
        spans = _PREFIX_TOK(ln)
        if not spans:
            continue
        fams = [type_family(classify(x.text)) for x in spans[:4]]
        while len(fams) < 4:
            fams.append("SHORT")
        sig = tuple(fams)
        if sig not in src.trees:
            continue
        from .e4_matcher import _walk
        b = _walk(src.trees[sig][0], spans, classify)
        if b <= 0:
            continue
        r = p3.match(ln, s)
        if r["reason"] == "gate_reject":
            gated += 1
            combo_s = _combo_str(_combo_of(
                spans[b - 1].text, spans[b].text if b < len(spans) else ""))
            if (",".join(sig) + "|" + combo_s) not in rs["sources"][s]["gate"]:
                no_evidence += 1

    # 5. LODO isolation: holdout source absent from the index -> KEEP
    lodo = {}
    for holdout in rs["sources"]:
        sub = HeaderRuleMatcherFused({
            "version": rs["version"], "tau": rs["tau"],
            "sources": {k: v for k, v in rs["sources"].items()
                        if k != holdout},
        })
        bad_h = 0
        n_h = 0
        for ln, _gt in by_src[holdout]:
            n_h += 1
            r = sub.match(ln, holdout)
            if r["action"] != "KEEP" or r["body_start_offset"] != 0:
                bad_h += 1
        lodo[holdout] = {"n": n_h, "not_kept": bad_h}

    return {
        "unknown_source": {"n": len(pairs), "violations": len(bad)},
        "cross_source": {
            t: {"accept": c["accept"], "keep": c["keep"],
                "over_strip": c["over_strip"]}
            for t, c in cross.items()
        },
        "dsware_focus": {"accept": dsw["accept"], "keep": dsw["keep"],
                         "over_strip": dsw["over_strip"],
                         "examples": cross_over_examples[:5]},
        "gate_rejects": {"total_gated_keep": gated,
                         "no_val_evidence": no_evidence},
        "lodo_isolation": lodo,
    }


# ---------------------------------------------------------------------------
# top level
# ---------------------------------------------------------------------------

def run_e4(data_root: Path, out_dir: Path) -> dict:
    datasets = load_datasets(data_root)
    splits = {n: split_dataset(items) for n, items in datasets.items()}
    dets, rules3 = _fit_all(datasets, splits)
    rulesets = {tau: compile_ruleset(tau, data_root, datasets, splits)
                for tau in TAUS}

    out_dir.mkdir(parents=True, exist_ok=True)
    for tau in TAUS:
        save_ruleset(rulesets[tau], out_dir / f"rules_tau{tau}.json")

    equiv = run_equivalence(splits, dets, rules3, rulesets)
    perf = run_performance(splits, dets, rules3, rulesets, out_dir)
    safety = run_safety(splits, rulesets)

    rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    payload = {
        "config": {
            "split": "learn/val/test = 30/20/50, seed 42 (identical to E2/E3)",
            "taus": list(TAUS),
            "warmup_lines": WARMUP, "n_repeat": N_REPEAT,
            "runtimes": {
                "P0": "E3-B full path (fitted E2-D predict + gate, "
                      "redundant re-tokenization)",
                "P1": "compiled flat rules, sequential candidate matching",
                "P2": "source index + signature index tree walk",
                "P3": "P2 fused: single pass, lazy classify cache, "
                      "precomputed accept sets",
            },
        },
        "compile_meta": {str(t): rulesets[t]["_compile_meta"]
                         for t in TAUS},
        "rule_stats": {
            str(t): {n: s["meta"] for n, s in rulesets[t]["sources"].items()}
            for t in TAUS
        },
        "equivalence": equiv,
        "performance": perf,
        "safety": safety,
        "_rss_kb_at_end": rss0,
    }
    (out_dir / "e4_results.json").write_text(
        json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    return payload


def print_report(p: dict) -> None:
    print("=== compile ===")
    for t, meta in p["compile_meta"].items():
        print(f"tau={t}: fit={meta['fit_seconds']}s "
              f"val={meta['val_stats_seconds']}s "
              f"totals={meta['totals']}")
    print("\n=== E4-C equivalence ===")
    for key, rep in p["equivalence"].items():
        print(f"-- {key} --")
        for v, r in rep["variants"].items():
            if v == "metrics":
                continue
            print(f"  {v}: agree {r['agree']}/{r['n']} "
                  f"action_diff={r['action_mismatch']} "
                  f"offset_diff={r['offset_mismatch']} "
                  f"new_over={r['new_over_strip']}")
        m = rep["variants"]["metrics"]
        print(f"  P0 pooled: acc={m['P0']['boundary_accuracy']} "
              f"over={m['P0']['over_strip_rate']} "
              f"cov={m['P0']['auto_strip_coverage']}")
        print(f"  P3 pooled: acc={m['P3']['boundary_accuracy']} "
              f"over={m['P3']['over_strip_rate']} "
              f"cov={m['P3']['auto_strip_coverage']}")
        if rep.get("e3_check"):
            print(f"  P0 == committed E3-B pooled: "
                  f"{rep['e3_check']['p0_equals_e3b_pooled']}")
    print("\n=== E4-D performance (avg us | p99 us | lines/s) ===")
    perf = p["performance"]
    for sname in ("high_hit_rate", "low_hit_rate", "unknown_source",
                  "repeated_line", "many_distinct", "long_lines",
                  "mixed_formats", "bracket_prefix"):
        s = perf.get(sname, {})
        row = "  ".join(
            f"{v}={s[v]['ns_avg']/1000:.2f}|{s[v]['ns_p99']/1000:.1f}|"
            f"{s[v]['lines_per_s']:.0f}"
            for v in ("P0", "P1", "P2", "P3") if v in s)
        print(f"  {sname:16s} n={s.get('n', 0):6d}  {row}")
    print("\n  rule_load:", perf.get("rule_load"))
    print("\n  scaling:")
    for k, v in perf.get("scaling", {}).items():
        if k == "p1_rule_list":
            for side, s in v.items():
                if isinstance(s, dict) and "P1" in s:
                    print(f"    P1 {side}: {s['name']} "
                          f"({s['n_flat_rules']} rules) "
                          f"{s['P1']['ns_avg']/1000:.2f} us/line")
        else:
            print(f"    {k}: P2 {v['P2']['ns_avg']/1000:.2f} us "
                  f"P3 {v['P3']['ns_avg']/1000:.2f} us "
                  f"({v['n_sources']} sources)")
    print("\n=== E4-E safety ===")
    s = p["safety"]
    print(f"  unknown_source violations: {s['unknown_source']['violations']}"
          f"/{s['unknown_source']['n']}")
    tot_acc = sum(c["accept"] for c in s["cross_source"].values())
    tot_over = sum(c["over_strip"] for c in s["cross_source"].values())
    tot_kept = sum(c["keep"] for c in s["cross_source"].values())
    print(f"  cross-source: accept={tot_acc} keep={tot_kept} "
          f"over_strip={tot_over}")
    print(f"  dsware focus: {s['dsware_focus']}")
    print(f"  gate keeps: {s['gate_rejects']}")
    bad_lodo = {k: v for k, v in s["lodo_isolation"].items()
                if v["not_kept"]}
    print(f"  LODO isolation: {len(s['lodo_isolation'])} holdouts, "
          f"violations={bad_lodo or 0}")

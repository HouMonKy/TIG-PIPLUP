"""E4-A: offline rule compiler.

Turns the E3-B runtime (E2-D SpanM3Prefix tree walk + per-(sig,combo)
reliability gate) into a versioned, self-contained rule set that can be
matched online WITHOUT any M2/M3 statistics, E2-D boundary search or
cross-log confidence computation.

Compiled rule set layout (JSON-serializable)::

    {
      "version": "header-rules/v1",
      "tau": 0.4,
      "tokenizer": {"max_spans": 17, "max_chars": 256},
      "sources": {
        "<source>": {
          "trees": {          # signature index (4-span type families)
            "TIME,ID,ID,ID": <tree-node>,
            ...
          },
          "gate": {           # (sig | combo) -> decision + metadata
            "TIME,ID,ID,ID|1010": {"accept": true, "n": 12, ...},
            ...
          },
          "flat_rules": [ ... ],   # P1 sequential runtime (path rules)
          "meta": {...}
        }
      }
    }

Tree node::

    leaf   -> {"b": <span-index boundary>}
    split  -> {"pos": j, "by": "family"|"value"|"kind",
               "children": {key: node}, "fallback": {fam: b}}

Semantics of the compiled tree replicate ``M3Detector.predict_token_boundary``
exactly (including SHORT short-circuit, strong-tuple keys, value/kind
splits and per-family fallbacks); the char boundary replicates
``SpanM3Refined.predict_offset`` (span-start offset + last_close_end
refinement); the gate replicates ``gating.gate_b`` at the compiled tau.
"""
from __future__ import annotations

import time
from pathlib import Path

from .e2_runner import load_datasets, split_dataset, suite_of
from .gating import build_rules, gate_b
from .methods import _Leaf, _Split
from .span_methods import SpanM3Prefix
from .tokentypes import STRONG_TYPES

RULES_VERSION = "header-rules/v1"
SEP = "\x1f"          # strong-split key separator (never in type families)
GATE_SEP = "|"


# ---------------------------------------------------------------------------
# tree serialization
# ---------------------------------------------------------------------------

def _enc_key(key) -> str:
    """Children key -> JSON-safe string."""
    if isinstance(key, tuple):          # strong family split: (fam, text)
        return key[0] + SEP + key[1]
    return key                          # plain family / value / kind / SHORT


def _dec_key(by: str, key: str):
    if by == "family" and SEP in key:
        fam, text = key.split(SEP, 1)
        return (fam, text)
    return key


def _serialize_node(node) -> dict:
    if isinstance(node, _Leaf):
        return {"b": node.boundary}
    by = "kind" if node.by_kind else ("value" if node.by_value else "family")
    return {
        "pos": node.pos,
        "by": by,
        "children": {_enc_key(k): _serialize_node(c)
                     for k, c in node.children.items()},
        "fallback": dict(node.fallback),
    }


def _tree_stats(node, depth: int = 0) -> dict:
    """Max split position / max leaf boundary / node count of a tree."""
    if isinstance(node, _Leaf):
        return {"max_split": -1, "max_leaf_b": node.boundary, "nodes": 1}
    a = {"max_split": node.pos, "max_leaf_b": -1, "nodes": 1}
    for child in node.children.values():
        s = _tree_stats(child, depth + 1)
        a["max_split"] = max(a["max_split"], s["max_split"])
        a["max_leaf_b"] = max(a["max_leaf_b"], s["max_leaf_b"])
        a["nodes"] += s["nodes"]
    return a


# ---------------------------------------------------------------------------
# flat rule flattening (P1 sequential runtime)
# ---------------------------------------------------------------------------
# condition kinds (all evaluated left-to-right; a rule fires iff all hold):
#   ("len_le", j)                len(spans) <= j            -> SHORT terminal
#   ("value", j, text)           spans[j].text == text      (by_value child)
#   ("kind",  j, kind)           inner_kind(spans[j]) == kind
#   ("fam",   j, fam)            family(spans[j]) == fam    (weak-family child)
#   ("strong", j, fam, text)     family == fam and text == text
#   ("not_value", j, texts)      text not in texts          (value fallback)
#   ("not_kind",  j, kinds)      kind not in kinds          (kind fallback)
#   ("fam_not_child", j, fam, texts)  fam==fam and text not in texts
#   ("fam_other", j, fams)       family not in fams         (unseen -> 0)

def _flatten_tree(tree, sig_key: str, source: str) -> list[dict]:
    rules: list[dict] = []

    def emit(conds: list, b: int):
        rules.append({
            "rule_id": f"{source}#{sig_key}#r{len(rules)}",
            "sig": sig_key,
            "conds": [list(c) for c in conds],
            "b": b,
        })

    def walk(node, conds):
        if isinstance(node, _Leaf):
            emit(conds, node.boundary)
            return
        j = node.pos
        # SHORT short-circuit happens before any key lookup
        emit(conds + [("len_le", j)], j)
        child_keys: list = []
        strong_by_fam: dict[str, set] = {}
        fams: set = set()
        for key, child in node.children.items():
            if key == "SHORT":
                continue          # unreachable at predict time (short-circuit)
            child_keys.append(key)
            if node.by_kind:
                walk(child, conds + [("kind", j, key)])
            elif node.by_value:
                walk(child, conds + [("value", j, key)])
            else:
                if isinstance(key, tuple):
                    fams.add(key[0])
                    strong_by_fam.setdefault(key[0], set()).add(key[1])
                    walk(child, conds + [("strong", j, key[0], key[1])])
                else:
                    fams.add(key)
                    walk(child, conds + [("fam", j, key)])
        # fallback terminals (negative conditions; after child rules)
        if node.by_kind:
            fb = node.fallback.get("*", 0)
            emit(conds + [("not_kind", j, sorted(child_keys))], fb)
        elif node.by_value:
            fb = node.fallback.get("*", 0)
            emit(conds + [("not_value", j, list(child_keys))], fb)
        else:
            # strong families: unseen value -> fallback (or 0)
            for fam, texts in strong_by_fam.items():
                emit(conds + [("fam_not_child", j, fam, sorted(texts))],
                     node.fallback.get(fam, 0))
            # weak families with a fallback but no child branch
            for fam, fb in node.fallback.items():
                if fam in strong_by_fam or fam in fams:
                    continue      # covered above / child always matches
                emit(conds + [("fam", j, fam)], fb)
            # any other family: unseen structure -> keep
            known = fams | set(node.fallback)
            emit(conds + [("fam_other", j, sorted(known))], 0)

    walk(tree, [])
    return rules


# ---------------------------------------------------------------------------
# compiler
# ---------------------------------------------------------------------------

def compile_ruleset(tau: float, data_root: Path,
                    datasets: dict | None = None,
                    splits: dict | None = None) -> dict:
    """Offline compile. Uses learn split for E2-D fitting and val split for
    E3-B rule statistics -- identical data flow to the E3 experiment, so the
    compiled rules are a faithful serialization of E3-B at ``tau``."""
    if datasets is None:
        datasets = load_datasets(data_root)
    if splits is None:
        splits = {n: split_dataset(items) for n, items in datasets.items()}

    t0 = time.perf_counter()
    sources: dict[str, dict] = {}
    fit_s = 0.0
    val_s = 0.0
    for name, (learn, val, _test) in splits.items():
        tf = time.perf_counter()
        det = SpanM3Prefix().fit([it["line"] for it in learn])
        fit_s += time.perf_counter() - tf
        tv = time.perf_counter()
        rules3 = build_rules(name, det, val)
        val_s += time.perf_counter() - tv

        trees: dict[str, dict] = {}
        flat: list[dict] = []
        gate: dict[str, dict] = {}
        n_nodes = 0
        need_dist: list[int] = []
        for sig, tree in det.group_trees.items():
            sig_key = ",".join(sig)
            trees[sig_key] = _serialize_node(tree)
            st = _tree_stats(tree)
            n_nodes += st["nodes"]
            need = max(4, st["max_split"] + 1, st["max_leaf_b"] + 1)
            need_dist.append(min(17, need))
            flat.extend(_flatten_tree(tree, sig_key, name))
        for (sig, combo), r in rules3.items():
            sig_key = ",".join(sig)
            gate[sig_key + GATE_SEP + "".join("1" if c else "0" for c in combo)] = {
                "accept": bool(gate_b(r, tau)),
                "n": r.n_val_stripped,
                "val_acc": round(r.val_acc, 4),
                "val_over": round(r.val_over_rate, 4),
                "stability": round(r.stability, 4),
                "reliability": r.reliability,
                "modal_boundary": r.modal_boundary,
            }
        sources[name] = {
            "trees": trees,
            "gate": gate,
            "flat_rules": flat,
            "meta": {
                "suite": suite_of(name),
                "n_learn": len(learn), "n_val": len(val),
                "n_trees": len(trees), "n_nodes": n_nodes,
                "n_flat_rules": len(flat),
                "n_gate_entries": len(gate),
                "n_gate_accepted": sum(1 for g in gate.values() if g["accept"]),
                "tree_max_need_max": max(need_dist) if need_dist else 0,
                "tree_max_need_p90": sorted(need_dist)[
                    int(len(need_dist) * 0.9)] if need_dist else 0,
            },
        }
    compile_s = time.perf_counter() - t0
    return {
        "version": RULES_VERSION,
        "tau": tau,
        "tokenizer": {"max_spans": 17, "max_chars": 256},
        "compiled_from": {
            "detector": "E2-D SpanM3Prefix (learn split, seed 42)",
            "gate": f"E3-B rule reliability at tau_B={tau}",
            "note": "rules learned offline; no online statistics",
        },
        "sources": sources,
        "_compile_meta": {
            "fit_seconds": round(fit_s, 3),
            "val_stats_seconds": round(val_s, 3),
            "total_seconds": round(compile_s, 3),
            "n_sources": len(sources),
            "totals": {
                k: sum(s["meta"][k] for s in sources.values())
                for k in ("n_trees", "n_flat_rules", "n_gate_entries",
                          "n_gate_accepted")
            },
        },
    }

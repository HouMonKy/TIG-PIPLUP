"""批量检测 runner。"""

import argparse
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

KNOWLEDGE_DB = Path(__file__).resolve().parent / "knowledge" / \
    "initial_variants.db"

def _write_jsonl(path, rows):
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

def load_lines(args):
    """读输入 -> [(module, raw_text), ...]。"""
    path = Path(args.input)
    if not path.exists():
        raise SystemExit(f"input not found: {path}")
    out = []
    if path.suffix == ".jsonl":
        for i, line in enumerate(path.open(encoding="utf-8")):
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            mod, raw = obj.get("module"), obj.get("raw_text")
            if not mod or not raw:
                raise SystemExit(
                    f"jsonl line {i}: need 'module' and 'raw_text'")
            out.append((mod, raw))
        return out

    strip_re = re.compile(args.strip_timestamp) \
        if args.strip_timestamp else None
    mod_re = re.compile(args.module_regex) \
        if args.module_regex else None
    if mod_re is None and not args.module:
        raise SystemExit(
            "plain text input needs --module or --module-regex")
    if mod_re is not None and "module" not in mod_re.groupindex:
        raise SystemExit("--module-regex needs a 'module' named group")

    for i, line in enumerate(path.open(encoding="utf-8",
                                       errors="replace")):
        line = line.rstrip("\n")
        if not line.strip():
            continue
        if mod_re is not None:
            m = mod_re.match(line)
            if not m:
                continue
            mod = m.group("module")
            raw = m.group("raw") if "raw" in mod_re.groupindex \
                else m.group(0)
        else:
            mod, raw = args.module, line
        if strip_re is not None:
            raw = strip_re.sub("", raw, count=1)
        out.append((mod, raw.strip()))
    return out

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="真实业务数据批量检测")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--mode", choices=("online", "empty",
                                           "readonly"),
                        default="online")
    parser.add_argument("--module", default=None,
                        help="纯文本输入的固定 module")
    parser.add_argument("--module-regex", default=None,
                        help="行首模块提取正则（含 module 命名组）")
    parser.add_argument("--strip-timestamp", default=None,
                        help="消息体裁剪正则（行首时间戳等）")
    parser.add_argument("--knowledge", default=str(KNOWLEDGE_DB),
                        help="预载知识库路径（none 关闭）")
    parser.add_argument("--config", default=None,
                        help="规则配置 json（缺省 config/config_build_dme.json）")
    parser.add_argument("--metadata", default=None,
                        help="模板元数据 json（缺省 config/template_metadata.json）")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)

    lines = load_lines(args)
    if args.limit:
        lines = lines[:args.limit]
    if not lines:
        raise SystemExit("no usable input lines")

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "state").mkdir(exist_ok=True)

    from .contracts import LogEntry, MODE_ENABLED
    from .lexical_matcher import LexicalMultisetMatcher
    from .detector import (
        VariantParamAnomalyDetector)
    from .parsing import ParserBridge
    from .rule_catalog import RuleCatalog

    catalog = RuleCatalog.load(args.config, args.metadata)
    known_modules = {g.module for g in catalog.groups}
    unmatched_modules = {m for m, _ in lines
                         if str(m).lower() not in known_modules}
    if unmatched_modules:
        print(f"WARNING: modules not in rule config (no rules "
              f"will fire): {sorted(unmatched_modules)}",
              file=sys.stderr)

    matcher = LexicalMultisetMatcher(
        tau=0.75, scoring_method="minhash", strip_query_values=True)
    for g in catalog.groups:
        matcher.register_group(g)
    matcher.bind_groups(catalog.groups)

    store_db = out_dir / "state" / "variants.db"
    if args.mode in ("online", "readonly") and \
            args.knowledge.lower() != "none":
        import shutil
        shutil.copy2(args.knowledge, store_db)

    detector = VariantParamAnomalyDetector(
        catalog, matcher=matcher, variant_mode=MODE_ENABLED,
        store_path=str(store_db),
        store_read_only=(args.mode == "readonly"))
    parser = ParserBridge()

    rows, anomalies = [], []
    t_start = time.time()
    for seq, (mod, raw) in enumerate(lines):
        rec = {"seq": seq, "module": mod, "raw_text": raw}
        t0 = time.perf_counter()
        try:
            parsed = parser.parse(raw, mod)
            entry = LogEntry(
                raw_text=raw, module=mod,
                template_id=parsed.template_id,
                template_detected=parsed.template_detected,
                log_id=f"real-{seq:08d}", timestamp=seq)
            result = detector.detect(entry)
            status = result.status
            rec["template_detected"] = parsed.template_detected
            rec["route"] = result.route
            rec["matched_group"] = result.group_id
            rec["cache"] = result.cache_status
            rec["cache_reason"] = result.cache_reason
            rec["rules"] = [
                {"param_name": rr.rule.param_name,
                 "value": rr.value_raw,
                 "outcome": rr.outcome,
                 "reason": rr.reason_code,
                 "detail": rr.detail}
                for rr in (result.rule_results or [])]

            rec["status"] = status
            rec["predicted"] = int(status == "anomaly")
            if result.anomalies:
                rec["anomaly_detail"] = result.anomalies
                anomalies.append(rec)
        except Exception as exc:
            rec["status"] = "error"
            rec["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        rec["latency_ms"] = round(
            (time.perf_counter() - t0) * 1000, 2)
        rows.append(rec)
        if (seq + 1) % 500 == 0:
            print(f"  {seq + 1}/{len(lines)} "
                  f"({time.time() - t_start:.0f}s)",
                  file=sys.stderr)

    wall = time.time() - t_start
    _write_jsonl(out_dir / "results_all.jsonl", rows)
    _write_jsonl(out_dir / "anomalies.jsonl", anomalies)

    lat = sorted(r["latency_ms"] for r in rows)

    def _pct(q):
        return round(lat[min(len(lat) - 1,
                             int(len(lat) * q))], 2) if lat else 0

    summary = {
        "n_input": len(lines),
        "status_counts": dict(Counter(r["status"] for r in rows)),
        "routes": dict(Counter(r.get("route") for r in rows)),
        "matched_groups": dict(Counter(
            r.get("matched_group") for r in rows
            if r.get("matched_group"))),
        "modules": dict(Counter(m for m, _ in lines)),
        "wall_time_s": round(wall, 1),
        "latency_ms": {"p50": _pct(0.5), "p90": _pct(0.9),
                       "mean": round(sum(lat) / len(lat), 2)
                       if lat else 0},
        "mode": args.mode,
        "knowledge_preloaded": (args.mode in ("online", "readonly")
                                and args.knowledge.lower() != "none"),
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        "utf-8")

    # 运行后模板表（导出失败显式告警，不静默吞——曾因此漏出
    # 陈旧 rule_id 变体导致的导出失败）
    try:
        from .catalog_format import (
            export_final_catalog, variants_by_group_from_store)
        fake_inputs = [{"module": m, "raw_text": r}
                       for m, r in lines]
        by_group = variants_by_group_from_store(
            detector._store.lookup_all(), fake_inputs)
        export_final_catalog(catalog, by_group, "realdata",
                             out_dir / "templates_final.json")
    except Exception as exc:
        print(f"WARNING: templates_final.json export failed: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return out_dir

if __name__ == "__main__":
    main()

"""模板表导出。"""

import json
import os
import tempfile
from pathlib import Path

from .position_mapper import tokenize_for_params


def variants_by_group_from_store(store_variants, inputs):
    """TemplateVariant 列表 -> build_catalog 的 variants_by_group。"""
    from .detector import event_fingerprint

    by_fp = {}
    for inp in inputs:
        fp = event_fingerprint(inp["raw_text"])
        by_fp.setdefault((inp.get("module"), fp), []).append(inp["raw_text"])

    out = {}
    for v in store_variants:
        sig = v.structural_signature or {}
        n = sig.get("n_tokens")
        example = None
        for raw in by_fp.get((v.module, v.event_fingerprint), []):
            if n is None or len(tokenize_for_params(raw)) == n:
                example = raw
                break
        out.setdefault(v.group_id, []).append({
            "template": v.template_text,
            "param_positions": v.param_positions,
            "state": v.state,
            "structural_signature": sig,
            "example_log": example,
            "target_definition_revision": v.target_definition_revision,
        })
    return out


def build_catalog(catalog, variants_by_group=None, run_id=None,
                  completed=True):
    variants_by_group = variants_by_group or {}
    groups = []
    for group in catalog.groups:
        public_ids = {r.rule_id: f"r{i + 1}"
                      for i, r in enumerate(group.rules)}
        rules = [
            {"rule_id": public_ids[r.rule_id],
             "param_name": r.param_name,
             "detect_mode": r.detect_mode,
             "detect_detail": r.detect_detail}
            for r in group.rules]
        original = {
            "template": group.template_raw,
            "param_positions": [
                {"rule_id": public_ids[r.rule_id],
                 "param_pos": str(r.param_pos)}
                for r in group.rules],
        }
        variants = []
        seen = set()
        for item in variants_by_group.get(group.group_id, []):
            if item.get("state", "executable") != "executable":
                continue
            rev = item.get("target_definition_revision")
            if rev is not None and rev != group.target_definition_revision:
                continue
            positions = item["param_positions"]
            if set(positions) != set(public_ids):
                raise ValueError(f"invalid rule positions: {group.group_id}")
            public_positions = [
                {"rule_id": public_ids[r.rule_id],
                 "param_pos": str(positions[r.rule_id])}
                for r in group.rules]
            template = item.get("template") or item.get("template_text")
            if not template:
                raise ValueError(
                    f"empty variant template: {group.group_id}")
            identity = (template, tuple(
                (p["rule_id"], p["param_pos"])
                for p in public_positions))
            if identity in seen:
                continue
            seen.add(identity)
            variants.append(_variant_entry(
                item, group, template, public_positions))
        groups.append({
            "group_id": f"{group.module}:{group.source_template_id}",
            "module": group.module,
            "template_id": group.source_template_id,
            "rules": rules,
            "original_template": original,
            "variants": variants,
        })
    return {"catalog_schema_version": 1, "rule_groups": groups,
            "export_meta": {"run_id": run_id, "completed": completed}}


def _variant_entry(item, group, template, public_positions):
    """单变体导出。"""
    entry = {"template": template}
    example = item.get("example_log")
    if example:
        entry["example_log"] = example
    sig = item.get("structural_signature") or {}
    n_tokens = sig.get("n_tokens")
    if example and n_tokens is not None:
        if len(tokenize_for_params(example)) != n_tokens:
            raise ValueError(
                f"example log token width != signature n_tokens "
                f"{n_tokens}: {group.group_id}")
    if n_tokens is not None:
        entry["n_tokens"] = n_tokens
    entry["param_positions"] = public_positions
    return entry


def save_catalog(data, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8",
                                     dir=path.parent,
                                     delete=False) as f:
        temporary = f.name
        try:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    os.replace(temporary, path)


def export_final_catalog(catalog, variants_by_group, run_id, path,
                         completed=True):
    data = build_catalog(catalog, variants_by_group, run_id, completed)
    save_catalog(data, path)
    return data

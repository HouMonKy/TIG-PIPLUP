"""规则目录编译。"""

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .contracts import (
    RuleGroup, RuleRecord, TemplateRecord, make_rule_id)
from .utils import (
    exact_template_key, normalize_to_tokens, semantic_template_text)

CATALOG_SCHEMA = "phase1-v2"

# 规则配置默认在本包 configuration/（可经 load(config_path) 覆盖）
_DEFAULT_DIR = Path(__file__).resolve().parent / "configuration"
_DEFAULT_CONFIG = _DEFAULT_DIR / "config_build_dme.json"
_DEFAULT_METADATA = _DEFAULT_DIR / "template_metadata.json"

class RuleCatalogError(RuntimeError):
    """配置/元数据校验失败（fail closed，不静默降级）。"""

def _meta_key(module: str, template_id: Any) -> str:
    return f"{module}|{template_id}"

class RuleCatalog:
    """编译后的规则目录（不可变；启动阶段加载一次）。"""

    def __init__(self, groups: List[RuleGroup],
                 records: List[TemplateRecord],
                 raw_config: List[Dict[str, Any]],
                 metadata: Dict[str, Any],
                 catalog_version: str,
                 config_path: Optional[Path] = None,
                 metadata_path: Optional[Path] = None):
        self.groups = groups
        self.records = records
        self.raw_config = raw_config
        self.metadata = metadata
        self.catalog_version = catalog_version
        self.config_path = config_path
        self.metadata_path = metadata_path
        # 原精确命中索引（兼容层：调用方显式绑定人工模板 ID；
        # module 归一小写，与 detect 入口的大小写不敏感路由对齐）
        self.template_config_dict: Dict[Tuple[str, Any], Dict[str, Any]] = {
            (str(item.get("module", "")).lower(),
             item.get("template_id", "")): item
            for item in raw_config
        }
        # 规则组索引
        self.group_dict: Dict[str, RuleGroup] = {
            g.group_id: g for g in groups}
        # 原模板精确文本索引：(module, exact_key) -> TemplateRecord
        self.exact_text_index: Dict[Tuple[str, str], TemplateRecord] = {
            (r.module, r.exact_key): r for r in records}
        self._module_records: Dict[str, List[TemplateRecord]] = {}
        for rec in records:
            self._module_records.setdefault(rec.module, []).append(rec)

    # ------------------------------------------------------------

    @classmethod
    def load(cls, config_path: Optional[str] = None,
             metadata_path: Optional[str] = None) -> "RuleCatalog":
        cfg_p = Path(config_path) if config_path else _DEFAULT_CONFIG
        meta_p = Path(metadata_path) if metadata_path else _DEFAULT_METADATA
        if not cfg_p.exists():
            raise RuleCatalogError(f"config not found: {cfg_p}")
        if not meta_p.exists():
            raise RuleCatalogError(f"metadata not found: {meta_p}")

        cfg_bytes = cfg_p.read_bytes()
        meta_bytes = meta_p.read_bytes()
        try:
            raw_config = json.loads(cfg_bytes.decode("utf-8"))
        except Exception as exc:
            raise RuleCatalogError(f"config json error: {exc}") from exc
        try:
            metadata = json.loads(meta_bytes.decode("utf-8"))
        except Exception as exc:
            raise RuleCatalogError(f"metadata json error: {exc}") from exc
        if not isinstance(raw_config, list):
            raise RuleCatalogError("config must be a JSON list")

        version = hashlib.sha256(
            CATALOG_SCHEMA.encode() + b"\0" + cfg_bytes + b"\0"
            + meta_bytes).hexdigest()[:12]

        groups, records = cls._compile(raw_config, metadata)
        return cls(groups, records, raw_config, metadata, version,
                   cfg_p, meta_p)

    # ------------------------------------------------------------

    @staticmethod
    def _compile(raw_config, metadata):
        templates_meta = metadata.get("templates", {})
        groups: List[RuleGroup] = []
        records: List[TemplateRecord] = []
        for item in raw_config:
            module = str(item.get("module", "")).lower()
            template_id = item.get("template_id", "")
            template_raw = item.get("template", "")
            if not module or template_id == "" or not template_raw:
                raise RuleCatalogError(
                    f"config entry missing module/template_id/template: "
                    f"{item!r}")

            rules: List[RuleRecord] = []
            for idx, p in enumerate(item.get("params", [])):
                param_name = p.get("param_name")
                if not param_name or not str(param_name).strip():
                    raise RuleCatalogError(
                        f"{module}|{template_id} rule[{idx}] missing "
                        f"param_name")
                # 黑白名单值类型统一为字符串（整型/浮点静默失效
                # 防护：'3' in [3] 永为 False）；normal_range 保持
                # 字典形态（执行器语义），边界须为数值
                mode = p.get("detect_mode", "")
                detail_raw = p.get("detect_detail", [])
                if mode == "normal_range":
                    if not isinstance(detail_raw, dict):
                        raise RuleCatalogError(
                            f"{module}|{template_id} rule[{idx}] "
                            f"normal_range detect_detail must be a "
                            f"dict with lower/upper")
                    detail = {}
                    for bound in ("lower", "upper"):
                        if bound in detail_raw:
                            v = detail_raw[bound]
                            if isinstance(v, bool) or not isinstance(
                                    v, (int, float)):
                                raise RuleCatalogError(
                                    f"{module}|{template_id} "
                                    f"rule[{idx}] normal_range "
                                    f"{bound} must be numeric: "
                                    f"{v!r}")
                            detail[bound] = v
                else:
                    if not isinstance(detail_raw, list):
                        raise RuleCatalogError(
                            f"{module}|{template_id} rule[{idx}] "
                            f"detect_detail must be a list")
                    detail = []
                    for x in detail_raw:
                        if isinstance(x, bool) or not isinstance(
                                x, (str, int, float)):
                            raise RuleCatalogError(
                                f"{module}|{template_id} "
                                f"rule[{idx}] invalid "
                                f"detect_detail value: {x!r}")
                        detail.append(str(x))
                rules.append(RuleRecord(
                    rule_id=make_rule_id(module, template_id,
                                         p.get("param_pos", ""),
                                         str(param_name).strip()),
                    rule_index=idx,
                    param_name=str(param_name).strip(),
                    param_pos=p.get("param_pos", ""),
                    detect_mode=p.get("detect_mode", ""),
                    detect_detail=detail,
                ))

            # ---- RuleGroup 与版本修订 ----
            rule_revision = hashlib.sha256(json.dumps(
                [r.as_dict() for r in rules], sort_keys=True,
                ensure_ascii=False).encode()).hexdigest()[:12]
            target_revision = hashlib.sha256(json.dumps(
                sorted(r.rule_id for r in rules),
                ensure_ascii=False).encode()).hexdigest()[:12]
            meta = templates_meta.get(_meta_key(module, template_id), {})
            protected = tuple(str(x).lower()
                              for x in meta.get("protected_literals", []))
            from .position_mapper import (
                tokenize_for_params as _tpl_tokens)
            from .utils import (
                _TRAILING_PUNCT)
            _punct = _TRAILING_PUNCT
            _tpl = [t.lower().strip(_punct)
                    for t in _tpl_tokens(template_raw)]
            context = set()
            for w in protected:
                if w in _tpl:
                    i = _tpl.index(w)
                    context.add((
                        w,
                        _tpl[i - 1] if i > 0 else None,
                        _tpl[i + 1] if i < len(_tpl) - 1 else None))
                else:
                    context.add((w, None, None))
            group = RuleGroup(
                group_id=f"{module}|{template_id}",
                module=module,
                source_template_id=template_id,
                template_raw=template_raw,
                rules=rules,
                rule_revision=rule_revision,
                target_definition_revision=target_revision,
                protected_literals=frozenset(protected),
                protected_context=frozenset(context))
            groups.append(group)

            # ---- TemplateRecord（词法特征为可选元数据）----
            names_display = [str(n)
                             for n in meta.get("parameter_names", [])]
            names_lower = {n.lower() for n in names_display}
            group_name = meta.get("conflict_group") or None
            distinguishing = frozenset(
                str(x).lower()
                for x in meta.get("distinguishing_params", []))
            # 区分字段超出 parameter_names 仅作元数据警告（不再校验失败）
            toks = normalize_to_tokens(template_raw)
            records.append(TemplateRecord(
                module=module,
                source_template_id=template_id,
                template_raw=template_raw,
                exact_key=exact_template_key(template_raw),
                semantic_text=semantic_template_text(template_raw),
                normalized_template=" ".join(toks),
                fixed_tokens=tuple(toks),
                fixed_token_set=frozenset(toks),
                parameter_names=frozenset(names_lower),
                parameter_names_display=tuple(names_display),
                rules=rules,
                protected_literals=protected,
                conflict_group=group_name,
                distinguishing_params=distinguishing,
            ))
        return groups, records

    # ------------------------------------------------------------

    def exact_config(self, module: Any, template_id: Any
                     ) -> Optional[Dict[str, Any]]:
        """兼容层：按人工模板 ID 查原配置（调用方显式绑定来源）。"""
        return self.template_config_dict.get((module, template_id))

    def exact_by_text(self, module: str, exact_key: str
                      ) -> Optional[TemplateRecord]:
        """按完整模板精确键查原模板。"""
        return self.exact_text_index.get((module, exact_key))

    def group(self, group_id: str) -> Optional[RuleGroup]:
        return self.group_dict.get(group_id)

    def module_records(self, module: str) -> List[TemplateRecord]:
        return self._module_records.get(module, [])

    def modules(self) -> List[str]:
        return sorted(self._module_records.keys())

    def migration_table(self) -> List[Dict[str, Any]]:
        """22 条规则 param_name 迁移表（人工维护元数据，供审计）。"""
        rows = []
        for rec in self.records:
            meta = self.metadata["templates"].get(
                _meta_key(rec.module, rec.source_template_id), {})
            mig = meta.get("migration", {})
            for r in rec.rules:
                rows.append({
                    "module": rec.module,
                    "template_id": rec.source_template_id,
                    "template": rec.template_raw,
                    "param_pos": r.param_pos,
                    "param_name": r.param_name,
                    "basis": mig.get("basis", ""),
                                        "notes": mig.get("notes", ""),
                })
        return rows

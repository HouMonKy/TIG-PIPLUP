"""参数异常检测器。"""

import logging
import re
import time
from dataclasses import replace
from pathlib import Path

from typing import Any, Dict, List, Optional, Tuple

from .contracts import (
    CACHE_HIT, CACHE_INVALIDATED, CACHE_NONE, CACHE_NOT_REGISTERED,
    CACHE_REGISTERED, CACHE_WRITE_FAILED, DetectionResult,
    FALLBACK_DISABLED, LogEntry, MODE_ENABLED, MODE_OFF,
    MODE_SHADOW, ParamInfoPort, REPRESENTATION_UNAVAILABLE,
    RULE_ANOMALY, RULE_NORMAL, RULE_UNRESOLVED, RuleResult,
    STATUS_ANOMALY, STATUS_NORMAL, STATUS_PARTIAL, STATUS_UNRESOLVED,
    TARGET_MISSING, TemplateRecord, TemplateVariant, VALID_MODES,
    coerce_param_list)
from .position_mapper import (
    compile_structural_signature, map_rule_positions,
    tokenize_for_params, tokenize_for_params_with_spans,
    verify_structural_signature)
from .rule_catalog import RuleCatalog
from .slot_anchor import compile_slot_anchor
from .utils import (
    n1_tokens)
from .lexical_matcher import LexicalMultisetMatcher
from .utils import (
    exact_template_key,
    template_fingerprint)
from .variant_store import (
    VariantStore, new_variant_id)

logger = logging.getLogger("parameter_anomaly.detector")

RSTRIP_CHARS = "!.:_-?*@}"

from .parsing import PARSER_REVISION \
    as PARSER_REVISION_DEFAULT  # noqa: F401

_UNSET = object()   # 槽位锚定编译缓存哨兵（区分"未编译"与"编译为 None"）

# 运行时变体库默认在本包 data/（gitignore，可由 store_path 覆盖）
_STORE_DIR = Path(__file__).resolve().parent / "data" / "variant_store"

class RuleConfigError(RuntimeError):
    pass

# 原异常规则执行器（忠实移植；两条路径共用）

def legacy_is_anomaly(param_value: str, detect_mode: str,
                     detect_detail: Any) -> bool:
    """原 ParamAnomalyDetector._is_anomaly 移植（语义不变）。"""
    if detect_mode == "black_list":
        return param_value.rstrip(RSTRIP_CHARS) in detect_detail
    if detect_mode == "white_list":
        return param_value not in detect_detail
    if detect_mode == "normal_range":
        if not isinstance(detect_detail, dict):
            return False
        lower = detect_detail.get("lower", float("-inf"))
        upper = detect_detail.get("upper", float("inf"))
        try:
            num = float(param_value)
            return not (lower <= num <= upper)
        except ValueError:
            return True
    raise RuleConfigError(
        f"unsupported detect_mode: {detect_mode!r}")

# 正则兜底（标准组合统一实现；覆盖 5 种形态 + "name is value"）

# 词形后缀组：常见屈折变化（复数 s / 过去式 ed / 进行式 ing）
# cost -> costs / timeout -> timeouts / error -> errors
_SUF = "(?:s|es|ed|ing)?"

_FALLBACK_PATTERNS = tuple(
    "\\b{pn}" + _SUF + suffix
    for suffix in (
        "\\s*=\\s*:\\s*([^\\s,\\)\\]]+)",      # ret=: -2
        "\\s*=\\s*([^\\s,\\)\\]]+)",           # ret=value
        "\\s*\\(\\s*([^\\s,\\)]+)",           # ret(value)
        "\\s*:\\s*([^\\s,\\)\\]]+)",           # ret:value
        "\\s+is\\s+([^\\s,\\)\\]]+)",          # status is 5
        "\\s+to\\s+([^\\s,\\)\\]]+)",          # link status to 2
        "\\s+([^\\s,\\)\\]]+)",                # ret value
    )
)

# 兜底值形态门：只接受值形态 token（数字 / 0x 十六进制 / >=4
# 位裸十六进制，允许负号）——与系统值定义（strip_value_tokens）
# 语义一致。防止 `name value` 形态把 from/is
# 等普通词当值（错位缓存 + 错判）。
_FALLBACK_VALUE_RE = re.compile(
    r"^-?(?:\d+|0x[0-9a-fA-F]+|[0-9a-fA-F]{4,})$")

def regex_fallback_span(raw_text: str, param_name: str
                        ) -> Optional[Tuple[str, Tuple[int, int]]]:
    """正则兜底取值 + 原文半开区间 span（判定与登记共用定位）。

    仅接受值形态 token（数字/十六进制/负数）：普通词（from/is
    等）不是值，不得进入判定与缓存。返回 (值, span)；未命中
    返回 None。
    """
    pn = re.escape(param_name)
    for pat in _FALLBACK_PATTERNS:
        m = re.search(pat.format(pn=pn), raw_text, re.IGNORECASE)
        if m:
            raw_g = m.group(1)
            lead = len(raw_g) - len(raw_g.lstrip("["))
            trail = len(raw_g) - len(raw_g.rstrip(".,;:!?}"))
            val = raw_g[lead: len(raw_g) - trail] if trail else \
                raw_g[lead:]
            if val and _FALLBACK_VALUE_RE.match(val):
                return (val,
                        (m.start(1) + lead, m.end(1) - trail))
    return None

def regex_fallback_extract(raw_text: str, param_name: str
                           ) -> Optional[str]:
    """正则兜底取值：按形态优先级在原文中找参数值。

    值清洗：去尾部标点、去前导 `[`（"s:[13, -> 13"——值在括号内
    的形态下正则字符类会带入 `[`）。
    （判定与入库共用定位），不做复合值二次猜测。
    """
    got = regex_fallback_span(raw_text, param_name)
    return got[0] if got else None

# tokenize_for_params / get_content_as_tokens 的唯一实现在
# position_mapper（旧分词语义复刻）；此处再导出保持 API 兼容。
__all__ = ["legacy_is_anomaly", "tokenize_for_params",
           "get_content_as_tokens", "build_runtime_param_list",
           "VariantParamAnomalyDetector", "RuleConfigError"]

def build_runtime_param_list(raw_text: str, positions: List[Any],
                             template_id: Any = None
                             ) -> List[ParamInfoPort]:
    """按配置位置生成运行时 param_list（旧系统语义复刻）。"""
    tokens = tokenize_for_params(raw_text)
    out = []
    for pos in positions:
        try:
            value = tokens[int(pos)]
        except (IndexError, ValueError, TypeError):
            continue
        out.append(ParamInfoPort(template_pos=pos,
                                 template_param=value,
                                 template_id=template_id))
    return out

def event_fingerprint(raw_text: str) -> str:
    """原文事件指纹（方案：缓存身份保留区分事件的信息）。

    = 清洗值后的有序 N1 token 序列哈希：
      - 值变化（数值宽度/形态/字母值）不改变指纹 -> 同事件缓存命中；
      - 业务词/字段差异（合并的不同事件）改变指纹 -> 各自
        独立缓存条目，互不串用；
      - 字段重排改变序列 -> 各自条目（位置布局不同，正确）。
    仅作缓存身份，不影响取值输入、span 坐标与精确模板键。
    """
    import hashlib
    from .utils import (
        strip_value_tokens)
    toks = strip_value_tokens(n1_tokens(raw_text or ""))
    return hashlib.sha256(
        " ".join(toks).encode("utf-8")).hexdigest()[:16]

# 结果聚合

def aggregate_status(rule_results: List[RuleResult]) -> str:
    if not rule_results:
        return STATUS_UNRESOLVED
    outcomes = {r.outcome for r in rule_results}
    if RULE_ANOMALY in outcomes:
        return STATUS_ANOMALY
    if RULE_UNRESOLVED in outcomes:
        if RULE_NORMAL in outcomes:
            return STATUS_PARTIAL
        return STATUS_UNRESOLVED
    return STATUS_NORMAL

def _legacy_anomaly_dict(entry: LogEntry, rule_value: Any,
                         source_template_id: Any, position: Any,
                         detect_mode: str, detect_detail: Any,
                         param_name: str, route: str,
                         variant_id: Optional[str] = None) -> Dict[str, Any]:
    """旧接口形态异常记录（增量字段：route/param_name/variant_id；
    变体路径 position 为当前变体位置，原位置仅审计）。"""
    d = {
        "time": entry.timestamp,
        "log_id": entry.log_id,
        "template_id": source_template_id,
        "template": entry.template_detected or "",
        "param_value": rule_value,
        "param_position": position,
        "detect_mode": detect_mode,
        "mode_detail": detect_detail,
        "route": route,
        "param_name": param_name,
    }
    if variant_id:
        d["variant_id"] = variant_id
    return d

# Phase 1 主入口

class VariantParamAnomalyDetector:
    """精确（原模板/变体）优先；未命中语义匹配前置；正则/槽位取值。

    模板槽位锚定（值形态门）。
    """

    def __init__(self, catalog: RuleCatalog,
                 matcher: Optional[Any] = None,
                 variant_mode: str = MODE_SHADOW,
                 store: Optional[VariantStore] = None,
                 store_path: Optional[str] = None,
                 store_read_only: bool = False,
                 parser_revision: str = PARSER_REVISION_DEFAULT):
        if variant_mode not in VALID_MODES:
            raise ValueError(
                f"variant_mode must be one of {VALID_MODES}, "
                f"got {variant_mode!r}")
        self.catalog = catalog
        self.variant_mode = variant_mode
        self.parser_revision = parser_revision
        self._matcher = matcher
        self._store = store
        self._store_path = store_path
        self._store_read_only = store_read_only
        self._matcher_ready = False
        # 模板槽位锚定编译缓存：(group_id, rule_id) -> SlotAnchor|None
        self._slot_anchors: Dict[Tuple[str, str], Any] = {}
        if variant_mode != MODE_OFF:
            self._init_matcher_and_store()

    # ------------------------------------------------------------

    def _init_matcher_and_store(self):
        """启动期：登记规则组条目 + 从持久库恢复可执行变体。"""
        # 默认匹配器 = 生产配置的词法双臂（原文包含度臂优先 +
        # MinHash 模板臂）；语义编码器臂仅经显式注入使用
        self.matcher = self._matcher or LexicalMultisetMatcher(
            tau=0.75, scoring_method="minhash", strip_query_values=True)
        if self._store is None:
            scope = "shadow" if self.variant_mode == MODE_SHADOW \
                else "main"
            path = self._store_path or str(_STORE_DIR / f"{scope}.db")
            self._store = VariantStore(path, scope=scope,
                                       read_only=self._store_read_only)
        for group in self.catalog.groups:
            self.matcher.register_group(group)
        # 接口，register_group 内已持有组引用）
        bind = getattr(self.matcher, "bind_groups", None)
        if bind is not None:
            bind(self.catalog.groups)
        # 恢复：目标定义修订一致的可执行变体进入匹配索引
        for variant in self._store.lookup_all():
            group = self.catalog.group(variant.group_id)
            if group is None:
                continue
            if variant.target_definition_revision != \
                    group.target_definition_revision:
                continue   # 目标定义已变：待冷路径重校验
            self.matcher.register_variant(variant, group)
        self._matcher_ready = True

    # ------------------------------------------------------------

    def detect(self, entry: LogEntry) -> DetectionResult:
        # module 归一（大小写不敏感路由）：业务传 Messages/MESSAGES
        # 等价于配置的 messages，全部键查询走同一索引
        mod = str(entry.module or "").lower()
        if mod != entry.module:
            entry = replace(entry, module=mod)
        params = coerce_param_list(entry.param_list)

        # ---- 1. 旧兼容 ID 路径（调用方显式绑定人工模板 ID）----
        if (entry.template_id is not None and entry.template_id != ""
                and entry.module and params):
            config = self.catalog.exact_config(entry.module,
                                               entry.template_id)
            if config is not None:
                return self._detect_exact_by_config(entry, params, config)

        if self.variant_mode == MODE_OFF:
            return self._unresolved(entry, FALLBACK_DISABLED)

        if not entry.module:
            return self._unresolved(entry, "MODULE_UNAVAILABLE")

        # ---- 2. 精确文本路径（原模板 / 可执行变体）----
        if entry.template_detected:
            key = exact_template_key(entry.template_detected)
            record = self.catalog.exact_by_text(entry.module, key)
            if record is not None:
                return self._detect_exact_original_text(entry, record)
            efp = event_fingerprint(entry.raw_text)
            variant = self._store.lookup(entry.module, efp)
            if variant is not None:
                result = self._detect_exact_variant(entry, variant)
                if result is not None:
                    return result
                # 签名校验失败：已失效，继续冷路径
            # 负缓存：路由可判但位置映射失败的结构（取值已存）
            neg = self._detect_negative_hit(entry, efp)
            if neg is not None:
                return neg

        # ---- 3. 语义匹配前置 ----
        return self._detect_semantic(entry)

    # ------------------------------------------------------------

    def _detect_exact_by_config(self, entry, params,
                                config) -> DetectionResult:
        """旧 ID 路径：当前分支基线行为（param_pos + param_list）。"""
        t0 = time.perf_counter()
        rule_results, anomalies = [], []
        param_dict = {str(p.template_pos): p.template_param
                      for p in params}
        group = self.catalog.group(
            f"{entry.module}|{config.get('template_id')}")
        rules = group.rules if group else []
        for rule in rules:
            param_value = param_dict.get(str(rule.param_pos))
            if not param_value:
                rule_results.append(RuleResult(
                    rule=rule, outcome=RULE_UNRESOLVED,
                    reason_code=TARGET_MISSING,
                    detail=f"param_pos {rule.param_pos!r} not in "
                           f"param_list"))
                continue
            try:
                is_anomaly = legacy_is_anomaly(
                    param_value, rule.detect_mode, rule.detect_detail)
            except RuleConfigError as exc:
                rule_results.append(RuleResult(
                    rule=rule, outcome=RULE_UNRESOLVED,
                    value_raw=param_value,
                    reason_code="RULE_CONFIG_ERROR", detail=str(exc)))
                continue
            if is_anomaly:
                rule_results.append(RuleResult(
                    rule=rule, outcome=RULE_ANOMALY,
                    value_raw=param_value,
                    position=rule.param_pos))
                anomalies.append(_legacy_anomaly_dict(
                    entry, param_value, config.get("template_id"),
                    rule.param_pos, rule.detect_mode, rule.detect_detail,
                    rule.param_name, "exact_original"))
            else:
                rule_results.append(RuleResult(
                    rule=rule, outcome=RULE_NORMAL,
                    value_raw=param_value, position=rule.param_pos))
        return DetectionResult(
            route="exact_original",
            status=aggregate_status(rule_results),
            module=entry.module,
            detected_template_id=entry.template_id,
            source_template_id=config.get("template_id"),
            group_id=f"{entry.module}|{config.get('template_id')}",
            rule_results=rule_results,
            anomalies=anomalies,   # ID 路径在所有模式下均为业务输出
            catalog_version=self.catalog.catalog_version,
            variant_mode=self.variant_mode,
            timings_ms={"exact": _ms(t0)})

    def _detect_exact_original_text(self, entry,
                                    record: TemplateRecord
                                    ) -> DetectionResult:
        """精确键命中原模板：按配置位置从当前原文取值。"""
        t0 = time.perf_counter()
        group = self.catalog.group(
            f"{record.module}|{record.source_template_id}")
        tokens = tokenize_for_params(entry.raw_text)
        rule_results, anomalies = [], []
        for rule in group.rules:
            try:
                pos_int = int(rule.param_pos)
                value = tokens[pos_int]
            except (IndexError, ValueError, TypeError):
                rule_results.append(RuleResult(
                    rule=rule, outcome=RULE_UNRESOLVED,
                    reason_code=TARGET_MISSING,
                    detail=f"position {rule.param_pos!r} out of range"))
                continue
            if not value:
                rule_results.append(RuleResult(
                    rule=rule, outcome=RULE_UNRESOLVED,
                    reason_code=TARGET_MISSING))
                continue
            self._eval_rule(entry, rule, value, pos_int, rule_results,
                            anomalies, route="exact_original",
                            business=True)
        return DetectionResult(
            route="exact_original",
            status=aggregate_status(rule_results),
            module=entry.module,
            detected_template_id=entry.template_id,
            source_template_id=record.source_template_id,
            group_id=group.group_id,
            rule_results=rule_results,
            anomalies=anomalies if self.variant_mode == MODE_ENABLED
            else [],
            catalog_version=self.catalog.catalog_version,
            variant_mode=self.variant_mode,
            shadow=(self.variant_mode == MODE_SHADOW),
            timings_ms={"exact": _ms(t0)})

    def _detect_negative_hit(self, entry, efp
                             ) -> Optional[DetectionResult]:
        """负缓存命中：取回冷路径取值，用当前规则重判（零模型调用）。

        适用于路由成功但位置映射失败的结构（参数缺失/配对失败等，
        无法登记正变体）。只校验目标定义修订：取值与当前规则重判，
        黑名单变化自动生效（与正变体缓存同一语义）。
        """
        neg = self._store.lookup_negative(entry.module, efp,
                                          self.parser_revision)
        if neg is None:
            return None
        group = self.catalog.group(neg["group_id"])
        if group is None:
            return None
        if neg["target_definition_revision"] != \
                group.target_definition_revision:
            return None   # 目标定义已变：取值需重校验，回冷路径
        values = neg["values"]
        rule_results: List[RuleResult] = []
        anomalies: List[Dict[str, Any]] = []
        for rule in group.rules:
            if rule.rule_id not in values:
                continue
            # 命中即 unresolved，不携带取值的判定
            rule_results.append(RuleResult(
                rule=rule, outcome=RULE_UNRESOLVED,
                reason_code=TARGET_MISSING,
                detail="negative cache: value missing"))
        if not rule_results:
            return None
        return DetectionResult(
            route="exact_negative",
            status=aggregate_status(rule_results),
            module=entry.module,
            detected_template_id=entry.template_id,
            source_template_id=group.source_template_id,
            group_id=group.group_id,
            rule_results=rule_results,
            anomalies=anomalies,
            cache_status=CACHE_HIT,
            cache_reason="negative_cache_hit",
            catalog_version=self.catalog.catalog_version,
            variant_mode=self.variant_mode,
            shadow=(self.variant_mode == MODE_SHADOW))

    def _detect_exact_variant(self, entry,
                              variant: TemplateVariant
                              ) -> Optional[DetectionResult]:
        """热路径：签名校验 -> 按变体自身位置取值 -> 组内最新规则。

        返回 None 表示签名/修订校验失败（已失效），继续冷路径。
        全程无模型调用。
        """
        t0 = time.perf_counter()
        group = self.catalog.group(variant.group_id)
        if group is None:
            self._store.invalidate(variant.variant_id, "group_missing")
            return None
        if variant.target_definition_revision != \
                group.target_definition_revision:
            # 目标定义已变：位置需重校验，不使用（回到冷路径）
            self._store.invalidate(variant.variant_id,
                                   "target_definition_changed")
            return None

        tokens = tokenize_for_params(entry.raw_text)
        # 不携带主句语义）；历史错误归属条目不因缓存命中绕过
        if group.protected_context:
            from .utils import (
                check_protected_gate)
            if not check_protected_gate(
                    group.protected_context, tokens):
                return None
        ok, reason = verify_structural_signature(
            variant.structural_signature, tokens)
        if not ok:
            self._store.invalidate(variant.variant_id, reason)
            self._last_invalidation = (variant.variant_id, reason)
            return None

        rule_results, anomalies = [], []
        for rule in group.rules:
            pos = variant.param_positions.get(rule.rule_id)
            if pos is None:
                rule_results.append(RuleResult(
                    rule=rule, outcome=RULE_UNRESOLVED,
                    reason_code=TARGET_MISSING,
                    detail=f"rule {rule.rule_id} has no position in "
                           f"variant"))
                continue
            try:
                value = tokens[int(pos)]
            except (IndexError, TypeError):
                rule_results.append(RuleResult(
                    rule=rule, outcome=RULE_UNRESOLVED,
                    reason_code=TARGET_MISSING))
                continue
            self._eval_rule(entry, rule, value, int(pos), rule_results,
                            anomalies, route="exact_variant",
                            business=(self.variant_mode == MODE_ENABLED),
                            variant_id=variant.variant_id)
        return DetectionResult(
            route="exact_variant",
            status=aggregate_status(rule_results),
            module=entry.module,
            detected_template_id=entry.template_id,
            source_template_id=group.source_template_id,
            variant_id=variant.variant_id,
            group_id=group.group_id,
            rule_results=rule_results,
            anomalies=anomalies,
            cache_status=CACHE_HIT,
            catalog_version=self.catalog.catalog_version,
            variant_mode=self.variant_mode,
            shadow=(self.variant_mode == MODE_SHADOW),
            timings_ms={"exact": _ms(t0)})

    # ------------------------------------------------------------

    def _detect_semantic(self, entry: LogEntry) -> DetectionResult:
        """语义匹配前置 -> 取值 -> 规则检测 -> 位置映射与登记。"""
        timings: Dict[str, float] = {}
        base = dict(module=entry.module,
                    detected_template_id=entry.template_id,
                    variant_mode=self.variant_mode,
                    catalog_version=self.catalog.catalog_version,
                    shadow=(self.variant_mode == MODE_SHADOW))

        if not entry.template_detected:
            return DetectionResult(
                route="unresolved", status=STATUS_UNRESOLVED,
                module=entry.module,
                detected_template_id=entry.template_id,
                reason_code=REPRESENTATION_UNAVAILABLE,
                variant_mode=self.variant_mode,
                catalog_version=self.catalog.catalog_version,
                shadow=(self.variant_mode == MODE_SHADOW))

        # ---- 语义匹配 ----
        # 原文包含度优先（更严格的规则先行）：归属率 0.9945 vs
        # 臂不可分。MinHash 模板臂作为拒判时的兜底。
        t0 = time.perf_counter()
        match_raw = getattr(self.matcher, "match_raw", None)
        outcome = match_raw(entry.module, entry.raw_text) \
            if match_raw is not None else None
        if outcome is None or not outcome.accepted \
                or outcome.group is None:
            outcome = self.matcher.match(entry.module,
                                         entry.template_detected,
                                         raw_text=entry.raw_text)
        timings["semantic"] = _ms(t0)
        if not outcome.accepted or outcome.group is None:
            return DetectionResult(
                route="unresolved", status=STATUS_UNRESOLVED,
                reason_code=outcome.reason_code,
                matching_evidence=outcome.evidence,
                timings_ms=timings,
                cache_status=CACHE_NOT_REGISTERED
                if outcome.accepted else CACHE_NONE,
                cache_reason="match_rejected"
                if not outcome.accepted else None,
                **base)
        group = outcome.group

        # ---- 按目标名取值（正则 -> 槽位锚定）----
        t2 = time.perf_counter()
        rule_results: List[RuleResult] = []
        anomalies: List[Dict[str, Any]] = []
        tokens, spans = tokenize_for_params_with_spans(entry.raw_text)
        slot_candidates: Dict[str, Tuple[str, Tuple[int, int]]] = {}
        slot_hit = False
        for rule in group.rules:
            value: Optional[str] = None
            reason_missing = TARGET_MISSING
            detail_missing = (f"param_name '{rule.param_name}' not "
                              f"located by regex/slot")
            got = regex_fallback_span(entry.raw_text, rule.param_name)
            if got is not None:
                value = got[0]
            from_slot = False
            if value is None:
                slot = self._locate_slot(group, rule, tokens, spans)
                if slot is not None:
                    if not _FALLBACK_VALUE_RE.match(slot.value):
                        rule_results.append(RuleResult(
                            rule=rule, outcome=RULE_UNRESOLVED,
                            value_raw=None,
                            reason_code="SLOT_SHAPE_GATE",
                            detail=f"slot value not value-shaped: "
                                   f"{slot.value!r}"))
                        continue
                    value = slot.value
                    from_slot = True
                    slot_hit = True
                    if not slot.sub_token:
                        # token 整值：可进位置映射（变体登记/热路径）
                        slot_candidates[rule.rule_id] = (slot.value,
                                                         slot.span)
            if value is None:
                rule_results.append(RuleResult(
                    rule=rule, outcome=RULE_UNRESOLVED,
                    reason_code=reason_missing,
                    detail=detail_missing))
                continue
            self._eval_rule(entry, rule, value, None, rule_results,
                            anomalies,
                            route="semantic_template_slot"
                            if from_slot else "semantic_regex",
                            business=(self.variant_mode == MODE_ENABLED))
        timings["rule_eval"] = _ms(t2)

        # ---- 位置映射（检测已定；仅决定可否缓存）----
        t3 = time.perf_counter()
        mapping = map_rule_positions(
            group, {}, tokens, spans,
            entry.template_detected or "",
            fallback_locator=lambda param_name: regex_fallback_span(
                entry.raw_text, param_name),
            slot_candidates=slot_candidates or None)
        timings["position_mapping"] = _ms(t3)
        pos_by_rule = mapping.positions()
        for rr in rule_results:
            if rr.rule.rule_id in pos_by_rule:
                rr.position = pos_by_rule[rr.rule.rule_id]

        cache_status = CACHE_NOT_REGISTERED
        cache_reason: Optional[str] = None
        variant_id: Optional[str] = None
        if mapping.all_ok:
            t4 = time.perf_counter()
            ok, reason = self._register_variant(
                entry, group, outcome, mapping, tokens)
            timings["store"] = _ms(t4)
            if ok:
                cache_status = CACHE_REGISTERED
                variant_id = reason   # register 返回 variant_id
            else:
                cache_status = CACHE_WRITE_FAILED if \
                    (reason or "").startswith("sqlite") else \
                    CACHE_NOT_REGISTERED
                cache_reason = reason
        else:
            cache_reason = ";".join(
                f"{k}:{v}" for k, v in mapping.reasons().items())
            # 负缓存：仅登记"全部目标取不到值"的结构（缺失与值
            #（实测存值会引入误报）。
            if rule_results and all(rr.value_raw is None
                                    for rr in rule_results):
                self._store.register_negative(
                    entry.module, event_fingerprint(entry.raw_text),
                    group.group_id,
                    {rr.rule.rule_id: None for rr in rule_results},
                    group.target_definition_revision,
                    group.rule_revision, self.parser_revision)

        result = DetectionResult(
            route="semantic_template_slot" if slot_hit
            else "semantic_regex",
            status=aggregate_status(rule_results),
            source_template_id=group.source_template_id,
            variant_id=variant_id,
            group_id=group.group_id,
            rule_results=rule_results,
            anomalies=anomalies,
            matching_evidence=outcome.evidence,
            cache_status=cache_status,
            cache_reason=cache_reason,
            timings_ms=timings,
            encoder_revision=outcome.evidence.encoder_revision,
            **base)
        self._log_detection(entry, result)
        return result

    # ------------------------------------------------------------

    def _locate_slot(self, group, rule, tokens, spans):
        """模板槽位锚定取值（编译缓存 + 唯一匹配）。

        编译失败（param_pos 不指向占位符槽位）返回 None——该规则
        退回纯名字驱动链路。与正则/模板专用逻辑无关：定位器完全
        由规则组模板与 param_pos 推导。
        """
        key = (group.group_id, rule.rule_id)
        anchor = self._slot_anchors.get(key, _UNSET)
        if anchor is _UNSET:
            anchor = compile_slot_anchor(
                group.template_raw, rule.param_pos,
                param_name=rule.param_name)
            self._slot_anchors[key] = anchor
        if anchor is None:
            return None
        return anchor.locate(tokens, spans)

    def _register_variant(self, entry, group, outcome, mapping, tokens
                          ) -> Tuple[bool, Optional[str]]:
        """登记可执行变体（含 build_runtime_param_list 字面回放验证）。"""
        positions = mapping.positions()
        for r in mapping.rule_results:
            if not r.ok:
                continue
            replay = build_runtime_param_list(entry.raw_text,
                                              [r.position])
            if not replay or replay[0].template_param != r.value:
                return False, f"replay_failed:{r.rule.rule_id}"
        variant = TemplateVariant(
            variant_id=new_variant_id(),
            group_id=group.group_id,
            module=entry.module,
            template_text=entry.template_detected or "",
            template_fingerprint=template_fingerprint(
                entry.template_detected or ""),
            param_positions=positions,
            structural_signature=compile_structural_signature(mapping),
            parser_revision=self.parser_revision,
            target_definition_revision=group.target_definition_revision,
            matched_template_id=group.source_template_id,
            similarity_score=outcome.score,
            encoder_revision=outcome.evidence.encoder_revision,
            event_fingerprint=event_fingerprint(entry.raw_text),
            state="executable")
        ok, reason = self._store.register(variant,
                                          rule_revision=group.rule_revision)
        if ok:
            self.matcher.register_variant(variant, group)
            return True, variant.variant_id
        if reason == "duplicate":
            return True, variant.variant_id   # 已存在：视为已登记
        return False, reason

    def _eval_rule(self, entry, rule, value, position, rule_results,
                   anomalies, route, business: bool,
                   variant_id: Optional[str] = None):
        try:
            is_anomaly = legacy_is_anomaly(value, rule.detect_mode,
                                           rule.detect_detail)
        except RuleConfigError as exc:
            rule_results.append(RuleResult(
                rule=rule, outcome=RULE_UNRESOLVED, value_raw=value,
                reason_code="RULE_CONFIG_ERROR", detail=str(exc),
                position=position))
            return
        if is_anomaly:
            rule_results.append(RuleResult(
                rule=rule, outcome=RULE_ANOMALY, value_raw=value,
                position=position))
            if business:
                src_tid = rule.rule_id.split("|")[1] \
                    if "|" in str(rule.rule_id) else None
                anomalies.append(_legacy_anomaly_dict(
                    entry, value, src_tid, position, rule.detect_mode,
                    rule.detect_detail, rule.param_name, route,
                    variant_id=variant_id))
        else:
            rule_results.append(RuleResult(
                rule=rule, outcome=RULE_NORMAL, value_raw=value,
                position=position))

    def _unresolved(self, entry, reason_code, **extra) -> DetectionResult:
        return DetectionResult(
            route="unresolved", status=STATUS_UNRESOLVED,
            module=entry.module,
            detected_template_id=entry.template_id,
            reason_code=reason_code, **extra)

    def _log_detection(self, entry, result):
        if self.variant_mode == MODE_OFF:
            return
        ev = result.matching_evidence
        logger.info(
            "parameter_anomaly: log_id=%s module=%s route=%s "
            "detected=%s group=%s variant=%s best=%s tau=%s "
            "cache=%s/%s status=%s reason=%s values=%s",
            entry.log_id, entry.module, result.route,
            entry.template_id, result.group_id, result.variant_id,
            ev.best_score if ev else None,
            ev.tau if ev else None,
            result.cache_status, result.cache_reason,
            result.status, result.reason_code,
            {r.rule.param_name: r.value_raw
             for r in result.rule_results})

    # ------------------------------------------------------------

# helpers

def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000.0, 3)

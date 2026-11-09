"""参数异常检测核心（正则版）。

路由：精确模板文本 -> 词法双臂匹配（原文包含度 + 模板 Jaccard）；
取值：参数名正则（七形态）优先，模板槽位锚定兜底；判定：黑名单/
白名单/区间（legacy_is_anomaly，原系统语义移植）。

规则组模板与知识库静态变体模板（knowledge/route_templates.json）
同为匹配候选。不做模板解析（解析由上游/ParserBridge 完成）、
不维护运行时缓存——每次检测按当前日志现算。
"""

import json
import logging
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .contracts import (
    LogEntry, RuleResult, DetectionResult, ROUTE_EXACT_ORIGINAL,
    ROUTE_UNRESOLVED, STATUS_UNRESOLVED, RULE_ANOMALY, RULE_NORMAL,
    RULE_UNRESOLVED, TARGET_MISSING, MODULE_UNAVAILABLE,
    REPRESENTATION_UNAVAILABLE, coerce_param_list)
from .lexical_matcher import LexicalMultisetMatcher
from .position_mapper import (
    tokenize_for_params, tokenize_for_params_with_spans)
from .slot_anchor import compile_slot_anchor
from .utils import exact_template_key

logger = logging.getLogger("parameter_anomaly.detector")

RSTRIP_CHARS = "!.:_-?*@}"

_UNSET = object()   # 槽位锚定编译缓存哨兵（区分"未编译"与"编译为 None"）

_ROUTE_TEMPLATES_DEFAULT = Path(__file__).resolve().parent / \
    "knowledge" / "route_templates.json"

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
# 等普通词当值（错判）。
_FALLBACK_VALUE_RE = re.compile(
    r"^-?(?:\d+|0x[0-9a-fA-F]+|[0-9a-fA-F]{4,})$")

def regex_fallback_span(raw_text: str, param_name: str
                        ) -> Optional[Tuple[str, Tuple[int, int]]]:
    """正则兜底取值 + 原文半开区间 span。

    仅接受值形态 token（数字/十六进制/负数）：普通词（from/is
    等）不是值，不得进入判定。返回 (值, span)；未命中返回 None。
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
    的形态下正则字符类会带入 `[`）。不做复合值二次猜测。
    """
    got = regex_fallback_span(raw_text, param_name)
    return got[0] if got else None

# tokenize_for_params / get_content_as_tokens 的唯一实现在
# position_mapper（旧分词语义复刻）；此处再导出保持 API 兼容。
__all__ = ["legacy_is_anomaly", "tokenize_for_params",
           "get_content_as_tokens", "build_runtime_param_list",
           "ParamAnomalyCoreDetector", "RuleConfigError"]

def build_runtime_param_list(raw_text: str, positions: List[Any],
                             template_id: Any = None
                             ) -> List:
    """按配置位置生成运行时 param_list（旧系统语义复刻）。"""
    from .contracts import ParamInfoPort
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

# 结果聚合

def aggregate_status(rule_results: List[RuleResult]) -> str:
    if not rule_results:
        return STATUS_UNRESOLVED
    outcomes = {r.outcome for r in rule_results}
    if RULE_ANOMALY in outcomes:
        return "anomaly"
    if RULE_UNRESOLVED in outcomes:
        if RULE_NORMAL in outcomes:
            return "partial"
        return "unresolved"
    return "normal"

def _legacy_anomaly_dict(entry: LogEntry, rule_value: Any,
                         source_template_id: Any, position: Any,
                         detect_mode: str, detect_detail: Any,
                         param_name: str, route: str
                         ) -> Dict[str, Any]:
    """旧接口形态异常记录（增量字段：route/param_name）。"""
    return {
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

# Phase 1 主入口

class ParamAnomalyCoreDetector:
    """精确（原模板）优先；词法双臂匹配；正则/槽位锚定取值。"""

    def __init__(self, catalog, matcher=None,
                 route_templates_path=None):
        """
        参数:
            catalog: RuleCatalog
            matcher: 可注入匹配器（缺省 LexicalMultisetMatcher，
                    tau=0.75）
            route_templates_path: 知识库静态变体模板 JSON；
                    None 用包内默认，"none" 关闭
        """
        self.catalog = catalog
        self.matcher = matcher or LexicalMultisetMatcher(
            tau=0.75, strip_query_values=True)
        for group in catalog.groups:
            self.matcher.register_group(group)
        bind = getattr(self.matcher, "bind_groups", None)
        if bind is not None:
            bind(catalog.groups)
        # 模板槽位锚定编译缓存：(group_id, rule_id) -> SlotAnchor|None
        self._slot_anchors: Dict[Tuple[str, str], Any] = {}
        self._load_route_templates(route_templates_path)

    def _load_route_templates(self, route_templates_path):
        """知识库静态变体模板注册为匹配候选。"""
        if route_templates_path is None:
            path = _ROUTE_TEMPLATES_DEFAULT
        elif str(route_templates_path).lower() == "none":
            return
        else:
            path = Path(route_templates_path)
        if not path.exists():
            if route_templates_path is not None:
                raise FileNotFoundError(
                    f"route templates not found: {path}")
            return
        for item in json.loads(path.read_text("utf-8")):
            group = self.catalog.group(item["group_id"])
            if group is None:
                continue
            self.matcher.register_routed_template(
                group, item["template"])

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
                return self._detect_exact_by_config(entry, params,
                                                    config)

        if not entry.module:
            return self._unresolved(entry, MODULE_UNAVAILABLE)

        # ---- 2. 精确文本路径（原模板）----
        if entry.template_detected:
            key = exact_template_key(entry.template_detected)
            record = self.catalog.exact_by_text(entry.module, key)
            if record is not None:
                return self._detect_exact_original_text(entry, record)

        # ---- 3. 词法双臂匹配 ----
        return self._detect_semantic(entry)

    # ------------------------------------------------------------

    def _detect_exact_by_config(self, entry, params,
                                config) -> DetectionResult:
        """旧 ID 路径（param_pos + param_list）。"""
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
            anomalies=anomalies,
            catalog_version=self.catalog.catalog_version)

    def _detect_exact_original_text(self, entry,
                                    record) -> DetectionResult:
        """精确键命中原模板：按配置位置从当前原文取值。"""
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
                            anomalies, route="exact_original")
        return DetectionResult(
            route="exact_original",
            status=aggregate_status(rule_results),
            module=entry.module,
            detected_template_id=entry.template_id,
            source_template_id=record.source_template_id,
            group_id=group.group_id,
            rule_results=rule_results,
            anomalies=anomalies,
            catalog_version=self.catalog.catalog_version)

    def _detect_semantic(self, entry: LogEntry) -> DetectionResult:
        """词法匹配路由 -> 正则/槽位锚定取值 -> 规则判定。"""
        if not entry.template_detected:
            return self._unresolved(entry, REPRESENTATION_UNAVAILABLE)

        # 原文包含度优先（更严格的规则先行）：归属率 0.9945；
        # 模板 Jaccard 臂作为拒判时的兜底。
        match_raw = getattr(self.matcher, "match_raw", None)
        outcome = match_raw(entry.module, entry.raw_text) \
            if match_raw is not None else None
        if outcome is None or not outcome.accepted \
                or outcome.group is None:
            outcome = self.matcher.match(entry.module,
                                         entry.template_detected,
                                         raw_text=entry.raw_text)
        if not outcome.accepted or outcome.group is None:
            return DetectionResult(
                route=ROUTE_UNRESOLVED, status=STATUS_UNRESOLVED,
                module=entry.module,
                detected_template_id=entry.template_id,
                reason_code=outcome.reason_code,
                matching_evidence=outcome.evidence)
        group = outcome.group

        # ---- 按目标名取值（正则 -> 槽位锚定）----
        rule_results: List[RuleResult] = []
        anomalies: List[Dict[str, Any]] = []
        tokens, spans = tokenize_for_params_with_spans(entry.raw_text)
        slot_hit = False
        for rule in group.rules:
            value: Optional[str] = None
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
            if value is None:
                rule_results.append(RuleResult(
                    rule=rule, outcome=RULE_UNRESOLVED,
                    reason_code=TARGET_MISSING,
                    detail=f"param_name '{rule.param_name}' not "
                           f"located by regex/slot"))
                continue
            self._eval_rule(entry, rule, value, None, rule_results,
                            anomalies,
                            route="semantic_template_slot"
                            if from_slot else "semantic_regex")

        result = DetectionResult(
            route="semantic_template_slot" if slot_hit
            else "semantic_regex",
            status=aggregate_status(rule_results),
            module=entry.module,
            detected_template_id=entry.template_id,
            source_template_id=group.source_template_id,
            group_id=group.group_id,
            rule_results=rule_results,
            anomalies=anomalies,
            matching_evidence=outcome.evidence,
            catalog_version=self.catalog.catalog_version)
        self._log_detection(entry, result)
        return result

    # ------------------------------------------------------------

    def _locate_slot(self, group, rule, tokens, spans):
        """模板槽位锚定取值（编译缓存 + 唯一匹配）。

        编译失败（param_pos 不指向占位符槽位）返回 None——该规则
        退回纯名字驱动链路。定位器完全由规则组模板与 param_pos
        推导。
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

    def _eval_rule(self, entry, rule, value, position, rule_results,
                   anomalies, route):
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
            src_tid = rule.rule_id.split("|")[1] \
                if "|" in str(rule.rule_id) else None
            anomalies.append(_legacy_anomaly_dict(
                entry, value, src_tid, position, rule.detect_mode,
                rule.detect_detail, rule.param_name, route))
        else:
            rule_results.append(RuleResult(
                rule=rule, outcome=RULE_NORMAL, value_raw=value,
                position=position))

    def _unresolved(self, entry, reason_code) -> DetectionResult:
        return DetectionResult(
            route=ROUTE_UNRESOLVED, status=STATUS_UNRESOLVED,
            module=entry.module,
            detected_template_id=entry.template_id,
            reason_code=reason_code)

    def _log_detection(self, entry, result):
        ev = result.matching_evidence
        logger.info(
            "parameter_anomaly: log_id=%s module=%s route=%s "
            "detected=%s group=%s best=%s tau=%s status=%s "
            "reason=%s values=%s",
            entry.log_id, entry.module, result.route,
            entry.template_id, result.group_id,
            ev.best_score if ev else None,
            ev.tau if ev else None,
            result.status, result.reason_code,
            {r.rule.param_name: r.value_raw
             for r in result.rule_results})

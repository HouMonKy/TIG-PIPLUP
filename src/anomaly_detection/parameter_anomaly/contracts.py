"""数据契约。"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# 路由
ROUTE_EXACT_ORIGINAL = "exact_original"
ROUTE_SEMANTIC_REGEX = "semantic_regex"
ROUTE_UNRESOLVED = "unresolved"

# 状态
STATUS_NORMAL = "normal"
STATUS_ANOMALY = "anomaly"
STATUS_UNRESOLVED = "unresolved"
STATUS_PARTIAL = "partial"

# 规则结果
RULE_NORMAL = "normal"
RULE_ANOMALY = "anomaly"
RULE_UNRESOLVED = "unresolved"

# 未判定原因码

NO_CANDIDATE = "NO_CANDIDATE"
LOW_SIMILARITY = "LOW_SIMILARITY"
TARGET_MISSING = "TARGET_MISSING"
MODULE_UNAVAILABLE = "MODULE_UNAVAILABLE"
# 语义表示不可用（编码失败 / 超窗；不静默截断）
REPRESENTATION_UNAVAILABLE = "REPRESENTATION_UNAVAILABLE"
RULE_CONFIG_ERROR = "RULE_CONFIG_ERROR"

# ------------------------------------------------------------
# 语义匹配（诊断）

@dataclass
class SemanticMatchCandidate:
    """语义匹配候选（诊断）。"""
    entry_key: str                       # 稳定模板键（group/routed）
    module: str
    group_id: str
    source_template_id: Any = None
    score: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {"entry_key": self.entry_key, "module": self.module,
                "group_id": self.group_id,
                "source_template_id": self.source_template_id,
                "score": round(self.score, 4)}

@dataclass
class SemanticMatchEvidence:
    """语义匹配证据：Top-K 仅诊断；决策只看 Top1 >= tau。"""
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    best_score: Optional[float] = None
    second_score: Optional[float] = None
    tau: Optional[float] = None
    representation: str = ""             # 实际参与匹配的 token 串

    def as_dict(self) -> Dict[str, Any]:
        return {"candidates": self.candidates,
                "best_score": self.best_score,
                "second_score": self.second_score,
                "tau": self.tau,
                "representation": self.representation}

@dataclass
class SemanticMatchOutcome:
    accepted: bool
    reason_code: Optional[str]
    group: Optional["RuleGroup"] = None
    score: Optional[float] = None
    evidence: SemanticMatchEvidence = field(
        default_factory=SemanticMatchEvidence)

# ------------------------------------------------------------
# 规则

@dataclass
class RuleRecord:
    """一条人工规则。rule_id 稳定标识（不依赖数组顺序）。"""
    rule_id: str
    rule_index: int                     # 配置内顺序（仅审计）
    param_name: str                     # 人工确认
    param_pos: Any                      # 原配置值，原样保留
    detect_mode: str
    detect_detail: Any

    def as_dict(self) -> Dict[str, Any]:
        return {"rule_id": self.rule_id, "rule_index": self.rule_index,
                "param_name": self.param_name, "param_pos": self.param_pos,
                "detect_mode": self.detect_mode,
                "detect_detail": self.detect_detail}

def make_rule_id(module: str, template_id: Any, param_pos: Any,
                 param_name: str) -> str:
    """稳定 rule_id：字段变化（目标定义变化）时自然改变，
    黑名单值（detect_detail）变化不影响。"""
    return f"{module}|{template_id}|{param_pos}|{param_name}"

@dataclass
class RuleGroup:
    """规则组：同一规则来源（原模板）的规则集合。

    组内规则共享模板与受保护词；组模板与知识库静态变体模板
    同为匹配候选。
    """
    group_id: str                       # module|source_template_id
    module: str
    source_template_id: Any
    template_raw: str
    rules: List[RuleRecord] = field(default_factory=list)
    rule_revision: str = ""
    target_definition_revision: str = ""
    # 受保护词（元数据 protected_literals，小写）：模板业务语义
    # 必备词。归属命中时须通过骨架上下文校验（词与模板邻居
    # 至少一侧相邻——辅助字段值位的孤立词不携带主句语义）。
    protected_literals: frozenset = frozenset()
    # protected_context: {(word, left, right), ...} 受保护词在模板
    # legacy 分词中的左右邻居（组合门的救援依据）
    protected_context: frozenset = frozenset()

    def as_dict(self) -> Dict[str, Any]:
        return {"group_id": self.group_id, "module": self.module,
                "source_template_id": self.source_template_id,
                "template_raw": self.template_raw,
                "rules": [r.as_dict() for r in self.rules],
                "rule_revision": self.rule_revision,
                "target_definition_revision":
                    self.target_definition_revision,
                "protected_literals": sorted(self.protected_literals)}

@dataclass
class TemplateRecord:
    """原模板编译记录（词法特征保留供精确文本匹配）。"""
    module: str
    source_template_id: Any
    template_raw: str
    exact_key: str                       # 精确键（保留占位符类型/标点）
    semantic_text: str                    # 语义文本（占位符统一 <*>）
    normalized_template: str             # 旧词法基线归一（目录元数据）
    fixed_tokens: Tuple[str, ...]
    fixed_token_set: frozenset
    parameter_names: frozenset           # 可选历史元数据（不再强制校验）
    parameter_names_display: Tuple[str, ...]
    rules: List[RuleRecord] = field(default_factory=list)
    protected_literals: Tuple[str, ...] = ()
    # 冲突组/区分字段保留为目录元数据
    conflict_group: Optional[str] = None
    distinguishing_params: frozenset = frozenset()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "module": self.module,
            "source_template_id": self.source_template_id,
            "template_raw": self.template_raw,
            "exact_key": self.exact_key,
            "semantic_text": self.semantic_text,
            "parameter_names": list(self.parameter_names_display),
            "rules": [r.as_dict() for r in self.rules],
            "protected_literals": list(self.protected_literals),
            "conflict_group": self.conflict_group,
            "distinguishing_params": sorted(self.distinguishing_params),
        }

# ------------------------------------------------------------
# 检测结果

@dataclass
class RuleResult:
    """单条规则执行结果。"""
    rule: RuleRecord
    outcome: str                  # normal | anomaly | unresolved
    value_raw: Optional[str] = None
    reason_code: Optional[str] = None
    detail: Optional[str] = None
    position: Optional[int] = None     # 精确路径使用的模板坐标（审计）

    def as_dict(self) -> Dict[str, Any]:
        return {"rule": self.rule.as_dict(), "outcome": self.outcome,
                "value_raw": self.value_raw,
                "reason_code": self.reason_code, "detail": self.detail,
                "position": self.position}

@dataclass
class DetectionResult:
    """统一检测结果。

    - detected_template_id：上游解析（解析器簇 ID）或调用方传入 ID；
    - source_template_id：规则来源原模板 ID（不与 detected 混写）；
    - group_id：命中的规则组标识；
    - anomalies：旧接口形态业务异常记录。
    """
    route: str
    status: str
    module: str
    detected_template_id: Any = None
    source_template_id: Any = None
    group_id: Optional[str] = None
    rule_results: List[RuleResult] = field(default_factory=list)
    anomalies: List[Dict[str, Any]] = field(default_factory=list)
    matching_evidence: Optional[SemanticMatchEvidence] = None
    reason_code: Optional[str] = None
    catalog_version: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "route": self.route,
            "status": self.status,
            "module": self.module,
            "detected_template_id": self.detected_template_id,
            "source_template_id": self.source_template_id,
            "group_id": self.group_id,
            "rule_results": [r.as_dict() for r in self.rule_results],
            "anomalies": self.anomalies,
            "matching_evidence":
                self.matching_evidence.as_dict()
                if self.matching_evidence else None,
            "reason_code": self.reason_code,
            "catalog_version": self.catalog_version,
        }

# ------------------------------------------------------------
# 检测入口输入契约

@dataclass
class ParamInfoPort:
    """参数位值。

    template_pos 为非负整数索引或其十进制字符串，属于当前正文
    token 坐标；它不是参数名（参数名由规则的 param_name 表达）。
    """
    template_pos: Any            # int 或 str
    template_param: str
    template_id: Any = None

    def as_dict(self) -> Dict[str, Any]:
        return {"template_id": self.template_id,
                "template_pos": self.template_pos,
                "template_param": self.template_param}

@dataclass
class LogEntry:
    """检测入口的单条日志输入。"""
    raw_text: str
    module: str
    template_id: Any = None              # 解析器簇 ID 或调用方显式 ID
    template_detected: Optional[str] = None
    param_list: List[ParamInfoPort] = field(default_factory=list)
    log_id: Any = None
    timestamp: Any = None
    source: str = ""
    content_tokens: Optional[List[str]] = None
    token_spans: Optional[List[Tuple[int, int]]] = None
    template_id_namespace: str = "rule"  # 旧人工 ID 接口；解析管线显式传 parser

    def as_dict(self) -> Dict[str, Any]:
        return {
            "raw_text": self.raw_text,
            "module": self.module,
            "template_id": self.template_id,
            "template_detected": self.template_detected,
            "param_list": [p.as_dict() for p in coerce_param_list(self.param_list)],
            "log_id": self.log_id,
            "timestamp": self.timestamp,
            "source": self.source,
            "content_tokens": self.content_tokens,
            "token_spans": self.token_spans,
            "template_id_namespace": self.template_id_namespace,
        }

def coerce_param_list(entries: List[Any]) -> List[ParamInfoPort]:
    """把多种 param_list 形态统一为 ParamInfoPort 列表。"""
    out: List[ParamInfoPort] = []
    for e in entries or []:
        if isinstance(e, ParamInfoPort):
            out.append(e)
        elif isinstance(e, dict):
            out.append(ParamInfoPort(
                template_pos=e.get("template_pos"),
                template_param=str(e.get("template_param", "")),
                template_id=e.get("template_id")))
        elif isinstance(e, (tuple, list)) and len(e) == 2:
            out.append(ParamInfoPort(template_pos=e[0],
                                     template_param=str(e[1])))
        elif hasattr(e, "template_pos") and hasattr(e, "template_param"):
            out.append(ParamInfoPort(
                template_pos=getattr(e, "template_pos"),
                template_param=str(getattr(e, "template_param")),
                template_id=getattr(e, "template_id", None)))
        else:
            raise ValueError(f"unsupported param_list entry: {e!r}")
    return out

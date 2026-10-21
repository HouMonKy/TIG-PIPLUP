"""数据契约。"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# 常量

ROUTE_EXACT_ORIGINAL = "exact_original"
ROUTE_EXACT_VARIANT = "exact_variant"
ROUTE_SEMANTIC_REGEX = "semantic_regex"
ROUTE_UNRESOLVED = "unresolved"

STATUS_NORMAL = "normal"
STATUS_ANOMALY = "anomaly"
STATUS_UNRESOLVED = "unresolved"
STATUS_PARTIAL = "partial"

# 规则结果
RULE_NORMAL = "normal"
RULE_ANOMALY = "anomaly"
RULE_UNRESOLVED = "unresolved"

# feature flag
MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ENABLED = "enabled"
VALID_MODES = (MODE_OFF, MODE_SHADOW, MODE_ENABLED)

# ---- 变体缓存状态 ----
CACHE_NONE = "none"                    # 未尝试缓存（exact 路径 / 未匹配）
CACHE_REGISTERED = "registered"        # 冷路径成功登记可执行变体
CACHE_NOT_REGISTERED = "not_registered"  # 检测完成但位置不可缓存
CACHE_HIT = "hit"                      # 热路径命中变体
CACHE_INVALIDATED = "invalidated"      # 签名校验失败，已失效并回冷路径
CACHE_WRITE_FAILED = "write_failed"    # 存储写入失败（检测结果保留）

POSITION_SCHEMA_LEGACY_TOKENS = "legacy_tokens_v1"

# 未判定原因码

NO_CANDIDATE = "NO_CANDIDATE"
LOW_SIMILARITY = "LOW_SIMILARITY"
TARGET_MISSING = "TARGET_MISSING"
EXTRACTION_FAILED = "EXTRACTION_FAILED"
MODULE_UNAVAILABLE = "MODULE_UNAVAILABLE"
INPUT_CONTRACT_VIOLATION = "INPUT_CONTRACT_VIOLATION"
# 语义表示不可用（编码失败 / 超窗；不静默截断）
REPRESENTATION_UNAVAILABLE = "REPRESENTATION_UNAVAILABLE"
POSITION_NOT_REPRESENTABLE = "POSITION_NOT_REPRESENTABLE"
RULE_CONFIG_ERROR = "RULE_CONFIG_ERROR"
FALLBACK_DISABLED = "FALLBACK_DISABLED"

# 参数取值（位置映射链候选）

@dataclass
class ExtractedParameter:
    """配对成功的参数：param_name -> 该结构。"""
    value_raw: str                       # 原始文本语义，不转 int/float
    param_span: Optional[Tuple[int, int]] = None    # 半开 [start, end)
    value_span: Optional[Tuple[int, int]] = None
    param_confidence: Optional[float] = None
    value_confidence: Optional[float] = None
    confidence: Optional[float] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "value_raw": self.value_raw,
            "param_span": list(self.param_span) if self.param_span else None,
            "value_span": list(self.value_span) if self.value_span else None,
            "confidence": self.confidence,
        }

# ------------------------------------------------------------

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

    变体共享组内规则；异常值更新读取组最新规则（rule_revision 变化
    不失效位置）；目标集合变化（target_definition_revision）触发
    位置重新校验。
    """
    group_id: str                       # module|source_template_id
    module: str
    source_template_id: Any
    template_raw: str
    rules: List[RuleRecord] = field(default_factory=list)
    rule_revision: str = ""
    target_definition_revision: str = ""
    # 受保护词（元数据 protected_literals，小写）：模板业务语义
    # 必备词。归属/缓存命中时须通过骨架上下文校验（词与模板邻居
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
    """原模板编译记录（词法特征保留供离线基线；线上语义匹配用
    语义文本与向量）。"""
    module: str
    source_template_id: Any
    template_raw: str
    exact_key: str                       # 精确键（保留占位符类型/标点）
    semantic_text: str                   # 语义文本（占位符统一 <*>）
    normalized_template: str             # 旧词法基线归一（离线用）
    fixed_tokens: Tuple[str, ...]
    fixed_token_set: frozenset
    parameter_names: frozenset           # 可选历史元数据（不再强制校验）
    parameter_names_display: Tuple[str, ...]
    rules: List[RuleRecord] = field(default_factory=list)
    # 以下冲突字段保留为数据兼容；线上匹配不再用作门控
    protected_literals: Tuple[str, ...] = ()
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
# ------------------------------------------------------------

@dataclass
class TemplateVariant:
    """持久化变体：共享规则组引用 + 自身参数位置。

    event_fingerprint：原文事件指纹（清洗值后的有序 N1 token 序列
    哈希）。解析器可能把不同事件合并为同一模板——仅凭解析模板
    指纹不足以区分，缓存身份必须同时携带事件指纹，否则不同事件
    会共用同一位置缓存（首次错误归属被持续放大）。
    """
    variant_id: str
    group_id: str
    module: str
    template_text: str                   # 实际解析模板
    template_fingerprint: str            # 精确键指纹
    param_positions: Dict[str, Any]      # rule_id -> 位置（int）
    position_schema: str = POSITION_SCHEMA_LEGACY_TOKENS
    structural_signature: Dict[str, Any] = field(default_factory=dict)
    parser_revision: str = ""
    target_definition_revision: str = ""
    matched_template_id: Any = None
    similarity_score: Optional[float] = None
    encoder_revision: str = ""
    event_fingerprint: str = ""          # 原文事件指纹
    state: str = "executable"            # executable | disabled

    def as_dict(self) -> Dict[str, Any]:
        return vars(self).copy()

# ------------------------------------------------------------
# ------------------------------------------------------------

@dataclass
class SemanticMatchCandidate:
    """语义匹配候选（诊断）。"""
    entry_key: str                       # 稳定模板键（group/variant）
    module: str
    group_id: str
    source_template_id: Any = None
    variant_id: Optional[str] = None
    score: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {"entry_key": self.entry_key, "module": self.module,
                "group_id": self.group_id,
                "source_template_id": self.source_template_id,
                "variant_id": self.variant_id,
                "score": round(self.score, 4)}

@dataclass
class SemanticMatchEvidence:
    """语义匹配证据：Top-K 仅诊断；决策只看 Top1 >= tau。"""
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    best_score: Optional[float] = None
    second_score: Optional[float] = None
    tau: Optional[float] = None
    encoder_revision: str = ""
    representation: str = ""             # semantic_text 实际编码串
    index_version: int = 0               # 匹配索引版本（新增条目递增）

    def as_dict(self) -> Dict[str, Any]:
        return {"candidates": self.candidates,
                "best_score": self.best_score,
                "second_score": self.second_score,
                "tau": self.tau,
                "encoder_revision": self.encoder_revision,
                "representation": self.representation,
                "index_version": self.index_version}

@dataclass
class SemanticMatchOutcome:
    accepted: bool
    reason_code: Optional[str]
    group: Optional[RuleGroup] = None
    matched_variant_id: Optional[str] = None
    score: Optional[float] = None
    evidence: SemanticMatchEvidence = field(
        default_factory=SemanticMatchEvidence)

# ------------------------------------------------------------
# ------------------------------------------------------------

@dataclass
class RuleResult:
    """单条规则执行结果。"""
    rule: RuleRecord
    outcome: str                  # normal | anomaly | unresolved
    value_raw: Optional[str] = None
    reason_code: Optional[str] = None
    detail: Optional[str] = None
    position: Optional[int] = None     # 变体路径下实际使用的位置（审计）

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
    - variant_id / group_id：变体缓存与规则组标识；
    - cache_status / cache_reason：变体缓存生命周期；
    - anomalies：旧接口形态业务异常（shadow 下非 exact_original 路由
      恒为空，保证旧业务结果不变）。
    """
    route: str
    status: str
    module: str
    detected_template_id: Any = None
    source_template_id: Any = None
    variant_id: Optional[str] = None
    group_id: Optional[str] = None
    rule_results: List[RuleResult] = field(default_factory=list)
    anomalies: List[Dict[str, Any]] = field(default_factory=list)
    matching_evidence: Optional[SemanticMatchEvidence] = None
    reason_code: Optional[str] = None
    cache_status: str = CACHE_NONE
    cache_reason: Optional[str] = None
    catalog_version: str = ""
    encoder_revision: str = ""
    variant_mode: str = MODE_OFF
    shadow: bool = False
    timings_ms: Dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "route": self.route,
            "status": self.status,
            "module": self.module,
            "detected_template_id": self.detected_template_id,
            "source_template_id": self.source_template_id,
            "variant_id": self.variant_id,
            "group_id": self.group_id,
            "rule_results": [r.as_dict() for r in self.rule_results],
            "anomalies": self.anomalies,
            "matching_evidence":
                self.matching_evidence.as_dict()
                if self.matching_evidence else None,
            "reason_code": self.reason_code,
            "cache_status": self.cache_status,
            "cache_reason": self.cache_reason,
            "catalog_version": self.catalog_version,
            "encoder_revision": self.encoder_revision,
            "variant_mode": self.variant_mode,
            "shadow": self.shadow,
            "timings_ms": self.timings_ms,
        }

# 检测入口输入契约

@dataclass
class ParamInfoPort:
    """旧系统 ParamInfo 的对等结构（template_pos 为配置位置）。"""
    template_pos: Any            # int 或 str
    template_param: str
    template_id: Any = None

    def as_dict(self) -> Dict[str, Any]:
        return {"template_id": self.template_id,
                "template_pos": self.template_pos,
                "template_param": self.template_param}

@dataclass
class LogEntry:
    """Phase 1 检测入口的单条日志输入。"""
    raw_text: str
    module: str
    template_id: Any = None              # 解析器簇 ID 或调用方显式 ID
    template_detected: Optional[str] = None
    param_list: List[ParamInfoPort] = field(default_factory=list)
    log_id: Any = None
    timestamp: Any = None
    source: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "raw_text": self.raw_text,
            "module": self.module,
            "template_id": self.template_id,
            "template_detected": self.template_detected,
            "param_list": [p.as_dict() for p in self.param_list],
            "log_id": self.log_id,
            "timestamp": self.timestamp,
            "source": self.source,
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

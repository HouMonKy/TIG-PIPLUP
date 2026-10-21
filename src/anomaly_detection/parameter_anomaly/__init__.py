"""parameter_anomaly - 日志参数异常检测。"""

from .param_detector import ParamAnomalyDetector   # 业务总接口
from .contracts import (
    CACHE_HIT, CACHE_INVALIDATED, CACHE_NONE, CACHE_NOT_REGISTERED,
    CACHE_REGISTERED, CACHE_WRITE_FAILED, DetectionResult,
    FALLBACK_DISABLED, LogEntry, MODE_ENABLED, MODE_OFF, MODE_SHADOW,
    MODULE_UNAVAILABLE, ParamInfoPort, POSITION_NOT_REPRESENTABLE,
    POSITION_SCHEMA_LEGACY_TOKENS, REPRESENTATION_UNAVAILABLE,
    RULE_ANOMALY, RULE_NORMAL, RULE_UNRESOLVED, RuleGroup, RuleRecord,
    RuleResult, STATUS_ANOMALY, STATUS_NORMAL, STATUS_PARTIAL,
    STATUS_UNRESOLVED, TARGET_MISSING, TemplateRecord, TemplateVariant,
    VALID_MODES, coerce_param_list, make_rule_id,
)
from .detector import (
    VariantParamAnomalyDetector, build_runtime_param_list,
    legacy_is_anomaly, tokenize_for_params)
from .parsing import (
    PARSER_REVISION, ParserBridge, ParserBridgeError)
from .position_mapper import (
    compile_structural_signature, map_rule_positions,
    resolve_target_value, tokenize_for_params_with_spans,
    verify_structural_signature)
from .rule_catalog import RuleCatalog, RuleCatalogError
from .utils import (
    exact_template_key, semantic_template_text, template_fingerprint)
from .variant_store import VariantStore

__all__ = [
    "ParamAnomalyDetector",
    "RuleCatalog", "RuleCatalogError", "VariantParamAnomalyDetector",
    "ParserBridge", "ParserBridgeError", "LogEntry",
    "DetectionResult", "VariantStore",
]

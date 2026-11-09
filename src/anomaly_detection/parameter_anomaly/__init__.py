"""parameter_anomaly - 日志参数异常检测（正则版）。"""

from .param_detector import ParamAnomalyDetector   # 业务总接口
from .contracts import (
    DetectionResult, LogEntry, MODULE_UNAVAILABLE,
    ParamInfoPort, REPRESENTATION_UNAVAILABLE, RULE_ANOMALY,
    RULE_NORMAL, RULE_UNRESOLVED, RuleGroup, RuleRecord, RuleResult,
    STATUS_ANOMALY, STATUS_NORMAL, STATUS_PARTIAL, STATUS_UNRESOLVED,
    TARGET_MISSING, TemplateRecord, coerce_param_list, make_rule_id,
)
from .detector import (
    ParamAnomalyCoreDetector, build_runtime_param_list,
    legacy_is_anomaly, tokenize_for_params)
from .parsing import (
    PARSER_REVISION, ParserBridge, ParserBridgeError)
from .position_mapper import tokenize_for_params_with_spans
from .rule_catalog import RuleCatalog, RuleCatalogError
from .utils import (
    exact_template_key, semantic_template_text, template_fingerprint)

__all__ = [
    "ParamAnomalyDetector",
    "RuleCatalog", "RuleCatalogError", "ParamAnomalyCoreDetector",
    "ParserBridge", "ParserBridgeError", "LogEntry",
    "DetectionResult",
]

"""日志解析与 per-module 解析器管理。"""

import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional

from .contracts import LogEntry, ParamInfoPort
from .utils import template_fingerprint

logger = logging.getLogger("parameter_anomaly.parsing")

PARSER_REVISION = "piplup@remote_v1"


class ParserBridgeError(RuntimeError):
    pass


class ParseOutcome:
    __slots__ = ("raw_text", "module", "template_id", "template_detected",
                 "confidence", "backend", "change_type", "fingerprint")

    def __init__(self, raw_text: str, module: str, template_id,
                 template, confidence: float, backend: str,
                 change_type=None):
        self.raw_text = raw_text
        self.module = module
        self.template_id = template_id
        self.template_detected = template
        self.confidence = confidence
        self.backend = backend
        self.change_type = change_type
        self.fingerprint = template_fingerprint(template)

    @property
    def is_piplup(self) -> bool:
        return self.backend == "piplup"

    def as_dict(self) -> Dict:
        return {"raw_text": self.raw_text, "module": self.module,
                "template_id": self.template_id,
                "template_detected": self.template_detected,
                "confidence": self.confidence, "backend": self.backend,
                "change_type": self.change_type,
                "fingerprint": self.fingerprint}


class PiplupParser:
    """PIPLUP 模板解析器（引用业务 src/data_process/ 下的远端 main 固定版）。"""

    def __init__(self, use_cache: bool = True,
                 cache_path: Optional[str] = None):
        self.use_cache = use_cache
        if cache_path is None:
            state_dir = Path(__file__).resolve().parent / "data" / \
                "parser_state"
            state_dir.mkdir(parents=True, exist_ok=True)
            cache_path = str(state_dir / "vd_piplup_cache.pkl")

        from src.data_process.template_extractor.parsing_algorithm.PIPLUP \
            import LogParser
        self._parser = LogParser(
            log_format=None, module_name="vd",
            use_cache=use_cache, cache_path=cache_path,
            use_template_cache=True)
        self.stats = {"total_parsed": 0, "clusters": 0}
        self._cluster_ids: Dict[str, str] = {}
        self._prev_templates: Dict[str, str] = {}

    def parse(self, log_line: str) -> Dict[str, Any]:
        self.stats["total_parsed"] += 1
        try:
            return self._parse_piplup(log_line)
        except Exception as exc:
            logger.debug("PIPLUP parse error: %s", exc)
            return self._fallback(log_line)

    def _parse_piplup(self, log_line: str) -> Dict[str, Any]:
        templates = self._parser.parse_list([log_line])
        template = templates[0] if templates else log_line
        cluster_id = self._cluster_ids.get(template)
        if cluster_id is None:
            cluster_id = str(len(self._cluster_ids))
            self._cluster_ids[template] = cluster_id
        prev = self._prev_templates.get(cluster_id)
        change_type = "cluster_template_changed" \
            if prev is not None and prev != template else None
        self._prev_templates[cluster_id] = template
        self.stats["clusters"] = len(self._parser.logCluL)
        return {
            "template_id": cluster_id,
            "template": template,
            "confidence": 0.95,
            "backend": "piplup",
            "change_type": change_type,
        }

    def _fallback(self, log_line: str) -> Dict[str, Any]:
        tokens = log_line.split()
        tpl = ["<*>" if re.match(r"^\d+(?:\.\d+)?$", t) else t
               for t in tokens]
        return {
            "template_id": "fallback",
            "template": " ".join(tpl),
            "confidence": 0.6,
            "backend": "fallback",
            "change_type": None,
        }

    def reset(self):
        cache_path = self._parser.cache_path
        from src.data_process.template_extractor.parsing_algorithm.PIPLUP \
            import LogParser
        self._parser = LogParser(
            log_format=None, module_name="vd",
            use_cache=self.use_cache, cache_path=cache_path,
            use_template_cache=True)
        self.stats = {"total_parsed": 0, "clusters": 0}
        self._cluster_ids = {}
        self._prev_templates = {}


class ParserBridge:
    """per-module 解析器管理 + LogEntry 构造。"""

    def __init__(self):
        self._parsers: Dict[str, PiplupParser] = {}

    def parser(self, module: str) -> PiplupParser:
        if module not in self._parsers:
            self._parsers[module] = PiplupParser()
        return self._parsers[module]

    def parse(self, raw_text: str, module: str) -> ParseOutcome:
        r = self.parser(module).parse(raw_text)
        return ParseOutcome(
            raw_text=raw_text, module=module,
            template_id=r["template_id"],
            template=r["template"],
            confidence=float(r.get("confidence", 0.0)),
            backend=r.get("backend", "piplup"),
            change_type=r.get("change_type"))

    def build_log_entry(self, raw_text: str, module: str,
                        log_id=None, timestamp=None,
                        param_list: Optional[list] = None
                        ) -> tuple:
        outcome = self.parse(raw_text, module)
        return LogEntry(
            raw_text=raw_text, module=module,
            template_id=outcome.template_id,
            template_detected=outcome.template_detected,
            param_list=param_list or [],
            log_id=log_id, timestamp=timestamp,
        ), outcome

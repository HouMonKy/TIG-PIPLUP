#!/usr/bin/python3.7
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2012-2020. All rights reserved.
"""参数异常检测对外接口（业务契约不变）。

内部实现为参数异常检测系统（正则版）：模板/词面匹配路由 +
通用正则与模板槽位锚定两级取值，零模型调用。

对外接口（与 anomaly_detection.py 的调用约定一致）：
    ParamAnomalyDetector(product)
    detect(log_entries, window_size, output_path=None)
        -> (detected_outliers, detected_events, total_anomaly_count)
    detect_all_module_logs(log_entries=..., window_size=...)
        -> {module: {source: [outlier]}}

输入日志条目字段（LogTemplateRelation）：source / time / log_id /
template_id（解析模板 ID）/ module / template（首次解析模板）/
param_list（旧 ID 对位形态，兼容保留）/ raw_content（去格式正文；
PIPLUP 链路由 pipeline_adapter.attach_first_parse 挂载）。正文缺省
为空串——稀疏参数位无法可靠还原原文，缺证据以 unresolved 呈现。
业务异常输出 template_id 回写输入日志的解析模板 ID（旧合并按它
查 id_to_cluster），规则配置 ID 见 source_template_id（审计）。
"""

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .contracts import LogEntry
from .detector import ParamAnomalyCoreDetector
from .rule_catalog import RuleCatalog

_PKG_DIR = Path(__file__).resolve().parent

class ParamAnomalyDetector:
    """参数异常检测器（业务总接口的实现方）。"""

    def __init__(self, product: str, learn: bool = True,
                 learned_templates_path=None):
        """
        参数:
            product: 产品名称，如 'DME'（配置 configuration/config_build_{product}.json）
            learn: 冷路径命中即把解析模板登记为匹配候选（变体学习）
            learned_templates_path: 学习模板落盘 JSON（缺省本包
                        data/learned_templates.json，供部署覆盖）
        """
        self.product = product
        config_name = f"config_build_{product.lower()}.json"
        config_path = _PKG_DIR / "configuration" / config_name
        if not config_path.exists():
            raise ValueError(f"不支持的产品: {product}（缺少 {config_name}）")
        self.config_path = str(config_path)
        self.config_data = json.loads(config_path.read_text("utf-8"))
        self.template_config_dict = {
            (str(item.get("module", "")).lower(),
             item.get("template_id", "")): item
            for item in self.config_data}

        self.catalog = RuleCatalog.load(
            self.config_path,
            str(_PKG_DIR / "configuration" / "template_metadata.json"))

        self._detector = ParamAnomalyCoreDetector(
            self.catalog, learn=learn,
            learned_templates_path=learned_templates_path or
            str(_PKG_DIR / "data" / "learned_templates.json"))

    # 对外检测接口

    def detect(self, log_entries, window_size: int,
               output_path: str = None):
        """检测日志条目中的异常参数。

        返回: (detected_outliers, detected_events, total_anomaly_count)
        """
        outliers, events = self._run(log_entries, window_size)
        total = len(outliers)
        if output_path and total > 0:
            self.save_results(outliers, total, output_path)
        return outliers, events, total

    def detect_all_module_logs(self, log_entries,
                               window_size: int) -> Dict[str, Dict[str, list]]:
        """多 module/source 批量检测。

        返回: {"module": {"source": [outlier]}}
        """
        result: Dict[str, Dict[str, list]] = defaultdict(
            lambda: defaultdict(list))
        for entry in log_entries or []:
            # 分组键保持业务传入的原始 module（与语义/频率检测的
            # 分组一致，供上层 merge 对齐）；路由归一在 detect() 内
            module = getattr(entry, "module", "")
            source = getattr(entry, "source", "")
            outliers, _ = self._run([entry], window_size)
            result[module][source].extend(outliers)
        return {m: dict(s) for m, s in result.items()}

    # 内部：条目转换与执行

    def _run(self, log_entries, window_size: int
             ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        outliers: List[Dict[str, Any]] = []
        for entry in log_entries or []:
            template = getattr(entry, "template", None) or ""
            module = getattr(entry, "module", "")
            if not template or not module:
                continue
            log_entry = self._to_log_entry(entry)
            result = self._detector.detect(log_entry)
            for anom in result.anomalies or []:
                ts = anom.get("time") or 0
                if window_size:
                    ts = ts - ts % window_size
                outliers.append({
                    "time": ts,
                    "log_id": anom.get("log_id"),
                    # 业务契约：template_id 回写输入日志的解析模板
                    # ID（旧合并按它查 id_to_cluster）；规则配置 ID
                    # 见 source_template_id（审计）
                    "template_id": getattr(entry, "template_id", None),
                    "source_template_id": anom.get("template_id"),
                    "template": anom.get("template"),
                    "param_value": anom.get("param_value"),
                    "param_position": anom.get("param_position"),
                    "detect_mode": anom.get("detect_mode"),
                    "mode_detail": anom.get("mode_detail"),
                })
        # 事件聚合（按窗口起始时间）
        event_dict: Dict[Any, Dict[str, list]] = {}
        for o in outliers:
            ts = o["time"]
            event_dict.setdefault(
                ts, {"log_id": [], "templateId": []})
            event_dict[ts]["log_id"].append(o["log_id"])
            event_dict[ts]["templateId"].append(o["template_id"])
        events = [{"timestamp": ts, "log_id": d["log_id"],
                   "templateId": d["templateId"]}
                  for ts, d in event_dict.items()]
        return outliers, events

    def _to_log_entry(self, entry) -> LogEntry:
        """业务日志条目 -> 检测入口 LogEntry。

        正文取上游挂载的 raw_content（PIPLUP 链路首次解析结果，
        由 pipeline_adapter.attach_first_parse 写入）；缺省为
        空串，不由模板占位符重建（稀疏参数位无法可靠还原原文）。
        """
        template = getattr(entry, "template", None) or ""
        params = getattr(entry, "param_list", None) or []
        raw_text = getattr(entry, "raw_content", None) or ""
        return LogEntry(
            raw_text=raw_text,
            module=getattr(entry, "module", ""),
            template_id=getattr(entry, "template_id", None),
            template_detected=template or None,
            param_list=params,
            log_id=getattr(entry, "log_id", ""),
            timestamp=getattr(entry, "time", 0) or 0)

    # 结果落盘（与旧实现一致）

    def save_results(self, detected_outliers, total_count, output_path):
        """保存检测结果到 JSON 文件。"""
        try:
            results = {
                "detected_anomalies": detected_outliers,
                "total_anomaly_count": total_count,
                "detection_time": datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"),
                "config_path": self.config_path
            }
            output = Path(output_path)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(results, ensure_ascii=False, indent=4),
                encoding="utf-8")
            return True
        except Exception as e:
            print(f"保存结果失败: {str(e)}")
            return False

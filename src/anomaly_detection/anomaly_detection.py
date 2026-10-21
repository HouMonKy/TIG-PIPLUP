#!/usr/bin/python3.7
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2012-2020. All rights reserved.
import json
import pickle
import datetime
from collections import defaultdict
from typing import List
import os

from .parameter_anomaly.param_detector import ParamAnomalyDetector
from .quantitive_anomaly.quantitive_detector import QuantityAnomalyDetector
from .semantic_anomaly.semantic_anomaly import SemanticAnomalyDetector
from ..common.atomic_op import group_by_template_id
from ..data_process.log_context import LogContext
from ..data_process.template_extractor.log_template_repository import LogTemplateRepository, MixedAggrWindow, AggrWindow
from ..data_process.template_extractor.parsing_algorithm import TemplateMiner
from src.evaluation.ParserMetrics import normalize_template

def same_template(t1: str, t2: str) -> bool:
    t1 = normalize_template(t1)
    t2 = normalize_template(t2)
    tokens1 = t1.split()
    tokens2 = t2.split()

    if len(tokens1) != len(tokens2):
        return False
    token_cnt = 0
    for a, b in zip(tokens1, tokens2):
        # 若任一是通配符
        if a == "<*>" or b == "<*>":
            continue

        # 若完全相同
        if a == b:
            token_cnt += 1
            continue

        return False

    if token_cnt < 2:
        return False

    return True

class AnomalyDetection:
    # 类级别的缓存，存放已经初始化好的产品实例
    _product_instances = {}

    @classmethod
    def get_detector(cls, mode="all", product=None):
        """
        顶层工厂方法：外部调用只调用这个
        """
        product_key = "MAE" if product == "MAE" else "DME"
        cache_key = (product_key, mode)

        if cache_key not in cls._product_instances:
            print(f"--- [System Init] 正在初始化 {product_key} (Mode: {mode}) 的检测组件... ---")
            # 这里的初始化会触发对应 mode 的子类加载
            cls._product_instances[cache_key] = cls(mode=mode, product=product_key)

        return cls._product_instances[cache_key]

    def __init__(self, mode="all", product=None):
        self.mode = mode
        self.product = product

        if self.mode == "all" or self.mode == "semantic":
            self._semantic_detector = SemanticAnomalyDetector(case=self.product)
        if self.mode == "all" or self.mode == "quantitative":
            self._quantity_detector = QuantityAnomalyDetector()
        if self.mode == "all" or self.mode == "parametric":
            self._parameter_detector = ParamAnomalyDetector(self.product)

        diagnosis = True  # 是否开启简易诊断
        if diagnosis:
            base_dir = os.path.dirname(os.path.abspath(__file__))  # 获取当前文件所在路径
            self.finger_print_lib_path = os.path.join(base_dir, './semantic_anomaly/configurations/finger_print_lib/finger_print_lib.json')

    def anomaly_detection(self, source: str, module: str, start_time: int, end_time: int, window_size: int,
                      repository: LogTemplateRepository, template_cache: TemplateMiner,
                      white_list, k_sigma=6, module_result_path=None):

        windows = repository.get_aggr_windows(start_time, end_time, window_size, module, source)
        anomaly_data, anomaly_event = self._single_detection(start_time, end_time, module, source, repository, template_cache,
                                                             white_list, window_size, windows, k_sigma=k_sigma,
                                                             module_result_path=module_result_path)
        if self.product == 'MAE':
            return anomaly_data, anomaly_event
        reports = []
        for event in anomaly_event:
            thresh = 0.95
            dt = (
                datetime.datetime.fromtimestamp(event['timestamp'] / 1000))  # 转为秒级并解析
            format_time = dt.strftime("%Y-%m-%d %H:%M:%S")  # 格式化输出
            templates = event["template"]
            templates_unique = list(set(templates))
            with open(self.finger_print_lib_path, "r", encoding="utf-8") as f:
                finger_print = json.load(f)
            for case_name, case_data in finger_print.items():
                match_fingerprints = []
                case_score = 0
                for template in templates_unique:
                    for finger_print in case_data["clues"]:
                        for s, weight in finger_print.items():
                            if same_template(s, template):
                                case_score += weight
                                match_fingerprints.append(template)
                if case_score > thresh:
                    reports.append(
                        {"format_time": format_time, "case_name": case_name, "case_score": case_score,
                         "fault_mode_clue": match_fingerprints, "abn_event": event,
                         "module": module, "source": source})
        return anomaly_data, anomaly_event, reports

    def all_detect(self, start_time: int, end_time: int, window_size: int,
                   repository: LogTemplateRepository, context: LogContext,
                   k_sigma=6):
        semantic_result = self._detect_all_semantic(context, end_time, repository, start_time, window_size)
        quantitative_result = {}
        # quantitative_result = self._detect_all_quantitative(context, end_time, 5, repository, start_time, window_size)
        parametric_result = self._detect_all_parametric(context, end_time, repository, start_time, window_size)
        result = self.merge_batch_result(parametric_result, quantitative_result, semantic_result, context)
        if self.product == 'MAE':
            return result
        with open(self.finger_print_lib_path, "r", encoding="utf-8") as f:
            finger_print_lib = json.load(f)
        for res in result:
            reports = []
            module = res["module"]
            source = res["source"]
            for event in res["detectEvent"]:
                dt = (datetime.datetime.fromtimestamp(event['timestamp']))  # 转为秒级并解析
                format_time = dt.strftime("%Y-%m-%d %H:%M:%S")  # 格式化输出
                templates = event["template"]
                templates_unique = list(set(templates))
                for case_name, case_data in finger_print_lib.items():
                    match_fingerprints = []
                    thresh = case_data["threshold"]
                    case_score = 0
                    for template in templates_unique:
                        for finger_print in case_data["clues"]:
                            for s, weight in finger_print.items():
                                if same_template(s, template):
                                    case_score += weight
                                    match_fingerprints.append(template)
                    if case_score > thresh:
                        reports.append(
                            {"format_time": format_time, "case_name": case_data["caseID"], "case_score": case_score,
                             "fault_mode_clue": match_fingerprints, "abn_event": event,
                             "module": module, "source": source})
                res["reports"] = reports
        return result

    def merge_batch_result(self, parametric_result, quantitative_result, semantic_result, context):
        result = []
        # 将所有检测结果的module 和source 合并，然后根据合并结果遍历，并将相同module 和source的三种结果合并
        merged_keys: dict[str, set[str]] = {}
        for module, value in semantic_result.items():
            if module in merged_keys:
                merged_keys[module].update(value.keys())
            else:
                merged_keys[module] = set(value.keys())
        for module, value in quantitative_result.items():
            if module in merged_keys:
                merged_keys[module].update(value.keys())
            else:
                merged_keys[module] = set(value.keys())
        for module, value in parametric_result.items():
            if module in merged_keys:
                merged_keys[module].update(value.keys())
            else:
                merged_keys[module] = set(value.keys())
        for module, sources in merged_keys.items():
            for source in sources:
                quantitative_data = quantitative_result.get(module, {}).get(source, [])
                semantic_data = semantic_result.get(module, {}).get(source, [])
                parametric_data = parametric_result.get(module, {}).get(source, [])
                datas, event = self.merge_results(semantic_data, parametric_data, quantitative_data)
                template_cache = context.get_drain(module)
                # 参数检测返回的 templateId 是配置人工 ID（如 2012），
                # 与 id_to_cluster 的簇 ID（Drain 簇号 / PIPLUP crc32）
                # 分属两套编号；查不到簇时回退到 outlier 自带模板文本
                param_templates = {
                    (str(o.get("template_id")), str(o.get("log_id"))):
                        o.get("template")
                    for o in parametric_data}
                for ev in event:
                    ev["template"] = [
                        self._cluster_template(template_cache, tid)
                        or param_templates.get((str(tid), str(lid)))
                        for tid, lid in zip(ev["template_id"], ev["log_id"])]
                for data in datas:
                    data["template"] = self._cluster_template(
                        template_cache, data["templateId"]) \
                        or param_templates.get(
                            (str(data["templateId"]), str(data["logId"])))
                result.append({"module": module, "source": source, "detectLine": datas, "detectEvent": event})
        return result

    @staticmethod
    def _cluster_template(template_cache, template_id):
        """按 templateId 查簇模板文本；非数字 ID 或查不到返回 None。"""
        try:
            cluster = template_cache.extractor.id_to_cluster.get(
                int(template_id))
        except (TypeError, ValueError):
            return None
        return cluster.get_template() if cluster is not None else None

    def _detect_all_parametric(self, context, end_time, repository, start_time, window_size) -> dict[str, dict[str, []]]:
        parametric_result: dict[str, dict[str, []]] = {}
        if self.mode == "all" or self.mode == "parametric":
            logs = repository.get_window_logs(start_time, end_time)
            for log in logs:
                if log.template:
                    continue
                drain = context.get_drain(log.module)
                cluster = drain.extractor.id_to_cluster.get(log.template_id)
                if cluster is not None:
                    log.template = cluster.get_template()
            parametric_result = self._parameter_detector.detect_all_module_logs(
                log_entries=logs, window_size=window_size)
        return parametric_result

    def _detect_all_semantic(self, context, end_time, repository, start_time, window_size) -> dict[str, dict[str, []]]:
        semantic_result: dict[str, dict[str, []]] = {}
        if self.mode == "all" or self.mode == "semantic":
            window_map: dict[str, dict[str, MixedAggrWindow]] = repository.group_window_by_module(start_time, end_time,
                                                                                                  window_size)
            for module, module_window_map in window_map.items():
                windows: List[MixedAggrWindow] = list(module_window_map.values())
                template_id_to_windows = group_by_template_id(windows)
                template_cache = context.get_drain(module)
                white_list = context.get_white_list(module)
                one_module_result: dict[str, []] = self._semantic_detector.detect_one_module(template_id_to_windows, module,
                                                                                                   template_cache,
                                                                                                   white_list)
                semantic_result[module] = one_module_result
        return semantic_result

    def _detect_all_quantitative(self, context, end_time, k_sigma, repository, start_time, window_size) -> dict[str, dict[str, []]]:
        quantitative_result: dict[str, dict[str, []]] = {}
        if self.mode == "all" or self.mode == "quantitative":
            all_modules_window_map: dict[str, dict[str, dict[str, AggrWindow]]] = repository.get_all_aggr_windows(
                start_time,
                end_time,
                window_size)
            for module, module_data in all_modules_window_map.items():
                template_cache = context.get_drain(module)
                white_list = context.get_white_list(module)
                one_module_result = {}
                quantitative_result[module] = one_module_result
                for source, source_data in module_data.items():
                    window_map = group_by_template_id(list(source_data.values()))
                    anomaly_data, anomaly_event = self._quantity_detector.detect(source=source,
                                                                                 module=module,
                                                                                 k_sigma=k_sigma,
                                                                                 window_map=window_map,
                                                                                 template_cache=template_cache,
                                                                                 white_list=white_list)
                    one_module_result[source] = anomaly_data
        return quantitative_result

    def _single_detection(self, start_time, end_time, module, source, repository, template_cache, white_list, window_size,
                          windows, k_sigma=6, module_result_path=None):
        window_map = group_by_template_id(windows)
        # semantic
        semantic_data, semantic_event = [], []
        if self.mode == "all" or self.mode == "semantic":
            semantic_data, semantic_event = self._semantic_detector.detect(source=source,
                                                                           module=module,
                                                                           k_sigma=k_sigma,
                                                                           window_map=window_map,
                                                                           template_cache=template_cache,
                                                                           white_list_dict=white_list)
        # quantitative
        quantitative_data, quantitative_event = [], []
        if self.mode == "all" or self.mode == "quantitative":
            quantitative_data, quantitative_event = self._quantity_detector.detect(source=source,
                                                                                   module=module,
                                                                                   k_sigma=k_sigma,
                                                                                   window_map=window_map,
                                                                                   template_cache=template_cache,
                                                                                   white_list=white_list)
        # parameter
        parametric_data, parametric_event, total = [], [], []
        if self.mode == "all" or self.mode == "parametric":
            log_template_parameter = repository.get_window_logs_by_module_and_source(start_time, end_time, module, source, template_cache)
            parametric_data, parametric_event, total = self._parameter_detector.detect(log_entries=log_template_parameter,
                                                                                       window_size=window_size,
                                                                                       output_path=module_result_path)

        anomaly_data, anomaly_event = self.merge_results(semantic_data, parametric_data, quantitative_data)
        for ev in anomaly_event:
            ev["template"] = [template_cache.extractor.id_to_cluster[int(templateId)].get_template() for templateId in ev["template_id"]]
        for data in anomaly_data:
            data["template"] = template_cache.extractor.id_to_cluster.get(int(data["templateId"])).get_template()
        return anomaly_data, anomaly_event

    @staticmethod
    def merge_results(semantic_data, parametric_data, quantitative_data):
        anomaly_data_dict = defaultdict(dict)
        time_format = "%Y-%m-%d %H:%M:%S"
        for data in semantic_data:
            template_id, log_id, timestamp = data["templateId"], data["log_id"], data["timestamp"]
            metadata = {}
            for key, value in data.items():
                if key not in ["templateId", "log_id", "timestamp"]:
                    metadata[key] = value
            anomaly_data_dict[(template_id, log_id, timestamp)]["semantic"] = metadata

        for data in parametric_data:
            template_id, log_id, timestamp = str(data["template_id"]), str(data["log_id"]), data["time"]
            metadata = {}
            for key, value in data.items():
                if key not in ["template_id", "log_id", "time"]:
                    metadata[key] = value
            anomaly_data_dict[(template_id, log_id, timestamp)]["parametric"] = metadata

        for data in quantitative_data:
            template_id, log_id, timestamp = str(data["templateId"]), str(data["log_id"]), data["timestamp"]
            metadata = {}
            for key, value in data.items():
                if key not in ["templateId", "log_id", "timestamp"]:
                    metadata[key] = value
            anomaly_data_dict[(template_id, log_id, timestamp)]["quantitative"] = metadata

        anomaly_data = []
        for key, value in anomaly_data_dict.items():
            anomaly_data.append({
                "template_id": key[0],
                "log_id": key[1],
                "timestamp": key[2],
                "metadata": value
            })
        combined_data = []
        for data in anomaly_data:
            one_data = {"logId":data["log_id"], "timestamp":data["timestamp"], "templateId":data["template_id"]}
            if "parametric" in data["metadata"]:
                one_data["anomalyType"] =["parametric"]
                parameter = data["metadata"]["parametric"]["param_value"]
                one_data["summary"] = [f"日志参数异常,异常参数为 {parameter}"]
            if "quantitative" in data["metadata"]:
                times = data["metadata"]["quantitative"]["log_count"]
                if "anomalyType" in one_data:
                    one_data["anomalyType"].append("quantitative")
                    one_data["summary"].append(f"日志频率异常,当前时间窗内出现{times}次")
                else:
                    one_data["anomalyType"] = ["changeRecord"]
                    one_data["summary"] = [f"日志频率异常,当前时间窗内出现{times}次"]
            if "semantic" in data["metadata"]:
                if "anomalyType" in one_data:
                    one_data["anomalyType"].append("semantic")
                    one_data["summary"].append("日志内容异常请检查")
                else:
                    one_data["anomalyType"] = ["semantic"]
                    one_data["summary"] = ["日志内容异常请检查"]
            combined_data.append(one_data)
        anomaly_event_res = defaultdict(list)
        for data in anomaly_data:
            anomaly_event_res[data["timestamp"]].append({
                    "template_id": data["template_id"],
                    "log_id": data["log_id"],
                    "meta" : data["metadata"]
                }
            )
        anomaly_event = []
        for key, value in anomaly_event_res.items():
            if abs(key) > 1e10:
                key /= 1000.0
            start_time = datetime.datetime.fromtimestamp(key)
            end_time = start_time + datetime.timedelta(minutes=10)
            formatted_start = start_time.strftime(time_format)
            formatted_end = end_time.strftime(time_format)
            summary = f"{formatted_start}-{formatted_end} "
            if len(semantic_data) > 0 or len(parametric_data) > 0 or len(quantitative_data) > 0:
                if len(semantic_data) > 0:
                    semantic_data_in_win = [d for d in value if "semantic" in d["meta"]]
                    summary += f"语义异常检出{len(semantic_data_in_win)}个 "
                if len(parametric_data) > 0:
                    parameter_data_in_win = [d for d in value if "parametric" in d["meta"]]
                    summary += f"参数异常检出{len(parameter_data_in_win)}个 "
                if len(quantitative_data) > 0:
                    quantitative_data_in_win = [d for d in value if "quantitative" in d["meta"]]
                    summary += f"频率异常检出{len(quantitative_data_in_win)}个 "
            else:
                summary += f"未检出异常"
            anomaly_event.append(
                {"timestamp": key,
                 "template_id": [d["template_id"] for d in value],
                 "log_id": [d["log_id"] for d in value],
                 "summary": summary
                 }
            )
        return combined_data, anomaly_event


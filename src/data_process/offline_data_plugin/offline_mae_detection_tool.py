#!/usr/bin/python3.7
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2012-2020. All rights reserved.
import json

from src.anomaly_detection.anomaly_detection import AnomalyDetection
from src.data_process.log_context import LogContext
from src.data_process.offline_data_plugin.mae_data_plugin.mae_data_plugin import MAEDataPlugin
from src.data_process.offline_data_plugin.utils import timestamp_to_window, convert_timestamp
from src.data_process.template_extractor.log_template_parsing_process import parse_logs, parse_logs_no_header
from src.data_process.template_extractor.log_template_repository import LogTemplateRepository
from src.evaluation.metrics import Evaluator
from src.common.global_config import GlobalConfig


class OfflineMAEDetectionTool:
    def __init__(self, mode, product=None, test_data_path=None):
        self._context = LogContext("MAE")
        self._data_plugin = MAEDataPlugin()
        self.mode = mode
        self.data = None
        self.data_len = None
        self.product = product
        self.test_data_path = test_data_path
        self.batch_load_data()

    def batch_load_data(self):
        if self.test_data_path:
            # 如果是有测试数据，就从这里传入结构化数据路径
            self.data = self._data_plugin.read_data_samples(self.test_data_path)
        else:
            # 如果没有就用默认的基线化数据集
            self.data = self._data_plugin.read_data_samples()
        self.data_len = len(self.data)

    def start_detect(self,
                     window_size=60,
                     data_index=0,
                     module_result_path=None):
        if not self.data:
            self.batch_load_data()
            # 如果是有测试数据，就从这里传入结构化数据路径
        # 如果没有就用默认的基线化数据集
        data = self.data[data_index]['logInfo']

        parse_results = parse_logs_no_header(data, self._context)
        print(f"{data[0]['source']} parse_results size: {len(parse_results)}")
        self._context.persistence_drain()

        log_repository: LogTemplateRepository = self._context.log_repository
        # first sample
        first_sample = data[0]
        # last sample
        last_sample = data[-1]
        start_time = first_sample["timestamp"] - window_size // 2
        end_time = last_sample["timestamp"] + window_size // 2
        results = AnomalyDetection.get_detector(mode=self.mode, product=self.product).all_detect(
                                                                  start_time=start_time,
                                                                  end_time=end_time,
                                                                  window_size=window_size,
                                                                  repository=log_repository,
                                                                  context=self._context)
        log_repository.clear()
        event = results[0]["detectEvent"] if results else []
        data_group = "_".join(first_sample["source"].split("_")[:3])
        case_num = int(first_sample["source"].split("_")[-2])

        labels = []
        data_path_root = GlobalConfig().get_dataset_root()
        with open(f"{data_path_root}/CN/raw_data/{data_group}/result_new.jsonl", 'r', encoding="utf-8") as f:
            for line in f:
                res = json.loads(line)
                if res["caseNum"] == case_num:
                    # labels = [int(l.strip()) for l in res["anomalyLines"].lstrip("[").rstrip("]").split(",")]
                    labels = res["anomalyLines"]

        template_ids = []
        for ev in event:
            template_ids.extend(ev["template_id"])

        if not len(labels):
            print(f"skip sample without label：{data_group} - {case_num}")
            # 返回0而不是-1，避免混淆矩阵出现负数
            return 0, 0, 0, 0, len(set(template_ids)), len(data), event, data_group, case_num, labels

        try:
            label = [timestamp_to_window(timestamp=int(data[num-1]['timestamp']), start_time=start_time, end_time=end_time, window_size=window_size) for num in labels]
            label = list(set(label))
            tp, fp, tn, fn = Evaluator(start_time=start_time, end_time=end_time,
                                       window_size=window_size).compute_confusion_matrix([ev["timestamp"] for ev in event], label)

            return tp, fp, tn, fn, len(set(template_ids)), len(data), event, data_group, case_num, labels

        except Exception as e:
            print(e)
            print(labels, len(data), first_sample["source"])
            return 0, 0, 0, 0, len(set(template_ids)), len(data), event, data_group, case_num, labels

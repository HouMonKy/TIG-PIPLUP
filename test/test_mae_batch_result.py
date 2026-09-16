#!/usr/bin/python3.7
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2012-2020. All rights reserved.

import json
import os
import unittest
from datetime import datetime
from time import time
from pathlib import Path

import pandas as pd
from tqdm import tqdm

from src.common.global_config import GlobalConfig, config
from src.data_process.offline_data_plugin.offline_mae_detection_tool import OfflineMAEDetectionTool
from src.data_process.offline_data_plugin.utils import convert_timestamp
from src.evaluation.LogAnalysis import LogAnalysisEvaluator

def mae_batch_result_core(mode=None, test_data_path=None, output=False):
    if not mode:
        mode = "semantic"
    total_start_time = time()
    offline_mae_tool = OfflineMAEDetectionTool(mode=mode, product='MAE', test_data_path=test_data_path)
    total_load_time = time()
    output_path_root = GlobalConfig().get_dataset_root()
    rows = []
    count_logs = 0
    for index, data in tqdm(enumerate(offline_mae_tool.data)):
        start_time = time()
        count_logs += len(data['logInfo'])
        module_result_path = f"{output_path_root}/CN/result/{mode}/detect_raw/{data['logInfo'][0]['source']}.json"
        tp, fp, tn, fn, num_anom_templates, num_input_logs, event, data_group, case_num, labels = offline_mae_tool.start_detect(
            window_size=600000, data_index=index, module_result_path=module_result_path)
        rows.append({
            "case": case_num,
            "data_group": data_group,
            "tp": tp,
            "fp": fp,
            "tn": tn,
            "fn": fn,
            "num_anomaly_templates": num_anom_templates,
            "num_input_logs": num_input_logs,
        })
        end_time = time()
        print(f"{index}th sample - {data_group} - {case_num} - processed {len(data['logInfo'])} logs in {end_time - start_time}s >>> Performance {len(data['logInfo'])/(end_time - start_time)} logs/sec <<<\n\n")
        if output:
            file_path = f"{output_path_root}/CN/result/{mode}/detect_label/{data['logInfo'][0]['source']}.json"
            Path(file_path).parent.mkdir(parents=True, exist_ok=True)
            # add format_time dimension
            for single_event in event:
                single_event["format_time"] = convert_timestamp(single_event["timestamp"])
                log_label = []
                for single_log_id in single_event["log_id"]:
                    line_num = int(single_log_id.split("_")[-1]) + 1
                    if line_num in labels:
                        log_label.append("tp")
                    else:
                        log_label.append("fp")
                single_event["log_label"] = log_label
            with open(file_path, 'w') as f:
                json.dump(event, f, indent=4, sort_keys=True, default=str)
    if output:
        df = pd.DataFrame(rows)
        now = str(datetime.now().strftime("%m%d"))
        df.to_csv(f"{output_path_root}/CN/result/{mode}/mae_detection_result_dataset_{now}.csv", index=False)
    total_end_time = time()
    print(f"--------------  程序总运行时间: {total_end_time - total_start_time} 秒  --------------")
    print(f"-->  原始离线数据读取耗时: {total_load_time - total_start_time} 秒")
    print(f"-->  数据解析和异常检测耗时: {total_end_time - total_load_time} 秒")
    print(f"-->  数据解析和异常检测性能: {count_logs / (total_end_time - total_load_time)} 条/秒")
    return None


class TestMAEBatchResult(unittest.TestCase):
    def test_mae_batch_result_semantic(self):
        self.assertIsNone(mae_batch_result_core(mode="semantic", output=True))

    def test_mae_batch_result_parametric(self):
        self.assertIsNone(mae_batch_result_core(mode="parametric", output=True))

    def test_mae_batch_result_quantitative(self):
        self.assertIsNone(mae_batch_result_core(mode="quantitative", output=True))

    def test_mae_batch_result_all(self):
        self.assertIsNone(mae_batch_result_core(mode="all", output=True))

    def test_mae_batch_result_functional(self):
        test_dir = os.path.join(config.get_dataset_root(), 'CN', 'test_data', "processed.json")
        self.assertIsNone(mae_batch_result_core(mode="all", test_data_path=test_dir))

    def test_result_in_different_levels(self):
        """ 统计event、template、log三种level下的效果 """
        dataset_dir = config.get_dataset_root()
        date = None # 如果不指定日期，默认使用最新日期, 也可以指定日期如 "1024"
        mode = "all" # 或者指定模块 e.g."semantic", "quantitative", "parametric"
        skip_304case = True
        self.assertIsNone(LogAnalysisEvaluator(dataset_dir, mode, date, skip_304case).evaluate())

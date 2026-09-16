#!/usr/bin/python3.7
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2012-2020. All rights reserved.


import os
import json

from tqdm import tqdm

from src.common.global_config import GlobalConfig
from src.data_process.offline_data_plugin.mae_data_plugin.mae_log_parser import MaeLogParser

config = GlobalConfig()


class MAEDataPlugin:

    def __init__(self):
        # 配置固定
        self._raw_data_dir = os.path.join(config.get_dataset_root(), 'CN', 'raw_data')
        self._struct_data_dir = os.path.join(config.get_dataset_root(), 'CN', 'result', "extract_from_raw.json")
        self._processed_data_dir = os.path.join(config.get_dataset_root(), 'CN', 'result', "processed.json")
        self._tag_file_name = 'result.jsonl'
        self._output_log_path = 'output_logs'
        self._log_parser = MaeLogParser()

    def parse_log_file(self, data_group_path):
        samples = []
        result_path = os.path.join(data_group_path, self._tag_file_name)

        with open(result_path, 'r', encoding='utf-8') as f:
            result_data = [json.loads(line) for line in f]

        output_logs_path = os.path.join(data_group_path, 'output_logs')
        for log_file in os.listdir(output_logs_path):
            if not log_file.endswith('.log'):
                continue

            # 根据文件名，hardcode解析
            parts = log_file[:-4].split('_')
            module = '_'.join(parts[0:1])
            # eo -> eo_sel
            if module == "eo":
                module = "sel"
            # 构造source ID,暂时:{data_group}_{log_file}
            data_group_name = os.path.basename(data_group_path)
            source_id = f"{data_group_name}_{log_file.replace('.log', '')}"
            # 读取日志内容,后面写到json里
            with open(os.path.join(output_logs_path, log_file), 'r', encoding='utf-8') as f:
                log_lines = [line.rstrip('\\n') for line in f]

            # 匹配result数据
            for item in result_data:
                # 原来的字段是： outputPath/{log_file}
                # 对应一下文件
                if item["outputPath"][12:] == log_file:
                    sample = {
                        'source': source_id,
                        'module': module,
                        'log': [line for i, line in enumerate(log_lines)],
                        'anomalyLines': item['anomalyLines'],
                        'case': f"{item['deviceType']}_{item['faultObject']}"
                    }
                    samples.append(sample)
                    break

        return samples

    def batch_process_raw_log(self):
        all_samples = []
        for data_group in os.listdir(self._raw_data_dir):
            # if data_group.startswith('data_group_1'):
            data_group_path = os.path.join(self._raw_data_dir, data_group)
            all_samples.extend(self.parse_log_file(data_group_path))

        with open(self._struct_data_dir, 'w', encoding='utf-8') as f:
            json.dump({'logSamples': all_samples}, f, indent=2, ensure_ascii=False)

    def generate_data_sample(self):

        with open(self._struct_data_dir, 'r', encoding='utf-8') as f:
            struct_data = json.load(f)
        data_samples = []
        # 鲁棒性处理，填充问题样本
        history_timestamp = None
        history_log = None
        for each_sample in struct_data["logSamples"]:
            single_sample_log = {
                "logInfo": []
            }
            module = each_sample.get('module')
            logs = each_sample.get('log')
            source = each_sample.get('source')
            for index, log_line in tqdm(enumerate(logs)):
                result = self._log_parser.parse(module, log_line)
                if not (result and result.get('Content')):
                    if history_log:
                        result = history_log
                    result["Content"] = history_log["rawLog"]
                elif 'timestamp' not in result:
                    result['timestamp'] = history_timestamp
                single_logInfo = {
                    "logId": source + "_" + str(index),
                    "source": source,
                    "timestamp": result.get('timestamp'),
                    "rawLog": result.get('Content'),
                    "module": module
                }
                # 重置历史记录
                history_log = single_logInfo
                history_timestamp = result.get('timestamp')
                single_sample_log.get("logInfo").append(single_logInfo)
            if len(single_sample_log.get("logInfo")) == 0:
                continue
            data_samples.append(single_sample_log)

        with open(self._processed_data_dir, 'w', encoding='utf-8') as f:
            json.dump(data_samples, f, indent=4, ensure_ascii=False)
        return struct_data

    def read_data_samples(self, data_dir=None):
        if data_dir:
            # 用于读取测试数据
            with open(data_dir, 'r', encoding='utf-8') as f:
                struct_data = json.load(f)
            return struct_data
        # 检测是否已经提取了结构化参数
        if not os.path.exists(self._struct_data_dir):
            print("no structured data, need to regenerate")
            self.batch_process_raw_log()

        if not os.path.exists(self._processed_data_dir):
            print("data not exist, need to reprocess")
            self.generate_data_sample()

        with open(self._processed_data_dir, 'r', encoding='utf-8') as f:
            struct_data = json.load(f)
        return struct_data

#!/usr/bin/python3.7
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2012-2020. All rights reserved.

"""
功能说明：
    本脚本用于 MAE-CN 数据预处理场景
    从原始日志数据中提取并增强日志条目信息，生成增强日志JSON文件。
    主要流程包括：
    1. 从指定路径读取日志条目（tag）信息。
    2. 对日志条目进行增强处理，添加模板（template）和参数列表（param list）信息。
    3. 按CASE对日志条目进行分组。
    4. 使用离线工具对每个CASE进行处理。
    5. 将最终增强后的日志条目保存为JSON文件，供后续配置生成使用。

输入：
    - dataset/raw_data/ 目录下的原始日志数据。
    - dataset/result/ 目录下的日志条目（tag）信息。

输出：
    - src/anomaly_detection/parameter_anomaly/configuration/enhanced_log_entries_mae.json
      增强后的日志条目JSON文件，包含模板和参数信息。

依赖模块：
    - os：用于处理文件路径。
    - GlobalConfig：用于获取项目根目录路径。
    - recursive_search：用于递归搜索日志条目。
    - EnhancedLogEntry：用于创建增强日志条目对象。
    - LogTemplateProcessor：用于处理日志模板。
    - OffLineTool：用于执行离线处理逻辑。
    - save_enhanced_entries_to_json：用于保存增强后的日志条目为JSON文件。

执行方式：
    直接运行该脚本即可完成日志条目的增强处理和JSON文件的生成。
"""
import json
import os
import pickle
from src.common.global_config import GlobalConfig
from src.data_process.offline_data_plugin.mae_data_plugin.log_entry import EnhancedLogEntry
from src.data_process.offline_data_plugin.offline_tool import LogTemplateProcessor, \
    save_enhanced_entries_to_json_mae, recursive_search_mae, OffLineTool

root_dir = GlobalConfig().get_project_root()
result_path = os.path.join(root_dir, 'dataset', 'CN', '0813')
raw_data_dir = os.path.join(root_dir, 'dataset', 'CN', '0813')

# # 1. 读取tag，获取基本tag信息
# logEntry = recursive_search_mae(result_path)
#
# # 2. 增强tag，增加template和param list信息
# all_entries = logEntry.get_entries()
# enhanced_entries = []
# for entry in all_entries:
#     enhanced_entry = EnhancedLogEntry(
#         case=entry.case,
#         module=entry.module,
#         file_name=entry.file_name,
#         file_path=entry.file_path,
#         line_content=entry.line_content
#     )
#     enhanced_entries.append(enhanced_entry)
#
# # 按CASE分组
# case_groups = {}
# for entry in enhanced_entries:
#     if entry.case not in case_groups:
#         case_groups[entry.case] = []
#     case_groups[entry.case].append(entry)
#
# tool = OffLineTool()
# tool.init_log_format('MAE')
# processor = LogTemplateProcessor(raw_data_dir, tool)
#
# # 处理每个CASE组
# for case, entries in case_groups.items():
#     print(f"开始处理CASE: {case}")
#     success = processor.process_case_mae(case, entries)
#     print(f"处理结果: {'成功' if success else '失败'}")

output_json_path = os.path.join(root_dir, 'src', 'anomaly_detection', 'parameter_anomaly', 'configuration', 'enhanced_log_entries_mae.json')
output_pkl_path = os.path.join(root_dir, 'src', 'anomaly_detection', 'parameter_anomaly', 'configuration', 'enhanced_log_entries_mae_debug.pkl')
# with open(output_pkl_path, 'wb') as f:
#     pickle.dump(enhanced_entries, f)

with open(output_pkl_path, 'rb') as f:
    enhanced_entries = pickle.load(f)

save_success = save_enhanced_entries_to_json_mae(enhanced_entries, output_json_path)

if save_success:
    print("保存成功")
else:
    print("保存失败")
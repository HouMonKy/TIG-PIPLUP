#!/usr/bin/python3.7
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2012-2020. All rights reserved.

"""
功能说明：
    本脚本用于 MAE-CN 参数配置文件构建
    从增强日志JSON文件中生成配置文件（config_build.json）。
    它读取指定路径下的增强日志JSON文件，通过调用工具函数进行处理，
    最终将生成的配置写入到指定的输出文件中。

输入：
    enhanced_log_entries.json - 增强日志格式的JSON文件，包含用于生成配置的原始数据。

输出：
    config_build.json - 生成的配置文件，用于后续的参数异常检测流程。

依赖模块：
    - os：用于处理文件路径。
    - GlobalConfig：用于获取项目根目录路径。
    - generate_config_from_enhanced_json：用于将增强日志转换为配置文件的工具函数。

执行方式：
    直接运行该脚本即可完成配置文件的生成。
"""

import os

from src.common.global_config import GlobalConfig
from src.data_process.offline_data_plugin.offline_tool import generate_config_from_enhanced_json_mae

# 输入文件路径（增强日志JSON）
config = GlobalConfig()
enhanced_json_path = os.path.join(config.get_project_root(), 'src', 'anomaly_detection', 'parameter_anomaly',
                                  'configuration', 'enhanced_log_entries_mae_checked.json')

# 输出文件路径（配置JSON）
config_json_path = os.path.join(config.get_project_root(), 'src', 'anomaly_detection', 'parameter_anomaly',
                                'configuration', 'config_build_mae_checked.json')

# 生成配置
generate_config_from_enhanced_json_mae(enhanced_json_path, config_json_path)

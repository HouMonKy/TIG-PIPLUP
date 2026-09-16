#!/usr/bin/python3.7
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2012-2020. All rights reserved.


import json
import os
import re
from datetime import datetime
from typing import List, Dict, Any, Optional
from collections import defaultdict
from sklearn.ensemble import IsolationForest
from src.anomaly_detection.parameter_anomaly.configuration.param_config import FUZZY_MATCH_ALPHA, SPECIAL_TOKENS
from src.common.atomic_op import get_content_as_tokens
from src.common.global_config import GlobalConfig
from src.data_process.template_extractor.log_template_repository import LogTemplateRelation


class ParamAnomalyDetector:
    def __init__(self, product: str):
        """
        参数异常检测器初始化

        参数:
            product: 产品名称，如 'MAE' 或 'DME'
            config_path: 配置文件路径 (config_build.json)，不同产品有不同的config_build.json文件
        """
        self.product = product
        self.config_path = self._set_config_path()
        self.config_data = self._load_config()
        # 预处理配置数据为字典，加速查找
        self.template_config_dict = {}
        for item in self.config_data:
            key = (item.get('module', ''), item.get('template_id', ''))
            self.template_config_dict[key] = item

    def _set_config_path(self):
        """
                根据产品名称动态设置配置文件路径
                """
        config = GlobalConfig()
        project_root = config.get_project_root()
        config_dir = os.path.join(project_root, 'src', 'anomaly_detection', 'parameter_anomaly', 'configuration')

        # 根据产品名称选择配置文件
        if self.product == 'MAE':
            config_file = 'config_build_mae.json'
        elif self.product == 'DME':
            config_file = 'config_build_dme.json'
        else:
            raise ValueError(f"不支持的产品: {self.product}")

        return os.path.join(config_dir, config_file)

    def _load_config(self):
        """加载配置文件"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"加载配置文件失败: {str(e)}")
            raise

    def _config_fuzzy_match(self, template_detected, module_detected, alpha=FUZZY_MATCH_ALPHA):
        """
        根据相似度阈值判断str_new是否与str_raw匹配
        参数:
        template_detected (str): 待匹配的字符串
        module_detected (str): 待匹配的模块
        alpha (float): 相似度阈值（0到1之间）, 默认为 0.6
        返回:
        bool: 是否匹配
        """
        # 特殊标识符集合
        special_tokens = SPECIAL_TOKENS

        for item in self.config_data:
            if item.get('module') != module_detected:
                continue
            else:
                template_config = item.get('template')
                # 按空格分割字符串
                raw_parts = template_config.split()
                new_parts = template_detected.split()
                # 长度必须一致，Drain抽取模板长度不会变
                if len(raw_parts) != len(new_parts):
                    continue
                total = len(raw_parts)
                # 异常处理，如果空字符串
                if total == 0:
                    continue
                match_count = 0
                for r_part, n_part in zip(raw_parts, new_parts):
                    # 处理特殊标识符位置
                    if r_part in special_tokens:
                        if n_part == r_part:
                            match_count += 1
                    else:
                        # 允许保留原样或替换为<*>
                        if n_part == r_part or n_part == '<*>' or r_part == '<*>':
                            match_count += 1
                # 计算相似度
                similarity = match_count / total
                if similarity >= alpha:
                    return item
        return None

    def _get_param_value(self, log_content, param_position):
        # 预处理与 log_template_parsing_process.py 的 parse_logs 保持一致
        split_result = re.split(r'([:=,\[\]\(\)])', log_content)
        split_content = log_content.strip()
        if len(split_result) > 1:
            split_content = ' '.join(split_result)

        content_tokens = get_content_as_tokens(split_content)
        # 确保位置在有效范围内
        if param_position < 0 or param_position >= len(content_tokens):
            return False, None

        param_value = content_tokens[param_position]
        return True, param_value

    def detect_all_module_logs(self, log_entries: List[LogTemplateRelation], window_size: int) -> dict[str, dict[str,[]]]:
        """
        给定一组包含了多个module和source的日志数据，输出每个module 和source的检测结果
        Args:
            log_entries: 包含了多个module和source的日志数据
            window_size: 时间窗大小，以ms为单位
            output_path: 结果输出文件路径
        Returns:
            {"module": { "source": []} } 数组为data
        """
        result = defaultdict(lambda: defaultdict(list))

        try:
            for entry in log_entries:
                log_id = getattr(entry, 'log_id', '')
                template_id = getattr(entry, 'template_id', '')
                template = getattr(entry, 'template', '')
                module = getattr(entry, 'module', '')
                source = getattr(entry, 'source', '')
                param_list = getattr(entry, 'param_list', [])
                log_timestamp = getattr(entry, 'time', 0)

                # 跳过无需检测的条目
                if not (template_id and module and source and param_list):
                    continue

                # 重新计算，每条日志时间改为窗口起始时间
                timestamp = log_timestamp - log_timestamp % window_size

                # 快速获取对应模板的配置，一条模板存在多个被检测参数情况
                config_key = (module, template_id)
                template_config = self.template_config_dict.get(config_key, {})
                if not template_config:
                    continue

                # 将参数列表转为字典加速查找
                param_dict = {str(p.template_pos): p.template_param for p in param_list}

                # 检测每个参数位置的异常
                for param_config in template_config.get('params', []):
                    try:
                        position = param_config.get('param_pos', '')
                        detect_mode = param_config.get('detect_mode', '')
                        detect_detail = param_config.get('detect_detail', [])
                        # 获取参数值
                        param_value = param_dict.get(position)
                        if not param_value:
                            continue
                        # 检测异常
                        if self._is_anomaly(param_value, detect_mode, detect_detail):
                            # 构建异常记录（不包含time和log_id字段）
                            outlier = {
                                "time": timestamp,
                                "log_id": log_id,
                                "template_id": template_id,
                                "template": template,
                                "param_value": param_value,
                                "param_position": position,
                                "detect_mode": detect_mode,
                                "mode_detail": detect_detail
                            }
                            # 将结果按模块和源分类存储
                            result[module][source].append(outlier)

                    except Exception as e:
                        print(f"处理参数配置时出错: {str(e)}")

            return {module_key: dict(source_dict) for module_key, source_dict in result.items()}

        except Exception as e:
            print(f"参数异常检测失败: {str(e)}")
            return {}

    def detect(self, log_entries, window_size, output_path=None) :
        """
        检测日志条目中的异常参数

        参数:
            log_entries: EnhancedLogEntry对象列表
            output_path: 输出结果文件路径 (可选)

        返回:
            (detected_outliers, detected_event, total_anomaly_count) 元组
        """
        detected_outliers = []
        total_anomaly_count = 0

        try:
            # print(f"开始异常检测，共 {len(log_entries)} 条日志")

            for entry in log_entries:
                log_id = getattr(entry, 'log_id', '')
                template_id = getattr(entry, 'template_id', 0)
                template = getattr(entry, 'template', '')
                module = getattr(entry, 'module', '')
                param_list = getattr(entry, 'param_list', [])
                log_timestamp = getattr(entry, 'time', 0)

                # 跳过无需检测的条目
                if not template or not module or not param_list:
                    continue

                # 重新计算，每条日志时间改为窗口起始时间
                timestamp = log_timestamp - log_timestamp % window_size

                # 获取对应模板的配置，一条模板存在多个被检测参数情况
                template_config = {}
                for item in self.config_data:
                    if item.get('template_id', '') == template_id and item.get('module', '') == module:
                        template_config = item
                        break

                if not template_config:
                    continue

                # 检测每个参数位置的异常
                for param_config in template_config.get('params', []):
                    try:
                        position = param_config.get('param_pos', '')
                        detect_mode = param_config.get('detect_mode', '')
                        detect_detail = param_config.get('detect_detail', [])

                        # 获取参数值
                        param_value = None
                        for item in param_list:
                            if item.template_pos == position:
                                param_value = item.template_param
                                break

                        if not param_value:
                            continue
                        # 检测异常
                        if self._is_anomaly(param_value, detect_mode, detect_detail):
                            total_anomaly_count += 1
                            detected_outliers.append({
                                "time": timestamp,
                                "log_id": log_id,
                                "template_id": template_id,
                                "template": template,
                                "param_value": param_value,
                                "param_position": position,
                                "detect_mode": detect_mode,
                                "mode_detail": detect_detail
                            })
                    except Exception as e:
                        print(f"处理参数配置时出错: {str(e)}")

            # print(f"检测完成，发现 {total_anomaly_count} 个异常参数")

            # 保存结果（如果指定了输出路径）
            if output_path and total_anomaly_count > 0:
                self.save_results(detected_outliers, total_anomaly_count, output_path)

            # 新增：生成detected_event
            event_dict = {}
            for entry in detected_outliers:
                ts = entry['time']
                log_id = entry['log_id']
                template_id = entry['template_id']

                if ts not in event_dict:
                    event_dict[ts] = {'log_id': [], 'templateId': []}
                event_dict[ts]['log_id'].append(log_id)
                event_dict[ts]['templateId'].append(template_id)

            detected_events = []
            for ts, data in event_dict.items():
                detected_events.append({
                    'timestamp': ts,
                    'log_id': data['log_id'],
                    'templateId': data['templateId']
                })

            return detected_outliers, detected_events, total_anomaly_count

        except Exception as e:
            print(f"参数异常检测失败: {str(e)}")
            return [], 0

    def _is_anomaly(self, param_value, detect_mode, detect_detail):
        """检查参数值是否异常"""
        if detect_mode == "black_list":
            return param_value.rstrip('!.:_-?*@') in detect_detail
        elif detect_mode == "white_list":
            return param_value not in detect_detail
        elif detect_mode == "normal_range":
            if not isinstance(detect_detail, dict):
                return False
            lower = detect_detail.get('lower', float('-inf'))
            upper = detect_detail.get('upper', float('inf'))
            try:
                num_value = float(param_value)
                return not (lower <= num_value <= upper)
            except ValueError:
                return True
        else:
            try:
                # 尝试数值转换
                num_value = float(param_value)
                values = [num_value] * 10  # 创建样本集
                clf = IsolationForest(contamination=0.1)
                clf.fit([[v] for v in values])
                prediction = clf.predict([[num_value]])[0]
                return prediction == -1
            except Exception as e:
                print(f"参数异常检测失败: {str(e)}")
                return False

    def save_results(self, detected_outliers, total_count, output_path):
        """保存检测结果到JSON文件"""
        try:
            # 创建结果字典
            results = {
                "detected_anomalies": detected_outliers,
                "total_anomaly_count": total_count,
                "detection_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "config_path": self.config_path
            }

            # 创建输出目录（如果不存在）
            os.makedirs(os.path.dirname(output_path), exist_ok=True)

            # 写入文件
            with open(output_path, 'w', encoding='utf-8') as jsonfile:
                json.dump(results, jsonfile, ensure_ascii=False, indent=4)

            # print(f"检测结果已保存至: {output_path}")
            return True
        except Exception as e:
            print(f"保存结果失败: {str(e)}")
            return False
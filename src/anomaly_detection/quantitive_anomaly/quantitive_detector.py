#!/usr/bin/python3.7
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2012-2020. All rights reserved.
import math
from collections import OrderedDict
from datetime import datetime

import numpy as np
import pandas as pd
from scipy.stats import norm

from src.data_process.template_extractor.parsing_algorithm import TemplateMiner


class QuantityAnomalyDetector:

    def __init__(self):
        pass

    @staticmethod
    def convert_window_map_to_input(window_map):
        node_data = []
        for template_id, windows in window_map.items():
            # 提取时间戳和事件数，并组合成元组
            time_event_pairs = [(window.timestamp, window.count) for window in windows]
            
            # 按时间戳排序
            time_event_pairs.sort(key=lambda x: x[0])
            
            # 拆分为排序后的 time 和 event 列表
            window_begins = [pair[0] for pair in time_event_pairs]
            template_count_arr = [pair[1] for pair in time_event_pairs]
            
            # 提取 log_id，取 window 中首次出现的 log
            log_id = [window.logs[0] if window.logs else None for window in windows]
            
            node_data.append({
                "template_id": template_id,
                "time": window_begins,
                "event": template_count_arr,
                "log_id": log_id
            })
        return node_data

    def find_new_templates_for_node(self, node_data, white_list=None):
        total_template_count = 0
        new_templates = []
        new_templates_first_occurrence = []
        new_templates_count = []
        new_log_ids = []

        for record in node_data:
            total_template_count += 1
            if record["template_id"] in white_list:
                continue
            # 查找新出现模板
            current_data = {
                "template_id": record['template_id'],
                "time": record['time'],
                "event": record['event'],
                "log_id": record["log_id"]
            }
            time_interval = pd.to_timedelta('30min')
            previous_time_interval = int(time_interval.total_seconds() * 1000)
            previous_data = []
            new_template_id, new_template_time, new_template_count, new_template_log_id = self.find_new_templates_based_on_seq(
                previous_data, current_data, previous_time_interval, white_list)
            if new_template_id:
                new_templates.extend(new_template_id)
                new_templates_first_occurrence.extend(new_template_time)
                new_templates_count.extend(new_template_count)
                new_log_ids.extend(new_template_log_id)
                continue
        result_dict = {
            'template_id': new_templates,
            'FirstOccurrenceTime': new_templates_first_occurrence,
            'CountInQueryPeriod': new_templates_count,
            'TotalTemplateCount': total_template_count,
            'log_id': new_log_ids
        }

        if result_dict['template_id']:
            result_dict = self.assign_scores(result_dict, 'CountInQueryPeriod', 99, 50)
        else:
            result_dict = {}

        return result_dict

    def find_change_templates_for_node(self, node_data, new_template, k_sigma):
        total_template_count = 0
        change_templates = []
        change_templates_time = []
        percentage_changes = []
        change_log_ids = []
        window_count_arr = []
        if not new_template:
            new_template["template_id"] = {}
        for record in node_data:
            total_template_count += 1
            current_data = {
                "template_id": record['template_id'],
                "time": record['time'],
                "event": record['event'],
                "log_id": record["log_id"]
            }
            previous_mean, previous_std = self.calculate_mean_std(current_data["event"])
            if previous_std == 0:
                continue
            time_interval = pd.to_timedelta('30min')
            previous_time_interval = int(time_interval.total_seconds() * 1000)
            change_id, change_index, change_list, log_ids, count_arr = self.find_change_templates_based_on_sigma(
                current_data,
                previous_time_interval,
                k_sigma=k_sigma)
            if change_index:
                change_templates.extend(change_id)
                change_templates_time.extend(change_index)
                percentage_changes.extend(change_list)
                change_log_ids.extend(log_ids)
                window_count_arr.extend(count_arr)
                continue
        result_dict = {
            'template_id': change_templates,
            'FirstOccurrenceTime': change_templates_time,
            'percentage_change': percentage_changes,
            'TotalTemplateCount': total_template_count,
            'log_id': change_log_ids,
            'window_counts': window_count_arr
        }
        if result_dict["template_id"]:
            result_dict = self.assign_scores(result_dict, 'percentage_change', 49, 1)
        else:
            result_dict = {}
        return result_dict

    @staticmethod
    def assign_scores(result_dict, order_key, max_score, min_score):
        score_range = max_score - min_score
        mean_score = (max_score + min_score) / 2
        std_dev = score_range / 6  # 6 标准差约覆盖 99.7% 的数据

        # 用于存储最终分数的列表
        scores = [0] * len(result_dict['template_id'])

        # 分组数据
        grouped_data = {}
        for i in range(len(result_dict['template_id'])):
            time = result_dict['FirstOccurrenceTime'][i]
            if time not in grouped_data:
                grouped_data[time] = []
            grouped_data[time].append((result_dict[order_key][i], i))

        # 对每组数据进行排序并分配分数
        for time, group in grouped_data.items():
            # 按 CountInQueryPeriod 从低到高排序
            group.sort(reverse=False, key=lambda x: abs(x[0]))
            num_templates = len(group)
            # 为每个组生成分数
            ranks = np.arange(1, num_templates + 1)
            group_scores = norm.ppf((ranks - 0.5) / num_templates) * std_dev + mean_score
            group_scores = np.clip(group_scores, min_score, max_score).round().astype(int).tolist()
            # 分配分数
            for idx, (count, original_index) in enumerate(group):
                scores[original_index] = group_scores[idx]

        # 将分数添加到 result_dict
        result_dict['Scores'] = scores
        return result_dict

    @staticmethod
    def find_new_templates_based_on_seq(previous_data, current_data, previous_time_interval, white_list={}):
        prev_data_seq = previous_data['event'] if previous_data else []
        cur_data_seq = current_data['event']
        id = current_data['template_id']
        log_ids = current_data['log_id']
        all_id_results = []
        all_times = []
        all_counts = []
        all_log_id = []
        last_time = 0
        if sum(prev_data_seq) == 0 and cur_data_seq and str(id) not in white_list:
            for i in range(len(cur_data_seq)):
                if cur_data_seq[i] > 0:
                    if current_data['time'][i] > last_time + previous_time_interval:
                        all_id_results.append(id)
                        all_log_id.append(log_ids[i])
                        all_times.append(current_data['time'][i])
                        all_counts.append(cur_data_seq[i])
                    last_time = current_data['time'][i]
                    return all_id_results, all_times, all_counts, all_log_id
        return [], 0, 0, []

    @staticmethod
    def calculate_mean_std(non_zero_list):
        return np.mean(non_zero_list), np.std(non_zero_list)

    @staticmethod
    def find_change_templates_based_on_sigma(current_data, time_interval, k_sigma=3):
        id = []
        cur_data_seq = current_data['event']
        all_time = []
        precentage_change_list = []
        previous_time = 0
        new_log_ids = []
        window_count_arr = []
        arr = np.array(cur_data_seq)
        sum_all = np.sum(arr)
        sum_of_squares_all = np.sum(arr ** 2)
        n = len(cur_data_seq)

        if n <= 2:
            return None, None, None, None, None

        for i in range(n):
            sum_without_x = sum_all - cur_data_seq[i]
            mean = sum_without_x / (n - 1)
            sum_of_squares_without_x = sum_of_squares_all - cur_data_seq[i] ** 2
            variance = (sum_of_squares_without_x - (sum_without_x ** 2 / (n - 1))) / (n - 1)
            std = math.sqrt(variance) if variance >= 0 else 0

            if abs(cur_data_seq[i] - mean) > k_sigma * std and abs(cur_data_seq[i] - mean) > 10:
                if current_data['time'][i] > previous_time + time_interval:
                    precentage_change = ((cur_data_seq[i] - mean) / mean) * 100
                    id.append(current_data['template_id'])
                    all_time.append(current_data['time'][i])
                    precentage_change_list.append(precentage_change)
                    new_log_ids.append(current_data['log_id'][i])
                    window_count_arr.append(cur_data_seq[i])
                previous_time = current_data['time'][i]
        return id, all_time, precentage_change_list, new_log_ids, window_count_arr

    @staticmethod
    def summarize_and_generate_summary(new_template_dict, change_template_dict, related_source="OS"):
        """
        汇总模板变化信息并生成摘要数据。

        :param new_template_dict: 包含新模板数据的字典
        :param change_template_dict: 包含模板变化记录的字典
        :param node_id: 节点 ID
        :param related_source: 相关源
        :param window_size: 时间窗口大小
        :return: 转换后的摘要数据列表
        """

        total_template_count = 0
        new_template_dict = {}
        # 数量检测不检测新增模板，新增模板由语义检测功能代替，因此此处赋空
        if 'FirstOccurrenceTime' in new_template_dict:
            total_template_count = new_template_dict.get('TotalTemplateCount', 0)
        if 'FirstOccurrenceTime' in change_template_dict:
            if total_template_count == 0:
                total_template_count = change_template_dict.get('TotalTemplateCount', 0)

        result = []

        # 获取 new_templates 和 changes 中时间的并集
        new_times = set(new_template_dict[
                            'FirstOccurrenceTime']) if new_template_dict and 'FirstOccurrenceTime' in new_template_dict else set()
        change_times = set(change_template_dict[
                               'FirstOccurrenceTime']) if change_template_dict and 'FirstOccurrenceTime' in change_template_dict else set()
        time_points = new_times.union(change_times)

        for time_point in time_points:
            new_templates_at_time = set(zip(
                [tid for tid, t in zip(new_template_dict['template_id'], new_template_dict['FirstOccurrenceTime'])
                 if t == time_point],
                [s for s, t in zip(new_template_dict['Scores'], new_template_dict['FirstOccurrenceTime']) if
                 t == time_point],
                [k for k, t in zip(new_template_dict['log_id'], new_template_dict['FirstOccurrenceTime']) if
                 t == time_point],
                [0] * len(time_points)
            )) if new_template_dict and 'FirstOccurrenceTime' in new_template_dict else set()
            change_records_at_time = list(zip(
                [tid for tid, t in
                 zip(change_template_dict['template_id'], change_template_dict['FirstOccurrenceTime']) if
                 t == time_point],
                [s for s, t in zip(change_template_dict['Scores'], change_template_dict['FirstOccurrenceTime']) if
                 t == time_point],
                [k for k, t in zip(change_template_dict['log_id'], change_template_dict['FirstOccurrenceTime']) if
                 t == time_point],
                [k for k, t in zip(change_template_dict['window_counts'], change_template_dict['FirstOccurrenceTime']) if
                 t == time_point],
            )) if change_template_dict and 'FirstOccurrenceTime' in change_template_dict else []

            templates_scores = list(new_templates_at_time) + list(change_records_at_time)
            templates_scores_dict = OrderedDict()
            for template, score, log_id, log_count in templates_scores:
                templates_scores_dict[template] = {'score': score, 'log_id': log_id, 'log_count': log_count}

            result.append({
                'Time': time_point,
                'TemplatesScores': templates_scores_dict,
                'NewTemplatesSet': new_templates_at_time,
                'NewTemplatesCount': len(new_templates_at_time),
                'ChangeRecordsSet': change_records_at_time,
                'ChangeRecordsCount': len(change_records_at_time),
                'TotalTemplateCount': total_template_count
            })
        # 生成最终的摘要数据
        summary_result = []
        for entry in result:
            timestamp = entry['Time']
            new_templates_count = entry['NewTemplatesCount']
            change_templates_count = entry['ChangeRecordsCount']
            templates_scores = entry['TemplatesScores']
            total_counts = entry['TotalTemplateCount']
            
            for template_id, info in templates_scores.items():
                summary_result.append({
                    "timestamp": timestamp,
                    'TotalTemplate': total_counts,
                    "NewTemplatesCount": new_templates_count,
                    "ChangeRecordsCount": change_templates_count,
                    "related_source": [related_source],
                    "templateId": template_id,
                    "log_id": info['log_id'],
                    "score": info['score'],
                    "log_count": info['log_count'],
                })
        return summary_result

    def process_all_nodes(self, node_data, source, template_cache: TemplateMiner, standard_percent, white_list=None):
        """
        对所有节点的数据进行处理，返回每个节点新增模板的汇总结果。

        :param source:
        :param standard_percent:
        :param node_data: 所有节点的数据字典
        :param white_list: 白名单字典
        :param template_cache 模板查询
        :param template_dict:template对应第一个LogID
        :return: 每个节点新增模板的汇总结果
        """
        all_results = []

        # 将传入数据进行检测，找出新增和数量异常模板
        node_results = []
        new_df = self.find_new_templates_for_node(node_data, white_list=white_list)
        change_df = self.find_change_templates_for_node(node_data, new_df, standard_percent)
        final_dict = self.summarize_and_generate_summary(new_df, change_df, source)
        # 将处理完的数据进行汇总
        for row in final_dict:
            timestamp = row['timestamp']
            total_template = row['TotalTemplate']
            new_template = row['NewTemplatesCount']
            change_template = row['ChangeRecordsCount']
            templateId = row['templateId']
            log_id = row['log_id']
            node_results.append({
                "timestamp": timestamp,
                "TotalTemplate": total_template,
                "NewTemplatesCount": new_template,
                "ChangeRecordsCount": change_template,
                "related_source": source,
                "templateId": templateId,
                "log_id": log_id,
                "score": row['score'],
                "log_count": row['log_count'],
            })
        node_results.sort(key=lambda x: (x['NewTemplatesCount'], x['ChangeRecordsCount']), reverse=True)
        final_result = [
            {
                "timestamp": item.get("timestamp"),
                "related_source": item.get("related_source"),
                "templateId": item.get("templateId"),
                "log_id": item.get("log_id"),
                "score": item.get("score"),
                "log_count": item.get("log_count"),
            }
            for item in node_results
        ]

        all_results.extend(final_result)

        # 添加 id 字段
        for idx, result in enumerate(all_results, start=1):
            result['rank'] = idx

        # 返回最终的 JSON 格式
        result = {
            "detected_result": all_results
        }
        # print(result)
        return result

    def detect(self, source, module, window_map, template_cache: TemplateMiner, white_list, k_sigma):
        node_data = self.convert_window_map_to_input(window_map)
        data = self.process_all_nodes(node_data=node_data,
                                      source=source,
                                      template_cache=template_cache,
                                      white_list=white_list,
                                      standard_percent=k_sigma)
        event_map = {}
        for window in data["detected_result"]:
            if window["timestamp"] not in event_map:
                event_map[window["timestamp"]] = {"timestamp": window["timestamp"],
                                                  "templateId": [window["templateId"]], "log_id": [window["log_id"]],
                                                  "score": [window["score"]]}
            else:
                event_map[window["timestamp"]]["templateId"].append(window["templateId"])
                event_map[window["timestamp"]]["log_id"].append(window["log_id"])
                event_map[window["timestamp"]]["score"].append(window["score"])
        event = []
        for key, value in event_map.items():
            event.append(value)
        return data["detected_result"], event

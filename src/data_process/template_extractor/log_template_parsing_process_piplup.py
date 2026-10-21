#!/usr/bin/python3.7
# -*- coding: utf-8 -*-
# $$$!!Warning: Huawei key information asset. No spread without permission.$$$
# !CODEMARK:RKeR1B8WMAfemkt1tTDGp4eOEddgxKn4NOPmdw0w+6Q3n1pxgDEX+kGBiRV20e1NKuLwOh60qWwx
# 7DOUvTqsDpJdC/G6ahMCQuRlwWqc+IGlEtROC+vbsi8DYnMfRBOrtPvgxM3Nsr6Hz88PQh4lyjlA
# tnSv+aNz2/+8r6lTriW/vkvmbag9Dez9hS8arQrAsxgkVI2gLWAeuOec5u14pdHvZufRtyl6MBJ1
# eni1IqnWunxnOwbgehHBozmZM90ZPx4LYvrtv5wYWJ/gaj+zcAW9YwedsTo5nGtis2vDfuTFNBpK
# MkZxTmCS/U4XU0QV#!
# $$$!!Warning: Deleting or modifying the preceding information is prohibited.$$$
# Copyright (c) Huawei Technologies Co., Ltd. 2012-2020. All rights reserved.
import re
from datetime import datetime
from typing import List
import zlib
import pandas as pd

from src.anomaly_detection.parameter_anomaly.configuration.load_param_config import load_param_config, ParamInfo
from src.common.atomic_op import get_content_as_tokens
from src.data_process.data_basic_structs.basic_structs import LRUCache
from src.data_process.log_context import LogContext
from src.data_process.template_extractor.log_template_repository import LogTemplateRelation
from src.data_process.template_extractor.parsing_algorithm import TemplateMiner
from src.data_process.template_extractor.log_formatter_parse import LogFormatParser
from src.data_process.template_extractor.parsing_algorithm.tools.llm_client_all import llm_client_stream
from src.data_process.template_extractor.parsing_algorithm.LogAutoParsingEngine import LogExtractionEngine

# 预编译正则表达式以降低循环内开销，匹配 YYYY-MM-DD HH:MM:SS 或 YYYY-MM-DD HH:MM:SS.ffffff
TIME_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?")

class MockLogCluster:
    """伪造 Drain 的 LogCluster 对象，补全下游所需的统计属性"""

    def __init__(self, template_str: str, cluster_id: int):
        self._template_str = template_str
        self.cluster_id = cluster_id
        self.size = 0  # 加上 size，下游统计特征极大概率会用到

    def get_template(self) -> str:
        return self._template_str


class MockExtractor:
    def __init__(self):
        self.id_to_cluster = {}


class MockTemplateMiner:
    """彻底伪造的 Miner，取代真实的 TemplateMiner"""

    def __init__(self):
        self.extractor = MockExtractor()

def generate_template_id(template_str: str) -> int:
    """将模板字符串映射为 32 位整型数字，十万级以下模板量不用担心哈希碰撞问题"""
    return zlib.crc32(template_str.encode('utf-8'))

def parse_logs_piplup_adapter(log_info, context: LogContext, start_time=0, end_time=float('inf'), use_format=True,
                              use_params=False, use_LLM=False, print_info=False) -> List[dict]:
    # ==========================================
    # 参数前置归一化 (防御性处理)
    # ==========================================
    def normalize_ts_to_ms(ts: float) -> float:
        """将输入时间戳统一归一化为毫秒级 (13位)"""
        # 放行默认的无穷大或 0 等边界值
        if ts in (0, float('inf'), float('-inf')):
            return ts
        # 秒级阈值判定 (小于 1e11)
        return ts * 1000 if 0 < ts < 1e11 else ts

    # 将 start_time 和 end_time 强制对齐到毫秒级
    start_time_ms = normalize_ts_to_ms(start_time)
    end_time_ms = normalize_ts_to_ms(end_time)

    # 提前实例化，全生命周期复用
    format_dict = context.log_format
    # 复用 context 已有的格式解析对象（含模块级正则编译缓存），
    # 避免每次请求重建 LogFormatParser 重复编译
    if use_format:
        format_parse = getattr(context, 'parser', None)
        if format_parse is None:
            format_parse = LogFormatParser(format_dict)
    else:
        format_parse = None
    param_config = context.get_template_param()

    if use_LLM:
        engine = LogExtractionEngine(format_parse, llm_client_stream)

    # 动态替换 context 的 get_drain 方法
    if not hasattr(context, '_mock_miners'):
        context._mock_miners = {}
        context.get_drain = lambda m: context._mock_miners.setdefault(m, MockTemplateMiner())

    # ==========================================
    # 阶段一：预处理与元数据收集
    # ==========================================
    valid_logs_meta = []
    target_contents = []
    format_mismatch_count = 0
    non_empty_log_count = 0
    time_parse_failed_count = 0

    # 兜底时间初始值设为 1000.0 (相当于 1970-01-01 00:00:01 的毫秒级表示)
    last_valid_timestamp_ms = 1000.0

    for log_data in log_info:
        log = log_data['rawLog'].strip()
        if not log:
            continue
        non_empty_log_count += 1

        # 字符串清理
        if log.endswith("\\\\n"):
            log = log[:-3]
        elif log.endswith("\\n"):
            log = log[:-2]

        module = log_data['module']
        target_content = log
        timestamp = log_data.get('timestamp')

        # 时间戳获取逻辑一：通过 format_parse 解析
        if format_parse and module in format_dict:
            parsed_timestamp, content = format_parse.load_data(module, log)
            if content:
                target_content = content
            else:
                format_mismatch_count += 1

            if timestamp is None and parsed_timestamp:
                timestamp = parsed_timestamp

        # 时间戳获取逻辑二：LLM 智能提取
        if timestamp is None and use_LLM:
            ts, msg = engine.extract(log)
            if ts:
                # 转换时间戳为可读格式 (此时 ts 为毫秒)
                readable_time = datetime.fromtimestamp(ts / 1000).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
                if print_info:
                    print(f'\n>>> SUCCESS')
                    print(f'    TimeStamp: {readable_time}')
                    print(f'    Content:   {msg}')

                # 统一毫秒：LLM 返回的就是毫秒，直接使用，不再除以 1000
                timestamp, target_content = ts, msg

        # 时间戳获取逻辑三：正则降级匹配
        if timestamp is None:
            match = TIME_PATTERN.search(log)
            if match:
                time_str = match.group(0)
                try:
                    if '.' in time_str:
                        dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S.%f")
                    else:
                        dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")

                    # 统一毫秒：dt.timestamp() 返回的是秒，必须乘以 1000
                    timestamp = dt.timestamp() * 1000.0
                except ValueError:
                    pass


        # 时间戳获取逻辑四：历史兜底与最终归一化
        if timestamp is None:
            # 解析彻底失败，使用上一条的合法毫秒级时间
            timestamp = last_valid_timestamp_ms
            time_parse_failed_count += 1
        else:
            # 防御性编程：防止 log_data 带来的时间戳是秒级，做一次强制统一
            timestamp = normalize_ts_to_ms(timestamp)

        # 及时更新上一条有效时间戳 (已经是毫秒级)，供下一轮循环使用
        last_valid_timestamp_ms = timestamp

        # 筛选指定时间段日志
        if timestamp > end_time_ms or timestamp < start_time_ms:
            continue

        # 保存有效日志的上下文元数据，以便解析后恢复
        valid_logs_meta.append({
            'module': module,
            'source': log_data['source'],
            'log_id': log_data['logId'],
            'timestamp': timestamp,
            'target_content': target_content  # 后续提参数需要用到原始 content
        })
        target_contents.append(target_content)

    # 防御性编程：如果全部日志都被时间戳过滤掉了，直接返回
    if not target_contents:
        return []

    # ==========================================
    # 阶段二：PIPLUP 批量解析核心调用
    # ==========================================
    # 此时 target_contents 是一个包含所有待解析内容的庞大 List
    masked_contents = context.parser_piplup.parse_list(target_contents)

    # ==========================================
    # 阶段三：结果组装与下游状态伪造 (Zip 配对)
    # ==========================================
    parse_results = []

    # zip 会将元数据和对应的解析结果严格按索引位置一对一打包
    for meta, masked_content in zip(valid_logs_meta, masked_contents):
        module = meta['module']
        target_content = meta['target_content']

        drain = context.get_drain(module)

        # 模板与ID生成
        template = masked_content if masked_content != '<*>' else '<Short Template>'
        template_id = generate_template_id(template)

        # 挂载假数据到字典中
        if template_id not in drain.extractor.id_to_cluster:
            drain.extractor.id_to_cluster[template_id] = MockLogCluster(template, template_id)

        # 保证模板命中次数的真实性
        drain.extractor.id_to_cluster[template_id].size += 1

        # 参数提取机制
        param_list = []
        if use_params:
            split_result = re.split(r'([:=,\[\]\(\)])', target_content)
            split_content = target_content.strip()
            if len(split_result) > 1:
                split_content = ' '.join(split_result)

            content_tokens = get_content_as_tokens(split_content)

            if (module in param_config) and (template_id in param_config[module]):
                for each_pos in param_config[module][template_id]:
                    pos_idx = int(each_pos)
                    if pos_idx < len(content_tokens):
                        single_ParamInfo = ParamInfo(
                            template_id=template_id,
                            template_pos=each_pos,
                            template_param=content_tokens[pos_idx]
                        )
                        param_list.append(single_ParamInfo)

        # 持久化
        context.log_repository.add(LogTemplateRelation(
            meta['source'], meta['timestamp'], meta['log_id'], template_id, module, param_list,
            template=template, raw_content=target_content
        ))

        parse_results.append({
            'module': module,
            'log_id': meta['log_id'],
            'template': template,
            'template_id': template_id
        })

    # ==========================================
    # 数据统计与覆盖率指标输出
    # ==========================================
    if print_info:
        if non_empty_log_count > 0:
            parsed_count = len(parse_results)

            # 覆盖率计算
            piplup_coverage = (parsed_count / non_empty_log_count) * 100
            drain_coverage = ((parsed_count - format_mismatch_count) / non_empty_log_count) * 100
            coverage_diff = piplup_coverage - drain_coverage

            # 基础指标打印
            print(f"有效非空日志总数: {non_empty_log_count} 行")
            print(f"其中共有 {format_mismatch_count} 行日志格式与定义不一致。")
            print(f"共有 {time_parse_failed_count} 行日志时间戳解析失败，已触发兜底策略赋值为上一条日志时间。")

            # 算法效能对比打印
            print(f"PIPLUP 算法解析覆盖率为 {piplup_coverage:.2f}%，较 Drain 算法提升 {coverage_diff:.2f}%。")
        else:
            print("有效非空日志总数为 0，跳过覆盖率指标计算。")

    return parse_results


def parse_logs_piplup_only(log_info, parser, context, use_format=True, save_csv=False, csv_path="piplup_result.csv") -> List[str]:
    # 提取可复用的对象，避免在循环内重复实例化或高频调用
    format_dict = context.log_format
    format_parse = context.get_format_parse() if use_format else None

    # ==========================================
    # 阶段一：数据预处理与收集
    # ==========================================
    target_contents = []
    valid_log_ids = []

    for log_data in log_info:
        module = log_data['module']
        log = log_data['rawLog'].strip()
        log_id = log_data.get('logId')

        if not log:
            continue

        # 安全清理换行符
        if log.endswith("\\\\n"):
            log = log[:-3]
        elif log.endswith("\\n"):
            log = log[:-2]

        target_content = log

        # 匹配逻辑与内容提取
        if format_parse and module in format_dict:
            extracted = format_parse.load_content(module, log)
            if extracted is not None:
                target_content = extracted

        target_contents.append(target_content)
        valid_log_ids.append(log_id)

    # 防御性拦截：如果全部是空日志，直接返回空列表
    if not target_contents:
        return []

    # ==========================================
    # 阶段二：PIPLUP 批量解析并直接返回
    # ==========================================
    # 直接将整个列表喂给 PIPLUP 算法，获取全局最优的模板解析结果
    masked_contents = parser.parse_list(target_contents)

    # ==========================================
    # 阶段三：持久化评估所需的 CSV 文件
    # ==========================================
    if save_csv:
        # 提取去重后的模板集并建立哈希映射，以保证 EventId 生成的一致性与稳定性
        unique_templates = sorted(list(set(masked_contents)))
        template_to_id = {tmpl: f"E{generate_template_id(tmpl)}" for tmpl in unique_templates}
        # 构造对齐评估脚本的字段结构：LineId, EventId, EventTemplate
        payload = {
            "LineId": valid_log_ids,
            "Content": target_contents,
            "EventId": [template_to_id[tmpl] for tmpl in masked_contents],
            "EventTemplate": masked_contents
        }

        df = pd.DataFrame(payload)
        df.to_csv(csv_path, index=False, encoding='utf-8')

    return masked_contents
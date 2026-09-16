#!/usr/bin/python3.7
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2012-2020. All rights reserved.
import re
from typing import List
import pandas as pd

from src.anomaly_detection.parameter_anomaly.configuration.load_param_config import load_param_config, ParamInfo
from src.common.atomic_op import get_content_as_tokens
from src.data_process.data_basic_structs.basic_structs import LRUCache
from src.data_process.log_context import LogContext
from src.data_process.template_extractor.log_template_repository import LogTemplateRelation
from src.data_process.template_extractor.parsing_algorithm import TemplateMiner
from src.data_process.template_extractor.log_formatter_parse import LogFormatParser


def parse_logs(log_info, context: LogContext) -> List[dict]:
    parse_results=[]
    # 逐条解析
    for log_data in log_info:
        module = log_data['module']
        source = log_data['source']
        drain: TemplateMiner = context.get_drain(module)
        cache: LRUCache = context.get_lru_cache(module)
        format_dict = context.log_format
        if module not in format_dict:
            if context.template_config.print_invalid_log:
                print(f"no format for module: {module}")
            continue
        log_id = log_data['logId']

        log = log_data['rawLog'].strip()
        if log.endswith("\\\\n"):
            log = log[:-3]
        if log.endswith("\\n"):
            log = log[:-2]
        format_parse = context.get_format_parse()
        content = format_parse.load_content(module, log)
        if not content or content == "":
            if context.template_config.print_invalid_log:
                print(f"invalid log: {log}, module: {module}")
            continue
        timestamp = log_data['timestamp']
        masked_content = drain.masker.mask(content)
        template_id = cache.get(masked_content)
        if template_id is None:
            split_result = re.split(r'([:=,\[\]\(\)])', masked_content)
            if len(split_result) > 1:
                masked_content = ' '.join(split_result)
            cluster = drain.match(masked_content)

            if cluster is None:
                result = drain.add_log_message(masked_content)
                template = result['template_mined']
                template_id = result['cluster_id']
            else:
                template = cluster.get_template()
                template_id = cluster.cluster_id
            cache.put(masked_content, template_id)
        else:
            template = drain.extractor.id_to_cluster.get(template_id).get_template()
        if template == '<*>':
            template = '<Short Template>'
        split_result = re.split(r'([:=,\[\]\(\)])', content)
        split_content = content.strip()
        if len(split_result) > 1:
            split_content = ' '.join(split_result)
        content_tokens = get_content_as_tokens(split_content)
        param_list = []
        param_config = context.get_template_param()
        # 根据template_id按需获取参数信息，并存储
        if (module in param_config) and (template_id in param_config[module]):
            for each_pos in param_config[module][template_id]:
                single_ParamInfo = ParamInfo(
                    # 三要素：模板id，检测的位置，以及对应位置的具体值
                    template_id=template_id,
                    template_pos=each_pos,
                    template_param=content_tokens[int(each_pos)]
                )
                param_list.append(single_ParamInfo)
        context.log_repository.add(LogTemplateRelation(source, timestamp, log_id, template_id, module, param_list))
    return parse_results

def parse_logs_no_header(log_info, context: LogContext) -> List[dict]:
    parse_results = []
    parsed_log = []
    param_config = load_param_config()
    # 逐条解析
    for log_data in log_info:
        module = log_data['module']
        source = log_data['source']
        drain: TemplateMiner = context.get_drain(module)
        cache: LRUCache = context.get_lru_cache(module)
        log_id = log_data['logId']
        content = log_data['rawLog'].strip()
        if "timestamp" in log_data:
            timestamp = log_data['timestamp']
        parsed_log.append(content)
        masked_content = drain.masker.mask(content)

        template_id = cache.get(masked_content)
        if template_id is None:
            split_result = re.split(r'([:=,\[\]\(\)])', masked_content)
            if len(split_result) > 1:
                masked_content = ' '.join(split_result)
            cluster = drain.match(masked_content)

            if cluster is None:
                result = drain.add_log_message(masked_content)
                template = result['template_mined']
                template_id = result['cluster_id']
            else:
                template = cluster.get_template()
                template_id = cluster.cluster_id
            cache.put(masked_content, template_id)
        else:
            template = drain.extractor.id_to_cluster.get(template_id).get_template()

        if template == '<*>':
            template = '<Short Template>'
        split_result = re.split(r'([:=,\[\]\(\)])', content)
        split_content = content.strip()
        if len(split_result) > 1:
            split_content = ' '.join(split_result)
        content_tokens = get_content_as_tokens(split_content)
        parse_result = {'module': module, 'log_id': log_id, 'template': template, 'template_id': template_id}
        parse_results.append(parse_result)
        param_list = []
        # 根据template_id按需获取参数信息，并存储
        if (module in param_config) and (template_id in param_config[module]):
            for each_pos in param_config[module][template_id]:
                single_ParamInfo = ParamInfo(
                    # 三要素：模板id，检测的位置，以及对应位置的具体值
                    template_id=template_id,
                    template_pos=each_pos,
                    template_param=content_tokens[int(each_pos)]
                )
                param_list.append(single_ParamInfo)
        context.log_repository.add(LogTemplateRelation(source, timestamp, log_id, template_id, module, param_list))
    return parse_results

def parse_logs_dem(log_info, context: LogContext, abn_type = "", abn_inject_excel = "") -> List[dict]:

    parse_results = []
    parsed_log = []
    param_config = load_param_config()
    # 逐条解析
    for log_data in log_info:
        module = log_data['module']
        source = log_data['source']
        drain: TemplateMiner = context.get_drain(module)
        cache: LRUCache = context.get_lru_cache(module)
        format_dict = context.log_format
        if module not in format_dict:
            if context.template_config.print_invalid_log:
                print(f"no format for module: {module}")
            continue
        log_id = log_data['logId']

        log = log_data['rawLog'].strip().strip('\\\\n')
        format_parse = LogFormatParser(format_dict)
        timestamp, content = format_parse.load_data(module, log)
        if not timestamp or not content:
            if context.template_config.print_invalid_log:
                print(f"invalid log: {log}, module: {module}")
            continue
        if "timestamp" in log_data:
            timestamp = log_data['timestamp']

        parsed_log.append(content)

        if content == '':
            content = log
        masked_content = drain.masker.mask(content)

        template_id = cache.get(masked_content)
        if template_id is None:
            split_result = re.split(r'([:=,\[\]\(\)])', masked_content)
            if len(split_result) > 1:
                masked_content = ' '.join(split_result)
            cluster = drain.match(masked_content)

            if cluster is None:
                result = drain.add_log_message(masked_content)
                template = result['template_mined']
                template_id = result['cluster_id']
            else:
                template = cluster.get_template()
                template_id = cluster.cluster_id
            cache.put(masked_content, template_id)
        else:
            template = drain.extractor.id_to_cluster.get(template_id).get_template()

        if template == '<*>':
            template = '<Short Template>'
        split_result = re.split(r'([:=,\[\]\(\)])', content)
        split_content = content.strip()
        if len(split_result) > 1:
            split_content = ' '.join(split_result)
        content_tokens = get_content_as_tokens(split_content)
        parse_result = {'module': module, 'log_id': log_id, 'template': template, 'template_id': template_id}
        parse_results.append(parse_result)
        param_list = []
        # 根据template_id按需获取参数信息，并存储
        if (module in param_config) and (template_id in param_config[module]):
            for each_pos in param_config[module][template_id]:
                single_ParamInfo = ParamInfo(
                    # 三要素：模板id，检测的位置，以及对应位置的具体值
                    template_id=template_id,
                    template_pos=each_pos,
                    template_param=content_tokens[int(each_pos)]
                )
                param_list.append(single_ParamInfo)
        context.log_repository.add(LogTemplateRelation(source, timestamp, log_id, template_id, module, param_list))
    return parse_results


def parse_logs_dem_with_csv(log_info, context, abn_type="", abn_inject_excel="", save_csv=False,
                            csv_path="drain3_result.csv") -> List[dict]:
    """
    parse_logs_dem 的非侵入式包装函数，用于在解析完成后导出标准格式的评估 CSV。
    """
    # 1. 透明调用底层解析内核，不改变原有业务逻辑与副作用（如 LogRepository 写入）
    parse_results = parse_logs_dem(log_info, context, abn_type, abn_inject_excel)

    # 2. 拦截输出并执行离线指标评估的持久化逻辑
    if save_csv and parse_results:
        event_ids = [res.get('template_id') for res in parse_results]
        event_templates = [res.get('template') for res in parse_results]

        # 提取原函数的 log_id，而不是重新生成 range
        line_ids = [res.get('log_id') for res in parse_results]

        formatted_event_ids = [f"E{eid}" if str(eid).isdigit() else str(eid) for eid in event_ids]

        payload = {
            "LineId": line_ids,
            "EventId": formatted_event_ids,
            "EventTemplate": event_templates
        }

        df = pd.DataFrame(payload)
        df.to_csv(csv_path, index=False, encoding='utf-8')

    return parse_results
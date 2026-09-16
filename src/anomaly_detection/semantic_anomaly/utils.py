from typing import List, Tuple
import re
from .data_models import Log, TemplateInfo, AbnormalLog, AbnormalEvent, TemplateInfoOneModule, AbnormalLogPerSource, \
    AbnormalEventPerSource


def format_input(window_map, template_cache, source, module, white_list):
    """
    将输入处理成结构化数据
    :param window_map: templateId: [ArrWindow, ArrWindow, ...] 其中每一个ArrWindow都是window_size切割后的产物 -> 即每个window_size内相同模板的日志被汇聚成了一条ArrWindow对象,时间戳被替换成对应window的时间戳
    :param template_cache: Drain算法的TemplateMiner对象，保有template信息
    :param source
    :param module
    :return:
    """
    preprocessed = []
    for templateId in window_map:
        if templateId in white_list:
            continue
        template = template_cache.extractor.id_to_cluster[templateId].get_template()
        for win_arr in window_map[templateId]:
            win_dict_list = win_arr.to_dict_list()
            for win_dict in win_dict_list:
                template_info = TemplateInfo(
                    template=template,
                    regex_pattern=re.sub(r'<\*>', r'(.*?)', template),
                    template_id=str(templateId),
                    log=Log(log_id=win_dict["logId"],
                            timestamp=win_dict["timestamp"],
                            source=source,
                            module=module,
                            raw_log="")
                )
                preprocessed.append(template_info)
    return sorted(preprocessed, key=lambda x: x.timestamp)


def format_output(abnormal_logs: List[AbnormalLogPerSource], abnormal_events: List[AbnormalEventPerSource]) -> Tuple[List, List]:
    data = [
        {"timestamp": log.timestamp,
         "templateId": log.template_id,
         "log_id": log.log_id,
         "source": log.source,
         "anomalyType": log.anomaly_type} for log in abnormal_logs]

    events = [
        {
            "timestamp": event.window_start_time,
            "templateId": event.template_ids,
            "log_id": event.log_ids,
            "score": event.score,
        } for event in abnormal_events
    ]
    return data, events

def format_output_one_module(abnormal_logs: List[AbnormalLog]) -> List[dict]:
    data = [
        {"timestamp": log.timestamp,
         "templateId": log.template_id,
         "log_id": log.log_id,
         "source": log.source,
         "sources": log.log_source,
         "anomalyType": log.anomaly_type} for log in abnormal_logs]
    return data


def format_input_one_module(windows, template_cache, module, white_list):
    """
    将输入处理成结构化数据
    :param windows: templateId: [MixedAggrWindow, MixedAggrWindow, ...] 其中每一个MixedAggrWindow都是window_size切割后的产物 -> 即每个window_size内相同模板的日志被汇聚成了一条MixedAggrWindow对象,时间戳被替换成对应window的时间戳
    :param template_cache: Drain算法的TemplateMiner对象，保有template信息
    :param module
    :return:
    """
    preprocessed = []
    for templateId in windows:
        if templateId in white_list:
            continue
        template = template_cache.extractor.id_to_cluster[templateId].get_template()
        for win_arr in windows[templateId]:
            win_dict_list = win_arr.to_dict_list()
            for win_dict in win_dict_list:
                template_info = TemplateInfoOneModule(
                    template=template,
                    regex_pattern=re.sub(r'<\*>', r'(.*?)', template),
                    template_id=str(templateId),
                    log=Log(log_id=win_dict["logId"],
                            timestamp=win_dict["timestamp"],
                            module=module,
                            source=win_dict["source"],
                            raw_log=""),
                    log_source = [win_dict["source"]]
                )
                preprocessed.append(template_info)
    return sorted(preprocessed, key=lambda x: x.timestamp)




def verify_one_regex(log: str, regex: str, exact_match: bool = False) -> bool:
    """
    verify if the regex pattern matches the target log.
    """
    log = log.replace(",", "")
    regex = regex.replace(",", "")
    # regex = re.escape(regex).replace(r"\\s\*\(\.\*\?\)", r"\s*(.*?)").replace(r"\(\.\*\?\)", "(.*?)")
    if exact_match:
        regex = f'^{regex}$'
    try:
        return re.search(regex, log) is not None
    except re.error as e:
        print(f"Error in verify_one_regex: {e}")
        return False

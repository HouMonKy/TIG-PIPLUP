import time
from typing import List

from src.data_process.template_extractor.parsing_algorithm import TemplateMiner


class LogTemplateRelation:
    def __init__(self, source, create_time, log_id, template_id, module, param_list,
                 template=None, raw_content=None):
        self.source = source
        self.time = create_time
        self.log_id = log_id
        self.template_id = template_id
        self.module = module
        self.template = template
        self.param_list = param_list
        # 原始日志正文（去格式后），供新版参数异常检测器自主取值
        self.raw_content = raw_content

    def to_dict(self):
        param_arr = []
        for param in self.param_list:
            param_arr.append(param.to_dict())
        return {"source": self.source, "time": self.time, "log_id": self.log_id, "template_id": self.template_id, "module": self.module, "template": self.template, "param_list":param_arr}


class AggrWindow:
    """
    这个汇聚时间窗只包含了单个module和单个source的所有时间窗的统计数据
    """
    def __init__(self, templateId, timestamp):
        self.logs = []
        self.count = 0
        self.template = None
        self.timestamp = timestamp
        self.templateId = templateId
        self.anomalyType = "normal"
        self.summary = "正常"

    def to_dict_list(self):
        result = []
        for logId in self.logs:
            result.append({"logId": logId, "anomalyType": self.anomalyType, "timestamp": self.timestamp,
                           "templateId": self.templateId, "template": self.template, "summary": self.summary})
        return result

class MixedAggrWindow:
    """
    这个时间窗包含了单个module以及module下所有的source的统计数据，logs是一个map，key表示source value是log_id列表，表示这个source下所有的日志信息
    """
    def __init__(self, template_id, timestamp):
        self.logs = {}
        self.template = None
        self.timestamp = timestamp
        self.templateId = template_id

    def to_dict_list(self):
        result = []
        for source, log_ids in self.logs.items():
            for log_id in log_ids:
                result.append({"logId": log_id, "timestamp": self.timestamp,
                           "templateId": self.templateId, "template": self.template, "source": source})
        return result


def cal_window_start(timestamp, window_size):
    return timestamp - timestamp % window_size


class LogTemplateRepository:
    def __init__(self, duration):
        # 按照 source的维度进行存储，每个source按照配置
        self.logs = {}
        self.duration = duration
        self.last_remove = 0

    # 新增一条日志信息，如果距离上次清理超过1小时，则触发清理逻辑
    def add(self, log: LogTemplateRelation):
        cur_time = int(time.time() * 1000)
        if cur_time > self.last_remove + 3600000:
            self.remove_timeout()
            self.last_remove = cur_time
        if log.module in self.logs:
            self.logs[log.module][log.log_id] = log
        else:
            one_module_log = {log.log_id: log}
            self.logs[log.module] = one_module_log

    # 清理超过了指定时间的所有数据
    def remove_timeout(self):
        oldest = int(time.time() * 1000) - self.duration
        print(f"start remove timeout, timeout = {oldest}")
        for module in list(self.logs.keys()):
            one_module = self.logs[module]
            delete_count = 0
            for log_id in list(one_module.keys()):
                one_log = one_module[log_id]
                if one_log.time <= oldest:
                    del one_module[log_id]
                    delete_count += 1
            print(f"deleted {delete_count} logs for {module}")

    # 根据秒级时间戳start查询start之前一天的数据,按照固定3分钟一个时间窗口返回统计数据
    def get_aggr_windows(self, start, end, window_size, module, source) -> List[AggrWindow]:
        if module not in self.logs:
            return []
        one_module = self.logs[module]
        window_map = {}
        for log in one_module.values():
            if log.module != module or log.source != source:
                continue
            if not (start <= log.time <= end):
                continue
            window_begin = cal_window_start(log.time, window_size)
            window_key = "{}_{}".format(log.template_id, window_begin)
            if window_key not in window_map:
                aggr_window = AggrWindow(log.template_id, window_begin)
                window_map[window_key] = aggr_window
                aggr_window.count += 1
                aggr_window.logs.append(log.log_id)
            else:
                aggr_window = window_map[window_key]
                aggr_window.count += 1
                aggr_window.logs.append(log.log_id)
        return list(window_map.values())

    def get_window_logs_by_module_and_source(self, start: int, end:int, module: str, source: str, drain: TemplateMiner) -> List[LogTemplateRelation]:
        """
        查询指定时间段的日志列表，返回信息中包含模板信息，参数列表
        """
        if module not in self.logs:
            return []
        one_module = self.logs[module]
        result = []
        for log in one_module.values():
            if start <= log.time <= end and log.source == source:
                log.template = drain.extractor.id_to_cluster.get(log.template_id).get_template()
                result.append(log)
        return result

    def get_window_logs(self, start: int, end:int) -> List[LogTemplateRelation]:
        """
        查询指定时间段的日志列表，返回信息中包含模板信息，参数列表
        """
        result = []
        for module, one_module_logs in self.logs.items():
            for log_id, log in one_module_logs.items():
                if start <= log.time <= end:
                    result.append(log)
        return result


    def group_window_by_module(self, start, end, window_size) -> dict[str, dict[str, MixedAggrWindow]]:
        """
        查询指定时间内的所有module和source的数据
        结构如下所示：
        {
            "module_messages": {
                "1759248_1759248000000":{
                    "logs":{"source":[1, 2], "source2": [3, 4,5,6]},
                    "template_id": "1759248",
                    "timestamp": 1759248000000
                }
            }
        }
        """
        result = {}
        for module, one_module_logs in self.logs.items():
            for log_id, log in one_module_logs.items():
                if log.time < start or log.time > end:
                    continue
                one_module_windows = result.setdefault(module, {})
                window_begin = cal_window_start(log.time, window_size)
                window_key = "{}_{}".format(log.template_id, window_begin)
                if window_key not in one_module_windows:
                    aggr_window = MixedAggrWindow(log.template_id, window_begin)
                    one_module_windows[window_key] = aggr_window
                    if log.source in aggr_window.logs:
                        aggr_window.logs[log.source].append(log_id)
                    else:
                        aggr_window.logs[log.source] = [log.log_id]
                else:
                    aggr_window = one_module_windows[window_key]
                    if log.source in aggr_window.logs:
                        aggr_window.logs[log.source].append(log_id)
                    else:
                        aggr_window.logs[log.source] = [log.log_id]
        return result

    def get_all_aggr_windows(self, start, end, window_size) -> dict[str, dict[str, dict[str, AggrWindow]]]:
        """
        查询指定时间内的所有module和source的数据
        结构如下所示：
        {
            "module_messages": {
                "source_messages":{
                     "1759248_1759248000000":{
                         "logs":[1, 2, 3, 4,5,6,],
                         "count": 1,
                         "template_id": "1759248",
                         "timestamp": 1759248000000
                     }
                }
            }
        }
        """
        result = {}
        for module, one_module_logs in self.logs.items():
            for log_id, log in one_module_logs.items():
                if log.time < start or log.time > end:
                    continue
                one_module_windows = result.setdefault(module, {})
                one_module_source_windows = one_module_windows.setdefault(log.source, {})
                window_begin = cal_window_start(log.time, window_size)
                window_key = "{}_{}".format(log.template_id, window_begin)
                if window_key not in one_module_source_windows:
                    aggr_window = AggrWindow(log.template_id, window_begin)
                    one_module_source_windows[window_key] = aggr_window
                    aggr_window.count += 1
                    aggr_window.logs.append(log.log_id)
                else:
                    aggr_window = one_module_source_windows[window_key]
                    aggr_window.count += 1
                    aggr_window.logs.append(log.log_id)
        return result

    def clear(self):
        self.logs = {}

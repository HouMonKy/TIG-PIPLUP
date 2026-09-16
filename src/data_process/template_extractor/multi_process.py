import multiprocessing
import re
import threading
import time
from typing import Optional

from src.anomaly_detection.parameter_anomaly.configuration.load_param_config import ParamInfo
from src.common.atomic_op import get_content_as_tokens
from src.data_process.log_context import LogContext
from src.data_process.template_extractor.log_template_parsing_process import parse_logs
from src.data_process.template_extractor.log_template_repository import LogTemplateRelation


class ProcessContext:
    context: LogContext
    id: Optional[int]
    start_time: Optional[int]

    def __init__(self, context: LogContext):
        self.context = context
        self.id = None
        self.start_time = None

    def initialize(self, process_id: int):
        self.id = process_id
        self.start_time = int(time.time() * 1000)

def initialize_worker(shared_context: LogContext):
    global worker_context
    print("initialize_worker")
    worker_context = ProcessContext(shared_context)
    worker_context.initialize(multiprocessing.current_process().pid)


def process_data(log_infos: list[dict]):
    global worker_context
    print(f"process data in process: {worker_context.id}")
    begin = int(time.time() * 1000)
    context = worker_context.context

    # 子进程只进行可以并行的日志解析成content和timestamp的操作，drain树必须是单节点的
    masked_logs = []
    for log_data in log_infos:
        module = log_data['module']
        source = log_data['source']
        drain = context.get_drain(module)
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
        masked_logs.append({"log_id": log_id, "source": source, "timestamp": timestamp, "masked_content": masked_content, "module": module, "content": content})
    end = int(time.time() * 1000)
    print(f"process log content cost: {end - begin}")
    return masked_logs


class MultiProcess:
    def __init__(self, context: LogContext, max_workers = 3):
        self.max_workers = max_workers
        self.context = context
        self.pool = multiprocessing.Pool(
                processes = max_workers,
                initializer = initialize_worker,
                initargs = (context,)
        )


    def collect_results(self, results):
        for mask_log in results:
            module = mask_log['module']
            source = mask_log['source']
            timestamp = mask_log['timestamp']
            masked_content = mask_log['masked_content']
            content = mask_log['content']
            log_id = mask_log['log_id']
            drain = self.context.get_drain(module)
            cache = self.context.get_lru_cache(module)
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
            split_result = re.split(r'([:=,\[\]\(\)])', content)
            split_content = content.strip()
            if len(split_result) > 1:
                split_content = ' '.join(split_result)
            content_tokens = get_content_as_tokens(split_content)
            param_list = []
            param_config = self.context.get_template_param()
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
            self.context.log_repository.add(LogTemplateRelation(source, timestamp, log_id, template_id, module, param_list))
        end = time.time() * 1000
        print(f"process drain match cost: {end}", flush=True)

    def execute(self, log_infos: list):
        result = self.pool.apply_async(process_data, args = (log_infos,), callback = self.collect_results)

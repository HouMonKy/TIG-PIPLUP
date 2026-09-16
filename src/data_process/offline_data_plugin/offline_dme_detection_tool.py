from src.anomaly_detection.anomaly_detection import AnomalyDetection
from src.data_process.log_context import LogContext
from src.data_process.offline_data_plugin.dme_data_plugin.log_file_converter import LogFileConverter
from src.data_process.offline_data_plugin.utils import timestamp_to_window
from src.data_process.template_extractor.log_template_parsing_process import parse_logs
from src.data_process.template_extractor.log_template_repository import LogTemplateRepository
from src.evaluation.metrics import Evaluator
from src.common.global_config import GlobalConfig


class OfflineDMEDetectionTool(object):
    def __init__(self, mode: str):
        self.context = LogContext()
        self.mode = mode

    def start_detect(self,
                     file_dir: str,
                     module: str,
                     source: str,
                     start_time: int,
                     end_time: int,
                     window_size: int,
                     data_index=0,
                     label=[],
                     module_result_path=None):
        # 读取日志文件
        log_infos = LogFileConverter.read_and_convert_directory(file_dir, source, module)
        # 日志处理 模板抽取
        parse_results = parse_logs(log_infos, self.context)
        print(f"parse_results size: {len(parse_results)}")
        self.context.persistence_drain()

        # 异常检测
        log_repository: LogTemplateRepository = self.context.log_repository
        # 请求体中的数据只包含需要检测的最后一个时间窗的数据
        drain = self.context.get_drain(module)
        if window_size is None:
            window_size = 180000
        data, event = AnomalyDetection.get_detector(mode=self.mode).anomaly_detection(source=source,
                                                                         module=module,
                                                                         start_time=start_time,
                                                                         end_time=end_time,
                                                                         window_size=window_size,
                                                                         repository=log_repository,
                                                                         template_cache=drain,
                                                                         white_list=self.context.get_white_list(module),
                                                                         module_result_path=module_result_path)

        tp, fp, tn, fn = Evaluator(start_time=start_time, end_time=end_time, window_size=window_size).compute_confusion_matrix([ev["timestamp"] for ev in event], label)
        template_ids = []
        for ev in event:
            template_ids.extend(ev["template_id"])
        self.context.log_repository.clear()
        return tp, fp, tn, fn, len(set(template_ids)), len(parse_results), event


if __name__ == '__main__':
    import pandas as pd
    import json
    from pathlib import Path

    tool = OfflineDMEDetectionTool()
    labels = {
        1: (1754017479000, 1754018321000),
        2: (1754270641000, 1754271240000),
        3: (1754017479000, 1754018321000),
        4: (1754287400000, 1754029038000),
        5: (1754017388000, 1754288025000),
        6: (1754287400000, 1754029038000),
        7: (1754017479000, 1754018321000),
        8: (1754039280000, 1754039400000),
        10: (1754037180000, 1754037360000),
        11: (1754374680000, 1754375280000),
        12: (1754363520000, 1754364600000),
        13: (1754030100000, 1754030700000),
        14: (1754033866000, 1754034600000),
        15: (1754035549000, 1754036263000),
        19: (1754272735000, 1754273413000),
        20: (1754274226000, 1754274826000),
        21: (1754275845000, 1754276476000),
        24: (1754289515000, 1754290150000),
        25: (1754290860000, 1754291464000),
        26: (1754292727000, 1754293447000),
        27: (1754360700000, 1754361300000),
        28: (1754294700000, 1754295300000),
        29: (1754296200000, 1754296860000),
        30: (1754357100000, 1754357700000),
    }
    start_time = 1753977600000
    end_time = 1754409599000
    window_size = 600000

    label_res = {}
    for key in labels:
        t1, t2 = labels[key]
        st1, st2 = (timestamp_to_window(timestamp=t1, start_time=start_time, end_time=end_time, window_size=window_size),
                    timestamp_to_window(timestamp=t2, start_time=start_time, end_time=end_time, window_size=window_size))
        label_res[key] = list(range(st1, st2+1, window_size))

    output_path_root = GlobalConfig().get_dataset_root()
    rows = []
    for key, value in label_res.items():
        for module in ["osd", "eds"]:
            tp, fp, tn, fn, num_anom_templates, num_templates, event = tool.start_detect(f"dataset/input/DME/case{key}/{module}",
                                                                                         module=module,
                                                                                         source=module,
                                                                                         start_time=start_time,
                                                                                         end_time=end_time,
                                                                                         window_size=window_size,
                                                                                         label=value,
                                                                                         module_result_path=None)
            rows.append({
                "case": key,
                "module": module,
                "tp": tp,
                "fp": fp,
                "tn": tn,
                "fn": fn,
                "num_anom_templates": num_anom_templates,
                "num_templates": num_templates,
            })

            file_path = f"{output_path_root}/output/DME/LLM/case{key}/案例{key}_{module}.json"
            Path(file_path).parent.mkdir(parents=True, exist_ok=True)
            with open(file_path, 'w') as f:
                json.dump(event, f, indent=4, sort_keys=True, default=str)

    df = pd.DataFrame(rows)
    df.to_csv(f"{output_path_root}/output/DME/dme_detection_result_llm.csv", index=False)

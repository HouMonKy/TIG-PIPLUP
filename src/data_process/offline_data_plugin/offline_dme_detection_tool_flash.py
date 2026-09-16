import os
from src.anomaly_detection.anomaly_detection import AnomalyDetection
from src.data_process.log_context import LogContext
from src.data_process.offline_data_plugin.dme_data_plugin.log_file_converter import LogFileConverter
from src.data_process.template_extractor.log_template_parsing_process import parse_logs_dem, parse_logs
from src.data_process.template_extractor.log_template_repository import LogTemplateRepository
import json
from pathlib import Path
import datetime


class OfflineDMEDetectionToolFlash(object):
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
        log_infos = LogFileConverter.read_and_convert_single_file(file_dir, source, module)
        for log_info in log_infos:
            log_info["rawLog"] = log_info["rawLog"].replace("] [", "][")

        # 日志处理 模板抽取
        parse_results = parse_logs_dem(log_infos, self.context)
        print(f"parse_results size: {len(parse_results)}")
        self.context.persistence_drain()

        # 异常检测
        log_repository: LogTemplateRepository = self.context.log_repository
        # 请求体中的数据只包含需要检测的最后一个时间窗的数据
        drain = self.context.get_drain(module)
        if window_size is None:
            window_size = 180000

        result = AnomalyDetection.get_detector(mode=self.mode).all_detect(start_time=start_time,
                                                                           end_time=end_time,
                                                                           window_size=window_size,
                                                                           repository=log_repository,
                                                                           context=self.context)
                # print(f"file", reports)
        print(f'log detection and diagnosis finished {file_dir}')
        self.context.log_repository.clear()  # 清理日志缓存

        return result, parse_results


if __name__ == '__main__':
    modules = ['Messages', 'msg']
    replacements = {"msg":"Messages"}
    tool = OfflineDMEDetectionToolFlash(mode="all")
    data_dir = "run_交行案例_template"
    data_parent_dir = "../../../dataset/output/Flash/" + data_dir
    record_event = True
    record_report = True
    for dirpath, dirnames, filenames in os.walk(data_parent_dir):
        for filename in filenames:
            # 打印文件的绝对路径
            module = 'test'
            file_path = os.path.join(dirpath)
            filepath = Path(os.path.join(dirpath, filename))
            for part in filepath.parts:
                if part in modules:
                    module = part
            if module == 'test':
                print(file_path,"不属于任何模块")
                continue

            for k,v in replacements.items():
                if module == k:
                    module = v
                    break

            try:
                result, parse_results = tool.start_detect(str(filepath), module=module, source=module,
                                                    start_time=1704038400000, end_time=2754409540000,
                                                    window_size=600000, label=[0])
                if not result:
                    continue
                output_base_dir = file_path.replace(data_dir, data_dir+"_output")
                if not os.path.exists(output_base_dir):
                    os.makedirs(output_base_dir)
                for res in result:
                    reports = res["reports"]
                    base_dir = "../../../dataset/output/Flash"
                    if reports and record_report:
                        file_json = os.path.basename(filepath) + 'event_reports.json'
                        output_path = os.path.join(output_base_dir, file_json)
                        if not os.path.exists(base_dir):
                            os.makedirs(base_dir)
                        try:
                            with open(output_path, "w", encoding="utf-8") as f:
                                json.dump(reports, f, ensure_ascii=False, indent=2, default=str)
                                print("save to", output_path)
                        except Exception as e:
                            print(e)
                    if record_event:
                        logid2template = {d['log_id']: d['template'] for d in parse_results}
                        for res in result:
                            event = res["detectEvent"]
                            for i, e in enumerate(event):
                                event[i]['template'] = [logid2template[log_id] for log_id in event[i]['log_id']]
                                dt = (
                                    datetime.datetime.fromtimestamp(event[i]['timestamp']))  # 转为秒级并解析
                                event[i]['formatted_time'] = dt.strftime("%Y-%m-%d %H:%M:%S")  # 格式化输出

                            if event:
                                file_json = os.path.basename(filepath) + 'event.json'
                                output_file = os.path.join(output_base_dir, file_json)
                                if not os.path.exists(base_dir):
                                    os.makedirs(base_dir)
                                try:
                                    with open(output_file, "w", encoding="utf-8") as f:
                                        json.dump(event, f, ensure_ascii=False, indent=2, default=str)
                                        print("save to", output_file)
                                except Exception as e:
                                    print(e)
            except Exception as e:
                print("error:",filename,e)
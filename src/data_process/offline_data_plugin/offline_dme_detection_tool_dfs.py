import os
from zoneinfo import ZoneInfo

from datasets import tqdm

from src.anomaly_detection.anomaly_detection_local_dfs import AnomalyDetection
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
        # # 读取日志文件
        # log_infos = LogFileConverter.read_and_convert_file_dme(file_dir, source, module)
        # # for log_info in log_infos:
        # #     log_info["rawLog"] = log_info["rawLog"].replace("] [", "][")
        #
        # # 日志处理 模板抽取
        # parse_results = parse_logs_dem(log_infos, self.context)
        # print(f"parse_results size: {len(parse_results)}")
        # self.context.persistence_drain()

        # 异常检测
        log_repository: LogTemplateRepository = self.context.log_repository
        # 请求体中的数据只包含需要检测的最后一个时间窗的数据
        drain = self.context.get_drain(module)
        if window_size is None:
            window_size = 180000

        result = AnomalyDetection(mode=self.mode).all_detect(start_time=start_time,
                                                                           end_time=end_time,
                                                                           window_size=window_size,
                                                                           repository=log_repository,
                                                                           context=self.context,
                                                                           file_dir=file_dir)
                # print(f"file", reports)
        print(f'log detection and diagnosis finished {file_dir}')
        self.context.log_repository.clear()  # 清理日志缓存

        return result, parse_results


if __name__ == '__main__':
    modules = ['fdsa','deploy','eds','vbs','fsm','ha','osd']
    replacements = {"msg":"Messages"}
    tool = OfflineDMEDetectionToolFlash(mode="all")
    data_dir = r"\DFS_DATA_2025_filtered" #run_交行案例_template、stable_logs、交行案例_extracted_log,tempora_test, DME_MULTI_MODAL_DATA/CASE1
    data_parent_dir = r"D:\DME-Detection\dataset" + data_dir
    for dir1 in os.listdir(data_parent_dir):
        for per_batch_name in os.listdir(os.path.join(data_parent_dir,dir1)):
            record_event = True
            record_report = True
            per_batch_path = os.path.join(data_parent_dir,dir1,per_batch_name)
            for dirpath, dirnames, filenames in tqdm(os.walk(per_batch_path)):
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

                    # 读取日志文件
                    log_infos = LogFileConverter.read_and_convert_single_file(str(filepath), module, module)
                    # for log_info in log_infos:
                    #     log_info["rawLog"] = log_info["rawLog"].replace("] [", "][")

                    # 日志处理 模板抽取
                    parse_results = parse_logs_dem(log_infos, tool.context)
                    tool.context.persistence_drain()
            try:
                start_time = "2025-01-01 00:00:00"
                end_time = "2025-12-31 23:59:59"
                local_tz = ZoneInfo("Asia/Shanghai")
                start_time = datetime.datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=local_tz)
                start_time = int(start_time.timestamp() * 1000)
                end_time = datetime.datetime.strptime(end_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=local_tz)
                end_time = int(end_time.timestamp() * 1000)
                result, parse_results = tool.start_detect(None, module=module, source=module,
                                                    start_time=start_time, end_time=end_time,
                                                    window_size=600000, label=[0])
                res, report = result
                result_output_path = per_batch_path + "_output"
                if not os.path.exists(result_output_path):
                    os.makedirs(result_output_path)
                with open(os.path.join(result_output_path,"all_event_output.json"), "w", encoding="utf-8") as f:
                    json.dump(res, f, ensure_ascii=False, indent=4)
                with open(os.path.join(result_output_path,"detected_report_output.json"), "w", encoding="utf-8") as f:
                    json.dump(report, f, ensure_ascii=False, indent=4)
                print("result is saved to " + result_output_path)
            except Exception as e:
                print("error:",filename,e)
            finally:
                print("该批次缓存已清空")
                tool.context.log_repository.clear()
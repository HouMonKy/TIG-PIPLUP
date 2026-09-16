import re
import json
import traceback
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict
import pickle
import os

from src.common.global_config import GlobalConfig
from src.anomaly_detection.anomaly_detection import AnomalyDetection
from src.data_process.log_context import LogContext
from src.data_process.offline_data_plugin.dme_data_plugin.log_file_converter import LogFileConverter
from src.data_process.template_extractor.log_template_parsing_process_piplup import parse_logs_piplup_adapter
from src.data_process.template_extractor.log_template_repository import LogTemplateRepository
from src.data_process.template_extractor.parsing_algorithm.PIPLUP import LogParser


class OfflineDMEDetectionToolFlash:
    def __init__(self, mode: str):
        self.context = LogContext(product_name='DME', module_name='Messages')
        self.mode = mode

    def start_detect(self,
                     file_dir: str,
                     start_time: int,
                     end_time: int,
                     window_size: int = 180000):
        log_repository: LogTemplateRepository = self.context.log_repository

        result = AnomalyDetection.get_detector(mode=self.mode, product="DME").all_detect(
            start_time=start_time,
            end_time=end_time,
            window_size=window_size,
            repository=log_repository,
            context=self.context,
        )
        print(f'log detection and diagnosis finished: {file_dir}')
        self.context.log_repository.clear()

        return result


def extract_timestamps_from_path(target_path: Path):
    """
    从目标绝对路径中提取 YYYYMMDD 格式的日期，并增加前后缓冲时间，
    返回放宽后的毫秒级时间戳，以兼容跨天日志和时区偏移。
    """
    path_str = str(target_path)

    # 匹配连续的8位数字 (YYYYMMDD)
    match = re.search(r"(\d{8})", path_str)
    if not match:
        raise ValueError(f"路径 '{path_str}' 中未找到8位有效日期(YYYYMMDD)。")

    date_str = match.group(1)

    # 解析为基础 datetime 对象 (当天 00:00:00)
    base_dt = datetime.strptime(date_str, "%Y%m%d")

    # 加入时间缓冲 (Time Buffer)
    # 往前放宽 1 天，往后放宽 2 天 (共计 72 小时的超宽窗口)
    # 彻底解决 跨越凌晨 以及 UTC/本地时区 的偏差问题
    start_dt = base_dt - timedelta(days=1)
    end_dt = base_dt + timedelta(days=2)

    # 转换为毫秒级时间戳
    start_ts_ms = int(start_dt.timestamp() * 1000)
    end_ts_ms = int(end_dt.timestamp() * 1000)

    return start_ts_ms, end_ts_ms


def build_snapshot(result_data):
    snapshot = {}
    for res in result_data:
        module = res["module"]

        for event in res["detectEvent"]:
            ts = str(event["timestamp"])

            if ts not in snapshot:
                snapshot[ts] = {}
            if module not in snapshot[ts]:
                snapshot[ts][module] = set()

            snapshot[ts][module].update(event["template"])

    return {ts: {m: list(t_set) for m, t_set in modules.items()} for ts, modules in snapshot.items()}


def  build_snapshot_format(result_data):
    """
    按原有的 1分钟（精确到分钟）为维度进行聚合，
    增加模板频次统计与 summary 汇总字段。
    """
    snapshot = {}
    tz_utc_8 = timezone(timedelta(hours=8))

    for res in result_data:
        module = res["module"]

        for event in res["detectEvent"]:
            ts_numeric = float(event["timestamp"])

            # 恢复原逻辑：直接格式化为 YYYY-MM-DD HH:MM (保留如 16:35 的原始分钟精度)
            dt = datetime.fromtimestamp(ts_numeric, tz=tz_utc_8)
            ts_key = dt.strftime('%Y-%m-%d %H:%M')

            # 1. 初始化该分钟的结构
            if ts_key not in snapshot:
                snapshot[ts_key] = {
                    # Summary 模板，例如 "2026-04-21 16:35 异常检出{}个"
                    "summary_template": f"{ts_key} 异常检出{{}}个",
                    "total_count": 0,
                    "modules": {}
                }

            if module not in snapshot[ts_key]["modules"]:
                # 使用 defaultdict(int) 方便做频次累加
                snapshot[ts_key]["modules"][module] = defaultdict(int)

            # 2. 统计频次并累加总数
            templates = event["template"]
            # 防御性设计：确保 templates 可迭代
            if isinstance(templates, str):
                templates = [templates]

            for tmpl in templates:
                snapshot[ts_key]["modules"][module][tmpl] += 1
                snapshot[ts_key]["total_count"] += 1

    # 3. 组装并格式化最终的 JSON 结构
    final_result = {}
    for ts_key, data in snapshot.items():
        # 将 total_count 填入 summary 字符串中
        summary_str = data["summary_template"].format(data["total_count"])

        # 构建当前分钟的字典
        window_dict = {
            "summary": summary_str
        }

        # 将各模块的计数结果转为普通字典合并进去
        for mod, tmpl_counts in data["modules"].items():
            window_dict[mod] = dict(tmpl_counts)

        final_result[ts_key] = window_dict

    return final_result

def export_piplup_cache_to_txt(cache_path: str, output_path: str):
    """
    读取 PIPLUP 序列化缓存并导出至文本文件。

    参数:
    - cache_path: .pkl 缓存文件的绝对路径。
    - output_path: 目标 .txt 文件的导出路径。
    """
    if not os.path.exists(cache_path):
        print(f"Error: Cache file not found at {cache_path}")
        return

    try:
        with open(cache_path, 'rb') as f:
            cache_data = pickle.load(f)

        logCluL = cache_data.get('logCluL', [])

        with open(output_path, 'w', encoding='utf-8') as f_out:
            # 写入元数据头部
            f_out.write("PIPLUP Cache Template Export Report\n")
            f_out.write("=" * 60 + "\n")
            f_out.write(f"Export Time:    {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f_out.write(f"Source Cache:   {cache_path}\n")
            f_out.write(f"Total Clusters: {len(logCluL)}\n")
            f_out.write("=" * 60 + "\n\n")

            total_templates = 0
            for i, cluster in enumerate(logCluL):
                templates = cluster.templateList
                hit_time = getattr(cluster, 'hit_time', 0)
                total_templates += len(templates)

                # 格式化写入每个聚类簇信息
                f_out.write(f"[Cluster {i:04d}] | Hit Count: {hit_time} | Sub-templates: {len(templates)}\n")
                for j, template in enumerate(templates):
                    f_out.write(f"  |-- T{j}: {template}\n")
                f_out.write("-" * 40 + "\n")

            f_out.write(f"\n[EOF] Total Templates Exported: {total_templates}\n")

        print(f"Success: Templates exported to {output_path}")

    except Exception as e:
        print(f"Error during export: {e}")

def export_piplup_cache_to_txt_pure(cache_path: str, output_path: str):
    """
    读取 PIPLUP 序列化缓存，仅提取日志模板并以精简格式导出至文本文件。

    参数:
    - cache_path: .pkl 缓存文件的绝对路径。
    - output_path: 目标 .txt 文件的导出路径。
    """
    if not os.path.exists(cache_path):
        print(f"Error: Cache file not found at {cache_path}")
        return

    try:
        with open(cache_path, 'rb') as f:
            cache_data = pickle.load(f)

        logCluL = cache_data.get('logCluL', [])

        with open(output_path, 'w', encoding='utf-8') as f_out:
            for cluster in logCluL:
                # 获取该聚类簇下的所有子模板列表
                templates = getattr(cluster, 'templateList', [])
                for template in templates:
                    # 逐行写入纯模板文本，不做任何格式包装
                    f_out.write(f"{template}\n")

        print(f"Success: Clean templates exported to {output_path}")

    except Exception as e:
        print(f"Error during export: {e}")


def main():
    # 此处路径需根据实际执行环境配置
    data_parent_dir = Path(r"D:\DME-data\治理数据")
    if not data_parent_dir.exists():
        print(f"Target directory not found: {data_parent_dir}")
        return

    module = 'Messages'
    global_parser = LogParser(log_format='<Content>', module_name=module, use_cache=True)

    # 是否开启时间范围过滤
    is_filter_by_time_range = False

    # 默认路径配置
    project_root = GlobalConfig().get_project_root()
    persistence_dir = os.path.join(project_root, 'config', 'persistence')
    CACHE_FILE = os.path.join(persistence_dir, f"{module}_piplup_cache.pkl")
    OUTPUT_FILE = os.path.join(persistence_dir, f"{module}_piplup_templates_view.txt")
    OUTPUT_FILE_PURE = os.path.join(persistence_dir, f"{module}_piplup_templates_pure.txt")

    cnt = 0
    parse_duration = 0

    # 逐文件进行模板抽取与异常检测
    for filepath in data_parent_dir.rglob('all_merged_logs.log'):
        if not filepath.is_file():
            continue
        print(f"\nProcessing file: {filepath}")

        # 每次循环实例化一个新的 tool，确保不同文件之间的检测上下文完全隔离
        tool = OfflineDMEDetectionToolFlash(mode="all")

        # 时间范围计算与过滤逻辑
        if is_filter_by_time_range:
            try:
                # 提取文件路径中的时间戳, 返回前后 72 小时的时间段
                start_time, end_time = extract_timestamps_from_path(filepath)
                print(f"Time range filter applied -> start_time: {start_time}, end_time: {end_time}")
            except ValueError as e:
                # 遇到路径不规范且提取失败时，降级采用默认时间段
                default_start_dt = datetime(2026, 5, 15)
                start_time = int(default_start_dt.timestamp() * 1000)
                end_time = 1759248000000000000  # 结束时间保持极大值，即不设上限
                print(f"Path format invalid. Fallback to default range -> start_time: {start_time}, end_time: {end_time}")
        else:
            # 关闭时间过滤时，放开所有时间窗口边界 (0 ~ 极大值)
            start_time = 0
            end_time = 1759248000000000000
            print("Time range filter disabled. Processing all logs.")

        # ==========================================
        # 阶段一：日志读取与模板抽取
        # ==========================================

        # 记录单文件处理起始时间
        file_start_time = time.perf_counter()

        log_infos = LogFileConverter.read_and_convert_single_file(str(filepath), module, module)
        log_infos = [{**log_info, "rawLog": log_info["rawLog"].replace("] [", "][")} for log_info in log_infos]
        parse_results = parse_logs_piplup_adapter(log_infos, tool.context, start_time, end_time)

        # 记录单文件处理结束时间并计算指标
        file_end_time = time.perf_counter()
        file_duration = file_end_time - file_start_time
        file_parse_count = len(parse_results)

        # 计算单文件解析速率 (lines/s)
        file_speed = file_parse_count / file_duration if file_duration > 0 else 0.0

        print(f"parse_results size: {file_parse_count}")
        print(f"File metrics -> Time: {file_duration:.3f} s, Speed: {file_speed:.2f} lines/s")

        cnt += file_parse_count
        parse_duration += file_duration

        # ==========================================
        # 阶段二：异常检测移入循环内部，逐文件检测
        # ==========================================
        try:
            # 结果输出路径设置为 all_merged_logs.log 所在的同级目录
            result_output_path = filepath.parent

            # 传入当前文件的 start_time 和 end_time，而不是硬编码的时间
            result = tool.start_detect(
                file_dir=str(result_output_path),
                start_time=start_time,
                end_time=end_time,
                window_size=600000 # 10 minutes
            )

            output_file_snapshot = result_output_path / "snapshot.json"
            with open(output_file_snapshot, "w", encoding="utf-8") as f:
                json.dump(build_snapshot(result), f, ensure_ascii=False, indent=4)

            output_file_format = result_output_path / "snapshot_format.json"
            with open(output_file_format, "w", encoding="utf-8") as f:
                json.dump( build_snapshot_format(result), f, ensure_ascii=False, indent=4)

            print(f"Detection result is saved to: {result_output_path}")

        except Exception as e:
            print(f"Error during detection for {filepath}: {str(e)}")
            traceback.print_exc()

        print("-" * 50)

    # 记录全局解析结束时间并计算全局指标
    total_speed = cnt / parse_duration if parse_duration > 0 else 0.0

    print(f"\n================ SUMMARY ================")
    print(f"Total parsed lines: {cnt}")
    print(f"Total parsing execution time: {parse_duration:.3f} seconds")
    print(f"Global processing speed: {total_speed:.2f} lines/s")

    # 全量执行完毕后，统一保存一次全局模板缓存
    global_parser.save_cache()
    # 导出缓存文件到TXT
    export_piplup_cache_to_txt(CACHE_FILE, OUTPUT_FILE)
    export_piplup_cache_to_txt_pure(CACHE_FILE, OUTPUT_FILE_PURE)


if __name__ == '__main__':
    main()
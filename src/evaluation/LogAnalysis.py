import os
import json
import pickle
import glob
import pandas as pd
from collections import defaultdict
from itertools import chain

class LogAnalysisEvaluator:
    """
    日志异常检测结果评估器, 支持 event、template、log 三种level统计
    注：
    1、如果数据集标签有变动，要删掉CACHE_DIR文件夹重新生成缓存文件
    2、这里event level中的TP统计标准比CSV中的更严格，要求至少命中1条异常日志才算TP，CSV统计的时候不要求
    """

    NUM_GROUPS = 5
    CACHE_DIR = "cache"

    def __init__(self, dataset_dir: str, mode: str, date: str = None, skip_304case: bool = False):
        self.raw_dir = os.path.join(dataset_dir, "CN", "raw_data")
        self.base_dir = os.path.join(dataset_dir, "CN", "result")
        self.mode = mode
        allowed_modes = {"semantic", "quantitative", "parametric", "all"}
        if self.mode not in allowed_modes:
            raise ValueError(f"Invalid mode: {self.mode}, Must be one of: {', '.join(allowed_modes)}")

        if date is not None:
            self.date = date # 如果用户有指定日期，使用指定日期
        else:
            self.date = self._get_latest_date() # 如果用户没有指定，自动选最新日期
            print(f"最新可用结果：mae_detection_result_dataset_{self.date}.csv")

        self.skip_304case = skip_304case
        self.suffix = "skip_304" if self.skip_304case else "include_304"

        self.sum_events = None
        self.sum_input_lines = None
        self.sum_input_templates = None
        self.sum_anomaly_events = None
        self.sum_anomaly_lines = None
        self.sum_anomaly_templates = None
        self.cache_all = None

        self.compression_rate_template = None # 评测集中的日志总数 / 模型上报的异常模板总数
        self.compression_rate_log = None # 评测集中的日志总数 / 模型上报的异常日志总数
        os.makedirs(self.CACHE_DIR, exist_ok=True)

    def _get_latest_date(self):
        """从文件名中提取日期（如 1024），并选择最新日期"""
        dir_path = os.path.join(self.base_dir, self.mode)
        pattern = os.path.join(dir_path, "mae_detection_result_dataset_*.csv")
        files = glob.glob(pattern)
        if not files:
            raise FileNotFoundError(f"在目录 {dir_path} 中没有找到任何 CSV 文件。")

        valid_dates = []
        for f in files:
            basename = os.path.basename(f)
            date_str = basename.split("_")[-1].replace(".csv", "")
            if len(date_str) != 4:
                continue  # 忽略非四位的
            valid_dates.append(date_str)

        if not valid_dates:
            raise ValueError(f"在目录 {dir_path} 中未找到有效的日期格式文件（如 mae_detection_result_dataset_1024.csv）")

        latest_date = max(valid_dates)
        return latest_date

    @staticmethod
    def _warn(msg: str, path: str, e: Exception):
        """通用错误信息打印"""
        print(f"{msg}：{path}\n   └─ 错误详情：{e}")

    def _iterate_json_files(self, module_dir: str):
        """通用 JSON 文件遍历器"""
        if not os.path.exists(module_dir):
            raise FileNotFoundError(f"模块目录不存在，无法读取：{module_dir}")
        for file in os.listdir(module_dir):
            if not file.endswith(".json"):
                continue
            if self.skip_304case and "_304_" in file:
                continue
            file_path = os.path.join(module_dir, file)
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    yield file, json.load(f)
            except Exception as e:
                self._warn("读取文件失败", file_path, e)

    def _load_cache(self):
        """加载评测集中的异常标签缓存"""
        cache_path = os.path.join(self.CACHE_DIR, f"anomaly_data_{self.suffix}.pkl")
        if os.path.exists(cache_path):
            with open(cache_path, "rb") as f:
                self.cache_all = pickle.load(f)
            print("已加载 anomaly_data.pkl 缓存")
            self.sum_anomaly_lines = len(self.cache_all.get("anomaly_lines", []))
            self.sum_anomaly_templates = len(self.cache_all.get("anomaly_templates", []))
            return True
        return False

    def _save_cache(self):
        """保存异常标签缓存"""
        cache_path = os.path.join(self.CACHE_DIR, f"anomaly_data_{self.suffix}.pkl")
        with open(cache_path, "wb") as f:
            pickle.dump(self.cache_all, f)

    def collect_all_input_templates(self):
        cache_path = os.path.join(self.CACHE_DIR, f"input_templates_{self.suffix}.pkl")
        if os.path.exists(cache_path):
            with open(cache_path, "rb") as f:
                print("已加载 input_templates.pkl 缓存")
                self.sum_input_templates = pickle.load(f)
                return self.sum_input_templates

        # 否则重新计算
        print("正在重新计算 sum_input_templates...")
        self.sum_input_templates = self.count_unique_templates_from_txt("175cases_templateId.txt")
        with open(cache_path, "wb") as f:
            pickle.dump(self.sum_input_templates, f)
        return self.sum_input_templates


    def collect_all_label_lines(self) -> int:
        """收集所有异常日志行 ID 并缓存"""
        if self.sum_anomaly_lines is not None:
            return self.sum_anomaly_lines
        if self._load_cache() and "anomaly_lines" in self.cache_all:
            print(f"已缓存异常日志行：{self.sum_anomaly_lines}")
            return self.sum_anomaly_lines

        print("正在计算 sum_anomaly_lines...")

        results = set()
        for i in range(self.NUM_GROUPS):
            group_name = f"data_group_{i + 1}"
            file_path = os.path.join(self.raw_dir, group_name, "result_new.jsonl")
            if not os.path.exists(file_path):
                print(f"文件不存在：{file_path}")
                continue

            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    for line in f:
                        data = json.loads(line)
                        if self.skip_304case and data.get("caseNum") == 304:
                            continue
                        output_path = data.get("outputPath", "")
                        anomaly_lines = data.get("anomalyLines", [])
                        if not anomaly_lines:
                            continue

                        log_name = os.path.basename(output_path).replace(".log", "")
                        prefix = f"{group_name}_{log_name}_"
                        results.update(f"{prefix}{int(n) - 1}" for n in anomaly_lines)
            except Exception as e:
                self._warn("读取文件失败", file_path, e)

        # 缓存
        self.cache_all = {"anomaly_lines": results}
        self._save_cache()

        # 输出文件（可选）
        with open("anomalyLines_logId.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(results))

        self.sum_anomaly_lines = len(results)
        print(f"已统计异常日志总数：{self.sum_anomaly_lines}")
        return self.sum_anomaly_lines


    def collect_all_label_templates(self) -> int:
        """收集所有异常日志模板 ID 并缓存"""
        if self.sum_anomaly_templates is not None:
            return self.sum_anomaly_templates
        if self._load_cache() and "anomaly_templates" in self.cache_all:
            print(f"已缓存异常模板：{self.sum_anomaly_templates}")
            return self.sum_anomaly_templates

        # 若 anomaly_lines 尚未生成，则调用前置函数
        if not self.cache_all or "anomaly_lines" not in self.cache_all:
            self.collect_all_label_lines()
        focus_lines = self.cache_all["anomaly_lines"]

        print("正在匹配异常日志对应模板...")

        results = set()
        matched_lines = []

        with open("175cases_templateId.txt", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) < 2:
                    continue
                log_id, template_id = parts[0], parts[1]
                if log_id in focus_lines:
                    matched_lines.append(f"{log_id}\t{template_id}")
                    results.add(template_id)

        # 更新缓存
        self.cache_all["anomaly_templates"] = results
        self._save_cache()

        # 输出文件(可选）
        with open("anomalyLines_templateId.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(matched_lines))

        self.sum_anomaly_templates = len(results)
        print(f"{len(matched_lines)} 条异常日志匹配完成！异常模板总数：{self.sum_anomaly_templates}")
        return self.sum_anomaly_templates

    def count_unique_templates_from_txt(self, file_path: str) -> int:
        """读取 log_id-template_id 文件，返回唯一 template_id 数量"""
        numbers = set()
        templates = set()
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for line_num, line in enumerate(f, 1):
                    parts = line.strip().split()
                    if len(parts) < 2:
                        continue
                    log_id, template_str = parts[:2]
                    if self.skip_304case and "_304_" in log_id:
                        continue
                    try:
                        templates.add(int(template_str))
                        numbers.add(log_id)
                    except ValueError:
                        print(f"第 {line_num} 行格式错误，跳过：'{line.strip()}'")
                        continue
            print(f"文件 {os.path.basename(file_path)}：{len(numbers)} 行，{len(templates)} 模板")
            return len(templates)
        except FileNotFoundError:
            print(f"文件未找到：{file_path}")
            return 0
        except Exception as e:
            self._warn("读取文件失败", file_path, e)
            return 0

    def extract_tp_fp_by_level(self, module_dir: str):
        """从 JSON 文件中提取模板级（template-level）与日志级（log-level）的 TP/FP 唯一 ID 集合。"""
        template_level = defaultdict(set)
        log_level = defaultdict(set)

        for _, data in self._iterate_json_files(module_dir):
            for item in data:
                labels = item.get("log_label", [])
                tids = item.get("template_id", [])
                lids = item.get("log_id", [])
                for label, tid, lid in zip(labels, tids, lids):
                    template_level[label].add(tid)
                    log_level[label].add(lid)
        return dict(template_level), dict(log_level)

    def extract_event_level_tp_fp(self, module_dir: str):
        """从 JSON 文件中提取事件级（event-level）的 TP 和 FP，以 (file, timestamp) 为唯一键。"""
        event_level = defaultdict(set)
        for file, data in self._iterate_json_files(module_dir):
            for item in data:
                label = item.get("log_label", "")
                timestamp = item.get("timestamp")
                key = (file, timestamp)
                if "tp" in label:
                    event_level["tp"].add(key)
                else:
                    event_level["fp"].add(key)
        return dict(event_level)

    def collect_all_input_events(self):
        file_path = os.path.join(self.base_dir, self.mode, f"mae_detection_result_dataset_{self.date}.csv")
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"CSV 文件不存在：{file_path}")

        df = pd.read_csv(file_path, usecols=['case', 'tp', 'fp', 'tn', 'fn', 'num_anomaly_templates', 'num_input_logs'])
        if self.skip_304case:
            df = df[df["case"] != 304]

        sum_tp = df['tp'].sum()
        sum_fp = df['fp'].sum()
        sum_tn = df['tn'].sum()
        sum_fn = df['fn'].sum()
        sum_anom_templates = df['num_anomaly_templates'].sum()
        self.sum_input_lines = df['num_input_logs'].sum()
        self.sum_events = sum_tp + sum_fp + sum_tn + sum_fn
        self.sum_anomaly_events = sum_tp + sum_fn
        self.compression_rate_template = self.sum_input_lines / sum_anom_templates

    @staticmethod
    def merge_dict_set(*dicts):
        """高效合并 set 字典"""
        merged = defaultdict(set)
        for k, v in chain.from_iterable(d.items() for d in dicts):
            merged[k] |= v
        return dict(merged)

    @staticmethod
    def calc_metrics(tp: int, fp: int, total: int, total_anomaly: int) -> dict:
        """计算统计指标"""
        fn = total_anomaly - tp
        tn = total - total_anomaly - fp
        precision = tp / (tp + fp) * 100 if (tp + fp) else 0
        recall = tp / (tp + fn) * 100 if (tp + fn) else 0
        return {"tp": tp, "fp": fp, "tn": tn, "fn": fn, "precision": precision, "recall": recall}

    def print_metrics(self, level: str, result_dict: dict, total: int, total_anomaly: int):
        """打印统计指标"""
        print(f"==================== {self.mode} @ {level} =======================")
        tp_set =  result_dict.get("tp", set())
        fp_set = result_dict.get("fp", set())
        intersection = tp_set & fp_set
        if intersection:
            print("警告：检测结果中 'tp' 和 'fp' 存在交集！")
            print(f"交集项（即被同时列为真正例和假正例）：{sorted(intersection)}")
            print("请确认数据标注或推理结果是否正确，避免逻辑冲突。")
        tp = len(tp_set)
        fp = len(fp_set)
        metrics = self.calc_metrics(tp, fp, total, total_anomaly)
        for k, v in metrics.items():
            if isinstance(v, float):
                print(f"{k}: {v:.2f}%")
            else:
                print(f"{k}: {v}")
        if "log level" in level.lower():
            self.compression_rate_log = total / (tp + fp) if (tp + fp) else 0
            print(f"==================== 压缩比 =======================")
            print(f"评测集中的日志总数 / 模型上报的异常模板总数 = {self.compression_rate_template:.2f}")
            print(f"评测集中的日志总数 / 模型上报的异常日志总数 = {self.compression_rate_log:.2f}")

    def evaluate(self):
        print("开始日志异常检测评估流程...")

        self.collect_all_input_events()
        self.collect_all_input_templates()
        self.collect_all_label_lines()
        self.collect_all_label_templates()

        event_dict = self.extract_event_level_tp_fp(os.path.join(self.base_dir, self.mode, "detect_label"))
        t, l = self.extract_tp_fp_by_level(os.path.join(self.base_dir, self.mode, "detect_label"))

        self.print_metrics("event level", event_dict, self.sum_events, self.sum_anomaly_events)
        self.print_metrics("template level", t, self.sum_input_templates, self.sum_anomaly_templates)
        self.print_metrics("log level", l, self.sum_input_lines, self.sum_anomaly_lines)

        print(f"{self.mode} 模块在MAE-CN数据集({self.suffix})上的评估流程完成！")
        return None
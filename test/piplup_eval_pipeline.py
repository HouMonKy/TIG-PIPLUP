import os
import re
import time
import pandas as pd
from pathlib import Path
from typing import List

from src.data_process.log_context import LogContext
from src.data_process.template_extractor.log_template_parsing_process import parse_logs_dem_with_csv
from src.data_process.template_extractor.log_template_parsing_process_piplup import parse_logs_piplup_only
from src.evaluation.ParserMetrics import calculate_metrics_normalized, normalize_template
from src.data_process.template_extractor.parsing_algorithm.PIPLUP import LogParser

TEST_MODULE = 'Messages'

def apply_top5_calibration(df_gt: pd.DataFrame) -> pd.DataFrame:
    """
    定向真值修正：将 Drain3 (伪GT) 中已知的、存在边界切分错误的模板，
    替换为人工核对后的标准 Ground Truth 模板。
    """
    correction_map = {
        # 修正 1：恢复被 Drain3 错误泛化的系统级函数名 (欠拟合)
        "PROTOCOL : MOD_BASE : IDNAME : <*> failed : <NUM>": "PROTOCOL : MOD_BASE : IDNAME : Nfs4CreateUpdateIdNamehash failed : <NUM>",

        # 修正 2：恢复被 Drain3 错误泛化的另一个函数名及键名(过拟合)
        "PROTOCOL : MOD_BASE : IDNAME : <*> failed for <*> <NUM>": "PROTOCOL : MOD_BASE : IDNAME : Nfs4IdMapGidToGnameInner failed for gId <*>",

        # 修正 3：泛化被 Drain3 错误保留的 64 位连续十六进制实例 ID (过拟合)
        "VAF : ( INST : : ffffffffffffffff ) Gns look up start name ( <*> ) .": "VAF : ( INST : : <*> ) Gns look up start name ( <*> ) .",

        # 修正 4：修复 Drain3 因硬切分导致十六进制参数块断裂的问题 (欠拟合)
        "SPC : ( <NUM> : 4c70 ) <MAC-HEX>.": "SPC : ( <NUM> : <*> ) <MAC-HEX>."

        # 注：'Search tracepoint...' 未纳入修正字典，作为 PIPLUP 真实的错误接受客观惩罚。
    }

    df_gt_calibrated = df_gt.copy()
    df_gt_calibrated['EventTemplate'] = df_gt_calibrated['EventTemplate'].replace(correction_map)

    # 统计并打印修正命中的行数，确保修正字典生效
    calibrated_rows = (df_gt['EventTemplate'] != df_gt_calibrated['EventTemplate']).sum()
    print(f"  [数据修正] 已在基准集中定向修复 {calibrated_rows} 行脏标签 (Label Noise)。")

    return df_gt_calibrated

def load_and_merge_test_data(base_dir: str, scenario_prefix: str = None) -> List[dict]:
    """
    递归扫描目标目录，读取所有 'all_merged_logs.log' 并拼装为 List[dict] 结构。
    :param base_dir: 数据根目录
    :param scenario_prefix: 可选的场景号前缀（如 "01"），仅加载该场景号下的日志，
                            通过匹配顶层子目录名以 "{prefix}_" 开头来过滤。
    """
    base_path = Path(base_dir)

    # 治理数据各批次统一读取 all_merged_messages_logs.log
    TARGET_LOG_NAME = "all_merged_messages_logs.log"

    # 若指定了场景号前缀，则只扫描以 "{prefix}_" 开头的顶层子目录，避免重复加载全量数据
    if scenario_prefix is not None:
        prefix_with_sep = f"{scenario_prefix}_"
        candidate_dirs = [p for p in base_path.iterdir()
                          if p.is_dir() and p.name.startswith(prefix_with_sep)]
        target_files = []
        for d in candidate_dirs:
            target_files.extend(d.rglob("all_merged_logs.log"))
            target_files.extend(d.rglob(TARGET_LOG_NAME))
    else:
        target_files = list(base_path.rglob("all_merged_logs.log"))
        # 注意：不能直接覆盖，两种文件名可能并存（原实现会静默丢弃 all_merged_logs.log）
        seen = {str(f) for f in target_files}
        for extra in base_path.rglob(TARGET_LOG_NAME):
            if str(extra) not in seen:
                target_files.append(extra)

    if not target_files:
        print(f"[数据加载] 在路径 {base_dir} 下未匹配到目标文件。")
        return []

    print(f"[数据加载] 扫描到 {len(target_files)} 个目标文件，开始执行内存级聚合...")

    merged_log_info = []
    global_log_id = 1

    for file_path in target_files:
        try:
            # 采用 errors='ignore' 规避非标控制字符或截断引起的解码异常
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    log_entry = {
                        "logId": global_log_id,
                        "module": TEST_MODULE,  # 按需替换
                        "source": TEST_MODULE,  # 按需替换
                        "rawLog": line
                    }

                    merged_log_info.append(log_entry)
                    global_log_id += 1

        except Exception as e:
            print(f"[异常] 文件 {file_path} 读取失败，错误类型：{type(e).__name__}")

    print(f"[数据加载] 聚合完成。合计载入 {len(merged_log_info)} 条物理行。")
    return merged_log_info

def execute_parsing_pipeline(log_info: List[dict], dem_context, piplup_parser, piplup_context, dataset_name: str, csv_dir: str = None):
    """
    解析执行管线：基于数据集名称生成独立的数据切片文件。
    :param csv_dir: CSV 输出目录，默认为当前工作目录（None）。
    """
    if csv_dir:
        path_gt = os.path.join(csv_dir, f"drain3_result_{dataset_name}.csv")
        path_pred = os.path.join(csv_dir, f"piplup_result_{dataset_name}.csv")
    else:
        path_gt = f"drain3_result_{dataset_name}.csv"
        path_pred = f"piplup_result_{dataset_name}.csv"

    # [阶段一] Drain3 基线执行
    if os.path.exists(path_gt):
        print(f"[{dataset_name}] 检测到 {path_gt}，跳过 Drain3 解析。")
    else:
        print(f"[{dataset_name}] 运行 Drain3 解析并保存到CSV文件...")

        start_time = time.time()
        parse_logs_dem_with_csv(
            log_info=log_info,
            context=dem_context,
            save_csv=True,
            csv_path=path_gt
        )
        elapsed = time.time() - start_time
        log_count = len(log_info)
        throughput = log_count / elapsed if elapsed > 0 else 0

        print(f"[{dataset_name}] Drain3 解析完成: {log_count} 条日志, 耗时 {elapsed:.2f}s, 吞吐量 {throughput:.1f} 条/秒")

    # [阶段二] PIPLUP (Python) 推断执行
    if os.path.exists(path_pred):
        print(f"[{dataset_name}] 检测到 {path_pred}，跳过 PIPLUP (Python) 解析。")
    else:
        print(f"[{dataset_name}] 运行 PIPLUP 解析并保存到CSV文件...")

        start_time = time.time()
        parse_logs_piplup_only(
            log_info=log_info,
            parser=piplup_parser,
            context=piplup_context,
            use_format=True,
            save_csv=True,
            csv_path=path_pred
        )
        elapsed = time.time() - start_time
        log_count = len(log_info)
        throughput = log_count / elapsed if elapsed > 0 else 0

        print(f"[{dataset_name}] PIPLUP 解析完成: {log_count} 条日志, 耗时 {elapsed:.2f}s, 吞吐量 {throughput:.1f} 条/秒")

        # 保存解析树缓存（export_cache_report 方法已从 LogParser 移除，不再调用）
        piplup_parser.save_cache()

# ==========================================
# 2. 评估输出对齐：在日志头标明当前数据集
# ==========================================
def compare_csv_metrics(path_gt: str, path_pred: str, name_gt: str, name_pred: str, dataset_name: str) -> dict:
    try:
        df_gt = pd.read_csv(path_gt)
        df_pred = pd.read_csv(path_pred)
    except FileNotFoundError as e:
        print(f"[跳过] 缺失 {dataset_name} 的评估文件: {e}")
        return {}

    # 1. 注入真值修正逻辑 (仅当以 Drain3 作为基线时才触发)
    if name_gt == "Drain3":
        df_gt = apply_top5_calibration(df_gt)

    # 2. 交集对齐逻辑
    intersect_lines = set(df_gt['LineId']).intersection(set(df_pred['LineId']))
    if len(intersect_lines) < len(df_gt) or len(intersect_lines) < len(df_pred):
        df_gt = df_gt[df_gt['LineId'].isin(intersect_lines)]
        df_pred = df_pred[df_pred['LineId'].isin(intersect_lines)]

    df_gt = df_gt.sort_values("LineId").reset_index(drop=True)
    df_pred = df_pred.sort_values("LineId").reset_index(drop=True)

    # 3. 计算并输出归一化指标
    metrics = calculate_metrics_normalized(df_gt, df_pred)

    title_suffix = " (经 GT 局部真值修正)" if name_gt == "Drain3" else ""
    print(f"\n=== [数据集: {dataset_name}] 评估报告: {name_pred} vs {name_gt}{title_suffix} ===")
    print(f"Grouping Accuracy (GA): {metrics['GA']:.4f}")
    print(f"Parsing Accuracy  (PA): {metrics['PA']:.4f}")
    print(f"F1 of Grouping   (FGA): {metrics['FGA']:.4f}")
    print(f"F1 of Template   (FTA): {metrics['FTA']:.4f}")

    return metrics


def print_top5_error_analysis(path_gt: str, path_pred: str, name_gt: str, name_pred: str, dataset_name: str):
    """
    双维度算法差异探测：
    1. GA Mismatch (聚类分群差异)：日志被分配到了错误的簇。
    2. PA Mismatch (边界切分差异)：分群正确，但常量/变量的提取边界不同。
    """
    # 1. 增加防御性读取，防止特定批次文件缺失阻断主流程
    try:
        df_gt = pd.read_csv(path_gt)
        df_pred = pd.read_csv(path_pred)
    except FileNotFoundError as e:
        print(f"[跳过差异分析] 缺失 {dataset_name} 的评估文件: {e}")
        return

    # 2. 兼容对齐 Short Template 标记
    if 'EventTemplate' in df_pred.columns:
        df_pred['EventTemplate'] = df_pred['EventTemplate'].astype(str).str.replace('<Short Template>', '<*>')
    if 'EventTemplate' in df_gt.columns:
        df_gt['EventTemplate'] = df_gt['EventTemplate'].astype(str).str.replace('<Short Template>', '<*>')

    # 3. 构造归一化对比列
    df_gt['NormTemplate'] = df_gt['EventTemplate'].astype(str).apply(normalize_template)
    df_pred['NormTemplate'] = df_pred['EventTemplate'].astype(str).apply(normalize_template)

    merged = pd.merge(df_gt, df_pred, on='LineId', suffixes=(f'_{name_gt}', f'_{name_pred}'))

    if merged.empty:
        print(f"\n[差异分析警告] 数据集 {dataset_name} 行对齐交集为空。")
        return

    # 4. 提取分群一致的映射拓扑
    col_event_gt = f'EventId_{name_gt}'
    col_event_pred = f'EventId_{name_pred}'
    col_norm_gt = f'NormTemplate_{name_gt}'
    col_norm_pred = f'NormTemplate_{name_pred}'
    col_raw_gt = f'EventTemplate_{name_gt}'
    col_raw_pred = f'EventTemplate_{name_pred}'

    # 建立主流映射：GT 中的簇对应 Pred 中的哪个簇（取众数）
    cluster_mapping = merged.groupby(col_event_gt)[col_event_pred].agg(lambda x: x.mode()[0] if not x.empty else None)
    merged['Is_Same_Cluster'] = merged[col_event_pred] == merged[col_event_gt].map(cluster_mapping)

    # ==========================================
    # 模块 A：GA Mismatch (聚类分群差异) 分析
    # ==========================================
    ga_mismatched = merged[~merged['Is_Same_Cluster']]

    if ga_mismatched.empty:
        print(f"\n=== [数据集: {dataset_name}] Top 5 聚类分群差异 (GA Mismatch) [{name_pred} vs {name_gt}] ===")
        print("未发现聚类分群差异 (GA 高度一致)。")
    else:
        # 按 GT 的原始模板频次统计，找出最常被分错类的 Top 5 模板
        top_ga_diffs = ga_mismatched[col_raw_gt].value_counts().head(5).index
        print(f"\n=== [数据集: {dataset_name}] Top 5 聚类分群差异 (GA Mismatch) [{name_pred} vs {name_gt}] ===")
        for template_gt in top_ga_diffs:
            subset = ga_mismatched[ga_mismatched[col_raw_gt] == template_gt]
            gt_id = subset[col_event_gt].iloc[0]

            # 找到 Pred 将其错误分配到的最主要簇
            pred_id = subset[col_event_pred].value_counts().index[0]
            sample_row = subset[subset[col_event_pred] == pred_id].iloc[0]

            expected_pred_id = cluster_mapping.get(gt_id, "Unknown")

            print(f"\n[{name_gt} 聚类: {gt_id}] : {sample_row[col_raw_gt]}")
            print(f"[{name_pred} 聚类: {pred_id}] : {sample_row[col_raw_pred]}")
            print(
                f"  -> 诊断: {name_gt}将此日志归为簇[{gt_id}]，其主流对齐簇应为{name_pred}的[{expected_pred_id}]。但当前日志被{name_pred}异化分配到了簇[{pred_id}]。")

    # ==========================================
    # 模块 B：PA Mismatch (解析边界差异) 分析
    # ==========================================
    pa_mismatched = merged[(merged[col_norm_gt] != merged[col_norm_pred]) & merged['Is_Same_Cluster']]

    if pa_mismatched.empty:
        print(f"\n=== [数据集: {dataset_name}] Top 5 边界切分差异 (PA Mismatch) [{name_pred} vs {name_gt}] ===")
        print("未发现核心算法边界差异 (PA 解析边界高度一致)。")
    else:
        top_pa_diffs = pa_mismatched[col_raw_gt].value_counts().head(5).index
        print(f"\n=== [数据集: {dataset_name}] Top 5 边界切分差异 (PA Mismatch) [{name_pred} vs {name_gt}] ===")
        for template_gt in top_pa_diffs:
            sample_row = pa_mismatched[pa_mismatched[col_raw_gt] == template_gt].iloc[0]
            print(f"\n[{name_gt} 原始] : {sample_row[col_raw_gt]}")
            print(f"[{name_pred} 原始] : {sample_row[col_raw_pred]}")
            print(f"  -> {name_gt} 归一化: {sample_row[col_norm_gt]}")
            print(f"  -> {name_pred} 归一化: {sample_row[col_norm_pred]}")


# ==========================================
# 3. 主函数：使用配置化驱动多路测试
# ==========================================
if __name__ == "__main__":

    # ============================================================
    # [全量测试] 治理数据 01~58 场景遍历（已注释，按需恢复）
    # ============================================================
    # # 注册多路测试数据集字典
    # # 扩展时只需在此处追加 Key-Value 映射即可
    # # 测试数据根目录与场景号映射：value 为 (数据根目录, 场景号前缀)
    # # 治理数据中每个场景号对应 2 个批次目录，会自动聚合其下所有 all_merged_messages_logs.log
    # GOVERNED_DATA_ROOT = r"D:\DME-data\治理数据"
    # # 全量覆盖治理数据（58 个场景前缀 01~58，每个前缀下 2 个批次目录，自动聚合其下所有 all_merged_messages_logs.log）
    # DATASET_NUMBERS = [f"{i:02d}" for i in range(1, 59)]
    # TEST_DATASETS = {num: (GOVERNED_DATA_ROOT, num) for num in DATASET_NUMBERS}
    #
    # # 初始化全局解析器上下文 (若涉及模型状态变更，需在循环内重置或独立实例化)
    # dem_context = LogContext()
    # piplup_context = LogContext()
    # piplup_parser = LogParser(log_format='<Content>', module_name=TEST_MODULE, use_cache=True, br_thresh=2)
    #
    # # 遍历执行测试管线
    # for dataset_name, (target_directory, scenario_prefix) in TEST_DATASETS.items():
    #     print(f"\n" + "=" * 50)
    #     print(f"开始执行批次测试: {dataset_name}")
    #     print(f"=" * 50)
    #
    #     # 数据加载
    #     if not os.path.exists(target_directory):
    #         print(f"[路径错误] 未找到物理路径 {target_directory}，跳过此批次。")
    #         continue
    #
    #     log_infos = load_and_merge_test_data(target_directory, scenario_prefix)
    #     if not log_infos:
    #         continue
    #
    #     log_infos = [{**log_info, "rawLog": log_info["rawLog"].replace("] [", "][")} for log_info in log_infos]
    #
    #     # 声明此批次依赖的文件路径
    #     path_drain3 = f"drain3_result_{dataset_name}.csv"
    #     path_piplup_py = f"piplup_result_{dataset_name}.csv"
    #     path_piplup_rust = f"structured_{dataset_name}.csv"
    #
    #     try:
    #         # 1. 触发解析引擎生成
    #         execute_parsing_pipeline(log_infos, dem_context, piplup_parser, piplup_context, dataset_name)
    #
    #         # 2. 评估组 A：PIPLUP (Python) vs Drain3 (基线)
    #         compare_csv_metrics(path_drain3, path_piplup_py, "Drain3", "PIPLUP-Py", dataset_name)
    #         # 打印具体差异
    #         print_top5_error_analysis(path_drain3, path_piplup_py, "Drain3", "PIPLUP-Py", dataset_name)
    #
    #         # 3. 评估组 B：PIPLUP (Rust) vs PIPLUP (Python) (验证语义对齐)
    #         if os.path.exists(path_piplup_rust):
    #             compare_csv_metrics(path_piplup_py, path_piplup_rust, "PIPLUP-Py", "PIPLUP-Rust", dataset_name)
    #             # 打印具体差异
    #             print_top5_error_analysis(path_piplup_py, path_piplup_rust, "PIPLUP-Py", "PIPLUP-Rust", dataset_name)
    #         else:
    #             print(f"\n[跳过] 未检测到 Rust 输出文件 {path_piplup_rust}。")
    #
    #     except Exception as e:
    #         print(f"[异常中断] 数据集 {dataset_name} 执行失败: {e}")

    # ============================================================
    # [抽样测试] D:\DME-data\test 目录下已预抽样数据
    # 目录结构: D:\DME-data\test\Messages\all_merged_messages_logs.log
    # ============================================================
    SAMPLE_DATA_ROOT = r"D:\DME-data\test"
    SAMPLE_DATASET_NAME = "sample_test"

    # 抽样数据读取：test目录下为 Messages/all_merged_messages_logs.txt，
    # 与全量测试的 {scenario_prefix}_*/**/ 结构不同，直接从子目录读取
    def load_sample_test_data(base_dir: str) -> List[dict]:
        base_path = Path(base_dir)
        TARGET_LOG_NAME = "all_merged_messages_logs.txt"

        # 抽样数据目录结构：base_dir/Messages/all_merged_messages_logs.txt
        target_files = list(base_path.rglob(TARGET_LOG_NAME))

        if not target_files:
            print(f"[数据加载] 在路径 {base_dir} 下未匹配到目标文件。")
            return []

        print(f"[数据加载] 扫描到 {len(target_files)} 个目标文件，开始执行内存级聚合...")

        merged_log_info = []
        global_log_id = 1

        for file_path in target_files:
            try:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue

                        log_entry = {
                            "logId": global_log_id,
                            "module": TEST_MODULE,
                            "source": TEST_MODULE,
                            "rawLog": line
                        }

                        merged_log_info.append(log_entry)
                        global_log_id += 1

            except Exception as e:
                print(f"[异常] 文件 {file_path} 读取失败，错误类型：{type(e).__name__}")

        print(f"[数据加载] 聚合完成。合计载入 {len(merged_log_info)} 条物理行。")
        return merged_log_info

    # 初始化解析器上下文
    dem_context = LogContext()
    piplup_context = LogContext()
    piplup_parser = LogParser(log_format='<Content>', module_name=TEST_MODULE, use_cache=True, br_thresh=2)

    print(f"\n" + "=" * 50)
    print(f"开始执行抽样测试: {SAMPLE_DATASET_NAME}")
    print(f"=" * 50)

    # 数据加载
    if not os.path.exists(SAMPLE_DATA_ROOT):
        print(f"[路径错误] 未找到物理路径 {SAMPLE_DATA_ROOT}，退出。")
    else:
        log_infos = load_sample_test_data(SAMPLE_DATA_ROOT)
        if log_infos:
            log_infos = [{**log_info, "rawLog": log_info["rawLog"].replace("] [", "][")} for log_info in log_infos]

            # 抽样测试输出文件保存到 test 目录下
            path_drain3 = os.path.join(SAMPLE_DATA_ROOT, f"drain3_result_{SAMPLE_DATASET_NAME}.csv")
            path_piplup_py = os.path.join(SAMPLE_DATA_ROOT, f"piplup_result_{SAMPLE_DATASET_NAME}.csv")
            path_piplup_rust = os.path.join(SAMPLE_DATA_ROOT, f"structured_{SAMPLE_DATASET_NAME}.csv")

            try:
                # 1. 触发解析引擎生成（使用绝对路径覆盖内部默认的相对路径）
                execute_parsing_pipeline(log_infos, dem_context, piplup_parser, piplup_context,
                                        SAMPLE_DATASET_NAME, csv_dir=SAMPLE_DATA_ROOT)

                # 2. 评估组 A：PIPLUP (Python) vs Drain3 (基线)
                compare_csv_metrics(path_drain3, path_piplup_py, "Drain3", "PIPLUP-Py", SAMPLE_DATASET_NAME)
                print_top5_error_analysis(path_drain3, path_piplup_py, "Drain3", "PIPLUP-Py", SAMPLE_DATASET_NAME)

                # 3. 评估组 B：PIPLUP (Rust) vs PIPLUP (Python) (验证语义对齐)
                if os.path.exists(path_piplup_rust):
                    compare_csv_metrics(path_piplup_py, path_piplup_rust, "PIPLUP-Py", "PIPLUP-Rust", SAMPLE_DATASET_NAME)
                    print_top5_error_analysis(path_piplup_py, path_piplup_rust, "PIPLUP-Py", "PIPLUP-Rust", SAMPLE_DATASET_NAME)
                else:
                    print(f"\n[跳过] 未检测到 Rust 输出文件 {path_piplup_rust}。")

            except Exception as e:
                print(f"[异常中断] 抽样测试执行失败: {e}")
"""模块3：答案生成和对比 — 生成两版 PIPLUP 的 cluster+result 文件，基于 GT 评估 PA/GA/FTA/FGA。

CLI:
    python -m eval_pipeline answer \
        --input <jsonl_or_folder> \
        --piplup-orig <python_module_path> \
        --piplup-opt <python_module_path> \
        [--postprocessing-orig <python_module_path>] \
        --gt <gt_jsonl_path> \
        --output <result_dir> \
        [--scenarios 01,02,...] [--module Messages]

流程：
1. 对每版 PIPLUP 各跑一次 parse_logs_piplup_adapter（子进程隔离），保存 result.jsonl + clusters.json
2. 加载 GT（来自模块4输出 final_gt.jsonl）
3. 用 calculate_metrics_normalized() 评估两版结果 vs GT
4. 输出 eval_report.json
"""
import argparse
import json
import sys
import os
import gc
import time
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from eval_pipeline._shared.data_loader import load_jsonl, write_jsonl, load_json, write_json
from eval_pipeline._shared.metrics import calculate_metrics_normalized


def _run_piplup_and_save(input_path, parser_module_path, postprocessing_orig_path, output_dir,
                         module_name, cold_start, scenarios, data_root, label):
    """运行 PIPLUP 并保存 result.jsonl + clusters.json。子进程隔离。"""
    from multiprocessing import Process, Pipe

    parent_conn, child_conn = Pipe()
    p = Process(
        target=_piplup_worker,
        args=(child_conn, input_path, parser_module_path, postprocessing_orig_path,
              module_name, cold_start, scenarios, data_root),
    )
    p.start()
    result = parent_conn.recv()
    p.join()
    parent_conn.close()

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    results = result['results']
    clusters = result['clusters']

    result_path = out / f"{label}_result.jsonl"
    with open(result_path, 'w', encoding='utf-8') as f:
        for r in results:
            f.write(json.dumps({
                'log_id': r['log_id'],
                'template': r['template'],
                'template_id': r['template_id'],
            }, ensure_ascii=False) + '\n')

    cluster_path = out / f"{label}_clusters.json"
    with open(cluster_path, 'w', encoding='utf-8') as f:
        json.dump(clusters, f, ensure_ascii=False, indent=2)

    print(f"  {label}: {len(results)} 行, {len(clusters)} 簇 → {result_path.name}, {cluster_path.name}")
    return results, clusters


def _piplup_worker(conn, input_path, parser_module_path, postprocessing_orig_path,
                   module_name, cold_start, scenarios, data_root):
    """子进程 worker：运行 PIPLUP 并返回 results + clusters。"""
    import importlib, sys
    from pathlib import Path

    project_root = Path(__file__).resolve().parent.parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    from eval_pipeline._shared.data_loader import load_jsonl, load_raw_lines, load_raw_lines_by_scenarios, build_log_info
    from src.data_process.log_context import LogContext
    from src.data_process.template_extractor.parsing_algorithm import postprocessing
    from src.data_process.template_extractor.log_template_parsing_process_piplup import parse_logs_piplup_adapter
    from src.common.global_config import GlobalConfig

    # 加载数据
    if input_path and str(input_path).endswith('.jsonl'):
        log_info = load_jsonl(input_path)
    elif scenarios:
        raw_lines = load_raw_lines_by_scenarios(data_root, scenarios)
        log_info = build_log_info(raw_lines, module_name)
        del raw_lines
    else:
        raw_lines = load_raw_lines(input_path)
        log_info = build_log_info(raw_lines, module_name)
        del raw_lines

    # 清缓存
    cache_file = os.path.join(GlobalConfig.get_project_root(), "config", "persistence", f"{module_name}_piplup_cache.pkl")
    if os.path.exists(cache_file):
        for attempt in range(5):
            try:
                os.remove(cache_file)
                break
            except PermissionError:
                time.sleep(0.5 * (attempt + 1))
    gc.collect()

    # monkey-patch 原版 postprocessing
    _saved = None
    if postprocessing_orig_path:
        postprocessing_orig_mod = importlib.import_module(postprocessing_orig_path)
        _saved = postprocessing.correct_single_template
        postprocessing.correct_single_template = postprocessing_orig_mod.correct_single_template

    # 运行
    parser_class = importlib.import_module(parser_module_path).LogParser
    context = LogContext(module_name=module_name)
    context.parser_piplup = parser_class(
        log_format='<Content>', module_name=module_name,
        use_cache=(not cold_start)
    )

    results = parse_logs_piplup_adapter(log_info, context)

    if _saved is not None:
        postprocessing.correct_single_template = _saved

    # 构建 cluster 数据
    clusters = []
    for idx, cluster in enumerate(context.parser_piplup.logCluL):
        clusters.append({
            'cluster_id': idx,
            'template': cluster.templateList[0] if cluster.templateList else '',
            'hit_count': cluster.hit_time,
            'num_templates': len(cluster.templateList),
        })

    result_list = []
    for r in results:
        result_list.append({
            'log_id': r['log_id'],
            'template': r['template'],
            'template_id': r['template_id'],
        })

    conn.send({'results': result_list, 'clusters': clusters})
    conn.close()


def _to_eval_dataframe(records: list) -> pd.DataFrame:
    """将 result 记录列表转为评估用 DataFrame（LineId, EventTemplate, EventId）。"""
    rows = []
    for r in records:
        rows.append({
            'LineId': int(r['log_id']),
            'EventTemplate': r['template'],
            'EventId': str(r['template_id']),
        })
    return pd.DataFrame(rows)


def _gt_to_eval_dataframe(gt_records: list) -> pd.DataFrame:
    """将 GT 记录列表转为评估用 DataFrame（LineId, EventTemplate, EventId）。"""
    rows = []
    for r in gt_records:
        rows.append({
            'LineId': int(r['log_id']),
            'EventTemplate': r['template'],
            'EventId': str(r['template_id']),
        })
    return pd.DataFrame(rows)


def generate_answers(
    input_path: str,
    piplup_orig_path: str,
    piplup_opt_path: str,
    gt_path: str,
    output_dir: str,
    postprocessing_orig_path: str = None,
    cold_start: bool = True,
    module_name: str = "Messages",
    scenarios: list = None,
    data_root: str = None,
):
    """答案生成和评估主流程。"""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # ===== Step 1: 运行两版 PIPLUP =====
    print(f"\n{'='*60}")
    print(f"  Step 1: 运行两版 PIPLUP 生成 cluster + result")
    print(f"{'='*60}")

    results_orig, clusters_orig = _run_piplup_and_save(
        input_path, piplup_orig_path, postprocessing_orig_path,
        out, module_name, cold_start, scenarios, data_root, "piplup_orig")

    results_opt, clusters_opt = _run_piplup_and_save(
        input_path, piplup_opt_path, None,
        out, module_name, cold_start, scenarios, data_root, "piplup_opt")

    # ===== Step 2: 加载 GT 并评估 =====
    print(f"\n{'='*60}")
    print(f"  Step 2: 加载 GT 并评估 PA/GA/FTA/FGA")
    print(f"{'='*60}")

    gt_records = load_jsonl(gt_path)
    print(f"  GT: {gt_path} ({len(gt_records)} 条)")

    df_gt = _gt_to_eval_dataframe(gt_records)

    # 评估原版
    df_orig = _to_eval_dataframe(results_orig)
    # 对齐 LineId（GT 和 result 需要相同行数）
    common_ids = set(df_gt['LineId']) & set(df_orig['LineId'])
    df_gt_aligned = df_gt[df_gt['LineId'].isin(common_ids)].sort_values('LineId').reset_index(drop=True)
    df_orig_aligned = df_orig[df_orig['LineId'].isin(common_ids)].sort_values('LineId').reset_index(drop=True)
    metrics_orig = calculate_metrics_normalized(df_gt_aligned, df_orig_aligned)

    # 评估优化版
    df_opt = _to_eval_dataframe(results_opt)
    df_opt_aligned = df_opt[df_opt['LineId'].isin(common_ids)].sort_values('LineId').reset_index(drop=True)
    metrics_opt = calculate_metrics_normalized(df_gt_aligned, df_opt_aligned)

    print(f"  PIPLUP原版:   PA={metrics_orig['PA']:.4f}, GA={metrics_orig['GA']:.4f}, "
          f"FTA={metrics_orig['FTA']:.4f}, FGA={metrics_orig['FGA']:.4f}")
    print(f"  PIPLUP优化版: PA={metrics_opt['PA']:.4f}, GA={metrics_opt['GA']:.4f}, "
          f"FTA={metrics_opt['FTA']:.4f}, FGA={metrics_opt['FGA']:.4f}")

    # ===== Step 3: 保存评估报告 =====
    eval_report = {
        "piplup_orig": metrics_orig,
        "piplup_opt": metrics_opt,
        "gt_path": str(gt_path),
        "gt_count": len(gt_records),
        "evaluated_lines": len(common_ids),
    }

    report_path = out / "eval_report.json"
    write_json(report_path, eval_report)
    print(f"\n  评估报告已保存: {report_path}")

    # 汇总
    print(f"\n{'='*60}")
    print(f"  评估结果汇总")
    print(f"{'='*60}")
    print(f"  {'算法':<16s}  {'PA':>8s}  {'GA':>8s}  {'FTA':>8s}  {'FGA':>8s}")
    print(f"  {'-'*16}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*8}")
    print(f"  {'PIPLUP原版':<16s}  {metrics_orig['PA']:>8.4f}  {metrics_orig['GA']:>8.4f}  {metrics_orig['FTA']:>8.4f}  {metrics_orig['FGA']:>8.4f}")
    print(f"  {'PIPLUP优化版':<16s}  {metrics_opt['PA']:>8.4f}  {metrics_opt['GA']:>8.4f}  {metrics_opt['FTA']:>8.4f}  {metrics_opt['FGA']:>8.4f}")

    return eval_report


def main():
    parser = argparse.ArgumentParser(description="答案生成和对比：生成 cluster+result + 评估")
    parser.add_argument("--input", required=True, help="输入 JSONL 文件或文件夹路径")
    parser.add_argument("--piplup-orig", required=True, help="PIPLUP 原版模块路径")
    parser.add_argument("--piplup-opt", required=True, help="PIPLUP 优化版模块路径")
    parser.add_argument("--postprocessing-orig", default=None, help="原版后处理模块路径")
    parser.add_argument("--gt", required=True, help="GT JSONL 文件路径（模块4输出 final_gt.jsonl）")
    parser.add_argument("--output", required=True, help="结果输出目录")
    parser.add_argument("--module", default="Messages", help="日志模块名")
    parser.add_argument("--cold-start", action="store_true", default=True, help="冷启动（清缓存）")
    parser.add_argument("--scenarios", default=None, help="场景列表，逗号分隔")
    parser.add_argument("--data-root", default=r"D:\DME-data\单个日志模块", help="数据根目录")

    args = parser.parse_args()

    scenarios = None
    if args.scenarios:
        scenarios = [s.strip() for s in args.scenarios.split(',')]

    generate_answers(
        input_path=args.input,
        piplup_orig_path=args.piplup_orig,
        piplup_opt_path=args.piplup_opt,
        gt_path=args.gt,
        output_dir=args.output,
        postprocessing_orig_path=args.postprocessing_orig,
        cold_start=args.cold_start,
        module_name=args.module,
        scenarios=scenarios,
        data_root=args.data_root,
    )


if __name__ == '__main__':
    main()

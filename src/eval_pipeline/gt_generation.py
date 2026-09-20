"""模块4：GT 生成 — 对比两版 PIPLUP 的 cluster，一致的记录为 GT，不一致的送 LLM 裁决。

CLI:
    python -m eval_pipeline gt \
        --input <jsonl_or_folder> \
        --piplup-orig <python_module_path> \
        --piplup-opt <python_module_path> \
        [--postprocessing-orig <python_module_path>] \
        --llm-config <yaml_path> \
        --output <gt_dir> \
        [--scenarios 01,02,...] [--module Messages] \
        [--limit N] [--dry-run] [--retry-failed] [--live]

流程：
1. 对两版 PIPLUP 各跑一次 parse_logs_piplup_adapter（子进程隔离），生成各自 result.jsonl + clusters.json
2. cluster 对比：按行对齐两版 result，一致的 cluster 直接记入 GT，不一致的收集 sample_log 送 LLM 裁决
3. LLM 裁决：串行执行 + 限流自适应 + 断点续跑
4. 输出：all_gt_tasks.jsonl + resolved_results.jsonl + final_gt.jsonl + gt_summary.json
"""
import argparse
import json
import sys
import os
import gc
import time
import logging
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from eval_pipeline._shared.data_loader import load_jsonl, write_jsonl, load_json, write_json
from eval_pipeline._shared.metrics import normalize_template
from eval_pipeline._shared.adjudication import (
    StateManager, run_adjudication,
    ADJ_ORIGINAL, ADJ_OPTIMIZED, ADJ_EQUIVALENT,
    STATUS_RESOLVED,
    ALL_TASKS_FILE,
)

logger = logging.getLogger(__name__)


def _run_piplup_and_save(input_path, parser_module_path, postprocessing_orig_path, output_dir,
                         module_name, cold_start, scenarios, data_root, label):
    """运行 PIPLUP 并保存 result.jsonl + clusters.json。返回 (results, clusters)。"""
    import importlib
    from multiprocessing import Process, Pipe

    parser_class = importlib.import_module(parser_module_path).LogParser
    postprocessing_orig_mod = None
    if postprocessing_orig_path:
        postprocessing_orig_mod = importlib.import_module(postprocessing_orig_path)

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

    # 保存
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

    # 恢复
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

    # 转换 results 格式
    result_list = []
    for r in results:
        result_list.append({
            'log_id': r['log_id'],
            'template': r['template'],
            'template_id': r['template_id'],
        })

    conn.send({'results': result_list, 'clusters': clusters})
    conn.close()


def _compare_clusters(results_orig, results_opt, raw_log_info):
    """对比两版 PIPLUP 的 result，生成一致/不一致的 GT 任务。

    Returns:
        gt_tasks: 一致的任务列表（直接作为 GT）
        adjudication_tasks: 不一致的任务列表（需 LLM 裁决）
    """
    # 建立 log_id → template 映射
    orig_map = {r['log_id']: r for r in results_orig}
    opt_map = {r['log_id']: r for r in results_opt}

    # 建立 template_id → sample_logs 映射（各取最多5条）
    orig_samples = {}  # template_id -> [sample_log, ...]
    opt_samples = {}
    raw_map = {r['logId']: r['rawLog'] for r in raw_log_info} if raw_log_info else {}

    for r in results_orig:
        tid = r['template_id']
        if tid not in orig_samples:
            orig_samples[tid] = []
        if len(orig_samples[tid]) < 5:
            lid = str(r['log_id'])
            sample = raw_map.get(lid, '')
            if sample:
                orig_samples[tid].append(sample)

    for r in results_opt:
        tid = r['template_id']
        if tid not in opt_samples:
            opt_samples[tid] = []
        if len(opt_samples[tid]) < 5:
            lid = str(r['log_id'])
            sample = raw_map.get(lid, '')
            if sample:
                opt_samples[tid].append(sample)

    # 按 template_id 分组
    orig_clusters = {}  # template_id -> set of log_ids
    opt_clusters = {}
    for r in results_orig:
        orig_clusters.setdefault(r['template_id'], set()).add(str(r['log_id']))
    for r in results_opt:
        opt_clusters.setdefault(r['template_id'], set()).add(str(r['log_id']))

    # 按行对齐比较
    gt_tasks = []  # 一致的任务
    adjudication_tasks = []  # 不一致的任务

    # 收集所有不重复的 template 对
    compared_pairs = set()
    seq = 0

    for log_id, r_orig in orig_map.items():
        r_opt = opt_map.get(log_id)
        if r_opt is None:
            continue

        # 比较归一化后的 template
        norm_orig = normalize_template(r_orig['template'])
        norm_opt = normalize_template(r_opt['template'])

        if norm_orig == norm_opt:
            # 一致：直接记入 GT
            if log_id not in [t.get('log_id') for t in gt_tasks]:
                gt_tasks.append({
                    'log_id': log_id,
                    'template': r_orig['template'],
                    'template_id': r_orig['template_id'],
                    'adjudication': 'consistent',
                    'reason': '两版 PIPLUP 模板一致',
                    'confidence': 1.0,
                })
        else:
            # 不一致：创建裁决任务
            tid_orig = r_orig['template_id']
            tid_opt = r_opt['template_id']
            pair_key = (tid_orig, tid_opt)
            if pair_key not in compared_pairs:
                compared_pairs.add(pair_key)
                seq += 1
                adjudication_tasks.append({
                    'seq': seq,
                    'TaskID': f"GT_{seq:04d}",
                    'piplup_ori': r_orig['template'],
                    'piplup_opt': r_opt['template'],
                    'template_id_orig': tid_orig,
                    'template_id_opt': tid_opt,
                    'sample_log': orig_samples.get(tid_orig, [])[:5],
                })

    return gt_tasks, adjudication_tasks


def generate_gt(
    input_path: str,
    piplup_orig_path: str,
    piplup_opt_path: str,
    output_dir: str,
    postprocessing_orig_path: str = None,
    llm_config_path: str = None,
    cold_start: bool = True,
    module_name: str = "Messages",
    scenarios: list = None,
    data_root: str = None,
    live: bool = False,
    dry_run: bool = False,
    limit: int = 0,
    retry_failed: bool = False,
):
    """GT 生成主流程。"""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # ===== Step 1: 运行两版 PIPLUP =====
    print(f"\n{'='*60}")
    print(f"  Step 1: 运行两版 PIPLUP 生成 result + clusters")
    print(f"{'='*60}")

    run_dir = out / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)

    results_orig, clusters_orig = _run_piplup_and_save(
        input_path, piplup_orig_path, postprocessing_orig_path,
        run_dir, module_name, cold_start, scenarios, data_root, "piplup_orig")

    results_opt, clusters_opt = _run_piplup_and_save(
        input_path, piplup_opt_path, None,
        run_dir, module_name, cold_start, scenarios, data_root, "piplup_opt")

    # ===== Step 2: cluster 对比 =====
    print(f"\n{'='*60}")
    print(f"  Step 2: 对比两版 cluster，分类一致/不一致")
    print(f"{'='*60}")

    # 加载原始日志用于 sample_log
    raw_log_info = None
    if input_path and str(input_path).endswith('.jsonl'):
        raw_log_info = load_jsonl(input_path)

    gt_tasks, adjudication_tasks = _compare_clusters(results_orig, results_opt, raw_log_info)

    print(f"  一致: {len(gt_tasks)} 条")
    print(f"  不一致: {len(adjudication_tasks)} 条（需 LLM 裁决）")

    # 保存 all_gt_tasks.jsonl（不一致的任务，作为裁决输入）
    tasks_dir = out / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    all_tasks_path = tasks_dir / ALL_TASKS_FILE
    write_jsonl(all_tasks_path, adjudication_tasks)
    print(f"  裁决任务已保存: {all_tasks_path}")

    # 保存一致的任务
    consistent_path = out / "consistent_gt.jsonl"
    write_jsonl(consistent_path, gt_tasks)

    # ===== Step 3: LLM 裁决 =====
    if adjudication_tasks:
        print(f"\n{'='*60}")
        print(f"  Step 3: LLM 裁决不一致 cluster")
        print(f"{'='*60}")

        if dry_run:
            print("  [dry-run] 仅生成 Prompt，不调用 LLM")
        elif not live:
            print("  [mock] 使用 Mock 客户端")

        summary = run_adjudication(
            run_root=out,
            input_path=all_tasks_path,
            config_path=llm_config_path,
            live=live,
            dry_run=dry_run,
            limit=limit,
            concurrency=0,
            retry_failed=retry_failed,
        )
        print(f"  裁决完成: {json.dumps(summary, ensure_ascii=False)}")
    else:
        print("\n  无不一致 cluster，跳过 LLM 裁决")
        summary = {
            "total_task_count": 0,
            "resolved_task_count": 0,
            "failed_task_count": 0,
            "pending_task_count": 0,
        }

    # ===== Step 4: 生成最终 GT =====
    print(f"\n{'='*60}")
    print(f"  Step 4: 生成最终 GT 文件")
    print(f"{'='*60}")

    final_gt = _build_final_gt(out, gt_tasks, adjudication_tasks)

    final_gt_path = out / "final_gt.jsonl"
    write_jsonl(final_gt_path, final_gt)
    print(f"  最终 GT: {final_gt_path} ({len(final_gt)} 条)")

    # 汇总
    gt_summary = {
        "total_clusters_orig": len(clusters_orig),
        "total_clusters_opt": len(clusters_opt),
        "consistent": len(gt_tasks),
        "inconsistent": len(adjudication_tasks),
        "adjudicated": summary.get("resolved_task_count", 0),
        "failed": summary.get("failed_task_count", 0),
        "adjudication_distribution": summary.get("adjudication_distribution", {}),
    }
    write_json(out / "gt_summary.json", gt_summary)
    print(f"  汇总: {json.dumps(gt_summary, ensure_ascii=False)}")

    return gt_summary


def _build_final_gt(gt_dir: Path, consistent_tasks: list, adjudication_tasks: list) -> list:
    """构建最终 GT 文件：一致的直接写入 + 不一致的按裁决结果写入。"""
    from eval_pipeline._shared.adjudication import STATUS_RESOLVED

    final_gt = []

    # 一致的任务
    for t in consistent_tasks:
        final_gt.append({
            'log_id': t['log_id'],
            'template': t['template'],
            'template_id': t['template_id'],
            'adjudication': 'consistent',
            'reason': t.get('reason', ''),
            'confidence': t.get('confidence', 1.0),
        })

    # 不一致的任务：根据裁决结果选择 GT 模板
    results_dir = gt_dir / "results"
    resolved_path = results_dir / "resolved_gt_tasks.jsonl"

    if resolved_path.exists():
        resolved = load_jsonl(resolved_path)
        # 建立 (template_id_orig, template_id_opt) → adjudication 映射
        adj_map = {}
        for r in resolved:
            key = (r.get('template_id_orig'), r.get('template_id_opt'))
            adj_map[key] = r

        # 为每个不一致的 log_id 选择 GT 模板
        # 需要重新加载 result 来获取每行的 template_id
        runs_dir = gt_dir / "runs"
        orig_results = load_jsonl(runs_dir / "piplup_orig_result.jsonl")
        opt_results = load_jsonl(runs_dir / "piplup_opt_result.jsonl")

        orig_map = {r['log_id']: r for r in orig_results}
        opt_map = {r['log_id']: r for r in opt_results}

        # 建立 template_id 对 → 裁决结果映射
        for log_id in orig_map:
            r_orig = orig_map[log_id]
            r_opt = opt_map.get(log_id)
            if r_opt is None:
                continue

            norm_orig = normalize_template(r_orig['template'])
            norm_opt = normalize_template(r_opt['template'])
            if norm_orig == norm_opt:
                continue  # 一致的已处理

            key = (r_orig['template_id'], r_opt['template_id'])
            adj_result = adj_map.get(key)

            if adj_result is None:
                # 未裁决，跳过或使用原版
                final_gt.append({
                    'log_id': log_id,
                    'template': r_orig['template'],
                    'template_id': r_orig['template_id'],
                    'adjudication': 'pending',
                    'reason': '未裁决',
                    'confidence': 0.0,
                })
                continue

            adj = adj_result.get('Adjudication', '')
            reason = adj_result.get('Reason', '')
            confidence = adj_result.get('Confidence', 0)

            if adj == ADJ_ORIGINAL:
                gt_template = r_orig['template']
                gt_tid = r_orig['template_id']
            elif adj == ADJ_OPTIMIZED:
                gt_template = r_opt['template']
                gt_tid = r_opt['template_id']
            else:
                # equivalent 或其他：使用原版
                gt_template = r_orig['template']
                gt_tid = r_orig['template_id']

            final_gt.append({
                'log_id': log_id,
                'template': gt_template,
                'template_id': gt_tid,
                'adjudication': adj,
                'reason': reason,
                'confidence': confidence,
            })
    else:
        # 无裁决结果文件，不一致的按原版
        runs_dir = gt_dir / "runs"
        orig_results = load_jsonl(runs_dir / "piplup_orig_result.jsonl")
        opt_results = load_jsonl(runs_dir / "piplup_opt_result.jsonl")
        orig_map = {r['log_id']: r for r in orig_results}
        opt_map = {r['log_id']: r for r in opt_results}

        for log_id in orig_map:
            r_orig = orig_map[log_id]
            r_opt = opt_map.get(log_id)
            if r_opt is None:
                continue
            norm_orig = normalize_template(r_orig['template'])
            norm_opt = normalize_template(r_opt['template'])
            if norm_orig == norm_opt:
                continue
            final_gt.append({
                'log_id': log_id,
                'template': r_orig['template'],
                'template_id': r_orig['template_id'],
                'adjudication': 'unresolved',
                'reason': '未进行 LLM 裁决',
                'confidence': 0.0,
            })

    return final_gt


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    )

    parser = argparse.ArgumentParser(description="GT 生成：cluster 对比 + LLM 裁决")
    parser.add_argument("--input", required=True, help="输入 JSONL 文件或文件夹路径")
    parser.add_argument("--piplup-orig", required=True, help="PIPLUP 原版模块路径")
    parser.add_argument("--piplup-opt", required=True, help="PIPLUP 优化版模块路径")
    parser.add_argument("--postprocessing-orig", default=None, help="原版后处理模块路径")
    parser.add_argument("--llm-config", default=None, help="LLM 配置文件路径（YAML）")
    parser.add_argument("--output", required=True, help="GT 输出目录")
    parser.add_argument("--module", default="Messages", help="日志模块名")
    parser.add_argument("--cold-start", action="store_true", default=True, help="冷启动（清缓存）")
    parser.add_argument("--scenarios", default=None, help="场景列表，逗号分隔")
    parser.add_argument("--data-root", default=r"D:\DME-data\单个日志模块", help="数据根目录")
    parser.add_argument("--live", action="store_true", help="调用真实 LLM API")
    parser.add_argument("--dry-run", action="store_true", help="仅生成 Prompt，不调用 LLM")
    parser.add_argument("--limit", type=int, default=0, help="限制裁决任务数（0=不限）")
    parser.add_argument("--retry-failed", action="store_true", help="重试失败任务")

    args = parser.parse_args()

    scenarios = None
    if args.scenarios:
        scenarios = [s.strip() for s in args.scenarios.split(',')]

    generate_gt(
        input_path=args.input,
        piplup_orig_path=args.piplup_orig,
        piplup_opt_path=args.piplup_opt,
        output_dir=args.output,
        postprocessing_orig_path=args.postprocessing_orig,
        llm_config_path=args.llm_config,
        cold_start=args.cold_start,
        module_name=args.module,
        scenarios=scenarios,
        data_root=args.data_root,
        live=args.live,
        dry_run=args.dry_run,
        limit=args.limit,
        retry_failed=args.retry_failed,
    )


if __name__ == '__main__':
    main()

"""模块4：GT 生成 — 对比两版 PIPLUP 的 cluster，一致的记录为 GT，不一致的送 LLM 裁决。

CLI:
    python -m eval_pipeline gt \
        --input <jsonl_or_folder> \
        --piplup-orig <python_module_path> \
        --piplup-opt <python_module_path> \
        [--preprocessing-orig <python_module_path>] \
        [--postprocessing-orig <python_module_path>] \
        --llm-config <yaml_path> \
        --output <gt_dir> \
        [--scenarios 01,02,...] [--module Messages] \
        [--limit N] [--dry-run] [--retry-failed] [--live] [--save-prompts]

流程：
1. 对两版 PIPLUP 各跑一次解析（子进程隔离，冷启动默认并行），落盘
   runs/{label}_result.jsonl + clusters.json
2. 行级对齐对比：归一化模板一致 → 直接记 GT；不一致 → 按
   (原版模板, 优化版模板) 内容寻址生成裁决任务（TaskID 为内容哈希，
   断点续跑跨运行稳定），行→任务映射落盘 line_pairs.jsonl
3. LLM 裁决：支持并发（--concurrency）+ 限流自适应 + 断点续跑
4. 输出：final_gt.jsonl + gt_summary.json

GT 构建口径（裁决默认偏向的披露）：一致行两版模板相同；不一致行按
LLM 裁决选择，equivalent/pending/failed/neither 默认采用原版模板——
该口径使原版在这些行上 PA=1，解读优化版 PA 时需结合
adjudication_distribution 中的 equivalent/pending 数量。
"""
import hashlib
import json
import logging
from pathlib import Path

from eval_pipeline._shared.data_loader import load_jsonl, write_jsonl, write_json
from eval_pipeline._shared.metrics import normalize_template
from eval_pipeline._shared.piplup_runner import run_piplup_pair
from eval_pipeline._shared.adjudication import (
    run_adjudication,
    ADJ_ORIGINAL, ADJ_OPTIMIZED,
    ALL_TASKS_FILE,
)

logger = logging.getLogger(__name__)


def _task_id_for(norm_orig: str, norm_opt: str) -> str:
    """内容寻址 TaskID：同对模板跨运行稳定，断点续跑不会错位。"""
    digest = hashlib.sha1(
        (norm_orig + '\x00' + norm_opt).encode('utf-8')).hexdigest()[:16].upper()
    return f"GT_{digest}"


def _build_raw_map(input_path, scenarios, data_root, module_name):
    """构建 log_id(str) → 原始日志行 映射（供裁决样本采样）。

    logId 由 build_log_info 按 'str(i+1)' 规则生成，此处按同一规则重建。
    """
    from eval_pipeline._shared.data_loader import (
        load_raw_lines, load_raw_lines_by_scenarios, build_log_info,
    )
    if input_path and str(input_path).endswith('.jsonl'):
        rows = load_jsonl(input_path)
        return {str(r['logId']): r['rawLog'] for r in rows if r.get('rawLog')}
    if scenarios:
        raw_lines = load_raw_lines_by_scenarios(data_root, scenarios)
    else:
        raw_lines = load_raw_lines(input_path)
    log_info = build_log_info(raw_lines, module_name)
    del raw_lines
    return {r['logId']: r['rawLog'] for r in log_info}


def _compare_clusters(results_orig, results_opt, raw_map):
    """对比两版 PIPLUP 的行级 result，生成一致 GT 与裁决任务。

    Returns:
        gt_tasks: 一致的行列表（直接作为 GT）
        adjudication_tasks: 不一致的裁决任务（按模板对去重，TaskID 内容寻址）
        line_pairs: 行 → 模板对 映射（供 Step4 免重复归一化）
    """
    orig_map = {r['log_id']: r for r in results_orig}
    opt_map = {r['log_id']: r for r in results_opt}

    # template_id → 行集合（用于裁决样本采样：取两簇分歧行）
    orig_clusters = {}
    opt_clusters = {}
    for r in results_orig:
        orig_clusters.setdefault(r['template_id'], set()).add(str(r['log_id']))
    for r in results_opt:
        opt_clusters.setdefault(r['template_id'], set()).add(str(r['log_id']))

    # template_id → 样本日志（原始行）
    def _samples_for(ids):
        out = []
        for lid in ids:
            sample = raw_map.get(lid, '')
            if sample:
                out.append(sample)
            if len(out) >= 8:  # 采样池上限，后续按分歧优先截取
                break
        return out

    gt_tasks = []
    adjudication_tasks = []
    line_pairs = []
    compared_pairs = {}
    seq = 0

    for log_id, r_orig in orig_map.items():
        r_opt = opt_map.get(log_id)
        if r_opt is None:
            continue

        tid_orig, tid_opt = r_orig['template_id'], r_opt['template_id']
        norm_orig = normalize_template(r_orig['template'])
        norm_opt = normalize_template(r_opt['template'])

        if norm_orig == norm_opt:
            gt_tasks.append({
                'log_id': log_id,
                'template': r_orig['template'],
                'template_id': tid_orig,
                'adjudication': 'consistent',
                'reason': '两版 PIPLUP 模板一致',
                'confidence': 1.0,
            })
            line_pairs.append({
                'log_id': log_id, 'consistent': True,
                'template_id_orig': tid_orig, 'template_id_opt': tid_opt,
                'TaskID': '',
            })
            continue

        # 不一致：按 (tid_orig, tid_opt) 对去重，生成裁决任务
        pair_key = (tid_orig, tid_opt)
        task_id = _task_id_for(norm_orig, norm_opt)
        if pair_key not in compared_pairs:
            compared_pairs[pair_key] = task_id
            seq += 1

            # 裁决样本：优先两簇分歧行（一个版本归入而另一版本未归入的行），
            # 再以共同行补足——原实现仅取原版簇样本，裁决输入偏向原版
            ids_orig = orig_clusters.get(tid_orig, set())
            ids_opt = opt_clusters.get(tid_opt, set())
            diff_ids = sorted(ids_orig ^ ids_opt)
            common_ids = sorted(ids_orig & ids_opt)
            sample_ids = diff_ids[:5]
            if len(sample_ids) < 5:
                sample_ids += common_ids[:5 - len(sample_ids)]
            sample_logs = [raw_map[lid] for lid in sample_ids if lid in raw_map][:5]

            adjudication_tasks.append({
                'seq': seq,
                'TaskID': task_id,
                'piplup_ori': r_orig['template'],
                'piplup_opt': r_opt['template'],
                # 归一化模板对落盘：TaskID = sha1(norm_ori + \0 + norm_opt) 可审计
                'norm_ori': norm_orig,
                'norm_opt': norm_opt,
                'template_id_orig': tid_orig,
                'template_id_opt': tid_opt,
                'sample_log': sample_logs,
                'sample_diff_lines': len(diff_ids),
            })

        line_pairs.append({
            'log_id': log_id, 'consistent': False,
            'template_id_orig': tid_orig, 'template_id_opt': tid_opt,
            'TaskID': compared_pairs[pair_key],
        })

    return gt_tasks, adjudication_tasks, line_pairs


def generate_gt(
    input_path: str,
    piplup_orig_path: str,
    piplup_opt_path: str,
    output_dir: str,
    preprocessing_orig_path: str = None,
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
    save_prompts: bool = False,
    concurrency: int = 0,
):
    """GT 生成主流程。"""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # ===== Step 1: 运行两版 PIPLUP（冷启动默认并行） =====
    print(f"\n{'='*60}")
    print(f"  Step 1: 运行两版 PIPLUP 生成 result + clusters"
          f"（cold_start={cold_start}，{'并行' if cold_start else '串行'}）")
    print(f"{'='*60}")

    run_dir = out / "runs"

    results_orig, clusters_orig, results_opt, clusters_opt = run_piplup_pair(
        input_path=input_path, output_dir=str(run_dir), module_name=module_name,
        cold_start=cold_start, scenarios=scenarios, data_root=data_root,
        piplup_orig_path=piplup_orig_path,
        preprocessing_orig_path=preprocessing_orig_path,
        postprocessing_orig_path=postprocessing_orig_path,
        piplup_opt_path=piplup_opt_path,
    )

    # 输入签名落盘（answer --reuse-runs 时校验，防止错配比较）
    write_json(run_dir / "run_meta.json", {
        'input_path': str(input_path),
        'module': module_name,
        'scenarios': scenarios or [],
        'n_lines': len(results_orig),
    })

    # ===== Step 2: 行级对比 =====
    print(f"\n{'='*60}")
    print(f"  Step 2: 对比两版结果，分类一致/不一致")
    print(f"{'='*60}")

    raw_map = _build_raw_map(input_path, scenarios, data_root, module_name)
    gt_tasks, adjudication_tasks, line_pairs = _compare_clusters(
        results_orig, results_opt, raw_map)

    print(f"  一致: {len(gt_tasks)} 条")
    print(f"  不一致: {len(adjudication_tasks)} 条（聚合为 LLM 裁决任务）")

    # 行 → 任务映射落盘（Step4 免重复归一化）
    pairs_path = out / "line_pairs.jsonl"
    write_jsonl(pairs_path, line_pairs)

    # 保存裁决任务总账（内容寻址 TaskID，跨运行稳定）
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
            concurrency=concurrency,
            retry_failed=retry_failed,
            save_prompts=save_prompts,
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

    final_gt = _build_final_gt(out, gt_tasks, line_pairs, results_orig, results_opt)

    final_gt_path = out / "final_gt.jsonl"
    write_jsonl(final_gt_path, final_gt)
    print(f"  最终 GT: {final_gt_path} ({len(final_gt)} 条)")

    # 汇总
    gt_summary = {
        "total_clusters_orig": len(clusters_orig),
        "total_clusters_opt": len(clusters_opt),
        "total_lines": len(line_pairs),
        "consistent": len(gt_tasks),
        "inconsistent_lines": len(line_pairs) - len(gt_tasks),
        "adjudication_tasks": len(adjudication_tasks),
        "adjudicated": summary.get("resolved_task_count", 0),
        "failed": summary.get("failed_task_count", 0),
        "adjudication_distribution": summary.get("adjudication_distribution", {}),
    }
    write_json(out / "gt_summary.json", gt_summary)
    print(f"  汇总: {json.dumps(gt_summary, ensure_ascii=False)}")

    return gt_summary


def _build_final_gt(out: Path, consistent_tasks: list, line_pairs: list,
                    results_orig: list, results_opt: list) -> list:
    """构建最终 GT：一致行直接写入；不一致行按裁决结果选择模板。

    使用 Step2 落盘的 line_pairs（含 TaskID），避免对全量行重复归一化。
    """
    from eval_pipeline._shared.adjudication import STATUS_RESOLVED

    final_gt = list(consistent_tasks)

    # 裁决结果：TaskID → record
    resolved_path = out / "results" / "resolved_gt_tasks.jsonl"
    adj_map = {}
    if resolved_path.exists():
        for r in load_jsonl(resolved_path):
            if r.get('Status') == STATUS_RESOLVED or 'Adjudication' in r:
                adj_map[r['TaskID']] = r

    # 行级结果：log_id → 行记录
    orig_map = {r['log_id']: r for r in results_orig}
    opt_map = {r['log_id']: r for r in results_opt}

    for lp in line_pairs:
        if lp['consistent']:
            continue
        log_id = lp['log_id']
        r_orig = orig_map.get(log_id)
        r_opt = opt_map.get(log_id)
        if r_orig is None or r_opt is None:
            continue

        adj_result = adj_map.get(lp['TaskID'])
        if adj_result is None:
            # 未裁决（dry-run / 配额中断）：按原版，显式标记 pending
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

        # 裁决口径：original → 原版；optimized → 优化版；
        # equivalent/pending/neither/failed → 原版（偏向已在摘要中披露）
        if adj == ADJ_OPTIMIZED:
            gt_template, gt_tid = r_opt['template'], r_opt['template_id']
        else:
            gt_template, gt_tid = r_orig['template'], r_orig['template_id']

        final_gt.append({
            'log_id': log_id,
            'template': gt_template,
            'template_id': gt_tid,
            'adjudication': adj,
            'reason': reason,
            'confidence': confidence,
        })

    return final_gt

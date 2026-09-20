"""模块3：答案生成和对比 — 生成两版 PIPLUP 的 cluster+result 文件，基于 GT 评估 PA/GA/FTA/FGA。

CLI:
    python -m eval_pipeline answer \
        --input <jsonl_or_folder> \
        --piplup-orig <python_module_path> \
        --piplup-opt <python_module_path> \
        [--preprocessing-orig <python_module_path>] \
        [--postprocessing-orig <python_module_path>] \
        --gt <gt_jsonl_path> \
        --output <result_dir> \
        [--reuse-runs <gt_output_dir>] \
        [--scenarios 01,02,...] [--module Messages] [--warm-start]

流程：
1. 对每版 PIPLUP 各跑一次解析（子进程隔离，冷启动默认并行）；
   若 --reuse-runs 指向模块4的 gt 输出目录（含 runs/），直接复用已落盘
   的 result/clusters，省去 2×全量解析（需通过 run_meta 校验输入一致）
2. 加载 GT（来自模块4输出 final_gt.jsonl）
3. 用 calculate_metrics_normalized() 评估两版结果 vs GT
4. 输出 eval_report.json
"""
import json
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from eval_pipeline._shared.data_loader import load_jsonl, write_json
from eval_pipeline._shared.metrics import calculate_metrics_normalized
from eval_pipeline._shared.piplup_runner import run_piplup_pair


def _load_runs(runs_dir: Path, expect_input: str, expect_module: str,
               expect_scenarios, expect_lines: int = None):
    """从 gt 模块落盘的 runs 目录加载两版结果，校验输入一致性。"""
    meta_path = runs_dir / "run_meta.json"
    if meta_path.exists():
        import json as _json
        meta = _json.loads(meta_path.read_text(encoding='utf-8'))
        same = (meta.get('input_path') == str(expect_input)
                and meta.get('module') == expect_module
                and (meta.get('scenarios') or None) == (expect_scenarios or None))
        if not same:
            raise RuntimeError(
                f"runs 目录输入签名不匹配（{meta_path}），请重新运行 gt 或去掉 --reuse-runs")

    for label in ("piplup_orig", "piplup_opt"):
        if not (runs_dir / f"{label}_result.jsonl").exists():
            raise FileNotFoundError(f"runs 目录缺少 {label}_result.jsonl: {runs_dir}")

    results_orig = load_jsonl(runs_dir / "piplup_orig_result.jsonl")
    results_opt = load_jsonl(runs_dir / "piplup_opt_result.jsonl")

    clusters_orig, clusters_opt = [], []
    for label, store in (("piplup_orig", 'orig'), ("piplup_opt", 'opt')):
        cp = runs_dir / f"{label}_clusters.json"
        if cp.exists():
            import json as _json
            data = _json.loads(cp.read_text(encoding='utf-8'))
            (clusters_orig if store == 'orig' else clusters_opt).extend(data)

    if expect_lines is not None and len(results_orig) != expect_lines:
        raise RuntimeError(
            f"runs 结果行数 {len(results_orig)} 与当前输入 {expect_lines} 不一致，"
            "请重新运行 gt 或去掉 --reuse-runs")

    return results_orig, clusters_orig, results_opt, clusters_opt


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


def generate_answers(
    input_path: str,
    piplup_orig_path: str,
    piplup_opt_path: str,
    gt_path: str,
    output_dir: str,
    preprocessing_orig_path: str = None,
    postprocessing_orig_path: str = None,
    cold_start: bool = True,
    module_name: str = "Messages",
    scenarios: list = None,
    data_root: str = None,
    reuse_runs: str = None,
):
    """答案生成和评估主流程。"""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # ===== Step 1: 运行两版 PIPLUP（或复用 gt 模块已落盘的结果） =====
    print(f"\n{'='*60}")
    print(f"  Step 1: 生成两版 PIPLUP 的 cluster + result")
    print(f"{'='*60}")

    if reuse_runs:
        runs_dir = Path(reuse_runs)
        print(f"  复用已落盘结果: {runs_dir}")
        # 行数校验基于输入 JSONL（scenario 模式下无法低成本计数，跳过行数校验）
        expect_lines = None
        if input_path and str(input_path).endswith('.jsonl'):
            expect_lines = len(load_jsonl(input_path))
        results_orig, clusters_orig, results_opt, clusters_opt = _load_runs(
            runs_dir, input_path, module_name, scenarios, expect_lines)
        print(f"  piplup_orig: {len(results_orig)} 行 | piplup_opt: {len(results_opt)} 行（复用）")
    else:
        results_orig, clusters_orig, results_opt, clusters_opt = run_piplup_pair(
            input_path=input_path, output_dir=str(out), module_name=module_name,
            cold_start=cold_start, scenarios=scenarios, data_root=data_root,
            piplup_orig_path=piplup_orig_path,
            preprocessing_orig_path=preprocessing_orig_path,
            postprocessing_orig_path=postprocessing_orig_path,
            piplup_opt_path=piplup_opt_path,
        )

    # ===== Step 2: 加载 GT 并评估 =====
    print(f"\n{'='*60}")
    print(f"  Step 2: 加载 GT 并评估 PA/GA/FTA/FGA")
    print(f"{'='*60}")

    gt_records = load_jsonl(gt_path)
    print(f"  GT: {gt_path} ({len(gt_records)} 条)")

    df_gt = _to_eval_dataframe(gt_records)

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
        "reused_runs": bool(reuse_runs),
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

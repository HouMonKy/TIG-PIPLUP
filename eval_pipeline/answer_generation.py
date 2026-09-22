"""模块4：答案评估 — 用冻结的 GT 评估任意算法的解析质量（PA/GA/FTA/FGA）。

CLI:
    python -m eval_pipeline answer \
        --input <jsonl_or_folder> \
        --gt <gt_jsonl_path> [--gt-alt <alt_jsonl_path>] \
        (--parsers <module_path>[,<module_path>...] | --piplup-orig ... --piplup-opt ...) \
        [--preprocessing-orig ...] [--postprocessing-orig ...] \
        --output <result_dir> \
        [--reuse-runs <gt_runs_dir>] [--scenarios 01,02,...] [--module Messages] [--warm-start]

行级口径（簇不参与对齐）：
- GT 与被评算法的输出都是行级表 {logId, template, template_id}
- PA：逐行模板文本（归一化后）是否等于 GT
- GA：行集合划分（同模板的 LineId 集合）是否与 GT 的行集合族一致
- FTA：去重模板集合与 GT 模板集合的查准/查全
- 双变体均值：GT 目录若存在 final_gt_alt.jsonl（等价行交换变体），
  对两个变体各评一轮取均值——等价行上两版各得 0.5，消除 GT 构建偏向
- 覆盖率：evaluated_lines / gt_count 随报告输出

模块3（gt）产出的 eval_report.json 即由本模块的 evaluate_parsers_against_gt
计算；answer 子命令面向"GT 冻结后评估第三方版本"（被评估版本对 GT 无
投票权，无主场优势）。
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
from eval_pipeline._shared.piplup_runner import run_piplup_pair, load_runs_dir


def _to_eval_dataframe(records: list) -> pd.DataFrame:
    """将行级记录列表转为评估用 DataFrame（LineId, EventTemplate, EventId）。"""
    rows = []
    for r in records:
        rows.append({
            'LineId': int(r['log_id']),
            'EventTemplate': r['template'],
            'EventId': str(r['template_id']),
        })
    # 显式列定义：空表也保有列结构（GT 全排除场景）
    return pd.DataFrame(rows, columns=['LineId', 'EventTemplate', 'EventId'])


def evaluate_parsers_against_gt(gt_records: list, parser_results: dict,
                                gt_records_alt: list = None) -> dict:
    """评估一个或多个算法的行级结果 vs GT（支持双变体均值）。

    Args:
        gt_records: 主 GT 行级记录（final_gt.jsonl 内容）
        parser_results: {显示名: results 列表}，results 为
            {log_id, template, template_id} 行级记录
        gt_records_alt: 备变体 GT（等价行交换模板）；None 时单变体评估

    Returns:
        {
          'parsers': {name: {'PA','GA','FTA','FGA'(均值或单值),
                             'variants': {'main': {...}, 'alt': {...}},
                             'evaluated_lines'}},
          'gt_count': int, 'evaluated_lines': int, 'gt_alt_used': bool,
        }
    """
    df_gt = _to_eval_dataframe(gt_records)
    df_gt_alt = _to_eval_dataframe(gt_records_alt) if gt_records_alt else None

    # 对齐域：GT ∩ 全部被评算法的公共行（保证跨算法可比）
    if df_gt.empty:
        common_ids = set()
    else:
        common_ids = set(df_gt['LineId'])
    for results in parser_results.values():
        df_p = _to_eval_dataframe(results)
        common_ids &= set(df_p['LineId'])
    common_ids = sorted(common_ids)
    if not common_ids:
        # 空对齐域（GT 全排除 / 无公共行）：零值报告
        return {
            'gt_count': len(gt_records),
            'evaluated_lines': 0,
            'gt_alt_used': df_gt_alt is not None,
            'parsers': {name: {'PA': 0.0, 'GA': 0.0, 'FTA': 0.0, 'FGA': 0.0,
                               'evaluated_lines': 0, 'variants': {}}
                        for name in parser_results},
        }

    df_gt_aligned = (df_gt[df_gt['LineId'].isin(common_ids)]
                     .sort_values('LineId').reset_index(drop=True))
    df_gt_alt_aligned = (df_gt_alt[df_gt_alt['LineId'].isin(common_ids)]
                         .sort_values('LineId').reset_index(drop=True)
                         ) if df_gt_alt is not None else None

    report = {
        'gt_count': len(gt_records),
        'evaluated_lines': len(common_ids),
        'gt_alt_used': df_gt_alt_aligned is not None,
        'parsers': {},
    }

    for name, results in parser_results.items():
        df_pred = _to_eval_dataframe(results)
        df_pred_aligned = (df_pred[df_pred['LineId'].isin(common_ids)]
                           .sort_values('LineId').reset_index(drop=True))

        metrics_main = calculate_metrics_normalized(df_gt_aligned, df_pred_aligned)
        entry = {
            'PA': metrics_main['PA'], 'GA': metrics_main['GA'],
            'FTA': metrics_main['FTA'], 'FGA': metrics_main['FGA'],
            'evaluated_lines': len(common_ids),
            'variants': {'main': metrics_main},
        }

        if df_gt_alt_aligned is not None:
            metrics_alt = calculate_metrics_normalized(df_gt_alt_aligned, df_pred_aligned)
            entry['variants']['alt'] = metrics_alt
            # 双变体均值：等价行上 GT 主/备各取一版模板，均值消除构建偏向
            for key in ('PA', 'GA', 'FTA', 'FGA'):
                entry[key] = (metrics_main[key] + metrics_alt[key]) / 2

        report['parsers'][name] = entry

    return report


def _print_eval_report(report: dict):
    """打印评估汇总表。"""
    print(f"  GT 行数: {report['gt_count']} | 评估行数: {report['evaluated_lines']} "
          f"| 双变体均值: {'是' if report['gt_alt_used'] else '否'}")
    print(f"  {'算法':<20s}  {'PA':>8s}  {'GA':>8s}  {'FTA':>8s}  {'FGA':>8s}")
    print(f"  {'-'*20}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*8}")
    for name, m in report['parsers'].items():
        print(f"  {name:<20s}  {m['PA']:>8.4f}  {m['GA']:>8.4f}  "
              f"{m['FTA']:>8.4f}  {m['FGA']:>8.4f}")


def _load_runs(runs_dir: Path, expect_input: str, expect_module: str,
               expect_scenarios, expect_lines: int = None):
    """从 gt 模块落盘的 runs 目录加载两版结果，校验输入一致性。"""
    meta_path = runs_dir / "run_meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
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
    if expect_lines is not None and len(results_orig) != expect_lines:
        raise RuntimeError(
            f"runs 结果行数 {len(results_orig)} 与当前输入 {expect_lines} 不一致，"
            "请重新运行 gt 或去掉 --reuse-runs")
    return results_orig, results_opt


def generate_answers(
    input_path: str,
    piplup_orig_path: str = None,
    piplup_opt_path: str = None,
    gt_path: str = None,
    output_dir: str = None,
    parsers: list = None,
    preprocessing_orig_path: str = None,
    postprocessing_orig_path: str = None,
    cold_start: bool = True,
    module_name: str = "Messages",
    scenarios: list = None,
    data_root: str = None,
    reuse_runs: str = None,
    gt_alt_path: str = None,
):
    """答案评估主流程：解析（或复用）→ 评估 → 报告。

    算法来源三选一：
    - --parsers a,b,c（多算法，名称取模块名末段）
    - --piplup-orig + --piplup-opt（双版模式，可 --reuse-runs 复用 gt 落盘结果）
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # ===== 解析（或复用） =====
    if reuse_runs:
        if not (piplup_orig_path and piplup_opt_path):
            raise ValueError("--reuse-runs 仅支持 --piplup-orig/--piplup-opt 双版模式")
        runs_dir = Path(reuse_runs)
        print(f"  复用已落盘结果: {runs_dir}")
        expect_lines = None
        if input_path and str(input_path).endswith('.jsonl'):
            expect_lines = len(load_jsonl(input_path))
        results_orig, results_opt = load_runs_dir(
            runs_dir, input_path, module_name, scenarios, expect_lines)
        parser_results = {'PIPLUP原版': results_orig, 'PIPLUP优化版': results_opt}
    elif parsers:
        parser_results = {}
        for parser_path in parsers:
            label = Path(parser_path.replace('.', '/')).stem
            results, _clusters = _run_single(
                input_path, parser_path, out, f"parser_{label}", module_name,
                cold_start, scenarios, data_root)
            parser_results[label] = results
    elif piplup_orig_path and piplup_opt_path:
        results_orig, _c1, results_opt, _c2 = run_piplup_pair(
            input_path=input_path, output_dir=str(out), module_name=module_name,
            cold_start=cold_start, scenarios=scenarios, data_root=data_root,
            piplup_orig_path=piplup_orig_path,
            preprocessing_orig_path=preprocessing_orig_path,
            postprocessing_orig_path=postprocessing_orig_path,
            piplup_opt_path=piplup_opt_path,
        )
        parser_results = {'PIPLUP原版': results_orig, 'PIPLUP优化版': results_opt}
    else:
        raise ValueError("需要 --parsers 或 --piplup-orig + --piplup-opt 指定被评算法")

    # ===== 加载 GT（自动探测双变体） =====
    gt_records = load_jsonl(gt_path)
    gt_alt = None
    alt_path = (Path(gt_alt_path) if gt_alt_path
                else Path(gt_path).parent / 'final_gt_alt.jsonl')
    if alt_path.exists():
        gt_alt = load_jsonl(alt_path)

    print(f"  GT: {gt_path} ({len(gt_records)} 条)"
          f"{' + 双变体' if gt_alt else ''}")

    report = evaluate_parsers_against_gt(gt_records, parser_results, gt_alt)

    report['gt_path'] = str(gt_path)
    report['gt_alt_path'] = str(alt_path) if gt_alt else None
    report['reused_runs'] = bool(reuse_runs)

    report_path = out / "eval_report.json"
    write_json(report_path, report)

    print(f"\n  评估报告已保存: {report_path}")
    _print_eval_report(report)
    return report


def _run_single(input_path, parser_module_path, output_dir, label, module_name,
                cold_start, scenarios, data_root):
    """运行单个算法（answer 多算法模式）。"""
    from eval_pipeline._shared.piplup_runner import run_piplup_and_save
    return run_piplup_and_save(
        input_path=input_path, parser_module_path=parser_module_path,
        output_dir=output_dir, label=label, module_name=module_name,
        cold_start=cold_start, scenarios=scenarios, data_root=data_root,
    )

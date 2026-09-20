"""模块2：算法对比 — 对比 Drain3、PIPLUP原版、PIPLUP优化版 的解析性能。

CLI:
    python -m eval_pipeline compare \
        --input <jsonl_or_folder> \
        --piplup-orig <python_module_path> \
        --piplup-opt <python_module_path> \
        [--postprocessing-orig <python_module_path>] \
        --output <report_dir> \
        [--scenarios 01,02,...] [--cold-start] [--module Messages]

指标：吞吐(lines/s, 总耗时), CPU(平均%, 峰值%, CPU时间), 内存(峰值RSS, 稳态RSS, 增量, MB/百万行), 跳过数。
隔离：每算法独立子进程。
"""
import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from eval_pipeline._shared.runner import run_in_subprocess
from eval_pipeline._shared.data_loader import write_json


def run_comparison(
    input_path: str,
    piplup_orig_path: str,
    piplup_opt_path: str,
    output_dir: str,
    postprocessing_orig_path: str = None,
    cold_start: bool = True,
    module_name: str = "Messages",
    scenarios: list = None,
    data_root: str = None,
):
    """运行三算法性能对比。

    Args:
        input_path: JSONL 文件路径（优先使用）或文件夹路径
        piplup_orig_path: PIPLUP 原版模块路径
        piplup_opt_path: PIPLUP 优化版模块路径
        output_dir: 报告输出目录
        postprocessing_orig_path: 原版后处理模块路径
        cold_start: True=清缓存
        module_name: 日志模块名
        scenarios: 场景名列表（可选，用于按场景加载 .log）
        data_root: 数据根目录
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    algorithms = [
        ("Drain3", "drain", {}),
        ("PIPLUP原版", "piplup_orig", {"piplup_orig_path": piplup_orig_path, "postprocessing_orig_path": postprocessing_orig_path}),
        ("PIPLUP优化版", "piplup_opt", {"piplup_opt_path": piplup_opt_path}),
    ]

    all_results = {}

    for algo_name, algo_key, extra_params in algorithms:
        print(f"\n{'='*60}")
        print(f"  运行算法: {algo_name}")
        print(f"{'='*60}")

        # 判断输入模式
        is_jsonl = input_path and str(input_path).endswith('.jsonl')
        if is_jsonl:
            result = run_in_subprocess(
                algo_key=algo_key,
                input_path=input_path,
                cold_start=cold_start,
                module_name=module_name,
                **extra_params,
            )
        else:
            result = run_in_subprocess(
                algo_key=algo_key,
                input_path=input_path,
                cold_start=cold_start,
                module_name=module_name,
                data_root=data_root,
                scenario_names=scenarios,
                **extra_params,
            )

        all_results[algo_name] = result

        print(f"  吞吐: {result['throughput']:.0f} lines/s")
        print(f"  耗时: {result['elapsed']:.1f}s")
        print(f"  行数: {result['nlines']}")
        print(f"  峰值RSS: {result['peak_rss']:.1f} MB")
        print(f"  稳态RSS: {result['steady_rss']:.1f} MB")
        print(f"  内存增量: {result['rss_delta']:.1f} MB")
        print(f"  平均CPU: {result['avg_cpu']:.1f}%")
        print(f"  峰值CPU: {result['peak_cpu']:.1f}%")
        print(f"  CPU时间: {result['cpu_time']:.1f}s")
        print(f"  跳过数: {result['skip_count']}")
        print(f"  唯一模板: {result['unique_templates']}")
        print(f"  簇数: {result['total_clusters']}")

    # 保存原始 JSON
    raw_path = out / "algo_comparison_raw.json"
    write_json(raw_path, all_results)
    print(f"\n原始指标已保存: {raw_path}")

    # 生成报告
    report_path = out / "algo_comparison_report.txt"
    _generate_report(report_path, all_results)
    print(f"报告已保存: {report_path}")


def _generate_report(path: Path, results: dict):
    """生成性能对比报告。"""
    lines = []
    lines.append("=" * 80)
    lines.append("  算法性能对比报告")
    lines.append("=" * 80)
    lines.append("")

    # 性能表
    lines.append("  【性能】")
    lines.append(f"  {'算法':<16s}  {'lines/s':>10s}  {'耗时(s)':>8s}  {'CPU时间(s)':>10s}  {'跳过数':>8s}")
    lines.append(f"  {'-'*16}  {'-'*10}  {'-'*8}  {'-'*10}  {'-'*8}")
    for name, r in results.items():
        lines.append(f"  {name:<16s}  {r['throughput']:>10.0f}  {r['elapsed']:>8.1f}  {r['cpu_time']:>10.1f}  {r['skip_count']:>8d}")
    lines.append("")

    # CPU 表
    lines.append("  【CPU】")
    lines.append(f"  {'算法':<16s}  {'平均CPU%':>10s}  {'峰值CPU%':>10s}  {'CPU时间(s)':>10s}")
    lines.append(f"  {'-'*16}  {'-'*10}  {'-'*10}  {'-'*10}")
    for name, r in results.items():
        lines.append(f"  {name:<16s}  {r['avg_cpu']:>10.1f}  {r['peak_cpu']:>10.1f}  {r['cpu_time']:>10.1f}")
    lines.append("")

    # 内存表
    lines.append("  【内存】")
    lines.append(f"  {'算法':<16s}  {'峰值RSS(MB)':>12s}  {'稳态RSS(MB)':>12s}  {'增量(MB)':>10s}  {'MB/百万行':>10s}")
    lines.append(f"  {'-'*16}  {'-'*12}  {'-'*12}  {'-'*10}  {'-'*10}")
    for name, r in results.items():
        mb_per_million = r['rss_delta'] / (r['nlines'] / 1e6) if r['nlines'] > 0 else 0
        lines.append(f"  {name:<16s}  {r['peak_rss']:>12.1f}  {r['steady_rss']:>12.1f}  {r['rss_delta']:>10.1f}  {mb_per_million:>10.1f}")
    lines.append("")

    # 模板/簇
    lines.append("  【模板/簇】")
    lines.append(f"  {'算法':<16s}  {'唯一模板':>10s}  {'簇数':>10s}")
    lines.append(f"  {'-'*16}  {'-'*10}  {'-'*10}")
    for name, r in results.items():
        lines.append(f"  {name:<16s}  {r['unique_templates']:>10d}  {r['total_clusters']:>10d}")
    lines.append("")

    # 对比摘要
    names = list(results.keys())
    if len(names) >= 3:
        opt = results[names[2]]
        orig = results[names[1]]
        drain = results[names[0]]

        lines.append("  【对比摘要】")
        if orig['throughput'] > 0:
            tp_improvement = (opt['throughput'] - orig['throughput']) / orig['throughput'] * 100
            lines.append(f"  优化版 vs 原版 吞吐: {opt['throughput']:.0f} vs {orig['throughput']:.0f} lines/s ({tp_improvement:+.1f}%)")
        if drain['throughput'] > 0:
            tp_vs_drain = (opt['throughput'] - drain['throughput']) / drain['throughput'] * 100
            lines.append(f"  优化版 vs Drain3 吞吐: {opt['throughput']:.0f} vs {drain['throughput']:.0f} lines/s ({tp_vs_drain:+.1f}%)")
        lines.append(f"  优化版 vs 原版 簇数: {opt['total_clusters']} vs {orig['total_clusters']}")
        lines.append(f"  优化版 vs 原版 模板数: {opt['unique_templates']} vs {orig['unique_templates']}")
        lines.append("")

    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))


def main():
    parser = argparse.ArgumentParser(description="算法对比：Drain3 vs PIPLUP原版 vs PIPLUP优化版")
    parser.add_argument("--input", required=True, help="输入 JSONL 文件或文件夹路径")
    parser.add_argument("--piplup-orig", required=True, help="PIPLUP 原版模块路径（Python import 路径）")
    parser.add_argument("--piplup-opt", required=True, help="PIPLUP 优化版模块路径（Python import 路径）")
    parser.add_argument("--postprocessing-orig", default=None, help="原版后处理模块路径")
    parser.add_argument("--output", required=True, help="报告输出目录")
    parser.add_argument("--module", default="Messages", help="日志模块名")
    parser.add_argument("--cold-start", action="store_true", default=True, help="冷启动（清缓存）")
    parser.add_argument("--scenarios", default=None, help="场景列表，逗号分隔（如 01,02,03）")
    parser.add_argument("--data-root", default=r"D:\DME-data\单个日志模块", help="数据根目录")

    args = parser.parse_args()

    scenarios = None
    if args.scenarios:
        scenarios = [s.strip() for s in args.scenarios.split(',')]

    run_comparison(
        input_path=args.input,
        piplup_orig_path=args.piplup_orig,
        piplup_opt_path=args.piplup_opt,
        output_dir=args.output,
        postprocessing_orig_path=args.postprocessing_orig,
        cold_start=args.cold_start,
        module_name=args.module,
        scenarios=scenarios,
        data_root=args.data_root,
    )


if __name__ == '__main__':
    main()

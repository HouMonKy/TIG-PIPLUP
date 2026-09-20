"""eval_pipeline 统一入口：argparse 子命令。

用法:
    python -m eval_pipeline <subcommand> [args]

子命令:
    data     : 模块1 - .log → JSONL
    compare  : 模块2 - 三算法性能对比
    gt       : 模块4 - GT 生成（cluster 对比 + LLM 裁决）
    answer   : 模块3 - 生成 cluster+result + 评估

示例:
    # 模块1：.log → JSONL
    python -m eval_pipeline data --input D:\\DME-data\\单个日志模块 --output data.jsonl

    # 模块2：三算法性能对比
    python -m eval_pipeline compare --input data.jsonl \\
        --piplup-orig src.data_process.template_extractor.parsing_algorithm.PIPLUP_original \\
        --piplup-opt src.data_process.template_extractor.parsing_algorithm.PIPLUP \\
        --output report/

    # 模块4：GT 生成
    python -m eval_pipeline gt --input data.jsonl \\
        --piplup-orig src.data_process.template_extractor.parsing_algorithm.PIPLUP_original \\
        --piplup-opt src.data_process.template_extractor.parsing_algorithm.PIPLUP \\
        --postprocessing-orig src.data_process.template_extractor.parsing_algorithm.postprocessing_original \\
        --llm-config llm_adjudication/config/llm_config.example.yaml \\
        --output gt/ --dry-run

    # 模块3：答案生成和评估
    python -m eval_pipeline answer --input data.jsonl \\
        --piplup-orig src.data_process.template_extractor.parsing_algorithm.PIPLUP_original \\
        --piplup-opt src.data_process.template_extractor.parsing_algorithm.PIPLUP \\
        --postprocessing-orig src.data_process.template_extractor.parsing_algorithm.postprocessing_original \\
        --gt gt/final_gt.jsonl --output answer/
"""
import argparse
import sys
import logging
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main():
    parser = argparse.ArgumentParser(
        prog="eval_pipeline",
        description="评估流水线：数据处理 → 算法对比 → GT生成 → 答案评估",
    )
    subparsers = parser.add_subparsers(dest="subcommand", help="子命令")

    # ===== data 子命令 =====
    p_data = subparsers.add_parser("data", help="模块1：.log → JSONL")
    p_data.add_argument("--input", required=True, help="输入 .log 文件或文件夹路径")
    p_data.add_argument("--output", required=True, help="输出 JSONL 路径")
    p_data.add_argument("--module", default="Messages", help="日志模块名（默认 Messages）")

    # ===== compare 子命令 =====
    p_compare = subparsers.add_parser("compare", help="模块2：三算法性能对比")
    p_compare.add_argument("--input", required=True, help="输入 JSONL 文件或文件夹路径")
    p_compare.add_argument("--piplup-orig", required=True, help="PIPLUP 原版模块路径（Python import 路径）")
    p_compare.add_argument("--piplup-opt", required=True, help="PIPLUP 优化版模块路径（Python import 路径）")
    p_compare.add_argument("--postprocessing-orig", default=None, help="原版后处理模块路径")
    p_compare.add_argument("--output", required=True, help="报告输出目录")
    p_compare.add_argument("--module", default="Messages", help="日志模块名")
    p_compare.add_argument("--cold-start", action="store_true", default=True, help="冷启动（清缓存）")
    p_compare.add_argument("--scenarios", default=None, help="场景列表，逗号分隔（如 01,02,03）")
    p_compare.add_argument("--data-root", default=r"D:\DME-data\单个日志模块", help="数据根目录")

    # ===== gt 子命令 =====
    p_gt = subparsers.add_parser("gt", help="模块4：GT 生成（cluster 对比 + LLM 裁决）")
    p_gt.add_argument("--input", required=True, help="输入 JSONL 文件或文件夹路径")
    p_gt.add_argument("--piplup-orig", required=True, help="PIPLUP 原版模块路径")
    p_gt.add_argument("--piplup-opt", required=True, help="PIPLUP 优化版模块路径")
    p_gt.add_argument("--postprocessing-orig", default=None, help="原版后处理模块路径")
    p_gt.add_argument("--llm-config", default=None, help="LLM 配置文件路径（YAML）")
    p_gt.add_argument("--output", required=True, help="GT 输出目录")
    p_gt.add_argument("--module", default="Messages", help="日志模块名")
    p_gt.add_argument("--cold-start", action="store_true", default=True, help="冷启动（清缓存）")
    p_gt.add_argument("--scenarios", default=None, help="场景列表，逗号分隔")
    p_gt.add_argument("--data-root", default=r"D:\DME-data\单个日志模块", help="数据根目录")
    p_gt.add_argument("--live", action="store_true", help="调用真实 LLM API")
    p_gt.add_argument("--dry-run", action="store_true", help="仅生成 Prompt，不调用 LLM")
    p_gt.add_argument("--limit", type=int, default=0, help="限制裁决任务数（0=不限）")
    p_gt.add_argument("--retry-failed", action="store_true", help="重试失败任务")

    # ===== answer 子命令 =====
    p_answer = subparsers.add_parser("answer", help="模块3：答案生成和评估")
    p_answer.add_argument("--input", required=True, help="输入 JSONL 文件或文件夹路径")
    p_answer.add_argument("--piplup-orig", required=True, help="PIPLUP 原版模块路径")
    p_answer.add_argument("--piplup-opt", required=True, help="PIPLUP 优化版模块路径")
    p_answer.add_argument("--postprocessing-orig", default=None, help="原版后处理模块路径")
    p_answer.add_argument("--gt", required=True, help="GT JSONL 文件路径（模块4输出 final_gt.jsonl）")
    p_answer.add_argument("--output", required=True, help="结果输出目录")
    p_answer.add_argument("--module", default="Messages", help="日志模块名")
    p_answer.add_argument("--cold-start", action="store_true", default=True, help="冷启动（清缓存）")
    p_answer.add_argument("--scenarios", default=None, help="场景列表，逗号分隔")
    p_answer.add_argument("--data-root", default=r"D:\DME-data\单个日志模块", help="数据根目录")

    args = parser.parse_args()

    if args.subcommand is None:
        parser.print_help()
        sys.exit(1)

    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    )

    if args.subcommand == "data":
        from eval_pipeline.data_processing import process_log_to_jsonl
        process_log_to_jsonl(args.input, args.output, args.module)

    elif args.subcommand == "compare":
        from eval_pipeline.algo_comparison import run_comparison
        scenarios = [s.strip() for s in args.scenarios.split(',')] if args.scenarios else None
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

    elif args.subcommand == "gt":
        from eval_pipeline.gt_generation import generate_gt
        scenarios = [s.strip() for s in args.scenarios.split(',')] if args.scenarios else None
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

    elif args.subcommand == "answer":
        from eval_pipeline.answer_generation import generate_answers
        scenarios = [s.strip() for s in args.scenarios.split(',')] if args.scenarios else None
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

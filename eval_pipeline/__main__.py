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

from eval_pipeline.config import DEFAULT_DATA_ROOT


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
    p_compare = subparsers.add_parser("compare", help="模块2：三算法性能对比（独立可选）")
    p_compare.add_argument("--input", required=True, help="输入 JSONL 文件或文件夹路径")
    p_compare.add_argument("--piplup-orig", required=True, help="PIPLUP 原版模块路径（Python import 路径）")
    p_compare.add_argument("--piplup-opt", required=True, help="PIPLUP 优化版模块路径（Python import 路径）")
    p_compare.add_argument("--postprocessing-orig", default=None, help="原版后处理模块路径")
    p_compare.add_argument("--preprocessing-orig", default=None, help="原版预处理模块路径")
    p_compare.add_argument("--output", required=True, help="报告输出目录")
    p_compare.add_argument("--module", default="Messages", help="日志模块名")
    p_compare.add_argument("--warm-start", action="store_true", help="预热模式（先跑 5000 行再清空仓库后正式解析；默认冷启动清缓存）")
    p_compare.add_argument("--scenarios", default=None, help="场景列表，逗号分隔（如 01,02,03）")
    p_compare.add_argument("--data-root", default=DEFAULT_DATA_ROOT, help=f"数据根目录（默认: {DEFAULT_DATA_ROOT}，可用环境变量 EVAL_PIPELINE_DATA_ROOT 覆盖）")

    # ===== gt 子命令 =====
    p_gt = subparsers.add_parser("gt", help="模块3：GT 生成（行级对齐 + LLM 裁决 + 自动评估）")
    p_gt.add_argument("--input", required=True, help="输入 JSONL 文件或文件夹路径")
    p_gt.add_argument("--piplup-orig", required=True, help="PIPLUP 原版模块路径")
    p_gt.add_argument("--piplup-opt", required=True, help="PIPLUP 优化版模块路径")
    p_gt.add_argument("--postprocessing-orig", default=None, help="原版后处理模块路径")
    p_gt.add_argument("--preprocessing-orig", default=None, help="原版预处理模块路径")
    p_gt.add_argument("--llm-config", default=None, help="LLM 配置文件路径（YAML）")
    p_gt.add_argument("--output", required=True, help="GT 输出目录")
    p_gt.add_argument("--module", default="Messages", help="日志模块名")
    p_gt.add_argument("--warm-start", action="store_true", help="预热模式（默认冷启动清缓存）")
    p_gt.add_argument("--scenarios", default=None, help="场景列表，逗号分隔")
    p_gt.add_argument("--data-root", default=DEFAULT_DATA_ROOT, help=f"数据根目录（默认: {DEFAULT_DATA_ROOT}，可用环境变量 EVAL_PIPELINE_DATA_ROOT 覆盖）")
    p_gt.add_argument("--live", action="store_true", help="调用真实 LLM API")
    p_gt.add_argument("--dry-run", action="store_true", help="仅生成 Prompt，不调用 LLM")
    p_gt.add_argument("--limit", type=int, default=0, help="限制裁决任务数（0=不限）")
    p_gt.add_argument("--retry-failed", action="store_true", help="重试失败任务")
    p_gt.add_argument("--retry-lines", default=None, help="多轮裁决：传入上一轮输出的 retry_lines.jsonl，将这些任务的裁决状态重置后重新裁决（可配合更换 --llm-config / 模型）")
    p_gt.add_argument("--review-adjudicated", action="store_true", help="已成功裁决的任务也进入人工标注清单（HTML 显示当前裁决，可推翻如 original→optimized，回填后以人工为准）")
    p_gt.add_argument("--save-prompts", action="store_true", help="live 模式下也落盘每个任务的 Prompt（dry-run 恒落盘）")
    p_gt.add_argument("--concurrency", type=int, default=1, help="裁决并发线程数（1=串行；>1 线程池加速 LLM IO）")
    p_gt.add_argument("--audit-consistent", type=int, default=0, help="一致行抽样审计数量（0=关闭；量化两版共识错误率，GT 可信度指标）")
    p_gt.add_argument("--no-eval", action="store_true", help="跳过自动评估（默认在 gt 结束时自动产出 eval_report.json）")
    p_gt.add_argument("--human-annotations", default=None, help="人工标注回填文件（gt_annotator.html 导出的 jsonl，{TaskID, template}；加载 human_annotations_needed.jsonl 标注）")
    p_gt.add_argument("--reuse-runs", action="store_true", help="复用上次 gt 落盘的 runs/（输入签名校验），跳过重复解析——人工标注回填时使用")

    # ===== answer 子命令 =====
    p_answer = subparsers.add_parser("answer", help="模块4：GT 冻结后评估任意算法")
    p_answer.add_argument("--input", required=True, help="输入 JSONL 文件或文件夹路径")
    p_answer.add_argument("--piplup-orig", default=None, help="PIPLUP 原版模块路径（双版模式）")
    p_answer.add_argument("--piplup-opt", default=None, help="PIPLUP 优化版模块路径（双版模式）")
    p_answer.add_argument("--postprocessing-orig", default=None, help="原版后处理模块路径")
    p_answer.add_argument("--preprocessing-orig", default=None, help="原版预处理模块路径")
    p_answer.add_argument("--gt", required=True, help="GT JSONL 文件路径（模块4输出 final_gt.jsonl）")
    p_answer.add_argument("--reuse-runs", default=None, help="复用模块3已落盘的 runs 目录（跳过重复解析，自动校验输入签名；仅双版模式）")
    p_answer.add_argument("--parsers", default=None, help="待评估算法模块路径（逗号分隔，可多个；与 --piplup-orig/--piplup-opt 二选一）")
    p_answer.add_argument("--gt-alt", default=None, help="GT 备变体路径（默认自动探测 --gt 同目录 final_gt_alt.jsonl）")
    p_answer.add_argument("--output", required=True, help="结果输出目录")
    p_answer.add_argument("--module", default="Messages", help="日志模块名")
    p_answer.add_argument("--warm-start", action="store_true", help="预热模式（默认冷启动清缓存）")
    p_answer.add_argument("--scenarios", default=None, help="场景列表，逗号分隔")
    p_answer.add_argument("--data-root", default=DEFAULT_DATA_ROOT, help=f"数据根目录（默认: {DEFAULT_DATA_ROOT}，可用环境变量 EVAL_PIPELINE_DATA_ROOT 覆盖）")

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
            preprocessing_orig_path=args.preprocessing_orig,
            cold_start=not args.warm_start,
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
            preprocessing_orig_path=args.preprocessing_orig,
            postprocessing_orig_path=args.postprocessing_orig,
            llm_config_path=args.llm_config,
            cold_start=not args.warm_start,
            module_name=args.module,
            scenarios=scenarios,
            data_root=args.data_root,
            live=args.live,
            dry_run=args.dry_run,
            limit=args.limit,
            retry_failed=args.retry_failed,
            retry_lines_path=args.retry_lines,
            review_adjudicated=args.review_adjudicated,
            save_prompts=args.save_prompts,
            concurrency=args.concurrency,
            audit_consistent=args.audit_consistent,
            no_eval=args.no_eval,
            human_annotations_path=args.human_annotations,
            reuse_runs=args.reuse_runs,
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
            parsers=([p.strip() for p in args.parsers.split(',') if p.strip()]
                     if args.parsers else None),
            preprocessing_orig_path=args.preprocessing_orig,
            postprocessing_orig_path=args.postprocessing_orig,
            cold_start=not args.warm_start,
            reuse_runs=args.reuse_runs,
            gt_alt_path=args.gt_alt,
            module_name=args.module,
            scenarios=scenarios,
            data_root=args.data_root,
        )


if __name__ == '__main__':
    main()

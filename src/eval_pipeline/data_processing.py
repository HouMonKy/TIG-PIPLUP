"""模块1：数据处理 — 读取 .log 纯文本日志 → 生成 JSONL。

CLI:
    python -m eval_pipeline data --input <path> --output <jsonl_path> [--module Messages]

--input 可以是单个 .log 文件或文件夹（文件夹则 rglob *.log 递归读取）。
"""
import argparse
import sys
from pathlib import Path

# 确保 PROJECT_ROOT 在 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from eval_pipeline._shared.data_loader import load_raw_lines, write_jsonl


def process_log_to_jsonl(input_path: str, output_path: str, module: str = "Messages"):
    """读取 .log 纯文本文件，生成 JSONL。

    Args:
        input_path: .log 文件或文件夹路径
        output_path: 输出 JSONL 路径
        module: 日志模块名
    """
    print(f"输入: {input_path}")
    print(f"输出: {output_path}")
    print(f"模块: {module}")

    raw_lines = load_raw_lines(input_path)
    print(f"读取 {len(raw_lines)} 行非空日志")

    records = []
    for i, raw in enumerate(raw_lines):
        records.append({
            'rawLog': raw,
            'module': module,
            'source': 'eval_pipeline',
            'logId': str(i + 1),
        })

    write_jsonl(output_path, records)
    print(f"已写入 {len(records)} 条记录到 {output_path}")


def main():
    parser = argparse.ArgumentParser(description="数据处理：.log → JSONL")
    parser.add_argument("--input", required=True, help="输入 .log 文件或文件夹路径")
    parser.add_argument("--output", required=True, help="输出 JSONL 路径")
    parser.add_argument("--module", default="Messages", help="日志模块名（默认 Messages）")

    args = parser.parse_args()
    process_log_to_jsonl(args.input, args.output, args.module)


if __name__ == '__main__':
    main()

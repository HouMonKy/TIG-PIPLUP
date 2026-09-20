"""数据加载工具：.log 纯文本 → log_info dicts；JSONL 读写。"""
import json
from pathlib import Path
from typing import List


def load_raw_lines(input_path: str) -> List[str]:
    """从 .log 文件或文件夹加载原始日志行（递归 rglob *.log）。

    复制自 run_final_optimization.py:load_raw_lines。
    """
    p = Path(input_path)
    if p.is_file():
        files = [p]
    else:
        files = sorted(p.rglob('*.log'))
    lines = []
    for fp in files:
        with open(fp, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                line = line.strip()
                if line:
                    lines.append(line)
    return lines


def load_raw_lines_by_scenarios(data_root: str, scenario_names) -> List[str]:
    """按场景名加载日志行（场景名为前缀，匹配 {scenario}_* 目录下的 all_merged_messages_logs.log）。

    复制自 run_final_optimization.py:load_raw_lines 的场景版。
    """
    if isinstance(scenario_names, str):
        scenario_names = [scenario_names]
    base_path = Path(data_root)
    lines = []
    for scenario in scenario_names:
        prefix_with_sep = f"{scenario}_"
        candidate_dirs = [p for p in base_path.iterdir() if p.is_dir() and p.name.startswith(prefix_with_sep)]
        target_files = []
        for d in candidate_dirs:
            target_files.extend(d.rglob('all_merged_messages_logs.log'))
        for fp in sorted(target_files):
            with open(fp, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        lines.append(line)
    return lines


def build_log_info(raw_lines: List[str], module: str = "Messages") -> List[dict]:
    """将原始日志行转为 parse_logs_dem / parse_logs_piplup_adapter 所需的 log_info 格式。

    复制自 run_final_optimization.py:build_log_info。
    """
    log_info = []
    for i, raw in enumerate(raw_lines):
        log_info.append({
            'rawLog': raw,
            'module': module,
            'source': 'eval_pipeline',
            'logId': str(i + 1),
        })
    return log_info


def load_jsonl(path: str) -> List[dict]:
    """加载 JSONL 文件，返回字典列表。"""
    records = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def write_jsonl(path, records: List[dict]):
    """写入 JSONL 文件。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, 'w', encoding='utf-8') as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')


def load_json(path: str) -> dict:
    """加载 JSON 文件。"""
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def write_json(path, data: dict, indent=2):
    """写入 JSON 文件。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)

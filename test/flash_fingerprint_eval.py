#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
闪存异常日志诊断准确率验证脚本
用于对比不同版本解析算法的诊断结果与标准答案的准确率
"""

import os
import json
import sys
import pandas as pd
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from src.common.global_config import GlobalConfig, config

def normalize(s: str) -> str:
    """标准化字符串：去除多余空格、转小写"""
    return ' '.join(s.lower().split())


def get_gt(case_dir_name: str, report_name_to_code: dict) -> tuple:
    """获取 GT，使用模糊匹配"""
    d = normalize(case_dir_name)
    best_match = None
    best_ratio = 0.0

    for report_name, code in report_name_to_code.items():
        r = normalize(report_name)
        if r in d or d in r:
            return code, 1.0
        ratio = SequenceMatcher(None, d, r).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_match = code
    print(f"⚠️  案例 '{case_dir_name}' 最佳匹配: {best_match} (相似度: {best_ratio:.2f})")
    return best_match, best_ratio


def extract_diagnosis(case_path: Path) -> list:
    """从 event_reports.json 提取所有的 lognet 诊断结果"""
    diagnoses = set()
    for filepath in case_path.rglob('*event_reports.json'):
        try:
            with filepath.open('r', encoding='utf-8') as fp:
                data = json.load(fp)
                if isinstance(data, list):
                    for item in data:
                        cn = item.get('case_name')
                        if cn:
                            diagnoses.add(cn)
        except (json.JSONDecodeError, OSError) as e:
            print(f"Warning: Failed to parse {filepath.name} - {e}", file=sys.stderr)

    return sorted(list(diagnoses))


def check_correct(res_list: list, gt: str) -> str:
    """判断结果一致性（带 Emoji 状态标识）"""
    if not res_list:
        return '⚪ 未诊断'

    if gt == '未匹配':
        return '➖ 无GT对照'

    if len(res_list) == 1 and res_list[0] == gt:
        return '✅ 完全一致'
    elif gt in res_list:
        return '🟡 包含GT'
    else:
        return '❌ 不包含GT'


def main():
    sys.stdout.reconfigure(encoding='utf-8')

    # ================= 配置区 =================
    dataset_dir = Path(config.get_dataset_root())
    base_dir = dataset_dir / "output" / "Flash"
    excel_file = base_dir / "run_交行案例_template" / "闪存异常日志检测故障模式案例库.xlsx"

    versions = {
        "Drain": "run_交行案例_template_output",
        "PIPLUP": "run_交行案例_template_output_piplup",
    }

    # ================= 读取 Excel =================
    try:
        df = pd.read_excel(excel_file)
    except Exception as e:
        print(f"Error loading Excel: {e}")
        return

    report_name_to_code = {}
    for _, row in df.iterrows():
        report_name = str(row.get('案例报告名称', '')).replace('.docx', '').strip()
        code = row.get('案例编码')
        if report_name and report_name != 'nan':
            report_name_to_code[report_name] = code

    # ================= 提取各版本结果 =================
    all_results = {v: {} for v in versions}
    all_dirs_set = set()

    for version_name, version_dir_name in versions.items():
        version_path = base_dir / version_dir_name
        if not version_path.exists() or not version_path.is_dir():
            print(f"警告: 路径不存在或非目录 - {version_path}")
            continue

        for case_dir in version_path.iterdir():
            if case_dir.is_dir():
                all_results[version_name][case_dir.name] = extract_diagnosis(case_dir)
                all_dirs_set.add(case_dir.name)

    all_dirs = sorted(all_dirs_set)

    # ================= 预计算 GT =================
    gt_cache = {}
    for case_name in all_dirs:
        gt_code, _ = get_gt(case_name, report_name_to_code)
        gt_cache[case_name] = gt_code or '未匹配'

    # ================= 构建统计与对比数据 =================
    comparison_data = []
    stats = {
        v: {'total_diagnosed': 0, 'exact': 0, 'contains': 0, 'not_contains': 0, 'strict_acc': 0.0, 'loose_acc': 0.0} for
        v in versions}

    for case_name in all_dirs:
        gt = gt_cache[case_name]
        row = {'案例目录': case_name, 'GT': gt}

        for version_name in versions:
            res_list = all_results[version_name].get(case_name, [])
            res_str = ", ".join(res_list) if res_list else "未诊断"
            is_correct = check_correct(res_list, gt)

            row[f'{version_name}_结果'] = res_str
            row[f'{version_name}_对错'] = is_correct

            if is_correct not in ['⚪ 未诊断', '➖ 无GT对照'] and gt != '未匹配':
                stats[version_name]['total_diagnosed'] += 1
                if is_correct == '✅ 完全一致':
                    stats[version_name]['exact'] += 1
                elif is_correct == '🟡 包含GT':
                    stats[version_name]['contains'] += 1
                elif is_correct == '❌ 不包含GT':
                    stats[version_name]['not_contains'] += 1

        comparison_data.append(row)

    for version_name in versions:
        s = stats[version_name]
        if s['total_diagnosed'] > 0:
            s['strict_acc'] = (s['exact'] / s['total_diagnosed']) * 100
            s['loose_acc'] = ((s['exact'] + s['contains']) / s['total_diagnosed']) * 100

    # ================= 终端输出验证结果 =================
    df_comparison = pd.DataFrame(comparison_data)
    df_comparison.index = range(1, len(df_comparison) + 1)
    pd.set_option('display.unicode.east_asian_width', True)

    print("=" * 140)
    print("闪存异常日志诊断准确率验证报告")
    print("=" * 140)
    print(df_comparison.to_string(index=True))
    print("-" * 140)

    print("\n统计结果 (只统计已诊断且GT匹配的案例):")
    for version_name, s in stats.items():
        print(f"  {version_name}:")
        print(f"    总诊断数: {s['total_diagnosed']}")
        print(f"    完全一致: {s['exact']} | 包含GT: {s['contains']} | 不包含GT: {s['not_contains']}")
        print(f"    严格准确率 (完全一致): {s['strict_acc']:.2f}%")
        print(f"    宽泛准确率 (包含GT):   {s['loose_acc']:.2f}%\n")
    print("=" * 140)

    # ================= 输出 Markdown 文件 =================
    md_lines = [
        "# 闪存异常日志诊断准确率验证报告\n",
        "## 概述\n",
        f"- **生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- **Excel案例库**: {excel_file}",
        f"- **总案例数**: {len(all_dirs)} 个",
        f"- **对比版本数**: {len(versions)} 个\n",

        "## 指标说明\n",
        "- **✅ 完全一致 / 严格准确率**: 算法输出仅有一个诊断结果，且与真实标签 (GT) 完全相同。",
        "- **🟡 包含GT / 宽泛准确率**: 算法输出了多个诊断结果，真实标签 (GT) 位于其输出集合之中。（计算宽泛准确率时，分子 = 完全一致数 + 包含GT数）",
        "- **❌ 不包含GT**: 算法有诊断结果，但未能命中真实标签。",
        "- **⚪ 未诊断**: 算法未能输出任何诊断结果。",
        "- **➖ 无GT对照**: 在案例库 Excel 中未能匹配到该案例的真实标签，不计入准确率分母。\n",

        "## 统计对比\n"
    ]

    stats_df = pd.DataFrame({
        '指标': ['总诊断数', '✅ 完全一致', '🟡 包含GT', '❌ 不包含GT', '严格准确率', '宽泛准确率'],
        **{v: [
            stats[v]['total_diagnosed'],
            stats[v]['exact'],
            stats[v]['contains'],
            stats[v]['not_contains'],
            f"{stats[v]['strict_acc']:.2f}%",
            f"{stats[v]['loose_acc']:.2f}%"
        ] for v in versions}
    })
    md_lines.append(stats_df.to_markdown(index=False))
    md_lines.append("\n\n## 详细对比\n")

    df_md = df_comparison.copy()
    md_lines.append(df_md.to_markdown(index=True))
    md_lines.append("\n\n## 结论\n")

    for version_name, s in stats.items():
        md_lines.append(
            f"- **{version_name}** | 严格准确率: {s['strict_acc']:.2f}% | 宽泛准确率: {s['loose_acc']:.2f}%")

    md_out_path = base_dir / 'run_交行案例_template_诊断准确率验证报告.md'
    with md_out_path.open('w', encoding='utf-8') as f:
        f.write('\n'.join(md_lines) + '\n')

    print(f"报告已保存: {md_out_path.name}")


if __name__ == '__main__':
    main()
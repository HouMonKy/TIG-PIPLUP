#!/usr/bin/env python3.7
# -*- coding: utf-8 -*-
"""PIPLUP 版本间解析质量对比工具（行对分组口径，不依赖簇对齐）。

解决的问题：
  - 簇数量变化：不比簇，比"行的分组关系"（哪些行共模板）
  - 相同日志分到不同簇：这正是被度量的质量差异，由指标量化

用法：
  # 1) 分别用两个版本解析同一批日志，落盘行级结果（每行一条 JSON）：
  #    {"logId": 1, "template": "...", "raw": "原始日志行(可选)"}
  # 2) 对比：
  python test/piplup_parser_compare.py result_old.jsonl result_new.jsonl
  python test/piplup_parser_compare.py a.jsonl b.jsonl --raw-a a.jsonl --raw-b a.jsonl \
      --diff-out diffs.jsonl --diff-top 20

指标：
  一致性层   ARI / 双向 GA / Pairwise P-R-F1
  质量层     重复行一致性（相同原始行必须同模板）/ 模板数 / 组大小分布
  分歧层     最大分歧组对（A 组 ∩ B 组交叉块），供人工裁决
"""
import argparse
import json
import sys
from collections import Counter, defaultdict
from math import comb
from pathlib import Path


def load_jsonl(path):
    rows = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    rows.sort(key=lambda r: r['logId'])
    return rows


def ari(labels_a, labels_b):
    """调整兰德指数：1=完全一致, 0=随机水平, 负=反向。"""
    n = len(labels_a)
    if n < 2:
        return 1.0
    ca, cb = Counter(labels_a), Counter(labels_b)
    table = Counter(zip(labels_a, labels_b))
    sum_comb = sum(comb(c, 2) for c in table.values())
    sum_a = sum(comb(c, 2) for c in ca.values())
    sum_b = sum(comb(c, 2) for c in cb.values())
    total = comb(n, 2)
    expected = sum_a * sum_b / total
    max_index = (sum_a + sum_b) / 2
    if max_index == 0:
        return 1.0
    return (sum_comb - expected) / (max_index - expected)


def pairwise_f1(labels_a, labels_b):
    """同组行对的 P/R/F1（A 为预测、B 为参照方向 + 反向）。"""
    ca, cb = Counter(labels_a), Counter(labels_b)
    table = Counter(zip(labels_a, labels_b))
    overlap = sum(comb(c, 2) for c in table.values())
    pairs_a = sum(comb(c, 2) for c in ca.values())
    pairs_b = sum(comb(c, 2) for c in cb.values())
    p_a = overlap / pairs_a if pairs_a else 1.0
    r_a = overlap / pairs_b if pairs_b else 1.0
    f_a = 2 * p_a * r_a / (p_a + r_a) if (p_a + r_a) else 0.0
    return p_a, r_a, f_a


def grouping_accuracy(reference, candidate):
    """GA：以 reference 分组为参照，candidate 每组内最大一致行数占比。"""
    ref_groups = defaultdict(list)
    for i, r in enumerate(reference):
        ref_groups[r].append(i)
    correct = 0
    for lines in ref_groups.values():
        correct += max(Counter(candidate[i] for i in lines).values())
    return correct / len(reference)


def dup_consistency(rows):
    """重复行一致性：完全相同的原始行必须拿同一模板。返回违规组数。"""
    if not rows or 'raw' not in rows[0]:
        return None, 0
    groups = defaultdict(set)
    for r in rows:
        groups[r['raw']].add(r['template'])
    bad = {k: v for k, v in groups.items() if len(v) > 1}
    return (len(bad), sum(len(v) - 1 for v in bad.values())) if False else \
        (len(bad), len(groups))


def top_divergences(labels_a, labels_b, ids, top_n=20):
    """最大分歧组对：A 组 ∩ B 组交叉块（非纯块），按行数排序。"""
    cross = Counter(zip(labels_a, labels_b))
    # 纯块（该 A 组的所有行都在同一 B 组）不算分歧
    size_a = Counter(labels_a)
    blocks = []
    for (ga, gb), n in cross.items():
        if n < size_a[ga]:  # A 组被 B 拆开 → 分歧块
            blocks.append((n, ga, gb))
    blocks.sort(reverse=True)
    result = []
    for n, ga, gb in blocks[:top_n]:
        line_ids = [ids[i] for i in range(len(ids)) if labels_a[i] == ga and labels_b[i] == gb]
        result.append({'lines': n, 'a_template': ga, 'b_template': gb, 'logIds': line_ids[:50]})
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('result_a', help='版本 A 行级结果 jsonl（logId/template[/raw]）')
    ap.add_argument('result_b', help='版本 B 行级结果 jsonl')
    ap.add_argument('--diff-out', help='分歧块清单输出 jsonl')
    ap.add_argument('--diff-top', type=int, default=20)
    args = ap.parse_args()

    rows_a = load_jsonl(args.result_a)
    rows_b = load_jsonl(args.result_b)
    if [r['logId'] for r in rows_a] != [r['logId'] for r in rows_b]:
        print('错误：两份结果的 logId 集合不一致，无法对齐', file=sys.stderr)
        sys.exit(1)

    la = [r['template'] for r in rows_a]
    lb = [r['template'] for r in rows_b]
    ids = [r['logId'] for r in rows_a]

    print(f'行数: {len(la)}')
    print(f'模板数: A={len(set(la))}, B={len(set(lb))}')
    print()
    print('== 一致性（分组口径，与簇无关） ==')
    print(f'ARI            : {ari(la, lb):.4f}')
    p, r, f = pairwise_f1(la, lb)
    print(f'Pairwise(A→B)  : P={p:.4f} R={r:.4f} F1={f:.4f}')
    p2, r2, f2 = pairwise_f1(lb, la)
    print(f'Pairwise(B→A)  : P={p2:.4f} R={r2:.4f} F1={f2:.4f}')
    print(f'GA(A 参照评 B) : {grouping_accuracy(la, lb):.4f}')
    print(f'GA(B 参照评 A) : {grouping_accuracy(lb, la):.4f}')
    print()
    print('== 独立质量（各自硬指标） ==')
    for name, rows, labels in (('A', rows_a, la), ('B', rows_b, lb)):
        bad, total = dup_consistency(rows)
        if bad is not None:
            print(f'{name} 重复行一致性: 违规 {bad} / {total} 个不同行内容'
                  f'{"  ←←← 违反硬约束" if bad else "  ✓"}')
        sizes = sorted(Counter(labels).values(), reverse=True)
        if sizes:
            print(f'{name} 组大小分布: max={sizes[0]} median={sizes[len(sizes)//2]} '
                  f'单行组={sum(1 for s in sizes if s == 1)}')

    divs = top_divergences(la, lb, ids, args.diff_top)
    print()
    print(f'== 最大分歧块（A 组被 B 拆分的交叉块，共 {len(divs)} 个展示） ==')
    for d in divs[:10]:
        print(f"  {d['lines']:>6} 行 | A: {d['a_template'][:60]}")
        print(f"         | B: {d['b_template'][:60]}")
    if args.diff_out:
        with open(args.diff_out, 'w', encoding='utf-8') as f:
            for d in divs:
                f.write(json.dumps(d, ensure_ascii=False) + '\n')
        print(f'分歧块清单已写入 {args.diff_out}（供人工裁决）')


if __name__ == '__main__':
    main()

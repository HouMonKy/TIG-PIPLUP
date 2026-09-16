#!/usr/bin/env python3.7
# -*- coding: utf-8 -*-
"""PIPLUP 解析基准（自包含合成数据，可复跑）。

生成带种子的合成日志语料（kv 复合值、十六进制 ID、路径、IPv4、数字链、
括号结构等），测量：
  - 冷/热缓存下的 parse_list 吞吐与耗时
  - 峰值内存（tracemalloc，仅解析阶段）
  - 长期状态大小（storage_tree/logCluL pickle 字节数）
  - 模板数（簇数 / 输出模板数）

用法（仓库根目录）：
  python test/piplup_benchmark.py [--lines 3000] [--variants 40] [--json out.json]
"""
import argparse
import io
import json
import pickle
import random
import time
import tracemalloc

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data_process.template_extractor.parsing_algorithm.PIPLUP import LogParser

# 语料模板：{ph} 为变量槽位，覆盖常见日志结构
CORPUS_TEMPLATES = [
    'connection from {ip}:{port} accepted for user {user}',
    'task {taskid} started by scheduler on node node{nodenum}',
    'write file {path} done in {ms} ms',
    'name:fsi_snap_fs{fsnum}_{snapid} snapshot created on pool pool{poolnum}',
    '(xnet_x{xneta}:{xnetb})(num:{numv}){opword}',
    'RTOBJ_DP0x{hexa}_RV0x{hexb}_ID0x{hexc} applied',
    'cap:[{h1}_{h2}][{n1}_{n2}][{n3}_{n4}] updated',
    'apptype({appt}) objid({objid}) dsg kv over {sec}s elapsedTime:{ns} ns, kvnum({kv}) ret({ret}).',
    'error while processing request id: req_{reqid} code {code}',
    'job status:{status} progress {pct} percent',
    'query SELECT * FROM {table} WHERE id = {num} LIMIT {limit}',
    'device {serial} reported temperature {temp} C',
]


def _rand_hex(rng, n):
    return ''.join(rng.choice('0123456789abcdef') for _ in range(n))


def make_corpus(n_lines, n_variants, seed=20260914, with_provenance=False):
    """生成合成日志：每条基础模板产出 n_variants 个变量实例，循环填充至 n_lines。

    with_provenance=True 时同时返回每行的来源基础模板索引（用于真值评估：
    同源行应归并为同一输出模板，且不同源行不得共享输出模板——该真值
    由生成器明确定义，不以 Drain3 输出为真值）。"""
    rng = random.Random(seed)
    lines = []
    provenance = []
    for tpl_idx, tpl in enumerate(CORPUS_TEMPLATES):
        for _ in range(n_variants):
            lines.append(tpl.format(
                ip='10.%d.%d.%d' % (rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255)),
                port=rng.randint(1024, 65535),
                user='user%d' % rng.randint(1, 500),
                taskid=str(rng.randint(10000, 99999)),
                nodenum=str(rng.randint(1, 64)),
                path='/var/log/%s/%s.log' % ('abcdef'[rng.randint(0, 5)], rng.randint(1, 100)),
                ms=rng.randint(1, 5000),
                fsnum=str(rng.randint(1000, 9999)),
                snapid=str(rng.randint(100000, 999999)),
                poolnum=str(rng.randint(0, 32)),
                xneta='%d_%d_%d' % (rng.randint(0, 3), rng.randint(0, 8), rng.randint(0, 40)),
                xnetb=str(rng.randint(0, 100)),
                numv=str(rng.randint(0, 100)),
                opword=rng.choice(['server', 'client', 'bridge']),
                hexa=_rand_hex(rng, rng.randint(1, 4)),
                hexb=_rand_hex(rng, rng.randint(1, 4)),
                hexc=_rand_hex(rng, rng.randint(1, 4)),
                h1=_rand_hex(rng, 8), h2=_rand_hex(rng, 8),
                n1=str(rng.randint(0, 9)), n2=str(rng.randint(0, 9)),
                n3=str(rng.randint(0, 9)), n4=str(rng.randint(0, 9)),
                appt=str(rng.randint(0, 9)),
                objid=str(rng.randint(0, 999)),
                sec='%d.%d' % (rng.randint(0, 5), rng.randint(0, 9)),
                ns=str(rng.randint(10 ** 8, 10 ** 10)),
                kv=str(rng.randint(1, 64)),
                ret=str(rng.randint(0, 3)),
                reqid='%d_%d' % (rng.randint(1000, 9999), rng.randint(1000, 9999)),
                code=str(rng.randint(400, 599)),
                status=rng.choice(['running', 'pending', 'queued']),
                pct=str(rng.randint(0, 100)),
                table='tbl_%s' % rng.choice(['users', 'orders', 'items']),
                num=str(rng.randint(1, 10 ** 6)),
                limit=str(rng.choice([10, 50, 100])),
                serial='%d%s%d' % (rng.randint(10 ** 6, 10 ** 8 - 1),
                                   rng.choice(['CT', 'DH', 'ED']),
                                   rng.randint(10 ** 10, 10 ** 12 - 1)),
                temp=str(rng.randint(20, 90)),
            ))
            provenance.append(tpl_idx)
    # 打乱后循环填充到目标行数
    order = list(range(len(lines)))
    rng.shuffle(order)
    out, prov = [], []
    while len(out) < n_lines:
        for i in order:
            out.append(lines[i])
            prov.append(provenance[i])
            if len(out) >= n_lines:
                break
    if with_provenance:
        return out[:n_lines], prov[:n_lines]
    return out[:n_lines]


def _fixed_tokens(tpl):
    """基础模板中的固定词（不含变量槽的完整 token）。"""
    return [t for t in tpl.split() if '{' not in t and '}' not in t]


def ground_truth_eval(parser, lines, provenance):
    """合成真值评估（真值来自生成器来源定义，非 Drain3）。

    指标：
      over_split_groups       —— 同源行映射到多个输出模板的组数
      over_merged_templates   —— 被不同来源行共享的输出模板数（误合并）
      fixed_token_loss_lines  —— 固定词在其来源行的输出模板中缺失的行数
                                 （附明细；缺失可能是既有掩码语义，
                                 如 duration 规则连单位 ms 一起遮蔽）
      uncovered_lines         —— 输出模板不能覆盖其来源行的行数（含空输出）
    """
    results = parser.parse_list(lines)
    assert len(results) == len(lines)

    group_templates = {}
    for prov, tpl in zip(provenance, results):
        group_templates.setdefault(prov, set()).add(tpl)
    over_split_groups = sum(1 for s in group_templates.values() if len(s) > 1)

    template_sources = {}
    for prov, tpl in zip(provenance, results):
        template_sources.setdefault(tpl, set()).add(prov)
    over_merged_templates = sum(1 for s in template_sources.values() if len(s) > 1)

    import regex as re
    fixed_token_loss = 0
    fixed_token_loss_detail = {}
    uncovered_lines = 0
    for line, prov, tpl in zip(lines, provenance, results):
        if not tpl:
            uncovered_lines += 1
            continue
        missing = [w for w in _fixed_tokens(CORPUS_TEMPLATES[prov]) if w not in tpl]
        if missing:
            fixed_token_loss += 1
            key = str((prov, tuple(missing)))
            fixed_token_loss_detail[key] = fixed_token_loss_detail.get(key, 0) + 1
        parts = tpl.split('<*>')
        pattern = '.*?'.join(re.escape(p) for p in parts)
        if not re.fullmatch(pattern, line, re.DOTALL):
            uncovered_lines += 1

    return {
        'base_templates': len(CORPUS_TEMPLATES),
        'distinct_output_templates': len(set(results)),
        'over_split_groups': over_split_groups,
        'over_merged_templates': over_merged_templates,
        'fixed_token_loss_lines': fixed_token_loss,
        'fixed_token_loss_detail': fixed_token_loss_detail,
        'uncovered_lines': uncovered_lines,
    }


def measure(parser, lines, label, results):
    tracemalloc.start()
    t0 = time.perf_counter()
    out = parser.parse_list(lines)
    elapsed = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    results[label] = {
        'lines': len(lines),
        'seconds': round(elapsed, 4),
        'lines_per_sec': round(len(lines) / elapsed, 1),
        'peak_mem_mb': round(peak / 1048576, 2),
        'clusters': len(parser.logCluL),
        'distinct_templates': len(set(out)),
    }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--lines', type=int, default=3000)
    ap.add_argument('--variants', type=int, default=40)
    ap.add_argument('--json', type=str, default=None, help='结果 JSON 输出路径')
    ap.add_argument('--no-eval', action='store_true', help='跳过真值评估（只测性能）')
    args = ap.parse_args()

    corpus = make_corpus(args.lines, args.variants)
    print(f'corpus: {len(corpus)} lines, {len(CORPUS_TEMPLATES)} base templates, seed=20260914')

    results = {}

    # 冷缓存（模板缓存关闭）：全量预处理 + 树搜索
    p1 = LogParser(log_format='<Content>', module_name='bench', indir='.', outdir='/tmp/',
                   use_cache=False, use_template_cache=False)
    out1 = measure(p1, corpus, 'cold_no_template_cache', results)

    # 冷缓存（模板缓存开启）：首次解析填充两级缓存
    p2 = LogParser(log_format='<Content>', module_name='bench', indir='.', outdir='/tmp/',
                   use_cache=False, use_template_cache=True)
    out2 = measure(p2, corpus, 'cold_with_template_cache', results)

    # 热缓存：同一解析器再跑一遍相同语料（全部命中两级缓存）
    measure(p2, corpus, 'warm_template_cache', results)

    # 两级缓存开启下的短批次延迟（模拟在线流式：每批 20 行）
    p3 = LogParser(log_format='<Content>', module_name='bench', indir='.', outdir='/tmp/',
                   use_cache=False, use_template_cache=True)
    p3.parse_list(corpus)  # 预热
    batch_times = []
    for i in range(0, min(len(corpus), 1000), 20):
        t0 = time.perf_counter()
        p3.parse_list(corpus[i:i + 20])
        batch_times.append(time.perf_counter() - t0)
    results['stream_batch_20lines'] = {
        'batches': len(batch_times),
        'avg_ms': round(1000 * sum(batch_times) / len(batch_times), 3),
        'max_ms': round(1000 * max(batch_times), 3),
    }

    # 长期状态大小
    state = {'storage_tree': p1.storage_tree, 'shallow_tree': p1.shallow_tree,
             'logCluL': p1.logCluL}
    results['state_size'] = {
        'clusters': len(p1.logCluL),
        'pickle_mb': round(len(pickle.dumps(state)) / 1048576, 3),
    }

    # 缓存字典大小（空间成本）
    results['cache_size'] = {
        'template_cache_entries': len(p2.template_cache),
        'light_cache_entries': len(p2.light_cache),
    }

    # 一致性：缓存开关结果必须逐行一致
    mismatch = sum(1 for a, b in zip(out1, out2) if a != b)
    results['cache_consistency'] = {'mismatched_lines': mismatch}
    assert len(out1) == len(out2) == len(corpus)

    # 真值评估（合成真值来自生成器来源定义，不以 Drain3 输出为真值）
    if not args.no_eval:
        corpus_gt, provenance = make_corpus(args.lines, args.variants,
                                            with_provenance=True)
        eval_parser = LogParser(log_format='<Content>', module_name='bench',
                                indir='.', outdir='/tmp/', use_cache=False,
                                use_template_cache=False)
        results['ground_truth_eval'] = ground_truth_eval(eval_parser, corpus_gt,
                                                         provenance)

    print(json.dumps(results, indent=2, ensure_ascii=False))
    if args.json:
        with open(args.json, 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f'results written to {args.json}')


if __name__ == '__main__':
    main()

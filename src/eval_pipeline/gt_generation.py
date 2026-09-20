"""模块3：GT 生成 — 两版 PIPLUP 行级对齐 + LLM 裁决 → 版本中立的 GT 工件包。

CLI（一条命令产出全部质量结论）:
    python -m eval_pipeline gt \
        --input <jsonl_or_folder> \
        --piplup-orig <module> --preprocessing-orig <module> --postprocessing-orig <module> \
        --piplup-opt <module> \
        --llm-config <yaml> --output <gt_dir> --live \
        [--concurrency N] [--audit-consistent K] [--no-eval] \
        [--scenarios 01,02,...] [--module Messages] [--dry-run] [--retry-failed]

行级口径（簇不参与对齐，簇数差异不影响任何环节）：
- 对齐键是 logId。每行问"原版给这行的模板 vs 优化版给这行的模板"
- 归一化后相同 → 一致行，直接进 GT
- 不同 → 按（原版模板, 优化版模板）对聚合裁决任务（TaskID 内容寻址）

GT 构建三级逻辑：
- 一致行 → GT（可 --audit-consistent K 抽样 LLM 审计，量化"两版一起错"的共识错误率）
- 裁决 original/optimized → GT 取对应版本模板
- 裁决 equivalent → 双 GT 变体（主=原版，备=优化版），评估取均值消除偏向
- 裁决 neither + 合成模板通过程序校验（覆盖全部样本 + 固定词≥2）→ GT 取合成模板；
  校验失败 / insufficient_evidence / 未裁决 → excluded_lines.jsonl，计入覆盖率
- 输出 eval_report.json（默认自动评估，复用 runs，零额外解析）

GT 工件（供模块4 answer 评估任意第三方版本）：
  runs/{label}_result.jsonl + run_meta.json / line_pairs.jsonl /
  tasks/ + results/（裁决留痕）/ final_gt.jsonl + final_gt_alt.jsonl /
  excluded_lines.jsonl / consistent_audit.json / gt_summary.json / eval_report.json
"""
import hashlib
import json
import logging
import zlib
from pathlib import Path

from eval_pipeline._shared.data_loader import load_jsonl, write_jsonl, write_json
from eval_pipeline._shared.metrics import normalize_template
from eval_pipeline._shared.piplup_runner import run_piplup_pair
from eval_pipeline._shared.adjudication import (
    run_adjudication,
    ADJ_ORIGINAL, ADJ_OPTIMIZED, ADJ_EQUIVALENT, ADJ_NEITHER, ADJ_INSUFFICIENT,
    ALL_TASKS_FILE, _extract_json_from_response,
)

logger = logging.getLogger(__name__)


def _task_id_for(norm_orig: str, norm_opt: str) -> str:
    """内容寻址 TaskID：同对模板跨运行稳定，断点续跑不错位。"""
    digest = hashlib.sha1(
        (norm_orig + '\x00' + norm_opt).encode('utf-8')).hexdigest()[:16].upper()
    return f"GT_{digest}"


def _wildcard_cover(template: str, text: str) -> bool:
    """模板（<*> 通配）能否完整覆盖文本（行级覆盖校验，与算法版本无关）。

    空白折叠后按 <*> 切分字面量段做贪心匹配：首段前缀、中间段最早出现、
    末段后缀。"""
    text_n = ' '.join(text.split())
    if template == text_n:
        return True
    if '<*>' not in template:
        return False
    segs = template.split('<*>')
    n = len(text_n)
    head = segs[0]
    if head:
        if not text_n.startswith(head):
            return False
        pos = len(head)
    else:
        pos = 0
    for seg in segs[1:-1]:
        if not seg:
            continue
        idx = text_n.find(seg, pos)
        if idx == -1:
            return False
        pos = idx + len(seg)
    tail = segs[-1]
    if tail:
        start = n - len(tail)
        if start < pos or not text_n.startswith(tail, start):
            return False
    return True


def _build_raw_map(input_path, scenarios, data_root, module_name):
    """构建 log_id(str) → 原始日志行 映射（供裁决样本采样）。"""
    from eval_pipeline._shared.data_loader import (
        load_raw_lines, load_raw_lines_by_scenarios, build_log_info,
    )
    if input_path and str(input_path).endswith('.jsonl'):
        rows = load_jsonl(input_path)
        return {str(r['logId']): r['rawLog'] for r in rows if r.get('rawLog')}
    if scenarios:
        raw_lines = load_raw_lines_by_scenarios(data_root, scenarios)
    else:
        raw_lines = load_raw_lines(input_path)
    log_info = build_log_info(raw_lines, module_name)
    del raw_lines
    return {r['logId']: r['rawLog'] for r in log_info}


def _compare_clusters(results_orig, results_opt, raw_map):
    """行级对齐两版结果，生成一致 GT、裁决任务与行→任务映射。

    Returns:
        gt_tasks: 一致行列表（直接作为 GT）
        adjudication_tasks: 不一致裁决任务（按模板对去重，TaskID 内容寻址）
        line_pairs: 行级对齐明细（供 Step4 免重复归一化）
        without_counterpart: 某版缺失的行数（计入覆盖率）
    """
    orig_map = {r['log_id']: r for r in results_orig}
    opt_map = {r['log_id']: r for r in results_opt}

    # template_id → 行集合（供裁决样本采样：取两簇分歧行）
    orig_clusters = {}
    opt_clusters = {}
    for r in results_orig:
        orig_clusters.setdefault(r['template_id'], set()).add(str(r['log_id']))
    for r in results_opt:
        opt_clusters.setdefault(r['template_id'], set()).add(str(r['log_id']))

    gt_tasks = []
    adjudication_tasks = []
    line_pairs = []
    compared_pairs = {}
    seq = 0
    without_counterpart = 0

    for log_id, r_orig in orig_map.items():
        r_opt = opt_map.get(log_id)
        if r_opt is None:
            without_counterpart += 1
            continue

        tid_orig, tid_opt = r_orig['template_id'], r_opt['template_id']
        norm_orig = normalize_template(r_orig['template'])
        norm_opt = normalize_template(r_opt['template'])

        if norm_orig == norm_opt:
            gt_tasks.append({
                'log_id': log_id,
                'template': r_orig['template'],
                'template_id': tid_orig,
                'adjudication': 'consistent',
                'reason': '两版 PIPLUP 模板一致',
                'confidence': 1.0,
            })
            line_pairs.append({
                'log_id': log_id, 'consistent': True,
                'template_id_orig': tid_orig, 'template_id_opt': tid_opt,
                'TaskID': '',
            })
            continue

        pair_key = (tid_orig, tid_opt)
        task_id = _task_id_for(norm_orig, norm_opt)
        if pair_key not in compared_pairs:
            compared_pairs[pair_key] = task_id
            seq += 1

            # 裁决样本：两簇分歧行优先（一个版本归入而另一版未归入的行），
            # 再以共同行补足至 10 条——样本不足是 insufficient 的主因
            ids_orig = orig_clusters.get(tid_orig, set())
            ids_opt = opt_clusters.get(tid_opt, set())
            diff_ids = sorted(ids_orig ^ ids_opt)
            common_ids = sorted(ids_orig & ids_opt)
            sample_ids = diff_ids[:8]
            sample_ids += common_ids[:10 - len(sample_ids)]
            sample_logs = [raw_map[lid] for lid in sample_ids if lid in raw_map][:10]

            adjudication_tasks.append({
                'seq': seq,
                'TaskID': task_id,
                'piplup_ori': r_orig['template'],
                'piplup_opt': r_opt['template'],
                # 归一化模板对落盘：TaskID = sha1(norm_ori + \0 + norm_opt) 可审计
                'norm_ori': norm_orig,
                'norm_opt': norm_opt,
                'template_id_orig': tid_orig,
                'template_id_opt': tid_opt,
                'sample_log': sample_logs,
                'sample_diff_lines': len(diff_ids),
            })

        line_pairs.append({
            'log_id': log_id, 'consistent': False,
            'template_id_orig': tid_orig, 'template_id_opt': tid_opt,
            'TaskID': compared_pairs[pair_key],
        })

    return gt_tasks, adjudication_tasks, line_pairs, without_counterpart


def _load_adjudication_results(out: Path):
    """加载裁决结果与任务总账。返回 (adj_map, task_map, failed_ids)。"""
    results_dir = out / "results"
    adj_map, task_map, failed_ids = {}, {}, set()
    tasks_path = out / "tasks" / ALL_TASKS_FILE
    if tasks_path.exists():
        for t in load_jsonl(tasks_path):
            task_map[t['TaskID']] = t
    resolved_path = results_dir / "resolved_gt_tasks.jsonl"
    if resolved_path.exists():
        for r in load_jsonl(resolved_path):
            adj_map[r['TaskID']] = r
    failed_path = results_dir / "failed_gt_tasks.jsonl"
    if failed_path.exists():
        for r in load_jsonl(failed_path):
            failed_ids.add(r['TaskID'])
    return adj_map, task_map, failed_ids


def _validate_synthesized(proposal: str, sample_logs: list) -> bool:
    """neither 合成模板的硬校验：覆盖全部样本 + 固定词 ≥2。"""
    if not proposal or not sample_logs:
        return False
    fixed_words = [w for w in proposal.split() if w != '<*>']
    if len(fixed_words) < 2:
        return False
    return all(_wildcard_cover(proposal, log) for log in sample_logs)


def _build_final_gt(out: Path, consistent_tasks: list, line_pairs: list,
                    results_orig: list, results_opt: list):
    """构建主/备双 GT 变体与排除行清单。

    行级规则：
    - consistent/original → 两变体同模板
    - optimized           → 两变体均取优化版
    - equivalent          → 主=原版，备=优化版（评估均值消除偏向）
    - neither(合成通过)   → 两变体均取合成模板（EventId=crc32）
    - 其余                → excluded_lines（pending/failed/insufficient/
                             neither_invalid_proposal），计入覆盖率

    Returns:
        (final_gt_main, final_gt_alt, excluded)
    """
    adj_map, task_map, failed_ids = _load_adjudication_results(out)
    orig_map = {r['log_id']: r for r in results_orig}
    opt_map = {r['log_id']: r for r in results_opt}

    final_gt = list(consistent_tasks)
    final_gt_alt = list(consistent_tasks)
    excluded = []

    for lp in line_pairs:
        if lp['consistent']:
            continue
        log_id = lp['log_id']
        r_orig = orig_map.get(log_id)
        r_opt = opt_map.get(log_id)
        if r_orig is None or r_opt is None:
            excluded.append({'log_id': log_id, 'reason': 'no_counterpart'})
            continue

        task_id = lp['TaskID']
        adj = adj_map.get(task_id)
        if adj is None:
            reason = 'adjudication_failed' if task_id in failed_ids else 'pending'
            excluded.append({'log_id': log_id, 'reason': reason, 'TaskID': task_id})
            continue

        verdict = adj.get('Adjudication', '')
        base = {
            'log_id': log_id,
            'adjudication': verdict,
            'reason': adj.get('Reason', ''),
            'confidence': adj.get('Confidence', 0),
        }

        if verdict == ADJ_ORIGINAL:
            final_gt.append({**base, 'template': r_orig['template'],
                             'template_id': r_orig['template_id']})
            final_gt_alt.append({**base, 'template': r_orig['template'],
                                 'template_id': r_orig['template_id']})
        elif verdict == ADJ_OPTIMIZED:
            final_gt.append({**base, 'template': r_opt['template'],
                             'template_id': r_opt['template_id']})
            final_gt_alt.append({**base, 'template': r_opt['template'],
                                 'template_id': r_opt['template_id']})
        elif verdict == ADJ_EQUIVALENT:
            # 双变体：主=原版，备=优化版（等价行均值 0.5/0.5，无偏向）
            final_gt.append({**base, 'template': r_orig['template'],
                             'template_id': r_orig['template_id']})
            final_gt_alt.append({**base, 'template': r_opt['template'],
                                 'template_id': r_opt['template_id']})
        elif verdict == ADJ_NEITHER:
            proposal = adj.get('ProposedTemplate', '')
            task = task_map.get(task_id, {})
            if _validate_synthesized(proposal, task.get('sample_log', [])):
                synth_id = zlib.crc32(proposal.encode('utf-8'))
                record = {**base, 'template': proposal, 'template_id': synth_id}
                final_gt.append(record)
                final_gt_alt.append(dict(record))
            else:
                excluded.append({'log_id': log_id,
                                 'reason': 'neither_invalid_proposal',
                                 'TaskID': task_id})
        else:
            # insufficient_evidence 及未知裁决：无有效 GT，排除并报告
            excluded.append({'log_id': log_id, 'reason': verdict or 'unknown',
                             'TaskID': task_id})

    return final_gt, final_gt_alt, excluded


def _audit_consistent_lines(out: Path, consistent_tasks: list, raw_map: dict,
                            client, k: int, dry_run: bool):
    """抽样 K 个一致模板簇，LLM 审核模板是否正确代表样本行。

    量化"两版一起错"的共识错误率——GT 可信度的关键指标。
    """
    from eval_pipeline._shared.llm_client import LLMClient

    # 按模板聚合一致行，取样本
    groups = {}
    for t in consistent_tasks:
        groups.setdefault(t['template'], []).append(t['log_id'])
    ranked = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    sampled = ranked[:k]
    if not sampled:
        return {'sampled': 0, 'incorrect': 0, 'error_rate': 0.0, 'details': []}

    audit_prompt = """你是日志模板质量审核员。给定一个模板与它所代表的多条原始日志，判断模板是否正确：
- 模板只能使用 <*> 作为占位符
- 正确 = 能匹配全部日志，且字段名/动作词等固定语义未被吞掉；ID/数字/时间/地址等动态值应使用 <*>
只输出合法 JSON：
{"Verdict": "correct|incorrect", "Reason": "简要说明", "Confidence": 0.9}"""

    details = []
    incorrect = 0
    for template, log_ids in sampled:
        samples = [raw_map[lid] for lid in log_ids if lid in raw_map][:5]
        if not samples:
            continue
        if dry_run:
            details.append({'template': template, 'verdict': 'skipped',
                            'reason': 'dry-run', 'sample_lines': len(log_ids)})
            continue
        user_msg = f"模板:\n{template}\n\n原始日志:\n" + "\n".join(samples)
        verdict, reason = 'skipped', ''
        try:
            raw = client.chat([
                {"role": "system", "content": audit_prompt},
                {"role": "user", "content": user_msg},
            ])
            parsed = _extract_json_from_response(raw)
            if parsed and parsed.get('Verdict') in ('correct', 'incorrect'):
                verdict = parsed['Verdict']
                reason = parsed.get('Reason', '')
        except Exception as e:
            verdict, reason = 'error', str(e)
        if verdict == 'incorrect':
            incorrect += 1
        details.append({'template': template, 'verdict': verdict, 'reason': reason,
                        'sample_lines': len(log_ids)})

    n_judged = sum(1 for d in details if d['verdict'] in ('correct', 'incorrect'))
    result = {
        'sampled': len(details),
        'judged': n_judged,
        'incorrect': incorrect,
        'error_rate': (incorrect / n_judged) if n_judged else 0.0,
        'details': details,
    }
    write_json(out / "consistent_audit.json", result)
    logger.info(f"一致行审计: 抽样 {result['sampled']}，判定 {n_judged}，"
                f"错误 {incorrect}（共识错误率 {result['error_rate']:.1%}）")
    return result


def generate_gt(
    input_path: str,
    piplup_orig_path: str,
    piplup_opt_path: str,
    output_dir: str,
    preprocessing_orig_path: str = None,
    postprocessing_orig_path: str = None,
    llm_config_path: str = None,
    cold_start: bool = True,
    module_name: str = "Messages",
    scenarios: list = None,
    data_root: str = None,
    live: bool = False,
    dry_run: bool = False,
    limit: int = 0,
    retry_failed: bool = False,
    save_prompts: bool = False,
    concurrency: int = 0,
    audit_consistent: int = 0,
    no_eval: bool = False,
    client=None,
):
    """GT 生成主流程（一条命令产出 GT 工件包 + 两版质量评估报告）。"""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # ===== Step 1: 运行两版 PIPLUP（冷启动默认并行） =====
    print(f"\n{'='*60}")
    print(f"  Step 1: 运行两版 PIPLUP（行级解析结果落盘）")
    print(f"{'='*60}")

    run_dir = out / "runs"
    results_orig, clusters_orig, results_opt, clusters_opt = run_piplup_pair(
        input_path=input_path, output_dir=str(run_dir), module_name=module_name,
        cold_start=cold_start, scenarios=scenarios, data_root=data_root,
        piplup_orig_path=piplup_orig_path,
        preprocessing_orig_path=preprocessing_orig_path,
        postprocessing_orig_path=postprocessing_orig_path,
        piplup_opt_path=piplup_opt_path,
    )
    write_json(run_dir / "run_meta.json", {
        'input_path': str(input_path),
        'module': module_name,
        'scenarios': scenarios or [],
        'n_lines': len(results_orig),
    })

    # ===== Step 2: 行级对齐 =====
    print(f"\n{'='*60}")
    print(f"  Step 2: 行级对齐两版结果（簇不参与对齐）")
    print(f"{'='*60}")

    raw_map = _build_raw_map(input_path, scenarios, data_root, module_name)
    gt_tasks, adjudication_tasks, line_pairs, without_counterpart = _compare_clusters(
        results_orig, results_opt, raw_map)

    print(f"  一致: {len(gt_tasks)} 行")
    print(f"  不一致: {len(line_pairs) - len(gt_tasks)} 行"
          f"（聚合为 {len(adjudication_tasks)} 个裁决任务）")

    write_jsonl(out / "line_pairs.jsonl", line_pairs)
    tasks_dir = out / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    all_tasks_path = tasks_dir / ALL_TASKS_FILE
    write_jsonl(all_tasks_path, adjudication_tasks)
    write_jsonl(out / "consistent_gt.jsonl", gt_tasks)

    # ===== Step 3: LLM 裁决 =====
    if adjudication_tasks:
        print(f"\n{'='*60}")
        print(f"  Step 3: LLM 裁决不一致模板对")
        print(f"{'='*60}")
        if dry_run:
            print("  [dry-run] 仅生成 Prompt，不调用 LLM")
        elif not live:
            print("  [mock] 使用 Mock 客户端")

        summary = run_adjudication(
            run_root=out,
            input_path=all_tasks_path,
            config_path=llm_config_path,
            live=live,
            dry_run=dry_run,
            limit=limit,
            concurrency=concurrency,
            retry_failed=retry_failed,
            save_prompts=save_prompts,
            client=client,
        )
    else:
        summary = {"total_task_count": 0, "resolved_task_count": 0,
                   "failed_task_count": 0, "pending_task_count": 0}

    # ===== Step 4: 生成双变体 GT 与排除行 =====
    print(f"\n{'='*60}")
    print(f"  Step 4: 生成最终 GT（双变体）与排除行清单")
    print(f"{'='*60}")

    final_gt, final_gt_alt, excluded = _build_final_gt(
        out, gt_tasks, line_pairs, results_orig, results_opt)

    write_jsonl(out / "final_gt.jsonl", final_gt)
    write_jsonl(out / "final_gt_alt.jsonl", final_gt_alt)
    write_jsonl(out / "excluded_lines.jsonl", excluded)

    excluded_dist = {}
    for e in excluded:
        excluded_dist[e['reason']] = excluded_dist.get(e['reason'], 0) + 1
    total_lines = len(line_pairs)
    gt_coverage = len(final_gt) / total_lines if total_lines else 0.0
    print(f"  GT: {len(final_gt)} 行（覆盖率 {gt_coverage:.1%}）"
          f" | 排除 {len(excluded)} 行: {excluded_dist or '{}'}")

    # ===== Step 5（可选）: 一致行抽样审计 =====
    audit_result = None
    if audit_consistent > 0 and gt_tasks:
        print(f"\n{'='*60}")
        print(f"  Step 5: 一致行抽样审计（共识错误率）")
        print(f"{'='*60}")
        if client is None:
            from eval_pipeline._shared.llm_client import create_client
            audit_client = create_client(config_path=llm_config_path,
                                         mock=(not live), dry_run=dry_run)
        else:
            audit_client = client
        audit_result = _audit_consistent_lines(
            out, gt_tasks, raw_map, audit_client, audit_consistent, dry_run)
        print(f"  审计: 抽样 {audit_result['sampled']}，"
              f"共识错误率 {audit_result['error_rate']:.1%}")

    # ===== 汇总 =====
    gt_summary = {
        "total_clusters_orig": len(clusters_orig),
        "total_clusters_opt": len(clusters_opt),
        "total_lines": total_lines,
        "consistent": len(gt_tasks),
        "inconsistent_lines": len(line_pairs) - len(gt_tasks),
        "adjudication_tasks": len(adjudication_tasks),
        "adjudicated": summary.get("resolved_task_count", 0),
        "adjudication_distribution": summary.get("adjudication_distribution", {}),
        "gt_lines": len(final_gt),
        "gt_coverage": round(gt_coverage, 4),
        "excluded_lines": excluded_dist,
        "lines_without_counterpart": without_counterpart,
        "synthesized_templates": sum(
            1 for r in final_gt if r.get('adjudication') == ADJ_NEITHER),
        "consistent_audit": ({'sampled': audit_result['sampled'],
                              'judged': audit_result['judged'],
                              'error_rate': audit_result['error_rate']}
                             if audit_result else None),
    }
    write_json(out / "gt_summary.json", gt_summary)
    print(f"  汇总已保存: {out / 'gt_summary.json'}")

    # ===== Step 6（默认）: 自动评估两版 vs GT（复用 runs，零额外解析） =====
    if not no_eval:
        print(f"\n{'='*60}")
        print(f"  Step 6: 两版 vs GT 质量评估（双变体均值）")
        print(f"{'='*60}")
        from eval_pipeline.answer_generation import (
            evaluate_parsers_against_gt, _print_eval_report,
        )
        eval_report = evaluate_parsers_against_gt(
            final_gt,
            {'PIPLUP原版': results_orig, 'PIPLUP优化版': results_opt},
            final_gt_alt,
        )
        eval_report['gt_path'] = str(out / 'final_gt.jsonl')
        write_json(out / "eval_report.json", eval_report)
        _print_eval_report(eval_report)
    else:
        print("\n  [no-eval] 跳过自动评估")

    return gt_summary

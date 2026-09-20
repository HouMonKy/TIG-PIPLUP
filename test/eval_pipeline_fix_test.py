# -*- coding: utf-8 -*-
"""eval_pipeline 原版/优化版三文件改绑机制的回归测试。

覆盖：
1. 原版解析器使用原版 preprocess + 原版 correct_single_template
   （preprocessing 差异此前从未被处理，postprocessing 的 monkey-patch
   在 compare 模块的 import 顺序下是无效的——本测试锁定改绑机制）
2. 优化版解析器使用优化版模块（含 token_structure 引用不受改绑影响）
3. 改绑不依赖 import 顺序（先 import 解析器再改绑同样生效）
4. gt 全流程（mock LLM）冒烟：一致/不一致行的 GT 生成
"""
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(PROJECT_ROOT), str(PROJECT_ROOT / 'src'), str(Path(__file__).parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

FIXTURES = "eval_fixtures"


def _write_input(tmp_path, lines):
    p = tmp_path / "input.jsonl"
    with open(p, 'w', encoding='utf-8') as f:
        for i, line in enumerate(lines):
            f.write(json.dumps({
                'rawLog': line, 'module': 'Messages',
                'source': 'eval_fixtures', 'logId': str(i + 1),
            }, ensure_ascii=False) + '\n')
    return str(p)


def test_original_parser_uses_original_pre_and_postprocessing(tmp_path):
    """原版解析器必须使用原版 preprocess + 原版 correct_single_template。

    原版 PIPLUP 通过 `from .preprocessing import preprocess` 引用当前版
    模块——pipeline 必须改绑，否则"原版"实际混用优化版预处理
    （postprocessing 的旧 monkey-patch 还依赖 import 顺序）。
    """
    import importlib
    from eval_pipeline._shared.piplup_runner import _install_original_modules

    parser_mod = importlib.import_module(f"{FIXTURES}.PIPLUP_original")
    # 改绑前：引用的是当前版（fixtures 里的 OPT 函数）
    assert parser_mod.preprocess('x') == 'OPT_PP(x)'
    assert parser_mod.correct_single_template('x') == 'OPT_PPT(x)'

    _install_original_modules(
        parser_mod,
        f"{FIXTURES}.preprocessing_original",
        f"{FIXTURES}.postprocessing_original",
    )
    # 改绑后：原版函数生效
    assert parser_mod.preprocess('x') == 'ORIG_PP(x)'
    assert parser_mod.correct_single_template('x') == 'ORIG_PPT(x)'


def test_rebinding_works_regardless_of_import_order(tmp_path):
    """先 import 解析器、后改绑——旧 monkey-patch 在此顺序下失效，改绑必须生效。"""
    import importlib
    from eval_pipeline._shared.piplup_runner import _install_original_modules

    # 模拟 runner.py 顺序：先拿 parser_class
    parser_class = importlib.import_module(f"{FIXTURES}.PIPLUP_original").LogParser
    # 再改绑
    parser_mod = importlib.import_module(f"{FIXTURES}.PIPLUP_original")
    _install_original_modules(parser_mod,
                              f"{FIXTURES}.preprocessing_original",
                              f"{FIXTURES}.postprocessing_original")

    parser = parser_class()
    out = parser.parse_list(['line1', 'line2'])
    assert out == ['ORIG_PP(line1)|ORIG_PPT(line1)', 'ORIG_PP(line2)|ORIG_PPT(line2)']


def test_optimized_parser_unaffected_and_uses_token_structure():
    """优化版解析器不做改绑，优化版模块（含 token_structure 引用）原样工作。"""
    import importlib
    parser_class = importlib.import_module(f"{FIXTURES}.PIPLUP_optimized").LogParser
    parser = parser_class()
    out = parser.parse_list(['a'])
    assert out == ['OPT_PP(a)|OPT_PPT(a)']
    assert parser.used_token_structure == 'token_structure_available'


def test_gt_end_to_end_mock(tmp_path):
    """gt 全流程冒烟（mock LLM）：一致行直接进 GT，不一致行按裁决选择。"""
    import importlib
    from eval_pipeline._shared.piplup_runner import _install_original_modules

    # 让原版与优化版产生可预期的分歧：原版预处理给 ORIG 前缀，优化版给 OPT 前缀
    # → 两版模板文本必然不同 → 全部行进入裁决；mock 按 hash 决断
    parser_mod = importlib.import_module(f"{FIXTURES}.PIPLUP_original")
    _install_original_modules(parser_mod,
                              f"{FIXTURES}.preprocessing_original",
                              f"{FIXTURES}.postprocessing_original")

    lines = [f'event {i} happened now' for i in range(6)]
    input_path = _write_input(tmp_path, lines)
    gt_dir = tmp_path / "gt"

    from eval_pipeline.gt_generation import generate_gt
    summary = generate_gt(
        input_path=input_path,
        piplup_orig_path=f"{FIXTURES}.PIPLUP_original",
        piplup_opt_path=f"{FIXTURES}.PIPLUP_optimized",
        output_dir=str(gt_dir),
        preprocessing_orig_path=f"{FIXTURES}.preprocessing_original",
        postprocessing_orig_path=f"{FIXTURES}.postprocessing_original",
        live=False, dry_run=False, limit=0, retry_failed=False,
    )

    # 每行必然出现在 final_gt 或 excluded_lines 之一（双变体 + 排除机制）
    final_gt = [json.loads(l) for l in
                (gt_dir / "final_gt.jsonl").read_text(encoding='utf-8').splitlines() if l.strip()]
    final_gt_alt = [json.loads(l) for l in
                    (gt_dir / "final_gt_alt.jsonl").read_text(encoding='utf-8').splitlines() if l.strip()]
    excluded = [json.loads(l) for l in
                (gt_dir / "excluded_lines.jsonl").read_text(encoding='utf-8').splitlines() if l.strip()]
    assert len(final_gt) + len(excluded) == 6
    gt_ids = {r['log_id'] for r in final_gt}
    ex_ids = {r['log_id'] for r in excluded}
    assert not (gt_ids & ex_ids)
    assert gt_ids | ex_ids == {str(i + 1) for i in range(6)}
    assert len(final_gt_alt) == len(final_gt)
    # 每行裁决状态合法
    for r in final_gt:
        assert r['adjudication'] in ('consistent', 'original', 'optimized',
                                     'equivalent', 'neither')
    for e in excluded:
        assert e['reason'] in ('pending', 'adjudication_failed',
                               'insufficient_evidence', 'neither_invalid_proposal',
                               'no_counterpart')
    # runs 结果已落盘（供 answer --reuse-runs）
    assert (gt_dir / "runs" / "piplup_orig_result.jsonl").exists()
    assert (gt_dir / "runs" / "piplup_opt_result.jsonl").exists()
    assert (gt_dir / "runs" / "run_meta.json").exists()
    # line_pairs 已落盘
    assert (gt_dir / "line_pairs.jsonl").exists()
    # 任务总账 TaskID 为内容寻址（同对模板跨运行稳定）
    tasks = [json.loads(l) for l in
             (gt_dir / "tasks" / "all_gt_tasks.jsonl").read_text(encoding='utf-8').splitlines() if l.strip()]
    if tasks:
        import hashlib
        for t in tasks:
            expect = 'GT_' + hashlib.sha1(
                (t['norm_ori'] + '\x00' + t['norm_opt']).encode('utf-8')
            ).hexdigest()[:16].upper()
            assert t['TaskID'] == expect


def test_answer_reuse_runs(tmp_path):
    """answer --reuse-runs：跳过重复解析，直接评估 gt 落盘结果。"""
    import importlib
    from eval_pipeline._shared.piplup_runner import _install_original_modules

    parser_mod = importlib.import_module(f"{FIXTURES}.PIPLUP_original")
    _install_original_modules(parser_mod,
                              f"{FIXTURES}.preprocessing_original",
                              f"{FIXTURES}.postprocessing_original")

    lines = [f'event {i} happened now' for i in range(6)]
    input_path = _write_input(tmp_path, lines)
    gt_dir = tmp_path / "gt"

    from eval_pipeline.gt_generation import generate_gt
    generate_gt(
        input_path=input_path,
        piplup_orig_path=f"{FIXTURES}.PIPLUP_original",
        piplup_opt_path=f"{FIXTURES}.PIPLUP_optimized",
        output_dir=str(gt_dir),
        preprocessing_orig_path=f"{FIXTURES}.preprocessing_original",
        postprocessing_orig_path=f"{FIXTURES}.postprocessing_original",
        live=False, dry_run=False,
    )

    from eval_pipeline.answer_generation import generate_answers
    report = generate_answers(
        input_path=input_path,
        piplup_orig_path=f"{FIXTURES}.PIPLUP_original",
        piplup_opt_path=f"{FIXTURES}.PIPLUP_optimized",
        gt_path=str(gt_dir / "final_gt.jsonl"),
        output_dir=str(tmp_path / "answer"),
        preprocessing_orig_path=f"{FIXTURES}.preprocessing_original",
        postprocessing_orig_path=f"{FIXTURES}.postprocessing_original",
        reuse_runs=str(gt_dir / "runs"),
    )
    assert report['reused_runs'] is True
    # 期望 PA 从 GT 内容推导：mock 裁决可能选 optimized/equivalent，GT 模板随裁决走
    from eval_pipeline._shared.metrics import normalize_template
    final_gt = [json.loads(l) for l in
                (gt_dir / "final_gt.jsonl").read_text(encoding='utf-8').splitlines() if l.strip()]
    assert report['evaluated_lines'] == len(final_gt)
    gt_rows = {r['log_id']: r for r in final_gt}
    orig_out = {}
    for _line in (gt_dir / "runs" / "piplup_orig_result.jsonl").read_text(encoding='utf-8').splitlines():
        if _line.strip():
            _rec = json.loads(_line)
            orig_out[_rec['log_id']] = _rec
    final_gt_alt = [json.loads(l) for l in
                    (gt_dir / "final_gt_alt.jsonl").read_text(encoding='utf-8').splitlines() if l.strip()]
    gt_alt_rows = {r['log_id']: r for r in final_gt_alt}

    def _pa(gt_rows_x):
        return sum(
            1 for lid, gr in gt_rows_x.items()
            if normalize_template(gr['template']) == normalize_template(orig_out[lid]['template'])
        ) / len(gt_rows_x)

    # 报告 PA 为双变体均值
    expected_pa = (_pa(gt_rows) + _pa(gt_alt_rows)) / 2
    assert report['parsers']['PIPLUP原版']['PA'] == pytest.approx(expected_pa)


class ScriptedClient:
    """脚本化 LLM 客户端：按队列返回预设 JSON（测 neither/equivalent 等确定性路径）。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def is_configured(self):
        return True

    def get_model_name(self):
        return "scripted"

    def chat(self, messages, **kwargs):
        self.calls += 1
        idx = min(self.calls - 1, len(self.responses) - 1)
        return self.responses[idx]


def _make_gt_with_client(tmp_path, client, lines=None):
    """用脚本化客户端跑 gt 全流程，返回 (gt_dir, final_gt, final_gt_alt, excluded)。"""
    import importlib
    from eval_pipeline._shared.piplup_runner import _install_original_modules

    parser_mod = importlib.import_module(f"{FIXTURES}.PIPLUP_original")
    _install_original_modules(parser_mod,
                              f"{FIXTURES}.preprocessing_original",
                              f"{FIXTURES}.postprocessing_original")

    tmp_path = Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    lines = lines or [f'event {i} happened now' for i in range(4)]
    input_path = _write_input(tmp_path, lines)
    gt_dir = tmp_path / "gt"

    from eval_pipeline.gt_generation import generate_gt
    generate_gt(
        input_path=input_path,
        piplup_orig_path=f"{FIXTURES}.PIPLUP_original",
        piplup_opt_path=f"{FIXTURES}.PIPLUP_optimized",
        output_dir=str(gt_dir),
        preprocessing_orig_path=f"{FIXTURES}.preprocessing_original",
        postprocessing_orig_path=f"{FIXTURES}.postprocessing_original",
        live=True, dry_run=False, client=client,
    )
    final_gt = [json.loads(l) for l in
                (gt_dir / "final_gt.jsonl").read_text(encoding='utf-8').splitlines() if l.strip()]
    final_gt_alt = [json.loads(l) for l in
                    (gt_dir / "final_gt_alt.jsonl").read_text(encoding='utf-8').splitlines() if l.strip()]
    excluded = [json.loads(l) for l in
                (gt_dir / "excluded_lines.jsonl").read_text(encoding='utf-8').splitlines() if l.strip()]
    return gt_dir, final_gt, final_gt_alt, excluded


def _resp(adj, proposed=""):
    d = {"Adjudication": adj, "Reason": "scripted", "Confidence": 0.9}
    if proposed:
        d["ProposedTemplate"] = proposed
    return json.dumps(d, ensure_ascii=False)


def test_equivalent_dual_variant_unbiased(tmp_path):
    """equivalent → 双变体：主=原版、备=优化版；两版 PA 均值各 0.5。"""
    client = ScriptedClient([_resp('equivalent')])
    gt_dir, final_gt, final_gt_alt, excluded = _make_gt_with_client(tmp_path, client)

    assert len(excluded) == 0
    assert len(final_gt) == 4 and len(final_gt_alt) == 4
    # 每行：主变体 = 原版模板，备变体 = 优化版模板
    for r_main, r_alt in zip(final_gt, final_gt_alt):
        assert r_main['template'].startswith('ORIG_PP(')
        assert r_alt['template'].startswith('OPT_PP(')

    # 评估：双变体均值 → 两版 PA 各 0.5（等价行对称无偏）
    report = json.loads((gt_dir / "eval_report.json").read_text(encoding='utf-8'))
    assert report['gt_alt_used'] is True
    pa_orig = report['parsers']['PIPLUP原版']['PA']
    pa_opt = report['parsers']['PIPLUP优化版']['PA']
    assert pa_orig == pytest.approx(0.5)
    assert pa_opt == pytest.approx(0.5)


def test_neither_valid_synthesis(tmp_path):
    """neither + 合成模板通过校验 → GT 取合成模板，两版 PA 同为 0（都被否）。"""
    proposal = 'event <*> happened now'   # 覆盖全部样本，固定词=3
    client = ScriptedClient([_resp('neither', proposed=proposal)])
    gt_dir, final_gt, final_gt_alt, excluded = _make_gt_with_client(tmp_path, client)

    assert len(excluded) == 0
    assert len(final_gt) == 4
    for r in final_gt:
        assert r['adjudication'] == 'neither'
        assert r['template'] == proposal
    # 合成模板两变体同值
    assert [r['template'] for r in final_gt_alt] == [r['template'] for r in final_gt]
    # 两版 PA 均 0：合成模板不属于任何一版
    report = json.loads((gt_dir / "eval_report.json").read_text(encoding='utf-8'))
    assert report['parsers']['PIPLUP原版']['PA'] == pytest.approx(0.0)
    assert report['parsers']['PIPLUP优化版']['PA'] == pytest.approx(0.0)


def test_neither_invalid_proposal_excluded(tmp_path):
    """neither + 合成模板未通过覆盖/质量校验 → 排除行 + 覆盖率报告。"""
    # 覆盖失败：模板无法匹配样本（ORIG 前缀不属于原始日志文本）
    client = ScriptedClient([_resp('neither', proposed='WRONG <*> TEMPLATE')])
    gt_dir, final_gt, final_gt_alt, excluded = _make_gt_with_client(tmp_path, client)

    assert len(final_gt) == 0
    assert len(excluded) == 4
    assert all(e['reason'] == 'neither_invalid_proposal' for e in excluded)
    summary = json.loads((gt_dir / "gt_summary.json").read_text(encoding='utf-8'))
    assert summary['gt_coverage'] == 0.0
    assert summary['excluded_lines']['neither_invalid_proposal'] == 4

    # 纯通配提案（固定词<2）也应被拒
    client2 = ScriptedClient([_resp('neither', proposed='<*>')])
    _, fg2, _, ex2 = _make_gt_with_client(tmp_path / 'case2', client2)
    assert len(ex2) == 4 and len(fg2) == 0


def test_insufficient_and_pending_excluded(tmp_path):
    """insufficient_evidence / 未裁决 → 排除 + 覆盖率。"""
    client = ScriptedClient([_resp('insufficient_evidence')])
    gt_dir, final_gt, _, excluded = _make_gt_with_client(tmp_path, client)
    assert len(final_gt) == 0
    assert all(e['reason'] == 'insufficient_evidence' for e in excluded)

    # dry-run（无裁决结果）→ pending 排除
    client3 = ScriptedClient([])
    gt_dir3, final_gt3, _, excluded3 = _make_gt_with_client(
        tmp_path / 'case3', ScriptedClient([]))
    # dry_run=False 但脚本客户端返回空 → execute_task 抛错 → failed → excluded
    assert len(excluded3) == 4
    assert all(e['reason'] in ('pending', 'adjudication_failed') for e in excluded3)


def test_original_optimized_verdicts(tmp_path):
    """original/optimized 裁决 → GT 取对应版本，双变体同值。"""
    client = ScriptedClient([_resp('original')])
    _, fg, fga, ex = _make_gt_with_client(tmp_path, client)
    assert not ex and len(fg) == 4
    for r, ra in zip(fg, fga):
        assert r['template'].startswith('ORIG_PP(')
        assert ra['template'] == r['template']

    client2 = ScriptedClient([_resp('optimized')])
    _, fg2, fga2, _ = _make_gt_with_client(tmp_path / 'c2', client2)
    for r in fg2:
        assert r['template'].startswith('OPT_PP(')
    assert [r['template'] for r in fga2] == [r['template'] for r in fg2]


def test_audit_consistent_mock(tmp_path):
    """--audit-consistent 抽样审计：产出共识错误率工件。"""
    client = ScriptedClient([
        json.dumps({"Verdict": "incorrect", "Reason": "模板吞掉固定词", "Confidence": 0.9}),
    ])
    import importlib
    from eval_pipeline._shared.piplup_runner import _install_original_modules
    parser_mod = importlib.import_module(f"{FIXTURES}.PIPLUP_original")
    _install_original_modules(parser_mod,
                              f"{FIXTURES}.preprocessing_original",
                              f"{FIXTURES}.postprocessing_original")

    lines = [f'event {i} happened now' for i in range(4)]
    input_path = _write_input(tmp_path, lines)
    gt_dir = tmp_path / "gt_audit"

    # 审计路径单独验证（gt 主流程用一致场景需两版模板一致——fixtures 必不一致，
    # 故直接调用 _audit_consistent_lines 单元验证）
    from eval_pipeline.gt_generation import _audit_consistent_lines, _build_raw_map
    consistent = [{'log_id': str(i + 1), 'template': f'event <*> happened now',
                   'template_id': i, 'adjudication': 'consistent'} for i in range(4)]
    raw_map = _build_raw_map(input_path, None, None, 'Messages')
    result = _audit_consistent_lines(gt_dir, consistent, raw_map, client, k=10,
                                     dry_run=False)
    assert result['sampled'] >= 1
    assert result['error_rate'] == 1.0   # scripted 全部判 incorrect
    assert (gt_dir / "consistent_audit.json").exists()


def test_audit_synthesis_replaces_incorrect_consistent(tmp_path):
    """审计判 incorrect + 合成通过 → GT 模板替换为合成结果。"""
    import importlib
    from eval_pipeline._shared.piplup_runner import _install_original_modules
    parser_mod = importlib.import_module(f"{FIXTURES}.PIPLUP_original")
    _install_original_modules(parser_mod,
                              f"{FIXTURES}.preprocessing_original",
                              f"{FIXTURES}.postprocessing_original")

    lines = [f'event {i} happened now' for i in range(4)]
    input_path = _write_input(tmp_path, lines)
    gt_dir = tmp_path / "gt_audit2"

    # 流程：gt 先用 mock 裁决（无不一致场景则全 consistent——fixtures 两版
    # 模板必不同，故直接构造一致场景绕过对齐：手工构造 consistent_tasks 调组装）
    from eval_pipeline.gt_generation import (
        _build_final_gt, _audit_consistent_lines, _build_raw_map)
    raw_map = _build_raw_map(input_path, None, None, 'Messages')
    consistent = [{'log_id': str(i + 1), 'template': 'event <*> happened now',
                   'template_id': i, 'adjudication': 'consistent',
                   'TaskID': 'CS_TEST'} for i in range(4)]
    line_pairs = [{'log_id': str(i + 1), 'consistent': True,
                   'template_id_orig': i, 'template_id_opt': i,
                   'TaskID': 'CS_TEST'} for i in range(4)]
    results = [{'log_id': str(i + 1), 'template': 'event <*> happened now',
                'template_id': i} for i in range(4)]

    # 审计：判 incorrect + 合成通过
    client = ScriptedClient([json.dumps({
        "Verdict": "incorrect", "Reason": "吞掉数字", "Confidence": 0.9,
        "CorrectedTemplate": "event <*> happened now"})])
    audit = _audit_consistent_lines(gt_dir, consistent, raw_map, client, k=10,
                                    dry_run=False)
    assert len(audit['corrections']) == 1

    final_gt, final_gt_alt, excluded, human_applied = _build_final_gt(
        gt_dir, consistent, line_pairs, results, results,
        audit_corrections=audit['corrections'])
    assert len(excluded) == 0
    assert all(r['template'] == 'event <*> happened now'
               for r in final_gt)
    assert all(r['adjudication'] == 'audit_synthesized' for r in final_gt)


def test_audit_failed_correction_excluded(tmp_path):
    """审计判 incorrect + 合成未通过 → 一致行排除（audit_synthesis_failed）。"""
    import importlib
    from eval_pipeline._shared.piplup_runner import _install_original_modules
    parser_mod = importlib.import_module(f"{FIXTURES}.PIPLUP_original")
    _install_original_modules(parser_mod,
                              f"{FIXTURES}.preprocessing_original",
                              f"{FIXTURES}.postprocessing_original")

    lines = [f'event {i} happened now' for i in range(4)]
    input_path = _write_input(tmp_path, lines)
    gt_dir = tmp_path / "gt_audit3"

    from eval_pipeline.gt_generation import (
        _build_final_gt, _audit_consistent_lines, _build_raw_map)
    raw_map = _build_raw_map(input_path, None, None, 'Messages')
    consistent = [{'log_id': str(i + 1), 'template': 'event <*> happened now',
                   'template_id': i, 'adjudication': 'consistent',
                   'TaskID': 'CS_TEST'} for i in range(4)]
    line_pairs = [{'log_id': str(i + 1), 'consistent': True,
                   'template_id_orig': i, 'template_id_opt': i,
                   'TaskID': 'CS_TEST'} for i in range(4)]
    results = [{'log_id': str(i + 1), 'template': 'event <*> happened now',
                'template_id': i} for i in range(4)]

    # 合成模板不含必要固定词（纯通配）→ 校验失败
    client = ScriptedClient([json.dumps({
        "Verdict": "incorrect", "Reason": "r", "Confidence": 0.9,
        "CorrectedTemplate": "<*>"})])
    audit = _audit_consistent_lines(gt_dir, consistent, raw_map, client, k=10,
                                    dry_run=False)
    assert audit.get('failed_corrections', 0) >= 1

    failed_templates = {d['template'] for d in audit['details']
                        if d['verdict'] == 'incorrect' and not d.get('corrected')}
    final_gt, final_gt_alt, excluded, _ = _build_final_gt(
        gt_dir, consistent, line_pairs, results, results,
        audit_corrections=audit['corrections'],
        audit_failed_templates=failed_templates)
    assert len(final_gt) == 0
    assert all(e['reason'] == 'audit_synthesis_failed' for e in excluded)


def test_human_annotation_overrides_everything(tmp_path):
    """人工标注最高优先级：覆盖排除行、一致簇与 LLM 裁决。"""
    import importlib
    from eval_pipeline._shared.piplup_runner import _install_original_modules
    parser_mod = importlib.import_module(f"{FIXTURES}.PIPLUP_original")
    _install_original_modules(parser_mod,
                              f"{FIXTURES}.preprocessing_original",
                              f"{FIXTURES}.postprocessing_original")

    lines = [f'event {i} happened now' for i in range(4)]
    input_path = _write_input(tmp_path, lines)
    gt_dir = tmp_path / "gt_human"

    from eval_pipeline.gt_generation import (
        generate_gt, _build_final_gt, _build_raw_map)

    # 场景1：insufficient 排除行的 TaskID 被人工救回
    client = ScriptedClient([_resp('insufficient_evidence')])
    gt_dir, final_gt, final_gt_alt, excluded = _make_gt_with_client(
        gt_dir, client)
    task_ids = {e['TaskID'] for e in excluded}
    assert len(task_ids) == 4

    # 人工标注前 2 条 TaskID
    ann_path = gt_dir / 'human_annotations.jsonl'
    annotations = []
    for tid in list(task_ids)[:2]:
        annotations.append({'TaskID': tid, 'template': 'HUMAN correct template',
                            'annotator': 'tester'})
    with open(ann_path, 'w', encoding='utf-8') as f:
        for a in annotations:
            f.write(json.dumps(a, ensure_ascii=False) + '\n')

    # 重新组装（带人工标注）
    raw_map = _build_raw_map(input_path, None, None, 'Messages')
    from eval_pipeline.gt_generation import _compare_clusters
    results = [{'log_id': str(i + 1), 'template': 'ORIG_PP(x)|ORIG_PPT(x)',
                'template_id': i} for i in range(4)]
    gt_tasks, adj_tasks, line_pairs, _ = _compare_clusters(results, results, raw_map)
    final_gt2, final_gt_alt2, excluded2, human_applied = _build_final_gt(
        gt_dir, gt_tasks, line_pairs, results, results,
        human_map={a['TaskID']: a['template'] for a in annotations})
    # consistent 场景下全部一致行进 GT；人工标注针对 pair TaskID（GT_xxx）
    # 在一致场景不存在 → human_applied=0 是预期；改用排除场景验证
    assert human_applied == 0 and len(excluded2) == 0

    # 真实验证：对排除场景重新组装并回填人工标注
    final_gt3, final_gt_alt3, excluded3, applied3 = _build_final_gt(
        gt_dir, [], [lp for lp in line_pairs], results, results,
        human_map={a['TaskID']: a['template'] for a in annotations})
    # 上面 line_pairs 不含 GT_xxx，无法命中——用排除场景的原始数据重跑
    # （_make_gt_with_client 内部已完整跑过，此处直接验证组装函数的优先级）
    from eval_pipeline.gt_generation import _load_adjudication_results
    adj_map, task_map, failed_ids = _load_adjudication_results(gt_dir)
    line_pairs_real = [{'log_id': str(i + 1), 'consistent': False,
                        'template_id_orig': i, 'template_id_opt': i,
                        'TaskID': tid} for i, tid in enumerate(sorted(task_ids))]
    final_gt4, final_gt_alt4, excluded4, applied4 = _build_final_gt(
        gt_dir, [], line_pairs_real, results, results,
        human_map={a['TaskID']: a['template'] for a in annotations})
    assert applied4 == 2
    human_rows = [r for r in final_gt4 if r['adjudication'] == 'human']
    assert len(human_rows) == 2
    assert all(r['template'] == 'HUMAN correct template' for r in human_rows)
    # 剩余 2 条仍排除
    assert len(excluded4) == 2
    # 双变体同值
    assert [r['template'] for r in final_gt_alt4] == [r['template'] for r in final_gt4]


def test_human_review_tasks_exported(tmp_path):
    """human_review_tasks.jsonl 自动导出：排除行聚合 + 样本 + 候选。"""
    client = ScriptedClient([_resp('insufficient_evidence')])
    gt_dir, final_gt, final_gt_alt, excluded = _make_gt_with_client(
        tmp_path, client)
    review = [json.loads(l) for l in
              (gt_dir / "human_review_tasks.jsonl").read_text(encoding='utf-8').splitlines() if l.strip()]
    assert len(review) >= 1
    for rt in review:
        assert rt['TaskID'] and rt['sample_log'] and 'candidates' in rt
        assert set(rt['candidates'].keys()) >= {'original', 'optimized'}

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

    # 6 行全部进入 GT（一致 or 裁决后回填）
    final_gt = [json.loads(l) for l in
                (gt_dir / "final_gt.jsonl").read_text(encoding='utf-8').splitlines() if l.strip()]
    assert len(final_gt) == 6
    assert {r['log_id'] for r in final_gt} == {str(i + 1) for i in range(6)}
    # 每行裁决状态合法
    for r in final_gt:
        assert r['adjudication'] in ('consistent', 'original', 'optimized',
                                     'equivalent', 'neither',
                                     'insufficient_evidence', 'pending')
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
    assert report['evaluated_lines'] == 6
    # 期望 PA 从 GT 内容推导：mock 裁决可能选 optimized，GT 模板随裁决走
    from eval_pipeline._shared.metrics import normalize_template
    final_gt = [json.loads(l) for l in
                (gt_dir / "final_gt.jsonl").read_text(encoding='utf-8').splitlines() if l.strip()]
    gt_rows = {r['log_id']: r for r in final_gt}
    orig_out = {}
    for _line in (gt_dir / "runs" / "piplup_orig_result.jsonl").read_text(encoding='utf-8').splitlines():
        if _line.strip():
            _rec = json.loads(_line)
            orig_out[_rec['log_id']] = _rec
    expected_pa = sum(
        1 for lid, gr in gt_rows.items()
        if normalize_template(gr['template']) == normalize_template(orig_out[lid]['template'])
    ) / len(gt_rows)
    assert report['piplup_orig']['PA'] == pytest.approx(expected_pa)

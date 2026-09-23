"""生产基线上的最小结构合并回归；不依赖上一轮重构接口。"""
import itertools
import random
import re

import pytest

from src.data_process.template_extractor.parsing_algorithm.PIPLUP import (
    Logcluster, LogParser, _light_digit_re, _structure_aware_merge, path_delimiters,
)
from src.data_process.template_extractor.parsing_algorithm.postprocessing import correct_single_template
from src.data_process.template_extractor.parsing_algorithm.preprocessing import preprocess, sequence


def covers(template, text):
    return re.fullmatch('.*?'.join(re.escape(p) for p in template.split('<*>')), text, re.S) is not None


@pytest.mark.parametrize('a,b,expected', [
    ('name(DMI_SysPowerCheckThread)', 'name(DMI_CpuFreqOptThread)', 'name(<*>)'),
    ('(xnet_r0_0_14)', '(xnet_r0_0_50)', '(xnet_<*>)'),
    ('xnet_r0_0_14', 'xnet_r0_0_50', 'xnet_<*>'),
    ('GlobalInfo;)', 'FrameInfo;)', '<*>;)'),
    ('0,', '5,', '<*>,'),
    ('<*>', '0,', '<*>'),
    ('key:<*>', 'key:0,', 'key:<*>'),
    ('key:a(x)', 'key:b(y)', 'key:<*>)'),
    ('[a(x)]', '[b(y)]', '[<*>)]'),
    ('(a)(num:1)', '(b)(num:1)', '(<*>)(num:1)'),
    ('a:fooX', 'b:fooY', '<*>:foo<*>'),
])
def test_merge_examples(a, b, expected):
    for left, right in ((a, b), (b, a)):
        merged = _structure_aware_merge(left, right, path_delimiters)
        assert merged == expected
        assert covers(merged, left) and covers(merged, right)


def test_progressive_merge_preserves_history():
    rng = random.Random(42)
    atoms = ['abc', 'def', '14', '50', '<*>', '(', ')', '[', ']', '{', '}', ':', '_', ',', ';']
    for _ in range(500):
        history = [''.join(rng.choices(atoms, k=rng.randrange(1, 9))) for _ in range(5)]
        merged = history[0]
        for token in history[1:]:
            merged = _structure_aware_merge(merged, token, path_delimiters)
        for token in history:
            assert covers(merged, token), (merged, token, history)
            assert covers(merged, token.replace('<*>', 'payload(42)'))


@pytest.mark.parametrize('text', ['name(<*>)', '(xnet_<*>)', '[<*>)]', 'key:<*>)', '(<*>)(num:<*>)', 'value <*>, next', '<*>=<*>'])
def test_postprocess_keeps_structure(text):
    assert correct_single_template(text) == text


def test_multi_path_merge_covers_all_paths():
    rows = [['event', 'name(a)'], ['event', 'other(b)'], ['event', 'name(c)']]
    for order in itertools.permutations(rows):
        cluster = Logcluster(list(order[0]), br_thresh=2)
        for row in order[1:]:
            cluster.updateSequence(list(row))
        for row in rows:
            assert any(covers(t, ' '.join(row)) for t in cluster.templateList)


@pytest.mark.parametrize('estimation', [False, True])
def test_redundant_masks_equivalent(estimation):
    original = list(sequence)
    original.insert(2, 'epoch_ms_13d')
    original.insert(4, 'code_line_num')
    for n in (0, 1, 178, 1500001430000, 9999999999999):
        for text in (f'[{n}] ready', f'[module.c:action,{n}] done', f'[{n}] [app,pid:12,tid:45] count:{n}', f'value,{n}]suffix'):
            old = preprocess(text, estimation_stage=estimation, use_sequence=original)
            new = preprocess(text, estimation_stage=estimation)
            assert (old[0] if estimation else old) == (new[0] if estimation else new)


def test_light_normalization():
    assert _light_digit_re.sub('0', 'Arg:0_1_5') == 'Arg:0'
    for text in ('key:value', 'a_b', '()<*>', ',;_+-/'):
        assert _light_digit_re.sub('0', text) == text


@pytest.mark.parametrize('cache', [False, True])
def test_parser_smoke(cache):
    parser = LogParser('<Content>', 'minimal-test', use_cache=False, use_template_cache=cache, br_thresh=2)
    logs = ['worker name(DMI_SysPowerCheckThread) ready', 'worker name(DMI_CpuFreqOptThread) ready', 'worker name(DMI_OtherThread) ready']
    for _ in range(2):
        templates = parser.parse_list(logs)
        assert len(templates) == len(logs)
        assert all(covers(t, log) for t, log in zip(templates, logs))

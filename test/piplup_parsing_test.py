#!/usr/bin/python3.7
# -*- coding: utf-8 -*-
"""PIPLUP 解析算法测试套件（自包含，无外部数据集依赖）。

组织结构：
  第一部分  多路径与合并正确性（簇级概括 / 具体路径 / 输出候选模板）
  第二部分  token 结构合并与渐进泛化（_structure_aware_merge / _progressive_generalize）
  第三部分  解析缓存（CRC32 两级索引、计数、批次一致性）
  第四部分  保护性回归（当前正确、修复过程中必须保持的行为）

标记约定：
  @pytest.mark.xfail(reason="bug-<编号>: ...") 的用例在基线（0e1385b）上失败，
  用于复现已知缺陷；对应修复提交应移除 xfail 标记并使其通过。

运行方式（仓库根目录）：
  python -m pytest test/piplup_parsing_test.py -v
"""
import zlib

import pytest
import regex as re

from src.data_process.template_extractor.parsing_algorithm.PIPLUP import (
    LogParser,
    Logcluster,
    _structure_aware_merge,
    _lcp_lcs_merge,
    _progressive_generalize,
    path_delimiters,
)
from src.data_process.template_extractor.parsing_algorithm import preprocessing
from src.data_process.template_extractor.parsing_algorithm import token_structure as ts
from src.data_process.template_extractor.parsing_algorithm.postprocessing import (
    correct_single_template,
)

# CRC32 真实碰撞样例（两侧均为 816545939）
COLLISION_A = 'event rslozjkqnovlzvsh'
COLLISION_B = 'event mloqmynkuxosmwhv'


def make_parser(**kwargs):
    """构造不落盘、默认关闭缓存的解析器，参数可覆盖。"""
    kwargs.setdefault('log_format', '<Content>')
    kwargs.setdefault('module_name', 'test')
    kwargs.setdefault('indir', '.')
    kwargs.setdefault('outdir', '/tmp/')
    kwargs.setdefault('use_cache', False)
    kwargs.setdefault('use_template_cache', False)
    return LogParser(**kwargs)


def template_covers(template, text):
    """模板（含 <*> 通配）是否能完整覆盖 text。"""
    parts = re.split(r'<\*>', template)
    pattern = '.*?'.join(re.escape(p) for p in parts)
    return bool(re.fullmatch(pattern, text, re.DOTALL))


# =====================================================================
# 第一部分：多路径与合并正确性
# =====================================================================

class TestMultiPath:
    """簇级概括（logTemplate）、具体路径（pathList）与输出候选（templateList）
    各自职责：logTemplate 供 treeSearch/fastMatch 评分，可包含通配；
    pathList 在容量（br_thresh）未满时保留具体路径差异；
    templateList 是输出候选，未满容量时不应被簇级通配符抹平。"""

    def test_below_capacity_keeps_path_candidates(self):
        """br_thresh=5、仅 2 条路径时必须保留两个具体候选模板。"""
        parser = make_parser(br_thresh=5)
        logs = [
            'event server east stable one two three',
            'event client west stable one two three',
        ]
        result = parser.parse_list(logs)
        # 未达到容量阈值：两条路径都应保留为具体模板
        assert set(result) == {
            'event server east stable one two three',
            'event client west stable one two three',
        }
        # 输出模板必须各自覆盖其来源行
        for line, tpl in zip(logs, result):
            assert template_covers(tpl, line)

    def test_capacity_merge_covers_all_paths(self):
        """容量满后合并，合并模板必须覆盖所有参与合并的实际路径。"""
        parser = make_parser(br_thresh=2)
        logs = [
            'event server east stable one two three',
            'event client west stable one two three',
            'event north south stable one two three',
        ]
        result = parser.parse_list(logs)
        templates = set(result)
        # 合并后允许单一泛化模板，但必须覆盖全部三行
        assert len(templates) == 1
        for line in logs:
            assert template_covers(next(iter(templates)), line)

    def test_capacity_merge_not_exclude_bystander_paths(self):
        """容量满时合并 target 与新路径，同簇其他路径的取值不得被排除。

        场景：路径容量 3，前两条路径在 pos1 分叉（xnet_a / xnet_b），
        第四条日志触发与第一条路径合并；合并结果必须覆盖第二条路径
        未参与合并的取值。
        """
        parser = make_parser(br_thresh=3)
        logs = [
            'node xnet_a load',
            'node xnet_b load',
            'node xnet_c load',
            'node yext_z load',
        ]
        result = parser.parse_list(logs)
        # 最终模板必须覆盖全部四行（含未参与合并的旁观路径）
        for line, tpl in zip(logs, result):
            assert template_covers(tpl, line), (line, tpl)

    def test_merge_result_covers_both_inputs(self):
        """_structure_aware_merge(t1, t2) 的结果必须同时覆盖 t1 和 t2。"""
        pairs = [
            ('<*>', '0,'),
            ('0,', '<*>'),
            ('<*>', 'abc'),
            ('a:<*>', 'a:12,'),
            ('name(DMI_SysPowerCheckThread)', 'name(DMI_CpuFreqOptThread)'),
        ]
        for t1, t2 in pairs:
            merged = _structure_aware_merge(t1, t2, path_delimiters)
            assert template_covers(merged, t1), (t1, t2, merged)
            assert template_covers(merged, t2), (t1, t2, merged)

    def test_wildcard_merge_keeps_original_coverage(self):
        """通配侧覆盖具体侧时，结果不得窄于通配侧原有的覆盖范围。"""
        merged = _structure_aware_merge('<*>', '0,', path_delimiters)
        # 原 <*> 可接受 '7'，合并结果也必须接受
        assert template_covers(merged, '7')
        assert template_covers(merged, '0,')

    def test_shared_separator_still_fixed(self):
        """双方共有的分隔符仍应被固定（由自然 LCP/LCS 路径完成）。"""
        merged = _structure_aware_merge('0,', '5,', path_delimiters)
        assert merged == '<*>,'
        assert template_covers(merged, '0,')
        assert template_covers(merged, '5,')


# =====================================================================
# 第二部分：token 结构合并与渐进泛化
# =====================================================================

class TestStructureAwareMerge:
    def test_identical(self):
        assert _structure_aware_merge('abc', 'abc', path_delimiters) == 'abc'

    def test_plain_words_generalize(self):
        merged = _structure_aware_merge('server', 'client', path_delimiters)
        assert merged == '<*>'
        assert template_covers(merged, 'server')
        assert template_covers(merged, 'client')

    def test_prefix_preserved(self):
        merged = _structure_aware_merge('(xnet_r0_0_14:0)', '(xnet_r0_0_50:0)',
                                        path_delimiters)
        # 裸标识符合并保留完整公共前缀（数字组边界修剪掉部分数字组 '1'）
        assert merged == '(xnet_r0_0_<*>:0)'
        assert template_covers(merged, '(xnet_r0_0_14:0)')
        assert template_covers(merged, '(xnet_r0_0_50:0)')

    def test_generalized_form_recovers_prefix_on_new_variance(self):
        """已泛化形式遇到新的中段变化时，经 LCP/LCS 回退保留中缀结构，
        而不是塌缩为 <*>（渐进泛化平凡解的回退路径）。"""
        merged = _structure_aware_merge('(xnet_r0_0_<*>:0)', '(xnet_r1_2_33:0)',
                                        path_delimiters)
        assert merged == '(xnet_<*>:0)'
        assert template_covers(merged, '(xnet_r0_0_<*>:0)')
        assert template_covers(merged, '(xnet_r1_2_33:0)')

    def test_tail_preserved(self):
        merged = _structure_aware_merge('GlobalInfo;)', 'FrameInfo;)',
                                        path_delimiters)
        assert template_covers(merged, 'GlobalInfo;)')
        assert template_covers(merged, 'FrameInfo;)')

    def test_overlapping_protection(self):
        """head+tail 覆盖整个 token 时退化为 <*>（仅剩结构括号）。"""
        merged = _structure_aware_merge('(a)', '(b)', path_delimiters)
        assert merged == '(<*>)'
        assert template_covers(merged, '(a)')
        assert template_covers(merged, '(b)')


class TestProgressiveGeneralize:
    TOKEN = '(xnet_<*>:<*>)(num:<*>)server'

    def test_generates_multiple_levels(self):
        """应沿逻辑边界生成多于一个、逐步泛化的候选。"""
        candidates = list(_progressive_generalize(self.TOKEN, path_delimiters))
        assert len(candidates) >= 2
        # 去重
        assert len(set(candidates)) == len(candidates)
        # 每个候选都必须覆盖原 token（安全下界：不排除既有路径）
        for cand in candidates:
            assert template_covers(cand, self.TOKEN), cand

    def test_candidates_reach_tail_group(self):
        """候选序列应能推进到覆盖尾部结构组，例如形如 (xnet_<*>:<*>)<*> 的形式。"""
        candidates = list(_progressive_generalize(self.TOKEN, path_delimiters))
        # 至少应出现：只替换尾部常量、以及吸收尾部常量所在完整片段的两级
        flat = set(candidates)
        assert '(xnet_<*>:<*>)(num:<*>)<*>' in flat

    def test_no_bracket_truncation(self):
        """候选的固定部分括号必须平衡，不允许截断括号。"""
        candidates = list(_progressive_generalize(self.TOKEN, path_delimiters))
        assert candidates, '至少应有一个候选'
        for cand in candidates:
            fixed = cand.replace('<*>', '')
            depth = 0
            for ch in fixed:
                if ch in '([{':
                    depth += 1
                elif ch in ')]}':
                    depth -= 1
                    assert depth >= 0, f'候选 {cand} 固定部分括号失衡'
            assert depth == 0, f'候选 {cand} 固定部分括号失衡'

    def test_bounded_candidates(self):
        """候选数量必须有界，多通配 token 不应爆炸或死循环。"""
        token = 'a<*>b<*>c<*>d<*>e<*>ftail'
        candidates = list(_progressive_generalize(token, path_delimiters))
        assert len(candidates) <= 8

    def test_no_wildcard_no_candidate(self):
        assert list(_progressive_generalize('plain', path_delimiters)) == []

    def test_progressive_strictly_general(self):
        """每个候选必须覆盖前一个候选（逐步泛化、有进展）。"""
        candidates = list(_progressive_generalize('key_<*>:val_<*>:tail1',
                                                  path_delimiters))
        for prev, nxt in zip(candidates, candidates[1:]):
            assert template_covers(nxt, prev), (prev, nxt)


# =====================================================================
# 第三部分：解析缓存
# =====================================================================

class TestParsingCache:
    def test_crc32_collision_not_confused(self):
        assert zlib.crc32(COLLISION_A.encode()) == zlib.crc32(COLLISION_B.encode())
        parser = make_parser(sim_thresh='1', use_template_cache=True)
        result = parser.parse_list([COLLISION_A, COLLISION_B])
        # 两行内容不同、无泛化余地（sim=1），必须各得各的模板
        assert result[0] == COLLISION_A
        assert result[1] == COLLISION_B

    def test_cache_hit_updates_hit_time(self):
        """update_population=3，连续 5 次 alpha omega：缓存开/关行为必须一致。"""
        parser_off = make_parser(update_population=3)
        parser_on = make_parser(update_population=3, use_template_cache=True)
        logs = ['alpha omega'] * 5

        parser_off.parse_list(logs)
        parser_on.parse_list(logs)

        assert parser_on.logCluL[0].hit_time == parser_off.logCluL[0].hit_time == 5
        # 阈值维护：达到 update_population 后簇应从 'None' 重索引到 'alpha'
        for parser in (parser_off, parser_on):
            cluster = parser.logCluL[0]
            buckets = parser.storage_tree['None'].get(2, [])
            assert cluster not in buckets
            assert cluster in parser.storage_tree['alpha'].get(2, [])

    def test_no_stale_template_within_batch(self):
        """同批次缓存命中的行不得返回过期模板（缓存开/关逐行一致）。"""
        # 场景 1：路径容量未满，重复行走 Level 1 缓存
        logs1 = [
            'event server east stable one two three',
            'event server east stable one two three',
            'event client west stable one two three',
        ]
        assert make_parser(use_template_cache=True).parse_list(logs1) == \
            make_parser().parse_list(logs1)

    def test_no_stale_template_after_capacity_merge(self):
        """场景 2：同批次内容量满合并更新候选，缓存行不得持有旧模板。"""
        logs = [
            'event server east stable one two three',
            'event server east stable one two three',
            'event client west stable one two three',
        ]
        expected = make_parser(br_thresh=1).parse_list(logs)
        result = make_parser(br_thresh=1, use_template_cache=True).parse_list(logs)
        assert result == expected

    def test_consecutive_batches_consistent(self):
        """连续 parse_list 调用：行号游标不残留、模板状态正确延续。"""
        parser = make_parser()
        first = parser.parse_list(['alpha omega', 'beta gamma'])
        assert first == ['alpha omega', 'beta gamma']
        # 第二批：行号重新从 1 开始（logIDL 清空）
        second = parser.parse_list(['alpha omega', 'delta epsilon'])
        assert second[0] == 'alpha omega'
        assert second[1] == 'delta epsilon'
        for cluster in parser.logCluL:
            assert cluster.logIDL == []

    def test_persistence_roundtrip(self):
        """持久化加载后：簇状态保留、行号清空、br_thresh 以当前配置为准。"""
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = os.path.join(tmp, 'state.pkl')
            saver = make_parser(use_cache=True, cache_path=cache_path, br_thresh=5)
            saver.parse_list(['alpha omega x1', 'alpha omega x2'])
            saver.parse_list(['beta gamma y1'])
            saver.save_cache()

            loader = LogParser(log_format='<Content>', module_name='test',
                               indir='.', outdir='/tmp/', use_cache=True,
                               cache_path=cache_path, br_thresh=3)
            # 加载的簇 br_thresh 更新为当前配置
            assert all(c.br_thresh == 3 for c in loader.logCluL)
            assert all(c.logIDL == [] for c in loader.logCluL)
            result = loader.parse_list(['alpha omega x1', 'beta gamma y1'])
            # 重新解析已见过的行应得到一致的模板：
            # 容量未满时多路径保留具体候选（x1/x2 两条路径都在）
            assert result[0] == 'alpha omega x1'
            assert result[1] == 'beta gamma y1'

    def test_malformed_cache_does_not_leave_partial_state(self):
        """缺字段的缓存加载失败后，解析器必须保持完整的空状态。"""
        import os
        import pickle
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = os.path.join(tmp, 'broken.pkl')
            with open(cache_path, 'wb') as f:
                pickle.dump({'storage_tree': {'broken': {}}}, f)

            parser = LogParser(log_format='<Content>', module_name='test',
                               indir='.', outdir='/tmp/', use_cache=True,
                               cache_path=cache_path)

            assert parser.storage_tree == {'None': {}}
            assert parser.shallow_tree == {'None': []}
            assert parser.logCluL == []
            assert parser.parse_list(['alpha beta']) == ['alpha beta']

    def test_light_cache_verifies_full_match(self):
        """Level 1 命中必须做完整正则校验，不能只做子串存在性检查。"""
        parser = make_parser(use_template_cache=True)
        # 白盒构造：手动把一个“片段都在、但整体不匹配”的状态注入 light_cache。
        # 模板 'ab <*> cd'（片段 'ab ' 和 ' cd'）对行 'xx ab 3 cd'：
        # 两个片段都存在，但 ^ab .*? cd$ 无法完整匹配该行。
        line = 'xx ab 3 cd'
        light = re.sub(r'(?=[\d\s.,;\-/+_]*\d)[\d\s.,;\-/+_]+', '0', line)
        light_crc = zlib.crc32(light.encode('utf-8')) & 0xffffffff
        victim = Logcluster(logTemplate=['ab', '3', 'cd'], br_thresh=5)
        victim.templateList = ['ab <*> cd']
        parser.light_cache[light_crc] = [victim]

        result = parser.parse_list([line])
        # 不允许误命中注入的模板；正确结果来自完整解析（数值被遮蔽）
        assert result[0] != 'ab <*> cd'
        assert result[0] == 'xx ab <*> cd'


# =====================================================================
# 第五部分：token 内结构评分与合并
# =====================================================================

# 第五部分评估的掩码候选集（与主规则和兜底清理同步关闭，
# 验证结构机制承接后，第五提交再实际删除）
STRUCTURE_CANDIDATE_MASKS = {
    'underscore_composite_num', 'kv_compound_id', 'compound_num_id',
    'compound_hex_id', 'bracket_compound_id', 'underscore_num_suffix',
    'xnet_ctx_id', 'embedded_hex',
}


class MaskDisable:
    """上下文管理器：实验性关闭指定预处理规则（主规则 + 兜底清理）。"""

    def __init__(self, rules):
        self.rules = rules
        self._backup = None

    def __enter__(self):
        self._backup = preprocessing.sequence
        preprocessing.sequence = [r for r in self._backup if r not in self.rules]

    def __exit__(self, *exc):
        preprocessing.sequence = self._backup


class TestTokenStructure:
    def test_split_kv_guards(self):
        """冒号歧义守卫：IPv6/时间/URL 不拆分为 kv。"""
        assert ts.split_kv('fe80::1') is None
        assert ts.split_kv('2001:db8::1') is None
        assert ts.split_kv('12:34:56') is None
        assert ts.split_kv('http://example.com/a') is None
        assert ts.split_kv('<*>') is None
        # 正常 kv
        assert ts.split_kv('name:abc123') == ('', 'name', ':', 'abc123')
        assert ts.split_kv('id=42') == ('', 'id', '=', '42')
        assert ts.split_kv('(num:5)') == ('(', 'num', ':', '5)')

    def test_value_similarity(self):
        assert ts.value_similarity('x', 'x') == 1.0
        # 复合标识符/数字链：高
        assert ts.value_similarity('fsi_snap_fs5041_1102702', 'fsi_snap_fs6072_2203104') == 0.8
        assert ts.value_similarity('5041', '6072') == 0.8
        # 普通词（状态值）：0 —— 不能仅因结构一致归并
        assert ts.value_similarity('running', 'stopped') == 0.0

    def test_merge_values_number_group_trim(self):
        """数字组边界修剪：不保留巧合的部分数字前缀。"""
        assert ts.merge_values('xnet_r0_0_14', 'xnet_r0_0_50') == 'xnet_r0_0_<*>'
        assert ts.merge_values('fsi_snap_fs5041_1102702', 'fsi_snap_fs6072_2203104') == 'fsi_snap_fs<*>'
        # 完整单词段结尾的公共前缀保留
        assert ts.merge_values('vol/plane/5041', 'vol/plane/6072') == 'vol/plane/<*>'
        # 无共同点 → <*>
        assert ts.merge_values('running', 'stopped') == '<*>'

    def test_merge_kv_keeps_field_and_value_structure(self):
        merged = _structure_aware_merge('name:fsi_snap_fs5041_1102702',
                                        'name:fsi_snap_fs6072_2203104', path_delimiters)
        assert merged == 'name:fsi_snap_fs<*>'
        # 相邻结构与关闭括号不被吞掉
        assert _structure_aware_merge('(num:5)', '(num:7)', path_delimiters) == '(num:<*>)'
        assert _structure_aware_merge('name:val_5041:tail', 'name:val_6072:tail',
                                      path_delimiters) == 'name:val_<*>:tail'

    def test_merge_kv_different_keys_not_collapsed(self):
        """字段名不同：kv 合并不适用，走自然路径，不因结构相同加分。"""
        merged = _structure_aware_merge('src:abc123', 'dst:def456', path_delimiters)
        # 无共同前后缀 → 整体泛化（logTemplate 层面），输出侧由多候选保留
        assert merged == '<*>'

    def test_seqdist_structure_credits(self):
        parser = make_parser()
        # 同字段名 + 变量型值：(1 + 0.9 + 1)/3
        sim, _ = parser.seqDist(['snapshot', 'name:fsi_snap_fs5041_1102702', 'created'],
                                ['snapshot', 'name:fsi_snap_fs6072_2203104', 'created'])
        assert sim == pytest.approx((1 + 0.9 + 1) / 3)
        # 同字段名 + 普通词值（状态）：仅字段名证据 0.5
        sim, _ = parser.seqDist(['job', 'status:running', 'done'],
                                ['job', 'status:stopped', 'done'])
        assert sim == pytest.approx((1 + 0.5 + 1) / 3)
        # 不同字段名：结构不给分
        sim, _ = parser.seqDist(['read', 'src:abc', 'done'],
                                ['read', 'dst:def', 'done'])
        assert sim == pytest.approx(2 / 3)
        # 已泛化混合形式覆盖新值 → 0.9
        sim, _ = parser.seqDist(['a', 'name:x_<*>', 'b'], ['a', 'name:x_99', 'b'])
        assert sim == pytest.approx((1 + 0.9 + 1) / 3)
        # 纯 <*> 不计分且从分母剔除（无证据位不稀释相似度），
        # numOfPar 仍返回供 fastMatch 决胜
        sim, par = parser.seqDist(['a', '<*>', 'b'], ['a', 'zzz', 'b'])
        assert sim == pytest.approx(1.0) and par == 1
        # 分母全为纯 <*>：结构完全相同 → 1.0
        sim, par = parser.seqDist(['<*>', '<*>'], ['x', 'y'])
        assert sim == 1.0 and par == 2

    def test_kv_composite_merge_without_mask(self):
        """关闭 kv/复合标识符掩码后，复合值仍能解析归并（结构机制承接）。"""
        with MaskDisable(STRUCTURE_CANDIDATE_MASKS):
            logs = [f'snapshot name:fsi_snap_fs{1000 + i}_{2000000 + i * 7} created'
                    for i in range(6)]
            parser = make_parser(br_thresh=5)
            result = parser.parse_list(logs)
        # 6 个变体超过路径容量 → 合并为单一模板，字段名与值公共前缀完整保留
        assert len(set(result)) == 1
        template = result[0]
        assert template.startswith('snapshot name:fsi_snap_fs<*>')
        assert template.endswith('created')
        for line, tpl in zip(logs, result):
            assert template_covers(tpl, line)

    def test_unseen_composite_format_without_mask(self):
        """未见过的复合值格式（字母紧贴数字链），关闭掩码后仍可解析。"""
        with MaskDisable(STRUCTURE_CANDIDATE_MASKS):
            logs = [f'route svc:planeB{4001 + i} up' for i in range(6)]
            parser = make_parser(br_thresh=5)
            result = parser.parse_list(logs)
        assert len(set(result)) == 1
        # 数字组整体作为变量：公共字母前缀保留
        assert result[0].startswith('route svc:planeB')
        for line, tpl in zip(logs, result):
            assert template_covers(tpl, line)

    def test_bracket_composite_merge_without_mask(self):
        """关闭 compound_hex_id 等掩码后，括号复合 hex 值仍归并为单簇。"""
        with MaskDisable(STRUCTURE_CANDIDATE_MASKS):
            logs = [f'ZoneName(MTOBJ_DP0x{i}_RV0x{20 + i}_ID0x0) set' for i in range(6)]
            parser = make_parser(br_thresh=5)
            result = parser.parse_list(logs)
        assert len(parser.logCluL) == 1
        assert len(set(result)) == 1
        # 公共后缀 _ID0x0) 与前缀 MTOBJ_ 保留
        assert 'MTOBJ_' in result[0] and 'ID0x0' in result[0]
        for line, tpl in zip(logs, result):
            assert template_covers(tpl, line)

    def test_states_not_merged_by_structure_alone(self):
        """同字段名但普通词状态值 + 动作词不同：不得仅因结构归并。"""
        logs = ['task state:running finished ok', 'task state:stopped failed hard']
        parser = make_parser()
        result = parser.parse_list(logs)
        assert result[0] != result[1]
        assert len(parser.logCluL) == 2

    def test_same_key_states_keep_candidates(self):
        """同字段名的状态值：簇可能因其他 token 证据归并，
        但输出必须以多候选保留具体状态（不因结构塌缩）。"""
        logs = ['job status:running done', 'job status:stopped done']
        parser = make_parser()
        result = parser.parse_list(logs)
        assert 'running' in result[0]
        assert 'stopped' in result[1]

    def test_ipv6_value_under_key(self):
        """key 下的 IPv6 值：冒号不误判为嵌套 kv，值整体泛化。"""
        parser = make_parser()
        result = parser.parse_list(['link addr:fe80::1 up', 'link addr:fe80::2 up'])
        assert len(set(result)) == 1
        assert template_covers(result[0], 'link addr:fe80::99 up')

    def test_skeleton_similarity_bounded(self):
        """骨架一致本身给分有限（结构符号不产生高分）。"""
        assert ts.structure_similarity('(a1)', '(b2)') <= 0.5
        assert ts.structure_similarity('name:running', 'name:stopped') == pytest.approx(0.5)
        # 不同骨架 → 0
        assert ts.structure_similarity('(a1)', '[a1]') == 0.0

    def test_skeleton_preserves_alpha_prefix(self):
        """数字段骨架保留字母前缀作为固定证据（eth0/eth1 同族标识符）。

        原实现 [A-Za-z0-9_]*\\d[\\w.\\-]* 把整个标识符吞成 \\x00，
        同族标识符因 fixed=0 全部 0 分，真实数据中数千对同源簇无法合并。"""
        # 正例：仅数字段不同的同族标识符获得非零相似度
        assert ts.structure_similarity('eth0', 'eth1') > 0
        assert ts.structure_similarity('vcontroller_0', 'vcontroller_1') > 0
        assert ts.structure_similarity('GROUP_0', 'GROUP_1') > 0
        assert ts._skeleton('eth0') == 'eth\x00'
        # 反例：字母主体不同 → 骨架不同 → 0（不误合并）
        assert ts.structure_similarity('eth0', 'wlan0') == 0.0
        assert ts.structure_similarity(
            'Nfs4CreateUpdateIdNamehash', 'Nfs4CreateUpdateUidNamehash') == 0.0
        # 纯词（无数字）不受影响
        assert ts.structure_similarity('active', 'disable') == 0.0

    def test_kv_key_digit_suffix_same_field(self):
        """kv 字段名仅数字段不同（TS1/TS2）视为同字段名。"""
        # 正例：编号后缀是变量位，字母主体是固定证据
        assert ts.structure_similarity('TS1:operation', 'TS2:operation') == pytest.approx(1.0)
        # 反例：字母主体不同 → 0
        assert ts.structure_similarity('name:alice', 'age:alice') == 0.0

    def test_lcp_lcs_merge_drops_delimiter_free_tail(self):
        """LCS 区域无分隔符时丢弃纯字母尾巴，消除 <*>e 等合并伪影。"""
        D = path_delimiters
        # 纯单词合并不再残留无意义后缀
        assert _structure_aware_merge('active', 'disable', D) != '<*>e'
        for a, b in [('active', 'disable'), ('reader', 'writer'),
                     ('create', 'update'), ('enable', 'disable')]:
            merged = _lcp_lcs_merge(a, b, D)
            assert merged == '<*>', (a, b, merged)
        # 有分隔符的结构性尾巴仍保留
        assert _lcp_lcs_merge('GlobalInfo;)', 'FrameInfo;)', D) == '<*>;)'


# =====================================================================
# 第六部分：掩码删减台账（原规则 → 承接机制 → 正例 → 防误合并反例）
# =====================================================================

class TestMaskDeletionLedger:
    """每条已删除规则对应一个用例：正例证明结构机制承接（关闭/删除掩码
    后仍归并为正确模板），反例证明不会仅因结构一致而误合并。"""

    def test_underscore_composite_num_deleted(self):
        """原规则 (?<=[A-Za-z]:)\\d+(?:_\\d+)+（Cap:NUM_NUM）
        → 承接：kv 同字段名合并 + numerical 对边界数字段的遮蔽
        正例：pool Cap:111_222 used ×6 → pool Cap:<*> used"""
        logs = [f'pool Cap:{111 + i}_{222 + i * 3} used' for i in range(6)]
        parser = make_parser(br_thresh=5)
        result = parser.parse_list(logs)
        assert set(result) == {'pool Cap:<*> used'}
        for line, tpl in zip(logs, result):
            assert template_covers(tpl, line)

    def test_kv_compound_id_deleted(self):
        """原规则 (?<=[A-Za-z]:)(?=\\w*\\d)\\w+(?:_\\w+)*（name:fsi_snap_fs5041_1102702）
        → 承接：kv 同字段名 merge_values（数字组边界修剪）
        正例：见 test_kv_composite_merge_without_mask；反例：见
        test_same_key_states_keep_candidates（状态词不塌缩）"""
        assert True  # 由 TestTokenStructure 对应用例承接

    def test_compound_num_id_deleted(self):
        """原规则 [A-Za-z]\\w*\\d+(?:_\\d+)+（裸复合标识符 fsi_snap_fs5041_1058676）
        → 承接：_structure_aware_merge 裸标识符分支（完整公共前缀保留）
        正例：archive fsi_snap_fs{N}_{M} ok ×6 → archive fsi_snap_fs<*> ok"""
        logs = [f'archive fsi_snap_fs{5041 + i}_{1058676 + i * 11} ok' for i in range(6)]
        parser = make_parser(br_thresh=5)
        result = parser.parse_list(logs)
        assert set(result) == {'archive fsi_snap_fs<*> ok'}
        for line, tpl in zip(logs, result):
            assert template_covers(tpl, line)

    def test_compound_hex_id_deleted(self):
        """原规则（含 0x 复合标识符整体匹配 MTOBJ_DP0x0_RV0x23_ID0x0）
        → 承接：embedded_hex（保留）+ 裸标识符/LCP 结构合并
        正例：见 test_bracket_composite_merge_without_mask"""
        assert True  # 由 TestTokenStructure 对应用例承接

    def test_bracket_compound_id_deleted(self):
        """原规则 (?<=\\[)[a-fA-F\\d]+(?:_...)+(?=\\])（cap:[hex_hex]）
        → 承接：numerical 对 4+ hex 段的边界遮蔽 + LCP 结构合并
        正例：见 test_bracket_composite_merge_without_mask 与下方 cap 用例"""
        logs = [f'cap:[9d1d{i:02d}_8d00a477][0_0_{i}][0_0_0] updated' for i in range(6)]
        parser = make_parser(br_thresh=5)
        result = parser.parse_list(logs)
        assert len(set(result)) == 1
        for line, tpl in zip(logs, result):
            assert template_covers(tpl, line)

    def test_underscore_num_suffix_deleted(self):
        """原规则 (?<=[A-Za-z])_\\d+(?=...)（extend_2、CSD_8）
        → 承接：裸标识符分支（公共前缀 + <*>）
        正例：task extend_{N} done ×6 → task extend_<*> done"""
        logs = [f'task extend_{2 + i} done' for i in range(6)]
        parser = make_parser(br_thresh=5)
        result = parser.parse_list(logs)
        assert set(result) == {'task extend_<*> done'}

    def test_xnet_ctx_id_deleted(self):
        """原规则 (?<=\\()xnet_\\w+:\\d+(?=\\))（(xnet_r0_0_14:0)）—— 生产数据
        A/B 评估后删除（mask_ab_report：目标形态零命中，零代价）。

        多 token 场景由结构机制承接：ctx (xnet_r0_{N}_14:0) linked 归并单簇，
        模板覆盖原行。已知边界：整行单 token 形如 (xnet_x0_2_13:0)(num:5)server
        时 st_dict[1]=1 要求精确匹配且变量位于 kv key 位，删除后该形态将按
        取值分簇——生产回归若发现该形态成簇异常，恢复本规则即可。"""
        # 多 token：结构机制承接（规则已删除，直接验证）
        logs = [f'ctx (xnet_r0_{i}_14:0) linked' for i in range(6)]
        parser = make_parser(br_thresh=5)
        result = parser.parse_list(logs)
        assert len(set(result)) == 1
        for line, tpl in zip(logs, result):
            assert template_covers(tpl, line)

    def test_deleted_rules_not_in_sequence(self):
        """台账完备性：6 条结构承接删除 + 4 条纯冗余删除 + xnet_ctx_id
        （生产 A/B 零代价删除）；无保留候选。"""
        deleted = {'underscore_composite_num', 'kv_compound_id', 'compound_num_id',
                   'compound_hex_id', 'bracket_compound_id', 'underscore_num_suffix',
                   'xnet_ctx_id',
                   # 纯冗余（等价承接，非结构机制）：与既有规则替换区间相同或可归约
                   'datetime_full', 'kernel_uptime', 'epoch_ms_13d', 'code_line_num'}
        assert deleted.isdisjoint(preprocessing.sequence)
        assert deleted.isdisjoint(preprocessing.regex_match)
        assert deleted.isdisjoint(preprocessing.regex_map)
        for rule, _, _ in preprocessing._CLEANUP_RULES:
            assert rule not in deleted

    def test_datetime_full_deleted(self):
        """原规则 \\b\\d{4}-\\d{2}-\\d{2}\\s\\d{2}:\\d{2}:\\d{2}\\b（整体日期时间）
        → 承接：date + time 依次匹配产生 "<*> <*>"，_reduce_placeholders
        归约为单个 "<*>"，输出与整体遮蔽逐字节一致（等价承接，非结构机制）。
        正例：at 2026-02-04 11:39:58 job 88 done ×6 → at <*> job <*> done"""
        logs = [f'at 2026-02-04 11:39:{58 - i} job {88 + i} done' for i in range(6)]
        parser = make_parser(br_thresh=5)
        result = parser.parse_list(logs)
        assert set(result) == {'at <*> job <*> done'}
        for line, tpl in zip(logs, result):
            assert template_covers(tpl, line)

    def test_epoch_ms_13d_deleted(self):
        """原规则 (?<=\\[)\\d{13}(?=\\])（方括号内 13 位时间戳）
        → 承接：numerical 对同一数字区间产生相同替换（替换区间相同 → 纯冗余）。
        正例：[1500001430000] task 1001 done ×6 → [<*>] task <*> done"""
        logs = [f'[{1500001430000 + i}] task {1001 + i} done' for i in range(6)]
        parser = make_parser(br_thresh=5)
        result = parser.parse_list(logs)
        # [<*>] 头部块被日志头清理吞掉（与删除前行为一致，头部为噪声）
        assert set(result) == {'task <*> done'}
        for line, tpl in zip(logs, result):
            assert template_covers(tpl, line.split('] ', 1)[1])

    def test_code_line_num_deleted(self):
        """原规则 (?<=,)\\d+(?=\\])（",178]" 动态行号）
        → 承接：numerical 对同一数字区间产生相同替换（纯冗余）。
        正例：行号漂移的 6 条日志归并为同一模板。"""
        logs = [f'[cluster_view.c:procUpdateView,17{i}] node ok' for i in range(6)]
        parser = make_parser(br_thresh=5)
        result = parser.parse_list(logs)
        assert len(set(result)) == 1
        for line, tpl in zip(logs, result):
            assert template_covers(tpl, line)

    def test_kernel_uptime_deleted(self):
        """原规则 (?<=\\[)\\s*\\d+\\.\\d+(?=\\])（方括号内内核运行时间）
        → 承接：numerical 对浮点部分产生相同替换；内部前导空格产生的
        "[ <*>]" 由放宽后的日志头清理（\\[\\s*<\\*>\\s*\\]）吸收。
        正例：[ 4247.652837] app started ×6 → app started（头部整体清除）。"""
        logs = [f'[ {4240 + i}.{652800 + i * 7}] app started' for i in range(6)]
        parser = make_parser(br_thresh=5)
        result = parser.parse_list(logs)
        # 头部整体清除（与删除前行为一致，头部为噪声）
        assert set(result) == {'app started'}
        for line, tpl in zip(logs, result):
            assert template_covers(tpl, line.split('] ', 1)[1])


# =====================================================================
# 第四部分：保护性回归（当前正确，必须保持）
# =====================================================================

class TestProtectedBehavior:
    def test_numerical_preserves_trailing_period(self):
        """句点仅在后跟数字时才被数值规则消耗（句点是分隔符结构证据）。"""
        # 正例：数字后紧跟非数字的句点不再被吞
        assert preprocessing.preprocess(
            'name:17500.objType', use_sequence=['numerical']) == 'name:<*>.objType'
        assert preprocessing.preprocess(
            'clusterId:0.Res', use_sequence=['numerical']) == 'clusterId:<*>.Res'
        # 句尾句点保留（句子的结构证据）
        assert preprocessing.preprocess(
            'done in 5.', use_sequence=['numerical']) == 'done in <*>.'
        # 真小数仍整体通配
        assert preprocessing.preprocess(
            'value is 3.14 ok', use_sequence=['numerical']) == 'value is <*> ok'
        # size 的数字部分同步修复
        assert preprocessing.preprocess(
            'size 4.5KB ok', use_sequence=['size']) == 'size <*> ok'

    def test_preprocess_use_keys_supports_all_active_rules(self):
        """use_keys 分支必须覆盖默认 sequence 中的每条规则。"""
        assert set(preprocessing.sequence) <= set(preprocessing.regex_map)
        result = preprocessing.preprocess(
            '2026-02-04 11:39:58 INFO pid:53912,tid:53969',
            use_keys=True,
        )
        # datetime_full 已删除：date + time 依次遮蔽 + 归约等价承接
        assert 'DATE_TAG' in result
        assert 'TIME_TAG' in result
        assert 'LOG_LEVEL_TAG' in result
        assert 'PID_TID_TAG' in result

    def test_deleted_redundant_rules_not_in_sequence(self):
        """台账完备性：4 条纯冗余规则已删除（等价承接验证见下方 ledger）。"""
        deleted = {'datetime_full', 'kernel_uptime', 'epoch_ms_13d', 'code_line_num'}
        assert deleted.isdisjoint(preprocessing.sequence)
        assert deleted.isdisjoint(preprocessing.regex_match)

    def test_header_bracket_cleanup_tolerates_inner_space(self):
        """kernel_uptime 删除后，[ <浮点>] 头部仍需被日志头清理吞掉。"""
        result = preprocessing.preprocess('[ 4247.652837] boot ok')
        assert result == 'boot ok'

    def test_identical_lines_single_template(self):
        parser = make_parser()
        result = parser.parse_list(['connect to db ok'] * 3)
        assert set(result) == {'connect to db ok'}
        assert len(parser.logCluL) == 1

    def test_distinct_lines_separate_templates(self):
        parser = make_parser()
        result = parser.parse_list(['user login success', 'disk write failed'])
        assert result[0] != result[1]
        assert len(parser.logCluL) == 2

    def test_output_templates_cover_source_lines(self):
        """核心不变量：输出模板必须能覆盖其来源日志行（防误合并的反向保障）。"""
        corpus = [
            'task 1001 started by scheduler',
            'task 1002 started by scheduler',
            'connection from 10.0.0.1:8080 accepted',
            'connection from 10.0.0.9:9090 accepted',
            'write file /var/log/a.txt done',
            'write file /var/log/b.txt done',
            'user login success',
            'disk write failed',
        ]
        parser = make_parser()
        result = parser.parse_list(corpus)
        assert len(result) == len(corpus)
        for line, tpl in zip(corpus, result):
            assert template_covers(tpl, line), (line, tpl)

    def test_empty_and_degenerate_inputs(self):
        parser = make_parser()
        assert parser.parse_list([]) == []
        assert parser.parse_list(['']) == ['']

    def test_action_words_not_force_merged(self):
        """状态/动作词不同，不能仅因结构一致而归并。"""
        parser = make_parser()
        result = parser.parse_list([
            'job status:running finished ok',
            'job status:stopped failed hard',
        ])
        assert result[0] != result[1]

    def test_similar_structure_different_keys_not_merged(self):
        """key:value 结构相同但字段名不同，合并不应丢失字段名证据。

        修复 bug-1 后：行间仍归入同簇，但输出以多候选保留 src/dst 差异，
        不塌缩为 <*>。
        """
        parser = make_parser()
        result = parser.parse_list([
            'read src:blockdev0 size 4096 done',
            'read dst:cache0 size 4096 done',
        ])
        assert 'src' in result[0]
        assert 'dst' in result[1]

    def test_templatematches_semantics(self):
        assert Logcluster.templateMatches('a <*> c', 'a b c')
        assert Logcluster.templateMatches('a <*> c', 'a <*> c')
        assert not Logcluster.templateMatches('a <*> c', 'a b d')
        assert not Logcluster.templateMatches('a b c', 'a b')

    def test_tree_structure_basic(self):
        """新簇初始挂在 'None' 常量下，按序列长度分桶（常量重索引由
        update_population 阈值触发，见 test_update_population_triggers_reindex）。"""
        parser = make_parser()
        parser.parse_list(['alpha one two', 'beta three four'])
        assert 3 in parser.storage_tree['None']
        assert len(parser.storage_tree['None'][3]) == 2

    def test_update_population_triggers_reindex(self):
        """缓存关闭时 hit_time 达到 update_population 触发常量重索引（基线行为）。"""
        parser = make_parser(update_population=3)
        parser.parse_list(['alpha omega'] * 5)
        cluster = parser.logCluL[0]
        assert cluster.hit_time == 5
        assert cluster not in parser.storage_tree['None'].get(2, [])
        assert cluster in parser.storage_tree['alpha'].get(2, [])


class TestPostProcessing:
    def test_bracket_balance_fix(self):
        """未匹配的关闭括号前必须插入对应的开启括号。"""
        fixed = correct_single_template('name <*> )')
        fixed_no_wild = fixed.replace('<*>', '')
        depth = 0
        for ch in fixed_no_wild:
            if ch in '([{':
                depth += 1
            elif ch in ')]}':
                depth -= 1
        assert depth == 0, fixed
        assert '(' in fixed

    def test_cv_reduction(self):
        assert correct_single_template('a <*> <*> b') == 'a <*> b'

    def test_double_space(self):
        assert correct_single_template('a  b') == 'a b'

    def test_assignment_masking(self):
        assert correct_single_template('id=abc123') == 'id=<*>'



# =====================================================================
# 第七部分：外部审视意见复现测试（修复前失败，逐提交转绿）
# =====================================================================

class TestReviewIssues:
    """审视意见逐条复现：每个用例对应一个已实证的缺陷。"""

    def test_old_cache_missing_tpl_version_survives_update(self):
        """审视#1：旧版本缓存无 _tpl_version 属性，加载后继续更新不得崩溃。"""
        c = Logcluster(logTemplate=['event', 'x', 'ready'], br_thresh=5)
        c.pathList = [list(c.logTemplate)]
        del c._tpl_version  # 模拟旧缓存
        try:
            c.updateSequence(['event', 'y', 'ready'])
        except AttributeError:
            pytest.fail('旧缓存簇缺少 _tpl_version，updateSequence 直接崩溃')

    def test_reindex_returns_new_constant(self):
        """审视#3：_reindex_from_none 须返回新桶名供后续 mergeTemplates 使用。"""
        parser = make_parser()
        parser.parse_list(['event alpha ready', 'event alpha ready'])
        c = parser.logCluL[0]
        c.logTemplate = ['event', '<*>', 'ready']
        # 注册 event 常量键（原版语义：常量键由重索引建立）
        parser.shallow_tree.setdefault('event', [])
        parser.storage_tree.setdefault('event', {})
        ret = parser._reindex_from_none(c)
        assert ret == 'event', '搬桶后必须返回新桶名'

    def test_bucket_constant_key_order(self):
        """审视#4：桶优先级须沿用键建立顺序（原版语义），非输入词序。"""
        parser = make_parser()
        # 先注册 alpha，后注册 beta（键序 alpha→beta）
        parser.shallow_tree.setdefault('alpha', [])
        parser.shallow_tree.setdefault('beta', [])
        parser._constant_keys.update({'alpha', 'beta'})
        # 输入词序 beta 在前——原版键序语义应选 alpha
        assert parser._bucket_constant(['beta', 'alpha', 'x']) == 'alpha'

    def test_capacity_merge_preserves_state_words(self):
        """审视#5：路径容量满时状态词保护必须与未满时一致。"""
        logs = [f'job status:{"running" if i % 2 == 0 else "stopped"} mode m{i % 3} done'
                for i in range(12)]
        parser = make_parser()
        result = parser.parse_list(logs)
        joined = ' '.join(result)
        assert 'running' in joined and 'stopped' in joined, \
            '容量合并把 running/stopped 塌缩为 <*>（容量不应决定状态词重要性）'

    def test_capacity_merge_keeps_path_coverage(self):
        """审视#6：容量合并写回时，不得破坏其他路径对其来源行的覆盖。

        构造 lt 不变量失效的状态（旧版本缓存/迁移场景可达）：
        logTemplate[pos] 为具体值 'eth0'，但路径中存在 'wlan0'。
        容量合并 merge('eth0','eth6')='eth<*>'
        不覆盖 'wlan0'——无条件写回全部路径会破坏该路径的覆盖。"""
        c = Logcluster(logTemplate=['link', 'eth0', 'up', 'fast'], br_thresh=2)
        c.pathList = [['link', 'eth0', 'up', 'fast'],
                      ['link', 'wlan0', 'down', 'slow']]
        c.templateList = ['link eth0 up fast', 'link wlan0 down slow']
        ret = c.updateSequence(['link', 'eth6', 'up', 'fast'])
        # 无论合并/拒绝，两条路径都必须仍覆盖各自来源行
        p1, p2 = c.pathList
        assert ' '.join(p1).startswith('link eth') and 'up' in p1, f'p1: {p1}'
        assert p2 == ['link', 'wlan0', 'down', 'slow'] or ' '.join(p2).startswith('link wlan') \
            or ret == 'rejected', f'容量合并破坏了 wlan0 路径: {p2}'
        for path, src in zip(c.pathList, [['link', 'eth0', 'up', 'fast'],
                                          ['link', 'wlan0', 'down', 'slow']]):
            merged_tpl = ' '.join(path)
            covered = _token_covers(merged_tpl.split()[1] if '<*>' in merged_tpl else src[1], src[1]) \
                if False else all(a == b or '<*>' in a for a, b in zip(path, src))
            assert covered, f'路径不再覆盖来源行: {path} vs {src}'

    def test_load_cache_preserves_duplicate_template_clusters(self):
        """审视#2：同 logTemplate 不同路径的簇（A/B）加载后都必须保留。"""
        import os
        import tempfile
        parser = make_parser()
        ca = Logcluster(logTemplate=['event', '<*>', 'ready'], br_thresh=5)
        ca.pathList = [['event', 'alpha', 'ready']]
        ca.templateList = ['event alpha ready']
        cb = Logcluster(logTemplate=['event', '<*>', 'ready'], br_thresh=5)
        cb.pathList = [['event', 'beta', 'ready']]
        cb.templateList = ['event beta ready']
        parser.logCluL = [ca, cb]
        parser.storage_tree['None'][3] = [ca, cb]
        parser.shallow_tree['None'] = [ca, cb]
        with tempfile.NamedTemporaryFile(suffix='.pkl', delete=False) as f:
            path = f.name
        try:
            parser.cache_path = path
            parser.use_cache = True
            parser.save_cache()
            p2 = make_parser(use_cache=True, cache_path=path)
            assert len(p2.logCluL) == 2, \
                f'加载去重丢失了簇（期望 2 个，实际 {len(p2.logCluL)} 个）'
            paths = {tuple(p) for c in p2.logCluL for p in c.pathList}
            assert ('event', 'alpha', 'ready') in paths
            assert ('event', 'beta', 'ready') in paths
        finally:
            os.unlink(path)

    def test_short_version_left_boundary(self):
        """审视#7：短版本号清理须有左边界，不得把 AV1, 局部替换成 A<*>。"""
        assert correct_single_template('value AV1,<*> ok') == 'value AV1,<*> ok'

    def test_brace_cleanup_preserves_word_value(self):
        """审视#7：brace 组归约只限 ID 形态（含数字），词值不得整组吞掉。"""
        out = preprocessing.preprocess('set {1234:running} loaded')
        assert 'running' in out, f'词值被整组吞掉: {out!r}'

    def test_postprocess_cv_reduces_semicolon(self):
        """审视#7：后处理 <*> 链归约字符集须与预处理一致（含分号）。"""
        assert correct_single_template('a <*>;<*> b') == 'a <*> b'

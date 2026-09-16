#!/usr/bin/python3.7
# -*- coding: utf-8 -*-
"""解析入口一致性测试（第七节：在线/离线 Content 提取语义统一、混合 module 状态）。

覆盖：
  - 时间提取失败不破坏 Content 提取（load_data 与 load_content 语义一致）
  - 在线 adapter 与离线 only 两条链路对同一日志得到相同解析输入
  - adapter 复用 context 已有格式解析对象（不逐请求重建）
  - 混合 module 共享单一 PIPLUP 状态的现有设计记录（不擅自重构）

运行方式（仓库根目录）：
  python -m pytest test/piplup_entry_test.py -v
"""
from types import SimpleNamespace

import pytest

from src.data_process.template_extractor.log_formatter_parse import LogFormatParser
from src.data_process.template_extractor.log_template_parsing_process_piplup import (
    parse_logs_piplup_adapter,
    parse_logs_piplup_only,
)
from src.data_process.template_extractor.parsing_algorithm.PIPLUP import LogParser


def make_format_dict():
    # 与真实配置一致：字面方括号以 \[ 转义书写
    return {
        'mA': {
            'log_format': ['\\[<Time>\\] <Content>'],
            'time': ['Time'],
            'time_format': ['%Y-%m-%d %H:%M:%S'],
        },
    }


class MockRepository:
    def __init__(self):
        self.records = []

    def add(self, relation):
        self.records.append(relation)


class MockContext:
    """最小 context 替身：只提供 adapter/only 用到的属性。"""

    def __init__(self, use_shared_parser=True):
        self.log_format = make_format_dict()
        self.parser = LogFormatParser(self.log_format)
        self.parser_piplup = LogParser(log_format='<Content>', module_name='mixed',
                                       indir='.', outdir='/tmp/',
                                       use_cache=False, use_template_cache=False)
        self.log_repository = MockRepository()
        self._use_shared_parser = use_shared_parser

    def get_template_param(self):
        return {}

    def get_format_parse(self):
        return self.parser


class TestContentExtractionConsistency:
    def test_load_data_time_failure_keeps_content(self):
        """时间字段无法解析时，load_data 必须仍返回已提取的 Content
        （时间走降级链路），而不是丢弃内容退回整行。"""
        parser = LogFormatParser(make_format_dict())
        ts, content = parser.load_data('mA', '[bad-time] alpha ready')
        assert ts is None
        assert content == 'alpha ready'

    def test_load_data_normal_case(self):
        from datetime import datetime
        parser = LogFormatParser(make_format_dict())
        ts, content = parser.load_data('mA', '[2026-01-02 03:04:05] alpha ready')
        expected_ms = int(datetime(2026, 1, 2, 3, 4, 5).timestamp() * 1000)
        assert ts == expected_ms
        assert content == 'alpha ready'

    def test_load_data_offline_consistency(self):
        """时间解析失败场景下，load_data 与 load_content 的 Content 一致。"""
        fmt = make_format_dict()
        online = LogFormatParser(fmt)
        offline = LogFormatParser(fmt)
        log = '[bad-time] connect to db failed'
        _, c_online = online.load_data('mA', log)
        c_offline = offline.load_content('mA', log)
        assert c_online == c_offline == 'connect to db failed'

    def test_adapter_and_only_same_input(self):
        """在线 adapter 与离线 only 对同一（时间不可解析）日志产生相同
        的 PIPLUP 输入与模板。"""
        log = '[bad-time] alpha ready'
        ctx = MockContext()
        online = parse_logs_piplup_adapter(
            [{'rawLog': log, 'module': 'mA', 'source': 's', 'logId': 'l1'}],
            ctx, use_format=True)
        # 离线：独立的解析器与上下文
        ctx2 = MockContext()
        offline = parse_logs_piplup_only(
            [{'rawLog': log, 'module': 'mA', 'logId': 'l2'}],
            ctx2.parser_piplup, ctx2, use_format=True)
        assert online[0]['template'] == offline[0]
        # 模板来自 Content 而非整行（整行会含 [bad-time] 头）
        assert 'bad-time' not in online[0]['template']

    def test_adapter_reuses_context_parser(self):
        """adapter 必须复用 context 的格式解析对象，不逐请求重建。"""
        ctx = MockContext()
        parse_logs_piplup_adapter(
            [{'rawLog': '[2026-01-02 03:04:05] alpha ready', 'module': 'mA',
              'source': 's', 'logId': 'l1'}],
            ctx, use_format=True)
        # 复用断言：调用后 context.parser 的模块正则缓存已填充
        assert 'mA' in ctx.parser.format_regex

    def test_timestamp_fallback_chain(self):
        """时间解析失败时 adapter 走正则降级（TIME_PATTERN），并保留 Content。"""
        ctx = MockContext()
        results = parse_logs_piplup_adapter(
            [{'rawLog': '[bad-time] alpha ready 2026-01-02 03:04:05',
              'module': 'mA', 'source': 's', 'logId': 'l1'}],
            ctx, use_format=True)
        # 格式正则匹配到 Content；时间从正文中降级提取成功
        assert results[0]['template'] == 'alpha ready <*>'

    def test_repository_records_relation(self):
        ctx = MockContext()
        parse_logs_piplup_adapter(
            [{'rawLog': '[2026-01-02 03:04:05] alpha ready', 'module': 'mA',
              'source': 's', 'logId': 'l1'}],
            ctx, use_format=True)
        assert len(ctx.log_repository.records) == 1


class TestMixedModuleState:
    """现有设计记录：LogContext 持有单一 parser_piplup，所有 module 共享
    聚类状态与模板缓存文件（{module_name}_piplup_cache.pkl 按 module_name
    而非逐 module 命名）。混合批次中跨 module 的相同/相似日志会进入同一簇、
    获得相同 template_id（模板 ID 按内容 CRC32 生成，与 module 无关）。
    本轮不扩大为跨模块模型重构，仅以测试固化该行为。"""

    def test_same_content_across_modules_same_template_id(self):
        ctx = MockContext()
        results = parse_logs_piplup_adapter(
            [{'rawLog': '[bad-time] alpha ready', 'module': 'mA',
              'source': 's', 'logId': 'l1'},
             {'rawLog': '[bad-time] alpha ready', 'module': 'mB',
              'source': 's', 'logId': 'l2'}],
            ctx, use_format=False)
        assert results[0]['template_id'] == results[1]['template_id']
        # 但模块归属保留
        assert results[0]['module'] == 'mA'
        assert results[1]['module'] == 'mB'

    def test_similar_logs_across_modules_share_cluster(self):
        """不同 module 的相似日志进入同一簇（共享状态的相互影响，设计使然）。"""
        parser = LogParser(log_format='<Content>', module_name='mixed',
                           indir='.', outdir='/tmp/', use_cache=False,
                           use_template_cache=False)
        parser.parse_list(['svc alpha ready'])       # module A 的日志
        parser.parse_list(['svc beta ready'])        # module B 的相似日志
        assert len(parser.logCluL) == 1
        cluster = parser.logCluL[0]
        assert len(cluster.pathList) == 2  # 多路径保留两个候选

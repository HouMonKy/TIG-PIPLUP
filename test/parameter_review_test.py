"""Review regressions for coordinates, shared evidence and compatibility."""
import json
from types import SimpleNamespace

import pytest

from src.anomaly_detection.parameter_anomaly.contracts import LogEntry, ParamInfoPort
from src.anomaly_detection.parameter_anomaly.param_detector import ParamAnomalyDetector
from src.anomaly_detection.parameter_anomaly.pipeline_adapter import (
    attach_first_parse, _slot_anchors)
from src.anomaly_detection.parameter_anomaly.position_mapper import tokenize_for_params_with_spans


@pytest.mark.parametrize('raw, expected', [
    ('ret=10', ['ret', '=', '10']),
    ('code=10, ret:[-2]', ['code', '=', '10', 'ret', ':', '[', '-2', ']']),
    ('a\t( 3 ) , b:4', ['a', '(', '3', ')', 'b', ':', '4']),
    ('中文=0x10', ['中文', '=', '0x10']),
    ('  plain  text ', ['plain', 'text']),
    ('   ', []),
])
def test_legacy_coordinate_examples(raw, expected):
    tokens, spans = tokenize_for_params_with_spans(raw)
    assert tokens == expected
    assert [raw[start:end] for start, end in spans] == expected


def test_whitespace_variants_share_cache_but_literals_remain_distinct():
    _slot_anchors.cache_clear()
    for template in ['task ret=<*>', 'task   ret = <*>']:
        relation = SimpleNamespace(param_list=[])
        attach_first_parse(relation, template=template, raw_content='task ret=10')
        assert [p.template_param for p in relation.param_list] == ['10']
    assert _slot_anchors.cache_info().misses == 1
    assert _slot_anchors.cache_info().hits == 1
    attach_first_parse(SimpleNamespace(param_list=[]), template='task! ret=<*>',
                       raw_content='task! ret=10')
    assert _slot_anchors.cache_info().misses == 2


def test_literal_angle_tag_is_not_a_parameter():
    relation = SimpleNamespace(param_list=[])
    attach_first_parse(relation, template='task <error>', raw_content='task <error>')
    assert relation.param_list == []


def test_compound_known_placeholder_is_still_compiled():
    anchors = _slot_anchors(('offset+<HEX0x>',))
    assert len(anchors) == 1 and anchors[0].sub_token


def test_coordinate_conflict_warns_without_logging_values(caplog):
    relation = SimpleNamespace(template_id=99, module='test',
                               param_list=[ParamInfoPort('3', 'private-old-value')])
    attach_first_parse(relation, template='task ret=<*>', raw_content='task ret=10')
    assert relation.param_list[0].template_param == '10'
    messages = [r.getMessage() for r in caplog.records if 'coordinate conflict' in r.getMessage()]
    assert len(messages) == 1
    assert 'private-old-value' not in messages[0]
    assert 'ret=10' not in messages[0]
    caplog.clear()
    attach_first_parse(relation, template='task ret=<*>', raw_content='task ret=10')
    assert not caplog.records


def test_nonnumeric_parameter_key_does_not_override_current_field():
    core = ParamAnomalyDetector('DME')._detector
    entry = LogEntry(raw_text='Local setup link fail ret ( 0 ) .',
                     module='kmm-server', template_id=99,
                     template_detected='Local setup link fail ret ( <*> ) .',
                     template_id_namespace='parser',
                     param_list=[ParamInfoPort('ret', '10')])
    result = core.detect(entry)
    assert result.status == 'normal'
    assert result.rule_results[0].value_raw == '0'


def test_shared_input_json_roundtrip_and_repeated_detection():
    relation = SimpleNamespace(param_list=[], template_id=99, module='kmm-server')
    attach_first_parse(relation, template='Local setup link fail ret ( <*> ) .',
                       raw_content='Local setup link fail ret ( 10 ) .')
    detector = ParamAnomalyDetector('DME')
    entry = detector._to_log_entry(relation)
    encoded = json.loads(json.dumps(entry.as_dict()))
    restored = LogEntry(**encoded)
    assert restored.template_id_namespace == 'parser'
    assert detector._detector.detect(restored).status == 'anomaly'
    original_tokens = entry.content_tokens
    for _ in range(2):
        assert detector._detector.detect(entry).status == 'anomaly'
        assert entry.content_tokens is original_tokens

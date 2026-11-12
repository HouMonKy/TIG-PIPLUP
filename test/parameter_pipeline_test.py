"""Regression tests from real PIPLUP parsing through the business merger."""
import json
import shutil
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from src.common.global_config import GlobalConfig
from src.data_process.log_context import LogContext
from src.data_process.template_extractor.log_template_parsing_process_piplup import (
    parse_logs_piplup_adapter)
from src.anomaly_detection.anomaly_detection import AnomalyDetection
from src.anomaly_detection.parameter_anomaly.contracts import LogEntry, ParamInfoPort
from src.anomaly_detection.parameter_anomaly.param_detector import ParamAnomalyDetector
from src.data_process.offline_data_plugin.offline_detection_tool_with_piplup import main


BAD = 'DEVM FAILOVER:Lif(0x40090000ff000003) failover(reason:0) dest port(748600) is invalid.'
GOOD = BAD.replace('reason:0', 'reason:3')
RAWS = [BAD, GOOD, 'mark=334800 ' + BAD, GOOD + ' zone=300300']


@pytest.fixture
def context(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1]
    shutil.copytree(source / 'config', tmp_path / 'config',
                    ignore=shutil.ignore_patterns('persistence'))
    config = Path('src/anomaly_detection/parameter_anomaly/configuration')
    shutil.copytree(source / config, tmp_path / config,
                    ignore=shutil.ignore_patterns('__pycache__'))
    monkeypatch.setattr(GlobalConfig, 'get_project_root', staticmethod(lambda: str(tmp_path)))
    AnomalyDetection._product_instances.clear()
    yield LogContext(product_name='DME', module_name='Messages')
    AnomalyDetection._product_instances.clear()


def parse(context, raws, **kwargs):
    now = time.time() * 1000
    rows = [dict(rawLog='[2026-11-12 12:00:00.000] ' + raw,
                 module='Messages', source='fixture', logId=str(i), timestamp=now)
            for i, raw in enumerate(raws)]
    return parse_logs_piplup_adapter(rows, context, **kwargs)


def test_batch_shares_current_parameters_and_merges(context):
    with patch.object(context.parser_piplup, 'parse_list',
                      wraps=context.parser_piplup.parse_list) as parser:
        parsed = parse(context, RAWS)
        relation = context.log_repository.logs['Messages']['0']
        assert '0' in [p.template_param for p in relation.param_list]
        assert relation.template and relation.raw_content
        detector = AnomalyDetection(mode='parametric', product='DME')
        # Downstream must consume upstream token coordinates, without parsing again.
        with patch('src.anomaly_detection.parameter_anomaly.detector.tokenize_for_params_with_spans',
                   side_effect=AssertionError('repeated parameter tokenization')):
            result = detector.all_detect(0, float('inf'), 60000,
                                         context.log_repository, context)
        assert parser.call_count == 1
    lines = [line for group in result for line in group['detectLine']]
    assert {line['logId'] for line in lines} == {'0', '2'}
    for line in lines:
        original = parsed[int(line['logId'])]
        assert int(line['templateId']) == original['template_id']
        assert line['template'] == original['template']
        assert line['anomalyType'] == ['parametric']


def test_disabled_parameter_path_retains_old_behavior(context):
    parse(context, [BAD], use_params=False)
    relation = context.log_repository.logs['Messages']['0']
    assert not relation.param_list and relation.template is None
    assert not hasattr(relation, 'raw_content')


def test_multiple_values_and_single_source_query(context):
    parse(context, [BAD.replace('748600', '0xffffffffffffffff')])
    logs = context.log_repository.get_window_logs_by_module_and_source(
        0, float('inf'), 'Messages', 'fixture', context.get_drain('Messages'))
    outliers, events, total = ParamAnomalyDetector('DME').detect(logs, 60000)
    assert total == 2
    assert {outlier['param_value'] for outlier in outliers} == {'0', '0xffffffffffffffff'}
    assert {outlier['template_id'] for outlier in outliers} == {logs[0].template_id}
    assert events


def test_missing_target_is_not_a_blacklisted_value(context):
    parse(context, [BAD.replace('reason:0', 'code:0')])
    detector = AnomalyDetection(mode='parametric', product='DME')
    result = detector.all_detect(0, float('inf'), 60000, context.log_repository, context)
    assert not [line for group in result for line in group['detectLine']]


def test_rule_ids_remain_separate_from_parser_ids():
    core = ParamAnomalyDetector('DME')._detector
    values = [ParamInfoPort(template_pos='6', template_param='10')]
    legacy = LogEntry(raw_text='', module='kmm-server', template_id=2, param_list=values)
    assert core.detect(legacy).status == 'anomaly'
    parsed = LogEntry(raw_text='Local setup link fail ret ( 0 ) .',
                      module='kmm-server', template_id=2,
                      template_detected='Local setup link fail ret ( <*> ) .',
                      param_list=values, template_id_namespace='parser')
    assert core.detect(parsed).status == 'normal'


def test_offline_main_writes_real_parameter_results(context, tmp_path, monkeypatch):
    data = tmp_path / 'input'
    data.mkdir()
    stamp = time.strftime('%Y-%m-%d %H:%M:%S')
    (data / 'all_merged_logs.log').write_text(''.join(f'[{stamp}] {raw}\n' for raw in RAWS))
    entry = 'src.data_process.offline_data_plugin.offline_detection_tool_with_piplup'
    monkeypatch.setattr(entry + '.Path', lambda path: data)
    main()
    snapshot = json.loads((data / 'snapshot.json').read_text())
    assert snapshot
    formatted = json.loads((data / 'snapshot_format.json').read_text())
    assert formatted
    monkeypatch.setattr(entry + '.Path', lambda path: tmp_path / 'nonexistent')
    assert main() is None

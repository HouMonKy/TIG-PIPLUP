#!/usr/bin/python3.7
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2012-2020. All rights reserved.
import pytest
from src.anomaly_detection.anomaly_base.data_models import Log, TemplateInfo, AnomalyResult, AnomalyEvent, AnomalyType


# 测试用例
def test_log_initialization():
    log = Log()
    assert log.source == ''
    assert log.logId == -1
    assert log.rawLog == ''
    assert isinstance(log.timestamp, int)


def test_template_info_initialization():
    template_info = TemplateInfo()
    assert template_info.template == ''
    assert template_info.templateId == -1
    assert isinstance(template_info.log, Log)
    assert template_info.timestamp == template_info.log.timestamp
    assert template_info.rawLog == template_info.log.rawLog
    assert template_info.logId == template_info.log.logId
    assert template_info.source == template_info.log.source


def test_anomaly_result_initialization():
    result = AnomalyResult()
    assert result.anomalyType == AnomalyType.SEMANTIC
    assert result.templateId == -1
    assert result.template == ''
    assert result.logId == -1
    assert result.rawLog == ''
    assert result.source == ''
    assert result.timestamp == result.templateInfo.log.timestamp


def test_anomaly_event_initialization():
    event = AnomalyEvent()
    assert event.anomalyResults == []
    assert event.score == 95
    with pytest.raises(IndexError):
        event.timestamp  # 没有 anomalyResults 时会引发 IndexError


def test_log_with_custom_values():
    log = Log(source='TestSource', logId=123, rawLog='Test log content')
    assert log.source == 'TestSource'
    assert log.logId == 123
    assert log.rawLog == 'Test log content'
    assert isinstance(log.timestamp, int)


def test_template_info_with_custom_values():
    log = Log(source='TestSource', logId=123, rawLog='Test log content')
    template_info = TemplateInfo(template='Test Template', templateId=123, log=log)
    assert template_info.template == 'Test Template'
    assert template_info.templateId == 123
    assert template_info.log.source == 'TestSource'
    assert template_info.log.logId == 123
    assert template_info.log.rawLog == 'Test log content'
    assert template_info.timestamp == log.timestamp


def test_anomaly_result_with_custom_values():
    log = Log(source='TestSource', logId=123, rawLog='Test log content')
    template_info = TemplateInfo(template='Test Template', templateId=123, log=log)
    result = AnomalyResult(anomalyType=AnomalyType.QUANTITY, templateInfo=template_info)
    assert result.anomalyType == AnomalyType.QUANTITY
    assert result.templateId == 123
    assert result.template == 'Test Template'
    assert result.logId == 123
    assert result.rawLog == 'Test log content'
    assert result.source == 'TestSource'
    assert result.timestamp == log.timestamp


def test_anomaly_event_with_custom_values():
    log1 = Log(source='Source1', logId=1, rawLog='Log1 content')
    template_info1 = TemplateInfo(template='Template1', templateId=1, log=log1)
    result1 = AnomalyResult(anomalyType=AnomalyType.SEMANTIC, templateInfo=template_info1)

    log2 = Log(source='Source2', logId=2, rawLog='Log2 content')
    template_info2 = TemplateInfo(template='Template2', templateId=2, log=log2)
    result2 = AnomalyResult(anomalyType=AnomalyType.QUANTITY, templateInfo=template_info2)

    event = AnomalyEvent(anomalyResults=[result1, result2], score=85)
    assert event.score == 85
    assert event.timestamp == log1.timestamp
    assert event.templateIds == [1, 2]
    assert event.logIds == [1, 2]
    assert event.sources == ['Source1', 'Source2']


def test_anomaly_result_equality():
    result1 = AnomalyResult(templateInfo=TemplateInfo(templateId='123'))
    result2 = AnomalyResult(templateInfo=TemplateInfo(templateId='123'))
    result3 = AnomalyResult(templateInfo=TemplateInfo(templateId='456'))
    assert result1 == result2
    assert result1 != result3
    assert list({result1, result2}) == [result1]


def test_anomaly_event_invalid_score():
    with pytest.raises(ValueError):
        AnomalyEvent(score=101)  # 分数范围是0-100
    with pytest.raises(ValueError):
        AnomalyEvent(score=-1)  # 分数范围是0-100

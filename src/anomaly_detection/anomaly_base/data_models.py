#!/usr/bin/python3.7
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2012-2020. All rights reserved.
from datetime import datetime
from typing import List, Dict
from enum import Enum
from pydantic import BaseModel, Field


class AnomalyType(str, Enum):
    SEMANTIC = "SEMANTIC"
    QUANTITY = "QUANTITY"
    PARAMETER = "PARAMETER"


class Log(BaseModel):
    source: str = ''
    logId: int = -1
    rawLog: str = ''
    timestamp: int = int(datetime.now().timestamp() * 1000)


class TemplateInfo(BaseModel):
    template: str = ''
    templateId: int = -1
    log: Log = Field(default_factory=Log)

    @property
    def timestamp(self) -> int:
        return self.log.timestamp

    @property
    def rawLog(self) -> str:
        return self.log.rawLog

    @property
    def logId(self) -> int:
        return self.log.logId

    @property
    def source(self) -> str:
        return self.log.source

    def __eq__(self, other):
        return self.templateId == other.templateId

    def __hash__(self):
        return hash(self.templateId)


class AnomalyResult(BaseModel):
    anomalyType: AnomalyType = AnomalyType.SEMANTIC
    templateInfo: TemplateInfo = Field(default_factory=TemplateInfo)

    @property
    def timestamp(self) -> int:
        return self.templateInfo.timestamp

    @property
    def templateId(self) -> int:
        return self.templateInfo.templateId

    @property
    def template(self) -> str:
        return self.templateInfo.template

    @property
    def logId(self) -> int:
        return self.templateInfo.logId

    @property
    def rawLog(self) -> str:
        return self.templateInfo.rawLog

    @property
    def source(self) -> str:
        return self.templateInfo.source

    def __eq__(self, other):
        return self.templateId == other.templateId

    def __hash__(self):
        return hash(self.templateId)

    def data(self) -> Dict:
        return {
            "timestamp": self.timestamp,
            "anomalyType": self.anomalyType,
            "templateId": self.templateId,
            "template": self.template,
            "log_id": self.logId,
            "rawlog": self.rawLog,
            "source": self.source,
        }


class AnomalyEvent(BaseModel):
    anomalyResults: List[AnomalyResult] = Field(default_factory=list)
    score: int = Field(95, ge=0, le=100)

    @property
    def timestamp(self):
        return self.anomalyResults[0].timestamp

    @property
    def templateIds(self) -> List[int]:
        return [anomaly_res.templateId for anomaly_res in self.anomalyResults]

    @property
    def logIds(self) -> List[int]:
        return [anomaly_res.logId for anomaly_res in self.anomalyResults]

    @property
    def sources(self) -> List[str]:
        return [anomaly_res.source for anomaly_res in self.anomalyResults]

    def data(self) -> Dict:
        return {
            "timestamp": self.timestamp,
            "templateId": self.templateIds,
            "log_id": self.logIds,
            "source": self.sources,
            "score": self.score,
        }

    @classmethod
    def from_data(cls, data: Dict) -> 'AnomalyEvent':
        anomaly_results = []
        for i, template_id in enumerate(data["templateId"]):
            anomaly_res = AnomalyResult(
                anomalyType="SEMANTIC",
                templateInfo=TemplateInfo(
                    templateId=template_id,
                    template="",
                    log=Log(
                        source=data["source"][i] if "source" in data else "",
                        logId=data["log_id"][i],
                        timestamp=data["timestamp"],
                        rawLog="",
                    )
                )
            )
            anomaly_results.append(anomaly_res)

        return cls(
            score=data["score"][0] if isinstance(data["score"], list) else data["score"],
            anomalyResults=anomaly_results,
        )


class Tag(BaseModel):
    fileName: str = ""
    source: str = ""
    format: str = ""
    lines: List[TemplateInfo] = Field(default_factory=list)
    rule: str = ""





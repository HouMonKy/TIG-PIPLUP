#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
This module defines the data models that will be used through the system.
"""
from datetime import datetime
from enum import Enum
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field


class AnomalyType(str, Enum):
    """Defines the type of anomaly."""
    SEMANTIC = "SEMANTIC"
    QUANTITY = "QUANTITY"
    PARAMETER = "PARAMETER"


class LogLevel(str, Enum):
    """
    log level.
    """
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"
    FATAL = "FATAL"
    UNKNOWN = "UNKNOWN"


class Log(BaseModel):
    """
    data model for raw log.
    """
    log_id: str = ""
    raw_log: str = ''
    timestamp: int = Field(default_factory=lambda: int(datetime.now().timestamp() * 1000),
                           description="The timestamp of the log")
    source: str = ''
    module: str = ''
    level: LogLevel = LogLevel.INFO
    line_number: int = 0
    file_name: str = ""
    meta_data: Dict[str, Any] = Field(default_factory=dict,
                                      description="The meta data of the log, \
                                      such as the thread id, the process id, etc.")

    def __str__(self) -> str:
        return f"{self.timestamp} {self.level} {self.file_name}:{self.line_number} \
            {self.module} {self.raw_log}"

    def __repr__(self) -> str:
        return self.__str__()

    @classmethod
    def from_dict(cls, line: Dict[str, Any], file_name: str = "", line_number: int = 0) -> 'Log':
        """
        parse log from a dictionary
        """
        # Get content or use empty string as fallback
        raw_log = line.get('raw_log', '')

        # Get level or use UNKNOWN as fallback
        level_str = line.get('level', None)
        if level_str and level_str in LogLevel.__members__:
            level = LogLevel(level_str)
        else:
            level = LogLevel.UNKNOWN

        # Get timestamp or use current time as fallback
        ts = line.get("timestamp", None)
        timestamp = ts if ts is not None else int(datetime.now().timestamp() * 1000)

        return Log(
            log_id=line.get("log_id", str(line_number)),
            raw_log=raw_log,
            timestamp=timestamp,
            source=line.get("source", ""),
            module=line.get('module', ""),
            level=level,
            line_number=line_number,
            file_name=file_name,
            meta_data={"pid": line.get("process", ""), "tid": line.get("thread", "")},
        )


class TemplateInfo(BaseModel):
    """
    data model for template info.
    """
    template: str = ''
    regex_pattern: str = ''
    template_id: str = ''
    log: Log = Log()

    @property
    def formatted_template(self) -> str:
        """
        get the regex pattern of the template
        """
        return self.regex_pattern.replace("\s*(.*?)", "<*>").replace("(.*?)", "<*>") if len(self.template) == 0 and len(
            self.regex_pattern) != 0 else self.template

    @property
    def timestamp(self) -> int:
        """
        get the timestamp of the template
        """
        return self.log.timestamp

    @property
    def raw_log(self) -> str:
        """
        get the raw log of the template
        """
        return self.log.raw_log

    @property
    def log_id(self) -> str:
        """
        get the log id of the template
        """
        return self.log.log_id

    @property
    def source(self) -> str:
        """
        get the source of the template
        """
        return self.log.source

    @property
    def module(self) -> str:
        """
        get the module of the template
        """
        return self.log.module

    def __eq__(self, other):
        if not isinstance(other, TemplateInfo):
            return NotImplemented
        return self.template_id == other.template_id

    def __hash__(self):
        return hash(self.template_id)


class TemplateInfoOneModule(BaseModel):
    """
    data model for template info.
    """
    template: str = ''
    regex_pattern: str = ''
    template_id: str = ''
    log: Log = Log()
    log_source: list[str] = []

    @property
    def formatted_template(self) -> str:
        """
        get the regex pattern of the template
        """
        return self.regex_pattern.replace("\s*(.*?)", "<*>").replace("(.*?)", "<*>") if len(self.template) == 0 and len(
            self.regex_pattern) != 0 else self.template

    @property
    def timestamp(self) -> int:
        """
        get the timestamp of the template
        """
        return self.log.timestamp

    @property
    def raw_log(self) -> str:
        """
        get the raw log of the template
        """
        return self.log.raw_log

    @property
    def log_id(self) -> str:
        """
        get the log id of the template
        """
        return self.log.log_id

    @property
    def source(self) -> str:
        """
        get the source of the template
        """
        return self.log.source

    @property
    def module(self) -> str:
        """
        get the module of the template
        """
        return self.log.module


    def __eq__(self, other):
        if not isinstance(other, TemplateInfo):
            return NotImplemented
        return self.template_id == other.template_id

    def __hash__(self):
        return hash(self.template_id)




class AbnormalLog(BaseModel):
    """
    data model for abnormal logs.
    """
    anomaly_type: AnomalyType = AnomalyType.SEMANTIC
    template_info: TemplateInfoOneModule = None

    @property
    def timestamp(self) -> int:
        """
        get the timestamp of the anomaly log
        """
        return self.template_info.timestamp

    @property
    def template_id(self) -> str:
        """
        get the template id of the anomaly log
        """
        return self.template_info.template_id

    @property
    def template(self) -> str:
        """
        get the template of the anomaly log
        """
        return self.template_info.template

    @property
    def log_id(self) -> str:
        """
        get the log id of the anomaly log
        """
        return self.template_info.log_id

    @property
    def line_number(self) -> int:
        """
        get the line number of the anomaly log
        """
        return self.template_info.log.line_number

    @property
    def raw_log(self) -> str:
        """
        get the raw log of the anomaly log
        """
        return self.template_info.raw_log

    @property
    def source(self) -> str:
        """
        get the source of the anomaly log
        """
        return self.template_info.source

    @property
    def module(self) -> str:
        """
        get the module of the anomaly log
        """
        return self.template_info.module

    @property
    def log_source(self) -> list[str]:
        """
        get the log source of the anomaly log
        """
        return self.template_info.log_source

    def __eq__(self, other):
        if not isinstance(other, AbnormalLog):
            return NotImplemented
        return self.template_id == other.template_id

    def __hash__(self):
        return hash(self.template_id)

    def to_dict(self) -> Dict:
        """
        convert the anomaly log to a dictionary
        """
        return {
            "timestamp": self.timestamp,
            "anomaly_type": self.anomaly_type,
            "template_id": self.template_id,
            "template": self.template,
            "log_id": self.log_id,
            "raw_log": self.raw_log,
            "source": self.source,
            "module": self.module,
            "level": self.template_info.log.level,
            "line_number": self.template_info.log.line_number,
            "file_name": self.template_info.log.file_name,
            "meta_data": self.template_info.log.meta_data,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> 'AbnormalLog':
        """
        create an anomaly log from a dictionary
        """
        anomaly_type = data.get("anomaly_type", None)
        if anomaly_type and anomaly_type in AnomalyType.__members__:
            anomaly_type = AnomalyType(anomaly_type)
        else:
            anomaly_type = AnomalyType.SEMANTIC

        template_info = TemplateInfo(
            template_id=data.get("template_id", -1),
            template=data.get("template", ""),
            log=Log.from_dict(data),
        )

        return cls(
            anomaly_type=anomaly_type,
            template_info=template_info,
        )


class AbnormalLogPerSource(BaseModel):
    """
    data model for abnormal logs.
    """
    anomaly_type: AnomalyType = AnomalyType.SEMANTIC
    template_info: TemplateInfo = None

    @property
    def timestamp(self) -> int:
        """
        get the timestamp of the anomaly log
        """
        return self.template_info.timestamp

    @property
    def template_id(self) -> str:
        """
        get the template id of the anomaly log
        """
        return self.template_info.template_id

    @property
    def template(self) -> str:
        """
        get the template of the anomaly log
        """
        return self.template_info.template

    @property
    def log_id(self) -> str:
        """
        get the log id of the anomaly log
        """
        return self.template_info.log_id

    @property
    def line_number(self) -> int:
        """
        get the line number of the anomaly log
        """
        return self.template_info.log.line_number

    @property
    def raw_log(self) -> str:
        """
        get the raw log of the anomaly log
        """
        return self.template_info.raw_log

    @property
    def source(self) -> str:
        """
        get the source of the anomaly log
        """
        return self.template_info.source

    @property
    def module(self) -> str:
        """
        get the module of the anomaly log
        """
        return self.template_info.module

    @property
    def log_source(self) -> list[str]:
        """
        get the log source of the anomaly log
        """
        return self.template_info.log_source

    def __eq__(self, other):
        if not isinstance(other, AbnormalLog):
            return NotImplemented
        return self.template_id == other.template_id

    def __hash__(self):
        return hash(self.template_id)

    def to_dict(self) -> Dict:
        """
        convert the anomaly log to a dictionary
        """
        return {
            "timestamp": self.timestamp,
            "anomaly_type": self.anomaly_type,
            "template_id": self.template_id,
            "template": self.template,
            "log_id": self.log_id,
            "raw_log": self.raw_log,
            "source": self.source,
            "module": self.module,
            "level": self.template_info.log.level,
            "line_number": self.template_info.log.line_number,
            "file_name": self.template_info.log.file_name,
            "meta_data": self.template_info.log.meta_data,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> 'AbnormalLog':
        """
        create an anomaly log from a dictionary
        """
        anomaly_type = data.get("anomaly_type", None)
        if anomaly_type and anomaly_type in AnomalyType.__members__:
            anomaly_type = AnomalyType(anomaly_type)
        else:
            anomaly_type = AnomalyType.SEMANTIC

        template_info = TemplateInfo(
            template_id=data.get("template_id", -1),
            template=data.get("template", ""),
            log=Log.from_dict(data),
        )

        return cls(
            anomaly_type=anomaly_type,
            template_info=template_info,
        )



class AbnormalEvent(BaseModel):
    """
    data model for abnormal event.
    """
    abnormal_logs: List[AbnormalLog] = Field(default_factory=list)
    window_start_time: int = int(datetime.now().timestamp() * 1000)
    score: int = Field(95, ge=0, le=100)

    @property
    def count(self) -> int:
        """
        get the count of the logs in Event.
        """
        return len(self.abnormal_logs)

    @property
    def start_time(self) -> int:
        """
        get the first appear time of the event.
        """
        return self.abnormal_logs[0].timestamp

    @property
    def end_time(self) -> int:
        """
        get the last appear time of the event.
        """
        return self.abnormal_logs[-1].timestamp

    @property
    def timespan(self) -> int:
        """
        get the time span of the event.
        """
        return self.end_time - self.start_time

    @property
    def template_ids(self) -> List[str]:
        """
        get the template ids of the event.
        """
        return list({abnormal_log.template_id for abnormal_log in self.abnormal_logs})

    @property
    def log_ids(self) -> List[str]:
        """
        get the log ids of the event.
        """
        return [abnormal_log.log_id for abnormal_log in self.abnormal_logs]

    @property
    def line_numbers(self) -> List[int]:
        """
        get the line numbers of the event.
        """
        return [abnormal_log.line_number for abnormal_log in self.abnormal_logs]

    def to_dict(self) -> Dict:
        """
        convert the event to a dictionary
        """
        return {
            "abnormal_logs": [log.to_dict() for log in self.abnormal_logs],
            "score": self.score,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> 'AbnormalEvent':
        """
        create an event from a dictionary
        """
        abnormal_logs = []
        for log in data["abnormal_logs"]:
            abnormal_log = AbnormalLog.from_dict(log)
            abnormal_logs.append(abnormal_log)
        return cls(
            score=data["score"][0] if isinstance(data["score"], list) else data["score"],
            abnormal_logs=abnormal_logs,
        )


class AbnormalEventPerSource(BaseModel):
    """
    data model for abnormal event.
    """
    abnormal_logs: List[AbnormalLogPerSource] = Field(default_factory=list)
    window_start_time: int = int(datetime.now().timestamp() * 1000)
    score: int = Field(95, ge=0, le=100)

    @property
    def count(self) -> int:
        """
        get the count of the logs in Event.
        """
        return len(self.abnormal_logs)

    @property
    def start_time(self) -> int:
        """
        get the first appear time of the event.
        """
        return self.abnormal_logs[0].timestamp

    @property
    def end_time(self) -> int:
        """
        get the last appear time of the event.
        """
        return self.abnormal_logs[-1].timestamp

    @property
    def timespan(self) -> int:
        """
        get the time span of the event.
        """
        return self.end_time - self.start_time

    @property
    def template_ids(self) -> List[str]:
        """
        get the template ids of the event.
        """
        return list({abnormal_log.template_id for abnormal_log in self.abnormal_logs})

    @property
    def log_ids(self) -> List[str]:
        """
        get the log ids of the event.
        """
        return [abnormal_log.log_id for abnormal_log in self.abnormal_logs]

    @property
    def line_numbers(self) -> List[int]:
        """
        get the line numbers of the event.
        """
        return [abnormal_log.line_number for abnormal_log in self.abnormal_logs]

    def to_dict(self) -> Dict:
        """
        convert the event to a dictionary
        """
        return {
            "abnormal_logs": [log.to_dict() for log in self.abnormal_logs],
            "score": self.score,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> 'AbnormalEvent':
        """
        create an event from a dictionary
        """
        abnormal_logs = []
        for log in data["abnormal_logs"]:
            abnormal_log = AbnormalLog.from_dict(log)
            abnormal_logs.append(abnormal_log)
        return cls(
            score=data["score"][0] if isinstance(data["score"], list) else data["score"],
            abnormal_logs=abnormal_logs,
        )




class Tag(BaseModel):
    """
    data model for labels.
    """
    file_name: str = ""
    module: str = ""
    line: Log = Log()
    line_id: int = 0
    rule: Dict = {}


class EnrichedTemplateInfo(BaseModel):
    """
    data model for enriched template info.
    """
    template_id: str = ""
    template: str = ""
    regex_pattern: str = ""
    log_id: str = ""
    raw_log: str = ""
    anomaly_type: AnomalyType = AnomalyType.SEMANTIC
    level: LogLevel = LogLevel.INFO

    # LLM enrichment
    description: str = "The description of the template"
    keywords: List[str] = ["keywords", "template"]
    fault_type: str = "The fault type of the template"
    remediation: str = "The remediation of the template"
    semantic_interpretation: str = "The semantic interpretation of the template"
    potential_causes: List[str] = ["potential", "causes", "template"]
    impact_assessment: str = "The impact assessment of the template"
    related_components: List[str] = ["related", "components", "template"]
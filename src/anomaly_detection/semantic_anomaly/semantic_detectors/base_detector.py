from abc import ABC, abstractmethod
from typing import List, Dict, Any
from src.anomaly_detection.semantic_anomaly.data_models import TemplateInfo, AbnormalLog


class BaseDetector(ABC):
    """
    Abstract base class for log anomaly detectors.
    """

    def __init__(self, configs: Dict[str, Any]):
        self.window_size = configs.get("window_size", 180)
        self.step_size = configs.get("step_size", 180)

    @abstractmethod
    def detect(self, logs: List[TemplateInfo]) -> List[AbnormalLog]:
        """
        Detects anomalies in a list of logs.
        Args:
            logs: A list of Log objects, typically with templates populated.
        Returns:
            A list of Anomaly objects.
        """

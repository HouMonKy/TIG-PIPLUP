from collections import defaultdict
from typing import List
from src.anomaly_detection.anomaly_base.data_models import Tag, AnomalyEvent, TemplateInfo


class Evaluator:
    def __init__(self, start_time, end_time, window_size):
        self.start_time = int(start_time)
        self.end_time = int(end_time)
        self.window_size = window_size
        # 修正时间范围计算：确保包含所有可能的窗口
        start_window = self.start_time - self.start_time % window_size
        end_window = self.end_time - self.end_time % window_size + window_size
        self.time_range = list(range(start_window, end_window, window_size))
        self.time_range_set = set(self.time_range)  # 使用set提高查找效率

    def compute_confusion_matrix(self, pred_ts, label_ts):
        labels = set(label_ts)

        true_positive = set()
        false_positive = set()
        false_negative = set()

        # 超出时间范围不计入
        for pred in pred_ts:
            if pred in labels:
                true_positive.add(pred)
            elif pred in self.time_range_set:
                false_positive.add(pred)

        for label in labels:
            if label not in true_positive and label in self.time_range_set:
                false_negative.add(label)
        
        # 计算TN：总窗口数 - (TP + FP + FN)，确保不为负
        tn = len(self.time_range) - len(true_positive) - len(false_positive) - len(false_negative)
        tn = max(0, tn)  # 确保TN不为负数
        
        return len(true_positive), len(false_positive), tn, len(false_negative)

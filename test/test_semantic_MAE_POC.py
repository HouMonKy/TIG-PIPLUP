#!/usr/bin/python3.7
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2012-2020. All rights reserved.
import os
import unittest
from src.anomaly_detection.semantic_anomaly.semantic_anomaly import SemanticAnomalyDetector
from src.common.global_config import config

def read_txt_to_list(txt_path):
    try:
        with open(txt_path, 'r', encoding='utf-8') as f:
            return [line.strip() for line in f if line.strip()]
    except Exception as e:
        print(f"读取文件失败：{txt_path}, 错误：{e}")
        return []


class MAEPocBaseTest:
    """测试 指定方案 在 MAE-POC 数据集上的异常检测表现。"""
    @classmethod
    def setUpClass(cls):
        """在类级别初始化一次检测器，避免重复创建。"""
        cls.detector = SemanticAnomalyDetector(
            case="MAE",
            device=cls.DEVICE,
            print_abn_logs=True
        )
        cls.dataset_root = config.get_dataset_root()
        cls.detect_func = getattr(cls.detector, cls.DETECT_METHOD)

    def _get_test_file_path(self, sub_dir, file_name):
        """构建测试文件的完整路径。"""
        return os.path.join(self.dataset_root, "CN", sub_dir, file_name)

    def _run_detection(self, sub_dir, file_name, expected_type):
        """
        执行一次检测测试的通用方法。
        :param sub_dir: 子目录名，如 "MAE_POC"
        :param file_name: 文件名，如 "tp.txt" 或 "fp.txt"
        :param expected_type: 预期类型："all_abnormal"（全异常）或 "no_abnormal"（无异常）
        """
        txt_path = self._get_test_file_path(sub_dir, file_name)
        raw_logs = read_txt_to_list(txt_path)

        if not raw_logs:
            self.fail(f"输入文件为空：{txt_path}")

        abn_logs = self.detect_func(raw_logs)

        # 根据预期类型判断期望数量
        if expected_type == "all_abnormal":
            expected_count = len(raw_logs)
        elif expected_type == "no_abnormal":
            expected_count = 0
        else:
            raise ValueError(f"不支持的 expected_type: {expected_type}")

        self.assertEqual(
            expected_count,
            len(abn_logs),
            msg=f"{self.DEVICE} 检测结果错误：预期 {expected_count}，实际 {len(abn_logs)}\n文件：{txt_path}"
        )

class TestMAEPocCPU(MAEPocBaseTest, unittest.TestCase):
    DEVICE = "CPU"
    DETECT_METHOD = "detect_func"

    def test_tp(self):
        """测试 'tp.txt'：应检测到所有日志均为异常（全异常）。"""
        self._run_detection("MAE_POC", "tp.txt", "all_abnormal")

    def test_fp(self):
        """测试 'fp.txt'：应检测不到任何异常日志（无异常）。"""
        self._run_detection("MAE_POC", "fp.txt", "no_abnormal")


class TestMAEPocNPU(MAEPocBaseTest, unittest.TestCase):
    DEVICE = "NPU"
    DETECT_METHOD = "detect_func"

    def test_tp(self):
        """测试 'tp.txt'：应检测到所有日志均为异常（全异常）。"""
        self._run_detection("MAE_POC", "tp.txt", "all_abnormal")

    def test_fp(self):
        """测试 'fp.txt'：应检测不到任何异常日志（无异常）。"""
        self._run_detection("MAE_POC", "fp.txt", "no_abnormal")

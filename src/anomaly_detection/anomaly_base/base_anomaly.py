#!/usr/bin/python3.7
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2012-2020. All rights reserved.
from abc import ABC, abstractmethod


class BaseAnomalyDetector(ABC):
    def __init__(self, start_time, end_time, window_size):
        self.start_time = start_time
        self.end_time = end_time
        self.window_size = window_size

    @abstractmethod
    def detect(self, data):
        pass



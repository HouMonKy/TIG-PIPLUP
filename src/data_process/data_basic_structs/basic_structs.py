#!/usr/bin/python3.7
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2012-2020. All rights reserved.
from collections import OrderedDict


class LRUCache:
    def __init__(self, capacity: int):
        self.cache = OrderedDict()
        self.capacity = capacity  # 最大缓存容量

    def get(self, key):
        value = self.cache.get(key, None)
        if value is not None:
            self.cache.move_to_end(key)  # 标记为最近使用
        return value

    def put(self, key, value):
        if key in self.cache:
            self.cache.move_to_end(key)
        self.cache[key] = value
        if len(self.cache) > self.capacity:
            self.cache.popitem(last=False)  # 移除最久未使用的项

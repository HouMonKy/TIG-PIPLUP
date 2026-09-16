#!/usr/bin/python3.7
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2012-2020. All rights reserved.
import os


class GlobalConfig:
    _instance = None
    _config_store = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __setattr__(self, name, value):
        if name.startswith("_"):  # 保护内部属性
            super().__setattr__(name, value)
        else:
            self._config_store[name] = value

    def __getattr__(self, name):
        if name in self._config_store:
            return self._config_store[name]
        raise AttributeError(f"配置项 '{name}' 不存在")

    def __delattr__(self, name):
        if name in self._config_store:
            del self._config_store[name]
        else:
            raise AttributeError(f"配置项 '{name}' 不存在")

    def __contains__(self, name):
        return name in self._config_store

    def get(self, name, default=None):
        return self._config_store.get(name, default)

    def get_all(self):
        return self._config_store.copy()

    @staticmethod
    def get_project_root() -> str:
        """config和dataset放在src外，方便引用"""
        current_file_path = os.path.abspath(__file__)
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(current_file_path)))
        return project_root

    @staticmethod
    def get_dataset_root() -> str:
        """config和dataset放在src外，方便引用"""
        current_file_path = os.path.abspath(__file__)
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(current_file_path)))
        dataset_root = os.path.join(project_root, "dataset")
        return dataset_root


# 全局实例初始化
config = GlobalConfig()

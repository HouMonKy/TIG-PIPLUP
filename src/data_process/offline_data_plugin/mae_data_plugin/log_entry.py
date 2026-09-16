#!/usr/bin/python3.7
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2012-2020. All rights reserved.
class LogEntry:
    _seen = set()
    _entries = []

    def __init__(self, case=None, module=None, file_name=None, file_path=None, line_content=None):
        self.case = case
        self.module = module
        self.file_name = file_name
        self.file_path = file_path
        self.line_content = line_content

    def __str__(self):
        return (f"LogEntry {{"
                f"CASE: {self.case}, "
                f"module: {self.module}, "
                f"fileName: {self.file_name}, "
                f"filePath: {self.file_path}, "
                f"line: {self.line_content}"
                f"}}")

    @classmethod
    def add_entry(cls, case, module, file_name, file_path, line_content):
        row_key = (case, module, file_name, file_path, line_content)

        # 如果该行未处理过
        if row_key not in cls._seen:
            cls._seen.add(row_key)  # 标记为已处理
            entry = LogEntry(case, module, file_name, file_path, line_content)
            cls._entries.append(entry)
            return entry
        return None

    @classmethod
    def get_entries(cls):
        return cls._entries

    @classmethod
    def clear_entries(cls):
        cls._entries.clear()
        cls._seen.clear()


class EnhancedLogEntry(LogEntry):
    def __init__(self, case=None, module=None, file_name=None, file_path=None, line_content=None, template_id=None, template=None,
                 param_list=None, param_pos=None, timestamp=None):  # 新增 timestamp 参数
        super().__init__(case, module, file_name, file_path, line_content)
        self.template_id = template_id
        self.template = template
        self.param_list = param_list
        self.param_pos = param_pos
        self.timestamp = timestamp

    def __str__(self):
        base_str = super().__str__()
        return base_str[
               :-1] + (f", template_id: {self.template_id}, template: {self.template}, param_list: {self.param_list}, "
                       f"param_pos: {self.param_pos}, timestamp: {self.timestamp}}}")

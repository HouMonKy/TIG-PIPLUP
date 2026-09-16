#!/usr/bin/python3.7
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2012-2020. All rights reserved.
import json
import os
from typing import List

from src.common.global_config import GlobalConfig


def group_by_template_id(window_arr) -> dict[int, List]:
    template_map = {}
    for window in window_arr:
        if window.templateId not in template_map:
            one_template_windows = [window]
            template_map[window.templateId] = one_template_windows
        else:
            template_map[window.templateId].append(window)
    return template_map


def abs_path(path):
    config = GlobalConfig()
    root_dir = config.get_project_root()
    return os.path.join(root_dir, path)


def get_content_as_tokens(content: str):
    content = content.strip()
    extra_delimiters = [","]
    for delimiter in extra_delimiters:
        content = content.replace(delimiter, " ")
    content_tokens = content.split()
    return content_tokens

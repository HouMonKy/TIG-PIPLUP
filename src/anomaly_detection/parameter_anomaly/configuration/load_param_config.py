#!/usr/bin/python3.7
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2012-2020. All rights reserved.

import os
import json
from dataclasses import dataclass
from typing import Optional
from src.common.global_config import GlobalConfig


@dataclass
class ParamInfo:
    template_id: Optional[str]
    template_pos: int
    # 当同一个位置出现多个参数，例如：'<NUM>-<HEX0x>',需要改进为 template_param: list
    template_param: str

    def to_dict(self):
        return {"template_id": self.template_id, "template_pos": self.template_pos, "template_param": self.template_param}


def load_param_config(product='MAE'):
    """
    根据product参数加载对应的配置文件，提取template_id和param_pos信息
    :param product: 产品类型，支持 'MAE' 或 'DME'
    :return: 包含template_id到param_pos列表映射的字典
    {
        "eds":{
            template_id_1: pos:[param_pos_0,param_pos_1,param_pos_2],
            template_id_2: [param_pos_0,param_pos_1,param_pos_2],
        },
        "ccdb":{
            template_id_1: pos:[param_pos_0,param_pos_1,param_pos_2],
            template_id_2: [param_pos_0,param_pos_1,param_pos_2],
        },
    }
    """
    config = GlobalConfig()

    # 根据产品类型选择对应的配置文件
    if product.upper() == 'MAE':
        filename = 'config_build_mae.json'
    elif product.upper() == 'DME':
        filename = 'config_build_dme.json'
    else:
        raise ValueError("Invalid product parameter, must be 'MAE' or 'DME'")

    # 构造配置文件路径
    json_path = os.path.join(
        config.get_project_root(),
        'src', 'anomaly_detection', 'parameter_anomaly',
        'configuration', filename
    )

    # 读取并解析JSON文件
    with open(json_path, 'r') as f:
        config_data = json.load(f)

    # 数据处理：提取template_id和对应的param_pos列表
    template_param = {}
    for entry in config_data:
        module = entry['module']
        if module not in template_param:
            template_param[module] = {}
        template_id = entry['template_id']
        param_positions = [param['param_pos'] for param in entry['params']]
        template_param[module][template_id] = param_positions

    return template_param

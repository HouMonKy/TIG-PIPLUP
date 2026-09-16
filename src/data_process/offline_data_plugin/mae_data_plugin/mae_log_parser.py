import re
import json
from datetime import datetime
import os
from src.common.global_config import GlobalConfig

config = GlobalConfig()


class MaeLogParser:
    def __init__(self):
        """
        初始化日志解析器，加载配置文件

        参数:
            config_path (str): 配置文件路径
        """
        config_path = os.path.join(config.get_project_root(), 'config', 'format_mae.json')
        self.config = self.load_config(config_path)
        self.parsers = self.build_parsers()

    def load_config(self, config_path):
        """从JSON文件加载配置"""
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"配置文件不存在: {config_path}")

        with open(config_path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def build_parsers(self):
        """为所有模块构建正则表达式解析器"""
        parsers = {}

        for module, module_config in self.config.items():
            module_parsers = []

            for fmt in module_config.get("log_format", []):
                field_names = []
                regex_parts = ['^']  # 开始位置
                if module == "eor":
                    # 为 EOR 模块构建特定的正则表达式
                    pattern = r'^<(\d+)>(\w{3}) (\d{1,2}) (\d{4}) (\d{2}:\d{2}:\d{2}\+\d{2}:\d{2}) ([^ ]+) (.*)$'
                    field_names = ['id', 'Month', 'Date', 'Year', 'Hour', 'Device', 'Content']

                    module_parsers.append({
                        'pattern': re.compile(pattern),
                        'fields': field_names
                    })
                    continue  # 跳过常规处理
                if module == "sel":
                    # 允许字段包含空格（使用 [^,]+ 匹配非逗号字符）
                    pattern = r'^([^,]+),([^,]+),([^,]+),([^,]+),([^,]+),(.*)$'
                    field_names = ['Id', 'Time', 'Level', 'Event_code', 'Asserted_or_deasserted', 'Content']
                    module_parsers.append({
                        'pattern': re.compile(pattern),
                        'fields': field_names
                    })
                    continue  # 跳过常规处理
                # 分割格式字符串为字段和固定文本
                tokens = re.split(r'(<[^>]+>)', fmt)

                for token in tokens:
                    if not token:
                        continue

                    if token.startswith('<') and token.endswith('>'):
                        # 处理字段
                        field_name = token[1:-1]
                        field_names.append(field_name)

                        # 特殊处理id字段
                        if field_name == 'id':
                            regex_parts.append(r'<(\d+)>')  # 只匹配数字ID
                        elif field_name in ['Content', 'content']:
                            regex_parts.append('(.*)')  # 贪婪匹配剩余内容
                        else:
                            # 使用非贪婪匹配防止过度匹配
                            regex_parts.append(r'(\S+)')
                    else:
                        # 转义固定文本中的特殊字符
                        regex_parts.append(re.escape(token))

                regex_parts.append('$')  # 结束位置
                pattern = ''.join(regex_parts)

                module_parsers.append({
                    'pattern': re.compile(pattern),
                    'fields': field_names
                })

            parsers[module] = module_parsers

        return parsers

    def parse(self, module, log_line):
        log_line = log_line.strip()
        module_parsers = self.parsers.get(module.lower())

        if not module_parsers:
            raise ValueError(f"未找到模块配置: {module}")

        # 按优先级尝试所有解析器
        for parser in module_parsers:
            match = parser['pattern'].match(log_line)
            if match:
                result = {}
                for i, field in enumerate(parser['fields']):
                    result[field] = match.group(i + 1).strip()

                # 特殊字段处理和时间转换
                result = self.process_fields(module, result)
                return result

        # 所有格式都不匹配
        return None

    def process_fields(self, module, fields):
        """
        处理特殊字段和时间拼接

        参数:
            module (str): 模块名称
            fields (dict): 解析出的字段

        返回:
            dict: 处理后的字段字典
        """
        # 获取模块配置
        module_config = self.config.get(module, {})
        time_format = ""
        # 1. 时间字段拼接 (针对route/eor等模块)
        if module in ['route', 'eor']:
            if 'Month' in fields and 'Date' in fields and 'Year' in fields and 'Hour' in fields:
                time_str = f"{fields['Month']} {fields['Date']} {fields['Year']} {fields['Hour']}"
                fields['Time'] = time_str
                time_format = "%b %d %Y %H:%M:%S%z"

        if module in ['messages']:
            if 'Time' in fields:
                time_format = "%Y-%m-%dT%H:%M:%S.%f%z"
                tmp_pattern = r'^([^|]+)\|([^|]+)\|([^|]+)\|([^|]+)$'
                if re.match(tmp_pattern, fields['Time']):
                    time_str, level, process, module = re.match(tmp_pattern, fields['Time']).groups()
                    fields['Time'] = time_str
            elif 'Month' in fields and 'Date' in fields and 'Hour' in fields:
                year = datetime.now().year
                time_str = f"{fields['Month']} {fields['Date']} {year} {fields['Hour']}"
                fields['Time'] = time_str
                time_format = "%b %d %Y %H:%M:%S"

        if module in ['sel']:
            time_format = '%Y-%m-%d %H:%M:%S'

        # 2. 统一内容字段名称
        if 'content' in fields:
            fields['Content'] = fields.pop('content')

        # 3. 时间字段转换为时间戳
        if 'Time' in fields and time_format:
            # 使用统一的格式解析时间
            timestamp = self.convert_to_timestamp(fields['Time'], time_format)
            if timestamp is not None:
                # 添加统一的时间戳字段
                fields['timestamp'] = timestamp

        return fields

    def convert_to_timestamp(self, time_str, time_format):
        try:
            if time_str.startswith('1970'):
                time_str = time_str.replace('1970', datetime.now().strftime('%Y'))
            # 尝试解析时间字符串
            dt = datetime.strptime(time_str, time_format)
            return int(dt.timestamp()) * 1000
        except (ValueError, TypeError, Exception) as e:
            print(time_str, time_format)
            print("转换失败！！！", e)
            # 转换失败时返回None
            return None
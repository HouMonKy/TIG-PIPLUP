from datetime import datetime
import regex as re

from enum import Enum


class TimeFormatType(Enum):
    # 日志格式理应可枚举
    YEAR_MONTH_DAY_HOUR_TZ = 1
    MONTH_DAY_HOUR = 2
    CLASSIC = 3


class LogFormatParser:

    def __init__(self, log_format, indir='./'):
        self.path = indir
        self.logName = None
        self.df_log = None
        self.log_format = log_format
        self.log_file = None
        self.format_regex = {}

    def format(self, raw_log, module) -> (int, str):
        return self.load_data(module, raw_log)

    
    def parse_log_time_for_message(self, log_time, time_formats):
        for fmt in time_formats:
            try:
                return datetime.strptime(log_time, fmt)  # 尝试解析时间
            except ValueError:
                continue  # 如果失败，尝试下一种格式
        print(f"Error: Unsupported log_time format -> {log_time}")  # 记录无法解析的时间
        return None  # 如果都失败，则返回 None

    def generate_logformat_regex(self, logformat):
        """ Function to generate regular expression to split log messages
        """
        headers = []
        splitters = re.split(r'(<[^<>]+>)', logformat)
        regex = ''
        count = len(splitters)
        for k in range(count):
            if k % 2 == 0:
                splitter = re.sub(' +', '\\\s+', splitters[k])
                regex += splitter
            else:
                if k < count - 1 and (splitters[k + 1].strip() == '' and splitters[k + 1] != ''):
                    header = splitters[k].strip('<').strip('>')
                    regex += '(?P<%s>\S+)' % header
                    headers.append(header)
                else:
                    header = splitters[k].strip('<').strip('>')
                    regex += '(?P<%s>.*?)' % header
                    headers.append(header)
        regex = re.compile('^' + regex + '$')
        return headers, regex

    @staticmethod
    def extract_content(log, logformat_regex_list):
        for logformat_regex in logformat_regex_list:
            regex = logformat_regex[1]
            match = regex.search(log.strip())
            if (not match) or (match.group() == ''):  # 新增无效匹配的判断，匹配不上或者没有匹配均跳过
                continue
            content = match.groupdict().get('Content', None)
            return content
        return None

    def load_content(self, module: str, raw_log: str) -> (int, str):
        format_regex_list = []
        if module in self.format_regex:
            format_regex_list = self.format_regex[module]
        else:
            for format_item in self.log_format[module]['log_format']:
                headers, regex = self.generate_logformat_regex(format_item)
                format_regex_list.append((headers, regex))
            self.format_regex[module] = format_regex_list
        return self.extract_content(raw_log, format_regex_list)

    def log_to_dataframe(self, module, log, logformat_regex_list) -> (int, str):
        """
        将日志行解析为时间戳和日志内容

        时间戳解析有两种情况：
        1. 单一时间戳字符串（time_stamp）：形如 "2025-08-01 12:59:59.184"
            - 使用预定义的 time_format 进行解析（可能是单个格式或格式列表）

        2. 分离的时间字段（Year, Month, Date, Hour, tz 等）：
            - Year 不存在时默认使用 2026
            - tz（毫秒）不存在时默认使用 0
            - 根据字段存在情况自动判断格式进行组装解析

        返回：(时间戳毫秒或 0, 日志内容)。内容提取语义与 load_content
        一致：格式匹配即提取 Content；时间解析失败不破坏内容提取，
        仅使时间戳为 0（由调用方走时间降级链路）。
        """
        timestamp = 0
        content = None

        for logformat_regex in logformat_regex_list:
            regex = logformat_regex[1]
            match = regex.search(log.strip())

            # 无效匹配则跳过，尝试下一个正则
            if (not match) or (match.group() == ''):
                continue

            groups = match.groupdict()
            content = groups.get('Content', None)

            try:
                # 情况1：存在 time_stamp 字段，直接使用预定义格式解析完整时间戳字符串
                if groups.get('time_stamp', None):
                    timestamp = self._parse_timestamp_string(module, groups['time_stamp'])
                # 情况2：分离的时间字段，需要组装后解析
                else:
                    timestamp = self._parse_timestamp_fields(module, groups)
            except ValueError:
                # 时间解析失败：保留已提取的 Content，时间戳置 0
                timestamp = 0

            # 成功提取内容（无论时间是否解析成功），跳出循环
            break

        # 返回毫秒级时间戳（0 表示时间解析失败）和日志内容
        return int(timestamp * 1000), content

    # 常见时间戳格式列表，用于预定义格式失败时的回退尝试
    COMMON_TIME_FORMATS = [
        # 年-月-日 时:分:秒 系列
        '%Y-%m-%d %H:%M:%S',           # 2025-08-01 12:59:59
        '%Y-%m-%d %H:%M:%S.%f',        # 2025-08-01 12:59:59.184
        '%Y/%m/%d %H:%M:%S',           # 2025/08/01 12:59:59
        '%Y/%m/%d %H:%M:%S.%f',        # 2025/08/01 12:59:59.184
        # 年 月 日 时:分:秒 系列（英文月份缩写）
        '%Y %b %d %H:%M:%S',           # 2025 Jul 22 09:31:02
        '%Y %b %d %H:%M:%S.%f',        # 2025 Jul 22 09:31:02.184
        # 月 日 时:分:秒 系列（需要补充年份）
        '%b %d %H:%M:%S',              # Jul 22 09:31:02
        '%b %d %H:%M:%S.%f',           # Jul 22 09:31:02.184
        # 其他常见格式
        '%d/%b/%Y:%H:%M:%S',           # 22/Jul/2025:09:31:02 (Apache/Nginx 风格)
        '%d/%b/%Y:%H:%M:%S %z',        # 22/Jul/2025:09:31:02 +0800
    ]

    def _try_parse_with_fallback(self, time_str, primary_format=None):
        """
        尝试解析时间戳字符串，失败时回退到常见格式列表

        Args:
            time_str: 待解析的时间戳字符串
            primary_format: 优先尝试的格式（单个或列表）

        Returns:
            解析成功返回 timestamp（浮点秒），失败返回 0
        """
        time_str = time_str.strip()

        # 收集所有需要尝试的格式
        formats_to_try = []

        # 优先使用预定义格式
        if primary_format:
            if isinstance(primary_format, list):
                formats_to_try.extend(primary_format)
            else:
                formats_to_try.append(primary_format)

        # 添加常见格式作为回退
        formats_to_try.extend(self.COMMON_TIME_FORMATS)

        # 依次尝试各种格式
        for fmt in formats_to_try:
            try:
                dt = datetime.strptime(time_str, fmt)
                return dt.timestamp()
            except ValueError:
                continue

        # 所有格式都失败，返回 0
        return 0

    def _parse_timestamp_string(self, module, time_str):
        """
        解析完整的时间戳字符串（情况1）

        优先使用预定义的 time_format 进行解析，失败时尝试常见格式
        """
        time_format = self.log_format[module].get('time_format', None)
        # 截取前26个字符，避免超长字符串导致解析问题
        time_str = time_str.strip()[:26]

        timestamp = self._try_parse_with_fallback(time_str, time_format)
        return timestamp

    def _parse_timestamp_fields(self, module, groups):
        """
        解析分离的时间字段（情况2）

        从正则匹配结果中提取 Time, Year, Month, Date, Hour, tz 等字段
        - Time 字段：如果存在，直接解析整个时间字符串
        - Year 默认值：2026
        - tz（毫秒）默认值：0

        解析失败时会尝试多种常见格式
        """
        # 获取各时间字段，设置默认值
        year = groups.get('Year', None) or '2026'
        tz = groups.get('tz', None) or '0'
        month = groups.get('Month', None)
        date = groups.get('Date', None)
        hour = groups.get('Hour', None)
        time_val = groups.get('Time', None)  # 支持 Time 字段

        # 优先使用 Time 字段（如果存在）
        if time_val:
            # Time 字段包含完整的时间字符串
            time_str = time_val.strip()
            timestamp = self._try_parse_with_fallback(time_str, None)
            if timestamp == 0:
                return 0
        # 根据 Hour 字段是否存在判断解析方式
        elif hour:
            # 有 Hour 字段，说明是分离的年月日时分秒格式
            # 组装时间字符串，格式如：'2026 Jul 22 09:31:02'
            time_str = f"{year} {month} {date} {hour}"
            time_str = time_str.strip()

            # 尝试解析，失败时自动回退到常见格式
            timestamp = self._try_parse_with_fallback(time_str, '%Y %b %d %H:%M:%S')
            if timestamp == 0:
                return 0
        else:
            # 无 Hour 字段，使用配置中预定义的 time 字段列表组装
            time_parts = []
            for time_header in self.log_format[module]['time']:
                part = groups.get(time_header, None)
                if part:
                    time_parts.append(part)
            time_str = ' '.join(time_parts)
            time_format = self.log_format[module].get('time_format', None)

            # 尝试解析，失败时自动回退到常见格式
            timestamp = self._try_parse_with_fallback(time_str.strip(), time_format)
            if timestamp == 0:
                return 0

        # 添加毫秒部分：将 tz 转换为小数秒
        # 例如：tz='184' → 0.184 秒
        try:
            ms = float('0.' + tz)
            timestamp += ms
        except ValueError:
            # tz 无效时忽略毫秒部分，仅保留整秒
            pass

        return timestamp

    def refresh_format_regex(self, module):
        if module not in self.log_format:
            print(f"Warning: module {module} if not in log_fromat!")
        else:
            format_regex_list = []
            for format_item in self.log_format[module]['log_format']:
                headers, regex = self.generate_logformat_regex(format_item)
                format_regex_list.append((headers, regex))
            self.format_regex[module] = format_regex_list

    def load_data(self, module: str, raw_log: str) -> (int, str):
        format_regex_list = []
        if module in self.format_regex:
            format_regex_list = self.format_regex[module]
        else:
            for format_item in self.log_format[module]['log_format']:
                headers, regex = self.generate_logformat_regex(format_item)
                format_regex_list.append((headers, regex))
            self.format_regex[module] = format_regex_list
        time_result, content = self.log_to_dataframe(module, raw_log, format_regex_list)
        if time_result == 0:
            # 时间解析失败：不丢弃已提取的 Content（与离线 load_content
            # 语义一致），时间由调用方走降级链路（正则/历史兜底）
            return None, content
        return time_result, content
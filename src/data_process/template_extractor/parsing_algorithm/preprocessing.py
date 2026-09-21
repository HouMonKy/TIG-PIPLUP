import re



weekday_abb = ['Mon', 'Tue', 'Wed', "Thu", 'Fri', 'Sat', 'Sun']
weekday = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
month_abb = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
months = ["January", "February", "March", "April",
    "May", "June", "July", "August",
    "September", "October", "November", "December"]

regex_match = {
    'IPv4_port': r'(/|)(\d+\.){3}\d+(:\d+)?',
    'host_port': r'([\w-]+\.)+[\w-]+\:\d+',
    'package_host': r'([\w-]+\.){2,}[\w-]+(\$[\w-]+)*(\@[\w-]+)?',
    'Mac_address': r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$',
    'IPv6': r'(([0-9a-fA-F]{1,4}:){7}([0-9a-fA-F]{1,4}|:)|(([0-9a-fA-F]{1,4}:){1,7}|:):((:[0-9a-fA-F]{1,4}){1,7}|:))',
    'path': r'(/|)(([\w.-]+|\<\*\>)/)+([\w.-]+|\<\*\>)',
    'size': r'\b\d+\.?\d*\s?([KGTMkgtm]?(B|b)|([KGTMkgtm]))\b',
    'duration': r'\b\<?\d+\s?(sec|s|ms)\b',
    'block': r'blk\_\-?\d+',
    'numerical': r'\b(\-?\+?\d+\.?\d*)\b|\b0[Xx][a-fA-F\d]+\b|\b[a-fA-F\d]{4,}\b',
    'time': r'\b\d{2}:\d{2}(:\d{2}|:\d{2},\d+)?\b',
    'date': r'\b(\d{4}-\d{2}-\d{2})|\d{4}/\d{2}/\d{2}\b',
    'url': r'\bhttps?:\/\/(www\.)?[a-zA-Z0-9-]+(\.[a-zA-Z]{2,})+(:[0-9]{1,5})?(\/[^\s]*)?\b',
    'weekday_months': r'\b(%s)\b' % '|'.join(weekday_abb+weekday+month_abb+months),
    # CN 新增模式：
    # 1. 日期时间戳：匹配 20250719_154514+0800_1753202726 这种YYYYMMDD_HHMMSS+ (可选)加号及数字 + (可选)下划线及数字格式
    'timestamp_v2': r'\d{8}_\d{6}(\+\d+)?(_\d+)?\b',
    # 2. 状态对偶词：彻底解决 Asserted/Deasserted 导致的匹配失败
    'status_pair': r'\b(Asserted|Deasserted)\b',
    # 3. 序列号：匹配类似 SN:2102312HGYHVK... 的结构
    'serial_num': r'(?<=[S|B]N:)[A-Z0-9]+',
    # 4 CID -Key=Value
    'cid_kv_pairs': r'CID=0[xX][0-9a-fA-F]+(-[a-zA-Z]+=\w+)*',
    # DME 新增模式
    # 5. 日志级别：匹配日志中的日志级别词
    'log_level': r'\b(WARN|ERR|EMER|INFO|DBG)\b',
    # 6. 完整日期时间：匹配 YYYY-MM-DD HH:MM:SS 格式
    # 作用目标：[2026-02-04 11:39:58]
    # 工程逻辑：现有 'date' 和 'time' 是分离的，整合匹配可直接屏蔽整个 <Time> 字段，提升预处理效率。
    'datetime_full': r'\b\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2}\b',

    # 7. 内核时间戳/偏移量：匹配方括号内（允许前导空格）的浮点数
    # 作用目标：[ 4247.652837] 中的 " 4247.652837"
    # 工程逻辑：使用 (?<=\[) 和 (?=\]) 断言确保仅匹配方括号内的数据，防止误替换 Content 中的常规浮点数。
    'kernel_uptime': r'(?<=\[)\s*\d+\.\d+(?=\])',

    # 8. 13位长整型时间戳：匹配方括号内的13位数字
    # 作用目标：[1500001430000] 中的 "1500001430000"
    # 工程逻辑：限定在方括号内匹配13位纯数字，防止与常规 numerical 规则冲突。
    'epoch_ms_13d': r'(?<=\[)\d{13}(?=\])',

    # 9. 动态代码行号：匹配逗号与右方括号之间的数字
    # 作用目标：[cluster_op_view.c:procModifyVNodeViewInfo,178] 中的 "178"
    # 工程逻辑：代码变动会导致行号漂移，通过 (?<=,) 断言左侧必须为逗号，(?=\]) 断言右侧必须为右括号，实现精准捕获。
    'code_line_num': r'(?<=,)\d+(?=\])',

    # 10. 进程与线程标识符：匹配 pid 和 tid 键值对
    # 作用目标：[app_data,pid:53912,tid:53969] 中的 "pid:53912,tid:53969"
    # 工程逻辑：并发执行时 PID/TID 呈完全随机分布。匹配键值对结构可将该部分整体抽象为单一 Token。
    'pid_tid_pair': r'pid:\d+,tid:\d+',
    # 11. 小括号封装的序列号：匹配 sn(XXX) 内部的数据
    'sn_parentheses': r'(?<=sn\()[A-Za-z0-9]+(?=\))',

    # 12. 动态步骤描述：匹配 Step (XXX) 内部的文本
    'step_desc': r'(?<=Step \()[^)]+(?=\))',

    # 13. Vcontroller 配置项名称：匹配 Vcontroller: 后的纯英文字母与下划线变量
    'vcontroller_metric': r'(?<=Vcontroller:\s)[A-Za-z_]+',

    # 14. 子模块初始化描述：匹配 init module 与 begin/end 之间的动宾短语
    'fs_init_action': r'(?<=init module\s).*?(?=\s(?:begin|end\b))',
}

regex_map = {
    'path': 'PATH_TAG',
    'url': 'URL_TAG',
    'IPv4_port': 'IPv4_TAG',
    'package_host': 'PACKAGE_HOST_TAG',
    'Mac_address': 'MAC_TAG',
    'host_port': 'HOST_TAG',
    'IPv6': 'IPv6_TAG',
    'size': 'SIZE_TAG',
    'numerical': 'NUM_TAG',
    'duration': 'DUR_TAG',
    'block': 'BLK_TAG',
    'time': 'TIME_TAG',
    'date': 'DATE_TAG',
    'weekday_months': 'WM_TAG',
    'timestamp_v2': 'TIME_TAG_lb',
    'status_pair': 'STATUS_TAG_lb',
    'serial_num': 'SERIAL_TAG_lb',
    'cid_kv_pairs': 'CID_TAG_lb'
}

# 原版
# sequence = ['url', 'IPv4_port', 'host_port', 'package_host', 'IPv6', 'Mac_address', 'time', 'path', 'block', 'date', 'duration', 'size', 'numerical', 'weekday_months']

# CN版
# sequence = ['timestamp_v2', 'status_pair','serial_num', 'cid_kv_pairs', 'url', 'IPv4_port', 'host_port', 'package_host', 'IPv6', 'Mac_address', 'time',  'block', 'date',  'duration', 'size', 'numerical', 'weekday_months']

#DME版
sequence = ['datetime_full',  'kernel_uptime', 'epoch_ms_13d', 'log_level',  'code_line_num', 'pid_tid_pair' , 'sn_parentheses', 'step_desc', 'vcontroller_metric', 'fs_init_action', 'url', 'IPv4_port', 'host_port', 'package_host', 'IPv6', 'Mac_address', 'time', 'path', 'block', 'date', 'duration', 'size', 'numerical', 'weekday_months']

def preprocess(log, use_keys=False, estimation_stage=False, use_sequence=sequence, print=False):
    matched_types = []
    new_log = log
    

    if estimation_stage:
        if print: print('原始日志\n', new_log)
        if use_keys:
            # Replace the ones with symbols
            for key in use_sequence:
                new_log = re.sub(regex_match[key], regex_map[key], new_log)
                if new_log != log: 
                    matched_types.append(key)
                    log = new_log
        else:
            # Combine regex patterns into a single pattern
            for key in use_sequence:
                new_log = re.sub(regex_match[key], '<*>', new_log)
                if new_log != log: 
                    matched_types.append(key)
                    log = new_log
        # Eliminate repetitive <*>s
        new_log = re.sub(r'(\<\*\>\s?)+\<\*\>', '<*>', new_log)
        if print:print('预处理后\n', new_log)
        return new_log, sequence, matched_types
    else:
        if print:print('原始日志\n', new_log)
        if use_keys:
            # Replace the ones with symbols
            for key in use_sequence:
                new_log = re.sub(regex_match[key], regex_map[key], new_log)
        else:
            # Combine regex patterns into a single pattern
            for key in use_sequence:
                new_log = re.sub(regex_match[key], '<*>', new_log)
        # Eliminate repetitive <*>s
        new_log = re.sub(r'(\<\*\>\s?)+\<\*\>', '<*>', new_log)
        if print:print('预处理后\n', new_log)
        return new_log

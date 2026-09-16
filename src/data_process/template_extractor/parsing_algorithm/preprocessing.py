import re

# ── 预编译正则：模块加载时一次性编译，避免运行时 re._cache 查找开销 ──
# 所有正则使用非捕获组 (?:...) 替代捕获组 (...)，preprocess 全部替换为 '<*>'
# 不需要捕获内容，非捕获组可减少引擎开销，实测 550k 行提升 6.1%（主序列）/ 11.4%（端到端）

weekday_abb = ['Mon', 'Tue', 'Wed', "Thu", 'Fri', 'Sat', 'Sun']
weekday = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
month_abb = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
months = ["January", "February", "March", "April",
    "May", "June", "July", "August",
    "September", "October", "November", "December"]

# 非捕获组版本，预编译为模块级对象
_compiled = {
    'IPv4_port': re.compile(r'(?:/|)(?:\d+\.){3}\d+(?::\d+)?'),
    'host_port': re.compile(r'(?:[\w-]+\.)+[\w-]+\:\d+'),
    'package_host': re.compile(r'(?:[\w-]+\.){2,}[\w-]+(?:\$[\w-]+)*(?:\@[\w-]+)?'),
    'Mac_address': re.compile(r'^(?:[0-9A-Fa-f]{2}[:-]){5}(?:[0-9A-Fa-f]{2})$'),
    'IPv6': re.compile(r'((?:[0-9a-fA-F]{1,4}:){7}(?:[0-9a-fA-F]{1,4}|:)|((?:[0-9a-fA-F]{1,4}:){1,7}|:):((?::[0-9a-fA-F]{1,4}){1,7}|:))'),
    'path': re.compile(r'(?:/|)((?:[\w.-]+|\<\*\>)/)+([\w.-]+|\<\*\>)'),
    'size': re.compile(r'\b\d+\.?\d*\s?(?:[KGTMkgtm]?(?:B|b)|(?:[KGTMkgtm]))\b'),
    'duration': re.compile(r'\b\<?\d+\s?(?:sec|s|ms)\b'),
    'block': re.compile(r'blk\_\-?\d+'),
    # 修复 \b 导致负号/正号与数字割裂的问题：
    'numerical': re.compile(r'(?:\b|(?<=\W)|^)(?:-?\+?\d+\.?\d*|0[Xx][a-fA-F\d]+)(?:\b|(?=\W)|$)|\b[a-fA-F\d]{4,}\b'),
    'time': re.compile(r'\b\d{2}:\d{2}(?::\d{2}|:\d{2},\d+)?\b'),
    'date': re.compile(r'\b(?:\d{4}-\d{2}-\d{2})|\d{4}/\d{2}/\d{2}\b'),
    'url': re.compile(r'\bhttps?:\/\/(?:www\.)?[a-zA-Z0-9-]+(?:\.[a-zA-Z]{2,})+(?::[0-9]{1,5})?(?:\/[^\s]*)?\b'),
    'weekday_months': re.compile(r'\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|January|February|March|April|May|June|July|August|September|October|November|December)\b'),
    'timestamp_v2': re.compile(r'\d{8}_\d{6}(?:\+\d+)?(?:_\d+)?\b'),
    'status_pair': re.compile(r'\b(?:Asserted|Deasserted)\b'),
    'serial_num': re.compile(r'(?<=[S|B]N:)[A-Z0-9]+'),
    'cid_kv_pairs': re.compile(r'CID=0[xX][0-9a-fA-F]+(?:-[a-zA-Z]+=\w+)*'),
    'log_level': re.compile(r'\b(?:WARN|ERR|EMER|INFO|DBG)\b'),
    'datetime_full': re.compile(r'\b\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2}\b'),
    'kernel_uptime': re.compile(r'(?<=\[)\s*\d+\.\d+(?=\])'),
    'epoch_ms_13d': re.compile(r'(?<=\[)\d{13}(?=\])'),
    'code_line_num': re.compile(r'(?<=,)\d+(?=\])'),
    'pid_tid_pair': re.compile(r'pid:\d+,tid:\d+'),
    'sn_parentheses': re.compile(r'(?<=sn\()[A-Za-z0-9]+(?=\))'),
    'step_desc': re.compile(r'(?<=Step \()[^)]+(?=\))'),
    'vcontroller_metric': re.compile(r'(?<=Vcontroller:\s)[A-Za-z_]+'),
    'fs_init_action': re.compile(r'(?<=init module\s).*?(?=\s(?:begin|end\b))'),
    'brace_hex_value_pair': re.compile(r'\{[0-9a-fA-F]+:[0-9a-fA-F]+(?:_\d+)+\}'),
    'hex_dump': re.compile(r'D:[0-9a-fA-F]{2}(?: [0-9a-fA-F]{2})+'),
    'lucene_filename': re.compile(r'(?<![\w>])[_.][\w.~-]+'),
    'bracket_compound_id': re.compile(r'(?<=\[)[a-fA-F\d]+(?:_[a-fA-F\d]+)+(?=\])'),
    'kv_compound_id': re.compile(r'(?<=[A-Za-z]:)(?=\w*\d)\w+(?:_\w+)*'),
    # --- P0: identifier group 子正则 ---
    'compound_hex_id': re.compile(r'[A-Za-z][A-Za-z_]*0x[0-9a-fA-F]+(?:_[A-Za-z][A-Za-z_]*0x[0-9a-fA-F]+)*'),
    'xnet_ctx_id': re.compile(r'(?<=\()xnet_\w+:\d+(?=\))'),
    'embedded_hex': re.compile(r'(?<=[A-Za-z_(])0x[0-9a-fA-F]+'),
    'underscore_num_suffix': re.compile(r'(?<=[A-Za-z])_\d+(?=[^A-Za-z0-9]|$)'),
    'device_serial': re.compile(r'\d{4,}[A-Z]{2,}\d{2,}(?:[A-Z]{1,}\d{2,})+'),
    'compound_num_id': re.compile(r'[A-Za-z]\w*\d+(?:_\d+)+(?=[^A-Za-z0-9]|$)'),
}

# 保留原始字符串字典，供外部依赖使用（如 profile 脚本）
regex_match = {k: p.pattern for k, p in _compiled.items()}

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

# ── Group 机制 ──────────────────────────────────────────────
# 将职责相同的子正则归为一个 group。sequence 只引用 group 名，
# matched_types 在 group 级别记录，use_sequence 在 group 级别过滤。
# 这避免了细粒度 key 合并导致的 matched_types 变化问题。
#
# P0: identifier group — 标识符变量化
#   compound_hex_id     : 含 0x hex 的下划线复合标识符（整体匹配优先）
#   compound_num_id     : 含数字的下划线复合标识符
#   device_serial       : 数字与大写字母交替的设备序列号
#   embedded_hex        : 标识符内嵌的独立 0x hex 值（兜底）
#   underscore_num_suffix: 字母_数字 后缀
#   执行顺序：与原 sequence 一致（compound_hex_id → embedded_hex → underscore_num_suffix → device_serial → compound_num_id）
regex_groups = {
    'identifier': [
        'compound_hex_id',
        'embedded_hex',
        'underscore_num_suffix',
        'device_serial',
        'compound_num_id',
    ],
    # P2a: time_date group — 时间/日期变量化
    #   time 和 date 在 estimation 阶段 100% 匹配，合并不影响过滤行为
    'time_date': [
        'time',
        'date',
    ],
    # P2b: network group — 网络地址变量化
    #   IPv4_port (6.3%) 和 package_host (6.8%) 偶发匹配
    #   url/host_port/IPv6/Mac_address 从不匹配（0%）
    #   合并后：IPv4_port 匹配的日志会额外执行 url/IPv6/Mac_address（快速失败）
    #   不匹配的日志整个组被丢弃，与原版一致
    'network': [
        'url',
        'IPv4_port',
        'host_port',
        'package_host',
        'IPv6',
        'Mac_address',
    ],
    # （无 P1 — value group 回退：block+numerical 差异 0.6%，收益/代价比不佳）
}

# 反向映射：子正则名 → 所属 group 名
_sub_to_group = {}
for _gname, _skeys in regex_groups.items():
    for _skey in _skeys:
        _sub_to_group[_skey] = _gname

# ── 清理层预编译正则 ──
_re_consec_wildcard = re.compile(r'(\<\*\>\s?)+\<\*>')
_re_delim_patterns = [re.compile(r'<\*>%s<\*>' % re.escape(d))
                      for d in [':', ',', ';', '|', '=', '.', '-', '+', '#', '@', '$', '%', '&']]
_re_hex_dump_d = re.compile(r'D:(?:<\*> )?[0-9a-fA-F]{2}(?: [0-9a-fA-F]{2}| <\*>)*')
_re_hex_seq = re.compile(r' [0-9a-fA-F]{2}(?!\w)(?: [0-9a-fA-F]{2}(?!\w))+')
_re_cleanup_lucene = re.compile(r'(?<![\w>])[_.][\w.~-]+')
_re_cleanup_bracket = re.compile(r'(?<=\[)[a-fA-F\d]+(?:_[a-fA-F\d]+)+(?=\])')
_re_cleanup_compound_hex = re.compile(r'[A-Za-z][A-Za-z_]*0x[0-9a-fA-F]+(?:_[A-Za-z][A-Za-z_]*0x[0-9a-fA-F]+)*')
_re_cleanup_xnet = re.compile(r'(?<=\()xnet_\w+:\d+(?=\))')
_re_cleanup_embedded_hex = re.compile(r'(?<=[A-Za-z_(])0x[0-9a-fA-F]+')
_re_cleanup_underscore_num = re.compile(r'(?<=[A-Za-z])_\d+(?=[^A-Za-z0-9]|$)')
_re_cleanup_device_serial = re.compile(r'\d{4,}[A-Z]{2,}\d{2,}(?:[A-Z]{1,}\d{2,})+')
_re_cleanup_compound_num = re.compile(r'[A-Za-z]\w*\d+(?:_\d+)+(?=[^A-Za-z0-9]|$)')
_re_log_header = re.compile(r'^(?:\[(?:<\*>)?\]\s*)+')
_re_log_tail = re.compile(r'\[[a-z_]+,<\*>\]\s*$')

#DME版
# 顺序原则：越复杂/越特化的正则越先匹配，避免被通用正则提前截断
# xnet_ctx_id 须在 identifier group 之前（compound_num_id 会匹配 xnet_r0_0_14）
sequence = [
    'datetime_full', 'kernel_uptime', 'epoch_ms_13d', 'log_level',
    'code_line_num', 'pid_tid_pair', 'sn_parentheses', 'step_desc',
    'vcontroller_metric', 'fs_init_action',
    'brace_hex_value_pair', 'hex_dump', 'lucene_filename',
    'bracket_compound_id', 'kv_compound_id',
    'xnet_ctx_id', 'identifier',
    'network',
    'time_date', 'path', 'block', 'duration', 'size', 'numerical',
    'weekday_months',
]


def _apply_key(new_log, key, use_keys):
    """应用单个 sequence key（可能是 group 或独立正则）。"""
    if key in regex_groups:
        # Group: 依次应用组内所有子正则
        for sub_key in regex_groups[key]:
            if use_keys:
                new_log = _compiled[sub_key].sub(regex_map.get(sub_key, '<*>'), new_log)
            else:
                new_log = _compiled[sub_key].sub('<*>', new_log)
    else:
        if use_keys:
            new_log = _compiled[key].sub(regex_map.get(key, '<*>'), new_log)
        else:
            new_log = _compiled[key].sub('<*>', new_log)
    return new_log


def _post_cleanup(new_log):
    """序列正则应用后的兜底清理，estimation 和非 estimation 分支共用。"""
    # Eliminate repetitive <*>s
    new_log = _re_consec_wildcard.sub('<*>', new_log)
    # Reduce <*> separated by single delimiters (e.g., <*>:<*>:<*> → <*>)
    # 与 correct_single_template 的 CV 归约保持一致，避免 logTemplate/pathList 残留 <*>:<*>
    while True:
        prev = new_log
        for pat in _re_delim_patterns:
            new_log = pat.sub('<*>', new_log)
        if prev == new_log:
            break
    # Hex dump 清理：D:后残留的 2 字符 hex 字节（estimation 阶段可能未触发 hex_dump 正则）
    new_log = _re_hex_dump_d.sub('D:<*>', new_log)
    # Hex dump 清理：连续 2 字符 hex 序列
    new_log = _re_hex_seq.sub(' <*>', new_log)
    # Lucene 文件名清理
    new_log = _re_cleanup_lucene.sub('<*>', new_log)
    # 方括号内复合标识符清理
    new_log = _re_cleanup_bracket.sub('<*>', new_log)
    # 含 0x hex 的复合标识符整体清理
    new_log = _re_cleanup_compound_hex.sub('<*>', new_log)
    # xnet 上下文标识符清理
    new_log = _re_cleanup_xnet.sub('<*>', new_log)
    # 标识符内嵌 0x hex 清理
    new_log = _re_cleanup_embedded_hex.sub('<*>', new_log)
    # 下划线数字后缀清理
    new_log = _re_cleanup_underscore_num.sub('_<*>', new_log)
    # 设备序列号清理
    new_log = _re_cleanup_device_serial.sub('<*>', new_log)
    # 含数字的复合标识符清理
    new_log = _re_cleanup_compound_num.sub('<*>', new_log)
    # 日志头清理
    new_log = _re_log_header.sub('', new_log)
    # 日志尾部清理
    new_log = _re_log_tail.sub('', new_log)
    # 兜底清理后再次归约连续 <*>
    new_log = _re_consec_wildcard.sub('<*>', new_log)
    return new_log


def preprocess(log, use_keys=False, estimation_stage=False, use_sequence=sequence, print=False):
    matched_types = []
    new_log = log

    if estimation_stage:
        if print: print('原始日志\n', new_log)
        if use_keys:
            for key in use_sequence:
                new_log = _apply_key(new_log, key, use_keys=True)
                if new_log != log:
                    matched_types.append(key)
                    log = new_log
        else:
            for key in use_sequence:
                new_log = _apply_key(new_log, key, use_keys=False)
                if new_log != log:
                    matched_types.append(key)
                    log = new_log
        new_log = _post_cleanup(new_log)
        if print:print('预处理后\n', new_log)
        return new_log, sequence, matched_types
    else:
        if print:print('原始日志\n', new_log)
        if use_keys:
            for key in use_sequence:
                new_log = _apply_key(new_log, key, use_keys=True)
        else:
            for key in use_sequence:
                new_log = _apply_key(new_log, key, use_keys=False)
        new_log = _post_cleanup(new_log)
        if print:print('预处理后\n', new_log)
        return new_log

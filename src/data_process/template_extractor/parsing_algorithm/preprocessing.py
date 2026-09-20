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
    'size': r'\b\d+(?:\.\d+)?\s?([KGTMkgtm]?(B|b)|([KGTMkgtm]))\b',
    'duration': r'\b\<?\d+\s?(sec|s|ms)\b',
    'block': r'blk\_\-?\d+',
    # 修复 \b 导致负号/正号与数字割裂的问题：
    # 原版 \b(\-?\+?\d+\.?\d*)\b 中，\b 在 (-5) 的 - 和 5 之间形成词边界，
    # 导致引擎从 5 开始匹配，负号 - 被留在 <*> 外面，产生 (-<*>) 碎片化。
    # 修复：将左侧 \b 替换为 (?:\b|(?<=\W)|^)，允许前一个字符是非单词字符（如 (、=、空格）
    # 时也正确匹配带符号的数值；右侧同理替换为 (?:\b|(?=\W)|$)。
    # 句点仅在后跟数字时才消耗（\d+\.?\d* → \d+(?:\.\d+)?）：
    # 原版会把 '17500.objType' 的句点一起吞成 <*>objType，破坏句点作为
    # 分隔符的结构证据（残留字母附着在通配符后）；句尾句点同理被误吞。
    'numerical': r'(?:\b|(?<=\W)|^)(\-?\+?\d+(?:\.\d+)?|0[Xx][a-fA-F\d]+)(?:\b|(?=\W)|$)|\b[a-fA-F\d]{4,}\b',
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

    # 6-9. 已删除的候选掩码（等价承接验证见 TestMaskDeletionLedger）：
    #   - datetime_full（YYYY-MM-DD HH:MM:SS 整体遮蔽）：date + time 依次
    #     匹配产生 "<*> <*>"，随后 _reduce_placeholders 归约为单个 "<*>"，
    #     输出与整体遮蔽完全一致（等价性验证 0 diff）；
    #   - kernel_uptime（方括号内浮点 [ 4247.652837]）：numerical 对浮点
    #     部分产生相同替换；内部前导空格造成的 "[ <*>]" 差异由放宽后的
    #     日志头清理正则（\[\s*<\*>\s*\]）吸收，输出一致；
    #   - epoch_ms_13d（方括号内 13 位整数）：numerical 完全覆盖同一区间，
    #     替换结果完全相同（纯冗余，0 diff）；
    #   - code_line_num（",178]" 行号）：numerical 对同一数字区间产生
    #     相同替换（纯冗余，0 diff）。

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

    # 15. 下划线连接的复合数值 / 17. Key:复合标识符值 / 20. 方括号复合标识符 /
    # 21b. 含 0x hex 复合标识符 / 21c. xnet 上下文标识符 / 22. 下划线数字后缀 /
    # 24. 含数字复合标识符：
    #   以上 7 条业务掩码已删除，由 token 内结构机制承接（见
    #   parsing_algorithm/token_structure.py 与 PIPLUP._structure_aware_merge）：
    #   - key:value/key=value 同字段名 → merge_values（数字组边界修剪）
    #   - 裸复合标识符/数字链 → merge_values（公共前/后缀保留）
    #   - 括号结构 → LCP/LCS 合并（中缀保留，如 (xnet_<*>:0)）
    #   独立地址、时间、十六进制（embedded_hex 保留）等通用识别不受影响。
    # 删除验证见 test/piplup_parsing_test.py::TestMaskDeletionLedger。

    # 16. 大括号内十六进制:数值对：{hex:hex_N_N}
    # 作用目标：{165879a000:165879a000_0_0}{133fe97aee:133fe97aee_0_0}
    # 工程逻辑：numerical 的 \b[a-fA-F\d]{4,}\b 只替换冒号前的 hex 部分，
    #          冒号后的 hex_N_N 因下划线连接而残留。整对替换避免碎片化。
    'brace_hex_value_pair': r'\{[0-9a-fA-F]+:[0-9a-fA-F]+(?:_\d+)+\}',

    # 18. Hex dump 序列：D:后跟多个空格分隔的 2 字符十六进制值
    # 作用目标：Line13 :0 D:01 0f c2 7f f0 0c e2 d8 a0  getRootBufForReadExc
    # 工程逻辑：2 字符 hex（0f/c2/7f 等）不满足 numerical 的 {4,} 门槛，
    #          作为常量残留导致每条 hex dump 日志独占一个簇。
    #          D: 前缀 + 至少 2 个 hex 字节限定为 dump 格式，避免误匹配普通文本。
    'hex_dump': r'D:[0-9a-fA-F]{2}(?: [0-9a-fA-F]{2})+',

    # 19. Lucene 索引文件名/随机标识符：下划线或点开头的复合标识符
    # 作用目标：_1l1.fdm、_1l1.fdt、_je.fdm、_kxl.si、_kxm.fdm、_1l1_Lucene90FieldsIndex-doc_ids_362.tmp、.nfs0000000001601900000013ac、_7VF~XYZFDM
    # 工程逻辑：这类 token 以 _ 或 . 开头，内含字母数字点号下划线波浪号，
    #          numerical 的 \b 无法在 _ 后形成词边界，path 无 / 不匹配，
    #          导致每条日志的文件名残留在 logTemplate 中，seqDist 相似度不够，产生大量单命中簇。
    #          (?<!\w) 确保前一个字符不是单词字符，避免误匹配 pool_key、modid 等字段名中的下划线。
    'lucene_filename': r'(?<![\w>])[_.][\w.~-]+',

    # 20. 方括号内的复合标识符 / 21b. 含 0x 复合标识符 / 21c. xnet 上下文标识符 /
    # 22. 下划线数字后缀 / 24. 含数字复合标识符：
    #   均为已删除的候选掩码（承接关系见上方第 15 条注释与 TestMaskDeletionLedger）。

    # 21. 标识符内嵌的 0x hex 值：RTOBJ_DP0x0_RV0xffff
    # 作用目标：RTOBJ_DP0x0_RV0xffff、RTSETOBJ_DP0x0、DP0x0_RV0x12
    # 工程逻辑：0x hex 值嵌入下划线连接的标识符中，numerical 的 \b 无法在字母后匹配 0x，
    #          导致不同 hex 值的标识符残留，日志分裂为不同簇。
    #          (?<=[A-Za-z_]) 限定 0x 前面必须是字母或下划线（标识符内部），不匹配独立 0xNNNN。
    'embedded_hex': r'(?<=[A-Za-z_(])0x[0-9a-fA-F]+',

    # 21c. xnet 上下文标识符 —— 已删除（生产数据 A/B 评估：零代价，见
    # TestMaskDeletionLedger::test_xnet_ctx_id_deleted）。
    # 多 token 场景由结构机制承接；单 token 场景（st_dict[1]=1 精确匹配）
    # 为已知边界，若生产回归发现该形态成簇异常，恢复本规则即可。

    # 23. 设备序列号：数字与大写字母交替的混合序列号
    # 作用目标：2102312UCT10L8000290、2102313EDH10PA100243
    # 工程逻辑：设备序列号含多组数字和大写字母交替，numerical 无法整体匹配（字母打断数字），
    #          导致序列号残留在模板中，不同设备的日志分裂为不同簇。
    #          \d{4,} 起始限定至少4位数字，[A-Z]{2,} 至少2个大写字母，避免误匹配普通标识符。
    'device_serial': r'\d{4,}[A-Z]{2,}\d{2,}(?:[A-Z]{1,}\d{2,})+',
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
    'cid_kv_pairs': 'CID_TAG_lb',
    'log_level': 'LOG_LEVEL_TAG',
    'pid_tid_pair': 'PID_TID_TAG',
    'sn_parentheses': 'SERIAL_TAG',
    'step_desc': 'STEP_TAG',
    'vcontroller_metric': 'VCONTROLLER_TAG',
    'fs_init_action': 'FS_INIT_ACTION_TAG',
    'brace_hex_value_pair': 'HEX_PAIR_TAG',
    'hex_dump': 'HEX_DUMP_TAG',
    'lucene_filename': 'FILENAME_TAG',
    'embedded_hex': 'HEX_TAG',
    'device_serial': 'DEVICE_SERIAL_TAG',
}

# 原版
# sequence = ['url', 'IPv4_port', 'host_port', 'package_host', 'IPv6', 'Mac_address', 'time', 'path', 'block', 'date', 'duration', 'size', 'numerical', 'weekday_months']

# CN版
# sequence = ['timestamp_v2', 'status_pair','serial_num', 'cid_kv_pairs', 'url', 'IPv4_port', 'host_port', 'package_host', 'IPv6', 'Mac_address', 'time',  'block', 'date',  'duration', 'size', 'numerical', 'weekday_months']

#DME版
# 顺序原则：越复杂/越特化的正则越先匹配，避免被通用正则提前截断
# 已删除（由 token 内结构机制承接）：underscore_composite_num、kv_compound_id、
# compound_num_id、compound_hex_id、bracket_compound_id、underscore_num_suffix、xnet_ctx_id
# 已删除（纯冗余/等价承接，见 regex_match 内 6-9 注释）：datetime_full、
# kernel_uptime、epoch_ms_13d、code_line_num
sequence = ['log_level', 'pid_tid_pair' , 'sn_parentheses', 'step_desc', 'vcontroller_metric', 'fs_init_action', 'brace_hex_value_pair', 'hex_dump', 'lucene_filename', 'embedded_hex', 'device_serial', 'url', 'IPv4_port', 'host_port', 'package_host', 'IPv6', 'Mac_address', 'time', 'path', 'block', 'date', 'duration', 'size', 'numerical', 'weekday_months']

# 兜底清理：与 sequence 中规则一一对应的二次扫描（处理 <*> 交互后的残留）。
# 仅当对应规则在活跃规则集 use_sequence 中时执行——规则被（实验性）关闭时，
# 其兜底扫描同步关闭，避免"关掉主规则、兜底仍在遮蔽"的伪实验。
_CLEANUP_RULES = [
    # (规则名, 已编译正则, 替换)——预编译避免热路径反复查 re 模块内部缓存
    # (规则名, 正则, 替换)
    ('hex_dump', re.compile(r'D:(?:<\*> )?[0-9a-fA-F]{2}(?: [0-9a-fA-F]{2}| <\*>)*'), 'D:<*>'),
    # 连续 2 字符 hex 序列（hex_dump 的补充形态）
    # (?!\w) 防止匹配英文单词片段（如 "DB fa"iled）
    ('hex_dump', re.compile(r' [0-9a-fA-F]{2}(?!\w)(?: [0-9a-fA-F]{2}(?!\w))+'), ' <*>'),
    ('lucene_filename', re.compile(r'(?<![\w>])[_.][\w.~-]+'), '<*>'),
    ('embedded_hex', re.compile(r'(?<=[A-Za-z_(])0x[0-9a-fA-F]+'), '<*>'),
    ('device_serial', re.compile(r'\d{4,}[A-Z]{2,}\d{2,}(?:[A-Z]{1,}\d{2,})+'), '<*>'),
]

# 无条件清理（不与特定规则绑定），模块级预编译：
_HEADER_BRACKETS_RE = re.compile(r'^(?:\[\s*(?:<\*>)?\s*\]\s*)+')
_TAIL_MODULE_RE = re.compile(r'\[[a-z_]+,<\*>\]\s*$')
_RE_CONSEC_WILD = re.compile(r'(?:\<\*\>\s?)+\<\*>')


def _apply_cleanup(new_log, active_rules):
    """规则绑定的兜底清理 + 无条件归约（连续 <*>、日志头/尾清理）。"""
    for rule, pattern, repl in _CLEANUP_RULES:
        if rule in active_rules:
            new_log = pattern.sub(repl, new_log)
    # 无条件清理（不与特定规则绑定）
    # 日志头：去掉开头连续的 [<*>] 块（时间戳/运行时间/pid/日志级别预处理后产生），
    # 这些纯 <*> token 被 seqDist 跳过但计入分母，稀释相似度导致簇分裂。
    # 允许括号内部空白：kernel_uptime 删除后 numerical 不吞 "[ 4247.652837]"
    # 内部的前导空格，放宽为 \[\s*<\*>\s*\] 保持等价输出。
    new_log = _HEADER_BRACKETS_RE.sub('', new_log)
    # 日志尾：去掉末尾的 [module,<*>]（源模块名，format_chaos 会去除，no_format 保留导致分裂）
    new_log = _TAIL_MODULE_RE.sub('', new_log)
    # 兜底清理后再次归约连续 <*>（lucene_filename 等清理可能产生 <*><*> 碎片）
    new_log = _RE_CONSEC_WILD.sub('<*>', new_log)
    return new_log


# 被单个链式分隔符隔开的 <*> 链：<*>(D<*>)+ → <*>，D ∈ 集合
# 与原逐分隔符循环语义等价：一次匹配整条链（含混合分隔符），多轮循环
# 归约到的同一最终形态
_RE_SEP_WILD_CHAIN = re.compile(r'<\*>(?:[,:;|=.\-+#@$%&]<\*>)+')


def _reduce_placeholders(new_log):
    """归约连续 <*> 与被单个分隔符隔开的 <*> 链。"""
    return _RE_SEP_WILD_CHAIN.sub(
        '<*>', _RE_CONSEC_WILD.sub('<*>', new_log))


# 规则名 → 已编译正则的模块级缓存（规则数量有限，缓存有界）
_rule_pattern_cache = {}


def _get_rule_pattern(key):
    pattern = _rule_pattern_cache.get(key)
    if pattern is None:
        pattern = _rule_pattern_cache[key] = re.compile(regex_match[key])
    return pattern


def preprocess(log, use_keys=False, estimation_stage=False, use_sequence=None, print=False):
    # 默认在调用时解析模块全局 sequence（而非定义时绑定），
    # 保证实验性关闭规则（patch preprocessing.sequence）对两个阶段同时生效
    if use_sequence is None:
        use_sequence = sequence
    matched_types = []
    new_log = log

    if estimation_stage:
        if print: print('原始日志\n', new_log)
        # subn 返回替换次数：n>0 即命中，避免每条规则做一次整串相等比较
        if use_keys:
            # Replace the ones with symbols
            for key in use_sequence:
                new_log, n = _get_rule_pattern(key).subn(regex_map[key], new_log)
                if n:
                    matched_types.append(key)
        else:
            # Combine regex patterns into a single pattern
            for key in use_sequence:
                new_log, n = _get_rule_pattern(key).subn('<*>', new_log)
                if n:
                    matched_types.append(key)
        new_log = _reduce_placeholders(new_log)
        new_log = _apply_cleanup(new_log, use_sequence)
        if print: print('预处理后\n', new_log)
        return new_log, sequence, matched_types
    else:
        if print: print('原始日志\n', new_log)
        # 热路径：将 (key, 编译正则, 替换串) 绑定到局部变量，
        # 避免 per-rule 的字典查找与函数调用开销
        if use_keys:
            for key in use_sequence:
                new_log = _get_rule_pattern(key).sub(regex_map[key], new_log)
        else:
            for key in use_sequence:
                new_log = _get_rule_pattern(key).sub('<*>', new_log)
        new_log = _reduce_placeholders(new_log)
        new_log = _apply_cleanup(new_log, use_sequence)
        if print: print('预处理后\n', new_log)
        return new_log
        

        
    

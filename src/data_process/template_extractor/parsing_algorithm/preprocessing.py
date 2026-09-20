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
    # 修复 \b 导致负号/正号与数字割裂的问题：
    # 原版 \b(\-?\+?\d+\.?\d*)\b 中，\b 在 (-5) 的 - 和 5 之间形成词边界，
    # 导致引擎从 5 开始匹配，负号 - 被留在 <*> 外面，产生 (-<*>) 碎片化。
    # 修复：将左侧 \b 替换为 (?:\b|(?<=\W)|^)，允许前一个字符是非单词字符（如 (、=、空格）
    # 时也正确匹配带符号的数值；右侧同理替换为 (?:\b|(?=\W)|$)。
    'numerical': r'(?:\b|(?<=\W)|^)(\-?\+?\d+\.?\d*|0[Xx][a-fA-F\d]+)(?:\b|(?=\W)|$)|\b[a-fA-F\d]{4,}\b',
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

    # 15. 下划线连接的复合数值：Cap:NUM_NUM、Arg:D_D_D 等 Key:Value 结构
    # 作用目标：Cap:15749414426_16561907771_414286279520、Arg:0_2_5
    # 工程逻辑：下划线 _ 属于 \w，导致 \b 无法在数字段之间形成 word boundary，
    #          numerical 正则的 \b\d+\b 对这些复合值完全失效。
    #          用 (?<=[A-Za-z]:) 后行断言限定 Key:Value 结构，避免误替换独立数字。
    'underscore_composite_num': r'(?<=[A-Za-z]:)\d+(?:_\d+)+',

    # 16. 大括号内十六进制:数值对：{hex:hex_N_N}
    # 作用目标：{165879a000:165879a000_0_0}{133fe97aee:133fe97aee_0_0}
    # 工程逻辑：numerical 的 \b[a-fA-F\d]{4,}\b 只替换冒号前的 hex 部分，
    #          冒号后的 hex_N_N 因下划线连接而残留。整对替换避免碎片化。
    'brace_hex_value_pair': r'\{[0-9a-fA-F]+:[0-9a-fA-F]+(?:_\d+)+\}',

    # 17. Key:复合标识符值：name:fsi_snap_fs5041_1102702、root_cfg_space_partition_0_20 等
    # 作用目标：key:value 结构中 value 为字母+下划线+数字组成的复合标识符
    # 工程逻辑：下划线 _ 属于 \w，整个值是一个连续的 \w+ token，
    #          numerical 的 \b\d+\b 和 \b[a-fA-F\d]{4,}\b 均无法匹配。
    #          用 (?<=[A-Za-z]:) 限定 key:value 结构，避免误替换独立标识符。
    #          匹配至少含一个数字的 \w+ 序列（排除纯字母标识符如 name:active）。
    'kv_compound_id': r'(?<=[A-Za-z]:)(?=\w*\d)\w+(?:_\w+)*',

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

    # 20. 方括号内的复合标识符：[hex_hex_hex]、[num_num_num]
    # 作用目标：cap:[9d1d4000_8d00a477_7644d1][0_0_0][0_0_0]
    # 工程逻辑：下划线连接的 hex/数字复合标识符，numerical 的 \b 无法在 _ 后形成词边界，
    #          underscore_composite_num 要求前导 key: 但此处前导是 [，
    #          导致 cap 值残留在模板中，不同 cap 值的日志分裂为不同簇。
    #          (?<=\[) 和 (?=\]) 限定在方括号内，避免误匹配字段名。
    'bracket_compound_id': r'(?<=\[)[a-fA-F\d]+(?:_[a-fA-F\d]+)+(?=\])',

    # 21. 标识符内嵌的 0x hex 值：RTOBJ_DP0x0_RV0xffff
    # 作用目标：RTOBJ_DP0x0_RV0xffff、RTSETOBJ_DP0x0、DP0x0_RV0x12
    # 工程逻辑：0x hex 值嵌入下划线连接的标识符中，numerical 的 \b 无法在字母后匹配 0x，
    #          导致不同 hex 值的标识符残留，日志分裂为不同簇。
    #          (?<=[A-Za-z_]) 限定 0x 前面必须是字母或下划线（标识符内部），不匹配独立 0xNNNN。
    'embedded_hex': r'(?<=[A-Za-z_(])0x[0-9a-fA-F]+',

    # 21b. 含 0x hex 的下划线复合标识符整体匹配：MTOBJ_DP0x0_RV0x23_ID0x0
    # 作用目标：ZoneName(MTOBJ_DP0x0_RV0x23_ID0x0) → ZoneName(<*>)
    # 工程逻辑：embedded_hex 逐个替换 0x 值产生 MTOBJ_DP<*>_RV<*>_ID<*> 碎片，
    #          lucene_filename 兜底清理又吞掉 _RV/_ID 产生连续 <*>。
    #          整体匹配含 0x 的下划线复合标识符，一步到位替换为 <*>。
    #          放在 embedded_hex 之前优先整体匹配；不匹配的独立 0x 值由 embedded_hex 兜底。
    'compound_hex_id': r'[A-Za-z][A-Za-z_]*0x[0-9a-fA-F]+(?:_[A-Za-z][A-Za-z_]*0x[0-9a-fA-F]+)*',

    # 21c. xnet 上下文标识符：(xnet_r0_0_14:0) 中的 xnet_r0_0_14:0
    # 作用目标：(xnet_r0_0_14:0)Conn(...) → (<*>)Conn(...)
    # 工程逻辑：用 lookbehind/lookahead 保留外层括号，避免 <*>Conn 产生后处理吞字。
    'xnet_ctx_id': r'(?<=\()xnet_\w+:\d+(?=\))',

    # 22. 下划线连接的数字后缀：extend_2、CSD_8、ist_tx_1、xnet_mmon_17
    # 作用目标：字母_数字 格式的标识符中的数字部分
    # 工程逻辑：_ 是 \w 字符，\b 无法在 _ 和数字间形成词边界，numerical 不匹配。
    #          (?<=[A-Za-z]) 限定 _ 前面是字母，(?=[^A-Za-z0-9]|$) 限定数字后面是非字母数字或行尾，
    #          避免匹配 underscore_composite_num（如 0_0_0）和 kv_compound_id（如 dpId_0_Cap 中的 _0_）。
    #          注意：放在 underscore_composite_num 和 kv_compound_id 之后，让它们优先处理复合结构。
    'underscore_num_suffix': r'(?<=[A-Za-z])_\d+(?=[^A-Za-z0-9]|$)',

    # 23. 设备序列号：数字与大写字母交替的混合序列号
    # 作用目标：2102312UCT10L8000290、2102313EDH10PA100243
    # 工程逻辑：设备序列号含多组数字和大写字母交替，numerical 无法整体匹配（字母打断数字），
    #          导致序列号残留在模板中，不同设备的日志分裂为不同簇。
    #          \d{4,} 起始限定至少4位数字，[A-Z]{2,} 至少2个大写字母，避免误匹配普通标识符。
    'device_serial': r'\d{4,}[A-Z]{2,}\d{2,}(?:[A-Z]{1,}\d{2,})+',

    # 24. 含数字的复合标识符：字母开头，含数字+下划线+数字的标识符
    # 作用目标：fsi_snap_fs5041_1058676、xnet_r0_0_14
    # 工程逻辑：数字被 _ 包围时 \b 无法形成词边界，numerical 不匹配。
    #          [A-Za-z]\w*\d+ 匹配含数字的标识符前段，(?:_\d+)+ 匹配至少一组 _数字后缀。
    #          整体替换避免不同 ID 的标识符残留导致簇分裂。
    'compound_num_id': r'[A-Za-z]\w*\d+(?:_\d+)+(?=[^A-Za-z0-9]|$)',
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
# 顺序原则：越复杂/越特化的正则越先匹配，避免被通用正则提前截断
# embedded_hex 和 underscore_num_suffix 放在 kv_compound_id 之后、url 等通用模式之前：
# - embedded_hex 须在 kv_compound_id 之后（kv_compound_id 可能整体匹配含 0x 的复合标识符）
# - underscore_num_suffix 须在 underscore_composite_num 和 kv_compound_id 之后
#   （这两个先处理 _数字_数字_ 和 key:value 结构中的复合标识符）
sequence = ['datetime_full',  'kernel_uptime', 'epoch_ms_13d', 'log_level',  'code_line_num', 'pid_tid_pair' , 'sn_parentheses', 'step_desc', 'vcontroller_metric', 'fs_init_action', 'underscore_composite_num', 'brace_hex_value_pair', 'hex_dump', 'lucene_filename', 'bracket_compound_id', 'kv_compound_id', 'compound_hex_id', 'xnet_ctx_id', 'embedded_hex', 'underscore_num_suffix', 'device_serial', 'compound_num_id', 'url', 'IPv4_port', 'host_port', 'package_host', 'IPv6', 'Mac_address', 'time', 'path', 'block', 'date', 'duration', 'size', 'numerical', 'weekday_months']

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
        # Reduce <*> separated by single delimiters (e.g., <*>:<*>:<*> → <*>)
        # 与 correct_single_template 的 CV 归约保持一致，避免 logTemplate/pathList 残留 <*>:<*>
        while True:
            prev = new_log
            for _delim in [':', ',', ';', '|', '=', '.', '-', '+', '#', '@', '$', '%', '&']:
                new_log = re.sub(r'<\*>%s<\*>' % re.escape(_delim), '<*>', new_log)
            if prev == new_log:
                break
        # Hex dump 清理：D:后残留的 2 字符 hex 字节（estimation 阶段可能未触发 hex_dump 正则）
        new_log = re.sub(r'D:(?:<\*> )?[0-9a-fA-F]{2}(?: [0-9a-fA-F]{2}| <\*>)*', 'D:<*>', new_log)
        # Hex dump 清理：连续 2 字符 hex 序列
        # (?!\w) 防止匹配英文单词片段（如 "DB fa"iled → "DB fa" 是 2 对 hex 但属于单词 "DB failed"）
        new_log = re.sub(r' [0-9a-fA-F]{2}(?!\w)(?: [0-9a-fA-F]{2}(?!\w))+', ' <*>', new_log)
        # Lucene 文件名清理：estimation 阶段可能未触发 lucene_filename 正则
        # (?<![\w>]) 排除 <*> 后的 _标识符（如 <*>_RV），避免吞掉有意义的后缀
        new_log = re.sub(r'(?<![\w>])[_.][\w.~-]+', '<*>', new_log)
        # 方括号内复合标识符清理：estimation 阶段可能未触发 bracket_compound_id 正则
        new_log = re.sub(r'(?<=\[)[a-fA-F\d]+(?:_[a-fA-F\d]+)+(?=\])', '<*>', new_log)
        # 含 0x hex 的复合标识符整体清理：estimation 阶段可能未触发 compound_hex_id 正则
        new_log = re.sub(r'[A-Za-z][A-Za-z_]*0x[0-9a-fA-F]+(?:_[A-Za-z][A-Za-z_]*0x[0-9a-fA-F]+)*', '<*>', new_log)
        # xnet 上下文标识符清理：estimation 阶段可能未触发 xnet_ctx_id 正则
        new_log = re.sub(r'(?<=\()xnet_\w+:\d+(?=\))', '<*>', new_log)
        # 标识符内嵌 0x hex 清理：estimation 阶段可能未触发 embedded_hex 正则
        new_log = re.sub(r'(?<=[A-Za-z_(])0x[0-9a-fA-F]+', '<*>', new_log)
        # 下划线数字后缀清理：estimation 阶段可能未触发 underscore_num_suffix 正则
        new_log = re.sub(r'(?<=[A-Za-z])_\d+(?=[^A-Za-z0-9]|$)', '_<*>', new_log)
        # 设备序列号清理：estimation 阶段可能未触发 device_serial 正则
        new_log = re.sub(r'\d{4,}[A-Z]{2,}\d{2,}(?:[A-Z]{1,}\d{2,})+', '<*>', new_log)
        # 含数字的复合标识符清理：estimation 阶段可能未触发 compound_num_id 正则
        new_log = re.sub(r'[A-Za-z]\w*\d+(?:_\d+)+(?=[^A-Za-z0-9]|$)', '<*>', new_log)
        # 日志头清理：去掉开头连续的 [<*>] 块（时间戳/运行时间/pid/日志级别预处理后产生），
        # 这些纯 <*> token 被 seqDist 跳过但计入分母，稀释相似度导致簇分裂
        new_log = re.sub(r'^(?:\[(?:<\*>)?\]\s*)+', '', new_log)
        # 日志尾部清理：去掉末尾的 [module,<*>]（源模块名，format_chaos 会去除，no_format 保留导致分裂）
        new_log = re.sub(r'\[[a-z_]+,<\*>\]\s*$', '', new_log)
        # 兜底清理后再次归约连续 <*>（lucene_filename 等清理可能产生 <*><*> 碎片）
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
        # Reduce <*> separated by single delimiters (e.g., <*>:<*>:<*> → <*>)
        # 与 correct_single_template 的 CV 归约保持一致，避免 logTemplate/pathList 残留 <*>:<*>
        while True:
            prev = new_log
            for _delim in [':', ',', ';', '|', '=', '.', '-', '+', '#', '@', '$', '%', '&']:
                new_log = re.sub(r'<\*>%s<\*>' % re.escape(_delim), '<*>', new_log)
            if prev == new_log:
                break
        # Hex dump 清理：D:后残留的 2 字符 hex 字节（estimation 阶段可能未触发 hex_dump 正则）
        new_log = re.sub(r'D:(?:<\*> )?[0-9a-fA-F]{2}(?: [0-9a-fA-F]{2}| <\*>)*', 'D:<*>', new_log)
        # Hex dump 清理：连续 2 字符 hex 序列
        # (?!\w) 防止匹配英文单词片段（如 "DB fa"iled → "DB fa" 是 2 对 hex 但属于单词 "DB failed"）
        new_log = re.sub(r' [0-9a-fA-F]{2}(?!\w)(?: [0-9a-fA-F]{2}(?!\w))+', ' <*>', new_log)
        # Lucene 文件名清理：estimation 阶段可能未触发 lucene_filename 正则
        # (?<![\w>]) 排除 <*> 后的 _标识符（如 <*>_RV），避免吞掉有意义的后缀
        new_log = re.sub(r'(?<![\w>])[_.][\w.~-]+', '<*>', new_log)
        # 方括号内复合标识符清理：estimation 阶段可能未触发 bracket_compound_id 正则
        new_log = re.sub(r'(?<=\[)[a-fA-F\d]+(?:_[a-fA-F\d]+)+(?=\])', '<*>', new_log)
        # 含 0x hex 的复合标识符整体清理：estimation 阶段可能未触发 compound_hex_id 正则
        new_log = re.sub(r'[A-Za-z][A-Za-z_]*0x[0-9a-fA-F]+(?:_[A-Za-z][A-Za-z_]*0x[0-9a-fA-F]+)*', '<*>', new_log)
        # xnet 上下文标识符清理：estimation 阶段可能未触发 xnet_ctx_id 正则
        new_log = re.sub(r'(?<=\()xnet_\w+:\d+(?=\))', '<*>', new_log)
        # 标识符内嵌 0x hex 清理：estimation 阶段可能未触发 embedded_hex 正则
        new_log = re.sub(r'(?<=[A-Za-z_(])0x[0-9a-fA-F]+', '<*>', new_log)
        # 下划线数字后缀清理：estimation 阶段可能未触发 underscore_num_suffix 正则
        new_log = re.sub(r'(?<=[A-Za-z])_\d+(?=[^A-Za-z0-9]|$)', '_<*>', new_log)
        # 设备序列号清理：estimation 阶段可能未触发 device_serial 正则
        new_log = re.sub(r'\d{4,}[A-Z]{2,}\d{2,}(?:[A-Z]{1,}\d{2,})+', '<*>', new_log)
        # 含数字的复合标识符清理：estimation 阶段可能未触发 compound_num_id 正则
        new_log = re.sub(r'[A-Za-z]\w*\d+(?:_\d+)+(?=[^A-Za-z0-9]|$)', '<*>', new_log)
        # 日志头清理：去掉开头连续的 [<*>] 块（时间戳/运行时间/pid/日志级别预处理后产生），
        # 这些纯 <*> token 被 seqDist 跳过但计入分母，稀释相似度导致簇分裂
        new_log = re.sub(r'^(?:\[(?:<\*>)?\]\s*)+', '', new_log)
        # 日志尾部清理：去掉末尾的 [module,<*>]（源模块名，format_chaos 会去除，no_format 保留导致分裂）
        new_log = re.sub(r'\[[a-z_]+,<\*>\]\s*$', '', new_log)
        # 兜底清理后再次归约连续 <*>（lucene_filename 等清理可能产生 <*><*> 碎片）
        new_log = re.sub(r'(\<\*\>\s?)+\<\*\>', '<*>', new_log)
        if print:print('预处理后\n', new_log)
        return new_log

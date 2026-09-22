import regex as re
path_delimiters = {  # reduced set of delimiters for tokenizing for checking the path-like strings
    r'\s', r'\,', r'\!', r'\;', r'\:',
    r'\=', r'\|', r'\"', r'\'',
    r'\[', r'\]', r'\(', r'\)', r'\{', r'\}'
}
token_delimiters = path_delimiters.union({  # all delimiters for tokenizing the remaining rules
    r'\.', r'\-', r'\+', r'\@', r'\#', r'\$', r'\%', r'\&',
})

def _fix_bracket_balance(template: str) -> str:
    """修复模板中未匹配的关闭括号：在 <*>), ], } 前插入对应的开启括号。

    算法：
    1. 将 <*> 等占位符替换为 \x00（单字符占位），避免占位符内部的括号干扰计数
    2. 逐字符扫描，维护三种括号的深度计数器
    3. 遇到未匹配的关闭括号时，向前找最近的占位符，在其前面插入对应的开启括号
    4. 还原占位符并返回
    """
    placeholders = []
    def save_placeholder(m):
        placeholders.append(m.group(0))
        return '\x00'
    flat = re.sub(r'<[^>]+>', save_placeholder, template)

    fixes = []  # [(position_in_flat, char_to_insert)]
    depth = {'(': 0, '[': 0, '{': 0}
    close_to_open = {')': '(', ']': '[', '}': '{'}

    for i, ch in enumerate(flat):
        if ch in depth:
            depth[ch] += 1
        elif ch in close_to_open:
            open_ch = close_to_open[ch]
            if depth[open_ch] > 0:
                depth[open_ch] -= 1
            else:
                # 未匹配的关闭括号，在最近的前方占位符前插入开启括号
                for j in range(i - 1, -1, -1):
                    if flat[j] == '\x00':
                        fixes.append((j, open_ch))
                        break

    # 抵消：扫描结束后若某类括号仍有未匹配的开括号（depth > 0），
    # 说明模板中存在等量的未匹配开/闭括号（如 ][ 分隔符模式），
    # 这些不应触发插入修复。移除最晚的对应 fix，数量取 min(depth, fixes)。
    for open_ch in ('(', '[', '{'):
        if depth[open_ch] > 0:
            matching_fixes = [f for f in fixes if f[1] == open_ch]
            for k in range(min(depth[open_ch], len(matching_fixes))):
                fixes.remove(matching_fixes[-(k + 1)])

    if not fixes:
        return template

    # 从后往前插入，避免位移
    result = list(flat)
    for pos, insert_ch in sorted(fixes, key=lambda x: -x[0]):
        result.insert(pos, insert_ch)

    result_str = ''.join(result)

    # 还原占位符
    idx = 0
    final = []
    for ch in result_str:
        if ch == '\x00':
            final.append(placeholders[idx])
            idx += 1
        else:
            final.append(ch)

    return ''.join(final)


# Template correction function from LUNAR
def correct_single_template(template, user_strings=None):
    """Apply all rules to process a template.

    DS (Double Space)
    BL (Boolean) # we don't use this
    US (User String) # we don't use this
    DG (Digit)
    HEX (Hex Variables)
    PS (Path-like String) # we don't use this
    WV (Word concatenated with Variable)
    DV (Dot-separated Variables)
    CV (Consecutive Variables)

    """

    # boolean = {}
    # default_strings = {}
    path_delimiters = {  # reduced set of delimiters for tokenizing for checking the path-like strings
        r'\s', r'\,', r'\!', r'\;', r'\:',
        r'\=', r'\|', r'\"', r'\'',
        r'\[', r'\]', r'\(', r'\)', r'\{', r'\}'
    }
    token_delimiters = path_delimiters.union({  # all delimiters for tokenizing the remaining rules
        r'\.', r'\-', r'\+', r'\@', r'\#', r'\$', r'\%', r'\&',
        r'\_',  # 下划线：使 'xnet_<*>' 被拆分为 'xnet_' + '<*>'，保留标识符前缀
    })

    # if user_strings:
        # default_strings = default_strings.union(user_strings)

    # apply DS
    template = template.strip()
    template = re.sub(r'\s+', ' ', template)

    # apply PS
    # p_tokens = re.split('(' + '|'.join(path_delimiters) + ')', template)
    # new_p_tokens = []
    # for p_token in p_tokens:
        # if re.match(r'^(\/[^\/]+)+$', p_token):
            # p_token = '<*>'
        # new_p_tokens.append(p_token)
    # template = ''.join(new_p_tokens)
    # Replace the assignments
    # 负向前瞻 (?![A-Z]+(?:\s|$|[,;)\]}.])) 排除全大写词（如 SQL 关键字 REPLACE/INSERT/SELECT），
    # 这些是保留字而非动态值；全大写常量（如 INFO/ERROR）同理不应替换
    template = re.sub(r'(?<!!)=\s(?![A-Z]+(?:\s|$|[,;)\]}.]))[a-zA-Z0-9\_\./]+', '= <*>', template)
    template = re.sub(r'(?<!!)=(?![A-Z]+(?:\s|$|[,;)\]}.]))[a-zA-Z0-9\_\./]+', '=<*>', template)


    # tokenize for the remaining rules
    tokens = re.split('(' + '|'.join(token_delimiters) + ')', template)  # tokenizing while keeping delimiters
    new_tokens = []
    for token in tokens:
        # apply BL, US
        # for to_replace in boolean.union(default_strings):
            # if token.lower() == to_replace.lower():
                # token = '<*>'

        # Simplify the tokens
        if '<*>' in token:
            token = '<*>'

        # collect the result
        new_tokens.append(token)

    # make the template using new_tokens
    template = ''.join(new_tokens)

    # Reduce repetitive patterns
    # 排除结构性分隔符（括号、=）——这些分隔符承载语义结构信息，
    # structure_aware_merge 产生的 <*>)<*> 等模式应保留。
    # 仅对链式分隔符（空格、逗号、分号、下划线等）进行归约。
    _structural_delim = {r'\(', r'\)', r'\[', r'\]', r'\{', r'\}', r'\='}
    _reduction_delims = path_delimiters.union(token_delimiters) - _structural_delim
    while True:
        prev = template
        for delimiter in _reduction_delims:
            pattern = r'<\*>%s<\*>'%delimiter
            template = re.sub(r'%s'%pattern, '<*>', template)
        if prev == template:
            break

    # 兜底：修复 identifier<*>) → identifier(<*>) （半括号修正）
    # 注意：旧版正则 r'([a-zA-Z_]\w*)<\*\)' 缺少 >，实际匹配 identifier<*) 而非 identifier<*>)，已修复
    # 负向后行断言 (?<![\w(>])：当标识符前已是 (（开括号存在）、>（<*> 尾部，标识符是通配符后的连接符）
    # 或其他单词字符时不触发，避免在 name(DMI_<*>) 等场景中插入多余的开括号
    template = re.sub(r'(?<![\w(>])([a-zA-Z_]\w*)<\*>\)', r'\1(<*>)', template)

    # 吸收 <*> 前的短版本号前缀（如 V1, V2 等）
    # V1 是版本号常量前缀，太短不被预处理正则匹配，后处理中吸收进通配符
    template = re.sub(r'[A-Z]\d+,<\*>', '<*>', template)

    # Substitute consecutive variables only if not separated with any delimiter including space (CV)
    # NOTE: this should be done at the end
    #print("CV: ", template)
    while True:
        prev = template
        template = re.sub(r'<\*><\*>', '<*>', template)
        if prev == template:
            break
    #print("CV: ", template)
    
    # From LUNAR
    while " #<*># " in template:
        template = template.replace(" #<*># ", " <*> ")

    while " #<*> " in template:
        template = template.replace(" #<*> ", " <*> ")

    while ' "<*>" ' in template:
        template = template.replace(' "<*>" ', ' <*> ')
    # 无空格版本的引号包裹简化："<*>" → <*>, '<*>' → <*>
    while '"<*>"' in template:
        template = template.replace('"<*>"', '<*>')
    while "'<*>'" in template:
        template = template.replace("'<*>'", '<*>')

    while " '<*>' " in template:
        template = template.replace(" '<*>' ", " <*> ")

    while "<*><*>" in template:
        template = template.replace("<*><*>", "<*>")

    # 无空格版本的逗号/句号归约：<*>, <*> → <*>
    while "<*>, <*>" in template:
        template = template.replace("<*>, <*>", "<*>")
    while "<*>,<*>" in template:
        template = template.replace("<*>,<*>", "<*>")
    while "<*>. <*>" in template:
        template = template.replace("<*>. <*>", "<*>")
    while "<*>##<*>" in template:
        template = template.replace("<*>##<*>", "<*>")
    while "#<*>#" in template:
        template = template.replace("#<*>#", "<*>")
    while " <*> <*> " in template:
        template = template.replace(" <*> <*> ", " <*> ")

    # ---:<*> → <*>，--<*>:---() → <*>
    # 原始日志中 ---:0、---+0:---() 是"无值"占位符，preprocessing 只替换了数字部分，
    # 残留的 ---: 和 :---() 前后缀是无意义噪声，整体应归约为 <*>
    template = re.sub(r'-+:<\*>', '<*>', template)
    template = re.sub(r'-+<\*>:-+\(\)', '<*>', template)

    # 模板级括号平衡：修复未匹配的关闭括号（<*>) → (<*>) 等）
    template = _fix_bracket_balance(template)

    return template

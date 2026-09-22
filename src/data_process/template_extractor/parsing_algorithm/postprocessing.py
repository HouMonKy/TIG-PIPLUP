import regex as re
path_delimiters = {  # reduced set of delimiters for tokenizing for checking the path-like strings
    r'\s', r'\,', r'\!', r'\;', r'\:',
    r'\=', r'\|', r'\"', r'\'',
    r'\[', r'\]', r'\(', r'\)', r'\{', r'\}'
}
token_delimiters = path_delimiters.union({  # all delimiters for tokenizing the remaining rules
    r'\.', r'\-', r'\+', r'\@', r'\#', r'\$', r'\%', r'\&',
})

# 说明：原 _fix_bracket_balance 会在未匹配的关闭括号前插入开启括号（如
# 'value <*> ) done' → 'value (<*>) done'），原半括号修正规则会把
# 'identifier<*>)' 改写为 'identifier(<*>)'。两者都拿不到来源内容：
# 当来源本身含孤立关闭括号（如 'value 5) done'）时，插入的开括号在来源中
# 不存在，产生的模板看起来完整、却匹配不了来源行。
# 以来源匹配为准：来源不完整时保留来源的括号原样，不再凭空补入固定括号；
# 来源括号完整而合并破坏结构的场景，已在 _structure_aware_merge 的
# 合并位置利用两个输入修复（见 PIPLUP.py Step 5）。


# correct_single_template 为纯函数（结果仅依赖输入模板字符串，
# user_strings 参数从未参与计算）。结果缓存不依赖簇状态、不持久化，
# 容量上限，超限整体清空。
_CORRECT_CACHE = {}
_CORRECT_CACHE_CAP = 8192


def correct_single_template(template, user_strings=None):
    cached = _CORRECT_CACHE.get(template)
    if cached is not None:
        return cached
    result = _correct_single_template_impl(template, user_strings)
    if len(_CORRECT_CACHE) >= _CORRECT_CACHE_CAP:
        _CORRECT_CACHE.clear()
    _CORRECT_CACHE[template] = result
    return result


# Template correction function from LUNAR
def _correct_single_template_impl(template, user_strings=None):
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

    # 吸收 <*> 前的短版本号前缀（如 V1, V2 等）
    # V1 是版本号常量前缀，太短不被预处理正则匹配，后处理中吸收进通配符
    # 词边界限制：版本号须为独立 token（前面是非词字符或行首），
    # 避免 AV1,<*> 因内部命中 V1,<*> 被误伤为 A<*>（较长标识符的一部分）
    template = re.sub(r'\b[A-Z]\d+,<\*>', '<*>', template)

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

    return template

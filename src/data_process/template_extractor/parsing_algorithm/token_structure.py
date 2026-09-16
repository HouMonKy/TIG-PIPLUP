# -*- coding: utf-8 -*-
"""token 内结构识别：供候选评分（seqDist）与泛化（merge）共享的轻量机制。

第一轮覆盖：
- key:value / key=value（含括号/引号前缀）
- 括号、引号与既有 <*> 边界的骨架对齐
- 对应值中的复合标识符与数字链（共同前缀 + 数字组边界）

不覆盖（保留原处理路径）：跨空白字段值、字段重排、可选字段。

设计约束：
- 冒号歧义：整体形如 IPv6 / 时间 / URL 的 token 不拆分 kv；
  key 必须以字母开头、含十六进制以外的字母（排除 IPv6 组误判）。
- 字段名是固定证据：同字段名 + 值可变（复合标识符/数字链）→ 高相似；
  同字段名 + 普通词值不同 → 仅字段名证据（低相似）；
  字段名不同 → 结构不给分。
- 骨架相同只提供有限相似度：结构符号约束对齐，不因括号冒号数量加分，
  必须搭配固定文本证据。
"""
import regex as re

# ── 整体歧义守卫（模块级预编译） ──────────────────────────────
_IPV6_RE = re.compile(
    r'(([0-9a-fA-F]{1,4}:){7}([0-9a-fA-F]{1,4}|:)|'
    r'(([0-9a-fA-F]{1,4}:){1,7}|:):((:[0-9a-fA-F]{1,4}){1,7}|:))')
_TIME_RE = re.compile(r'\d{1,2}:\d{2}(:\d{2})?([.,]\d+)?(?=[^\d:]|$)')

# key 形状：字母开头，仅含字母/数字/下划线/点/连字符
_KEY_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_.\-]*$')
_HEX_CHARS = set('0123456789abcdefABCDEF')

# 含数字的词段（复合标识符/数字链）→ 骨架中的变量位
_VAR_RUN_RE = re.compile(r'[A-Za-z0-9_]*\d[\w.\-]*')


def split_kv(token):
    """把 token 拆为 (prefix, key, sep, value)；非明确 kv 结构返回 None。

    prefix：key 前的引导结构符（开括号/引号等，不含字母数字）；
    key：满足 key 形状且非纯十六进制组（IPv6 歧义守卫）；
    sep：':' 或 '='（取第一个，处于任意括号层级）；
    value：分隔符后的非空剩余（须含字母数字）。

    含 <*> 的 token 不拆分（交由既有通配机制处理）。
    """
    if '<*>' in token:
        return None
    if '://' in token:  # URL
        return None
    if _IPV6_RE.fullmatch(token) or _TIME_RE.fullmatch(token):
        return None
    for i, ch in enumerate(token):
        if ch == ':' or ch == '=':
            j = i - 1
            while j >= 0 and (token[j].isalnum() or token[j] in '_-.'):
                j -= 1
            key = token[j + 1:i]
            prefix = token[:j + 1]
            if not key or not _KEY_RE.match(key):
                return None
            if all(c in _HEX_CHARS for c in key):
                return None
            if prefix and any(c.isalnum() for c in prefix):
                return None
            value = token[i + 1:]
            if not value or not any(c.isalnum() for c in value):
                return None
            return prefix, key, ch, value
    return None


def _is_variable_like(value):
    """值是否像变量（含数字的标识符/数字链）。普通词（running 等）不算。"""
    return len(value) >= 2 and any(c.isdigit() for c in value)


def _common_affix_len(v1, v2, from_end=False):
    n = 0
    if from_end:
        for c1, c2 in zip(reversed(v1), reversed(v2)):
            if c1 == c2:
                n += 1
            else:
                break
    else:
        for c1, c2 in zip(v1, v2):
            if c1 == c2:
                n += 1
            else:
                break
    return n


def value_similarity(v1, v2):
    """同一字段名下两个值的相似度（0~1）。

    - 相等 → 1.0
    - 双方可变（复合标识符/数字链）且有共同前后缀或同骨架 → 0.8
    - 双方可变但几无共同点 → 0.4
    - 单方可变 → 0.2
    - 双方普通词（状态/动作词等）→ 0.0（字段名之外无合并证据）
    """
    if v1 == v2:
        return 1.0
    var1, var2 = _is_variable_like(v1), _is_variable_like(v2)
    if var1 and var2:
        if (_common_affix_len(v1, v2) >= 2
                or _common_affix_len(v1, v2, from_end=True) >= 2
                or _strip_digits(v1) == _strip_digits(v2)):
            return 0.8
        return 0.4
    if var1 or var2:
        return 0.2
    return 0.0


def _strip_digits(v):
    return re.sub(r'\d+', '', v)


def _skeleton(token):
    """骨架：把含数字的词段与 <*> 替换为变量位 \\x00，保留结构符与纯字母文本。"""
    t = token.replace('<*>', '\x00')
    return _VAR_RUN_RE.sub('\x00', t)


def _skeleton_similarity(t1, t2):
    """骨架对齐相似度：结构符约束对齐，得分必须来自固定文本证据。"""
    sk1, sk2 = _skeleton(t1), _skeleton(t2)
    if sk1 != sk2:
        return 0.0
    fixed = sum(1 for c in sk1 if c != '\x00')
    if fixed == 0:
        return 0.0
    # 上限 0.5：结构一致本身不能给出高分
    return 0.3 + 0.2 * min(1.0, fixed / 8.0)


def structure_similarity(t1, t2):
    """token 级结构相似度（0~1），供 seqDist 评分使用。"""
    if t1 == t2:
        return 1.0
    kv1, kv2 = split_kv(t1), split_kv(t2)
    if kv1 and kv2:
        (p1, k1, s1, v1), (p2, k2, s2, v2) = kv1, kv2
        if k1 == k2 and s1 == s2 and p1 == p2:
            # 同字段名：字段名是固定证据，值是可变槽位
            return 0.5 + 0.5 * value_similarity(v1, v2)
        # 字段名不同：结构不给分（保守，防止仅凭 key:value 形式归并）
        return 0.0
    return _skeleton_similarity(t1, t2)


def merge_values(v1, v2):
    """合并同一字段名下的两个值：保留数字组边界对齐的共同前/后缀。

    规则：
    - 共同前缀末尾若是"半个数字组"（两侧在该位置后均接数字），回退到
      数字组边界，避免 'xnet_r0_0_14' vs 'xnet_r0_0_50' 保留巧合前缀 '1'；
    - 共同后缀开头同理；
    - 前后缀覆盖整个值时退化为 <*>（重叠保护）。
    """
    if v1 == v2:
        return v1
    lcp = _common_affix_len(v1, v2)
    lcs = _common_affix_len(v1, v2, from_end=True)

    # 数字组边界修剪（前缀）
    while (lcp > 0 and v1[lcp - 1].isdigit()
           and ((lcp < len(v1) and v1[lcp].isdigit())
                or (lcp < len(v2) and v2[lcp].isdigit()))):
        lcp -= 1
    # 数字组边界修剪（后缀）
    s1, s2 = len(v1) - lcs, len(v2) - lcs
    while (lcs > 0 and v1[s1].isdigit()
           and ((s1 - 1 >= 0 and v1[s1 - 1].isdigit())
                or (s2 - 1 >= 0 and v2[s2 - 1].isdigit()))):
        s1 += 1
        s2 += 1
        lcs -= 1

    if lcp + lcs >= min(len(v1), len(v2)):
        return '<*>'
    prefix, suffix = v1[:lcp], v1[s1:]
    if not prefix and not suffix:
        return '<*>'
    return prefix + '<*>' + suffix


def merge_kv_tokens(t1, t2):
    """同字段名的两个 kv token 合并；不适用返回 None（交由既有路径）。

    仅处理双方均不含 <*> 的情况；含 <*> 的组合由通配覆盖检查与
    渐进泛化处理。
    """
    kv1, kv2 = split_kv(t1), split_kv(t2)
    if not (kv1 and kv2):
        return None
    (p1, k1, s1, v1), (p2, k2, s2, v2) = kv1, kv2
    if k1 != k2 or s1 != s2 or p1 != p2:
        return None
    return p1 + k1 + s1 + merge_values(v1, v2)

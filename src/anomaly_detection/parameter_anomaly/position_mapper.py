"""旧分词坐标（tokenizer 与 span）。

param_pos 的坐标语义源自旧系统 log_template_parsing_process 的
分词规则，此处为唯一实现：tokenize_for_params / tokenize_for_
params_with_spans / template_tokens_for_alignment。
"""

import re
from typing import List, Optional, Tuple

_SPLIT_RE = re.compile(r"([:=,\[\]\(\)])")

def get_content_as_tokens(content: str) -> List[str]:
    content = content.strip()
    content = content.replace(",", " ")
    return content.split()

def tokenize_for_params(content: str) -> List[str]:
    """旧系统 param 提取 tokenization（log_template_parsing_process
    复刻）：re.split 捕获拆分 -> 空格连接 -> 逗号替换空格 -> split。"""
    split_result = _SPLIT_RE.split(content)
    split_content = content.strip()
    if len(split_result) > 1:
        split_content = " ".join(split_result)
    return get_content_as_tokens(split_content)

def tokenize_for_params_with_spans(
        content: str) -> Tuple[List[str], List[Tuple[int, int]]]:
    """与 tokenize_for_params 完全一致的 token 序列 + 原文半开区间。

    以逐字符偏移跟踪完成同一变换链（不重复实现分词规则）。
    """
    content = content or ""
    pieces = _SPLIT_RE.split(content)
    out_chars: List[str] = []
    out_orig: List[Optional[int]] = []

    if len(pieces) > 1:
        offset = 0
        for i, piece in enumerate(pieces):
            if i > 0:
                out_chars.append(" ")       # join 插入的分隔空格
                out_orig.append(None)
            for j, ch in enumerate(piece):
                out_chars.append(ch)
                out_orig.append(offset + j)
            offset += len(piece)
    else:
        for j, ch in enumerate(content):
            out_chars.append(ch)
            out_orig.append(j)

    for k, ch in enumerate(out_chars):   # 逗号 -> 空格
        if ch == ",":
            out_chars[k] = " "

    tokens: List[str] = []
    spans: List[Tuple[int, int]] = []
    k, n = 0, len(out_chars)
    while k < n:
        if out_chars[k].isspace():
            k += 1
            continue
        start = k
        while k < n and not out_chars[k].isspace():
            k += 1
        tokens.append("".join(out_chars[start:k]))
        spans.append((out_orig[start], out_orig[k - 1] + 1))
    return tokens, spans

def template_tokens_for_alignment(template_text: str) -> List[str]:
    """模板文本的旧分词坐标 token（与日志同一坐标系；<*>
    不含分隔符，保持单 token）。"""
    return tokenize_for_params(template_text)

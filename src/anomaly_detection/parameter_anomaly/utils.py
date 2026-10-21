"""文本工具：匹配键、指纹、分词与值剥离。"""

import hashlib
import re
from typing import List, Optional

_PLACEHOLDER_RE = re.compile(r"<[^<>]*>")
_WORD_RE = re.compile(r"[a-z_][a-z0-9_]*")


def exact_template_key(text: str) -> str:
    """模板精确键：仅空白归一，保留占位符类型。"""
    return " ".join((text or "").split())


def template_fingerprint(text: str) -> str:
    """模板指纹：精确键的 SHA256 前 16 位。"""
    return hashlib.sha256(
        exact_template_key(text).encode("utf-8")).hexdigest()[:16]


def semantic_template_text(text: str) -> str:
    """语义文本：占位符统一为 <*>、空白归一。"""
    return " ".join(_PLACEHOLDER_RE.sub("<*>", text or "").split())


def normalize_to_tokens(text: str) -> list:
    """词法归一：小写 -> 去占位符 -> 丢纯数字 token。"""
    s = _PLACEHOLDER_RE.sub(" ", (text or "").lower())
    return [t for t in _WORD_RE.findall(s)
            if any(c.isalpha() for c in t)]


KNOWN_PLACEHOLDERS = ["<NUM>", "<*>", "<HEX0x>", "<MAC-HEX>",
                      "<HEX4>", "<MAC>", "<IP>", "<CMD>"]
_PLACEHOLDER_ALT = "|".join(re.escape(p.lower()) for p in KNOWN_PLACEHOLDERS)
_PLACEHOLDER_STRIP_RE = re.compile(_PLACEHOLDER_ALT)

_N1_TOKEN_RE = re.compile(r"-->|[a-z0-9_]+|[+\-]")

_VALUE_RE = re.compile(r"^(?:\d+|0x[0-9a-f]+|[0-9a-f]{4,})$")


def n1_tokens(text: Optional[str]) -> List[str]:
    """N1 token 序列（空序列 = 不可评分）。"""
    if not text:
        return []
    s = _PLACEHOLDER_STRIP_RE.sub(" ", text.casefold())
    return _N1_TOKEN_RE.findall(s)


def strip_value_tokens(tokens: List[str]) -> List[str]:
    """剥离值形态 token（数字/十六进制），保留骨架词。"""
    return [t for t in tokens if not _VALUE_RE.match(t)]


_VALUE_POS_SEPARATORS = frozenset({"=", ":", "(", "["})
_TRAILING_PUNCT = ".,;:!?" + "'" + '"'


def check_protected_gate(protected_context, legacy_tokens) -> bool:
    """受保护词组合门：每个受保护词须至少一个出现满足以下之一：
    1. 不在值位（前驱非 = : ( [）
    2. 与模板邻居相邻（bigram 存在）
    词完全缺失亦不通过。"""
    if not protected_context:
        return True
    toks = [t.lower().strip(_TRAILING_PUNCT)
            for t in legacy_tokens]
    bigrams = {(a, b) for a, b in zip(toks, toks[1:])}
    for word, left, right in protected_context:
        ok = False
        for i, t in enumerate(toks):
            if t != word:
                continue
            not_value = (i == 0 or
                         toks[i - 1] not in _VALUE_POS_SEPARATORS)
            adjacent = (
                (left is not None and (left, word) in bigrams) or
                (right is not None and (word, right) in bigrams))
            if not_value or adjacent:
                ok = True
                break
        if not ok:
            return False
    return True

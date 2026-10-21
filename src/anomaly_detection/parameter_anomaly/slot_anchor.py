"""模板槽位锚定取值。"""

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .position_mapper import (
    template_tokens_for_alignment, tokenize_for_params_with_spans)

__all__ = ["compile_slot_anchor", "SlotAnchor", "LocatedSlot",
           "selfcheck_rule"]

_PURE_PH_RE = re.compile(r"^<[^<>]+>$")
# 恰好一个占位符 + 可选前后缀的复合 token（QosFillVolMsgBuf+<HEX0x>）
_COMPOUND_PH_RE = re.compile(r"^(?P<prefix>[^<>]*)<[^<>]+>"
                             r"(?P<suffix>[^<>]*)$")
# 内容 token：含字母/数字（排除 ( ) , . = --> 等纯标点/算子）
_CONTENT_RE = re.compile(r"[A-Za-z0-9]")

_LEFT_MAX = 8    # 左锚窗口最大 token 数（覆盖 672 的 名词...值 距离 7~8）
_RIGHT_MAX = 2   # 右锚窗口最大 token 数

def _has_content(window) -> bool:
    return any(w is not None and _CONTENT_RE.search(w)
               for w in window)

def _name_in_window(param_name: Optional[str], window) -> bool:
    """参数名归属：名字的任一词（子串、忽略大小写）出现在窗口
    token 中——与 _find_param_name_token 的词匹配语义一致。

    多词名任一词命中即算（单词 token 无法包含完整多词名）。
    这是槽位归属证据：param_pos 指向的槽位上下文必须含有该
    规则的参数名，防止 param_pos 标到别的参数槽位上（错位
    读取，如把 ret 的值配给 retry）。param_name 未提供时跳过
    （编译期无法校验归属）。
    """
    if not param_name:
        return True
    words = [w for w in param_name.lower().split() if len(w) >= 2]
    if not words:
        words = [param_name.lower()]
    toks = [t.lower() for t in window if t is not None]
    return any(any(w in t for t in toks) for w in words)

@dataclass(frozen=True)
class LocatedSlot:
    value: str
    span: Tuple[int, int]        # 原文半开区间
    token_index: int
    sub_token: bool              # True: 值是 token 子串（不可登记变体）

@dataclass(frozen=True)
class SlotAnchor:
    param_pos: int
    left: Tuple[Optional[str], ...]    # None = 通配（模板占位符位）
    right: Tuple[str, ...]
    prefix: str = ""                   # 子串形态：token 前缀
    suffix: str = ""                   # 子串形态：token 后缀
    sub_token: bool = False

    def locate(self, tokens: List[str],
               spans: List[Tuple[int, int]]
               ) -> Optional[LocatedSlot]:
        """在日志 token 序列中唯一匹配锚窗口，捕获槽位值。

        锚窗口按证据量从大到小逐级收缩重试（先左窗后右锚）：
        变体在窗口内插词/删词/调序时，用仍然完整的局部上下文
        定位。左窗优先于右锚收缩——左窗携带参数名与结构词
        （语义上下文），右锚多为收尾标点。任一级唯一匹配即返回；
        某级歧义（>1 匹配）时更短的同侧窗口必然同样歧义（匹配集
        随窗口收缩单调扩大），收缩另一侧重试；全部级别无唯一
        匹配 -> None（保守拒判）。

        复合形态（子串值）允许收缩到零窗口：token 前后缀本身即
        定位证据（如 QosFillVolMsgBuf+ 前缀）。

        内容词约束：生效左窗必须含至少一个内容 token（字母/
        数字）。纯标点窗口（如 `(` + `)`）不携带参数名或结构词
        语义——易混淆日志（参数名被替换、标点骨架保留，如
        `ctx (1)` 对 `ret ( <NUM> )`）会因此在错误字段上取值，
        必须拒判。左窗内容随收缩单调减少，无内容即终止。
        """
        compound_evidence = (self.sub_token
                             and bool(_CONTENT_RE.search(
                                 self.prefix + self.suffix)))
        for L in range(len(self.left), -1, -1):
            left_slice = self.left[len(self.left) - L:] if L else ()
            if not (_has_content(left_slice) or compound_evidence):
                break   # 空窗/纯标点窗无内容词：更短左窗必同，终止
            for R in range(len(self.right), -1, -1):
                if L + R == 0 and not compound_evidence:
                    break
                matches = self._scan(tokens, spans, L, R)
                if len(matches) == 1:
                    c, val, sp = matches[0]
                    return LocatedSlot(value=val, span=sp,
                                       token_index=c,
                                       sub_token=self.sub_token)
                if len(matches) > 1:
                    break   # 同左窗下更短右锚必歧义，收缩左窗重试
        return None

    def _scan(self, tokens, spans, L: int, R: int):
        """以左窗 tokens[-L:] + 捕获 + 右锚 tokens[:R] 扫描。

        左窗保留靠近槽位的后缀（完整窗口的真后缀），模板占位符
        位作单 token 通配。返回匹配列表（可能为空/多个）。
        """
        left = self.left[len(self.left) - L:] if L else ()
        right = self.right[:R]
        matches: List[Tuple[int, str, Tuple[int, int]]] = []
        n = len(tokens)
        for s in range(0, n - L):
            c = s + L
            if c + R > n - 1:
                break
            ok = True
            for k, w in enumerate(left):
                if w is not None and tokens[s + k] != w:
                    ok = False
                    break
            if not ok:
                continue
            for k, w in enumerate(right):
                if tokens[c + 1 + k] != w:
                    ok = False
                    break
            if not ok:
                continue
            cap = tokens[c]
            if self.sub_token:
                if self.prefix and not cap.startswith(self.prefix):
                    continue
                if self.suffix and not cap.endswith(self.suffix):
                    continue
                val = cap[len(self.prefix):
                          len(cap) - len(self.suffix)] \
                    if self.suffix else cap[len(self.prefix):]
                if not val:
                    continue
                # 子串 span：token 区间内按前后缀收缩
                sp = (spans[c][0] + len(self.prefix),
                      spans[c][1] - len(self.suffix))
            else:
                val = cap
                sp = spans[c]
            matches.append((c, val, sp))
        return matches

def compile_slot_anchor(template_text: str, param_pos,
                        param_name: Optional[str] = None
                        ) -> Optional[SlotAnchor]:
    """从规则组模板 + param_pos 编译槽位定位器。

    编译期校验（不满足 -> None，该规则退回纯名字驱动链路）：
      - param_pos 可解析为 int 且落在模板 token 序列内；
      - 模板 token[param_pos] 为纯占位符或"前缀/后缀 + 占位符"
        复合形态（无占位符 -> param_pos 不指向取值槽位）；
      - 括号槽位规范化：param_pos 指向括号 token 且内侧紧邻
        占位符时前进/后退到占位符（值在括号内的配置坐标约定，
        与值侧 resolve_target_value 的成对括号裁剪同一语义；
        审计记录的 vbs|4029 坐标缺陷由此覆盖，不修改被治理的
        规则配置）；
      - 参数名归属：名字任一词须出现在锚窗口（左窗或右窗）——
        槽位属于该参数的结构证据，param_pos 标错到别的参数
        槽位时编译失败（运行时防护）。
    """
    try:
        pos = int(param_pos)
    except (TypeError, ValueError):
        return None
    ttpl = template_tokens_for_alignment(template_text or "")
    if not (0 <= pos < len(ttpl)):
        return None
    slot_tok = ttpl[pos]
    # ret=[4]、vbs|4029 sig=[11]），槽位侧同一语义：括号不是值，
    # '[' 上，值 <NUM> 在 17）由此通用规范化覆盖，不修改被
    # 治理的规则配置。
    for _ in range(2):   # 至多两层括号
        if slot_tok in ("[", "(") and pos + 1 < len(ttpl) \
                and _PURE_PH_RE.match(ttpl[pos + 1]):
            pos += 1
            slot_tok = ttpl[pos]
        elif slot_tok in ("]", ")") and pos - 1 >= 0 \
                and _PURE_PH_RE.match(ttpl[pos - 1]):
            pos -= 1
            slot_tok = ttpl[pos]
        else:
            break
    prefix = suffix = ""
    sub_token = False
    if not _PURE_PH_RE.match(slot_tok):
        m = _COMPOUND_PH_RE.match(slot_tok)
        if m is None:
            return None
        prefix, suffix = m.group("prefix"), m.group("suffix")
        if not prefix and not suffix:
            return None
        sub_token = True

    left: List[Optional[str]] = []
    i = pos - 1
    while i >= 0 and len(left) < _LEFT_MAX:
        t = ttpl[i]
        left.append(None if _PURE_PH_RE.match(t) else t)
        i -= 1
    left.reverse()
    # 遇占位符即停（占位符右侧内容不定位）
    right: List[str] = []
    j = pos + 1
    while j < len(ttpl) and len(right) < _RIGHT_MAX:
        t = ttpl[j]
        if _PURE_PH_RE.match(t):
            break
        right.append(t)
        j += 1
    # 括号规范化后的槽位也须通过（错位 param_pos 指向其它参数
    if not (_name_in_window(param_name, left)
            or _name_in_window(param_name, right)):
        return None
    return SlotAnchor(param_pos=pos, left=tuple(left),
                      right=tuple(right), prefix=prefix,
                      suffix=suffix, sub_token=sub_token)


from .lexical_matcher import _KNOWN_PH

_PH_SUB_RE = re.compile(
    "|".join(re.escape(p) for p in _KNOWN_PH), re.IGNORECASE)

_INSTANTIATE = {
    "<num>": "7",
    "<hex0x>": "0x1a",
    "<*>": "x1",
}

def _instantiate(template_text: str) -> str:
    def _repl(m):
        return _INSTANTIATE.get(m.group(0).lower(), "x1")
    return _PH_SUB_RE.sub(_repl, template_text)

def _expected_slot_value(slot_token: str) -> str:
    """槽位 token（纯占位符或复合形态）的实例化值。"""
    ph = re.search(r"<[^<>]+>", slot_token)
    return _INSTANTIATE.get(ph.group(0).lower(), "x1") if ph else ""

def selfcheck_rule(template_raw: str, param_pos, param_name=None) -> dict:
    """规则接入自检：模板实例化日志上的槽位定位器验证。

    用确定性占位符实例化得到规范日志，验证四点：
      1. 编译通过（param_pos 指向占位符/复合槽位，模板坐标
         与配置坐标一致）；
      2. 锚窗口在规范日志上唯一匹配；
      3. 捕获下标 == param_pos（规范坐标 1:1 对齐不变式）；
      4. 捕获值 == 槽位实例化值（复合形态验证前后缀切分）。

    失败说明 param_pos 标定或模板本身有问题，该规则应标记
    "槽位路径不可用"（退回名字驱动链路，行为不变）。通过不
    代表真实噪声下的正确性（由评测保证）。不依赖样例日志，
    可在规则加载/接入时零成本运行。
    """
    anchor = compile_slot_anchor(template_raw, param_pos,
                                 param_name=param_name)
    if anchor is None:
        return {"ok": False, "reason": "compile_failed",
                "param_pos": param_pos}
    raw = _instantiate(template_raw)
    tokens, spans = tokenize_for_params_with_spans(raw)
    located = anchor.locate(tokens, spans)
    if located is None:
        return {"ok": False, "reason": "locate_failed_on_canonical",
                "param_pos": anchor.param_pos}
    if located.token_index != anchor.param_pos:
        return {"ok": False, "reason": "capture_index_mismatch",
                "param_pos": anchor.param_pos,
                "captured_at": located.token_index,
                "value": located.value}
    ttpl = template_tokens_for_alignment(template_raw)
    expect = _expected_slot_value(ttpl[anchor.param_pos])
    if located.value != expect:
        return {"ok": False, "reason": "value_mismatch",
                "param_pos": anchor.param_pos,
                "value": located.value, "expect": expect}
    return {"ok": True, "reason": None, "param_pos": anchor.param_pos,
            "value": located.value, "sub_token": located.sub_token}

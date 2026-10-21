"""位置映射与结构签名。"""

import re
from typing import Any, Dict, List, Optional, Set, Tuple

from .contracts import (
    ExtractedParameter, POSITION_NOT_REPRESENTABLE,
    POSITION_SCHEMA_LEGACY_TOKENS, RuleGroup, RuleRecord,
    TARGET_MISSING)

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


def resolve_target_value(parameter: ExtractedParameter
                         ) -> Tuple[str, Optional[Tuple[int, int]]]:
    """规则目标取值：成对 [ ] 包裹时裁剪内部值并同步调整 span。

    实测形态：eds/10310 ret=[4] -> 取值结果 '[4]'；vbs/4029
    sig = [11] -> '[11]'。裁剪后与旧 param_pos token 语义一致。
    """
    v = parameter.value_raw
    span = parameter.value_span
    if (len(v) >= 2 and v.startswith("[") and v.endswith("]")
            and "[" not in v[1:-1] and "]" not in v[1:-1] and v[1:-1]):
        inner = v[1:-1]
        if span is not None:
            return inner, (span[0] + 1, span[1] - 1)
        return inner, None
    return v, span


def covering_token(spans: List[Tuple[int, int]],
                   value_span: Tuple[int, int]) -> Optional[int]:
    """找唯一准确覆盖 value 的 token（token 区间完整包含 value）。"""
    s, e = value_span
    hits = [i for i, (ts, te) in enumerate(spans) if ts <= s and e <= te]
    if len(hits) == 1:
        return hits[0]
    return None

def overlapping_tokens(spans, value_span) -> List[int]:
    s, e = value_span
    return [i for i, (ts, te) in enumerate(spans)
            if ts < e and s < te]


def align_template(template_tokens: List[str], log_tokens: List[str],
                   limit: int = 2) -> List[List[List[int]]]:
    """模板 token -> 覆盖的 log token 下标列表。

    固定 token（非 <*>）精确匹配 1 个 log token；<*>
    吸收 >=1 个连续 log token。回溯搜索最多 limit 个解；
    达到 limit 视为歧义，返回 []（调用方保守处理）。
    """
    solutions: List[List[List[int]]] = []
    n_t, n_l = len(template_tokens), len(log_tokens)

    def bt(ti: int, li: int, acc: List[List[int]]):
        if len(solutions) >= limit:
            return
        if ti == n_t:
            if li == n_l:
                solutions.append([list(x) for x in acc])
            return
        tok = template_tokens[ti]
        if tok == "<*>":
            for width in range(1, n_l - li + 1):
                acc.append(list(range(li, li + width)))
                bt(ti + 1, li + width, acc)
                acc.pop()
                if len(solutions) >= limit:
                    return
        else:
            if li < n_l and log_tokens[li] == tok:
                acc.append([li])
                bt(ti + 1, li + 1, acc)
                acc.pop()

    bt(0, 0, [])
    if len(solutions) >= limit:
        return []
    return solutions

def fixed_token_positions(template_tokens: List[str], log_tokens: List[str],
                          limit: int = 2) -> Optional[Set[int]]:
    """log token 下标中由模板固定 token 覆盖的位置集合。

    解唯一 -> 该解的固定位置；多解（通配边界歧义）-> 取各解
    固定位置的**交集**——歧义只发生在 <*> 边界划分，固定 token
    在全部解中位置一致时仍是安全锚点（合并解析模板
    `A B <*> <*> <*>` 的家族事件因此可登记：前缀固定 token
    在每个解中位置相同）；解数超过枚举上限或无解 -> None
    （保守，不缓存）。
    """
    max_solutions = 32
    sols = align_template(template_tokens, log_tokens,
                          limit=max_solutions)
    if not sols:
        return None
    fixed: Optional[Set[int]] = None
    saw_fixed = False
    for sol in sols:
        cur: Set[int] = set()
        for t_tok, idxs in zip(template_tokens, sol):
            if t_tok != "<*>":
                cur.update(idxs)
        if cur:
            saw_fixed = True
        fixed = cur if fixed is None else (fixed & cur)
    # 存在固定 token 但各解位置不一致（交集为空）-> 不可锚定
    if saw_fixed and not fixed:
        return None
    # 空集合法：模板无固定 token（调用方按无锚点不缓存）
    return fixed


class RulePositionResult:
    def __init__(self, rule: RuleRecord, ok: bool,
                 position: Optional[int] = None,
                 value: Optional[str] = None,
                 reason: Optional[str] = None,
                 left_anchor: Optional[Tuple[int, str]] = None,
                 right_anchor: Optional[Tuple[int, str]] = None):
        self.rule = rule
        self.ok = ok
        self.position = position
        self.value = value
        self.reason = reason
        self.left_anchor = left_anchor    # (log_token_index, token)
        self.right_anchor = right_anchor

    def as_dict(self) -> Dict[str, Any]:
        return {"rule_id": self.rule.rule_id, "ok": self.ok,
                "position": self.position, "value": self.value,
                "reason": self.reason,
                "left_anchor": self.left_anchor,
                "right_anchor": self.right_anchor}

class PositionMappingResult:
    def __init__(self, rule_results: List[RulePositionResult],
                 fixed_positions: Optional[Set[int]],
                 tokens: List[str],
                 template_tokens: List[str]):
        self.rule_results = rule_results
        self.fixed_positions = fixed_positions
        self.tokens = tokens
        self.template_tokens = template_tokens

    @property
    def n_tokens(self) -> int:
        return len(self.tokens)

    @property
    def all_ok(self) -> bool:
        return bool(self.rule_results) and all(r.ok for r
                                               in self.rule_results)

    def positions(self) -> Dict[str, int]:
        return {r.rule.rule_id: r.position for r in self.rule_results
                if r.ok}

    def reasons(self) -> Dict[str, str]:
        return {r.rule.rule_id: r.reason for r in self.rule_results
                if not r.ok}

def _find_param_name_token(tokens: List[str], param_name: str,
                           before_pos: int,
                           max_lookback: int = 2) -> Optional[int]:
    """在 value 位置之前找包含 param_name 的最近 token。

    验证配对正确性：value 应紧跟在包含参数名的 token 之后（跳过
    分隔符，最多回看 max_lookback 个非分隔符 token）。距离更远或
    找不到参数名 token，说明 参数名被配到了错误的值
    （如 code=10 extra_26: 28 中 code 配到 28）。
    多词参数名（error code / format flag）按词拆分，任一词命中
    即算（单词无法包含完整多词名）。
    """
    words = [w for w in param_name.lower().split() if len(w) >= 2]
    if not words:
        words = [param_name.lower()]
    delims = {'=', ':', '(', ')', '[', ']', ','}
    seen = 0
    for i in range(before_pos - 1, -1, -1):
        tok = tokens[i]
        if tok in delims:
            continue
        seen += 1
        low = tok.lower()
        if any(w in low for w in words):
            return i
        if seen >= max_lookback:
            return None
    return None

def _map_single_rule(rule, value, vspan, tokens, spans, fixed,
                     skip_name_check: bool = False
                     ) -> RulePositionResult:
    """单规则单候选值（正则）的完整映射链。

    skip_name_check: 候选值来自模板槽位锚定（位置由 param_pos
    编译的锚窗口唯一匹配确定）时，跳过参数名邻近校验——方向性
    结构中正确的名字-值对距离天然超出邻近门（osd|672 实测：
    名字驱动配对正确而被 lookback<=2 拒绝）；锚窗口已包含参数名
    上下文，配对正确性由模板对齐保证。
    """
    pos = covering_token(spans, vspan)
    if pos is None:
        overlap = overlapping_tokens(spans, vspan)
        return RulePositionResult(
            rule, False, value=value,
            reason=POSITION_NOT_REPRESENTABLE
            if len(overlap) > 1 else "VALUE_NOT_ALIGNED")
    if tokens[pos] != value:
        return RulePositionResult(
            rule, False, value=value, reason="REPLAY_MISMATCH")
    # 配对验证：参数名 token 须在值前方邻近（防 误配对，
    # 如 s 配到 ret 的值）
    if not skip_name_check and _find_param_name_token(
            tokens, rule.param_name, pos) is None:
        return RulePositionResult(
            rule, False, value=value, position=pos,
            reason="PAIRING_MISMATCH")
    if fixed is None:
        return RulePositionResult(
            rule, False, value=value, position=pos,
            reason="TEMPLATE_ALIGN_AMBIGUOUS")
    lefts = [f for f in fixed if f < pos]
    rights = [f for f in fixed if f > pos]
    if not lefts and not rights:
        return RulePositionResult(
            rule, False, value=value, position=pos,
            reason="ANCHOR_NOT_FIXED")
    left = (max(lefts), tokens[max(lefts)]) if lefts else None
    right = (min(rights), tokens[min(rights)]) if rights else None
    return RulePositionResult(
        rule, True, position=pos, value=value,
        left_anchor=left, right_anchor=right)

def map_rule_positions(
        group: RuleGroup,
        parameters: Dict[str, ExtractedParameter],
        tokens: List[str],
        spans: List[Tuple[int, int]],
        template_text: str,
        fallback_locator=None,
        slot_candidates: Optional[Dict[str, Tuple[str, Tuple[int, int]]]]
        = None,
) -> PositionMappingResult:
    """冷路径位置映射（§7.1）。

    每条规则按候选值依次尝试完整映射链：目标值（span 找准确
    覆盖 token -> 回放一致 -> 配对验证 -> 模板对齐锚点）；失败
    时若提供 fallback_locator(param_name) -> (value, span)，用
    正则定位的值重试同一链（缺参数/误配对时登记路径与
    判定路径的正则兜底语义一致）。slot_candidates 提供模板槽位
    锚定候选（rule_id -> (value, span)，来自 param_pos 编译的
    锚窗口唯一匹配）：跳过名字邻近校验（配对由模板对齐保证），
    优先于正则候选。全部失败记录首个失败原因。
    """
    tpl_tokens = template_tokens_for_alignment(template_text)
    fixed = fixed_token_positions(tpl_tokens, tokens)
    results: List[RulePositionResult] = []
    for rule in group.rules:
        parameter = parameters.get(rule.param_name)
        # 候选: (value, span, skip_name_check)
        candidates: List[Tuple[str, Optional[Tuple[int, int]], bool]] = []
        if parameter is not None:
            value, vspan = resolve_target_value(parameter)
            if vspan is not None:
                candidates.append((value, vspan, False))
            elif value is not None:
                candidates.append((value, None, False))
        if slot_candidates and rule.rule_id in slot_candidates:
            sv, sspan = slot_candidates[rule.rule_id]
            candidates.append((sv, sspan, True))
        if fallback_locator is not None:
            fb = fallback_locator(rule.param_name)
            if fb is not None:
                candidates.append((fb[0], fb[1], False))
        result: Optional[RulePositionResult] = None
        for value, vspan, skip_name in candidates:
            if vspan is None:
                r = RulePositionResult(
                    rule, False, value=value,
                    reason="VALUE_SPAN_MISSING")
            else:
                r = _map_single_rule(rule, value, vspan, tokens,
                                     spans, fixed,
                                     skip_name_check=skip_name)
            if r.ok:
                result = r
                break
            if result is None:
                result = r
        if result is None:
            result = RulePositionResult(
                rule, False, reason=TARGET_MISSING)
        results.append(result)
    return PositionMappingResult(results, fixed, tokens, tpl_tokens)

def compile_structural_signature(
        mapping: PositionMappingResult) -> Dict[str, Any]:
    """结构签名（legacy_tokens_v1）：token 宽度 + 每目标固定锚点。"""
    targets = {}
    for r in mapping.rule_results:
        if not r.ok:
            continue
        targets[r.rule.rule_id] = {
            "position": r.position,
            "left_anchor": {"index": r.left_anchor[0],
                            "token": r.left_anchor[1]}
            if r.left_anchor else None,
            "right_anchor": {"index": r.right_anchor[0],
                             "token": r.right_anchor[1]}
            if r.right_anchor else None,
        }
    return {
        "schema": POSITION_SCHEMA_LEGACY_TOKENS,
        "n_tokens": mapping.n_tokens,
        "template_tokens": list(mapping.template_tokens),
        "targets": targets,
    }

def verify_structural_signature(
        signature: Dict[str, Any],
        tokens: List[str]) -> Tuple[bool, Optional[str]]:
    """热路径签名校验：token 宽度 + 固定锚点下标/文本一致。"""
    if signature.get("schema") != POSITION_SCHEMA_LEGACY_TOKENS:
        return False, "schema_mismatch"
    if len(tokens) != signature.get("n_tokens"):
        return False, "token_width_changed"
    for rid, t in (signature.get("targets") or {}).items():
        p = t.get("position")
        if p is None or not (0 <= p < len(tokens)):
            return False, f"{rid}:position_out_of_range"
        for side in ("left_anchor", "right_anchor"):
            a = t.get(side)
            if a is None:
                continue
            idx, tok = a.get("index"), a.get("token")
            if not (0 <= idx < len(tokens)) or tokens[idx] != tok:
                return False, f"{rid}:{side}_changed"
    return True, None

"""词法匹配器。"""

import hashlib
import re
from typing import Dict, List, Optional, Tuple

from .contracts import (
    LOW_SIMILARITY, MODULE_UNAVAILABLE, NO_CANDIDATE,
    REPRESENTATION_UNAVAILABLE, RuleGroup, SemanticMatchCandidate,
    SemanticMatchEvidence, SemanticMatchOutcome, TemplateVariant)
from .position_mapper import (
    tokenize_for_params)
from .utils import (
    check_protected_gate)

# ---- N1 预处理（所有方法共用） ----
_N1_RE = re.compile(r"-->|[a-z0-9_]+|[+\-]")
_KNOWN_PH = ["<num>", "<*>", "<hex0x>", "<mac-hex>", "<hex4>",
             "<mac>", "<ip>", "<cmd>"]
_PH_SUB = re.compile("|".join(re.escape(p) for p in _KNOWN_PH))
_VALUE_RE = re.compile(r"^(?:\d+|0x[0-9a-f]+|[0-9a-f]{4,})$")

DEFAULT_THRESHOLD = 0.50
DEFAULT_METHOD = "minhash"

def _n1_tokens(text: str) -> List[str]:
    s = (text or "").casefold()
    s = _PH_SUB.sub(" ", s)
    return _N1_RE.findall(s)

def _strip_values(tokens: List[str]) -> List[str]:
    return [t for t in tokens if not _VALUE_RE.match(t)]

def event_fingerprint(raw_text: str) -> str:
    toks = _strip_values(_n1_tokens(raw_text or ""))
    return hashlib.sha256(
        " ".join(toks).encode()).hexdigest()[:16]

# ---- 评分函数 ----

def _fx_hash(token: str) -> int:
    h = 0
    for byte in token.encode("utf-8"):
        h = ((h << 5) | (h >> 59)) & 0xFFFFFFFFFFFFFFFF
        h ^= byte
        h = (h * 0x517cc1b727220a95) & 0xFFFFFFFFFFFFFFFF
    return h

_GOLDEN = 0x9e3779b97f4a7c15
_SILVER = 0x6a09e667f3bcc909
_MASK64 = (1 << 64) - 1

def _minhash_signature(tokens: List[str], size: int = 128) -> List[int]:
    base = sorted(set(_fx_hash(t) for t in tokens))
    sig = []
    for i in range(size):
        a = (i * _GOLDEN) & _MASK64 | 1
        c = (i * _SILVER) & _MASK64
        min_val = min((x * a + c) & _MASK64 for x in base) \
            if base else _MASK64
        sig.append(min_val)
    return sig

def _minhash_jaccard(sig_a: List[int], sig_b: List[int]) -> float:
    if not sig_a or len(sig_a) != len(sig_b):
        return 0.0
    agree = sum(1 for a, b in zip(sig_a, sig_b) if a == b)
    return agree / len(sig_a)

# 匹配器

class _Entry:
    __slots__ = ("entry_key", "module", "group_id",
                 "source_template_id", "variant_id",
                 "token_set", "token_counts", "n_tokens",
                 "repr_text", "signature",
                 "protected_context")

    def __init__(self, entry_key, module, group_id,
                 source_template_id, variant_id,
                 token_set, token_counts, n_tokens, repr_text,
                 signature, protected_context=frozenset()):
        self.entry_key = entry_key
        self.module = module
        self.group_id = group_id
        self.source_template_id = source_template_id
        self.variant_id = variant_id
        self.token_set = token_set
        self.token_counts = token_counts
        self.n_tokens = n_tokens
        self.repr_text = repr_text
        self.signature = signature
        self.protected_context = protected_context

class LexicalMultisetMatcher:
    """多策略模板匹配器。

    scoring_method:
      - "minhash": MinHash 128-permutation Jaccard 估计

    tau: 各方法需独立校准阈值。
    """

    def __init__(self, tau: float = DEFAULT_THRESHOLD,
                 scoring_method: str = DEFAULT_METHOD,
                 strip_query_values: bool = True,
                 top_k: int = 5,
                 minhash_size: int = 128,
                 tau_raw: float = 0.8):

        self.tau = tau
        self.scoring_method = scoring_method
        self.strip_query_values = strip_query_values
        self.top_k = top_k
        self.minhash_size = minhash_size
        self.tau_raw = tau_raw
        self._registered: Dict[str, Dict[str, dict]] = {}
        self._prepared: Dict[str, List[_Entry]] = {}
        self._groups: Dict[str, RuleGroup] = {}
        self._dirty = True
        self.index_version = 0

    def register_group(self, group: RuleGroup,
                       semantic_text: Optional[str] = None) -> None:
        self._register(group.module, group.group_id,
                       group.source_template_id, None,
                       group.template_raw,
                       f"group:{group.group_id}",
                       protected_context=group.protected_context)

    def register_variant(self, variant: TemplateVariant,
                         group: RuleGroup) -> None:
        self._register(variant.module, variant.group_id,
                       variant.matched_template_id, variant.variant_id,
                       variant.template_text,
                       f"variant:{variant.module}:"
                       f"{variant.template_fingerprint}:"
                       f"{variant.event_fingerprint}",
                       protected_context=group.protected_context)

    def _register(self, module, group_id, source_template_id,
                  variant_id, template_text, entry_key,
                  protected_context=frozenset()):
        toks = _n1_tokens(template_text)
        if self.strip_query_values:
            toks = _strip_values(toks)
        if not toks:
            return
        mod = self._registered.setdefault(module, {})
        if entry_key not in mod:
            self.index_version += 1
        mod[entry_key] = {
            "group_id": group_id,
            "source_template_id": source_template_id,
            "variant_id": variant_id,
            "template_text": template_text, "tokens": toks,
            "protected_context": frozenset(protected_context)}
        self._dirty = True

    def _ensure_prepared(self):
        if not self._dirty:
            return
        self._prepared = {}
        for module, items in self._registered.items():
            entries = []
            for key, info in items.items():
                toks = info["tokens"]
                ts = frozenset(toks)
                counts: Dict[str, int] = {}
                for t in toks:
                    counts[t] = counts.get(t, 0) + 1
                sig = _minhash_signature(toks, self.minhash_size) \
                    if self.scoring_method == "minhash" else None
                entries.append(_Entry(
                    key, module, info["group_id"],
                    info["source_template_id"], info["variant_id"],
                    ts, counts, len(toks), " ".join(toks),
                    sig,
                    info.get("protected_context", frozenset())))
            self._prepared[module] = entries
        self._dirty = False

    def group(self, group_id: str) -> Optional[RuleGroup]:
        return self._groups.get(group_id)

    def entries(self, module: Optional[str] = None) -> List:
        self._ensure_prepared()
        if module is None:
            return sum(self._prepared.values(), [])
        return list(self._prepared.get(module, []))

    def bind_groups(self, groups: List[RuleGroup]) -> None:
        for g in groups:
            self._groups[g.group_id] = g

    def _score(self, module: str, q_toks: List[str],
               q_set: frozenset, q_counts: Dict[str, int],
               q_sig: Optional[List[int]],
               e: _Entry) -> float:
        if q_sig is None:
            return 0.0
        return _minhash_jaccard(q_sig, e.signature)

    def match(self, module: str, template_text: Optional[str],
              raw_text: Optional[str] = None
              ) -> SemanticMatchOutcome:
        evidence = SemanticMatchEvidence(
            tau=self.tau, index_version=self.index_version,
            encoder_revision=self.scoring_method)
        if not module:
            return SemanticMatchOutcome(False, MODULE_UNAVAILABLE,
                                        evidence=evidence)
        self._ensure_prepared()
        cands = self._prepared.get(module, [])
        if not cands:
            return SemanticMatchOutcome(False, NO_CANDIDATE,
                                        evidence=evidence)
        if not template_text:
            return SemanticMatchOutcome(
                False, REPRESENTATION_UNAVAILABLE, evidence=evidence)

        q_toks = _n1_tokens(template_text)
        if self.strip_query_values:
            q_toks = _strip_values(q_toks)
        if not q_toks:
            return SemanticMatchOutcome(
                False, REPRESENTATION_UNAVAILABLE, evidence=evidence)
        q_set = frozenset(q_toks)
        q_counts: Dict[str, int] = {}
        for t in q_toks:
            q_counts[t] = q_counts.get(t, 0) + 1
        q_sig = _minhash_signature(q_toks, self.minhash_size) \
            if self.scoring_method == "minhash" else None
        evidence.representation = " ".join(q_toks)

        gate_tokens = tokenize_for_params(raw_text) \
            if raw_text is not None else q_toks
        scored = []
        for e in cands:
            if e.protected_context and not check_protected_gate(
                    e.protected_context, gate_tokens):
                continue
            s = self._score(module, q_toks, q_set, q_counts,
                            q_sig, e)
            if s > 0:
                scored.append(SemanticMatchCandidate(
                    entry_key=e.entry_key, module=module,
                    group_id=e.group_id,
                    source_template_id=e.source_template_id,
                    variant_id=e.variant_id, score=float(s)))
        if not scored:
            return SemanticMatchOutcome(False, NO_CANDIDATE,
                                        evidence=evidence)
        scored.sort(key=lambda c: (-c.score, c.entry_key))
        evidence.candidates = [c.as_dict()
                               for c in scored[:self.top_k]]
        evidence.best_score = scored[0].score
        evidence.second_score = scored[1].score \
            if len(scored) > 1 else None
        if scored[0].score < self.tau:
            return SemanticMatchOutcome(False, LOW_SIMILARITY,
                                        evidence=evidence)
        group = self._groups.get(scored[0].group_id)
        if not isinstance(group, RuleGroup):
            return SemanticMatchOutcome(False, NO_CANDIDATE,
                                        evidence=evidence)
        return SemanticMatchOutcome(
            True, None, group=group,
            matched_variant_id=scored[0].variant_id,
            score=scored[0].score, evidence=evidence)

    def match_raw(self, module: str,
                  raw_text: Optional[str]) -> SemanticMatchOutcome:
        """原文臂：以原文剥离 token 的包含度路由。

        评分 = containment = |原文token ∩ 候选token| / |候选token|
        （候选模板的业务词被原文覆盖的比例）。tie-break 用候选
        特异性（token 数多者优先）：家族模板互相包含（10304 ⊃
        10305）时，包含度同分，特异性选择最具体（token 最全）的
        组——原文包含 s 字段则归属 10304 而非其子集 10305。

        背景：解析器可能把区分性 token 泛化进 <*>（家族模板在
        模板臂不可分）；多字段日志相对极简组模板得分被稀释。
        原文是权威信号，解析模板是有损投影。校准（ind1100
        数据集）：被模板臂拒绝的已知样本 29/33 找回正确组、
        0 错路由，未知 60 条 0 误接受。evidence.representation
        以 raw: 前缀标记。
        """
        evidence = SemanticMatchEvidence(
            tau=self.tau_raw, index_version=self.index_version,
            encoder_revision=f"{self.scoring_method}+raw_containment")
        if not module:
            return SemanticMatchOutcome(False, MODULE_UNAVAILABLE,
                                        evidence=evidence)
        self._ensure_prepared()
        cands = self._prepared.get(module, [])
        if not cands:
            return SemanticMatchOutcome(False, NO_CANDIDATE,
                                        evidence=evidence)
        if not raw_text:
            return SemanticMatchOutcome(
                False, REPRESENTATION_UNAVAILABLE, evidence=evidence)
        q_toks = _n1_tokens(raw_text)
        if self.strip_query_values:
            q_toks = _strip_values(q_toks)
        if not q_toks:
            return SemanticMatchOutcome(
                False, REPRESENTATION_UNAVAILABLE, evidence=evidence)
        q_set = frozenset(q_toks)
        evidence.representation = "raw: " + " ".join(q_toks)

        # 是规则组的权威定义
        gate_tokens = tokenize_for_params(raw_text)
        scored = []
        for e in cands:
            if not e.token_set or not e.entry_key.startswith("group:"):
                continue
            if e.protected_context and not check_protected_gate(
                    e.protected_context, gate_tokens):
                continue
            s = len(q_set & e.token_set) / len(e.token_set)
            if s > 0:
                scored.append((s, len(e.token_set), e))
        if not scored:
            return SemanticMatchOutcome(False, NO_CANDIDATE,
                                        evidence=evidence)
        scored.sort(key=lambda t: (-t[0], -t[1], t[2].entry_key))
        evidence.candidates = [
            SemanticMatchCandidate(
                entry_key=e.entry_key, module=module,
                group_id=e.group_id,
                source_template_id=e.source_template_id,
                variant_id=e.variant_id,
                score=float(s)).as_dict()
            for s, _, e in scored[:self.top_k]]
        evidence.best_score = scored[0][0]
        evidence.second_score = scored[1][0] \
            if len(scored) > 1 else None
        if scored[0][0] < self.tau_raw:
            return SemanticMatchOutcome(False, LOW_SIMILARITY,
                                        evidence=evidence)
        group = self._groups.get(scored[0][2].group_id)
        if not isinstance(group, RuleGroup):
            return SemanticMatchOutcome(False, NO_CANDIDATE,
                                        evidence=evidence)
        return SemanticMatchOutcome(
            True, None, group=group,
            matched_variant_id=scored[0][2].variant_id,
            score=scored[0][0], evidence=evidence)

PARSER_NOTE = ("词法双臂匹配器（MinHash）；"
               "各方法需独立校准阈值。")

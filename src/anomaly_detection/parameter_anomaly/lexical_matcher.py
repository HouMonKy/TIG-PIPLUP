"""词法匹配器（双臂：原文包含度 + 模板 Jaccard）。"""

import re
from typing import Dict, List, Optional

from .contracts import (
    LOW_SIMILARITY, MODULE_UNAVAILABLE, NO_CANDIDATE,
    REPRESENTATION_UNAVAILABLE, RuleGroup, SemanticMatchCandidate,
    SemanticMatchEvidence, SemanticMatchOutcome)
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

def _n1_tokens(text: str) -> List[str]:
    s = (text or "").casefold()
    s = _PH_SUB.sub(" ", s)
    return _N1_RE.findall(s)

def _strip_values(tokens: List[str]) -> List[str]:
    return [t for t in tokens if not _VALUE_RE.match(t)]

# 匹配器

class _Entry:
    __slots__ = ("entry_key", "module", "group_id",
                 "source_template_id",
                 "token_set", "protected_context")

    def __init__(self, entry_key, module, group_id,
                 source_template_id,
                 token_set, protected_context=frozenset()):
        self.entry_key = entry_key
        self.module = module
        self.group_id = group_id
        self.source_template_id = source_template_id
        self.token_set = token_set
        self.protected_context = protected_context

class LexicalMultisetMatcher:
    """双臂模板匹配器。

    - match_raw（原文包含度臂）：候选模板业务词被原文覆盖的
      比例，仅对规则组模板（权威定义）打分；
    - match（模板 Jaccard 臂）：剥值 token 集合的精确 Jaccard，
      对规则组与知识库静态变体模板全量打分。

    tau / tau_raw 各臂独立校准阈值。
    """

    def __init__(self, tau: float = DEFAULT_THRESHOLD,
                 strip_query_values: bool = True,
                 top_k: int = 5,
                 tau_raw: float = 0.8):

        self.tau = tau
        self.strip_query_values = strip_query_values
        self.top_k = top_k
        self.tau_raw = tau_raw
        self._registered: Dict[str, Dict[str, dict]] = {}
        self._prepared: Dict[str, List[_Entry]] = {}
        self._groups: Dict[str, RuleGroup] = {}
        self._dirty = True

    def register_group(self, group: RuleGroup,
                       semantic_text: Optional[str] = None) -> None:
        self._register(group.module, group.group_id,
                       group.source_template_id,
                       group.template_raw,
                       f"group:{group.group_id}",
                       protected_context=group.protected_context)

    def register_routed_template(self, group: RuleGroup,
                                 template_text: str) -> None:
        """知识库静态变体模板：与规则组模板同为匹配候选。"""
        self._register(group.module, group.group_id,
                       group.source_template_id, template_text,
                       f"routed:{group.module}:{template_text}",
                       protected_context=group.protected_context)

    def _register(self, module, group_id, source_template_id,
                  template_text, entry_key,
                  protected_context=frozenset()):
        toks = _n1_tokens(template_text)
        if self.strip_query_values:
            toks = _strip_values(toks)
        if not toks:
            return
        mod = self._registered.setdefault(module, {})
        mod[entry_key] = {
            "group_id": group_id,
            "source_template_id": source_template_id,
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
                entries.append(_Entry(
                    key, module, info["group_id"],
                    info["source_template_id"],
                    frozenset(info["tokens"]),
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

    def _score(self, q_set: frozenset, e: _Entry) -> float:
        union = q_set | e.token_set
        if not union:
            return 0.0
        return len(q_set & e.token_set) / len(union)

    def match(self, module: str, template_text: Optional[str],
              raw_text: Optional[str] = None
              ) -> SemanticMatchOutcome:
        """模板臂：剥值 token 集合的精确 Jaccard。"""
        evidence = SemanticMatchEvidence(tau=self.tau)
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
        evidence.representation = " ".join(q_toks)

        gate_tokens = tokenize_for_params(raw_text) \
            if raw_text is not None else q_toks
        scored = []
        for e in cands:
            if e.protected_context and not check_protected_gate(
                    e.protected_context, gate_tokens):
                continue
            s = self._score(q_set, e)
            if s > 0:
                scored.append(SemanticMatchCandidate(
                    entry_key=e.entry_key, module=module,
                    group_id=e.group_id,
                    source_template_id=e.source_template_id,
                    score=float(s)))
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
        evidence = SemanticMatchEvidence(tau=self.tau_raw)
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

        # 规则组模板是权威定义
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
            score=scored[0][0], evidence=evidence)

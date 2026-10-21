"""M1-M4 header boundary detectors (rebuilt V1 baseline).

The original V1 implementation is lost (commit ac30623d missing).  These
classes re-implement the four ablation methods following their documented
definitions from the V1 experiment plan:

- M1: positional variation-rate identification only.
      Boundary = first token position whose value variation rate across the
      corpus exceeds 0.5.  Timestamps vary immediately, so this fails on
      virtually every timestamped format (matches V1's observed collapse).

- M2: positional variation rate + field types.
      A prefix position belongs to the header if the token value there is
      constant (>=90%), OR the position's dominant field type is stable
      (>=90%) AND header-like (date/time/level/host/id/bracket/...).
      Pure-number tokens additionally require digit-length stability
      (timestamp-like shape).  Boundary = first non-header prefix position.

- M3: M2 + short (4-token) typed prefix grouping + structural consistency.
      Lines are grouped by the type signature of their first 4 tokens;
      M2 statistics are computed per group (isolating mixed formats).
      The learned group boundary is truncated at the first prefix position
      whose within-group type consistency is < 0.8 (structural consistency
      constraint).  Small groups (<5 lines) fall back to the global M2
      boundary.

- M4: M3 + body evidence judgement.
      After the M3 boundary, if the first body token is value-like
      (ID/NUM/HEX/HOST/IP), the boundary is pulled back by one token on the
      suspicion that a message word was swallowed.  (V1 showed this hurts:
      it mostly re-opens headers.)

All methods work on whitespace tokens and return character offsets via
``token_boundary_to_char``.  b == 0 means "no header detected / keep line".
"""
from __future__ import annotations

from collections import Counter, defaultdict

from .tokentypes import (
    STRONG_TYPES, classify, is_headerish, signature_from_tokens, tokenize,
    token_boundary_to_char, tokens_to_boundary_char, type_family,
)

MAX_POS = 16
VARIATION_THRESHOLD = 0.5
CONST_THRESHOLD = 0.9
TYPE_CONSISTENCY = 0.9
M3_GROUP_MIN = 5
M3_SIG_LEN = 4
M3_STRUCT_CONSISTENCY = 0.8

VALUE_LIKE_TYPES = {"ID", "NUM", "NUM_EPOCH", "HEX", "HOST", "IP", "IP_PORT"}


class PositionStats:
    """Per-position value/type statistics over a set of lines.

    Type consistency is computed at family level (see ``type_family``).
    """

    def __init__(self, lines: list[str], tok=tokenize):
        self.value_counters: list[Counter] = []
        self.type_counters: list[Counter] = []
        self.n_at_pos: list[int] = []
        for line in lines:
            toks = tok(line)
            for j, t in enumerate(toks[:MAX_POS]):
                if j >= len(self.value_counters):
                    self.value_counters.append(Counter())
                    self.type_counters.append(Counter())
                    self.n_at_pos.append(0)
                self.value_counters[j][t.text] += 1
                self.type_counters[j][type_family(classify(t.text))] += 1
                self.n_at_pos[j] += 1

    def n(self, j: int) -> int:
        return self.n_at_pos[j] if j < len(self.n_at_pos) else 0

    def variation_rate(self, j: int) -> float:
        if j >= len(self.value_counters) or self.n(j) == 0:
            return 0.0
        top = self.value_counters[j].most_common(1)[0][1]
        return 1.0 - top / self.n(j)

    def top_type(self, j: int):
        if j >= len(self.type_counters) or self.n(j) == 0:
            return None, 0.0
        t, c = self.type_counters[j].most_common(1)[0]
        return t, c / self.n(j)

    def is_constant(self, j: int) -> bool:
        if j >= len(self.value_counters) or self.n(j) == 0:
            return False
        top = self.value_counters[j].most_common(1)[0][1]
        return top / self.n(j) >= CONST_THRESHOLD

    def is_header_position(self, j: int) -> bool:
        """M2 rule for a single position."""
        if self.n(j) == 0:
            return False
        if self.is_constant(j):
            return True
        t, tcons = self.top_type(j)
        if tcons < TYPE_CONSISTENCY:
            return False
        return is_headerish(t)


class M1Detector:
    name = "M1"
    uses_types = False

    def __init__(self, tokenizer=None):
        self._tok = tokenizer or tokenize

    def fit(self, lines: list[str]) -> "M1Detector":
        self.stats = PositionStats(lines, self._tok)
        self.boundary = 0
        for j in range(MAX_POS):
            if self.stats.n(j) < max(3, len(lines) * 0.5):
                break
            if self.stats.variation_rate(j) > VARIATION_THRESHOLD:
                self.boundary = j
                break
        return self

    def predict_token_boundary(self, line: str) -> int:
        return self.boundary

    def predict(self, line: str) -> int:
        return tokens_to_boundary_char(
            line, self._tok(line), self.predict_token_boundary(line))


class M2Detector:
    name = "M2"
    uses_types = True

    def __init__(self, tokenizer=None):
        self._tok = tokenizer or tokenize

    def fit(self, lines: list[str]) -> "M2Detector":
        self.stats = PositionStats(lines, self._tok)
        self.boundary = 0
        for j in range(MAX_POS):
            if self.stats.n(j) < max(3, len(lines) * 0.5):
                break
            if not self.stats.is_header_position(j):
                self.boundary = j
                break
            self.boundary = j + 1
        return self

    def predict_token_boundary(self, line: str) -> int:
        return self.boundary

    def predict(self, line: str) -> int:
        return tokens_to_boundary_char(
            line, self._tok(line), self.predict_token_boundary(line))


class _Leaf:
    __slots__ = ("boundary", "n")

    def __init__(self, boundary: int, n: int = 0):
        self.boundary = boundary
        self.n = n


class _Split:
    __slots__ = ("pos", "children", "fallback", "by_value", "by_kind")

    def __init__(self, pos: int, children: dict, fallback: dict,
                 by_value: bool = False, by_kind: bool = False):
        self.pos = pos
        self.children = children  # key -> node
        self.fallback = fallback  # family -> modal leaf boundary
        self.by_value = by_value  # True: children keyed by token text
        self.by_kind = by_kind    # True: children keyed by inner kind


def _modal_leaf_boundaries(node, acc: dict) -> None:
    """Accumulate (family -> weighted modal leaf boundary) over a subtree."""
    if isinstance(node, _Leaf):
        acc[("?", node.boundary)] = acc.get(("?", node.boundary), 0) + node.n
        return
    for child in node.children.values():
        _modal_leaf_boundaries(child, acc)


class M3Detector:
    """M2 + short prefix grouping + structural consistency.

    Architecture:
    1. Short-prefix grouping: train lines are grouped by the FAMILY-typed
       signature of their first 4 tokens ("[DATE, TIME, PUNCT, LEVEL]").
       This isolates coarser format families in mixed streams.  Groups
       smaller than M3_GROUP_MIN produce a keep-leaf (conservative).
    2. Within each group, a recursive scan continues from position 4:
       - constant position (>=90%)               -> consume, advance;
       - family-consistent (>=90%) + header-like -> consume, advance;
       - family-consistent + weak (WORD/OTHER..) -> header ends here;
       - family-mixed position                   -> SPLIT:
           * strong-family partitions (brackets/dates/levels) are further
             partitioned by VALUE (distinct structural values are distinct
             sub-formats, e.g. "[main]" vs "[LeaseRenewer:...]") and each
             sub-partition continues scanning;
           * weak-family partitions stop at the split position (a weak
             token after mixed structure is body, not header);
           * partitions below M3_GROUP_MIN stop at the split position.
    3. Guard A: a learned boundary is only accepted when the consumed
       prefix contains at least one STRONG type position (date/time/level/
       bracket/...); otherwise the line is kept unchanged.  HOST/ID/NUM
       alone are not strong evidence (host-like body protection).

    At prediction time, unseen signature groups and unseen split branches
    are kept unchanged (conservative abstain); a value miss inside a
    strong family falls back to the modal boundary of that family.
    """

    name = "M3"
    uses_types = True
    uses_grouping = True

    def __init__(self, tokenizer=None):
        self._tok = tokenizer or tokenize

    def fit(self, lines: list[str]) -> "M3Detector":
        self.n_train = len(lines)
        self.n_leaves = 0
        self.n_splits = 0
        groups: dict[tuple, list[str]] = defaultdict(list)
        for line in lines:
            groups[signature_from_tokens(self._tok(line), M3_SIG_LEN)].append(line)
        self.group_trees: dict[tuple, object] = {}
        self.group_sizes = {sig: len(v) for sig, v in groups.items()}
        for sig, glines in groups.items():
            if len(glines) < M3_GROUP_MIN:
                leaf = _Leaf(0, len(glines))
                self.n_leaves += 1
                self.group_trees[sig] = leaf
            else:
                # NOTE: the signature only GROUPS lines; the boundary scan
                # restarts from position 0 inside the group, so headers
                # shorter than the signature length remain expressible.
                self.group_trees[sig] = self._build(glines, 0)
        return self

    # -- learning ---------------------------------------------------------
    def _has_unclosed_bracket(self, lines: list[str], j: int) -> bool:
        """True when >= half of the lines have an unclosed '[' before pos j.

        This marks that the scan is INSIDE a multi-token bracket field
        ("[AsyncDispatcher event handler]"): weak tokens there are field
        content, not body words.
        """
        hits = 0
        for line, toks in self._lines_tokens(lines):
            depth = 0
            for t in toks[:j]:
                depth += t.text.count("[") - t.text.count("]")
            if depth > 0:
                hits += 1
        return hits >= 0.5 * len(lines)

    def _bracket_closes_later(self, lines: list[str], j: int) -> bool:
        """True when >= half of the lines CLOSE the bracket opened at pos j
        at any later character (including inside the same span).  A bracket
        that never closes ("[lease janitor flushed ...") is not a header
        field; consuming it would eat body text (challenge category:
        unclosed brackets)."""
        hits = 0
        for line, toks in self._lines_tokens(lines):
            if j < len(toks) and "]" in line[toks[j].start + 1:]:
                hits += 1
        return hits >= 0.5 * len(lines)

    def _prefix_has_strong(self, lines: list[str], b: int) -> bool:
        """Guard A: >= half of the lines show a strong type in [0, b)."""
        if b <= 0:
            return False
        hits = 0
        for line, toks in self._lines_tokens(lines):
            if any(
                type_family(classify(t.text)) in STRONG_TYPES for t in toks[:b]
            ):
                hits += 1
        return hits >= 0.5 * len(lines)

    def _last_strong_pos(self, lines: list[str], b: int) -> int:
        """Last position j < b where >= half of the lines carry a STRONG
        type span (see ``_strong_prefix_len`` for the span Guard B)."""
        best = -1
        for j in range(b):
            hits = 0
            for line, toks in self._lines_tokens(lines):
                if j < len(toks) and (
                        type_family(classify(toks[j].text)) in STRONG_TYPES):
                    hits += 1
            if hits >= 0.5 * len(lines):
                best = j
        return best

    def _strong_prefix_len(self, lines: list[str], b: int) -> int:
        """Length of the maximal CONTIGUOUS strong run from position 0.

        Used as the span Guard B fallback: when a body template was
        swallowed, only a strong prefix starting at position 0 is trusted
        as unambiguous header evidence.  A strong span in the MIDDLE of
        the line (a bracket inside the body, e.g. "status [ok 77] ...")
        does not count -- the run must start at 0.
        """
        run = 0
        for j in range(b):
            hits = 0
            for line, toks in self._lines_tokens(lines):
                if j < len(toks) and (
                        type_family(classify(toks[j].text)) in STRONG_TYPES):
                    hits += 1
            if hits >= 0.5 * len(lines):
                run = j + 1
            else:
                break
        return run

    def _leaf(self, lines: list[str], b: int, last_split: int | None) -> _Leaf:
        if b > 0:
            if not self._prefix_has_strong(lines, b):
                b = 0  # Guard A: no strong header evidence -> keep original
            else:
                # Guard B: a boundary consuming most of the line leaves no
                # room for a body -> a body template was swallowed.
                # Fall back to the last split position (where this partition
                # diverged structurally), else keep the original line.
                lens = sorted(len(t) for l, t in self._lines_tokens(lines))
                med = lens[len(lens) // 2] if lens else 0
                if med > 0 and b > 0.75 * med:
                    b = self._guard_b_fallback(lines, b, last_split)
        self.n_leaves += 1
        return _Leaf(b, len(lines))

    def _guard_b_fallback(self, lines: list[str], b: int,
                          last_split: int | None) -> int:
        """Base M3: fall back to the last split position, else keep."""
        b = last_split if last_split is not None else 0
        if b > 0 and not self._prefix_has_strong(lines, b):
            b = 0
        return b

    def _value_split(self, lines: list[str], j: int):
        """Partition lines by their token VALUE at position j."""
        parts: dict[str, list[str]] = defaultdict(list)
        for line, toks in self._lines_tokens(lines):
            parts[toks[j].text if j < len(toks) else "SHORT"].append(line)
        return parts

    def _bracket_position_is_body(self, lines: list[str], j: int) -> bool:
        """Hook: True when the BRACKET-family position j holds wrapped
        free-text content rather than a structural header field.

        Base M3 always returns False (a bracket-wrapped token is treated
        like any other header-ish type).  The E2 span-aware subclass
        overrides this (content-bracket guard).
        """
        return False

    def _inner_kind_of(self, inner: str | None) -> str:
        """Kind of a bracket inner at walk time (base: always structural)."""
        return "structural"

    def _bracket_structure_ahead(self, lines: list[str], j: int,
                                 lookahead: int = 3) -> bool:
        """True when >= half of the lines carry a bracket-family span in
        positions (j, j+lookahead].  Used to decide whether a structural
        partition of a kind split may keep scanning: header id brackets
        are followed by more brackets ("[t][i][i][l][content]"), while a
        lone id bracket followed by plain words is a body fragment
        ("[user5639] opened session")."""
        hits = 0
        for _line, toks in self._lines_tokens(lines):
            found = False
            for t in toks[j + 1:j + 1 + lookahead]:
                fam = type_family(classify(t.text))
                if fam.startswith("BRACKET"):
                    found = True
                    break
            if found:
                hits += 1
        return hits >= 0.5 * len(lines)

    def _bracket_kind_partition(self, lines: list[str], j: int):
        """Hook: partition the BRACKET-family position j into
        {"content": [...], "structural": [...]} when BOTH kinds are
        present and the content side is diverse free text.

        Returns None when no kind split applies.  Base M3 always returns
        None; the E2 span-aware subclass overrides this so that mixed
        formats sharing a signature (e.g. "[t][i][i][l][CONTENT]..." vs
        "[t][i][i][i][l][CONTENT]...") split instead of the majority
        kind deciding for everyone.
        """
        return None

    def _build(self, lines: list[str], j: int, last_split: int | None = None):
        stats = PositionStats(lines, self._tok)
        while j < MAX_POS:
            n = stats.n(j)
            if n < max(3, len(lines) * 0.5):
                return self._leaf(lines, j, last_split)
            if stats.is_constant(j):
                j += 1
                continue
            fam, famcons = stats.top_type(j)
            if famcons >= TYPE_CONSISTENCY:
                if fam == "BRACKET":
                    kind_parts = self._bracket_kind_partition(lines, j)
                    if kind_parts is not None:
                        children = {}
                        for kind_k, part in kind_parts.items():
                            if kind_k == "content" or len(part) < M3_GROUP_MIN:
                                children[kind_k] = self._leaf(part, j, j)
                            elif self._bracket_structure_ahead(part, j):
                                children[kind_k] = self._build(part, j + 1, j)
                            else:
                                # structural values not followed by more
                                # structure: treat as body fragment
                                children[kind_k] = self._leaf(part, j, j)
                        self.n_splits += 1
                        return self._mk_split(j, children, by_kind=True)
                    if self._bracket_position_is_body(lines, j):
                        # hook (E2-C): bracket-wrapped free text is body
                        return self._leaf(lines, j, last_split)
                if (fam == "BRACKET_OPEN"
                        and not self._bracket_closes_later(lines, j)):
                    # an opener that never closes is not a header field
                    return self._leaf(lines, j, last_split)
                if is_headerish(fam):
                    j += 1
                    continue
                # family-consistent but weak: body word -- UNLESS we are
                # inside a multi-token bracket field
                if self._has_unclosed_bracket(lines, j):
                    parts = self._value_split(lines, j)
                    children = {}
                    for val, part in parts.items():
                        if val == "SHORT" or len(part) < M3_GROUP_MIN:
                            children[val] = self._leaf(part, j, j)
                        else:
                            children[val] = self._build(part, j + 1, j)
                    self.n_splits += 1
                    return self._mk_split(j, children, by_value=True)
                return self._leaf(lines, j, last_split)  # weak failure: body
            # family-mixed position: split
            parts: dict = defaultdict(list)
            for line, toks in self._lines_tokens(lines):
                if j >= len(toks):
                    key = "SHORT"
                else:
                    fam_j = type_family(classify(toks[j].text))
                    key = (
                        (fam_j, toks[j].text)
                        if fam_j in STRONG_TYPES
                        else fam_j
                    )
                parts[key].append(line)
            children = {}
            for key, part in parts.items():
                if key == "SHORT":
                    children[key] = self._leaf(part, j, j)
                elif isinstance(key, tuple):
                    # strong family, value-uniform partition: structural
                    # sub-format, continue scanning
                    children[key] = self._build(part, j + 1, j)
                elif len(part) < M3_GROUP_MIN:
                    children[key] = self._leaf(part, j, j)
                elif self._has_unclosed_bracket(part, j):
                    # weak family inside a multi-token bracket field:
                    # split by value and continue
                    vparts = self._value_split(part, j)
                    sub = {}
                    for val, vpart in vparts.items():
                        if val == "SHORT" or len(vpart) < M3_GROUP_MIN:
                            sub[val] = self._leaf(vpart, j, j)
                        else:
                            sub[val] = self._build(vpart, j + 1, j)
                    self.n_splits += 1
                    children[key] = self._mk_split(j, sub, by_value=True)
                else:
                    # weak family after mixed structure: body, stop here
                    children[key] = self._leaf(part, j, j)
            self.n_splits += 1
            return self._mk_split(j, children)
        return self._leaf(lines, MAX_POS, last_split)

    def _mk_split(self, j: int, children: dict, by_value: bool = False,
                  by_kind: bool = False) -> _Split:
        # fallback: modal leaf boundary per family for value misses
        fam_modal: dict[str, dict[int, int]] = defaultdict(dict)
        for key, child in children.items():
            if by_value or by_kind:
                fam_k = "*"
            else:
                fam_k = key[0] if isinstance(key, tuple) else (
                    key if isinstance(key, str) and key != "SHORT" else "*"
                )
            acc: dict = {}
            _modal_leaf_boundaries(child, acc)
            for (__, b), c in acc.items():
                fam_modal[fam_k][b] = fam_modal[fam_k].get(b, 0) + c
        fallback = {
            fam_k: max(cnt.items(), key=lambda kv: kv[1])[0]
            for fam_k, cnt in fam_modal.items()
        }
        return _Split(j, children, fallback, by_value, by_kind)

    # -- inference --------------------------------------------------------
    def predict_token_boundary(self, line: str) -> int:
        toks = self._tok(line)
        sig = signature_from_tokens(toks, M3_SIG_LEN)
        node = self.group_trees.get(sig)
        if node is None:
            return 0  # unseen format family: conservative keep
        while isinstance(node, _Split):
            j = node.pos
            if j >= len(toks):
                return j
            if node.by_kind:
                t = toks[j].text
                inner = t[1:-1] if (len(t) >= 2 and t[0] == "[" and t[-1] == "]") else None
                key = self._inner_kind_of(inner)
                child = node.children.get(key)
                if child is None:
                    return node.fallback.get("*", 0)
            elif node.by_value:
                key = toks[j].text
                child = node.children.get(key)
                if child is None:
                    # unseen bracket-field content: use modal boundary of
                    # the field's observed values (conservative default)
                    return node.fallback.get("*", 0)
            else:
                fam = type_family(classify(toks[j].text))
                key = (fam, toks[j].text) if fam in STRONG_TYPES else fam
                child = node.children.get(key)
                if child is None:
                    if fam in node.fallback:
                        return node.fallback[fam]
                    return 0  # unseen structure: conservative keep
            node = child
        return node.boundary

    def predict(self, line: str) -> int:
        return tokens_to_boundary_char(
            line, self._tok(line), self.predict_token_boundary(line))

    def _lines_tokens(self, lines: list[str]):
        return [(l, self._tok(l)) for l in lines]


class M4Detector(M3Detector):
    name = "M4"

    def predict_token_boundary(self, line: str) -> int:
        b = super().predict_token_boundary(line)
        toks = self._tok(line)
        if 0 < b < len(toks):
            if classify(toks[b].text) in VALUE_LIKE_TYPES:
                b -= 1  # pull back: first body token looks like a value
        return b

    def predict(self, line: str) -> int:
        return tokens_to_boundary_char(
            line, self._tok(line), self.predict_token_boundary(line))


ALL_METHODS = [M1Detector, M2Detector, M3Detector, M4Detector]

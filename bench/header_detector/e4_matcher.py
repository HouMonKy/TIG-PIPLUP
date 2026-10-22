"""E4-B: online rule matchers (P1 / P2 / P3) plus the P0 baseline.

All runtimes share the same API::

    m.match(raw_log: str, source: str) -> {
        "action": "ACCEPT" | "KEEP",
        "body_start_offset": int,     # 0 == keep original line
        "rule_id": str | None,
        "reason": str | None,
    }

Runtimes:

- P0 (baseline): the E3-B full path -- fitted E2-D detector ``predict``
  followed by the E3-B gate with its redundant re-tokenization and
  re-prediction (exactly what ``e3_runner.apply_gate`` does online).
  No compiled rules; the performance baseline required by the experiment
  plan (NOT just the 5.35 us gate increment).

- P1 "sequential candidates": the compiled FLAT rule list per source;
  each line is matched by scanning the source's rules in order and
  evaluating the rule's prefix conditions.  Simple, no index.

- P2 "source index + prefix index": source -> signature -> compiled
  decision tree; the walk replicates ``M3Detector.predict_token_boundary``
  on plain tuple nodes.  Gate via compiled accept table.

- P3 "P2 + fast reject + bounded prefix parse": P2 fused into a single
  pass -- one prefix-limited tokenization (17 spans / 256 chars, the
  E2-D bound), a per-line classify cache so each span text is classified
  at most once (lazy: only spans actually queried by the matched rule
  path get classified, bounding work by rule depth rather than by 17),
  and a precomputed per-signature accept set for the gate.

None of the runtimes performs online statistics, clustering or boundary
search; P0's detector walk is inference on offline-learned trees (the
scientific baseline), not learning.
"""
from __future__ import annotations

import json
import re
import time
from itertools import islice
from pathlib import Path

from .gating import VALUE_LIKE_FAMS, WEAK_END_FAMS, boundary_features, build_rules, gate_b
from .methods import M3_SIG_LEN
from .span_methods import RE_WORDCHAR, SpanM3Prefix, inner_kind
from .spantok import Span, SpanAwareTokenizer
from .tokentypes import STRONG_TYPES, classify, signature_from_tokens, type_family

# runtime node encodings (tuples for speed)
LEAF = 0
SPLIT = 1
BY_FAMILY = 0
BY_VALUE = 1
BY_KIND = 2

_PREFIX_TOK = SpanAwareTokenizer(max_spans=17, max_chars=256).tokenize

# ---------------------------------------------------------------------------
# P3 fast prefix tokenizer (exactly equivalent to the 17/256 prefix scan)
# ---------------------------------------------------------------------------
# Three equivalence-preserving speedups over the exact scanner:
#
# 1. regex-hop scanning: instead of iterating every character, jump
#    between "interesting" characters (whitespace / brackets) found by a
#    C-level regex; runs of plain characters are skipped at C speed.
# 2. spans starting after the last opener bracket in the prefix can never
#    enter bracket depth -> plain maximal non-whitespace runs via regex.
# 3. lazy depth: the caller parses only as many spans as the matched rule
#    tree can query (per-tree ``need``); spans are returned as plain
#    tuples.  The original's EMPTY-span padding (scan stuck at the
#    256-char cap with non-whitespace ahead) is replicated up to ``need``
#    -- tree queries never exceed ``need - 1``, so this is exact.
_RE_NONWS = re.compile(r"\S+")
_RE_NEXT = re.compile(r"\s|[\[\({\]})]")
_OPENERS = "[({"
_CLOSER_OF = {"[": "]", "(": ")", "{": "}"}
_RE_PLAIN = re.compile(r"\S+")
_MEMO_FAM: dict = {}
_MEMO_CAP = 1 << 16


def _fam(text: str) -> str:
    """type_family(classify(text)) with a bounded pure-function memo."""
    f = _MEMO_FAM.get(text)
    if f is None:
        f = type_family(classify(text))
        if len(_MEMO_FAM) >= _MEMO_CAP:
            _MEMO_FAM.clear()
        _MEMO_FAM[text] = f
    return f


def _scan_hop(line: str, start: int, limit: int):
    """Regex-hop re-implementation of SpanAwareTokenizer._scan_span
    (identical outputs: (end, last_close_end))."""
    n = len(line)
    depth = 0
    stack = []
    j = start
    last_close_end = None
    starts_with_group = line[start] in _OPENERS
    while j < n:
        if j >= limit and depth == 0:
            break
        m = _RE_NEXT.search(line, j)
        if m is None:
            if depth == 0:
                return limit, last_close_end
            mm = _RE_PLAIN.match(line, start)
            return (mm.end() if mm else start + 1), None
        k = m.start()
        if k >= limit and depth == 0:
            return limit, last_close_end
        c = line[k]
        if c in _OPENERS:
            depth += 1
            stack.append(c)
        elif c in "]})":
            if depth > 0 and stack and _CLOSER_OF[stack[-1]] == c:
                depth -= 1
                stack.pop()
                if depth == 0:
                    last_close_end = k + 1
                    j = k + 1
                    if starts_with_group and j < n and line[j] in _OPENERS:
                        break       # adjacent pure bracket groups
                    continue
            # unmatched closer: plain character
        else:                       # whitespace
            if depth == 0:
                return k, last_close_end
        j = k + 1
    if depth > 0:
        mm = _RE_PLAIN.match(line, start)
        return (mm.end() if mm else start + 1), None
    return j, last_close_end


def _tok3(line: str, need: int = 17):
    """Prefix spans as (text, start, end, last_close_end) tuples, parsed
    up to ``need`` spans (exact prefix of the 17/256 scan)."""
    n = len(line)
    limit = 256 if n > 256 else n
    spans = []
    last_open = max(line.rfind("[", 0, limit),
                    line.rfind("(", 0, limit),
                    line.rfind("{", 0, limit))
    i = 0
    if last_open >= 0:
        while len(spans) < need:
            while i < n and line[i].isspace():
                i += 1
            if i >= n or i > last_open:
                break
            end, lce = _scan_hop(line, i, limit)
            spans.append((line[i:end], i, end, lce))
            i = end
    if len(spans) < need:
        for m in islice(_RE_NONWS.finditer(line, i, limit), need - len(spans)):
            spans.append((m.group(), m.start(), m.end(), None))
    # empty-span padding (original behaviour, bounded by need)
    if len(spans) < need:
        pos = spans[-1][2] if spans else 0
        m = _RE_NONWS.search(line, pos)
        if m is not None and m.start() >= limit:
            while len(spans) < need:
                spans.append(("", m.start(), m.start(), None))
    return spans

ACCEPT = "ACCEPT"
KEEP = "KEEP"


def _keep(reason: str) -> dict:
    return {"action": KEEP, "body_start_offset": 0, "rule_id": None,
            "reason": reason}


def _combo_of(last_text: str, first_text: str) -> tuple:
    """E3-B boundary combo from the two span texts at the boundary."""
    last_fam = type_family(classify(last_text))
    first_fam = type_family(classify(first_text)) if first_text else "END"
    return (
        last_fam in VALUE_LIKE_FAMS,
        last_fam == "CTAIL"
        and not any(c.isdigit() for c in last_text)
        and "[" not in last_text and "(" not in last_text,
        first_fam in VALUE_LIKE_FAMS,
        last_fam in WEAK_END_FAMS,
    )


def _combo_str(combo: tuple) -> str:
    return "".join("1" if c else "0" for c in combo)


def _boundary_char(line: str, spans, b: int) -> int:
    """``tokens_to_boundary_char`` + E2-C refinement (single source of
    truth for every runtime)."""
    if b <= 0:
        return 0
    if b >= len(spans):
        if b == len(spans):
            char = len(line.rstrip())
        else:
            return 0
    else:
        char = spans[b].start
    if 0 < b <= len(spans):
        last = spans[b - 1]
        lce = last.last_close_end
        if lce is not None and lce < last.end:
            trailing = line[lce:last.end]
            if trailing and RE_WORDCHAR.match(trailing):
                char = lce
    return char


# ---------------------------------------------------------------------------
# runtime tree decoding
# ---------------------------------------------------------------------------

def _decode_node(node: dict):
    if "b" in node:
        return (LEAF, node["b"])
    by = {"family": BY_FAMILY, "value": BY_VALUE, "kind": BY_KIND}[node["by"]]
    children = {}
    for key, child in node["children"].items():
        if by == BY_FAMILY and "\x1f" in key:
            fam, text = key.split("\x1f", 1)
            children[(fam, text)] = _decode_node(child)
        else:
            children[key] = _decode_node(child)
    return (SPLIT, node["pos"], by, children, node["fallback"])


def _walk(node, spans, classify_fn):
    """Replicates M3Detector.predict_token_boundary on decoded nodes."""
    while node[0] == SPLIT:
        _, j, by, children, fallback = node
        if j >= len(spans):
            return j
        text = spans[j].text
        if by == BY_FAMILY:
            fam = type_family(classify_fn(text))
            key = (fam, text) if fam in STRONG_TYPES else fam
            child = children.get(key)
            if child is None:
                return fallback.get(fam, 0)
        elif by == BY_VALUE:
            child = children.get(text)
            if child is None:
                return fallback.get("*", 0)
        else:  # BY_KIND
            if len(text) >= 2 and text[0] == "[" and text[-1] == "]":
                kind = inner_kind(text[1:-1])
            else:
                kind = "structural"
            child = children.get(kind)
            if child is None:
                return fallback.get("*", 0)
        node = child
    return node[1]


# ---------------------------------------------------------------------------
# P0: E3-B original full path (scientific + performance baseline)
# ---------------------------------------------------------------------------

class P0Baseline:
    name = "P0"

    def __init__(self, tau: float, dets: dict, rules3: dict):
        self.tau = tau
        self.dets = dets                # source -> fitted SpanM3Prefix
        self.rules3 = rules3            # source -> E3-B FamilyRule dict

    def match(self, raw_log: str, source: str) -> dict:
        det = self.dets.get(source)
        if det is None:
            return _keep("unknown_source")
        pred = det.predict(raw_log)
        if pred <= 0:
            return _keep("detector_abstain")
        feats = boundary_features(det, raw_log)
        combo = feats["combo"] if feats else (False, False, False, False)
        sig = signature_from_tokens(det._tok(raw_log), M3_SIG_LEN)
        rule = self.rules3[source].get((sig, combo))
        if not gate_b(rule, self.tau):
            reason = ("gate_reject_no_evidence" if rule is None
                      else "gate_reject_low_reliability")
            return _keep(reason)
        rule_id = f"{source}#{','.join(sig)}#{_combo_str(combo)}"
        return {"action": ACCEPT, "body_start_offset": pred,
                "rule_id": rule_id, "reason": None}


# ---------------------------------------------------------------------------
# P2 / P3 core (shared decoded structures)
# ---------------------------------------------------------------------------

def _need_of(node) -> int:
    """Maximum span index a tree can query (+1): split positions and leaf
    boundaries (the combo reads spans[b-1] and spans[b])."""
    if node[0] == LEAF:
        return node[1] + 1
    need = node[1] + 1                     # split queries spans[pos]
    for child in node[3].values():         # children
        n2 = _need_of(child)
        if n2 > need:
            need = n2
    return need


class _RuntimeSource:
    __slots__ = ("trees", "accept", "flat")

    def __init__(self, src: dict):
        trees = {}
        for sig_key, t in src["trees"].items():
            node = _decode_node(t)
            need = min(17, max(4, _need_of(node)))
            trees[tuple(sig_key.split(","))] = (node, need)
        self.trees = trees
        accept: dict = {}
        for key, entry in src["gate"].items():
            if entry["accept"]:
                sig_key, combo_s = key.split("|", 1)
                accept.setdefault(tuple(sig_key.split(",")), set()).add(combo_s)
        self.accept = {k: frozenset(v) for k, v in accept.items()}
        self.flat = src.get("flat_rules", [])


class HeaderRuleMatcher:
    """P2: source index + signature (prefix) index matching."""

    name = "P2"

    def __init__(self, ruleset: dict):
        self.tau = ruleset["tau"]
        self.version = ruleset["version"]
        self.sources = {n: _RuntimeSource(s)
                        for n, s in ruleset["sources"].items()}
        self._tok = _PREFIX_TOK

    # -- hot path ---------------------------------------------------------
    def match(self, raw_log: str, source: str) -> dict:
        src = self.sources.get(source)
        if src is None:
            return _keep("unknown_source")
        spans = self._tok(raw_log)
        if not spans:
            return _keep("detector_abstain")
        fams = [type_family(classify(s.text)) for s in spans[:4]]
        while len(fams) < 4:
            fams.append("SHORT")
        sig = tuple(fams)
        entry = src.trees.get(sig)
        if entry is None:
            return _keep("unknown_signature")
        b = _walk(entry[0], spans, classify)
        if b <= 0:
            return _keep("detector_abstain")
        char = _boundary_char(raw_log, spans, b)
        if char <= 0:
            return _keep("detector_abstain")
        combo = _combo_of(spans[b - 1].text,
                          spans[b].text if b < len(spans) else "")
        if _combo_str(combo) not in src.accept.get(sig, ()):
            return _keep("gate_reject")
        rule_id = f"{source}#{','.join(sig)}#{_combo_str(combo)}"
        return {"action": ACCEPT, "body_start_offset": char,
                "rule_id": rule_id, "reason": None}


# ---------------------------------------------------------------------------
# P3: fused single pass
# ---------------------------------------------------------------------------

class HeaderRuleMatcherFused(HeaderRuleMatcher):
    """P3: P2 + fast reject + bounded prefix parse (single fused pass).

    Optimizations over P2 (all equivalence-preserving):
    - fast prefix tokenizer (regex scan when the prefix has no opener
      bracket; exact depth-tracking scanner otherwise);
    - bounded cross-line memo of type_family(classify(text)) -- a pure
      function memo, not learned state;
    - single tokenization, fused gate with precomputed accept sets.
    """

    name = "P3"

    def match(self, raw_log: str, source: str) -> dict:  # noqa: C901
        src = self.sources.get(source)
        if src is None:
            return _keep("unknown_source")
        # stage 1: parse only the 4 signature spans (fast reject)
        spans = _tok3(raw_log, 4)
        n = len(spans)
        sig = tuple(
            _fam(spans[i][0]) if i < n else "SHORT" for i in range(4))
        entry = src.trees.get(sig)
        if entry is None:
            return _keep("unknown_signature")
        node, need = entry
        # stage 2: deeper tree -> parse the additional spans it can query
        if need > 4:
            spans = _tok3(raw_log, need)
            n = len(spans)
        # fused walk (tuples: text, start, end, lce)
        while node[0] == SPLIT:
            _, j, by, children, fallback = node
            if j >= n:
                b = j
                break
            text = spans[j][0]
            if by == BY_FAMILY:
                fam = _fam(text)
                child = children.get((fam, text) if fam in STRONG_TYPES
                                     else fam)
                if child is None:
                    b = fallback.get(fam, 0)
                    break
            elif by == BY_VALUE:
                child = children.get(text)
                if child is None:
                    b = fallback.get("*", 0)
                    break
            else:
                if len(text) >= 2 and text[0] == "[" and text[-1] == "]":
                    kind = inner_kind(text[1:-1])
                else:
                    kind = "structural"
                child = children.get(kind)
                if child is None:
                    b = fallback.get("*", 0)
                    break
            node = child
        else:
            b = node[1]
        if b <= 0:
            return _keep("detector_abstain")
        # boundary char + E2-C refinement
        if b >= n:
            if b != n:
                return _keep("detector_abstain")
            char = len(raw_log.rstrip())
        else:
            char = spans[b][1]
        if 0 < b <= n:
            lce = spans[b - 1][3]
            if lce is not None and lce < spans[b - 1][2]:
                trailing = raw_log[lce:spans[b - 1][2]]
                if trailing and RE_WORDCHAR.match(trailing):
                    char = lce
        if char <= 0:
            return _keep("detector_abstain")
        # inline combo (memoized classify; identical to _combo_of)
        last_t = spans[b - 1][0]
        last_fam = _fam(last_t)
        if b < n and spans[b][0]:
            first_fam = _fam(spans[b][0])
        else:
            first_fam = "END"
        combo_s = (
            ("1" if last_fam in VALUE_LIKE_FAMS else "0")
            + ("1" if (last_fam == "CTAIL"
                       and not any(c.isdigit() for c in last_t)
                       and "[" not in last_t and "(" not in last_t)
               else "0")
            + ("1" if first_fam in VALUE_LIKE_FAMS else "0")
            + ("1" if last_fam in WEAK_END_FAMS else "0")
        )
        if combo_s not in src.accept.get(sig, ()):
            return _keep("gate_reject")
        return {"action": ACCEPT, "body_start_offset": char,
                "rule_id": f"{source}#{','.join(sig)}#{combo_s}",
                "reason": None}


# ---------------------------------------------------------------------------
# P1: sequential flat-rule matching
# ---------------------------------------------------------------------------

class HeaderRuleMatcherSequential:
    """P1: precompiled flat rules, sequential candidate matching."""

    name = "P1"

    def __init__(self, ruleset: dict):
        self.tau = ruleset["tau"]
        self.version = ruleset["version"]
        self.flat: dict = {}
        self.gate: dict = {}
        for name, src in ruleset["sources"].items():
            self.flat[name] = src.get("flat_rules", [])
            self.gate[name] = src["gate"]

    def match(self, raw_log: str, source: str) -> dict:
        rules = self.flat.get(source)
        if rules is None:
            return _keep("unknown_source")
        spans = _PREFIX_TOK(raw_log)
        n = len(spans)
        # sequential candidate matching: the 4-span signature is part of
        # every rule's match conditions (root-leaf trees carry no path
        # conditions, so the sig check is what constrains them)
        fams = [type_family(classify(s.text)) for s in spans[:4]]
        while len(fams) < 4:
            fams.append("SHORT")
        sig_key = ",".join(fams)
        b = None
        matched = None
        for rule in rules:
            if rule["sig"] != sig_key:
                continue
            ok = True
            for cond in rule["conds"]:
                k = cond[0]
                if k == "len_le":
                    if n > cond[1]:
                        ok = False
                        break
                    continue
                j = cond[1]
                if j >= n:
                    ok = False
                    break
                text = spans[j].text
                if k == "value":
                    if text != cond[2]:
                        ok = False
                        break
                elif k == "fam":
                    if type_family(classify(text)) != cond[2]:
                        ok = False
                        break
                elif k == "strong":
                    if (type_family(classify(text)) != cond[2]
                            or text != cond[3]):
                        ok = False
                        break
                elif k == "kind" or k == "not_kind":
                    if len(text) >= 2 and text[0] == "[" and text[-1] == "]":
                        kind = inner_kind(text[1:-1])
                    else:
                        kind = "structural"
                    if k == "kind":
                        if kind != cond[2]:
                            ok = False
                            break
                    elif kind in cond[2]:
                        ok = False
                        break
                elif k == "not_value":
                    if text in cond[2]:
                        ok = False
                        break
                elif k == "fam_not_child":
                    if (type_family(classify(text)) != cond[2]
                            or text in cond[3]):
                        ok = False
                        break
                else:  # fam_other
                    if type_family(classify(text)) in cond[2]:
                        ok = False
                        break
            if ok:
                b = rule["b"]
                matched = rule
                break
        if matched is None:
            return _keep("rule_no_match")
        if b <= 0:
            return _keep("detector_abstain")
        char = _boundary_char(raw_log, spans, b)
        if char <= 0:
            return _keep("detector_abstain")
        combo_s = _combo_str(_combo_of(
            spans[b - 1].text, spans[b].text if b < len(spans) else ""))
        entry = self.gate[source].get(sig_key + "|" + combo_s)
        if entry is None or not entry["accept"]:
            return _keep("gate_reject")
        return {"action": ACCEPT, "body_start_offset": char,
                "rule_id": matched["rule_id"] + "#" + combo_s, "reason": None}


# ---------------------------------------------------------------------------
# load / save helpers
# ---------------------------------------------------------------------------

def save_ruleset(ruleset: dict, path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ruleset, ensure_ascii=False), encoding="utf-8")


def load_matchers(path, classes=(HeaderRuleMatcherSequential,
                                 HeaderRuleMatcher,
                                 HeaderRuleMatcherFused)) -> dict:
    """Load a rule file from disk and build the requested runtimes,
    returning (matchers, load_seconds)."""
    t0 = time.perf_counter()
    ruleset = json.loads(Path(path).read_text(encoding="utf-8"))
    raw = time.perf_counter() - t0
    t1 = time.perf_counter()
    out = {c.name: c(ruleset) for c in classes}
    build = time.perf_counter() - t1
    return out, {"json_parse_s": round(raw, 4), "index_build_s": round(build, 4)}

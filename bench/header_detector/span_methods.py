"""E2: span-aware M3 variants.

- E2-B (SpanM3): the E0 M3 algorithm running on SpanAwareTokenizer spans
  instead of whitespace tokens.  Bracket fields containing spaces become
  single spans ("[AsyncDispatcher event handler]"), adjacent bracket
  groups split ("[time][level][proc]"), so multi-token bracket headers
  become expressible.  No other change -- content brackets are NOT yet
  guarded (this is the ablation step).

- E2-C (SpanM3Refined): E2-B plus
  (a) content-bracket guard (overrides M3's ``_bracket_position_is_body``
      hook): a family-consistent BRACKET position whose wrapped values are
      free-text-like (>=60% plain word tokens inside, no temporal pattern,
      more than one word) AND highly diverse (distinct ratio > 0.5) is
      treated as BODY -- a bracket-wrapped span is never consumed into the
      header merely because it is bracketed;
  (b) char-level boundary refinement: when the last consumed header span
      contains a closed bracket group followed by trailing word characters
      ("[dme-server]disk"), the body starts at the first trailing word
      char (``span.last_close_end``), resolving boundaries INSIDE a span
      without re-joining tokens -- ``raw[body_start:]`` yields the body.

- E2-D (SpanM3Prefix): E2-C with a prefix-limited tokenizer (max 17
  spans / 256 chars) -- same decisions, less scanning on long lines.
"""
from __future__ import annotations

import re

from .methods import M3Detector
from .spantok import SpanAwareTokenizer, span_tokenize
from .tokentypes import tokens_to_boundary_char

RE_TEMPORAL = re.compile(
    r"\d{2,4}[-/:.]\d{1,2}[-/:.]\d{1,2}|\d{1,2}:\d{2}(:\d{2})?([.,:]\d+)?")
RE_WORDCHAR = re.compile(r"\w")

CONTENT_WORD_FRAC = 0.6
CONTENT_DISTINCT_RATIO = 0.5


def inner_kind(inner: str) -> str:
    """Classify the inside of a bracket-wrapped span.

    "structural": temporal patterns, single tokens (ids/levels/nums), or
    word sets containing non-word tokens (paths, hosts, ports).
    "content":   multi-token free text made mostly of plain words.
    """
    if not inner:
        return "structural"
    if RE_TEMPORAL.search(inner):
        return "structural"
    words = inner.split()
    if len(words) <= 1:
        return "structural"
    plain = sum(
        1 for w in words
        if re.fullmatch(r"[A-Za-z][A-Za-z_-]*", w.strip(",;:"))
    )
    return "content" if plain / len(words) >= CONTENT_WORD_FRAC else "structural"


class SpanM3Detector(M3Detector):
    """E2-B: M3 on spans (no content guard, no refinement)."""

    name = "E2-B"

    def __init__(self, tokenizer=None):
        super().__init__(tokenizer or span_tokenize)

    def predict(self, line: str) -> int:
        return tokens_to_boundary_char(
            line, self._tok(line), self.predict_token_boundary(line))


class SpanM3Refined(SpanM3Detector):
    """E2-C: span M3 + content-bracket guard + char-level refinement."""

    name = "E2-C"

    # -- Guard B fallback (span-specific) ---------------------------------
    def _guard_b_fallback(self, lines: list[str], b: int,
                          last_split: int | None) -> int:
        """Span tokenization groups lines more finely, so a homogeneous
        body-template group can scan all-constant to the end of the line
        (e.g. Apache "mod_jk child workerEnv in error state 6").  When the
        template is swallowed, cut after the contiguous STRONG prefix
        (unambiguous header evidence starting at position 0); brackets in
        the middle of the body ("status [ok 77] ...") do not count."""
        run = self._strong_prefix_len(lines, b)
        if run > 0:
            b = run
        elif last_split is not None:
            b = last_split
        else:
            b = 0
        if b > 0 and not self._prefix_has_strong(lines, b):
            b = 0
        return b

    # -- content-bracket guard -------------------------------------------
    def _bracket_position_is_body(self, lines: list[str], j: int) -> bool:
        values = []
        for _line, toks in self._lines_tokens(lines):
            if j < len(toks):
                t = toks[j].text
                if len(t) >= 2 and t[0] == "[" and t[-1] == "]":
                    values.append(t[1:-1])
        n = len(values)
        if n == 0:
            return False
        content_frac = sum(1 for v in values if inner_kind(v) == "content") / n
        if content_frac < 0.5:
            return False
        return len(set(values)) / n > CONTENT_DISTINCT_RATIO

    def _inner_kind_of(self, inner: str | None) -> str:
        if inner is None:
            return "structural"
        return inner_kind(inner)

    def _bracket_kind_partition(self, lines: list[str], j: int):
        """Split a BRACKET-consistent position into content / structural
        kinds when both are present and the content side is diverse free
        text.  Resolves mixed formats sharing a signature where the
        content bracket sits at different positions (Messages-style)."""
        content_lines: list[str] = []
        structural_lines: list[str] = []
        content_inners: list[str] = []
        for line, toks in self._lines_tokens(lines):
            if j >= len(toks):
                structural_lines.append(line)
                continue
            t = toks[j].text
            if len(t) >= 2 and t[0] == "[" and t[-1] == "]":
                inner = t[1:-1]
                if inner_kind(inner) == "content":
                    content_lines.append(line)
                    content_inners.append(inner)
                else:
                    structural_lines.append(line)
            else:
                structural_lines.append(line)
        if len(content_lines) < 3:
            return None
        if len(set(content_inners)) / len(content_inners) <= CONTENT_DISTINCT_RATIO:
            return None
        if not structural_lines:
            return None  # pure content handled by _bracket_position_is_body
        return {"content": content_lines, "structural": structural_lines}

    # -- char-level refinement -------------------------------------------
    def predict_offset(self, line: str) -> tuple[int, dict]:
        """Returns (body_start_offset, info) -- ``raw[offset:]`` is the body."""
        toks = self._tok(line)
        b = self.predict_token_boundary(line)
        char = tokens_to_boundary_char(line, toks, b)
        refined = False
        if 0 < b <= len(toks):
            last = toks[b - 1]
            if (last.last_close_end is not None
                    and last.last_close_end < last.end):
                trailing = line[last.last_close_end:last.end]
                if trailing and RE_WORDCHAR.match(trailing):
                    char = last.last_close_end
                    refined = True
        info = {
            "body_start_offset": char,
            "method": "span_aware",
            "confidence": None,
            "span_boundary": b,
            "refined": refined,
        }
        return char, info

    def predict(self, line: str) -> int:
        return self.predict_offset(line)[0]


class SpanM3Prefix(SpanM3Refined):
    """E2-D: E2-C with prefix-limited span scanning (17 spans / 256 chars)."""

    name = "E2-D"

    def __init__(self):
        super().__init__(SpanAwareTokenizer(max_spans=17, max_chars=256).tokenize)

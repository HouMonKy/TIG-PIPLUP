"""SpanAwareTokenizer for the HeaderDetector E2 experiment.

A Span is a maximal run of non-whitespace characters (at bracket depth 0)
in which bracket groups ``[] () {}`` are kept WHOLE even when they contain
spaces:

    "[2026-10-13 10:00:01] [AsyncDispatcher event handler] INFO"
     -> Span("[2026-10-13 10:00:01]", 0, 22)
        Span("[AsyncDispatcher event handler]", 23, 55)
        Span("INFO", 56, 60)

Guarantees:
- ``raw[start:end] == text`` for every span (half-open interval).
- No character is deleted, replaced or reordered; spans only partition
  the original string.
- Safe fallback: a token whose brackets are UNBALANCED at end-of-line is
  re-scanned as a plain whitespace token ("[AsyncDispatcher event" stays
  one token, the next token starts at "handler").
- Adjacency splitting is minimal: only a span that STARTS with a bracket
  group splits when the group closes immediately before another opener
  ("[a][b]" -> two spans).  Prefix/suffix-attached groups stay glued
  ("sshd(pam_unix)[19939]:", "kernel[0]:", "05:29:14.000000[INFO][svc]disk"
  are single spans; attached text after a closed group is exposed via
  ``span.last_close_end`` for char-level boundary refinement).
- Angle brackets ``< >`` are NOT treated as brackets (too ambiguous in
  real logs: comparisons, generics).
- Prefix mode (E2-D): stop scanning after ``max_spans`` spans or
  ``max_chars`` characters, whichever comes first.

The tokenizer never decides what is a header -- it only exposes structure.
Whether a bracket-wrapped span belongs to the header is decided by the
detector (see the content-bracket guard in span_methods.py).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

OPENERS = "[({"
CLOSER_OF = {"[": "]", "(": ")", "{": "}"}
RE_PLAIN_TOKEN = re.compile(r"\S+")
RE_ANY_WS = re.compile(r"\s")


@dataclass
class Span:
    text: str
    start: int
    end: int
    #: char offset just past the last bracket group that closed at depth 0
    #: inside this span (None if the span contains no closed group)
    last_close_end: int | None = None

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"Span({self.text[:32]!r}, {self.start}, {self.end})"


class SpanAwareTokenizer:
    def __init__(self, max_spans: int | None = None, max_chars: int | None = None):
        self.max_spans = max_spans
        self.max_chars = max_chars

    def tokenize(self, line: str) -> list[Span]:
        spans: list[Span] = []
        n = len(line)
        i = 0
        while i < n:
            # skip whitespace
            while i < n and line[i].isspace():
                i += 1
            if i >= n:
                break
            if self.max_spans is not None and len(spans) >= self.max_spans:
                break
            limit = n if self.max_chars is None else min(n, self.max_chars)
            end, last_close_end = self._scan_span(line, i, limit)
            spans.append(Span(line[i:end], i, end, last_close_end))
            i = end
        return spans

    # ------------------------------------------------------------------
    def _scan_span(self, line: str, start: int, limit: int) -> tuple[int, int | None]:
        """Scan one span starting at ``start``; returns (end, last_close_end).

        Splitting rule (minimal adjacency): a span that STARTS with a
        bracket group ends right after that group closes when the next
        character is another opener ("[time][level]" -> two spans).
        Everything else stays glued: "sshd(pam_unix)[19939]:",
        "workerEnv.init()", "kernel[0]:", "05:29:14.000000[INFO][svc]disk"
        are ONE span each -- attached text after a closed group is
        resolved at boundary time via ``last_close_end`` (char refinement),
        not by tokenization.
        """
        n = len(line)
        depth = 0
        stack: list[str] = []
        j = start
        last_close_end: int | None = None
        starts_with_group = line[start] in OPENERS
        while j < n:
            if j >= limit and depth == 0:
                break
            c = line[j]
            if c in OPENERS:
                depth += 1
                stack.append(c)
                j += 1
                continue
            if c in "]})":
                if depth > 0 and stack and CLOSER_OF[stack[-1]] == c:
                    depth -= 1
                    stack.pop()
                    if depth == 0:
                        last_close_end = j + 1
                        j += 1
                        if (starts_with_group and j < n
                                and line[j] in OPENERS):
                            break  # adjacent pure bracket groups
                        continue
                    j += 1
                    continue
                # unmatched closer at depth 0: plain character ("2005]")
                j += 1
                continue
            if c.isspace() and depth == 0:
                break
            j += 1
        if depth > 0:
            # unbalanced bracket(s): safe fallback to plain whitespace token
            m = RE_PLAIN_TOKEN.match(line, start)
            end = m.end() if m else start + 1
            return end, None
        return j, last_close_end


#: default full-line tokenizer
span_tokenize = SpanAwareTokenizer().tokenize
#: prefix-limited tokenizer (E2-D): 17 spans (MAX_POS + 1) or 256 chars
prefix_span_tokenize = SpanAwareTokenizer(max_spans=17, max_chars=256).tokenize

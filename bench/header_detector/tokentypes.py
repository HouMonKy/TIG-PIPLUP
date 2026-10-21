"""Whitespace tokenization (with char offsets) and field-type classification.

All M1-M4 detectors in the rebuilt V1 baseline operate on whitespace tokens.
This module deliberately does NOT modify PIPLUP tokenization in any way; it
is a standalone utility used only by the HeaderDetector experiments.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Level words (case-insensitive match)
# ---------------------------------------------------------------------------
LEVEL_WORDS = {
    "debug", "dbg", "info", "in", "warn", "warning", "error", "err",
    "fatal", "crit", "critical", "trace", "notice", "emerg", "emergency",
    "alert", "severe", "config", "fine", "finer", "finest", "failure",
}

MONTH_WORDS = {
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
    "nov", "dec",
    "january", "february", "march", "april", "june", "july", "august",
    "september", "october", "november", "december",
}

WEEKDAY_WORDS = {
    "mon", "tue", "wed", "thu", "fri", "sat", "sun",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday",
}

# ---------------------------------------------------------------------------
# Regex based token shape matchers (fullmatch on a single whitespace token)
# ---------------------------------------------------------------------------
RE_TIME = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?(?:[.,:]\d+)?$")
RE_DATE_YMD = re.compile(r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}$")
RE_DATE_SLASH2 = re.compile(r"^\d{2}/\d{2}/\d{2}$")
RE_DATE_COMPACT = re.compile(r"^\d{6}$")            # 081109 (yyMMdd)
RE_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?$"
)
RE_EPOCH = re.compile(r"^\d{9,13}$")
RE_NUM = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
RE_HEXID = re.compile(r"^(?:0[xX])?[0-9a-fA-F]{4,}$")
RE_IP = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
RE_IP_PORT = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}:\d+$")
RE_HOST = re.compile(r"^(?:[A-Za-z0-9_-]+\.)+[A-Za-z0-9_-]+$")   # dotted
RE_MAC = re.compile(r"^[0-9A-Fa-f]{2}(?::-[0-9A-Fa-f]{2}){5}$")
RE_WORD = re.compile(r"^[A-Za-z]+$")
RE_ALNUM_ID = re.compile(r"^[A-Za-z0-9]*\d[A-Za-z0-9]*$")       # has digit
RE_ID_PUNCT = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_:.\-/@$]*\d[A-Za-z0-9_:.\-/@$]*$"
)
RE_PUNCT = re.compile(r"^[\[\](){}<>,;:.\-_=+*/\\|!@#$%^&~`'\"?]+$")
RE_HOST_PORT = re.compile(r"^[\w.-]+:\d+$")
RE_DOTTED = re.compile(r"^[\w.\-@]+$")  # refined below: requires a dot + alpha
RE_DASHWORD = re.compile(r"^[A-Za-z]+(?:-[A-Za-z]+)+$")  # "osd-daemon", no digits

OPENERS = "[({<"
CLOSERS = "]})>"


@dataclass
class Token:
    text: str
    start: int  # char offset in original line
    end: int    # char offset (exclusive)


def tokenize(line: str) -> list[Token]:
    """Whitespace tokenization preserving character offsets."""
    toks = []
    i = 0
    n = len(line)
    while i < n:
        while i < n and line[i].isspace():
            i += 1
        if i >= n:
            break
        j = i
        while j < n and not line[j].isspace():
            j += 1
        toks.append(Token(line[i:j], i, j))
        i = j
    return toks


def _classify_simple(tok: str) -> str:
    """Classify a bracket-free token string."""
    # colon-tailed tokens: logger components ("sshd(pam_unix)[19939]:",
    # "dfs.DataNode$PacketResponder:", "executor.Foo:").
    if len(tok) > 1 and tok.endswith(":"):
        return "CTAIL"
    low = tok.lower()
    if low in LEVEL_WORDS:
        return "LEVEL"
    if low in MONTH_WORDS:
        return "MONTH"
    if low in WEEKDAY_WORDS:
        return "WEEKDAY"
    if RE_IP_PORT.fullmatch(tok):
        return "IP_PORT"
    if RE_IP.fullmatch(tok):
        return "IP"
    if RE_MAC.fullmatch(tok):
        return "MAC"
    if RE_DATETIME.fullmatch(tok):
        return "DATETIME"
    if RE_TIME.fullmatch(tok):
        return "TIME"
    if RE_DATE_YMD.fullmatch(tok):
        return "DATE"
    if RE_DATE_SLASH2.fullmatch(tok):
        return "DATE_SLASH"
    if RE_DATE_COMPACT.fullmatch(tok):
        return "DATE_COMPACT"
    if RE_NUM.fullmatch(tok):
        if RE_EPOCH.fullmatch(tok):
            return "NUM_EPOCH"
        return "NUM"
    if RE_HOST.fullmatch(tok):
        return "HOST"
    if RE_HOST_PORT.fullmatch(tok):
        return "HOST_PORT"
    # dotted identifiers: hosts, package names, executables ("chrome.exe",
    # "nova-api.log.1.2017-05-16_13:53:08" without colon-tail, etc.)
    if "." in tok and RE_DOTTED.fullmatch(tok) and any(c.isalpha() for c in tok):
        return "DOTTED"
    if RE_HEXID.fullmatch(tok) and any(c in "abcdefABCDEF" for c in tok):
        return "HEX"
    if RE_DASHWORD.fullmatch(tok):
        return "DASHWORD"
    if RE_WORD.fullmatch(tok):
        return "WORD"
    if RE_ID_PUNCT.fullmatch(tok):
        return "ID"
    if RE_ALNUM_ID.fullmatch(tok):
        return "ID"
    if RE_PUNCT.fullmatch(tok):
        return "PUNCT"
    return "OTHER"


def classify(tok: str) -> str:
    """Classify a raw token, annotating bracket structure.

    Examples:
        "[INFO]"          -> "BRACKET:LEVEL"
        "[2017-04-23"     -> "BRACKET_OPEN"
        "10:00:00]"       -> "BRACKET_CLOSE:TIME"
        "user123"         -> "ID"
        "jk2_init()"      -> "FCALL"  (function call, weak)
    """
    if not tok:
        return "OTHER"
    # fully wrapped in matching brackets?
    if len(tok) >= 2 and tok[0] in OPENERS and tok[-1] in CLOSERS:
        inner = tok[1:-1]
        if inner and not (inner[0] in OPENERS or inner[-1] in CLOSERS):
            return "BRACKET:" + _classify_simple(inner)
    # partial square/curly brackets are structural (log header brackets)
    if tok[0] in "[{" and CLOSERS.find(tok[-1]) == -1 and len(tok) > 1:
        return "BRACKET_OPEN"
    if tok[-1] in "]}" and OPENERS.find(tok[0]) == -1 and len(tok) > 1:
        return "BRACKET_CLOSE:" + _classify_simple(tok[:-1])
    # function-call-like tokens ("jk2_init()", "listenerCallback(...)") are
    # body content, not header structure
    if tok[-1] == ")" and len(tok) > 2:
        return "FCALL"
    return _classify_simple(tok)


#: types considered "header-like" when they are positionally consistent.
#: WORD is NOT header-like (pure words only count as header when constant).
HEADERISH_TYPES = {
    "LEVEL", "TIME", "DATE", "DATE_SLASH", "DATE_COMPACT", "DATETIME",
    "MONTH", "WEEKDAY", "IP", "IP_PORT", "HOST", "HOST_PORT", "MAC", "HEX",
    "ID", "NUM", "NUM_EPOCH", "DOTTED", "CTAIL", "DASHWORD", "PUNCT",
    "BRACKET_OPEN",
}

#: families that count as STRONG header evidence.  A learned boundary is
#: only accepted (Guard A) when the consumed prefix contains at least one
#: strong position; at family-mixed split positions, only strong-family
#: partitions may continue scanning as structural sub-formats.
#: HOST/ID/NUM/DOTTED/DASHWORD/CTAIL are deliberately NOT strong: alone
#: they cannot distinguish a header field from a host-like or colon-tailed
#: body start.
STRONG_TYPES = {
    "LEVEL", "TIME", "DATE", "DATE_SLASH", "DATE_COMPACT", "DATETIME",
    "MONTH", "WEEKDAY", "MAC", "BRACKET", "BRACKET_OPEN", "BRACKET_CLOSE",
}


def is_headerish(type_: str) -> bool:
    if type_.startswith("BRACKET"):
        return True
    return type_ in HEADERISH_TYPES


def type_family(t: str) -> str:
    """Collapse detailed types to their family for consistency/signature use.

    - BRACKET:<inner>  -> BRACKET      (a wrapped field is structurally a
                                        bracket field regardless of value)
    - BRACKET_CLOSE:<inner> -> BRACKET_CLOSE
    Positional consistency and prefix signatures operate at family level;
    this avoids value-dependent signature fragmentation (e.g. an id bracket
    rendering as "[392456de]" vs "[12345678]" producing different groups).
    """
    if t.startswith("BRACKET:"):
        return "BRACKET"
    if t.startswith("BRACKET_CLOSE:"):
        return "BRACKET_CLOSE"
    return t


def signature(line: str, length: int = 4) -> tuple:
    """Short typed prefix signature used for M3 grouping (family level)."""
    toks = tokenize(line)
    sig = []
    for t in toks[:length]:
        sig.append(type_family(classify(t.text)))
    while len(sig) < length:
        sig.append("SHORT")
    return tuple(sig)


def token_boundary_to_char(line: str, b: int) -> int:
    """Convert a token-index boundary to a char offset of body start.

    b == 0 means "no header / keep original" -> 0.
    If the line has fewer than b tokens, returns 0 (keep original, safe
    fallback for truncated lines).  If b == len(tokens), returns len(line).
    """
    if b <= 0:
        return 0
    toks = tokenize(line)
    if b >= len(toks):
        if b == len(toks):
            return len(line.rstrip())
        return 0  # line shorter than learned boundary: keep original
    return toks[b].start

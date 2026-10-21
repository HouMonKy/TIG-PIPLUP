"""Independent bracket-field challenge set for E2 (fresh seed 4242).

Eight categories (experiment-plan section 4), generated with vocabulary
disjoint from gen_stress.py so the challenge set is NOT a retuning of the
optimisation-time samples:

  1. normal       plain bracket fields, no spaces inside
  2. multispace   bracket fields containing multiple/internal spaces
  3. nested       bracket fields with nested brackets
  4. unclosed     header bracket that never closes (safe-fallback case)
  5. bodybracket  bodies that START with a bracket or contain brackets,
                  plus bodies attached directly after a closing bracket
                  ("[svc]disk ...")
  6. noheader     header-less lines (some containing brackets in the body)
  7. mixed        several header formats in one stream
  8. lookalike    header fields and body fragments share the same bracket
                  style (the guard must not swallow bracketed bodies)

Ground truth is recorded at construction time.  Deterministic (seed 4242).
"""
from __future__ import annotations

import argparse
import json
import random
from datetime import datetime, timedelta
from pathlib import Path

LEVELS = ["TRACE", "INFO", "WARN", "ERROR", "FATAL"]
# fresh service/subject vocabulary (disjoint from gen_stress PROCESSES)
SERVICES = [
    "ingest-gw", "auth-edge", "ledger-svc", "notify-bus", "search-idx",
    "media-trans", "queue-keeper", "session-hub", "config-pusher",
]
FIELDS = [
    "request router", "memory watcher", "index merger", "lease janitor",
    "snapshot mover", "retry policy", "usage sampler", "gc observer",
]

BODIES = [
    "replicated shard {n} to peer {hex}",
    "compacted wal segment {n} in {n2} ms",
    " evicted {n} stale entries from lru cache",
    "resync of account {hex} finished with {n} diffs",
    "throttled producer {key}, current rate {n} msg/s",
    "failed to stat file /data/edge/{key}.idx, errno {n}",
    "node {key2} left the cluster, quorum now {n}",
    "flushed {n} bytes to /wal/{hex}.seg",
    "renewed lease for {key2} ({n} seconds remaining)",
    "backpressure: inbound queue at {n} percent capacity",
]


def _body(rng: random.Random) -> str:
    tpl = rng.choice(BODIES)

    def sub(m):
        k = m.group(1)
        if k == "n":
            return str(rng.randint(0, 100000))
        if k == "n2":
            return str(rng.randint(1, 900))
        if k == "hex":
            return "%x" % rng.getrandbits(40)
        if k == "key":
            return rng.choice(["acct-91", "topic-7", "shard-main", "q4-report"])
        if k == "key2":
            return rng.choice(["edge-3", "core-11", "relay-2", "peer-8"])
        return "?"
    import re
    return re.sub(r"\{(n2|n|hex|key2|key)\}", sub, tpl)


def _ts(rng: random.Random) -> str:
    t = datetime(2026, 11, 2, 9, 0, 0) + timedelta(seconds=rng.randint(0, 86400))
    return t.strftime("%Y-%m-%d %H:%M:%S")


def gen(rng: random.Random, n: int) -> list[dict]:
    items = []
    for i in range(n):
        cat = i % 8
        ts = _ts(rng)
        lvl = rng.choice(LEVELS)
        svc = rng.choice(SERVICES)
        field = rng.choice(FIELDS)
        body = _body(rng)
        if cat == 0:  # normal
            head = f"[{ts}] [{lvl}] [{svc}] "
            line, bs = head + body, len(head)
        elif cat == 1:  # multispace
            field2 = field.replace(" ", "  ", 1)
            head = f"[{ts}] [{lvl}] [{field2} ] "
            line, bs = head + body, len(head)
        elif cat == 2:  # nested
            zone = f"zone-{rng.randint(1, 4)}"
            head = f"[{ts}] [{lvl}] [pool[{zone}] worker] "
            line, bs = head + body, len(head)
        elif cat == 3:  # unclosed bracket in header (fallback case)
            head = f"[{ts}] [{lvl}] [{field}"
            line = head + " " + body
            bs = len(f"[{ts}] [{lvl}] [")  # GT: keep the partial bracket?
            # correct behaviour for an unclosed field is ambiguous; the
            # safest defensible GT: header = the two closed fields, the
            # unclosed fragment belongs to the body (detector should not
            # swallow free text after an unbalanced '[').
            bs = len(f"[{ts}] [{lvl}] ")
        elif cat == 4:  # body contains / starts with brackets, attached body
            r = i % 3
            if r == 0:  # body starts with a content bracket
                head = f"[{ts}] [{lvl}] [{svc}] "
                line, bs = head + f"[{_body(rng)}] done", len(head)
            elif r == 1:  # body contains a bracket mid-way
                head = f"[{ts}] [{lvl}] [{svc}] "
                line = head + f"found [orphan seg {rng.randint(1, 99)}] in wal"
                bs = len(head)
            else:  # body attached right after the closing bracket
                head = f"[{ts}] [{lvl}] [{svc}]"
                line = head + body  # NO space: "[svc]replicated ..."
                bs = len(head)
        elif cat == 5:  # no header
            if i % 2 == 0:
                line, bs = _body(rng), 0
            else:  # no header but brackets inside the body
                line = f"status [ok {rng.randint(1, 99)}] reported by {svc}"
                bs = 0
        elif cat == 6:  # mixed formats in one stream
            r = i % 3
            if r == 0:
                head = f"[{ts}] [{lvl}] [{svc}] "
            elif r == 1:
                d = datetime(2026, 11, 2, 9, 0, 0) + timedelta(
                    seconds=rng.randint(0, 86400))
                head = d.strftime("%b %d %H:%M:%S ") + f"edge{rng.randint(1,9)} {svc}[{rng.randint(100,999)}]: "
            else:
                head = f"{ts} {lvl} {svc}: "
            line, bs = head + body, len(head)
        else:  # 7: lookalike -- bracketed header fields vs bracketed bodies
            # rng-based subvariant (i%2 is constant for i%8==7 lines)
            r = rng.randint(0, 1)
            if r == 0:
                # 4th bracket is a CONTENT bracket (diverse free text)
                head = f"[{ts}] [{lvl}] [{svc}] "
                line = head + f"[{_body(rng)}] confirmed"
                bs = len(head)
            else:
                # 4th bracket holds a varying user id (single token):
                # structurally indistinguishable from a header field --
                # registered as the known hard case
                head = f"[{ts}] [{lvl}] [{svc}] "
                line = head + f"[user{rng.randint(1000, 9999)}] opened session"
                bs = len(head)
        items.append({
            "line": line,
            "body_start": bs,
            "format": f"cat{cat}",
            "has_header": bs > 0,
            "category": cat,
        })
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="bench/header_data/challenge")
    ap.add_argument("--n", type=int, default=1600)
    ap.add_argument("--seed", type=int, default=4242)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    items = gen(rng, args.n)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with (out / "challenge-brackets.jsonl").open("w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    from collections import Counter
    print(json.dumps(Counter(it["format"] for it in items), indent=1))


if __name__ == "__main__":
    main()

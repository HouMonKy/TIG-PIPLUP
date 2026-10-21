"""Stress-set generation for the HeaderDetector experiments.

The historical V1 stress sets are lost together with the original code
(commit ac30623d is missing from the repository).  These stress sets are
REBUILT from the real business log format configurations in
``config/format_chaos.json`` (module formats cfg/deploy/fdsa/... as used by
PIPLUP) plus hand-designed challenge cases named in the V1.1 experiment plan
(bracket fields containing spaces, pure-word headers, host-like body starts,
header-less lines, bracket-packed Messages format).

Ground truth is exact by construction: ``body_start`` is recorded while the
line is rendered from its format string.

All generation is deterministic under a fixed seed (42).
"""
from __future__ import annotations

import argparse
import json
import random
import re
from datetime import datetime, timedelta
from pathlib import Path

PLACEHOLDER_RE = re.compile(r"<[A-Za-z0-9_]+>")

LEVELS = ["INFO", "WARN", "ERROR", "DEBUG", "NOTICE"]
PROCESSES = [
    "main-worker", "scheduler", "dme-server", "msg-handler", "ism-agent",
    "osd-daemon", "vbs-server", "sync-task", "ha-monitor", "rep-worker",
]
COMPONENTS = [
    "DataNode", "NameNode", "TaskScheduler", "VolumeManager", "HeartbeatMgr",
    "MetaService", "CacheMgr", "NetServer", "AuthModule", "ClusterMgr",
]

# Body templates with {}-style parameter slots.  Values are filled with
# realistic parameter-like tokens (ids / numbers / paths / ips).
BODY_TEMPLATES = [
    "start to sync volume data, total {n} chunks",
    "receive heartbeat from node-{n}, load {p} percent",
    "failed to read block blk_{hex}, retry {n} times",
    "update config key {key} to value {n}",
    "connection reset by peer, socket fd={n}",
    "process request op={key} done in {n} ms",
    "volume {key} changed state from active to degraded",
    "write {n} bytes to /data/dsware/{key}/chunk.dat",
    "checkpoint finished, elapsed {n} seconds",
    "metadata journal truncated at offset {hex}",
    "disk {n} smart health status ok, temperature {n} celsius",
    "received message from 10.{n}.{n}.{n}:{n}, dispatch to handler",
    "cache entry {hex} evicted, free slots {n}",
    "task {key} submitted by user admin, priority {n}",
    "rpc call GetVolumeInfo returned code {n}, msg success",
    "alarm cleared: disk usage back to {p} percent",
    "send audit event to kafka topic dsware-audit, seq={hex}",
    "init module {key} begin, worker threads {n}",
    "thread pool exhausted, active={n} queued={n} max={n}",
    "certificate will expire in {n} days, renew required",
]


def _fill_body(rng: random.Random) -> str:
    tpl = rng.choice(BODY_TEMPLATES)
    def sub(m):
        k = m.group(1)
        if k == "n":
            return str(rng.randint(0, 100000))
        if k == "p":
            return str(rng.randint(1, 99))
        if k == "hex":
            return "%x" % rng.getrandbits(48)
        if k == "key":
            return rng.choice(["vol-3f2a", "cfg.max.io", "task-991", "snap-7c", "quota.db"])
        return "?"
    return re.sub(r"\{(n|p|hex|key)\}", sub, tpl)


class FormatRenderer:
    """Render a format_chaos-style format string into a log line."""

    def __init__(self, fmt: str, time_formats: list[str], fid: str):
        self.fmt = fmt
        self.time_formats = time_formats or ["%Y-%m-%d %H:%M:%S"]
        self.fid = fid
        # tokenize into parts: literals and placeholders
        self.parts = []  # list of ("lit", text) | ("ph", name)
        pos = 0
        for m in PLACEHOLDER_RE.finditer(fmt):
            if m.start() > pos:
                self.parts.append(("lit", self._unescape(fmt[pos:m.start()])))
            self.parts.append(("ph", m.group(0)[1:-1]))
            pos = m.end()
        if pos < len(fmt):
            self.parts.append(("lit", self._unescape(fmt[pos:])))

    @staticmethod
    def _unescape(s: str) -> str:
        return s.replace("\\[", "[").replace("\\]", "]") \
                .replace("\\(", "(").replace("\\)", ")") \
                .replace("\\^", "^").replace("\\\\", "\\")

    def _gen_value(self, name: str, rng: random.Random, base_time: datetime) -> str:
        low = name.lower()
        if low == "time" or low == "time2":
            tf = rng.choice(self.time_formats)
            t = base_time + timedelta(seconds=rng.randint(0, 86400))
            try:
                return t.strftime(tf)
            except ValueError:
                return t.strftime("%Y-%m-%d %H:%M:%S")
        if low == "level":
            return rng.choice(LEVELS)
        if low == "ip":
            return "10.%d.%d.%d" % (rng.randint(0, 255), rng.randint(0, 255), rng.randint(1, 254))
        if low == "content":
            return _fill_body(rng)
        if low in ("path",):
            return "/data/dsware/vol-%02d" % rng.randint(1, 64)
        if low in ("line",):
            return str(rng.randint(100, 9999))
        if low in ("codeline",):
            return "%s.cpp:%d" % (rng.choice(["msg_handler", "volume_mgr", "meta_svc", "net_srv"]), rng.randint(100, 3000))
        if low in ("pidtid",):
            return "pid:%d,tid:%d" % (rng.randint(10000, 60000), rng.randint(10000, 60000))
        if low in ("module",):
            return rng.choice(COMPONENTS)
        if low in ("main",):
            return rng.choice(["main", "worker-%d" % rng.randint(1, 16), "async-pool"])
        if low in ("fsm",):
            return rng.choice(["IDLE", "SYNCING", "DEGRADED", "RECOVERING"])
        if low in ("ummm", "umm"):
            return "UMM"
        if low in ("action",):
            return rng.choice(["sync", "query", "report", "hb", "cleanup"])
        if low in ("process", "process2", "processs", "progress"):
            return rng.choice(PROCESSES)
        if low in ("server",):
            return "dsware-%d" % rng.randint(1, 8)
        # ids: ID, ID1, ID2, Id, Id2, id, id2, tid, osd, pid ...
        return "%08x" % rng.getrandbits(32)

    def render(self, rng: random.Random, base_time: datetime):
        """Returns (line, body_start).  body_start is the char offset where
        the message content starts; 0 means the whole line is content."""
        out = []
        body_start = None
        for kind, val in self.parts:
            if kind == "lit":
                out.append(val)
            else:
                rendered = self._gen_value(val, rng, base_time)
                if val.lower() == "content":
                    # content directly attached to a preceding '[' bracket:
                    # GT boundary = the bracket itself (bracket field kept in body)
                    if out and out[-1].endswith("["):
                        body_start = sum(len(x) for x in out) - 1
                    else:
                        body_start = sum(len(x) for x in out)
                out.append(rendered)
        line = "".join(out)
        if body_start is None:
            body_start = 0
        return line, body_start


def load_module_formats(config_path: Path) -> dict:
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    out = {}
    for module, spec in cfg.items():
        renderers = []
        for i, fmt in enumerate(spec.get("log_format", [])):
            renderers.append(FormatRenderer(fmt, spec.get("time_format", []), f"{module}#{i}"))
        out[module] = renderers
    return out


# ---------------------------------------------------------------------------
# Hand-designed challenge sets
# ---------------------------------------------------------------------------

def gen_bracket_space(rng: random.Random, n: int):
    """Bracket fields containing spaces (multi-token bracket fields)."""
    items = []
    bodies = [_fill_body(rng) for _ in range(n)]
    for i in range(n):
        r = i % 4
        body = bodies[i]
        if r == 0:
            # fixed-content bracket field with spaces (Hadoop-style)
            line = f"[{ts(rng)}] [INFO] [AsyncDispatcher event handler] {body}"
            bs = len(f"[{ts(rng)}] [INFO] [")
            # recompute precisely
            head = f"[{ts2(rng)}] [INFO] [AsyncDispatcher event handler] "
            line = head + body
            bs = len(head)
        elif r == 1:
            # variable bracket field: [pool-N thread M]
            head = f"[{ts2(rng)}] [INFO] [pool-{rng.randint(1,9)} thread {rng.randint(1,64)}] "
            line = head + body
            bs = len(head)
        elif r == 2:
            # apache-style id bracket with inner spaces
            head = f"[{ts2(rng)}] [req-{hexid(rng)} - - -] "
            line = head + body
            bs = len(head)
        else:
            # bracket field with variable-length mixed inner tokens
            inner = " ".join(rng.choice(["dispatcher", "for", "event", "handler", "pool"]) for _ in range(rng.randint(2, 3)))
            head = f"[{ts2(rng)}] [INFO] [{inner}{rng.randint(1,99)}] "
            line = head + body
            bs = len(head)
        items.append({"line": line, "body_start": bs, "format": f"bs{r}", "has_header": True})
    return items


def gen_pure_word(rng: random.Random, n: int):
    """Headers composed of (mostly) plain words - no brackets, no timestamps
    at the head, e.g. the fma formats 'CST <Time> TIP: <process> : <Content>'."""
    items = []
    for i in range(n):
        r = i % 4
        t = (datetime(2026, 3, 1) + timedelta(seconds=rng.randint(0, 864000))).strftime("%Y-%m-%d %H:%M:%S")
        body = _fill_body(rng)
        if r == 0:
            head = f"CST {t} TIP: {rng.choice(PROCESSES)} : "
        elif r == 1:
            head = f"CST {t} DEBUG: {rng.choice(PROCESSES)} : "
        elif r == 2:
            head = f"System service {rng.choice(COMPONENTS)} report: "
        else:
            head = f"DSWARE KMC audit {t}: "
        items.append({"line": head + body, "body_start": len(head), "format": f"pw{r}", "has_header": True})
    return items


def gen_host_body(rng: random.Random, n: int):
    """Bodies that START with host-like / user-like tokens (user123, node01).
    A detector must not swallow them into the header."""
    items = []
    host_like = [
        "user{n}", "node{n2}", "srv-dsware-{n}", "vm-{hex}",
        "admin{n}", "db-{n}", "client-{hex}",
    ]
    for i in range(n):
        t = (datetime(2026, 3, 1) + timedelta(seconds=rng.randint(0, 864000))).strftime("%Y-%m-%d %H:%M:%S")
        r = i % 5
        if r == 0:
            head = f"{t} {rng.choice(LEVELS)} "
        elif r == 1:
            head = f"[{t}] [{rng.choice(LEVELS)}] "
        elif r == 2:
            head = f"{t} {rng.choice(LEVELS)} {rng.choice(PROCESSES)} "
        elif r == 3:
            head = f"[{t}][{rng.choice(LEVELS)}][{rng.choice(PROCESSES)}] "
        else:
            head = f"{t} {rng.choice(LEVELS)} [{rng.choice(PROCESSES)}] "
        if rng.random() < 0.85:
            # host-like body start (the documented challenge)
            h = rng.choice(host_like)
            first = h.format(n=rng.randint(100, 999), n2=rng.randint(1, 64), hex="%x" % rng.getrandbits(16))
            rest = rng.choice(["opened connection to database", "reported disk usage exceeded threshold",
                               "synced {n} files in {n2} seconds", "restarted after crash, exit code {n}",
                               "mounted volume vol-{hex} successfully"])
            rest = rest.replace("{n}", str(rng.randint(1, 500))).replace("{n2}", str(rng.randint(1, 60))).replace("{hex}", "%x" % rng.getrandbits(16))
            body = first + " " + rest
        else:
            body = _fill_body(rng)
        items.append({"line": head + body, "body_start": len(head), "format": f"hb{r}", "has_header": True})
    return items


def gen_noheader(rng: random.Random, n: int):
    """Half of the lines have NO header at all (correct behaviour = keep)."""
    items = []
    for i in range(n):
        if i % 2 == 0:
            body = _fill_body(rng)
            items.append({"line": body, "body_start": 0, "format": "none", "has_header": False})
        else:
            t = (datetime(2026, 3, 1) + timedelta(seconds=rng.randint(0, 864000))).strftime("%Y-%m-%d %H:%M:%S")
            r = i % 4
            if r == 1:
                head = f"[{t}] [{rng.choice(LEVELS)}] "
            elif r == 3:
                head = f"{t} {rng.choice(LEVELS)} "
            else:
                head = f"{t} {rng.choice(LEVELS)} {rng.choice(PROCESSES)} "
            items.append({"line": head + _fill_body(rng), "body_start": len(head), "format": f"nh{r}", "has_header": True})
    return items


def ts(rng):
    return "2026-03-01 10:00:00"

def ts2(rng: random.Random) -> str:
    return (datetime(2026, 3, 1) + timedelta(seconds=rng.randint(0, 864000))).strftime("%Y-%m-%d %H:%M:%S")

def hexid(rng: random.Random) -> str:
    return "%x" % rng.getrandbits(32)


# ---------------------------------------------------------------------------
# Main generation
# ---------------------------------------------------------------------------

STRESS_SPECS = [
    # (name, source, n)
    ("stress-deploy", "module:deploy", 1000),
    ("stress-oam-console", "module:oam-console", 1000),
    ("stress-dsware", "module:fsa+fsm", 1000),
    ("stress-messages", "module:Messages", 1000),
    ("stress-bracket-space", "gen:bracket_space", 1000),
    ("stress-pure-word", "gen:pure_word", 1000),
    ("stress-host-body", "gen:host_body", 1000),
    ("stress-noheader", "gen:noheader", 1000),
]


def generate_all(out_dir: Path, config_path: Path, seed: int = 42):
    module_formats = load_module_formats(config_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for si, (name, source, n) in enumerate(STRESS_SPECS):
        rng = random.Random(seed + si)
        base_time = datetime(2026, 3, 1, 8, 0, 0)
        items = []
        if source.startswith("module:"):
            mods = source[len("module:"):].split("+")
            renderers = []
            for mod in mods:
                renderers.extend(module_formats.get(mod, []))
            if not renderers:
                raise RuntimeError(f"no formats for {mods}")
            for i in range(n):
                r = renderers[rng.randrange(len(renderers))]
                line, bs = r.render(rng, base_time)
                items.append({"line": line, "body_start": bs, "format": r.fid,
                              "has_header": bs > 0})
        else:
            gen = {"bracket_space": gen_bracket_space, "pure_word": gen_pure_word,
                   "host_body": gen_host_body, "noheader": gen_noheader}[source[4:]]
            items = gen(rng, n)
        with (out_dir / f"{name}.jsonl").open("w", encoding="utf-8") as f:
            for it in items:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
        n_hdr = sum(1 for it in items if it["has_header"])
        fmts = sorted({it["format"] for it in items})
        manifest[name] = {"n": len(items), "n_with_header": n_hdr, "n_formats": len(fmts)}
    return manifest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="bench/header_data/stress")
    ap.add_argument("--config", default="config/format_chaos.json")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    manifest = generate_all(Path(args.out), Path(args.config), args.seed)
    print(json.dumps(manifest, indent=1))


if __name__ == "__main__":
    main()

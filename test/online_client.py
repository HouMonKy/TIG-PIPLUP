"""
在线服务端到端验证客户端（DME 模式）。

流程：
1. 读取治理数据中场景 01 的一份 all_merged_logs.log 前 300 行
2. POST /lognet_parse  灌入日志（不传 timestamp，由服务端从 <Time> 字段提取）
3. 从原始行解析时间戳，算出检测窗口
4. POST /abnormal-check  做异常检测
5. 打印结果摘要

成功标准：两端无异常 + 合法 JSON 结构（小样本可能检不出异常，属正常）。
"""
import os
import re
import json
from pathlib import Path
from datetime import datetime

BASE_URL = "http://127.0.0.1:5001"
MODULE = "Messages"
SAMPLE_LINES = 300
GOVERNED_DATA_ROOT = r"D:\DME-data\治理数据"
TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?")

# --- HTTP 工具：优先 httpx，失败回退 urllib ---
try:
    import httpx
    _client = httpx.Client(trust_env=False, timeout=300.0)  # 不读系统代理环境变量

    def post_json(url, payload):
        r = _client.post(url, json=payload)
        r.raise_for_status()
        return r.json()

    def get_json(url):
        return _client.get(url).json()

except ImportError:
    import urllib.request
    _opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 空代理 → 绕过

    def post_json(url, payload):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"})
        with _opener.open(req, timeout=300) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get_json(url):
        with _opener.open(url, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))


def find_sample_file():
    """在治理数据中按场景号 01 找第一份 all_merged_logs.log"""
    root = Path(GOVERNED_DATA_ROOT)
    if not root.exists():
        raise RuntimeError(f"治理数据根目录不存在: {root}")
    # 优先取场景 01
    candidates = []
    for d in sorted(root.iterdir()):
        if d.is_dir() and d.name.startswith("01_"):
            candidates.extend(d.rglob("all_merged_logs.log"))
    # 兜底：任意场景
    if not candidates:
        candidates = list(root.rglob("all_merged_logs.log"))
    if not candidates:
        raise RuntimeError(f"{root} 下未找到 all_merged_logs.log")
    return candidates[0]


def load_sample(path, n=SAMPLE_LINES):
    """读取前 n 行，做与离线流水线一致的 '] [' -> '][' 归一化"""
    logs = []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if len(logs) >= n:
                break
            line = line.replace("] [", "][")
            logs.append(line)
    return logs


def parse_ts_ms(line):
    """从原始行提取毫秒级时间戳（仅用于客户端计算检测窗口）"""
    m = TIME_RE.search(line)
    if not m:
        return None
    s = m.group(0)
    fmt = "%Y-%m-%d %H:%M:%S.%f" if "." in s else "%Y-%m-%d %H:%M:%S"
    return datetime.strptime(s, fmt).timestamp() * 1000.0


def main():
    # 1. 健康检查
    try:
        h = get_json(f"{BASE_URL}/hello")
        print(f"[health] /hello -> {h}")
    except Exception as e:
        raise RuntimeError(f"健康检查失败，请确认服务已启动: {e}")

    # 2. 加载样本
    path = find_sample_file()
    print(f"[data] 样本文件: {path}")
    # source 取 Messages 目录的上两级（如 controller_0A），解析与检测阶段须用同一字符串
    source = path.parent.parent.name
    print(f"[data] 推导 source: {source}")
    lines = load_sample(path)
    print(f"[data] 已加载 {len(lines)} 行")

    # 3. 构建 logInfo（不带 timestamp，验证服务端提取逻辑）
    log_info = [
        {"rawLog": ln, "module": MODULE, "source": source, "logId": str(i)}
        for i, ln in enumerate(lines)
    ]

    # 4. POST /lognet_parse
    print(f"[parse] POST /lognet_parse ({len(log_info)} 条日志)...")
    t0 = datetime.now()
    pr = post_json(f"{BASE_URL}/lognet_parse", {"logInfo": log_info})
    dt = (datetime.now() - t0).total_seconds()
    print(f"[parse] 完成，耗时 {dt:.2f}s，响应键: {list(pr.keys())}")

    # 5. 从原始行计算检测窗口
    ts_list = [t for t in (parse_ts_ms(l) for l in lines) if t is not None]
    if not ts_list:
        raise RuntimeError("未能从样本解析出任何时间戳，无法构建检测窗口")
    start_ts = int(min(ts_list)) - 1000
    end_ts = int(max(ts_list)) + 1000
    print(f"[detect] 窗口: start={start_ts} end={end_ts} "
          f"(跨度 {(max(ts_list) - min(ts_list)) / 1000:.1f}s)")

    # 6. POST /abnormal-check
    payload = {
        "startTime": start_ts,
        "endTime": end_ts,
        "windowSize": 180000,
        "module": MODULE,
        "source": source,
    }
    print(f"[detect] POST /abnormal-check (module={MODULE}, source={source})...")
    t0 = datetime.now()
    resp = post_json(f"{BASE_URL}/abnormal-check", payload)
    dt = (datetime.now() - t0).total_seconds()
    print(f"[detect] 完成，耗时 {dt:.2f}s")
    print("[detect] 响应:")
    print(json.dumps(resp, indent=2, ensure_ascii=False, default=str))

    # 7. 摘要
    data = resp.get("data", [])
    event = resp.get("event", [])
    print("\n========== 摘要 ==========")
    print(f"灌入日志数           : {len(log_info)}")
    print(f"abnormal-check data  : {len(data)} 项")
    print(f"abnormal-check events: {len(event)} 个事件")
    if event:
        print("检出的事件（前 3 个）:")
        for ev in event[:3]:
            print(f"  - {ev.get('summary', ev)}")
    print("✅ 成功：服务返回合法 JSON 结构。")


if __name__ == "__main__":
    main()

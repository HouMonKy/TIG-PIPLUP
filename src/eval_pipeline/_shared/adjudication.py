"""LLM 裁决核心逻辑。复制自 gt_adjudication/ 各模块并整合。

包含：prompt 构建、引擎、结果解析、状态管理、运行入口。
"""
import json
import re
import time
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple

from eval_pipeline.config import (
    ADJ_ORIGINAL, ADJ_OPTIMIZED, ADJ_EQUIVALENT, ADJ_NEITHER, ADJ_INSUFFICIENT,
    VALID_ADJUDICATIONS,
    STATUS_RESOLVED, STATUS_FAILED,
    RETRY_JSON_PARSE_MAX,
    RATE_LIMIT_FREE_START_HOUR, RATE_LIMIT_FREE_END_HOUR,
    TASKS_DIR, RESULTS_DIR, PROMPTS_DIR,
    ALL_TASKS_FILE, RESOLVED_FILE, FAILED_FILE, RAW_RESPONSES_FILE,
)
from eval_pipeline._shared.llm_client import (
    LLMClient, RateLimitExceeded, create_client,
)
from eval_pipeline._shared.data_loader import load_jsonl, write_jsonl, load_json, write_json

logger = logging.getLogger(__name__)

PROGRESS_LOG_INTERVAL = 30
_flush_lock = threading.Lock()


# ===== Prompt 构建 =====

SYSTEM_PROMPT = """你是日志模板质量评判专家。请根据 SampleLogs 判断两个候选模板哪个更好。

背景：两个模板来自同一日志解析算法的不同版本（原版 vs 优化版），对同一批日志产生了不同的模板。你需要判断哪个模板更准确。

评判规则：
1. 必须优先分析 SampleLogs，两个候选模板仅供参考。
2. 模板只能使用 `<*>` 作为占位符。
3. ID、数字、时间、地址、路径等运行时数据应视为动态内容，替换为 <*>。
4. 保留更多有意义结构信息的模板更好。例如 `tracepoint(<*>)` 比 `<*>` 更好，因为它保留了字段名语义。
5. 在能匹配所有 SampleLogs 的前提下，优先选择固定语义完整、占位符更少、泛化能力更强的模板。

评判选项：
- `original`：原版模板更好
- `optimized`：优化版模板更好
- `equivalent`：两者语义等价（差异不影响匹配或语义）

只能输出合法 JSON，不得输出分析过程、Markdown 或额外文本：

{
"Adjudication": "original|optimized|equivalent",
"Reason": "简要说明评判依据，指出哪个模板保留/丢失了什么信息",
"Confidence": 0.95
}"""


def build_adjudication_prompt(task: dict) -> list:
    """构建裁决 Prompt。返回 messages 列表。"""
    sample_logs = "\n".join(task.get("sample_log", []))
    user_msg = f"""待分析数据：

SampleLogs:
{sample_logs}

OriginalTemplate:
{task.get('piplup_ori', '')}

OptimizedTemplate:
{task.get('piplup_opt', '')}"""

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]


# ===== 结果解析 =====

def _extract_json_from_response(text: str) -> Optional[dict]:
    """从 LLM 响应中提取 JSON 对象。处理 Markdown 代码块包裹。"""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    start = text.find('{')
    end = text.rfind('}')
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    return None


def parse_and_validate(raw_response: str) -> Tuple[dict, str]:
    """从 LLM 响应中提取裁决结果。

    Returns:
        (result_dict, status) — result_dict 包含 Adjudication/Reason/Confidence，
        status 为 STATUS_RESOLVED 或 STATUS_FAILED。
    """
    parsed = _extract_json_from_response(raw_response)
    if parsed is None:
        return {"ParseError": "JSON 提取失败"}, STATUS_FAILED

    adj = parsed.get("Adjudication", "")
    if adj not in VALID_ADJUDICATIONS:
        return {"ParseError": f"无效 Adjudication: {adj}"}, STATUS_FAILED

    confidence = parsed.get("Confidence", 0)
    reason = parsed.get("Reason", "")

    result = {
        "Adjudication": adj,
        "Reason": reason,
        "Confidence": confidence,
    }

    return result, STATUS_RESOLVED


# ===== 引擎 =====

def execute_task(task: dict, client: LLMClient) -> tuple:
    """执行单个裁决任务。

    Args:
        task: 含 seq / piplup_ori / piplup_opt / sample_log 的任务字典
        client: LLM 客户端（需有 .chat(messages) 方法）

    Returns:
        (result, status, raw_responses)
    """
    raw_responses = []
    messages = build_adjudication_prompt(task)

    for attempt in range(1 + RETRY_JSON_PARSE_MAX):
        raw = client.chat(messages)
        raw_responses.append(raw)

        result, status = parse_and_validate(raw)
        if status != STATUS_FAILED:
            return result, status, raw_responses

        if attempt < RETRY_JSON_PARSE_MAX:
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": "上一次输出不是合法 JSON。请仅输出合法 JSON。"})

    logger.warning(f"Task GT_{task.get('seq', 0):04d}: JSON 解析重试耗尽")
    return {"ParseError": "JSON 解析失败", "RetryCount": len(raw_responses)}, STATUS_FAILED, raw_responses


# ===== 状态管理 =====

class StateManager:
    """裁决任务状态管理器。

    核心逻辑：
    - 不可变总账 all_gt_tasks.jsonl：从输入文件转换，每条加 TaskID
    - 结果文件：resolved_gt_tasks.jsonl + failed_gt_tasks.jsonl + raw_gt_responses.jsonl
    - get_pending_tasks(): 计算 done_ids = resolved ∪ failed，返回未完成任务
    - record_result(): 追加到内存 dict
    - flush(): 全量重写结果文件
    """

    def __init__(self, run_root: Path):
        self.run_root = Path(run_root)
        self.tasks_dir = self.run_root / TASKS_DIR
        self.results_dir = self.run_root / RESULTS_DIR
        self.prompts_dir = self.run_root / PROMPTS_DIR

        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.prompts_dir.mkdir(parents=True, exist_ok=True)

        all_tasks_path = self.tasks_dir / ALL_TASKS_FILE
        if not all_tasks_path.exists():
            raise FileNotFoundError(f"任务总账不存在: {all_tasks_path}。请先运行 prepare。")
        self._all_tasks = load_jsonl(all_tasks_path)
        self._task_by_id = {t["TaskID"]: t for t in self._all_tasks}

        self._resolved = self._load_results(RESOLVED_FILE)
        self._failed = self._load_results(FAILED_FILE)
        self._raw_responses = self._load_results(RAW_RESPONSES_FILE)

        logger.info(
            f"StateManager 初始化: 总任务 {self.total_tasks}, "
            f"resolved={self.resolved_count}, failed={self.failed_count}, "
            f"pending={self.pending_count}"
        )

    def _load_results(self, filename: str) -> Dict[str, dict]:
        path = self.results_dir / filename
        if not path.exists():
            return {}
        records = load_jsonl(path)
        return {r["TaskID"]: r for r in records}

    @property
    def total_tasks(self) -> int:
        return len(self._all_tasks)

    @property
    def resolved_count(self) -> int:
        return len(self._resolved)

    @property
    def failed_count(self) -> int:
        return len(self._failed)

    @property
    def pending_count(self) -> int:
        done_ids = set(self._resolved.keys()) | set(self._failed.keys())
        return sum(1 for t in self._all_tasks if t["TaskID"] not in done_ids)

    @property
    def failed_task_ids(self) -> List[str]:
        return list(self._failed.keys())

    def get_pending_tasks(self, include_failed: bool = False) -> List[dict]:
        if include_failed:
            done_ids = set(self._resolved.keys())
        else:
            done_ids = set(self._resolved.keys()) | set(self._failed.keys())
        return [t for t in self._all_tasks if t["TaskID"] not in done_ids]

    def clear_failed(self):
        count = len(self._failed)
        self._failed.clear()
        logger.info(f"已清空 {count} 条 failed 记录，这些任务将重新处理")

    def record_result(self, task_id: str, result: dict, raw_response: str, status: str):
        task = self._task_by_id.get(task_id, {})
        if status == STATUS_RESOLVED:
            record = {**task, **result, "TaskID": task_id, "Status": status}
            self._resolved[task_id] = record
        elif status == STATUS_FAILED:
            record = {**task, **result, "TaskID": task_id, "Status": status}
            self._failed[task_id] = record
        self._raw_responses[task_id] = {
            "TaskID": task_id,
            "RawResponse": raw_response,
        }

    def flush(self):
        write_jsonl(self.results_dir / RESOLVED_FILE, list(self._resolved.values()))
        write_jsonl(self.results_dir / FAILED_FILE, list(self._failed.values()))
        write_jsonl(self.results_dir / RAW_RESPONSES_FILE, list(self._raw_responses.values()))

    def get_resolved_tasks(self) -> List[dict]:
        return list(self._resolved.values())


# ===== 限流守卫 =====

def _is_in_free_window() -> bool:
    now_hour = datetime.now().hour
    if RATE_LIMIT_FREE_START_HOUR > RATE_LIMIT_FREE_END_HOUR:
        return now_hour >= RATE_LIMIT_FREE_START_HOUR or now_hour < RATE_LIMIT_FREE_END_HOUR
    else:
        return RATE_LIMIT_FREE_START_HOUR <= now_hour < RATE_LIMIT_FREE_END_HOUR


def _seconds_until_free_window() -> float:
    now = datetime.now()
    target = now.replace(hour=RATE_LIMIT_FREE_START_HOUR, minute=0, second=0, microsecond=0)
    if now.hour >= RATE_LIMIT_FREE_START_HOUR:
        target += timedelta(days=1)
    delta = (target - now).total_seconds()
    return max(delta, 0)


def _wait_for_free_window(reason: str = "日配额耗尽"):
    if _is_in_free_window():
        logger.info(f"[限流] {reason}，但当前已在不限流时段，短暂等待后重试...")
        time.sleep(30)
        return

    wait_secs = _seconds_until_free_window()
    resume_time = datetime.now() + timedelta(seconds=wait_secs)
    logger.warning(
        f"[限流] {reason}，等待不限流时段 "
        f"({RATE_LIMIT_FREE_START_HOUR:02d}:00)..."
    )
    logger.warning(f"[限流] 预计 {resume_time.strftime('%Y-%m-%d %H:%M:%S')} 恢复执行 "
                   f"(等待 {wait_secs / 60:.0f} 分钟)")

    elapsed = 0
    while elapsed < wait_secs:
        chunk = min(60, wait_secs - elapsed)
        time.sleep(chunk)
        elapsed += chunk
        if elapsed < wait_secs:
            remaining = wait_secs - elapsed
            logger.info(f"[限流] 等待中... 剩余 {remaining / 60:.0f} 分钟")

    logger.info(f"[限流] 已进入不限流时段，恢复执行")


# ===== 运行入口 =====

def _write_final_output(run_root: Path, input_path: Path, state: StateManager):
    """将裁决结果回写到原始 JSONL 文件，添加 adjudication 和 reason 字段。"""
    original = load_jsonl(input_path)
    resolved = state.get_resolved_tasks()
    seq_to_result = {}
    for r in resolved:
        task_seq = r.get("seq", 0)
        seq_to_result[task_seq] = r

    for rec in original:
        seq = rec.get("seq", 0)
        if seq in seq_to_result:
            r = seq_to_result[seq]
            rec["adjudication"] = r.get("Adjudication", "")
            rec["reason"] = r.get("Reason", "")

    output_path = run_root / "inconsistent_for_adjudication.jsonl"
    write_jsonl(output_path, original)
    logger.info(f"最终输出已写入: {output_path} ({len(original)} 条)")


def _run_sequential(pending, client, dry_run, mock, run_root, state, start_time, input_path):
    """串行执行任务，含限流自适应。"""
    done_count = 0
    last_flush_time = start_time
    i = 0

    while i < len(pending):
        task = pending[i]
        task_id = task["TaskID"]

        messages = build_adjudication_prompt(task)
        prompt_dir = run_root / "prompts"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        write_json(prompt_dir / f"{task_id}.json", {"messages": messages})

        if dry_run and mock:
            logger.info(f"[dry-run] Task {task_id}: Prompt 已生成，跳过调用")
            done_count += 1
            i += 1
            continue

        try:
            result, status, raw_responses = execute_task(task, client)
        except RateLimitExceeded as e:
            _wait_for_free_window(reason=str(e))
            continue
        except Exception as e:
            logger.error(f"Task {task_id}: 裁决执行失败: {e}")
            result = {"ParseError": str(e)}
            status = STATUS_FAILED
            raw_responses = [str(e)]

        raw_response = "\n---RETRY---\n".join(raw_responses) if len(raw_responses) > 1 else (raw_responses[0] if raw_responses else "")

        with _flush_lock:
            state.record_result(task_id, result, raw_response, status)
        logger.info(f"Task {task_id} → {status} ({result.get('Adjudication', 'N/A')})")

        done_count += 1
        i += 1

        now = time.time()
        if done_count % 5 == 0 or (now - last_flush_time) > PROGRESS_LOG_INTERVAL:
            elapsed = now - start_time
            rate = done_count / elapsed if elapsed > 0 else 0
            remaining = (len(pending) - done_count) / rate if rate > 0 else 0
            total_done = state.resolved_count + state.failed_count
            logger.info(
                f"进度: {done_count}/{len(pending)} (本轮) | "
                f"总计: {total_done}/{state.total_tasks} "
                f"({rate:.2f} tasks/s, 预计剩余 {remaining:.0f}s) | "
                f"resolved={state.resolved_count} failed={state.failed_count}"
            )
            state.flush()
            last_flush_time = now

    return done_count


def _build_summary(state: StateManager) -> dict:
    resolved = state.get_resolved_tasks()
    adj_dist = {}
    for r in resolved:
        adj = r.get("Adjudication", "")
        if adj:
            adj_dist[adj] = adj_dist.get(adj, 0) + 1

    confidences = [r.get("Confidence", 0) for r in resolved]
    avg_conf = sum(confidences) / len(confidences) if confidences else 0.0

    return {
        "total_task_count": state.total_tasks,
        "resolved_task_count": state.resolved_count,
        "failed_task_count": state.failed_count,
        "pending_task_count": state.pending_count,
        "average_confidence": round(avg_conf, 4),
        "adjudication_distribution": adj_dist,
    }


def run_adjudication(
    run_root: Path,
    input_path: Path,
    config_path: Optional[str] = None,
    live: bool = False,
    dry_run: bool = False,
    limit: int = 0,
    concurrency: int = 0,
    retry_failed: bool = False,
) -> dict:
    """执行裁决任务。

    Args:
        run_root: 运行根目录
        input_path: 原始输入 JSONL 路径（用于最终回写）
        config_path: LLM 配置文件路径
        live: True = 调用真实 API；False = 使用 Mock 客户端
        dry_run: 仅生成 Prompt，不调用 LLM
        limit: 限制处理任务数（0 = 不限）
        concurrency: 并发数（0 = 从配置读取，目前仅支持串行）
        retry_failed: True 时将 failed 任务重新加入待处理队列
    """
    from eval_pipeline.config import LLM_CONCURRENCY

    mock = not live
    client = create_client(config_path=config_path, mock=mock, dry_run=dry_run)

    if not client.is_configured() and not (mock or dry_run):
        logger.error("LLM 客户端未配置，且非 mock/dry-run 模式。请填写配置文件。")
        raise RuntimeError("LLM 客户端未配置")

    if concurrency <= 0:
        if config_path:
            try:
                import yaml
                with open(config_path, 'r', encoding='utf-8') as f:
                    cfg = yaml.safe_load(f)
                concurrency = cfg.get("concurrency", LLM_CONCURRENCY)
            except Exception:
                concurrency = LLM_CONCURRENCY
        else:
            concurrency = LLM_CONCURRENCY
    concurrency = max(1, concurrency)

    state = StateManager(run_root)

    if retry_failed and state.failed_count > 0:
        state.clear_failed()

    pending = state.get_pending_tasks(include_failed=retry_failed)

    if limit > 0:
        pending = pending[:limit]

    logger.info(
        f"开始裁决: 总任务 {state.total_tasks}, "
        f"待处理 {len(pending)}, "
        f"并发数 {concurrency}, "
        f"模式 {'live' if live else 'mock'}"
        f"{', dry-run' if dry_run else ''}, "
        f"已完成 resolved={state.resolved_count} failed={state.failed_count}"
    )

    if not pending:
        logger.info("没有待处理任务")
    else:
        start_time = time.time()
        done_count = _run_sequential(pending, client, dry_run, mock, run_root, state, start_time, input_path)
        state.flush()
        total_time = time.time() - start_time
        logger.info(f"裁决完成: 耗时 {total_time:.1f}s, 共处理 {done_count} 个任务")

    if state.resolved_count > 0 or state.failed_count > 0:
        _write_final_output(run_root, input_path, state)

    summary = _build_summary(state)
    write_json(run_root / "reports" / "adjudication_summary.json", summary)

    return summary

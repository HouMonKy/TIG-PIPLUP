"""eval_pipeline 常量与默认配置。"""
import os

DEFAULT_MODULE = "Messages"
# 数据根目录：环境变量 EVAL_PIPELINE_DATA_ROOT 优先，避免机器相关路径硬编码
DEFAULT_DATA_ROOT = os.environ.get("EVAL_PIPELINE_DATA_ROOT", r"D:\DME-data\单个日志模块")

# 采样间隔（秒）
SAMPLE_INTERVAL = 0.05

# 预热行数（warm-start 模式）
WARMUP_LINES = 5000

# 场景列表
DEFAULT_SCENARIOS = [f"{i:02d}" for i in range(1, 59)]

# LLM 裁决相关常量
ADJ_ORIGINAL = "original"
ADJ_OPTIMIZED = "optimized"
ADJ_EQUIVALENT = "equivalent"
ADJ_NEITHER = "neither"
ADJ_INSUFFICIENT = "insufficient_evidence"

VALID_ADJUDICATIONS = {ADJ_ORIGINAL, ADJ_OPTIMIZED, ADJ_EQUIVALENT, ADJ_NEITHER, ADJ_INSUFFICIENT}

STATUS_PENDING = "pending"
STATUS_RESOLVED = "resolved"
STATUS_FAILED = "failed"

RETRY_JSON_PARSE_MAX = 2

CONFIDENCE_RESOLVED = 0.80

LLM_TEMPERATURE = 0
LLM_MAX_OUTPUT_TOKENS = 4096
LLM_TIMEOUT_SECONDS = 120
LLM_MAX_RETRIES = 3
LLM_CONCURRENCY = 1

# 限流
RATE_LIMIT_FREE_START_HOUR = 23
RATE_LIMIT_FREE_END_HOUR = 8

# 裁决子目录 / 文件名
TASKS_DIR = "tasks"
RESULTS_DIR = "results"
PROMPTS_DIR = "prompts"
ALL_TASKS_FILE = "all_gt_tasks.jsonl"
RESOLVED_FILE = "resolved_gt_tasks.jsonl"
FAILED_FILE = "failed_gt_tasks.jsonl"
RAW_RESPONSES_FILE = "raw_gt_responses.jsonl"

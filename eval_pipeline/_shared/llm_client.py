"""OpenAI 兼容 LLM 客户端实现。复制自 llm_adjudication/openai_client.py。

包含 LLMClient 抽象基类和 RateLimitExceeded 异常。
"""
import os
import time
import logging
import json
import hashlib
from abc import ABC, abstractmethod

import yaml

logger = logging.getLogger(__name__)


class RateLimitExceeded(Exception):
    """日配额耗尽 (HTTP 429)，需等到不限流时段再继续。"""
    pass


class LLMClient(ABC):
    """LLM 客户端抽象接口。"""

    @abstractmethod
    def chat(self, messages: list, **kwargs) -> str:
        ...

    @abstractmethod
    def is_configured(self) -> bool:
        ...

    @abstractmethod
    def get_model_name(self) -> str:
        ...


def _is_rate_limit_error(exc: Exception) -> bool:
    """判断异常是否为 HTTP 429 限流。"""
    try:
        from openai import RateLimitError
        if isinstance(exc, RateLimitError):
            return True
    except ImportError:
        pass
    status = getattr(exc, 'status_code', None)
    if status == 429:
        return True
    if '429' in str(exc) and ('rate' in str(exc).lower() or 'limit' in str(exc).lower()):
        return True
    return False


class OpenAICompatibleClient(LLMClient):
    """基于 openai 库的兼容客户端。"""

    def __init__(self, config_path: str):
        with open(config_path, 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f)

        self._base_url = cfg.get("base_url", "").strip()
        self._model = cfg.get("model", "").strip()
        self._temperature = cfg.get("temperature", 0)
        self._max_tokens = cfg.get("max_output_tokens", 4096)
        self._timeout = cfg.get("timeout_seconds", 120)
        self._max_retries = cfg.get("max_retries", 3)

        direct_key = cfg.get("api_key", "").strip()
        if direct_key:
            self._api_key = direct_key
        else:
            env_name = cfg.get("api_key_env", "LLM_API_KEY")
            self._api_key = os.environ.get(env_name, "").strip()
        self._client = None

    def _ensure_client(self):
        if self._client is not None:
            return
        if not self.is_configured():
            raise RuntimeError("LLM 客户端未正确配置：缺少 base_url、model 或 api_key")
        try:
            import httpx
            from openai import OpenAI

            # 内网网关免代理：配置键 no_proxy_hosts 提供，不覆盖已有 NO_PROXY
            no_proxy_hosts = cfg.get("no_proxy_hosts", "oneapi.rnd.huawei.com")
            existing_no_proxy = os.environ.get('NO_PROXY', '')
            if no_proxy_hosts and no_proxy_hosts not in existing_no_proxy:
                os.environ['NO_PROXY'] = (
                    f"{existing_no_proxy},{no_proxy_hosts}" if existing_no_proxy else no_proxy_hosts)
            httpx_client = httpx.Client(verify=cfg.get("verify_ssl", False), timeout=self._timeout)
            self._client = OpenAI(
                base_url=self._base_url,
                api_key=self._api_key,
                http_client=httpx_client,
            )
        except ImportError:
            raise ImportError("需要安装 openai 和 httpx 库：pip install openai httpx")

    def is_configured(self) -> bool:
        return bool(self._base_url and self._model and self._api_key)

    def get_model_name(self) -> str:
        return self._model or "unconfigured"

    _MAX_TOKENS_EXPAND_LIMIT = 8192

    def chat(self, messages: list, **kwargs) -> str:
        """发送聊天请求，含重试逻辑。遇到 429 立即抛出 RateLimitExceeded。
        finish_reason=length 时自动翻倍 max_tokens 重试。
        """
        self._ensure_client()

        temperature = kwargs.get("temperature", self._temperature)
        max_tokens = kwargs.get("max_tokens", self._max_tokens)

        last_error = None
        for attempt in range(1, self._max_retries + 1):
            try:
                response = self._client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                msg = response.choices[0].message
                finish_reason = response.choices[0].finish_reason
                content = msg.content

                if finish_reason == "length":
                    if max_tokens < self._MAX_TOKENS_EXPAND_LIMIT:
                        old_max = max_tokens
                        max_tokens = min(max_tokens * 2, self._MAX_TOKENS_EXPAND_LIMIT)
                        logger.warning(
                            f"模型输出被截断 (finish_reason=length, max_tokens={old_max})，"
                            f"扩大到 {max_tokens} 重试 (尝试 {attempt}/{self._max_retries})"
                        )
                        continue
                    else:
                        logger.warning(f"模型输出被截断，max_tokens 已达上限 {max_tokens}，返回截断内容")

                if content is None:
                    logger.info(f"模型返回 content=None, finish_reason={finish_reason}, "
                                f"tool_calls={getattr(msg, 'tool_calls', None)}, "
                                f"reasoning_content={getattr(msg, 'reasoning_content', None)}")
                    rc = getattr(msg, 'reasoning_content', None)
                    if rc:
                        content = rc
                    else:
                        raise ValueError(f"模型返回空内容, finish_reason={finish_reason}")

                return content.strip()
            except Exception as e:
                if _is_rate_limit_error(e):
                    logger.warning(f"检测到限流 (429)，抛出 RateLimitExceeded")
                    raise RateLimitExceeded(f"日配额耗尽: {e}")

                last_error = e
                logger.warning(
                    f"LLM 调用失败 (尝试 {attempt}/{self._max_retries}): {e}"
                )
                if attempt < self._max_retries:
                    wait = min(2 ** attempt, 30)
                    time.sleep(wait)

        raise RuntimeError(f"LLM 调用重试耗尽: {last_error}")


class GTMockClient(LLMClient):
    """模拟 LLM 客户端，返回 gt_adjudication 格式的 JSON 响应。复制自 gt_adjudication/mock_client.py。"""

    def __init__(self):
        self.call_count = 0

    def is_configured(self) -> bool:
        return True

    def get_model_name(self) -> str:
        return "gt_mock"

    def chat(self, messages: list, **kwargs) -> str:
        self.call_count += 1
        user_msg = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_msg = m.get("content", "")
                break

        h = int(hashlib.sha256(user_msg.encode('utf-8')).hexdigest()[:8], 16)
        adjudications = ["original", "optimized", "equivalent", "neither", "insufficient_evidence"]
        adj = adjudications[h % len(adjudications)]
        confidence = 0.85 + (h % 15) / 100

        result = {
            "Adjudication": adj,
            "Reason": f"Mock 裁决：基于确定性哈希的模拟结果 (h={h})。",
            "Confidence": round(confidence, 2),
        }
        if adj == "neither":
            # parse_and_validate 要求 neither 必须附 ProposedTemplate，
            # 否则该任务重试耗尽必然 FAILED。mock 从首条样本行合成
            # （含数字的 token 泛化为 <*>），仅保证 neither 链路可走通；
            # 合成质量由 gt 侧 _validate_synthesized 程序校验兜底。
            sample_line = ""
            in_samples = False
            for line in user_msg.splitlines():
                s = line.strip()
                if s == "SampleLogs:":
                    in_samples = True
                    continue
                if s == "OriginalTemplate:":
                    break
                if in_samples and s:
                    sample_line = s
                    break
            tokens = ['<*>' if any(ch.isdigit() for ch in tok) else tok
                      for tok in sample_line.split()]
            result["ProposedTemplate"] = ' '.join(tokens) if tokens else '<*>'
        return json.dumps(result, ensure_ascii=False)


def create_client(config_path=None, mock=False, dry_run=False) -> LLMClient:
    """创建 LLM 客户端。"""
    if mock or dry_run:
        return GTMockClient()
    if not config_path:
        raise ValueError("非 mock/dry-run 模式必须指定 config_path")
    return OpenAICompatibleClient(config_path)

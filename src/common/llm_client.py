#!/usr/bin/python3.7
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2012-2020. All rights reserved.
import os
import asyncio
import json
import logging
import os
from typing import List, Dict, Any, Optional

import httpx
from openai import AsyncOpenAI, OpenAI, RateLimitError, APIError, Timeout
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_random_exponential, retry_if_exception_type


class LLMClient:
    """Unified LLM client for all LogIndex operations."""

    def __init__(self,
                 api_key: Optional[str] = None,
                 base_url: Optional[str] = None,
                 model_name: str = "QwQ-32B",
                 request_timeout: int = 200,
                 llm_configs: Dict = None,
                 use_async: bool = True):
        """
        Initialize LLM client.

        Args:
            model_name: Default model to use
        """
        if api_key is None:
            api_key = os.environ.get("LLM_API_KEY")
            if not api_key:
                raise ValueError("API key must be provided or set in LLM_API_KEY environment variable")

        self.model_name = model_name

        self.use_async = use_async
        if self.use_async:
            http_client = httpx.AsyncClient(limits=httpx.Limits(max_connections=1000, max_keepalive_connections=100))
            self.client = AsyncOpenAI(api_key=api_key, base_url=base_url, http_client=http_client, timeout=request_timeout)
        else:
            http_client = httpx.Client(verify=False)
            self.client = OpenAI(api_key=api_key, base_url=base_url, http_client=http_client, timeout=request_timeout)

        self.llm_configs = llm_configs or {'temperature': 0.5,
                                           'max_tokens': 4096,
                                           'presence_penalty': 0.0,
                                           'frequency_penalty': 0.0,
                                           'seed': None,
                                           'top_p': 0.95}

        self.llm_call_count = 0

    @retry(stop=stop_after_attempt(3),
           wait=wait_random_exponential(min=1, max=60),
           retry=retry_if_exception_type((RateLimitError, APIError, Timeout)),
           before_sleep=lambda retry_state: logging.warning(
               f"Retrying LLM call after {retry_state.attempt_number} attempts due to {retry_state.outcome.exception()}, waiting {retry_state.next_action.sleep:.2f}s...")
           )
    def complete(self, prompt: str, system_prompt: Optional[str] = None, model_name: Optional[str] = None, stream: bool = False) -> str:
        """
        Get completion from LLM.

        Args:
            prompt: User prompt
            system_prompt: System prompt (optional)
            model_name: Model to use (overrides default)
            stream: whether streaming outputs

        Returns:
            Generated text response
        """
        self.llm_call_count += 1

        model = model_name or self.model_name
        if system_prompt is None:
            system_prompt = "You are a helpful assistant analyzing system logs."

        response = self.client.chat.completions.create(
            model=model,
            messages=[
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': prompt},
            ],
            stream=stream,
            **self.llm_configs,
        )
        if not stream:
            return response.choices[0].message.content
        else:
            outputs = []
            for trunk in response:
                content = json.loads(trunk.model_dump_json())['choices'][0]['delta']['content']
                outputs.append(content.strip("\n"))
            return ''.join(outputs)

    @retry(stop=stop_after_attempt(3),
           wait=wait_random_exponential(min=1, max=60),
           retry=retry_if_exception_type((RateLimitError, APIError, Timeout)),
           before_sleep=lambda retry_state: logging.warning(
               f"Retrying LLM call after {retry_state.attempt_number} attempts due to {retry_state.outcome.exception()}, waiting {retry_state.next_action.sleep:.2f}s...")
           )
    def complete_with_constraint(self, prompt: str, system_prompt: Optional[str] = None, model_name: Optional[str] = None, response_format: Optional[BaseModel] = None) -> str | BaseModel:
        model = model_name or self.model_name
        if system_prompt is None:
            system_prompt = "You are a helpful assistant analyzing system logs."

        if response_format is None:
            return self.complete(prompt=prompt, system_prompt=system_prompt, model_name=model)
        response = self.client.beta.chat.completions.parse(
            model=self.model_name,
            messages=[
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': prompt},
            ],
            response_format=response_format,
            **self.llm_configs,
        )
        return response.choices[0].message.parsed

    @retry(stop=stop_after_attempt(3),
           wait=wait_random_exponential(min=1, max=60),
           retry=retry_if_exception_type((RateLimitError, APIError, Timeout)),
           before_sleep=lambda retry_state: logging.warning(
               f"Retrying LLM call after {retry_state.attempt_number} attempts due to {retry_state.outcome.exception()}, waiting {retry_state.next_action.sleep:.2f}s...")
           )
    async def async_complete(self, prompt: str, system_prompt: Optional[str] = None, model_name: Optional[str] = None) -> str:
        """
        Get completion from LLM.

        Args:
            prompt: User prompt
            system_prompt: System prompt (optional)
            model_name: Model to use (overrides default)

        Returns:
            Generated text response
        """
        model = model_name or self.model_name

        if system_prompt is None:
            system_prompt = "You are a helpful assistant analyzing system logs."

        response = await self.client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            **self.llm_configs,
        )
        return response.choices[0].message.content

    async def batch_complete(self, prompts: List[Dict[str, Any]], model_name: Optional[str] = None, batch_size: int = 10) -> List[str]:
        """
        Process multiple prompts in batches.

        Args:
            prompts: List of prompt dictionaries with 'prompt' and optional 'system_prompt'
            model_name: Model to use
            batch_size: Number of prompts to process in parallel

        Returns:
            List of responses
        """
        model = model_name or self.model_name
        results = []

        for i in range(0, len(prompts), batch_size):
            batch = prompts[i:i + batch_size]
            tasks = []

            for prompt_dict in batch:
                prompt = prompt_dict['prompt']
                system_prompt = prompt_dict.get('system_prompt', None)

                task = self.async_complete(prompt, system_prompt, model)
                tasks.append(task)

            batch_results = await asyncio.gather(*tasks)
            results.extend(batch_results)

        return results


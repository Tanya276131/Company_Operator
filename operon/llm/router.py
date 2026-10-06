"""Role-based LLM router.

    router.complete("planner", messages, tools=...)

Handles, in order: record/replay lookup -> per-provider rate limiting -> call ->
exponential backoff + jitter on transient errors -> failover to the next candidate
for that role -> record the answer. Also offers `complete_json` (schema-validated
output with automatic repair retries) and per-role usage stats.
"""
from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from operon.llm.config import ModelsConfig, load_models_config
from operon.llm.recorder import Recorder, request_key
from operon.llm.types import (
    AllProvidersFailed,
    LLMBackend,
    LLMError,
    LLMResponse,
    ReplayMiss,
    TransientLLMError,
)

M = TypeVar("M", bound=BaseModel)


class RateLimiter:
    """Spaces calls so a provider never sees more than `rpm` requests per minute."""

    def __init__(self, rpm: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.interval = 60.0 / max(rpm, 0.1)
        self._clock, self._sleep = clock, sleep
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            start = max(self._next, now)
            self._next = start + self.interval
        if start > now:
            self._sleep(start - now)


class LLMRouter:
    def __init__(self, config: ModelsConfig, backend: LLMBackend | None = None,
                 recorder: Recorder | None = None, *, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 jitter: Callable[[], float] = random.random, timeout_s: float = 60.0):
        self.config = config
        if backend is None:
            from operon.llm.litellm_backend import LiteLLMBackend

            backend = LiteLLMBackend()
        self.backend = backend
        # NB: `is not None`, not `or`: an empty Recorder has len()==0 and would be falsy
        self.recorder = recorder if recorder is not None else Recorder(mode="off")
        self._sleep, self._jitter, self.timeout_s = sleep, jitter, timeout_s
        self._limiters: dict[str, RateLimiter] = {}
        self._clock = clock
        self._lock = threading.Lock()
        self.stats: dict[str, dict[str, int]] = defaultdict(
            lambda: {"calls": 0, "cache_hits": 0, "prompt_tokens": 0, "completion_tokens": 0,
                     "retries": 0, "failovers": 0}
        )

    @classmethod
    def from_env(cls, config_path: str | Path = "config/models.yaml",
                 backend: LLMBackend | None = None) -> "LLMRouter":
        from dotenv import load_dotenv

        load_dotenv()
        cfg = load_models_config(config_path)
        mode = os.environ.get("OPERON_LLM_MODE", "auto")
        rec = Recorder(os.environ.get("OPERON_LLM_RECORDINGS", "recordings/llm.jsonl"), mode)
        return cls(cfg, backend=backend, recorder=rec)

    def _limiter(self, provider: str) -> RateLimiter:
        with self._lock:
            if provider not in self._limiters:
                self._limiters[provider] = RateLimiter(
                    self.config.limits(provider).rpm, self._clock, self._sleep)
            return self._limiters[provider]

    def complete(self, role: str, messages: list[dict], *, tools: list[dict] | None = None,
                 response_format: dict | None = None, temperature: float = 0.0,
                 max_tokens: int = 2048) -> LLMResponse:
        if role not in self.config.roles:
            raise LLMError(f"unknown role '{role}' (configured: {sorted(self.config.roles)})")
        key = request_key(role, messages, tools, response_format, temperature)
        st = self.stats[role]
        hit = self.recorder.get(key)
        if hit is not None:
            st["cache_hits"] += 1
            return hit
        if self.recorder.mode == "replay":
            raise ReplayMiss(f"no recording for this '{role}' request (mode=replay)")

        errors: list[str] = []
        retry = self.config.retry
        for idx, cand in enumerate(self.config.roles[role]):
            if idx > 0:
                st["failovers"] += 1
            for attempt in range(retry.max_retries + 1):
                self._limiter(cand.provider).wait()
                try:
                    resp = self.backend.chat(
                        provider=cand.provider, model=cand.model, messages=messages, tools=tools,
                        response_format=response_format, temperature=temperature,
                        max_tokens=max_tokens, timeout=self.timeout_s)
                except TransientLLMError as e:
                    if attempt < retry.max_retries:
                        st["retries"] += 1
                        delay = min(retry.max_delay_s, retry.base_delay_s * 2**attempt)
                        self._sleep(delay * (0.5 + self._jitter()))
                        continue
                    errors.append(f"{cand.provider}/{cand.model}: {e} (retries exhausted)")
                    break
                except LLMError as e:
                    errors.append(f"{cand.provider}/{cand.model}: {e}")
                    break
                st["calls"] += 1
                st["prompt_tokens"] += resp.usage.get("prompt_tokens", 0)
                st["completion_tokens"] += resp.usage.get("completion_tokens", 0)
                self.recorder.put(key, role, resp)
                return resp
        raise AllProvidersFailed(errors)

    # ------------------------------------------------------------- structured output
    def complete_json(self, role: str, messages: list[dict], model_cls: type[M], *,
                      max_repairs: int = 2, **kw: Any) -> M:
        """Ask for JSON matching `model_cls`; on invalid output, show the model its error and retry."""
        schema = json.dumps(model_cls.model_json_schema(), sort_keys=True)
        msgs = list(messages) + [{
            "role": "system",
            "content": "Respond with ONE JSON object that validates against this JSON Schema. "
                       f"No prose, no markdown fences.\n{schema}"}]
        last_err = ""
        for _ in range(max_repairs + 1):
            resp = self.complete(role, msgs, **kw)
            text = resp.content or ""
            try:
                return model_cls.model_validate_json(_extract_json(text))
            except (ValidationError, ValueError) as e:
                last_err = str(e)[:500]
                msgs = msgs + [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": f"That was invalid: {last_err}\nReturn corrected JSON only."},
                ]
        raise LLMError(f"model did not produce valid {model_cls.__name__} JSON: {last_err}")


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _extract_json(text: str) -> str:
    t = _FENCE.sub("", text.strip())
    a, b = t.find("{"), t.rfind("}")
    if a == -1 or b == -1 or b < a:
        raise ValueError("no JSON object found in model output")
    return t[a : b + 1]

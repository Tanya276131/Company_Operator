"""LiteLLM backend: one interface to Gemini, Groq, Anthropic, Ollama, ...

Model strings follow LiteLLM's '<provider>/<model>' convention. API keys are read
from the environment (GEMINI_API_KEY, GROQ_API_KEY, ANTHROPIC_API_KEY).

NOTE: not exercised in the build environment (no provider keys there). The first thing
to do on your machine is `python scripts/llm_smoke.py` with a real key.
"""
from __future__ import annotations

import json
from typing import Any

from operon.llm.types import LLMError, LLMResponse, ToolCallReq, TransientLLMError

_TRANSIENT_NAMES = {
    "RateLimitError", "ServiceUnavailableError", "InternalServerError", "Timeout",
    "APIConnectionError", "APITimeoutError", "BadGatewayError", "OverloadedError",
}


def _is_transient(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    return type(exc).__name__ in _TRANSIENT_NAMES or status in (408, 429, 500, 502, 503, 504)


class LiteLLMBackend:
    def chat(self, *, provider: str, model: str, messages: list[dict], tools: list[dict] | None,
             response_format: dict | None, temperature: float, max_tokens: int,
             timeout: float) -> LLMResponse:
        import litellm  # lazy: tests and replay mode never need it

        kwargs: dict[str, Any] = dict(
            model=f"{provider}/{model}", messages=messages, temperature=temperature,
            max_tokens=max_tokens, timeout=timeout,
        )
        if tools:
            kwargs["tools"] = tools
        if response_format:
            kwargs["response_format"] = response_format
        try:
            resp = litellm.completion(**kwargs)
        except Exception as e:
            if _is_transient(e):
                raise TransientLLMError(f"{type(e).__name__}: {e}") from e
            raise LLMError(f"{type(e).__name__}: {e}") from e

        msg = resp.choices[0].message
        calls: list[ToolCallReq] = []
        for tc in getattr(msg, "tool_calls", None) or []:
            raw = tc.function.arguments
            try:
                args = json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
            except json.JSONDecodeError:
                args = {"_raw_arguments": raw}  # surfaced as a VALIDATION error downstream
            calls.append(ToolCallReq(id=getattr(tc, "id", "") or "", name=tc.function.name, arguments=args))
        usage = getattr(resp, "usage", None)
        return LLMResponse(
            content=msg.content, tool_calls=calls, model=model, provider=provider,
            usage={"prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                   "completion_tokens": getattr(usage, "completion_tokens", 0) or 0},
        )

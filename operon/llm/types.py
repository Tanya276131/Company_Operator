"""Provider-neutral request/response types and errors for the LLM layer."""
from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field


class ToolCallReq(BaseModel):
    id: str = ""
    name: str  # LLM-facing name, e.g. "portal__create_claim"
    arguments: dict[str, Any] = Field(default_factory=dict)


class LLMResponse(BaseModel):
    content: str | None = None
    tool_calls: list[ToolCallReq] = Field(default_factory=list)
    model: str = ""
    provider: str = ""
    usage: dict[str, int] = Field(default_factory=dict)
    cached: bool = False


class LLMError(Exception):
    """Non-retryable provider failure (bad key, unsupported request...). Triggers failover."""


class TransientLLMError(LLMError):
    """Retryable: rate limit, 5xx, timeout, connection error."""


class ReplayMiss(LLMError):
    """mode=replay and the request was never recorded."""


class AllProvidersFailed(LLMError):
    def __init__(self, errors: list[str]):
        super().__init__("all providers failed: " + " | ".join(errors))
        self.errors = errors


class LLMBackend(Protocol):
    def chat(
        self, *, provider: str, model: str, messages: list[dict], tools: list[dict] | None,
        response_format: dict | None, temperature: float, max_tokens: int, timeout: float,
    ) -> LLMResponse: ...

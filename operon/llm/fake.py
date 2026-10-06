"""Scripted backend for tests, offline demos and CI: no network, fully deterministic."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from operon.llm.types import LLMResponse


class FakeBackend:
    """script items may be an LLMResponse, an Exception (raised), a str (content),
    or a callable(call_kwargs) -> any of those. Items are consumed in order."""

    def __init__(self, script: list[Any] | None = None, default: Any = None):
        self.script = list(script or [])
        self.default = default
        self.calls: list[dict[str, Any]] = []

    def chat(self, **kw: Any) -> LLMResponse:
        self.calls.append(kw)
        item = self.script.pop(0) if self.script else self.default
        if item is None:
            raise AssertionError("FakeBackend script exhausted")
        if callable(item):
            item = item(kw)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, str):
            item = LLMResponse(content=item)
        return item.model_copy(update={"provider": kw["provider"], "model": kw["model"]})

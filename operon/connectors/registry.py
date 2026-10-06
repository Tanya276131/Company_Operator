"""Tool registry: the single door between the runtime and every connector.

* resolves "connector.tool" names and enforces an allow-list (fnmatch patterns)
* renders tools in the OpenAI function-calling format that LiteLLM/Gemini/Groq accept
* applies idempotency for WRITE/IRREVERSIBLE tools so a resumed run never repeats a write
"""
from __future__ import annotations

from collections.abc import Iterable
from fnmatch import fnmatch
from typing import Any, Protocol

from operon.connectors.base import Connector, ToolResult, ToolSpec
from operon.schemas import ErrorKind, RiskClass


class IdempotencyCache(Protocol):
    def get(self, key: str) -> dict[str, Any] | None: ...
    def put(self, key: str, run_id: str, tool: str, result: dict[str, Any]) -> None: ...


class ToolRegistry:
    def __init__(self, cache: IdempotencyCache | None = None):
        self._connectors: dict[str, Connector] = {}
        self.cache = cache

    # -- registration ---------------------------------------------------
    def register(self, connector: Connector) -> Connector:
        if connector.name in self._connectors:
            raise ValueError(f"connector '{connector.name}' already registered")
        self._connectors[connector.name] = connector
        return connector

    @property
    def connectors(self) -> dict[str, Connector]:
        return dict(self._connectors)

    # -- lookup ------------------------------------------------------------
    def resolve(self, qualified: str) -> tuple[Connector, ToolSpec]:
        cname, _, tname = qualified.partition(".")
        conn = self._connectors.get(cname)
        if conn is None or tname not in conn.tools:
            raise KeyError(qualified)
        return conn, conn.tools[tname]

    def specs(self, allowed: Iterable[str] | None = None) -> list[tuple[str, ToolSpec]]:
        patterns = list(allowed) if allowed is not None else None
        out = []
        for cname, conn in self._connectors.items():
            for tname, spec in conn.tools.items():
                q = f"{cname}.{tname}"
                if patterns is None or any(fnmatch(q, p) for p in patterns):
                    out.append((q, spec))
        return out

    # -- LLM function-calling view ----------------------------------------
    @staticmethod
    def to_llm_name(qualified: str) -> str:
        return qualified.replace(".", "__")

    @staticmethod
    def from_llm_name(llm_name: str) -> str:
        return llm_name.replace("__", ".", 1)

    def llm_tools(self, allowed: Iterable[str] | None = None) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": self.to_llm_name(q),
                    "description": f"[{spec.risk.value}] {spec.description}",
                    "parameters": spec.input_schema,
                },
            }
            for q, spec in self.specs(allowed)
        ]

    # -- execution ---------------------------------------------------------
    def call(
        self,
        qualified: str,
        args: dict[str, Any] | None = None,
        *,
        dry_run: bool = False,
        idempotency_key: str | None = None,
        run_id: str = "adhoc",
    ) -> ToolResult:
        try:
            conn, spec = self.resolve(qualified)
        except KeyError:
            return ToolResult(
                ok=False, error=f"unknown tool '{qualified}'", error_kind=ErrorKind.NOT_FOUND
            )
        mutating = spec.risk != RiskClass.READ and not dry_run
        use_cache = bool(mutating and idempotency_key and self.cache is not None)
        if use_cache:
            hit = self.cache.get(idempotency_key)  # type: ignore[union-attr]
            if hit is not None:
                res = ToolResult.model_validate(hit)
                res.replayed = True
                return res
        res = conn.invoke(qualified.split(".", 1)[1], args, dry_run=dry_run)
        if use_cache and res.ok:
            self.cache.put(idempotency_key, run_id, qualified, res.model_dump(mode="json"))  # type: ignore[union-attr]
        return res

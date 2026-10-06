"""Connector SDK.

A *connector* wraps one system (files, an internal web app, an HTTP API, a
browser...) and exposes typed *tools*. Adding a new company system means
writing a Connector subclass; the runtime loop never changes.

Each tool declares what the guard and executor need to know:
  * risk            READ / WRITE / IRREVERSIBLE  -> approval policy
  * idempotent      safe to replay with the same idempotency key
  * untrusted       output is derived from external text -> tainted (prompt-injection defence)
  * compensate_with name of a tool that undoes it (saga-style rollback)
  * dry_run         can preview its effect without performing it

Tools take a validated Pydantic args model and return plain data (or ToolOutput
when they also produce evidence files). Raise ToolError subclasses to signal a
classified failure; the repair ladder reacts to `ErrorKind`.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from operon.schemas import ErrorKind, RiskClass


# --------------------------------------------------------------------- errors
class ToolError(Exception):
    kind: ErrorKind = ErrorKind.PERMANENT

    def __init__(self, message: str, kind: ErrorKind | None = None):
        super().__init__(message)
        if kind is not None:
            self.kind = kind


class TransientError(ToolError):
    kind = ErrorKind.TRANSIENT


class NotFoundError(ToolError):
    kind = ErrorKind.NOT_FOUND


class ValidationFailed(ToolError):
    kind = ErrorKind.VALIDATION


class ConflictError(ToolError):
    kind = ErrorKind.CONFLICT


class PermissionDenied(ToolError):
    kind = ErrorKind.PERMISSION


# ---------------------------------------------------------------------- models
class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    risk: RiskClass = RiskClass.READ
    idempotent: bool = True
    untrusted: bool = False
    compensate_with: str | None = None
    supports_dry_run: bool = False


class ToolOutput(BaseModel):
    """Return this from a tool when it also produces evidence artifacts (screenshots...)."""

    output: Any = None
    artifacts: list[str] = Field(default_factory=list)


class ToolResult(BaseModel):
    ok: bool
    output: Any = None
    error: str | None = None
    error_kind: ErrorKind | None = None
    duration_ms: float = 0.0
    artifacts: list[str] = Field(default_factory=list)
    tainted: bool = False
    replayed: bool = False  # served from the idempotency cache, not re-executed
    dry_run: bool = False


# ------------------------------------------------------------------- decorator
def tool(
    *,
    args: type[BaseModel] | None = None,
    risk: RiskClass = RiskClass.READ,
    idempotent: bool | None = None,
    untrusted: bool = False,
    compensate_with: str | None = None,
    dry_run: bool = False,
    name: str | None = None,
) -> Callable:
    """Mark a Connector method as a tool."""

    def deco(fn: Callable) -> Callable:
        fn._tool_meta = {  # type: ignore[attr-defined]
            "name": name or fn.__name__,
            "args": args,
            "risk": risk,
            "idempotent": (risk == RiskClass.READ) if idempotent is None else idempotent,
            "untrusted": untrusted,
            "compensate_with": compensate_with,
            "dry_run": dry_run,
            "description": (fn.__doc__ or "").strip().split("\n\n")[0].replace("\n", " "),
        }
        return fn

    return deco


# ------------------------------------------------------------------ connector
class Connector:
    name: str = "connector"
    description: str = ""

    def __init__(self) -> None:
        self._tools: dict[str, tuple[ToolSpec, Callable, type[BaseModel] | None]] = {}
        seen: set[str] = set()
        for klass in type(self).__mro__:  # subclass first so overrides win
            for attr_name, attr in vars(klass).items():
                meta = getattr(attr, "_tool_meta", None)
                if not meta or attr_name in seen:
                    continue
                seen.add(attr_name)
                model = meta["args"]
                schema = (
                    model.model_json_schema()
                    if model is not None
                    else {"type": "object", "properties": {}}
                )
                spec = ToolSpec(
                    name=meta["name"],
                    description=meta["description"],
                    input_schema=schema,
                    risk=meta["risk"],
                    idempotent=meta["idempotent"],
                    untrusted=meta["untrusted"],
                    compensate_with=meta["compensate_with"],
                    supports_dry_run=meta["dry_run"],
                )
                self._tools[spec.name] = (spec, getattr(self, attr_name), model)

    # -- introspection ---------------------------------------------------
    @property
    def tools(self) -> dict[str, ToolSpec]:
        return {n: t[0] for n, t in self._tools.items()}

    def manifest(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "tools": [s.model_dump(mode="json") for s in self.tools.values()],
        }

    # -- hooks the executor/verifier can use ------------------------------
    def compensation_args(self, tool_name: str, args: dict[str, Any], output: Any) -> dict[str, Any]:
        """Arguments for `spec.compensate_with` that undo a completed call of `tool_name`."""
        raise NotImplementedError(f"{self.name}.{tool_name} defines no compensation")

    # -- execution ---------------------------------------------------------
    def invoke(self, tool_name: str, args: dict[str, Any] | None = None, *, dry_run: bool = False) -> ToolResult:
        entry = self._tools.get(tool_name)
        if entry is None:
            return ToolResult(
                ok=False,
                error=f"unknown tool '{self.name}.{tool_name}'",
                error_kind=ErrorKind.NOT_FOUND,
            )
        spec, fn, model = entry
        try:
            parsed = model.model_validate(args or {}) if model is not None else None
        except ValidationError as e:
            return ToolResult(
                ok=False,
                error=f"invalid arguments: {e.errors(include_url=False, include_context=False)}",
                error_kind=ErrorKind.VALIDATION,
            )
        if dry_run and not spec.supports_dry_run and spec.risk != RiskClass.READ:
            return ToolResult(
                ok=False,
                error=f"'{self.name}.{tool_name}' does not support dry_run",
                error_kind=ErrorKind.VALIDATION,
            )
        t0 = time.perf_counter()
        try:
            call_args: list[Any] = [] if parsed is None else [parsed]
            kwargs = {"dry_run": dry_run} if spec.supports_dry_run else {}
            out = fn(*call_args, **kwargs)
        except ToolError as e:
            return ToolResult(
                ok=False,
                error=str(e),
                error_kind=e.kind,
                duration_ms=(time.perf_counter() - t0) * 1000,
            )
        except Exception as e:  # unclassified: surface it, never crash the loop
            return ToolResult(
                ok=False,
                error=f"{type(e).__name__}: {e}",
                error_kind=ErrorKind.UNKNOWN,
                duration_ms=(time.perf_counter() - t0) * 1000,
            )
        artifacts: list[str] = []
        if isinstance(out, ToolOutput):
            artifacts, out = out.artifacts, out.output
        return ToolResult(
            ok=True,
            output=out,
            artifacts=artifacts,
            duration_ms=(time.perf_counter() - t0) * 1000,
            tainted=spec.untrusted,
            dry_run=dry_run,
        )

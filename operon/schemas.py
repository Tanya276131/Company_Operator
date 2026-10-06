"""Core data contracts for the Operon runtime.

Everything the runtime persists or passes between components is one of these
Pydantic models, so the contract compiler, planner, executor, guard, verifier
and trace UI all speak the same language.

Event payload conventions are documented in DESIGN.md ("Event log").
"""
from __future__ import annotations

import datetime as dt
import uuid
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# --------------------------------------------------------------------------- enums
class RiskClass(str, Enum):
    """How dangerous a tool call is. The guard keys approvals off this."""

    READ = "read"
    WRITE = "write"
    IRREVERSIBLE = "irreversible"

    @property
    def rank(self) -> int:
        return {"read": 0, "write": 1, "irreversible": 2}[self.value]


class ErrorKind(str, Enum):
    """Why a tool call failed. The repair ladder keys off this."""

    TRANSIENT = "transient"  # retrying may help (timeout, 5xx)
    NOT_FOUND = "not_found"
    VALIDATION = "validation"  # bad arguments -> fix the call, don't just retry
    PERMISSION = "permission"  # blocked by the system or the guard
    CONFLICT = "conflict"  # state conflict (e.g. already submitted)
    PERMANENT = "permanent"
    UNKNOWN = "unknown"


class RunStatus(str, Enum):
    RECEIVED = "received"
    CONTRACTED = "contracted"
    PLANNED = "planned"
    EXECUTING = "executing"
    AWAITING_APPROVAL = "awaiting_approval"
    REPAIRING = "repairing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    ESCALATED = "escalated"


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class EventType(str, Enum):
    RUN_CREATED = "run_created"
    CONTRACT_COMPILED = "contract_compiled"
    PLAN_CREATED = "plan_created"
    PLAN_REVISED = "plan_revised"
    STEP_STARTED = "step_started"
    TOOL_CALLED = "tool_called"
    OBSERVATION = "observation"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    REPAIR_ATTEMPTED = "repair_attempted"
    VERIFICATION_RESULT = "verification_result"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    RUN_ESCALATED = "run_escalated"
    LESSON_PROPOSED = "lesson_proposed"
    NOTE = "note"


# ----------------------------------------------------------------------- contract
class AcceptanceCriterion(BaseModel):
    """A *checkable* statement of what 'done' means.

    `check` is a deterministic spec the verifier executes against the system of
    record, e.g. {"tool": "portal.get_claim", "args": {...}, "assert": [...]}.
    The exact check grammar is provisional: the verifier defines it.
    """

    id: str
    description: str
    check: dict[str, Any] = Field(default_factory=dict)


class TaskContract(BaseModel):
    """Output of the contract compiler: a vague request made explicit."""

    run_id: str
    request: str
    workflow: str | None = None
    goal: str
    entities: dict[str, Any] = Field(default_factory=dict)
    missing_inputs: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    permitted_tools: list[str] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    risk_tier: RiskClass = RiskClass.WRITE
    notes: list[str] = Field(default_factory=list)
    created_at: dt.datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------- plan
class PlanStep(BaseModel):
    id: str
    description: str
    tool: str  # qualified name, e.g. "portal.create_claim"
    args: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    precondition: str | None = None
    expected_postcondition: str | None = None
    risk: RiskClass = RiskClass.READ
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0


class Plan(BaseModel):
    version: int = 1
    rationale: str = ""
    steps: list[PlanStep] = Field(default_factory=list)

    def step(self, step_id: str) -> PlanStep:
        for s in self.steps:
            if s.id == step_id:
                return s
        raise KeyError(step_id)

    def next_runnable(self) -> PlanStep | None:
        """First PENDING step whose dependencies are all DONE (or SKIPPED)."""
        done = {s.id for s in self.steps if s.status in (StepStatus.DONE, StepStatus.SKIPPED)}
        for s in self.steps:
            if s.status == StepStatus.PENDING and all(d in done for d in s.depends_on):
                return s
        return None

    def interrupted_steps(self) -> list[PlanStep]:
        """Steps left RUNNING: the process died mid-step. Resume must re-run them
        using idempotency keys, never blindly."""
        return [s for s in self.steps if s.status == StepStatus.RUNNING]

    @property
    def is_complete(self) -> bool:
        return all(s.status in (StepStatus.DONE, StepStatus.SKIPPED) for s in self.steps)


# ------------------------------------------------------------------- observations
class Observation(BaseModel):
    step_id: str
    tool: str
    ok: bool
    output: Any = None
    error: str | None = None
    error_kind: ErrorKind | None = None
    duration_ms: float = 0.0
    artifacts: list[str] = Field(default_factory=list)
    tainted: bool = False  # output came from untrusted text (documents, web pages)
    ts: dt.datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------- approvals
class ApprovalRequest(BaseModel):
    id: str = Field(default_factory=lambda: new_id("apr"))
    run_id: str
    step_id: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    risk: RiskClass = RiskClass.IRREVERSIBLE
    reason: str = ""
    evidence: list[str] = Field(default_factory=list)
    requested_at: dt.datetime = Field(default_factory=utcnow)


class ApprovalDecision(BaseModel):
    approval_id: str
    decision: Literal["approved", "rejected", "edited"]
    edited_args: dict[str, Any] | None = None
    reviewer: str = "human"
    comment: str = ""
    decided_at: dt.datetime = Field(default_factory=utcnow)


# ------------------------------------------------------------- verification output
class EvidenceRef(BaseModel):
    id: str = Field(default_factory=lambda: new_id("ev"))
    kind: Literal["screenshot", "record", "file", "api_response", "other"] = "other"
    description: str = ""
    path: str | None = None
    sha256: str | None = None
    data: dict[str, Any] | None = None


class CriterionResult(BaseModel):
    criterion_id: str
    passed: bool
    detail: str = ""
    evidence: list[EvidenceRef] = Field(default_factory=list)


class CompletionCertificate(BaseModel):
    """Only the verifier may issue this. The executor can never mark a run complete."""

    run_id: str
    passed: bool
    results: list[CriterionResult] = Field(default_factory=list)
    summary: str = ""
    issued_at: dt.datetime = Field(default_factory=utcnow)
    issued_by: Literal["verifier"] = "verifier"


# ------------------------------------------------------------------------ events
class Event(BaseModel):
    seq: int | None = None
    run_id: str
    type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)
    ts: dt.datetime = Field(default_factory=utcnow)
    prev_hash: str = ""
    hash: str = ""


class RunState(BaseModel):
    """A run's current state, rebuilt purely by folding its events (see state/store.py)."""

    run_id: str
    status: RunStatus = RunStatus.RECEIVED
    request: str = ""
    workflow: str | None = None
    contract: TaskContract | None = None
    plan: Plan | None = None
    pending_approval: ApprovalRequest | None = None
    last_observation: Observation | None = None
    certificate: CompletionCertificate | None = None
    reason: str | None = None  # failure / escalation reason or completion summary
    events_seen: int = 0

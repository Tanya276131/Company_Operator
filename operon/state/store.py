"""Event-sourced, hash-chained run store (SQLite).

Why event sourcing: a run's state is *derived* by folding its events, so a
crashed worker can resume from the log, the trace UI can replay it, and the
audit trail is the source of truth rather than a side effect.

Why a hash chain: every event stores sha256(prev_hash | run_id | type | ts |
payload). `verify_chain()` detects any edited or deleted row, which makes the
audit log tamper-evident.

Also holds the idempotency cache that stops a resumed run from repeating a
write that already happened.
"""
from __future__ import annotations

import datetime as dt
import enum
import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from operon.schemas import (
    ApprovalRequest,
    CompletionCertificate,
    Event,
    EventType,
    Observation,
    Plan,
    RunState,
    RunStatus,
    StepStatus,
    TaskContract,
    utcnow,
)

GENESIS_HASH = "0" * 64


def _default(o: Any) -> Any:
    if isinstance(o, BaseModel):
        return o.model_dump(mode="json")
    if isinstance(o, (dt.datetime, dt.date)):
        return o.isoformat()
    if isinstance(o, enum.Enum):
        return o.value
    if isinstance(o, Path):
        return o.as_posix()
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    raise TypeError(f"not JSON serialisable: {type(o).__name__}")


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_default)


def compute_hash(prev_hash: str, run_id: str, etype: str, ts: str, payload_text: str) -> str:
    raw = f"{prev_hash}|{run_id}|{etype}|{ts}|{payload_text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class EventStore:
    """Append-only event log + idempotency cache. Thread-safe; WAL for file DBs."""

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    prev_hash TEXT NOT NULL,
                    hash TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, seq);
                CREATE TABLE IF NOT EXISTS tool_results (
                    idempotency_key TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    result TEXT NOT NULL,
                    ts TEXT NOT NULL
                );
                """
            )

    # ------------------------------------------------------------------ writing
    def append(self, run_id: str, etype: EventType, payload: dict[str, Any] | None = None) -> Event:
        payload_text = canonical_json(payload or {})
        ts = utcnow().isoformat()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT hash FROM events ORDER BY seq DESC LIMIT 1"
                ).fetchone()
                prev = row["hash"] if row else GENESIS_HASH
                h = compute_hash(prev, run_id, etype.value, ts, payload_text)
                cur = self._conn.execute(
                    "INSERT INTO events(run_id,type,payload,ts,prev_hash,hash) VALUES (?,?,?,?,?,?)",
                    (run_id, etype.value, payload_text, ts, prev, h),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return Event(
            seq=cur.lastrowid,
            run_id=run_id,
            type=etype,
            payload=json.loads(payload_text),
            ts=dt.datetime.fromisoformat(ts),
            prev_hash=prev,
            hash=h,
        )

    # ------------------------------------------------------------------ reading
    @staticmethod
    def _row_to_event(r: sqlite3.Row) -> Event:
        return Event(
            seq=r["seq"],
            run_id=r["run_id"],
            type=EventType(r["type"]),
            payload=json.loads(r["payload"]),
            ts=dt.datetime.fromisoformat(r["ts"]),
            prev_hash=r["prev_hash"],
            hash=r["hash"],
        )

    def events(self, run_id: str | None = None, after_seq: int = 0) -> list[Event]:
        with self._lock:
            if run_id is None:
                rows = self._conn.execute(
                    "SELECT * FROM events WHERE seq > ? ORDER BY seq", (after_seq,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM events WHERE run_id = ? AND seq > ? ORDER BY seq",
                    (run_id, after_seq),
                ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def run_ids(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id FROM events GROUP BY run_id ORDER BY MIN(seq)"
            ).fetchall()
        return [r["run_id"] for r in rows]

    # -------------------------------------------------------------- audit chain
    def verify_chain(self) -> tuple[bool, int | None]:
        """Recompute every hash. Returns (ok, first_bad_seq)."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
        prev = GENESIS_HASH
        for r in rows:
            expected = compute_hash(prev, r["run_id"], r["type"], r["ts"], r["payload"])
            if r["prev_hash"] != prev or r["hash"] != expected:
                return False, r["seq"]
            prev = r["hash"]
        return True, None

    # ------------------------------------------------- idempotency (tool results)
    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT result FROM tool_results WHERE idempotency_key = ?", (key,)
            ).fetchone()
        return json.loads(row["result"]) if row else None

    def put(self, key: str, run_id: str, tool: str, result: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO tool_results VALUES (?,?,?,?,?)",
                (key, run_id, tool, canonical_json(result), utcnow().isoformat()),
            )

    # --------------------------------------------------------------- projection
    def state(self, run_id: str) -> RunState:
        return project(run_id, self.events(run_id))

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ------------------------------------------------------------------------ fold
def project(run_id: str, events: list[Event]) -> RunState:
    """Fold a run's events into its current RunState (pure function: no I/O)."""
    st = RunState(run_id=run_id)
    for ev in events:
        st.events_seen += 1
        p = ev.payload
        t = ev.type
        if t == EventType.RUN_CREATED:
            st.status = RunStatus.RECEIVED
            st.request = p.get("request", "")
            st.workflow = p.get("workflow")
        elif t == EventType.CONTRACT_COMPILED:
            st.contract = TaskContract.model_validate(p["contract"])
            st.status = RunStatus.CONTRACTED
        elif t in (EventType.PLAN_CREATED, EventType.PLAN_REVISED):
            st.plan = Plan.model_validate(p["plan"])
            st.status = RunStatus.PLANNED
        elif t == EventType.STEP_STARTED and st.plan:
            step = st.plan.step(p["step_id"])
            step.status = StepStatus.RUNNING
            step.attempts += 1
            st.status = RunStatus.EXECUTING
        elif t == EventType.OBSERVATION and st.plan:
            obs = Observation.model_validate(p["observation"])
            st.last_observation = obs
            step = st.plan.step(obs.step_id)
            step.status = StepStatus.DONE if obs.ok else StepStatus.FAILED
        elif t == EventType.APPROVAL_REQUESTED:
            st.pending_approval = ApprovalRequest.model_validate(p["approval"])
            st.status = RunStatus.AWAITING_APPROVAL
        elif t == EventType.APPROVAL_RESOLVED:
            st.pending_approval = None
            st.status = RunStatus.EXECUTING
        elif t == EventType.REPAIR_ATTEMPTED:
            st.status = RunStatus.REPAIRING
        elif t == EventType.VERIFICATION_RESULT:
            st.certificate = CompletionCertificate.model_validate(p["certificate"])
            st.status = RunStatus.VERIFYING
        elif t == EventType.RUN_COMPLETED:
            st.status = RunStatus.COMPLETED
            st.reason = p.get("summary")
        elif t == EventType.RUN_FAILED:
            st.status = RunStatus.FAILED
            st.reason = p.get("reason")
        elif t == EventType.RUN_ESCALATED:
            st.status = RunStatus.ESCALATED
            st.reason = p.get("reason")
    return st

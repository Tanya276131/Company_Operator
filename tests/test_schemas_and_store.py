import sqlite3

from operon.schemas import (
    ApprovalRequest, EventType, Observation, Plan, PlanStep, RunStatus, StepStatus, TaskContract,
)
from operon.state.store import EventStore


def make_plan():
    return Plan(steps=[
        PlanStep(id="s1", description="read", tool="files.read_text"),
        PlanStep(id="s2", description="create", tool="portal.create_claim", depends_on=["s1"]),
    ])


def test_plan_dependency_order():
    p = make_plan()
    assert p.next_runnable().id == "s1"
    p.step("s1").status = StepStatus.DONE
    assert p.next_runnable().id == "s2"
    p.step("s2").status = StepStatus.DONE
    assert p.next_runnable() is None and p.is_complete


def test_event_chain_and_tamper_detection(tmp_path):
    s = EventStore(tmp_path / "e.db")
    s.append("r1", EventType.RUN_CREATED, {"request": "x"})
    s.append("r2", EventType.RUN_CREATED, {"request": "y"})
    s.append("r1", EventType.NOTE, {"msg": "hello"})
    assert s.verify_chain() == (True, None)
    raw = sqlite3.connect(tmp_path / "e.db")
    raw.execute("UPDATE events SET payload='{\"msg\":\"evil\"}' WHERE seq=3")
    raw.commit()
    ok, bad = s.verify_chain()
    assert not ok and bad == 3


def test_projection_resume_and_approval_flow():
    s = EventStore()
    rid = "run_1"
    s.append(rid, EventType.RUN_CREATED, {"request": "reimburse", "workflow": "expense_reimbursement"})
    contract = TaskContract(run_id=rid, request="reimburse", goal="g")
    s.append(rid, EventType.CONTRACT_COMPILED, {"contract": contract})
    s.append(rid, EventType.PLAN_CREATED, {"plan": make_plan()})
    s.append(rid, EventType.STEP_STARTED, {"step_id": "s1"})
    # crash here: s1 is left RUNNING
    st = s.state(rid)
    assert st.status == RunStatus.EXECUTING
    assert [x.id for x in st.plan.interrupted_steps()] == ["s1"]

    obs = Observation(step_id="s1", tool="files.read_text", ok=True)
    s.append(rid, EventType.OBSERVATION, {"observation": obs})
    apr = ApprovalRequest(run_id=rid, step_id="s2", tool="portal.submit_claim")
    s.append(rid, EventType.APPROVAL_REQUESTED, {"approval": apr})
    st = s.state(rid)
    assert st.status == RunStatus.AWAITING_APPROVAL and st.pending_approval.id == apr.id
    assert st.plan.step("s1").status == StepStatus.DONE
    s.append(rid, EventType.APPROVAL_RESOLVED, {"approval_id": apr.id, "decision": "approved"})
    assert s.state(rid).pending_approval is None
    s.append(rid, EventType.RUN_FAILED, {"reason": "boom"})
    st = s.state(rid)
    assert st.status == RunStatus.FAILED and st.reason == "boom"


def test_idempotency_cache_roundtrip():
    s = EventStore()
    assert s.get("k") is None
    s.put("k", "r", "portal.create_claim", {"ok": True, "output": {"id": "CLM-1"}})
    assert s.get("k")["output"]["id"] == "CLM-1"

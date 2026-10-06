"""Foundation demo (no LLM needed): proves the pieces compose.

A *hand-written* plan runs through the real components: sandboxed file reads, an
idempotent write to the mock portal, an irreversible submit, event-sourced state, a
hash-chained audit log and an independent check against the system of record. Your job
is to replace the hard-coded plan with Understand -> Plan -> Execute -> Observe -> Verify.

    python scripts/foundation_demo.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from mock_company.portal.app import create_app
from operon.connectors.files import FilesConnector
from operon.connectors.portal import PortalConnector
from operon.connectors.registry import ToolRegistry
from operon.schemas import EventType, Observation, Plan, PlanStep, RiskClass, TaskContract, new_id
from operon.state.store import EventStore
from operon.workflows import load_workflow

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    store = EventStore()
    reg = ToolRegistry(cache=store)
    reg.register(FilesConnector(ROOT / "mock_company"))
    reg.register(PortalConnector(client=TestClient(create_app())))
    wf = load_workflow(ROOT / "workflows" / "expense.yaml")

    run = new_id("run")
    request = "Reimburse Rahul's flight from 3 October"
    store.append(run, EventType.RUN_CREATED, {"request": request, "workflow": wf.name})
    contract = TaskContract(run_id=run, request=request, workflow=wf.name,
                            goal="Claim and submit Rahul's INR 7850 flight", entities={"employee_id": "E001"},
                            permitted_tools=wf.permitted_tools, acceptance_criteria=wf.acceptance_criteria,
                            risk_tier=RiskClass.IRREVERSIBLE)
    store.append(run, EventType.CONTRACT_COMPILED, {"contract": contract})
    receipt = "docs/receipts/rahul_2026-10-03_flight.txt"
    claim = {"employee_id": "E001", "category": "travel", "amount": 7850, "expense_date": "2026-10-03",
             "receipt_ref": receipt, "client_ref": f"{run}:s2"}
    plan = Plan(rationale="hand-written demo plan", steps=[
        PlanStep(id="s1", description="Read the receipt", tool="files.read_text", args={"path": receipt}),
        PlanStep(id="s2", description="Create draft claim", tool="portal.create_claim", args=claim,
                 depends_on=["s1"], risk=RiskClass.WRITE),
        PlanStep(id="s3", description="Submit claim (final)", tool="portal.submit_claim",
                 args={"claim_id": "$s2.id"}, depends_on=["s2"], risk=RiskClass.IRREVERSIBLE),
    ])
    store.append(run, EventType.PLAN_CREATED, {"plan": plan})

    outputs: dict[str, dict] = {}
    while (step := store.state(run).plan.next_runnable()) is not None:
        args = {k: (outputs[v[1:].split(".")[0]][v.split(".")[1]] if isinstance(v, str) and v.startswith("$") else v)
                for k, v in step.args.items()}
        store.append(run, EventType.STEP_STARTED, {"step_id": step.id})
        res = reg.call(step.tool, args, idempotency_key=f"{run}:{step.id}", run_id=run)
        outputs[step.id] = res.output if isinstance(res.output, dict) else {}
        obs = Observation(step_id=step.id, tool=step.tool, ok=res.ok, output=res.output, error=res.error,
                          error_kind=res.error_kind, duration_ms=res.duration_ms, tainted=res.tainted)
        store.append(run, EventType.OBSERVATION, {"observation": obs})
        print(f"  {step.id} {step.tool:<22} ok={res.ok} tainted={res.tainted} {res.duration_ms:.1f}ms")

    # Independent verification: re-query the system of record, do not trust the executor.
    final = reg.call("portal.get_claim", {"claim_id": outputs["s2"]["id"]}).output
    passed = final["status"] == "submitted" and final["amount"] == 7850 and bool(final["receipt_ref"])
    store.append(run, EventType.RUN_COMPLETED if passed else EventType.RUN_FAILED,
                 {"summary": f"claim {final['id']} submitted"} if passed else {"reason": "verification failed"})

    st = store.state(run)
    print(f"\nrun status : {st.status.value}  ({st.reason})")
    print(f"events     : {st.events_seen}   audit chain intact: {store.verify_chain()[0]}")
    print(f"portal says: {final['id']} status={final['status']} amount={final['amount']}")


if __name__ == "__main__":
    main()

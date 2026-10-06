import pytest
from fastapi.testclient import TestClient

from mock_company.portal.app import create_app
from operon.connectors.portal import PortalConnector
from operon.connectors.registry import ToolRegistry
from operon.schemas import ErrorKind

CLAIM = dict(employee_id="E001", category="travel", amount=7850, expense_date="2026-10-03",
             receipt_ref="docs/receipts/rahul_2026-10-03_flight.txt", client_ref="run1:s3")


@pytest.fixture
def env():
    app = create_app()
    client = TestClient(app)
    reg = ToolRegistry()
    reg.register(PortalConnector(client=client))
    return app, client, reg


def test_create_is_idempotent_on_client_ref_and_submit_is_final(env):
    _, _, reg = env
    a = reg.call("portal.create_claim", CLAIM)
    b = reg.call("portal.create_claim", CLAIM)
    assert a.ok and a.output["id"] == b.output["id"] == "CLM-0001"
    assert len(reg.call("portal.list_claims", {"employee_id": "E001"}).output) == 1
    sub = reg.call("portal.submit_claim", {"claim_id": "CLM-0001"})
    assert sub.output["status"] == "submitted"
    assert reg.call("portal.submit_claim", {"claim_id": "CLM-0001"}).ok  # idempotent
    dele = reg.call("portal.delete_claim", {"claim_id": "CLM-0001"})
    assert dele.error_kind == ErrorKind.CONFLICT


def test_server_side_rules_surface_as_classified_errors(env):
    _, _, reg = env
    no_receipt = {**CLAIM, "receipt_ref": None, "client_ref": "run1:s9"}
    cid = reg.call("portal.create_claim", no_receipt).output["id"]
    r = reg.call("portal.submit_claim", {"claim_id": cid})
    assert not r.ok and r.error_kind == ErrorKind.VALIDATION and "receipt" in r.error
    assert reg.call("portal.get_employee", {"employee_id": "E999"}).error_kind == ErrorKind.NOT_FOUND
    assert reg.call("portal.create_claim", {**CLAIM, "amount": -5}).error_kind == ErrorKind.VALIDATION


def test_dry_run_does_not_mutate_and_compensation_args(env):
    _, _, reg = env
    d = reg.call("portal.create_claim", CLAIM, dry_run=True)
    assert d.ok and d.dry_run and reg.call("portal.list_claims").output == []
    conn, spec = reg.resolve("portal.create_claim")
    created = reg.call("portal.create_claim", CLAIM)
    assert spec.compensate_with == "delete_claim"
    args = conn.compensation_args("create_claim", CLAIM, created.output)
    assert reg.call("portal.delete_claim", args).ok
    assert reg.call("portal.list_claims").output == []


def test_chaos_503_is_transient_then_recovers(env):
    app, client, reg = env
    client.post("/admin/chaos", json={"fail_next": 2})
    first = reg.call("portal.list_claims")
    assert not first.ok and first.error_kind == ErrorKind.TRANSIENT
    assert reg.call("portal.list_claims").error_kind == ErrorKind.TRANSIENT
    assert reg.call("portal.list_claims").ok  # recovered


def test_ui_variants_rename_ids_but_keep_labels(env):
    _, client, _ = env
    a = client.get("/claims/new").text
    client.post("/admin/chaos", json={"ui_variant": "b"})
    b = client.get("/claims/new").text
    assert "id='amount'" in a and "id='f-amt'" in b and "Amount (INR)" in a and "Amount (INR)" in b
    client.post("/claims/new", content="employee_id=E001&category=meals&amount=100&expense_date=2026-10-01",
                headers={"content-type": "application/x-www-form-urlencoded"})
    assert "CLM-0001" in client.get("/claims").text
    client.post("/admin/reset")
    assert client.get("/api/claims").json() == []

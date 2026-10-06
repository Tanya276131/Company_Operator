"""Connector for the mock expense portal's REST API (mock_company/portal).

This doubles as the template for any internal HTTP system: classify HTTP
failures into ErrorKind so the repair ladder knows whether to retry (5xx /
timeouts -> TRANSIENT), fix the call (422 -> VALIDATION) or escalate (409/403).
"""
from __future__ import annotations

from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from operon.connectors.base import (
    Connector,
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ToolError,
    TransientError,
    ValidationFailed,
    tool,
)
from operon.schemas import RiskClass


class EmployeeArgs(BaseModel):
    employee_id: str


class ListClaimsArgs(BaseModel):
    employee_id: str | None = None
    status: Literal["draft", "submitted"] | None = None


class ClaimIdArgs(BaseModel):
    claim_id: str


class CreateClaimArgs(BaseModel):
    employee_id: str
    category: Literal["meals", "travel", "lodging", "cab", "other"]
    amount: float = Field(gt=0, description="Amount in the stated currency")
    currency: str = "INR"
    expense_date: str = Field(description="ISO date YYYY-MM-DD")
    receipt_ref: str | None = Field(None, description="Path of the receipt document")
    description: str = ""
    client_ref: str | None = Field(
        None, description="Idempotency reference, e.g. '<run_id>:<step_id>'. Reusing it returns the same claim."
    )


class PortalConnector(Connector):
    name = "portal"
    description = "Acme expense portal: employees and reimbursement claims."

    def __init__(self, base_url: str = "http://127.0.0.1:8000", client: httpx.Client | None = None,
                 timeout: float = 10.0):
        super().__init__()
        self.client = client or httpx.Client(base_url=base_url, timeout=timeout)

    # -- http helper -----------------------------------------------------------
    def _request(self, method: str, path: str, **kw: Any) -> Any:
        try:
            r = self.client.request(method, path, **kw)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise TransientError(f"portal unreachable: {type(e).__name__}") from e
        if r.status_code >= 500:
            raise TransientError(f"portal error {r.status_code}")
        if r.status_code < 400:
            return r.json()
        detail = ""
        try:
            detail = r.json().get("detail", "")
        except Exception:
            detail = r.text[:200]
        if r.status_code == 404:
            raise NotFoundError(str(detail))
        if r.status_code in (400, 422):
            raise ValidationFailed(str(detail))
        if r.status_code == 403:
            raise PermissionDenied(str(detail))
        if r.status_code == 409:
            raise ConflictError(str(detail))
        raise ToolError(f"portal returned {r.status_code}: {detail}")

    # -- read tools ------------------------------------------------------------
    @tool(args=EmployeeArgs)
    def get_employee(self, a: EmployeeArgs):
        """Look up an employee (name, department, manager)."""
        return self._request("GET", f"/api/employees/{a.employee_id}")

    @tool(args=ListClaimsArgs)
    def list_claims(self, a: ListClaimsArgs):
        """List reimbursement claims, optionally filtered by employee and status."""
        params = {k: v for k, v in a.model_dump().items() if v is not None}
        return self._request("GET", "/api/claims", params=params)

    @tool(args=ClaimIdArgs)
    def get_claim(self, a: ClaimIdArgs):
        """Fetch one claim by id (the system of record, used for verification)."""
        return self._request("GET", f"/api/claims/{a.claim_id}")

    # -- write tools -----------------------------------------------------------
    @tool(args=CreateClaimArgs, risk=RiskClass.WRITE, idempotent=True,
          compensate_with="delete_claim", dry_run=True)
    def create_claim(self, a: CreateClaimArgs, dry_run: bool = False):
        """Create a DRAFT claim. Safe to retry when client_ref is set. Not final until submitted."""
        if dry_run:
            return {"would_create": a.model_dump()}
        return self._request("POST", "/api/claims", json=a.model_dump())

    @tool(args=ClaimIdArgs, risk=RiskClass.IRREVERSIBLE, idempotent=True, dry_run=True)
    def submit_claim(self, a: ClaimIdArgs, dry_run: bool = False):
        """Submit a draft claim for payment. FINAL: a submitter cannot withdraw it."""
        if dry_run:
            claim = self._request("GET", f"/api/claims/{a.claim_id}")
            return {"would_submit": claim}
        return self._request("POST", f"/api/claims/{a.claim_id}/submit")

    @tool(args=ClaimIdArgs, risk=RiskClass.WRITE, idempotent=True)
    def delete_claim(self, a: ClaimIdArgs):
        """Delete a DRAFT claim (compensation for create_claim). Submitted claims cannot be deleted."""
        return self._request("DELETE", f"/api/claims/{a.claim_id}")

    def compensation_args(self, tool_name: str, args: dict[str, Any], output: Any) -> dict[str, Any]:
        if tool_name == "create_claim":
            return {"claim_id": output["id"]}
        return super().compensation_args(tool_name, args, output)

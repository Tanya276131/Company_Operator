"""Acme Corp's mock expense portal: a REST API *and* an HTML UI over one SQLite DB.

It exists so the agent can be tested against a real, controllable system:

* REST  /api/...      for API connectors
* HTML  /claims ...   for the browser connector
* Chaos /admin/chaos  to inject failures on demand (503s, latency, renamed UI ids)
* Server-side rules the agent must respect, not bypass:
    - a claim needs a receipt_ref to be submitted
    - submitted claims are final (no delete)
    - client_ref makes claim creation idempotent

Run:  python -m uvicorn mock_company.portal.app:app --port 8000
"""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import sqlite3
import threading
from typing import Any, Literal
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

SEED_EMPLOYEES = [
    ("E001", "Rahul Sharma", "rahul.sharma@acme.example", "E010", "Engineering"),
    ("E002", "Priya Nair", "priya.nair@acme.example", "E010", "Engineering"),
    ("E003", "Aarav Mehta", "aarav.mehta@acme.example", "E011", "Sales"),
    ("E010", "Meera Iyer", "meera.iyer@acme.example", None, "Engineering"),
    ("E011", "Kabir Singh", "kabir.singh@acme.example", None, "Sales"),
]

DEFAULT_CHAOS: dict[str, Any] = {"fail_next": 0, "latency_ms": 0, "ui_variant": "a"}


class ClaimIn(BaseModel):
    employee_id: str
    category: Literal["meals", "travel", "lodging", "cab", "other"]
    amount: float = Field(gt=0)
    currency: str = "INR"
    expense_date: str
    receipt_ref: str | None = None
    description: str = ""
    client_ref: str | None = None


class ChaosIn(BaseModel):
    fail_next: int | None = None
    latency_ms: int | None = None
    ui_variant: Literal["a", "b"] | None = None


class PortalDB:
    def __init__(self, path: str = ":memory:", seed: bool = True):
        self._lock = threading.RLock()
        self._c = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._c.row_factory = sqlite3.Row
        self.reset(seed)

    def reset(self, seed: bool = True) -> None:
        with self._lock:
            self._c.executescript(
                """
                DROP TABLE IF EXISTS employees; DROP TABLE IF EXISTS claims;
                CREATE TABLE employees(id TEXT PRIMARY KEY, name TEXT, email TEXT,
                                       manager_id TEXT, department TEXT);
                CREATE TABLE claims(id TEXT PRIMARY KEY, client_ref TEXT UNIQUE, employee_id TEXT,
                                    category TEXT, amount REAL, currency TEXT, expense_date TEXT,
                                    receipt_ref TEXT, description TEXT, status TEXT,
                                    created_at TEXT, submitted_at TEXT);
                """
            )
            if seed:
                self._c.executemany("INSERT INTO employees VALUES (?,?,?,?,?)", SEED_EMPLOYEES)

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        with self._lock:
            r = self._c.execute(sql, args).fetchone()
        return dict(r) if r else None

    def many(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._c.execute(sql, args).fetchall()]

    def run(self, sql: str, args: tuple = ()) -> None:
        with self._lock:
            self._c.execute(sql, args)

    def create_claim(self, c: ClaimIn) -> tuple[dict, bool]:
        """Returns (claim, created). Idempotent on client_ref."""
        with self._lock:
            if c.client_ref:
                existing = self.one("SELECT * FROM claims WHERE client_ref = ?", (c.client_ref,))
                if existing:
                    return existing, False
            if not self.one("SELECT 1 FROM employees WHERE id = ?", (c.employee_id,)):
                raise HTTPException(404, f"employee {c.employee_id} not found")
            try:
                dt.date.fromisoformat(c.expense_date)
            except ValueError:
                raise HTTPException(422, "expense_date must be an ISO date (YYYY-MM-DD)")
            n = self.one("SELECT COUNT(*) AS n FROM claims")["n"] + 1  # type: ignore[index]
            cid = f"CLM-{n:04d}"
            self.run(
                "INSERT INTO claims VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, c.client_ref, c.employee_id, c.category, c.amount, c.currency,
                 c.expense_date, c.receipt_ref, c.description, "draft",
                 dt.datetime.now(dt.timezone.utc).isoformat(), None),
            )
            return self.one("SELECT * FROM claims WHERE id = ?", (cid,)), True  # type: ignore[return-value]

    def submit_claim(self, cid: str) -> dict:
        with self._lock:
            claim = self.one("SELECT * FROM claims WHERE id = ?", (cid,))
            if not claim:
                raise HTTPException(404, f"claim {cid} not found")
            if claim["status"] == "submitted":
                return claim
            if not claim["receipt_ref"]:
                raise HTTPException(422, "receipt_ref is required to submit a claim")
            self.run(
                "UPDATE claims SET status='submitted', submitted_at=? WHERE id=?",
                (dt.datetime.now(dt.timezone.utc).isoformat(), cid),
            )
            return self.one("SELECT * FROM claims WHERE id = ?", (cid,))  # type: ignore[return-value]

    def delete_claim(self, cid: str) -> dict:
        with self._lock:
            claim = self.one("SELECT * FROM claims WHERE id = ?", (cid,))
            if not claim:
                raise HTTPException(404, f"claim {cid} not found")
            if claim["status"] != "draft":
                raise HTTPException(409, "only draft claims can be deleted")
            self.run("DELETE FROM claims WHERE id = ?", (cid,))
            return {"deleted": cid}


# ------------------------------------------------------------------------- HTML
_IDS = {
    "a": {"employee_id": "employee_id", "category": "category", "amount": "amount",
          "expense_date": "expense_date", "receipt_ref": "receipt_ref",
          "description": "description", "button": "Save draft"},
    "b": {"employee_id": "f-emp", "category": "f-cat", "amount": "f-amt",
          "expense_date": "f-date", "receipt_ref": "f-receipt",
          "description": "f-desc", "button": "Create claim"},
}


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title>"
        "<style>body{font-family:system-ui;margin:2rem;max-width:760px}label{display:block;margin:.6rem 0 .2rem}"
        "table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:.4rem;text-align:left}</style>"
        f"</head><body><h1>Acme Expense Portal</h1><nav><a href='/claims'>Claims</a> | "
        f"<a href='/claims/new'>New claim</a></nav><h2>{html.escape(title)}</h2>{body}</body></html>"
    )


def create_app(db_path: str = ":memory:", seed: bool = True) -> FastAPI:
    app = FastAPI(title="Acme Expense Portal (mock)")
    db = PortalDB(db_path, seed)
    app.state.db = db
    app.state.chaos = dict(DEFAULT_CHAOS)
    chaos_lock = threading.Lock()

    @app.middleware("http")
    async def chaos_middleware(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api") or path.startswith("/claims"):
            ch = app.state.chaos
            if ch["latency_ms"] > 0:
                await asyncio.sleep(ch["latency_ms"] / 1000)
            with chaos_lock:
                failing = ch["fail_next"] > 0
                if failing:
                    ch["fail_next"] -= 1
            if failing:
                return JSONResponse({"detail": "service temporarily unavailable"}, status_code=503)
        return await call_next(request)

    # ------------------------------------------------------------------ admin
    @app.get("/admin/chaos")
    def get_chaos():
        return app.state.chaos

    @app.post("/admin/chaos")
    def set_chaos(c: ChaosIn):
        for k, v in c.model_dump(exclude_none=True).items():
            app.state.chaos[k] = v
        return app.state.chaos

    @app.post("/admin/reset")
    def reset():
        db.reset(True)
        app.state.chaos.update(DEFAULT_CHAOS)
        return {"reset": True}

    # -------------------------------------------------------------------- API
    @app.get("/api/employees/{employee_id}")
    def get_employee(employee_id: str):
        e = db.one("SELECT * FROM employees WHERE id = ?", (employee_id,))
        if not e:
            raise HTTPException(404, f"employee {employee_id} not found")
        return e

    @app.get("/api/claims")
    def list_claims(employee_id: str | None = None, status: str | None = None):
        sql, args = "SELECT * FROM claims WHERE 1=1", []
        if employee_id:
            sql += " AND employee_id = ?"; args.append(employee_id)
        if status:
            sql += " AND status = ?"; args.append(status)
        return db.many(sql + " ORDER BY id", tuple(args))

    @app.get("/api/claims/{claim_id}")
    def get_claim(claim_id: str):
        c = db.one("SELECT * FROM claims WHERE id = ?", (claim_id,))
        if not c:
            raise HTTPException(404, f"claim {claim_id} not found")
        return c

    @app.post("/api/claims")
    def create_claim(c: ClaimIn):
        claim, created = db.create_claim(c)
        return JSONResponse(claim, status_code=201 if created else 200)

    @app.post("/api/claims/{claim_id}/submit")
    def submit_claim(claim_id: str):
        return db.submit_claim(claim_id)

    @app.delete("/api/claims/{claim_id}")
    def delete_claim(claim_id: str):
        return db.delete_claim(claim_id)

    # --------------------------------------------------------------------- UI
    @app.get("/", response_class=HTMLResponse)
    def home():
        return RedirectResponse("/claims")

    @app.get("/claims", response_class=HTMLResponse)
    def claims_page():
        rows = db.many("SELECT * FROM claims ORDER BY id")
        trs = "".join(
            f"<tr><td>{html.escape(r['id'])}</td><td>{html.escape(r['employee_id'])}</td>"
            f"<td>{html.escape(r['category'])}</td><td>{r['amount']:.2f} {html.escape(r['currency'])}</td>"
            f"<td>{html.escape(r['expense_date'])}</td><td>{html.escape(r['status'])}</td>"
            f"<td>{'<form method=post action=/claims/' + html.escape(r['id']) + '/submit><button>Submit claim</button></form>' if r['status'] == 'draft' else ''}</td></tr>"
            for r in rows
        )
        return _page("Claims", f"<table><tr><th>ID</th><th>Employee</th><th>Category</th>"
                               f"<th>Amount</th><th>Date</th><th>Status</th><th></th></tr>{trs}</table>")

    @app.get("/claims/new", response_class=HTMLResponse)
    def new_claim_form():
        v = _IDS[app.state.chaos["ui_variant"]]
        cats = "".join(f"<option value='{c}'>{c}</option>" for c in ("meals", "travel", "lodging", "cab", "other"))
        form = (
            "<form method='post' action='/claims/new'>"
            f"<label for='{v['employee_id']}'>Employee ID</label><input id='{v['employee_id']}' name='employee_id'>"
            f"<label for='{v['category']}'>Category</label><select id='{v['category']}' name='category'>{cats}</select>"
            f"<label for='{v['amount']}'>Amount (INR)</label><input id='{v['amount']}' name='amount' type='number' step='0.01'>"
            f"<label for='{v['expense_date']}'>Expense date</label><input id='{v['expense_date']}' name='expense_date' placeholder='YYYY-MM-DD'>"
            f"<label for='{v['receipt_ref']}'>Receipt reference</label><input id='{v['receipt_ref']}' name='receipt_ref'>"
            f"<label for='{v['description']}'>Description</label><input id='{v['description']}' name='description'>"
            f"<p><button type='submit'>{v['button']}</button></p></form>"
        )
        return _page("New claim", form)

    @app.post("/claims/new")
    async def new_claim_submit(request: Request):
        data = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
        try:
            claim_in = ClaimIn(
                employee_id=data.get("employee_id", ""), category=data.get("category", "other"),
                amount=float(data.get("amount") or 0), expense_date=data.get("expense_date", ""),
                receipt_ref=data.get("receipt_ref") or None, description=data.get("description", ""),
            )
        except Exception as e:
            return _page("New claim: error", f"<p>Invalid input: {html.escape(str(e))}</p>")
        try:
            db.create_claim(claim_in)
        except HTTPException as e:
            return _page("New claim: error", f"<p>{html.escape(str(e.detail))}</p>")
        return RedirectResponse("/claims", status_code=303)

    @app.post("/claims/{claim_id}/submit")
    def submit_from_ui(claim_id: str):
        try:
            db.submit_claim(claim_id)
        except HTTPException as e:
            return _page("Submit failed", f"<p>{html.escape(str(e.detail))}</p>")
        return RedirectResponse("/claims", status_code=303)

    return app


app = create_app()

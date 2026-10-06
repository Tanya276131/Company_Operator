"""Smoke-test the Playwright connector (UNTESTED in the build env).
    python -m playwright install chromium
    python -m uvicorn mock_company.portal.app:app --port 8000     # in another terminal
    python scripts/browser_smoke.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from operon.connectors.browser import BrowserConnector
from operon.connectors.registry import ToolRegistry

reg = ToolRegistry()
b = reg.register(BrowserConnector())
try:
    print(reg.call("browser.goto", {"url": "http://127.0.0.1:8000/claims/new"}))
    for label, val in [("Employee ID", "E001"), ("Amount (INR)", "980"), ("Expense date", "2026-10-04")]:
        print(reg.call("browser.fill", {"target": label, "value": val}))
    print(reg.call("browser.screenshot", {"name": "form_filled"}))
    print(reg.call("browser.click", {"target": "Save draft"}))
    print(reg.call("browser.snapshot").output["text"][:300])
finally:
    b.close()

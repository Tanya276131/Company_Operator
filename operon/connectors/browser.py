"""Playwright browser connector (DOM-first computer use).

Design: prefer the DOM over pixels. `snapshot` returns the interactive elements
(label, id, name, value) plus visible text, which an LLM can act on cheaply and
deterministically; screenshots are *evidence* (and a future vision fallback).
`fill`/`click` accept a human label or text first and a CSS selector second, so
they survive a renamed element id (see the mock portal's `ui_variant` chaos mode).

STATUS: written against Playwright's sync API but NOT exercised in the build
environment (browser binaries could not be downloaded there). Run
`python -m playwright install chromium` and smoke-test it with
`python scripts/browser_smoke.py` before relying on it.

Page *text* is untrusted: goto/snapshot are marked untrusted so it is tainted.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from operon.connectors.base import (
    Connector,
    NotFoundError,
    ToolOutput,
    TransientError,
    ValidationFailed,
    tool,
)
from operon.schemas import RiskClass

_SNAPSHOT_JS = """
() => {
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const label = el => (el.labels && el.labels.length ? el.labels[0].innerText.trim()
    : (el.getAttribute('aria-label') || el.placeholder || el.innerText || el.value || '')).trim();
  const els = [];
  document.querySelectorAll('input,select,textarea,button,a[href],[role=button]').forEach(el => {
    if (!vis(el)) return;
    els.push({tag: el.tagName.toLowerCase(), type: el.type || null, id: el.id || null,
      name: el.getAttribute('name'), label: label(el).slice(0, 80),
      value: (el.value || '').slice(0, 80), href: el.getAttribute('href')});
  });
  return {title: document.title, url: location.href, elements: els,
          text: document.body.innerText.slice(0, 4000)};
}
"""


class GotoArgs(BaseModel):
    url: str


class FillArgs(BaseModel):
    target: str = Field(description="Field label text (preferred) or a CSS selector")
    value: str


class ClickArgs(BaseModel):
    target: str = Field(description="Button/link text (preferred) or a CSS selector")


class ScreenshotArgs(BaseModel):
    name: str = Field("page", pattern=r"^[\w\-]+$")


class BrowserConnector(Connector):
    name = "browser"
    description = "Operate web apps through the DOM: navigate, read, fill, click, screenshot."

    def __init__(self, headless: bool = True, artifacts_dir: str | Path = "artifacts/screenshots",
                 timeout_ms: int = 8000):
        super().__init__()
        self.headless = headless
        self.artifacts_dir = Path(artifacts_dir)
        self.timeout_ms = timeout_ms
        self._pw: Any = None
        self._browser: Any = None
        self._page: Any = None

    # -- lifecycle ---------------------------------------------------------------
    def page(self) -> Any:
        if self._page is None:
            from playwright.sync_api import sync_playwright  # lazy: optional at import time

            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=self.headless)
            self._page = self._browser.new_page()
            self._page.set_default_timeout(self.timeout_ms)
        return self._page

    def close(self) -> None:
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()
        self._pw = self._browser = self._page = None

    @staticmethod
    def _timeout_error() -> type[Exception]:
        from playwright.sync_api import TimeoutError as PWTimeout

        return PWTimeout

    # -- tools -------------------------------------------------------------------
    @tool(args=GotoArgs, untrusted=True)
    def goto(self, a: GotoArgs):
        """Navigate to a URL and return the page title and final URL."""
        pg = self.page()
        try:
            pg.goto(a.url, wait_until="domcontentloaded")
        except self._timeout_error() as e:
            raise TransientError(f"navigation timed out: {a.url}") from e
        return {"title": pg.title(), "url": pg.url}

    @tool(untrusted=True)
    def snapshot(self):
        """Return the current page's interactive elements and visible text."""
        return self.page().evaluate(_SNAPSHOT_JS)

    @tool(args=FillArgs, risk=RiskClass.WRITE, idempotent=True)
    def fill(self, a: FillArgs):
        """Fill a form field identified by its label text or a CSS selector."""
        pg = self.page()
        loc = pg.get_by_label(a.target)
        if loc.count() == 0:
            loc = pg.locator(a.target)
        if loc.count() == 0:
            raise NotFoundError(f"no field matching '{a.target}'")
        first = loc.first
        try:
            if first.evaluate("e => e.tagName") == "SELECT":
                first.select_option(a.value)
            else:
                first.fill(a.value)
        except self._timeout_error() as e:
            raise TransientError(f"timed out filling '{a.target}'") from e
        return {"filled": a.target}

    @tool(args=ClickArgs, risk=RiskClass.WRITE, idempotent=False)
    def click(self, a: ClickArgs):
        """Click a button or link by its text or a CSS selector. May submit forms."""
        pg = self.page()
        loc = pg.get_by_role("button", name=a.target)
        if loc.count() == 0:
            loc = pg.get_by_text(a.target, exact=True)
        if loc.count() == 0:
            loc = pg.locator(a.target)
        if loc.count() == 0:
            raise NotFoundError(f"nothing clickable matching '{a.target}'")
        try:
            loc.first.click()
            pg.wait_for_load_state("domcontentloaded")
        except self._timeout_error() as e:
            raise TransientError(f"timed out clicking '{a.target}'") from e
        return {"clicked": a.target, "url": pg.url}

    @tool(args=ScreenshotArgs)
    def screenshot(self, a: ScreenshotArgs):
        """Save a screenshot as evidence and return its path."""
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        path = self.artifacts_dir / f"{a.name}.png"
        self.page().screenshot(path=str(path))
        if not path.exists():
            raise ValidationFailed("screenshot failed")
        return ToolOutput(output={"path": path.as_posix()}, artifacts=[path.as_posix()])

import pytest

from operon.connectors.base import Connector, ToolOutput, tool
from operon.connectors.files import FilesConnector
from operon.connectors.registry import ToolRegistry
from operon.schemas import ErrorKind, RiskClass
from operon.state.store import EventStore
from pydantic import BaseModel


@pytest.fixture
def sandbox(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    (root / "docs").mkdir()
    (root / "docs" / "a.txt").write_text("hello", encoding="utf-8")
    return FilesConnector(root)


def test_files_read_is_tainted_and_sandboxed(sandbox):
    reg = ToolRegistry()
    reg.register(sandbox)
    r = reg.call("files.read_text", {"path": "docs/a.txt"})
    assert r.ok and r.output["content"] == "hello" and r.tainted
    esc = reg.call("files.read_text", {"path": "../secret.txt"})
    assert not esc.ok and esc.error_kind == ErrorKind.PERMISSION
    assert reg.call("files.read_text", {"path": "docs/missing.txt"}).error_kind == ErrorKind.NOT_FOUND


def test_files_write_only_in_workspace_and_dry_run(sandbox):
    reg = ToolRegistry()
    reg.register(sandbox)
    bad = reg.call("files.write_text", {"path": "docs/x.txt", "content": "z"})
    assert not bad.ok and bad.error_kind == ErrorKind.PERMISSION
    dry = reg.call("files.write_text", {"path": "workspace/x.txt", "content": "z"}, dry_run=True)
    assert dry.ok and dry.dry_run and not (sandbox.writable / "x.txt").exists()
    ok = reg.call("files.write_text", {"path": "workspace/x.txt", "content": "z"})
    assert ok.ok and (sandbox.writable / "x.txt").read_text() == "z"
    conflict = reg.call("files.write_text", {"path": "workspace/x.txt", "content": "different"})
    assert conflict.error_kind == ErrorKind.CONFLICT


def test_registry_validation_unknown_and_llm_schema(sandbox):
    reg = ToolRegistry()
    reg.register(sandbox)
    assert reg.call("nope.tool").error_kind == ErrorKind.NOT_FOUND
    bad = reg.call("files.read_text", {"max_chars": "abc"})
    assert bad.error_kind == ErrorKind.VALIDATION
    tools = reg.llm_tools(allowed=["files.read_*"])
    assert [t["function"]["name"] for t in tools] == ["files__read_text"]
    assert ToolRegistry.from_llm_name("files__read_text") == "files.read_text"


class _Args(BaseModel):
    n: int


class Counter(Connector):
    name = "counter"

    def __init__(self):
        super().__init__()
        self.calls = 0

    @tool(args=_Args, risk=RiskClass.WRITE, idempotent=True)
    def bump(self, a: _Args):
        """Increment a counter."""
        self.calls += a.n
        return {"total": self.calls}

    @tool()
    def boom(self):
        """Always fails with an unclassified error."""
        raise RuntimeError("kaput")

    @tool()
    def shot(self):
        """Returns evidence."""
        return ToolOutput(output={"x": 1}, artifacts=["a.png"])


def test_idempotency_prevents_double_write():
    store = EventStore()
    reg = ToolRegistry(cache=store)
    c = reg.register(Counter())
    r1 = reg.call("counter.bump", {"n": 5}, idempotency_key="run:s1", run_id="run")
    r2 = reg.call("counter.bump", {"n": 5}, idempotency_key="run:s1", run_id="run")  # resume replay
    assert r1.ok and r2.replayed and c.calls == 5
    reg.call("counter.bump", {"n": 5}, idempotency_key="run:s2", run_id="run")
    assert c.calls == 10


def test_unclassified_errors_are_contained_and_artifacts_returned():
    reg = ToolRegistry()
    reg.register(Counter())
    r = reg.call("counter.boom")
    assert not r.ok and r.error_kind == ErrorKind.UNKNOWN and "kaput" in r.error
    s = reg.call("counter.shot")
    assert s.ok and s.artifacts == ["a.png"] and s.output == {"x": 1}

"""Run this FIRST on your machine once you have an API key in .env:  python scripts/llm_smoke.py
Checks: key works, model names in config are valid, tool calling round-trips, recording works."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from operon.connectors.files import FilesConnector
from operon.connectors.registry import ToolRegistry
from operon.llm.router import LLMRouter

ROOT = Path(__file__).resolve().parents[1]
reg = ToolRegistry()
reg.register(FilesConnector(ROOT / "mock_company"))
router = LLMRouter.from_env(ROOT / "config" / "models.yaml")
for role in ("planner", "executor"):
    resp = router.complete(role, [{"role": "user", "content": "List the files in docs/receipts using a tool."}],
                           tools=reg.llm_tools(["files.*"]))
    print(role, "->", resp.provider, resp.model, "| tool_calls:", [(c.name, c.arguments) for c in resp.tool_calls],
          "| cached:", resp.cached)
print("stats:", dict(router.stats))

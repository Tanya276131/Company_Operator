"""Workflows as data.

A workflow is a YAML file: which sources matter, which tools are permitted, which
actions need a human, and what 'done' means (acceptance criteria). The runtime loop
never changes when you add one; that is the 'add new company workflows quickly'
claim made literal.
"""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from operon.schemas import AcceptanceCriterion


class ApprovalRule(BaseModel):
    tool: str  # qualified name or fnmatch pattern, e.g. "portal.submit_claim"
    require_approval: bool = True
    reason: str = ""
    when_arg_gt: dict[str, float] | None = None  # only if args[key] > value


class Workflow(BaseModel):
    name: str
    description: str = ""
    triggers: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    permitted_tools: list[str] = Field(default_factory=list)
    approval_rules: list[ApprovalRule] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def matches(self, request: str) -> bool:
        r = request.lower()
        return any(t.lower() in r for t in self.triggers)


def load_workflow(path: str | Path) -> Workflow:
    with open(path, encoding="utf-8") as f:
        return Workflow.model_validate(yaml.safe_load(f))


def load_workflows(directory: str | Path) -> dict[str, Workflow]:
    out: dict[str, Workflow] = {}
    for p in sorted(Path(directory).glob("*.y*ml")):
        wf = load_workflow(p)
        out[wf.name] = wf
    return out

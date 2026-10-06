"""Record / replay of LLM calls.

Why: (1) evals and ablations re-run thousands of calls: replay makes them free and
reproducible; (2) committed recordings let a reviewer run the demo and evals with
NO API key (mode=replay).

Cache key = sha256(role, messages, tools, response_format, temperature). It is
deliberately independent of provider/model, so a recording made on Gemini still
replays when the call would now fail over to Groq.

CONSEQUENCE: keep volatile data (run ids, timestamps, random ids) out of prompts, or
every run misses the cache. Pass such values through tool args or state instead.
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

from operon.llm.types import LLMResponse

VALID_MODES = ("off", "record", "replay", "auto")


def request_key(role: str, messages: list[dict], tools: list[dict] | None,
                response_format: dict | None, temperature: float) -> str:
    blob = json.dumps(
        {"role": role, "messages": messages, "tools": tools or [],
         "response_format": response_format, "temperature": temperature},
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class Recorder:
    def __init__(self, path: str | Path = "recordings/llm.jsonl", mode: str = "auto"):
        if mode not in VALID_MODES:
            raise ValueError(f"mode must be one of {VALID_MODES}")
        self.path = Path(path)
        self.mode = mode
        self._lock = threading.Lock()
        self._cache: dict[str, dict[str, Any]] = {}
        if mode != "off" and self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rec = json.loads(line)
                    self._cache[rec["key"]] = rec

    @property
    def can_read(self) -> bool:
        return self.mode in ("replay", "auto")

    @property
    def can_write(self) -> bool:
        return self.mode in ("record", "auto")

    def get(self, key: str) -> LLMResponse | None:
        if not self.can_read:
            return None
        rec = self._cache.get(key)
        if rec is None:
            return None
        resp = LLMResponse.model_validate(rec["response"])
        resp.cached = True
        return resp

    def put(self, key: str, role: str, resp: LLMResponse) -> None:
        if not self.can_write:
            return
        stored = resp.model_copy(update={"cached": False})
        rec = {"key": key, "role": role, "response": stored.model_dump(mode="json")}
        with self._lock:
            self._cache[key] = rec
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, sort_keys=True) + "\n")

    def __len__(self) -> int:
        return len(self._cache)

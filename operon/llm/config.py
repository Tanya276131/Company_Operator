"""Model/role configuration with ${ENV_VAR:-default} substitution."""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator


class ConfigError(Exception):
    pass


class Candidate(BaseModel):
    provider: str
    model: str


class ProviderLimits(BaseModel):
    rpm: float = 10.0


class RetryPolicy(BaseModel):
    max_retries: int = 3
    base_delay_s: float = 1.0
    max_delay_s: float = 20.0


class ModelsConfig(BaseModel):
    roles: dict[str, list[Candidate]]
    providers: dict[str, ProviderLimits] = Field(default_factory=dict)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)

    @field_validator("roles", mode="before")
    @classmethod
    def _wrap_single(cls, v):
        return {k: (x if isinstance(x, list) else [x]) for k, x in v.items()}

    def limits(self, provider: str) -> ProviderLimits:
        return self.providers.get(provider, ProviderLimits())


_ENV = re.compile(r"\$\{([A-Za-z0-9_]+)(?::-([^}]*))?\}")


def _substitute(text: str, env: dict[str, str]) -> str:
    def repl(m: re.Match) -> str:
        name, default = m.group(1), m.group(2)
        val = env.get(name)
        if val:  # empty string counts as unset
            return val
        if default is not None:
            return default
        raise ConfigError(f"environment variable {name} is required by the model config")

    return _ENV.sub(repl, text)


def load_models_config(path: str | Path, env: dict[str, str] | None = None) -> ModelsConfig:
    text = Path(path).read_text(encoding="utf-8")
    data = yaml.safe_load(_substitute(text, dict(os.environ) if env is None else env))
    return ModelsConfig.model_validate(data)

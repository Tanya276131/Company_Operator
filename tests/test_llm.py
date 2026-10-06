import pytest
from pydantic import BaseModel

from operon.llm.config import ConfigError, load_models_config, ModelsConfig
from operon.llm.fake import FakeBackend
from operon.llm.recorder import Recorder
from operon.llm.router import LLMRouter, RateLimiter
from operon.llm.types import (AllProvidersFailed, LLMError, LLMResponse, ReplayMiss, ToolCallReq,
                              TransientLLMError)

CFG = ModelsConfig.model_validate({
    "roles": {"planner": [{"provider": "gemini", "model": "g1"}, {"provider": "groq", "model": "q1"}]},
    "providers": {"gemini": {"rpm": 600}, "groq": {"rpm": 600}},
    "retry": {"max_retries": 2, "base_delay_s": 0.0, "max_delay_s": 0.0},
})
MSG = [{"role": "user", "content": "plan it"}]


def router(script, recorder=None):
    be = FakeBackend(script)
    return LLMRouter(CFG, backend=be, recorder=recorder, sleep=lambda s: None, jitter=lambda: 0.5), be


def test_retries_transient_errors_then_succeeds():
    r, be = router([TransientLLMError("429"), TransientLLMError("429"), "ok"])
    resp = r.complete("planner", MSG)
    assert resp.content == "ok" and resp.provider == "gemini" and len(be.calls) == 3
    assert r.stats["planner"]["retries"] == 2


def test_fails_over_to_next_provider():
    r, be = router([TransientLLMError("x")] * 3 + ["from groq"])
    resp = r.complete("planner", MSG)
    assert resp.provider == "groq" and resp.content == "from groq" and r.stats["planner"]["failovers"] == 1


def test_nonretryable_error_fails_over_immediately_and_all_fail_raises():
    r, be = router([LLMError("bad key"), LLMError("bad key too")])
    with pytest.raises(AllProvidersFailed) as e:
        r.complete("planner", MSG)
    assert len(be.calls) == 2 and "bad key" in str(e.value)
    with pytest.raises(LLMError):
        r.complete("nope", MSG)


def test_record_then_replay_without_backend(tmp_path):
    path = tmp_path / "llm.jsonl"
    tc = LLMResponse(tool_calls=[ToolCallReq(id="1", name="portal__get_claim", arguments={"claim_id": "CLM-1"})])
    r1, _ = router([tc], Recorder(path, "record"))
    r1.complete("planner", MSG)
    # reviewer without keys: backend would explode, replay serves the recording
    r2, be2 = router([], Recorder(path, "replay"))
    resp = r2.complete("planner", MSG)
    assert resp.cached and resp.tool_calls[0].arguments == {"claim_id": "CLM-1"} and not be2.calls
    with pytest.raises(ReplayMiss):
        r2.complete("planner", [{"role": "user", "content": "never recorded"}])


def test_auto_mode_records_then_hits(tmp_path):
    rec = Recorder(tmp_path / "a.jsonl", "auto")
    r, be = router(["first"], rec)
    assert not r.complete("planner", MSG).cached
    assert r.complete("planner", MSG).cached and len(be.calls) == 1


class Plan(BaseModel):
    steps: list[str]
    risky: bool


def test_complete_json_repairs_invalid_output():
    r, be = router(["Sure! here you go", '```json\n{"steps": ["a"], "risky": "maybe"}\n```',
                    '{"steps": ["a", "b"], "risky": false}'])
    out = r.complete_json("planner", MSG, Plan)
    assert out.steps == ["a", "b"] and out.risky is False and len(be.calls) == 3
    assert "invalid" in be.calls[2]["messages"][-1]["content"]


def test_rate_limiter_spaces_calls():
    t = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        t[0] += s

    rl = RateLimiter(rpm=60, clock=lambda: t[0], sleep=sleep)  # 1 call / second
    rl.wait(); rl.wait(); rl.wait()
    assert slept == pytest.approx([1.0, 1.0])


def test_config_env_substitution(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text('roles:\n  planner: {provider: gemini, model: "${M:-fallback-model}"}\n', encoding="utf-8")
    assert load_models_config(p, env={}).roles["planner"][0].model == "fallback-model"
    assert load_models_config(p, env={"M": "custom"}).roles["planner"][0].model == "custom"
    p.write_text('roles:\n  planner: {provider: gemini, model: "${REQUIRED}"}\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_models_config(p, env={})


def test_shipped_config_loads():
    cfg = load_models_config("config/models.yaml", env={})
    assert set(cfg.roles) == {"planner", "executor", "verifier", "cheap"}

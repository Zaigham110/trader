from typing import Literal

from pydantic import BaseModel, Field

from goldtrader.db import connect, migrate
from goldtrader.llm.breaker import CircuitBreaker
from goldtrader.llm.budget import Budget
from goldtrader.llm.fake import HANG, REFUSE, FakeProvider
from goldtrader.llm.prompts import PromptRegistry
from goldtrader.llm.provider import (
    PRICES,
    ProviderTransientError,
    ToolCall,
    ToolSpec,
    Usage,
    estimate_cost,
)
from goldtrader.llm.runtime import AgentSpec, Tool, run_agent
from goldtrader.llm.store import LLMStore

NOW = 1_762_300_000_000  # 2025-11-04 23:46:40 UTC


class Decision(BaseModel):
    action: Literal["wait", "enter"]
    confidence: float = Field(ge=0, le=1)
    thesis: str = Field(min_length=20)


GOOD = {"action": "wait", "confidence": 0.5, "thesis": "nothing to do right now, stay flat"}


def _store():
    conn = connect(None)
    migrate(conn)
    return LLMStore(conn)


def _spec(**over) -> AgentSpec:
    reg = PromptRegistry()
    base = dict(
        name="strategy",
        tier="standard",
        system_prompt=reg.register("strategy.system", 1, "You are the strategy bot."),
        build_messages=lambda ctx: [{"role": "user", "content": f"features: {ctx}"}],
        output_model=Decision,
        timeout_s=5.0,
        max_turns=6,
        max_cost_usd=1.0,
    )
    base.update(over)
    return AgentSpec(**base)


def _fallback(ctx, reason):
    return Decision(action="wait", confidence=0.0, thesis=f"fallback because {reason:<20}")


def test_happy_path_records_call_and_run():
    fake = FakeProvider()
    fake.script("standard", [GOOD])
    store = _store()
    spec = _spec(finalize=lambda out, ctx: {"summary": out.action})
    run = run_agent(spec, {"regime": "trend"}, fake, store, now_ms=NOW)
    assert run.status == "ok" and not run.degraded
    assert run.output == Decision(**GOOD)
    assert run.turns == 1 and run.model_id == "claude-sonnet-5-5"
    assert run.finalized == {"summary": "wait"}
    req = fake.requests[0]
    assert req.system == "You are the strategy bot."
    assert req.output_schema["additionalProperties"] is False
    assert req.metadata["prompt_hash"] == spec.system_prompt.hash
    calls = store.calls_for_run(run.run_id)
    assert len(calls) == 1 and calls[0]["status"] == "ok"
    assert calls[0]["prompt_hash"] == spec.system_prompt.hash
    assert calls[0]["schema_version"] == 1
    assert calls[0]["input_tokens"] > 0 and calls[0]["cost_usd"] > 0
    assert len(calls[0]["payload_hash"]) == 64
    row = store.get_run(run.run_id)
    assert row["status"] == "ok" and row["turns"] == 1 and '"action":"wait"' in row["output_json"]
    assert abs(run.cost_usd - calls[0]["cost_usd"]) < 1e-12


def test_invalid_output_is_repaired_with_errors_fed_back():
    fake = FakeProvider()
    fake.script("standard", [{**GOOD, "confidence": 2}, "garbage text", GOOD])
    store = _store()
    run = run_agent(_spec(), {}, fake, store, now_ms=NOW)
    assert run.status == "ok" and run.turns == 3
    second = fake.requests[1].messages
    assert second[-2]["role"] == "assistant"
    assert second[-1]["role"] == "user" and "above maximum" in second[-1]["content"]
    third = fake.requests[2].messages
    assert "not valid JSON" in third[-1]["content"]
    statuses = [r["status"] for r in store.calls_for_run(run.run_id)]
    assert statuses == ["invalid", "invalid", "ok"]


def test_invalid_three_times_falls_back_degraded():
    fake = FakeProvider()
    bad = {**GOOD, "thesis": "short"}
    fake.script("standard", [bad, bad, bad, GOOD])
    store = _store()
    breaker = CircuitBreaker("strategy", store)
    run = run_agent(_spec(fallback=_fallback), {}, fake, store, now_ms=NOW, breaker=breaker)
    assert run.status == "degraded" and run.degraded
    assert run.output.action == "wait" and run.output.confidence == 0.0
    assert run.failure_reason.startswith("invalid output")
    assert run.turns == 3 and fake.pending("standard") == 1
    assert breaker.failures == 1
    assert store.get_run(run.run_id)["degraded"] == 1


def test_invalid_without_fallback_returns_invalid_status():
    fake = FakeProvider()
    fake.script("standard", ["x", "y", "z"])
    run = run_agent(_spec(), {}, fake, _store(), now_ms=NOW)
    assert run.status == "invalid" and run.output is None and not run.degraded


def test_check_problems_are_fed_back_like_schema_errors():
    fake = FakeProvider()
    fake.script("standard", [{**GOOD, "action": "enter"}, GOOD])

    def check(out: Decision) -> list[str]:
        return ["enter requires confidence >= 0.6"] if out.action == "enter" else []

    run = run_agent(_spec(check=check), {}, fake, _store(), now_ms=NOW)
    assert run.status == "ok" and run.turns == 2
    assert "confidence >= 0.6" in fake.requests[1].messages[-1]["content"]


def test_transient_errors_retry_with_backoff_then_succeed():
    fake = FakeProvider()
    fake.script(
        "standard",
        [ProviderTransientError("503"), ProviderTransientError("429"), GOOD],
    )
    slept: list[float] = []
    store = _store()
    run = run_agent(_spec(), {}, fake, store, now_ms=NOW, sleep=slept.append)
    assert run.status == "ok" and run.turns == 1
    assert slept == [1.0, 2.0]
    statuses = [r["status"] for r in store.calls_for_run(run.run_id)]
    assert statuses == ["transient_error", "transient_error", "ok"]


def test_transient_errors_exhausted_is_a_failure():
    fake = FakeProvider()
    fake.script("standard", [ProviderTransientError("boom")] * 4 + [GOOD])
    run = run_agent(_spec(fallback=_fallback), {}, fake, _store(), now_ms=NOW, sleep=lambda s: None)
    assert run.status == "degraded" and "transient errors exhausted" in run.failure_reason
    assert fake.pending("standard") == 1


def test_hang_hits_deadline_and_falls_back():
    fake = FakeProvider()
    fake.script("standard", [HANG])
    store = _store()
    spec = _spec(timeout_s=0.3, fallback=_fallback)
    run = run_agent(spec, {}, fake, store, now_ms=NOW)
    assert run.status == "degraded" and run.degraded
    assert "deadline" in run.failure_reason
    assert 250 <= run.latency_ms < 2000
    assert fake.requests[0].timeout_s <= 0.3
    assert [r["status"] for r in store.calls_for_run(run.run_id)] == ["timeout"]


def test_hang_without_fallback_is_timeout_status():
    fake = FakeProvider()
    fake.script("standard", [HANG])
    run = run_agent(_spec(timeout_s=0.2), {}, fake, _store(), now_ms=NOW)
    assert run.status == "timeout" and run.output is None


def test_slow_tool_counts_against_the_same_deadline():
    import time

    fake = FakeProvider()
    fake.script("standard", [ToolCall("t1", "slow", {}), GOOD])
    tool = Tool(
        ToolSpec("slow", "sleeps", {"type": "object", "properties": {}}),
        lambda inp: time.sleep(2.0),
    )
    run = run_agent(_spec(timeout_s=0.3, tools=[tool]), {}, fake, _store(), now_ms=NOW)
    assert run.status == "timeout" and "tool" in run.failure_reason
    assert len(fake.requests) == 1


def test_tool_round_trip_with_input_validation_error():
    fake = FakeProvider()
    fake.script(
        "standard",
        [
            [
                ToolCall("c1", "get_features", {"symbol": "XAUUSDm"}),
                ToolCall("c2", "get_features", {"symbol": 12}),
                ToolCall("c3", "nope", {}),
            ],
            GOOD,
        ],
    )
    seen: list[dict] = []

    def handler(inp: dict) -> dict:
        seen.append(inp)
        return {"atr": 12.5, "symbol": inp["symbol"]}

    tool = Tool(
        ToolSpec(
            "get_features",
            "read-only features",
            {
                "type": "object",
                "properties": {"symbol": {"type": "string"}},
                "required": ["symbol"],
                "additionalProperties": False,
            },
        ),
        handler,
    )
    run = run_agent(_spec(tools=[tool]), {}, fake, _store(), now_ms=NOW)
    assert run.status == "ok" and run.turns == 2
    assert seen == [{"symbol": "XAUUSDm"}]  # the invalid input never reached the handler
    assert [t.name for t in fake.requests[0].tools] == ["get_features"]
    msgs = fake.requests[1].messages
    assert msgs[-2]["role"] == "assistant" and msgs[-2]["content"][0]["type"] == "tool_use"
    results = msgs[-1]["content"]
    assert msgs[-1]["role"] == "user" and [r["tool_use_id"] for r in results] == ["c1", "c2", "c3"]
    assert results[0]["is_error"] is False and '"atr": 12.5' in results[0]["content"]
    assert results[1]["is_error"] is True and "expected type string" in results[1]["content"]
    assert results[2]["is_error"] is True and "unknown tool" in results[2]["content"]


def test_tool_handler_exception_is_reported_not_raised():
    fake = FakeProvider()
    fake.script("standard", [ToolCall("c1", "boom", {}), GOOD])

    def handler(inp):
        raise RuntimeError("db down")

    tool = Tool(ToolSpec("boom", "", {"type": "object"}), handler)
    run = run_agent(_spec(tools=[tool]), {}, fake, _store(), now_ms=NOW)
    assert run.status == "ok"
    assert "db down" in fake.requests[1].messages[-1]["content"][0]["content"]


def test_max_turns_cap_stops_tool_loops():
    fake = FakeProvider()
    fake.script("standard", [ToolCall("c", "echo", {})] * 10)
    tool = Tool(ToolSpec("echo", "", {"type": "object"}), lambda inp: "ok")
    run = run_agent(_spec(tools=[tool], max_turns=3), {}, fake, _store(), now_ms=NOW)
    assert run.status == "error" and "max_turns" in run.failure_reason
    assert run.turns == 3 and len(fake.requests) == 3


def test_budget_exhausted_returns_budget_without_calling_provider():
    fake = FakeProvider()
    fake.script("standard", [GOOD])
    store = _store()
    store.record_call(
        agent="other",
        run_id="r0",
        model_id="claude-sonnet-5-5",
        prompt_hash="x",
        schema_version=1,
        usage=Usage(1, 1),
        cost_usd=2.9999,
        latency_ms=1,
        status="ok",
        created_ms=NOW - 60_000,
    )
    budget = Budget(daily_usd=3.0, monthly_usd=60.0)
    breaker = CircuitBreaker("strategy", store)
    assert budget.allows(store, NOW, estimated_cost=0.0)
    run = run_agent(
        _spec(fallback=_fallback), {}, fake, store, now_ms=NOW, budget=budget, breaker=breaker
    )
    assert run.status == "ok"  # 2.9999 < 3.00: fits; its own cost tips the day over
    assert fake.pending("standard") == 0
    fake.script("standard", [GOOD])
    run = run_agent(
        _spec(fallback=_fallback), {}, fake, store, now_ms=NOW, budget=budget, breaker=breaker
    )
    assert run.status == "budget" and run.output is None and not run.degraded
    assert "daily budget" in run.failure_reason
    assert fake.pending("standard") == 1 and len(fake.requests) == 1
    assert breaker.failures == 0  # a budget stop is not a failure
    assert store.get_run(run.run_id)["status"] == "budget"


def test_breaker_open_skips_the_call_and_half_open_trial_runs():
    fake = FakeProvider()
    store = _store()
    breaker = CircuitBreaker("strategy", store)
    for _ in range(3):
        fake.script("standard", [REFUSE])
        run = run_agent(_spec(), {}, fake, store, now_ms=NOW, breaker=breaker)
        assert run.status == "error" and run.failure_reason == "refusal"
    assert breaker.state == "open"
    fake.script("standard", [GOOD])
    run = run_agent(_spec(), {}, fake, store, now_ms=NOW + 1000, breaker=breaker)
    assert run.status == "breaker_open" and len(fake.requests) == 3
    # a fresh process reloads the open breaker from the store
    reloaded = CircuitBreaker("strategy", store)
    assert reloaded.state == "open" and reloaded.next_trial_ms == breaker.next_trial_ms
    run = run_agent(_spec(), {}, fake, store, now_ms=NOW + 1800 * 1000, breaker=reloaded)
    assert run.status == "ok" and reloaded.state == "closed"


def test_run_cost_cap_is_enforced():
    fake = FakeProvider()
    fake.script("standard", [GOOD])
    run = run_agent(_spec(max_cost_usd=1e-9, fallback=_fallback), {}, fake, _store(), now_ms=NOW)
    assert run.status == "degraded" and "over cap" in run.failure_reason


def test_cost_accounting_matches_the_price_table():
    fake = FakeProvider()
    fake.script("standard", [GOOD])
    store = _store()
    run = run_agent(_spec(), {}, fake, store, now_ms=NOW)
    call = store.calls_for_run(run.run_id)[0]
    usage = Usage(call["input_tokens"], call["output_tokens"])
    in_p, out_p = PRICES["claude-sonnet-5-5"]
    expected = (usage.input_tokens * in_p + usage.output_tokens * out_p) / 1e6
    assert abs(call["cost_usd"] - expected) < 1e-12
    assert abs(estimate_cost("claude-sonnet-5-5", usage) - expected) < 1e-12
    assert abs(run.cost_usd - expected) < 1e-12
    assert store.spent_since(NOW) == call["cost_usd"]


def test_bot_bug_in_build_messages_becomes_error_not_exception():
    def broken(ctx):
        raise KeyError("features")

    run = run_agent(_spec(build_messages=broken), {}, FakeProvider(), _store(), now_ms=NOW)
    assert run.status == "error" and "build_messages" in run.failure_reason

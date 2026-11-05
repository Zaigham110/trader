from goldtrader.db import connect, migrate
from goldtrader.llm.prompts import PromptRegistry, PromptRef, prompts_hash, register
from goldtrader.llm.provider import PRICES, Usage, estimate_cost
from goldtrader.llm.store import LLMStore

NOW = 1_762_300_000_000


def _store() -> tuple[LLMStore, object]:
    conn = connect(None)
    assert migrate(conn) >= 2  # other sessions add later migrations
    return LLMStore(conn), conn


def test_migration_creates_llm_tables_once():
    store, conn = _store()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"llm_calls", "agent_runs", "breaker_state", "prompts"} <= tables
    v = migrate(conn)
    assert v >= 2
    versions = [r[0] for r in conn.execute("SELECT version FROM schema_version ORDER BY version")]
    assert versions == list(range(1, v + 1))  # 002 did not insert its own schema_version row


def test_call_round_trip_and_spent_since():
    store, _ = _store()
    usage = Usage(1000, 200, cache_read_tokens=500, cache_write_tokens=100)
    cost = estimate_cost("claude-haiku-5-5", usage)
    in_p, out_p = PRICES["claude-haiku-5-5"]
    assert (
        abs(cost - (1000 * in_p + 500 * in_p * 0.1 + 100 * in_p * 1.25 + 200 * out_p) / 1e6) < 1e-15
    )
    rid = store.record_call(
        agent="research",
        run_id="run1",
        model_id="claude-haiku-5-5",
        prompt_hash="abcd" * 4,
        schema_version=3,
        usage=usage,
        cost_usd=cost,
        latency_ms=321,
        status="ok",
        stop_reason="end_turn",
        payload_hash="ff" * 32,
        created_ms=NOW,
    )
    assert rid == 1
    row = store.calls_for_run("run1")[0]
    assert (
        row["input_tokens"],
        row["output_tokens"],
        row["cache_read_tokens"],
        row["cache_write_tokens"],
    ) == (1000, 200, 500, 100)
    assert row["schema_version"] == 3 and row["stop_reason"] == "end_turn"
    assert row["model_id"] == "claude-haiku-5-5" and row["latency_ms"] == 321
    assert store.spent_since(NOW) == cost
    assert store.spent_since(NOW + 1) == 0.0


def test_run_round_trip_replaces_on_same_id():
    store, _ = _store()
    store.record_run(
        run_id="r1",
        agent="strategy",
        started_ms=NOW,
        finished_ms=None,
        status="running",
        turns=0,
        cost_usd=0.0,
        degraded=False,
        failure_reason=None,
    )
    store.record_run(
        run_id="r1",
        agent="strategy",
        started_ms=NOW,
        finished_ms=NOW + 500,
        status="degraded",
        turns=3,
        cost_usd=0.01,
        degraded=True,
        failure_reason="invalid output",
        output={"action": "wait"},
    )
    row = store.get_run("r1")
    assert row["status"] == "degraded" and row["degraded"] == 1 and row["turns"] == 3
    assert row["output_json"] == '{"action":"wait"}'
    assert store.get_run("nope") is None


def test_breaker_state_round_trip():
    store, _ = _store()
    assert store.get_state("x") is None
    store.set_state(
        "x",
        {
            "state": "open",
            "failures": 3,
            "opened_ms": NOW,
            "next_trial_ms": NOW + 5,
            "updated_ms": NOW,
        },
    )
    assert store.get_state("x") == {
        "state": "open",
        "failures": 3,
        "opened_ms": NOW,
        "next_trial_ms": NOW + 5,
        "updated_ms": NOW,
    }
    store.set_state("x", {"state": "closed", "failures": 0, "updated_ms": NOW + 9})
    s = store.get_state("x")
    assert s["state"] == "closed" and s["opened_ms"] is None and s["updated_ms"] == NOW + 9


def test_prompt_registry_hashes_and_store():
    reg = PromptRegistry()
    a = reg.register("research.system", 1, "Extract gold-relevant events.")
    assert isinstance(a, PromptRef) and len(a.hash) == 16
    assert reg.get("research.system") is a
    h1 = reg.prompts_hash()
    assert len(h1) == 16
    reg.register("research.system", 1, "Extract gold-relevant events.")  # idempotent
    assert reg.prompts_hash() == h1
    reg.register("strategy.system", 1, "Decide wait/enter/exit.")
    h2 = reg.prompts_hash()
    assert h2 != h1
    changed = PromptRegistry()
    changed.register("research.system", 1, "Extract gold-relevant events!")
    changed.register("strategy.system", 1, "Decide wait/enter/exit.")
    assert changed.prompts_hash() != h2  # a one-character prompt change changes the stamp
    try:
        reg.register("strategy.system", 1, "different text")
    except ValueError as e:
        assert "already registered" in str(e)
    else:
        raise AssertionError("re-registering with different text must fail")

    store, _ = _store()
    store.record_prompt(a, created_ms=NOW)
    store.record_prompt(a, created_ms=NOW + 1)  # no-op
    assert store.get_prompt("research.system", 1) == a
    assert store.get_prompt("research.system", 2) is None


def test_module_level_registry_functions():
    ref = register("test.module_prompt", 1, "module level")
    assert ref.name == "test.module_prompt"
    assert len(prompts_hash()) == 16

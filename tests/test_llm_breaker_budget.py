from goldtrader.db import connect, migrate
from goldtrader.llm.breaker import CLOSED, HALF_OPEN, OPEN, CircuitBreaker
from goldtrader.llm.budget import Budget, day_start_ms, month_start_ms
from goldtrader.llm.provider import Usage
from goldtrader.llm.store import LLMStore

NOW = 1_762_300_000_000
H30 = 1800 * 1000


def _store() -> LLMStore:
    conn = connect(None)
    migrate(conn)
    return LLMStore(conn)


def test_breaker_opens_after_three_consecutive_failures_and_persists():
    store = _store()
    b = CircuitBreaker("research", store)
    b.record_failure(NOW)
    b.record_failure(NOW + 1)
    assert b.state == CLOSED and b.allows(NOW + 2)
    b.record_success(NOW + 3)  # resets the streak
    assert b.failures == 0
    for i in range(3):
        b.record_failure(NOW + 10 + i)
    assert b.state == OPEN and not b.allows(NOW + 20)
    assert b.next_trial_ms == NOW + 12 + H30
    assert [(e.from_state, e.to_state) for e in b.events] == [(CLOSED, OPEN)]
    saved = store.get_state("research")
    assert saved["state"] == OPEN and saved["failures"] == 3
    assert saved["next_trial_ms"] == b.next_trial_ms
    assert CircuitBreaker("research", store).state == OPEN


def test_half_open_trial_then_doubling_wait_up_to_six_hours():
    b = CircuitBreaker("research", None)
    for _ in range(3):
        b.record_failure(NOW)
    t = NOW + H30 - 1
    assert not b.allows(t)
    t += 1
    assert b.allows(t) and b.state == HALF_OPEN
    assert not b.allows(t + 1)  # only one trial while half-open
    b.record_failure(t, "still broken")
    assert b.state == OPEN and b.wait_s == 3600
    assert b.next_trial_ms == t + 3600 * 1000
    waits = [3600]
    for _ in range(6):
        t = b.next_trial_ms
        assert b.allows(t)
        b.record_failure(t)
        waits.append(b.wait_s)
    assert waits == [3600, 7200, 14400, 21600, 21600, 21600, 21600]
    t = b.next_trial_ms
    assert b.allows(t)
    b.record_success(t)
    assert b.state == CLOSED and b.failures == 0 and b.allows(t + 1)
    transitions = [(e.from_state, e.to_state) for e in b.events]
    assert transitions[0] == (CLOSED, OPEN)
    assert transitions[-1] == (HALF_OPEN, CLOSED)
    assert all(e.agent == "research" for e in b.events)


def test_breaker_without_store_works_in_memory():
    b = CircuitBreaker("x")
    assert b.allows(NOW)
    b.record_failure(NOW)
    assert b.failures == 1


def _spend(store: LLMStore, at_ms: int, usd: float) -> None:
    store.record_call(
        agent="a",
        run_id="r",
        model_id="claude-haiku-5-5",
        prompt_hash="p",
        schema_version=1,
        usage=Usage(10, 10),
        cost_usd=usd,
        latency_ms=5,
        status="ok",
        created_ms=at_ms,
    )


def test_budget_day_and_month_windows_are_utc():
    assert day_start_ms(NOW) == 1_762_300_800_000 - 86_400_000  # 2025-11-04T00:00Z
    assert month_start_ms(NOW) == 1_761_955_200_000  # 2025-11-01T00:00Z
    store = _store()
    day0 = day_start_ms(NOW)
    _spend(store, day0 - 1, 1.0)  # yesterday
    _spend(store, day0 + 1, 0.5)  # today
    _spend(store, month_start_ms(NOW) - 1, 100.0)  # last month
    budget = Budget(daily_usd=3.0, monthly_usd=60.0)
    assert budget.spent_today(store, NOW) == 0.5
    assert budget.spent_month(store, NOW) == 1.5
    assert budget.allows(store, NOW, estimated_cost=2.5)
    assert not budget.allows(store, NOW, estimated_cost=2.6)
    assert "daily" in budget.refusal(store, NOW, 2.6)
    _spend(store, day0 + 2, 2.5)
    assert budget.refusal(store, NOW) is None  # exactly 3.0 still fits
    _spend(store, day0 + 3, 0.01)
    assert "daily budget" in budget.refusal(store, NOW)


def test_monthly_budget_is_checked_too():
    store = _store()
    _spend(store, month_start_ms(NOW) + 1, 59.0)
    budget = Budget(daily_usd=3.0, monthly_usd=60.0)
    assert budget.allows(store, NOW, 1.0)
    assert "monthly" in budget.refusal(store, NOW, 1.5)

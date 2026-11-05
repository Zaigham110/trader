from goldtrader.brain.gates import GateInput, gate_entry
from goldtrader.brain.scheduler import Job, Scheduler
from goldtrader.config.loader import parse_config
from goldtrader.config.schema import StrategyConfig
from goldtrader.contracts import StrategyProposal
from goldtrader.gateway.market import LiveFeatures
from goldtrader.types import SessionState


def test_scheduler_boundaries_triggers_and_isolation():
    now = {"t": 1_000_000.0}
    calls = []
    s = Scheduler(clock=lambda: now["t"])
    s.add(Job("fast", lambda: calls.append("fast"), every_seconds=60))
    s.add(Job("boom", lambda: 1 / 0, every_seconds=60))
    s.add(Job("daily", lambda: calls.append("daily"), daily_at_utc="08:30"))
    s.add(Job("gated", lambda: calls.append("gated"), every_seconds=60, only_when=lambda: False))
    # 13:46 UTC on day one: the 08:30 daily job has not run today → due now; 'gated' is skipped
    assert s.tick() == ["fast", "boom", "daily"]
    assert calls == ["fast", "daily"] and s.jobs[1].failures == 1
    assert "ZeroDivisionError" in s.jobs[1].last_error
    assert s.tick() == []  # same minute → nothing due
    now["t"] += 60
    assert "fast" in s.tick()
    s.trigger("fast")
    assert s.tick() == ["fast"]
    # daily job: 1_000_000 s is 1970-01-12 13:46:40 UTC; move to next day 08:31
    now["t"] = 1_000_000 + (24 - 13) * 3600 - 46 * 60 - 40 + 8 * 3600 + 31 * 60
    ran = s.tick()
    assert "daily" in ran and calls.count("daily") == 2
    now["t"] += 60
    assert "daily" not in s.tick()


def _feat(**over):
    base = dict(
        time_s=0,
        atr=40.0,
        atr_pct=0.01,
        spread_pips=20.0,
        spread_regime="normal",
        regime="normal",
        trend_state=1,
        rv=0.2,
        rv_pctile=0.5,
        hour_utc=10,
    )
    base.update(over)
    return LiveFeatures(**base)


def _prop(**over):
    base = dict(
        action="enter",
        side="long",
        stop_atr=1.0,
        target_atr=1.5,
        confidence=0.8,
        thesis="Trend state flipped positive with normal spread and no event nearby.",
        invalidation="Close below the fast EMA.",
        horizon_minutes=120,
        agrees_with_signal=True,
    )
    base.update(over)
    return StrategyProposal(**base)


def _gi(prop, **over):
    base = dict(
        proposal=prop,
        proposal_id="p1",
        proposal_age_s=5.0,
        bundle_id="b1",
        signal_side=1,
        features=_feat(),
        session=SessionState.OPEN,
        entries_allowed=True,
        open_positions=0,
        risk_budget=80,
        bid=4000.0,
        ask=4000.2,
        now_ms=1_000,
    )
    base.update(over)
    return GateInput(**base)


CFG = parse_config({"owner": {"risk": {"max_volume_lots": 1.0}}})
STRAT = StrategyConfig(id="t", kind="trend_atr", enabled=True)


def test_gate_entry_builds_intent_with_atr_geometry():
    out = gate_entry(_gi(_prop()), CFG, STRAT)
    assert out.ok and out.intent is not None and out.intent.kind == "enter"
    assert out.intent.stop_price == 3960.2 and out.intent.target_price == 4060.2
    assert out.intent.source == "strategy_bot" and out.intent.bundle_id == "b1"


def test_gate_entry_rejections():
    assert gate_entry(_gi(_prop(action="wait")), CFG, STRAT).reason == "wait"
    assert gate_entry(_gi(_prop(), proposal_age_s=120), CFG, STRAT).reason == "proposal_stale"
    assert (
        gate_entry(_gi(_prop(), session=SessionState.NEWS_WINDOW), CFG, STRAT).reason
        == "session_news_window"
    )
    assert gate_entry(_gi(_prop(), open_positions=1), CFG, STRAT).reason == "max_positions"
    assert gate_entry(_gi(_prop(), risk_budget=0), CFG, STRAT).reason == "risk_budget_zero"
    assert gate_entry(_gi(_prop(confidence=0.3)), CFG, STRAT).reason.startswith("confidence")
    assert (
        gate_entry(_gi(_prop(side="short"), signal_side=1), CFG, STRAT).reason
        == "disagrees_with_signal"
    )
    disc = StrategyConfig(id="t", kind="trend_atr", allow_discretionary=True)
    assert gate_entry(_gi(_prop(side="short"), signal_side=1), CFG, disc).ok
    assert gate_entry(_gi(_prop(stop_atr=2.5)), CFG, STRAT).reason.startswith("stop_atr")
    assert gate_entry(_gi(_prop(target_atr=0.5)), CFG, STRAT).reason.startswith("reward_risk")
    assert (
        gate_entry(_gi(_prop(), features=_feat(spread_regime="wide")), CFG, STRAT).reason
        == "spread"
    )
    assert gate_entry(_gi(_prop(), features=None), CFG, STRAT).reason == "no_atr"
    ex = gate_entry(_gi(_prop(action="exit")), CFG, STRAT)
    assert ex.ok and ex.intent.kind == "exit"
    mv = gate_entry(_gi(_prop(action="move_stop", new_stop_price=3990.0)), CFG, STRAT)
    assert mv.ok and mv.intent.new_stop_price == 3990.0

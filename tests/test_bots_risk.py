"""Risk-advisor: hard rules force 0, the model can only lower, fallback keeps code in charge."""

from __future__ import annotations

import pytest

from goldtrader.bots.risk_advisor import (
    RiskBudget,
    build_risk_messages,
    consecutive_losses,
    hard_rules,
    risk_advisor_spec,
)
from goldtrader.llm.fake import FakeProvider
from goldtrader.llm.provider import ProviderError
from goldtrader.llm.runtime import run_agent
from tests.test_bots_helpers import NOW, cfg, ctx, db, features, state


def test_hard_rules_pass_through_min_of_advised():
    assert hard_rules(ctx(), 70) == (70, [])
    assert hard_rules(ctx(), 250) == (100, [])
    assert hard_rules(ctx(), -5) == (0, [])


@pytest.mark.parametrize(
    "over, reason",
    [
        ({"kill_switch": True}, "kill_switch"),
        ({"paused": True}, "paused"),
        ({"incident_open": True}, "incident_open"),
        ({"session": "NEWS_WINDOW"}, "session_news_window"),
        ({"entries_allowed": False}, "entries_not_allowed"),
        ({"tick_age_s": 30.0}, "stale_ticks"),
        ({"tick_age_s": None}, "stale_ticks"),
        ({"day_pnl": -200.0}, "daily_loss_limit"),  # 2% of 10 000
        ({"equity": 8_900.0}, "drawdown_limit"),  # 11% below balance
        ({"margin_level": 250.0}, "margin_level_low"),
        ({"spec_ok": False}, "spec_changed"),
        ({"connected": False}, "disconnected"),
        ({"trade_allowed": False}, "trade_not_allowed"),
    ],
)
def test_hard_rules_force_zero(over, reason):
    budget, reasons = hard_rules(ctx(gateway_state=state(**over)), 100)
    assert budget == 0 and reason in reasons


def test_hard_rules_spread_stale_and_news_window():
    assert hard_rules(ctx(features=features(spread_regime="wide")), 90)[1] == ["spread_regime_wide"]
    assert hard_rules(ctx(features=None), 90)[1] == ["spread_regime_unknown"]
    assert hard_rules(ctx(features=features(spread_pips=80.0)), 90)[1] == ["spread_above_cap"]
    assert hard_rules(ctx(gateway_state=None), 90) == (0, ["no_gateway_state"])
    assert "news_window" in hard_rules(ctx(minutes_to_event=10.0, next_event="NFP"), 90)[1]
    assert hard_rules(ctx(minutes_to_event=45.0, next_event="NFP"), 90) == (90, [])


def test_consecutive_losses_and_message_inputs():
    trades = [{"net_pnl": -1}, {"net_pnl": -2}, {"net_pnl": 3}, {"net_pnl": -1}]
    assert consecutive_losses(trades) == 2 and consecutive_losses([]) == 0
    text = build_risk_messages(ctx(recent_trades=trades, next_event="CPI", minutes_to_event=40))[0][
        "content"
    ]
    assert "Consecutive losses: 2" in text and "CPI in 40 min" in text
    assert "= -200.00 USD" in text  # daily limit in USD from balance


def test_run_model_can_only_lower():
    conn, store = db()
    fake = FakeProvider()
    spec = risk_advisor_spec(cfg())
    assert spec.tools == [] and spec.tier == "fast"
    fake.script("fast", [{"budget": 40, "reasons": ["vol elevated"]}])
    run = run_agent(spec, ctx(), fake, store, now_ms=NOW)
    assert run.status == "ok"
    assert run.finalized == RiskBudget(40, 40, ["vol elevated"], [], False)
    # model says 100 but the kill switch is on → 0
    fake.script("fast", [{"budget": 100, "reasons": []}])
    run = run_agent(spec, ctx(gateway_state=state(kill_switch=True)), fake, store, now_ms=NOW)
    assert run.finalized.budget == 0 and run.finalized.hard_rules == ["kill_switch"]


def test_run_fallback_is_100_degraded_but_hard_rules_still_apply():
    conn, store = db()
    fake = FakeProvider()
    spec = risk_advisor_spec(cfg())
    fake.script("fast", [ProviderError("down")])
    run = run_agent(spec, ctx(), fake, store, now_ms=NOW)
    assert run.status == "degraded" and run.output.budget == 100
    assert run.output.reasons[0].startswith("degraded")
    assert run.finalized.budget == 100 and run.finalized.degraded
    fake.script("fast", [ProviderError("down")])
    run = run_agent(spec, ctx(gateway_state=state(paused=True)), fake, store, now_ms=NOW)
    assert run.finalized.budget == 0 and run.finalized.hard_rules == ["paused"]

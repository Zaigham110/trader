"""Strategy bot: prompt inputs, structural checks, code overrides (agrees_with_signal, clips),
fallback; no tools (AGENTS §3.2)."""

from __future__ import annotations

from goldtrader.bots.strategy import (
    build_strategy_messages,
    check_proposal,
    normalise_proposal,
    recent_news,
    strategy_spec,
)
from goldtrader.contracts import StrategyProposal
from goldtrader.llm.fake import REFUSE, FakeProvider
from goldtrader.llm.runtime import run_agent
from tests.test_bots_helpers import GOOD_PROPOSAL, HOUR, NOW, STRAT, WAIT_PROPOSAL, cfg, ctx, db


def test_spec_shape_and_no_tools():
    spec = strategy_spec(cfg(), STRAT)
    assert spec.name == "strategy" and spec.tier == "standard" and spec.effort == "medium"
    assert spec.tools == [] and spec.output_model is StrategyProposal
    assert spec.timeout_s == 45.0 and spec.max_cost_usd == 0.10


def test_messages_carry_every_input_and_only_recent_news():
    c = ctx(
        news_events=[
            {"created_ms": NOW - 2 * HOUR, "summary": "CPI hot", "event_type": "inflation"},
            {"created_ms": NOW - 9 * HOUR, "summary": "stale item", "event_type": "fed"},
        ],
        recent_trades=[{"trade_id": f"t{i}", "net_pnl": -1.0} for i in range(12)],
        lessons=[f"lesson {i}" for i in range(7)],
        open_position={"ticket": 1, "side": "long", "profit": 3.2},
        next_event="FOMC",
        minutes_to_event=95,
    )
    text = build_strategy_messages(c, STRAT)[0]["content"]
    assert "Deterministic signal: long" in text and "Risk budget (code-decided, 0–100): 80" in text
    assert "CPI hot" in text and "stale item" not in text
    assert "t9" in text and "t10" not in text  # last 10 trades
    assert "lesson 4" in text and "lesson 5" not in text  # latest 5 lessons
    assert "ticket=1" in text and "FOMC in 95 min" in text
    assert "stated 0.72" in text and "stop_atr bounds [0.30, 1.50]" in text
    assert len(recent_news(c.news_events, NOW)) == 1


def test_check_requires_fields_per_action():
    assert check_proposal(StrategyProposal(**GOOD_PROPOSAL)) == []
    missing = StrategyProposal(**{**GOOD_PROPOSAL, "side": None, "stop_atr": None})
    probs = check_proposal(missing)
    assert any("side" in p for p in probs) and any("stop_atr" in p for p in probs)
    mv = StrategyProposal(**{**WAIT_PROPOSAL, "action": "move_stop"})
    assert check_proposal(mv) == ["action move_stop requires new_stop_price"]


def test_normalise_overrides_agreement_and_clips_bounds():
    c = ctx(signal_side=1)
    lying = StrategyProposal(**{**GOOD_PROPOSAL, "side": "short", "agrees_with_signal": True})
    fixed = normalise_proposal(lying, c, STRAT)
    assert fixed.agrees_with_signal is False
    honest = normalise_proposal(StrategyProposal(**GOOD_PROPOSAL), c, STRAT)
    assert honest.agrees_with_signal is True
    wide = StrategyProposal(**{**GOOD_PROPOSAL, "stop_atr": 3.0, "target_atr": 0.5})
    clipped = normalise_proposal(wide, c, STRAT)
    assert clipped.stop_atr == 1.5 and clipped.target_atr == 1.5  # ≥ stop × min_rr
    tight = StrategyProposal(**{**GOOD_PROPOSAL, "stop_atr": 0.1})
    assert normalise_proposal(tight, c, STRAT).stop_atr == 0.3
    waiting = normalise_proposal(StrategyProposal(**WAIT_PROPOSAL), ctx(signal_side=0), STRAT)
    assert waiting.agrees_with_signal is True


def test_run_happy_path_returns_finalized_proposal():
    conn, store = db()
    fake = FakeProvider()
    fake.script("standard", [{**GOOD_PROPOSAL, "side": "short", "stop_atr": 2.5}])
    run = run_agent(strategy_spec(cfg(), STRAT), ctx(signal_side=1), fake, store, now_ms=NOW)
    assert run.status == "ok"
    assert run.output.agrees_with_signal is True  # what the model said
    assert run.finalized.agrees_with_signal is False and run.finalized.stop_atr == 1.5  # code
    assert fake.requests[0].tools == []


def test_invalid_output_is_repaired_once():
    conn, store = db()
    fake = FakeProvider()
    fake.script("standard", [{**GOOD_PROPOSAL, "side": None}, GOOD_PROPOSAL])
    run = run_agent(strategy_spec(cfg(), STRAT), ctx(), fake, store, now_ms=NOW)
    assert run.status == "ok" and run.turns == 2
    assert "action enter requires side" in fake.requests[1].messages[-1]["content"]


def test_fallback_is_degraded_wait():
    conn, store = db()
    fake = FakeProvider()
    fake.script("standard", [REFUSE])
    run = run_agent(strategy_spec(cfg(), STRAT), ctx(signal_side=1), fake, store, now_ms=NOW)
    assert run.status == "degraded" and run.degraded
    assert run.output.action == "wait" and run.output.confidence == 0.0
    assert run.output.thesis.startswith("degraded: provider unavailable")
    assert run.finalized.action == "wait" and run.finalized.agrees_with_signal is False

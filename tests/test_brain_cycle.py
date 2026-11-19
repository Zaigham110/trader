"""DecisionCycle end to end with FakeProvider and a recording fake gateway (ARCHITECTURE §3.1)."""

from __future__ import annotations

import json

from goldtrader.bots.registry import BotRegistry
from goldtrader.brain.cycle import DecisionCycle
from goldtrader.contracts import GatewayState, Intent, IntentResult
from goldtrader.llm.fake import FakeProvider
from goldtrader.llm.provider import ProviderError
from tests.test_bots_helpers import (
    GOOD_PROPOSAL,
    NOW,
    STRAT,
    WAIT_PROPOSAL,
    cfg,
    db,
    features,
    news_items,
    state,
)

RISK_OK = {"budget": 80, "reasons": ["calm"]}


class FakeGateway:
    def __init__(self, st: GatewayState | None, result_status: str = "executed"):
        self.st = st
        self.result_status = result_status
        self.intents: list[Intent] = []

    def state(self) -> GatewayState | None:
        return self.st

    def send_intent(self, intent: Intent) -> IntentResult:
        self.intents.append(intent)
        return IntentResult(
            intent_id=intent.intent_id,
            status=self.result_status,
            risk_code=None if self.result_status == "executed" else "R_SPREAD",
            order_ticket=111,
            position_id=222,
            filled_price=4000.2,
            volume=0.01,
        )


def _cycle(gateway, *, provider="fake", signal=1, feats=None, **kw):
    conn, store = db()
    feats = feats if feats is not None else features()
    fake = FakeProvider() if provider == "fake" else None
    reg = BotRegistry(cfg(), fake, store, conn, strat=STRAT)
    clock = {"t": NOW}
    cyc = DecisionCycle(
        cfg(),
        STRAT,
        reg,
        gateway,
        conn,
        features_fn=lambda: feats,
        signal_fn=lambda: signal,
        news_fn=lambda: [],
        trades_fn=lambda: [],
        lessons_fn=lambda: reg.lessons(5),
        clock_ms=lambda: clock["t"],
        **kw,
    )
    return cyc, reg, fake, conn, clock


def _rows(conn, table):
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]


def test_happy_enter_sends_intent_and_journals_everything():
    gw = FakeGateway(state())
    cyc, reg, fake, conn, _ = _cycle(gw)
    fake.script("fast", [RISK_OK])
    fake.script("standard", [GOOD_PROPOSAL])
    res = cyc.run_once()
    assert res.status == "sent" and res.gate_reason == "enter" and res.risk_budget == 80
    assert res.intent_result.status == "executed" and len(gw.intents) == 1
    it = gw.intents[0]
    assert it.kind == "enter" and it.side == "long" and it.proposal_id == res.proposal_id
    assert it.stop_price == 3960.2 and it.target_price == 4060.2 and it.bundle_id == "t1"
    (prop,) = _rows(conn, "proposals")
    assert prop["proposal_id"] == res.proposal_id and prop["gate_status"] == "sent"
    assert prop["gate_reason"] == "executed" and prop["bot"] == "strategy"
    assert len(prop["inputs_hash"]) == 16 and json.loads(prop["raw_json"])["status"] == "ok"
    (intent_row,) = _rows(conn, "intents")
    assert intent_row["status"] == "executed" and intent_row["proposal_id"] == res.proposal_id
    assert json.loads(intent_row["result_json"])["order_ticket"] == 111
    (rb,) = _rows(conn, "risk_budgets")
    assert rb["advised"] == 80 and rb["budget"] == 80 and json.loads(rb["hard_rules_json"]) == []
    (cyc_row,) = _rows(conn, "brain_cycles")
    steps = [s["step"] for s in json.loads(cyc_row["steps_json"])]
    assert steps == [
        "gateway_state",
        "context",
        "risk_advisor",
        "strategy",
        "route",
        "gate",
        "intent",
    ]
    assert cyc_row["status"] == "sent" and cyc_row["proposal_id"] == res.proposal_id
    assert len(_rows(conn, "agent_runs")) == 2


def test_wait_sends_nothing():
    gw = FakeGateway(state())
    cyc, reg, fake, conn, _ = _cycle(gw, signal=0)
    fake.script("fast", [RISK_OK])
    fake.script("standard", [WAIT_PROPOSAL])
    res = cyc.run_once()
    assert res.status == "wait" and gw.intents == [] and res.intent_result is None
    (prop,) = _rows(conn, "proposals")
    assert prop["gate_status"] == "wait" and _rows(conn, "intents") == []


def test_invalid_output_is_repaired_then_sent():
    gw = FakeGateway(state())
    cyc, reg, fake, conn, _ = _cycle(gw)
    fake.script("fast", [RISK_OK])
    fake.script("standard", ["not json", GOOD_PROPOSAL])
    res = cyc.run_once()
    assert res.status == "sent" and len(gw.intents) == 1
    assert "invalid" in fake.requests[2].messages[-1]["content"].lower()
    run_row = conn.execute("SELECT * FROM agent_runs WHERE agent='strategy'").fetchone()
    assert run_row["turns"] == 2 and run_row["status"] == "ok"


def test_provider_failure_degrades_to_wait_without_intent():
    gw = FakeGateway(state())
    cyc, reg, fake, conn, _ = _cycle(gw)
    fake.script("fast", [RISK_OK])
    fake.script("standard", [ProviderError("down")])
    res = cyc.run_once()
    assert res.status == "wait" and gw.intents == []
    (prop,) = _rows(conn, "proposals")
    assert prop["bot"] == "strategy:degraded" and prop["action"] == "wait"
    assert "degraded" in prop["thesis"]
    assert reg.breakers["strategy"].failures == 1


def test_hard_rules_zero_budget_blocks_enter_even_when_model_says_enter():
    gw = FakeGateway(state(incident_open=True))
    cyc, reg, fake, conn, _ = _cycle(gw)
    fake.script("fast", [{"budget": 100, "reasons": []}])
    fake.script("standard", [GOOD_PROPOSAL])
    res = cyc.run_once()
    assert res.status == "gated" and res.gate_reason == "risk_budget_zero" and res.risk_budget == 0
    assert gw.intents == []
    (rb,) = _rows(conn, "risk_budgets")
    assert rb["advised"] == 100 and rb["budget"] == 0
    assert json.loads(rb["hard_rules_json"]) == ["incident_open"]
    (prop,) = _rows(conn, "proposals")
    assert prop["gate_status"] == "gated" and prop["gate_reason"] == "risk_budget_zero"


def test_gateway_unavailable_makes_no_bot_call():
    gw = FakeGateway(None)
    cyc, reg, fake, conn, _ = _cycle(gw)
    res = cyc.run_once()
    assert res.status == "gateway_unavailable" and fake.requests == []
    assert _rows(conn, "agent_runs") == [] and len(_rows(conn, "brain_cycles")) == 1


def test_kill_switch_and_pause_block_before_any_bot_call():
    for over in ({"kill_switch": True}, {"paused": True}):
        gw = FakeGateway(state(**over))
        cyc, reg, fake, conn, _ = _cycle(gw)
        res = cyc.run_once()
        assert res.status == "blocked" and res.gate_reason == next(iter(over))
        assert fake.requests == [] and gw.intents == []


def test_gateway_rejection_is_recorded_on_intent_and_proposal():
    gw = FakeGateway(state(), result_status="rejected")
    cyc, reg, fake, conn, _ = _cycle(gw)
    fake.script("fast", [RISK_OK])
    fake.script("standard", [GOOD_PROPOSAL])
    res = cyc.run_once()
    assert res.status == "sent" and res.intent_result.status == "rejected"
    (row,) = _rows(conn, "intents")
    assert row["status"] == "rejected" and row["risk_code"] == "R_SPREAD"
    assert _rows(conn, "proposals")[0]["gate_reason"] == "rejected"


def test_cycle_never_raises():
    gw = FakeGateway(state())
    cyc, reg, fake, conn, _ = _cycle(gw)

    def boom():
        raise RuntimeError("features exploded")

    cyc.features_fn = boom
    res = cyc.run_once()
    assert res.status == "error" and "features exploded" in res.gate_reason
    (row,) = _rows(conn, "brain_cycles")
    assert row["status"] == "error" and "features exploded" in row["steps_json"]


def test_no_provider_fails_flat_unless_signal_only_fallback():
    gw = FakeGateway(state())
    cyc, reg, fake, conn, _ = _cycle(gw, provider=None)
    res = cyc.run_once()
    assert res.status == "flat" and res.gate_reason == "no_provider" and gw.intents == []
    # risk-advisor ran as a degraded fallback (no provider) and hard rules still decided
    (rb,) = _rows(conn, "risk_budgets")
    assert rb["degraded"] == 1 and rb["budget"] == 100
    assert _rows(conn, "proposals") == []

    gw2 = FakeGateway(state())
    cyc2, reg2, _, conn2, _ = _cycle(gw2, provider=None, signal_only_fallback=True)
    res2 = cyc2.run_once()
    assert res2.status == "sent" and len(gw2.intents) == 1 and gw2.intents[0].side == "long"
    (prop,) = _rows(conn2, "proposals")
    assert prop["bot"] == "signal_only" and prop["gate_status"] == "sent"
    # flat signal → nothing even with the flag
    gw3 = FakeGateway(state())
    cyc3, *_ = _cycle(gw3, provider=None, signal=0, signal_only_fallback=True)
    assert cyc3.run_once().status == "flat" and gw3.intents == []


def test_open_breaker_skips_strategy_bot_and_fails_flat():
    gw = FakeGateway(state())
    cyc, reg, fake, conn, _ = _cycle(gw)
    b = reg.breakers["strategy"]
    for _ in range(3):
        b.record_failure(NOW - 1000, "test")
    assert b.state == "open"
    fake.script("fast", [RISK_OK])
    res = cyc.run_once()
    assert res.status == "flat" and res.gate_reason == "breaker_open"
    assert [r.tier for r in fake.requests] == ["fast"]  # the strategy tier was never called


def test_review_closed_trade_records_postmortem_and_lessons():
    gw = FakeGateway(state())
    cyc, reg, fake, conn, _ = _cycle(gw)
    trade = {
        "trade_id": "tr9",
        "side": "long",
        "volume": 0.01,
        "entry_ms": NOW - 3_600_000,
        "exit_ms": NOW - 60_000,
        "entry_price": 4000.0,
        "exit_price": 4010.0,
        "spread_cost": 0.2,
        "slippage_cost": 0.1,
        "commission": 0.0,
        "swap": 0.0,
    }
    fake.script(
        "standard",
        [
            {
                "outcome_class": "correct_win",
                "signal_pnl": 10.0,
                "timing_pnl": 0.0,
                "slippage": 0.1,
                "costs": 0.2,
                "lessons": [
                    "Trend entries after a pullback held",
                    "Trend entries after a pullback held!",
                ],
                "confidence": 0.6,
            }
        ],
    )
    run = cyc.review_closed_trade(trade)
    assert run.status == "ok"
    (pm,) = _rows(conn, "postmortems")
    assert (
        pm["trade_id"] == "tr9"
        and pm["signal_pnl"] == 10.0
        and pm["outcome_class"] == "correct_win"
    )
    lessons = _rows(conn, "lessons")
    assert len(lessons) == 1 and lessons[0]["postmortem_id"] == pm["id"]
    assert reg.lessons(5) == ["Trend entries after a pullback held"]


def test_registry_records_news_events_and_dropped():
    conn, store = db()
    fake = FakeProvider()
    reg = BotRegistry(cfg(), fake, store, conn)
    from goldtrader.bots.common import BotContext

    items = news_items(2)
    fake.script(
        "fast",
        [
            {
                "events": [
                    {
                        "item_id": "n1",
                        "relevance": "high",
                        "event_type": "fed",
                        "direction_for_gold": "bullish",
                        "magnitude": "medium",
                        "horizon": "hours",
                        "confidence": 0.8,
                        "summary": "Pause signalled",
                    }
                ],
                "dropped": ["n2: duplicate"],
            }
        ],
    )
    run = reg.run("research", BotContext(now_ms=NOW, cfg=cfg(), news_items=items))
    assert run.status == "ok"
    (ev,) = _rows(conn, "news_events")
    assert ev["item_id"] == "n1" and ev["relevance"] == "high" and ev["run_id"] == run.run_id
    (dr,) = _rows(conn, "news_dropped")
    assert dr["item_id"] == "n2" and dr["reason"] == "duplicate"
    prompts = {r["name"] for r in conn.execute("SELECT name FROM prompts")}
    assert "research.extract.system" in prompts and "strategy.system" in prompts


# ----------------------------------------------------------------- code-only demo trades (D-068)
LOW_CONF = {**GOOD_PROPOSAL, "confidence": 0.5}
WAIT_ON_SIGNAL = {**WAIT_PROPOSAL, "thesis": "The rule has been below break-even; waiting."}


def _probe_cycle(gw, per_day=2, gap=60, signal=1, **owner):
    cyc, reg, fake, conn, clock = _cycle(gw, signal=signal)
    cyc.cfg = cfg(
        owner={
            "risk": {"max_volume_lots": 1.0},
            "code_only_demo_trades_per_day": per_day,
            "code_only_min_gap_minutes": gap,
            **owner,
        }
    )
    return cyc, fake, conn, clock


def test_demo_sends_a_capped_number_of_rule_signals_at_once_before_the_agents_answer():
    gw = FakeGateway(state())
    cyc, fake, conn, clock = _probe_cycle(gw)
    fake.script("fast", [RISK_OK] * 6)
    fake.script("standard", [WAIT_ON_SIGNAL, LOW_CONF, WAIT_ON_SIGNAL, WAIT_ON_SIGNAL])
    res = cyc.run_once()
    assert res.status == "sent" and res.gate_reason == "code_only" and len(gw.intents) == 1
    assert res.intent_result.status == "executed"
    it = gw.intents[0]
    assert it.kind == "enter" and it.side == "long"
    assert (it.stop_price, it.target_price) == (3960.2, 4060.2)  # the rule's own 1.0 / 1.5 ATR
    props = {p["bot"]: p for p in _rows(conn, "proposals")}
    assert props["strategy"]["gate_status"] == "wait"  # the agent was still asked, and recorded
    assert props["strategy"]["proposal_id"] == res.proposal_id
    probe = props["code_only"]
    assert probe["gate_status"] == "sent" and probe["proposal_id"] == it.proposal_id
    assert "sent at once, before the agents answered" in probe["thesis"]
    assert "1 of 2 today" in probe["thesis"]
    steps = json.loads(_rows(conn, "brain_cycles")[0]["steps_json"])
    names = [s["step"] for s in steps]
    # the order goes out before any model is asked
    assert names == [
        "gateway_state", "context", "code_only", "intent", "risk_advisor", "strategy", "route", "gate",
    ]  # fmt: skip
    assert steps[3]["delay_ms"] == 0 and len(fake.requests) == 2
    # too soon after the last one: the gap holds even though the daily cap is not reached
    clock["t"] += 30 * 60_000
    assert cyc.run_once().status == "gated" and len(gw.intents) == 1
    clock["t"] += 31 * 60_000  # NOW is 23:46 UTC, so this is the next UTC day: the count restarts
    assert cyc.run_once().status == "sent" and len(gw.intents) == 2
    clock["t"] += 61 * 60_000
    assert cyc.run_once().status == "sent" and len(gw.intents) == 3  # 2 of 2 for the new day
    clock["t"] += 61 * 60_000
    fake.script("standard", [WAIT_ON_SIGNAL])
    assert cyc.run_once().status == "wait" and len(gw.intents) == 3  # daily cap reached


def test_code_only_never_overrides_a_code_rule_and_is_demo_only():
    for kwargs, st, signal, script in (
        ({"per_day": 0}, state(), 1, WAIT_ON_SIGNAL),  # switched off (the default)
        ({}, state(), 0, WAIT_PROPOSAL),  # no signal from the rule
        ({}, state(open_positions=1), 1, WAIT_ON_SIGNAL),  # a code gate still blocks it
        ({}, state(session="CLOSE_ONLY", entries_allowed=False), 1, WAIT_ON_SIGNAL),
        ({"mode": "shadow"}, state(), 1, WAIT_ON_SIGNAL),  # not a demo-mode system
    ):
        gw = FakeGateway(st)
        cyc, fake, conn, _ = _probe_cycle(gw, signal=signal, **kwargs)
        fake.script("fast", [RISK_OK])
        fake.script("standard", [script])
        res = cyc.run_once()
        assert gw.intents == [] and res.status in ("wait", "gated", "blocked"), kwargs
    # a wide spread blocks the real-time order like any other entry
    gw = FakeGateway(state())
    cyc, reg, fake, conn, _ = _cycle(gw, feats=features(spread_regime="wide"))
    cyc.cfg = cfg(owner={"risk": {"max_volume_lots": 1.0}, "code_only_demo_trades_per_day": 2})
    fake.script("fast", [RISK_OK])
    fake.script("standard", [WAIT_ON_SIGNAL])
    assert cyc.run_once().status == "wait" and gw.intents == []
    why = [p["gate_reason"] for p in _rows(conn, "proposals") if p["bot"] == "code_only"]
    # refused at once, and again after the agent waited: the code rules set the budget to zero
    assert len(why) == 2 and set(why) <= {"risk_budget_zero", "spread"}
    # the order follows the rule's signal: an agent arguing the other way changes nothing
    gw = FakeGateway(state())
    cyc, fake, conn, _ = _probe_cycle(gw, signal=-1)
    fake.script("fast", [RISK_OK])
    fake.script("standard", [GOOD_PROPOSAL])  # proposes long against a short signal
    res = cyc.run_once()
    assert res.status == "sent" and [i.side for i in gw.intents] == ["short"]
    agent = [p for p in _rows(conn, "proposals") if p["bot"] == "strategy"][0]
    assert agent["gate_reason"] == "disagrees_with_signal"  # its own entry is still refused


def test_stop_and_target_are_placed_around_the_price_at_send_time_not_at_cycle_start():
    class Moving(FakeGateway):
        def __init__(self):
            super().__init__(state())
            self.calls = 0

        def state(self):
            self.calls += 1
            # the agents take seconds to answer; by then the market has fallen 10 USD
            return state() if self.calls == 1 else state(bid=3990.0, ask=3990.2)

    gw = Moving()
    cyc, reg, fake, conn, _ = _cycle(gw)
    fake.script("fast", [RISK_OK])
    fake.script("standard", [GOOD_PROPOSAL])
    res = cyc.run_once()
    assert res.status == "sent" and gw.calls == 2
    it = gw.intents[0]
    assert (it.stop_price, it.target_price) == (3950.2, 4050.2)  # around 3990.2, not 4000.2

    class Gone(FakeGateway):
        def __init__(self):
            super().__init__(state())
            self.calls = 0

        def state(self):
            self.calls += 1
            return state() if self.calls == 1 else None

    gw2 = Gone()
    cyc2, reg2, fake2, conn2, _ = _cycle(gw2)
    fake2.script("fast", [RISK_OK])
    fake2.script("standard", [GOOD_PROPOSAL])
    res2 = cyc2.run_once()
    assert res2.status == "gateway_unavailable" and gw2.intents == []  # never send blind
    assert _rows(conn2, "proposals")[0]["gate_status"] == "gated"


def test_the_demo_config_as_shipped_can_place_its_own_trade():
    """The rule's configured stop and target must fit every schema on the way to an order.
    D-073 widened them past an old proposal limit and every attempt failed until this was fixed."""
    from goldtrader.config import load_config

    demo = load_config("config/demo.yaml")
    strat = next(s for s in demo.strategies if s.enabled)
    assert demo.owner.code_only_demo_trades_per_day > 0
    gw = FakeGateway(state())
    cyc, reg, fake, conn, _ = _cycle(gw, feats=features(atr=2.0))
    cyc.cfg, cyc.strat = demo, strat
    fake.script("fast", [RISK_OK])
    fake.script("standard", [WAIT_ON_SIGNAL])
    res = cyc.run_once()
    assert res.status == "sent" and res.gate_reason == "code_only", res.gate_reason
    it = gw.intents[0]
    stop, target = float(strat.params["stop_atr"]), float(strat.params["target_atr"])
    assert stop <= demo.owner.risk.stop_atr_max
    assert it.stop_price == round(4000.2 - stop * 2.0, 2)
    assert it.target_price == round(4000.2 + target * 2.0, 2)
    assert len(gw.intents) == 1  # one decision, one entry, even though the agent then waited
    # and a second signal while that position is open is refused by the one-position rule
    gw.st = state(open_positions=1)
    fake.script("fast", [RISK_OK])
    fake.script("standard", [WAIT_ON_SIGNAL])
    assert cyc.run_once().status == "wait" and len(gw.intents) == 1


def test_one_decision_never_sends_two_entries_even_if_the_agent_also_wants_in():
    gw = FakeGateway(state())  # a gateway whose state still shows no position after the order
    cyc, fake, conn, _ = _probe_cycle(gw, per_day=100, gap=0)
    fake.script("fast", [RISK_OK])
    fake.script("standard", [GOOD_PROPOSAL])  # the agent proposes the same entry at 0.8
    res = cyc.run_once()
    assert res.status == "sent" and res.gate_reason == "code_only" and len(gw.intents) == 1
    agent = [p for p in _rows(conn, "proposals") if p["bot"] == "strategy"][0]
    assert (agent["gate_status"], agent["gate_reason"]) == ("gated", "already_entered")

"""Shared fixtures for the bot tests (no tests here)."""

from __future__ import annotations

import sqlite3

from goldtrader.bots.common import BotContext
from goldtrader.config.loader import parse_config
from goldtrader.config.schema import Config, StrategyConfig
from goldtrader.contracts import GatewayState
from goldtrader.db import connect, migrate
from goldtrader.gateway.market import LiveFeatures
from goldtrader.llm.store import LLMStore

NOW = 1_762_300_000_000  # 2025-11-04 23:46:40 UTC
HOUR = 3_600_000

GOOD_PROPOSAL = {
    "action": "enter",
    "side": "long",
    "stop_atr": 1.0,
    "target_atr": 1.5,
    "new_stop_price": None,
    "confidence": 0.8,
    "thesis": "Trend state positive, spread normal, no event within the horizon.",
    "invalidation": "Close below the fast EMA.",
    "horizon_minutes": 120,
    "agrees_with_signal": True,
}
WAIT_PROPOSAL = {
    **GOOD_PROPOSAL,
    "action": "wait",
    "side": None,
    "stop_atr": None,
    "target_atr": None,
    "confidence": 0.4,
    "thesis": "Nothing to do: signal is flat and spread is widening into the fix.",
}


def cfg(**over) -> Config:
    raw = {
        "owner": {"risk": {"max_volume_lots": 1.0}},
        "strategies": [{"id": "t1", "kind": "trend_atr", "enabled": True}],
    }
    raw.update(over)
    return parse_config(raw)


STRAT = StrategyConfig(id="t1", kind="trend_atr", enabled=True)


def db() -> tuple[sqlite3.Connection, LLMStore]:
    conn = connect(None)
    assert migrate(conn) >= 5
    return conn, LLMStore(conn)


def features(**over) -> LiveFeatures:
    base = dict(
        time_s=NOW // 1000 - 60,
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


def state(**over) -> GatewayState:
    base = dict(
        time_ms=NOW,
        connected=True,
        trade_allowed=True,
        session="OPEN",
        entries_allowed=True,
        bid=4000.0,
        ask=4000.2,
        spread_pips=20.0,
        atr=40.0,
        tick_age_s=0.5,
        equity=10_000.0,
        balance=10_000.0,
        margin_free=9_900.0,
        margin_level=1500.0,
        open_positions=0,
        kill_switch=False,
        paused=False,
        owner_hash="abcd",
        spec_ok=True,
        incident_open=False,
        day_pnl=0.0,
        positions=[],
    )
    base.update(over)
    return GatewayState(**base)


def ctx(config: Config | None = None, **over) -> BotContext:
    base = dict(
        now_ms=NOW,
        cfg=config or cfg(),
        features=features(),
        gateway_state=state(),
        signal_side=1,
        recent_trades=[],
        lessons=[],
        news_events=[],
        open_position=None,
        calibration_line="last 20 entries: stated 0.72, hit 0.55",
        risk_budget=80,
    )
    base.update(over)
    return BotContext(**base)


def news_items(n: int = 2, **over) -> list[dict]:
    items = []
    for i in range(1, n + 1):
        items.append(
            {
                "id": f"n{i}",
                "source": "reuters",
                "title": f"Fed official hints at pause {i}",
                "published_ms": NOW - 30 * 60_000,
                "body": "Officials signalled rates may stay on hold through year end.",
                **over,
            }
        )
    return items

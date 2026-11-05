"""Shared fixtures for the chat tests (no tests here)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from goldtrader.chat.actions import ActionContext
from goldtrader.chat.tools import ToolContext
from goldtrader.config.loader import load_config, parse_config
from goldtrader.config.schema import Config
from goldtrader.contracts import GatewayState, Intent, IntentResult
from goldtrader.db import connect, migrate
from goldtrader.gateway.market import LiveFeatures
from goldtrader.llm.store import LLMStore
from goldtrader.notify.confirm import ConfirmStore

NOW = 1_762_300_000_000  # 2025-11-04 23:46:40 UTC
HOUR = 3_600_000
DAY = 24 * HOUR
OWNER = 777_001
STRANGER = 999_002


class Clock:
    def __init__(self, now: int = NOW) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now

    def advance(self, ms: int) -> int:
        self.now += ms
        return self.now


def cfg(**over: Any) -> Config:
    raw: dict[str, Any] = {
        "owner": {"telegram_owner_chat_id": OWNER},
        "strategies": [{"id": "t1", "kind": "trend_atr", "enabled": True}],
    }
    raw.update(over)
    return parse_config(raw)


def write_cfg(path: Path, **over: Any) -> Config:
    raw: dict[str, Any] = {
        "version": 1,
        "session": {"pre_break_buffer_min": 15},
        "owner": {"telegram_owner_chat_id": OWNER, "risk": {"daily_loss_pct": 2.0}},
    }
    raw.update(over)
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return load_config(path)


def db() -> tuple[sqlite3.Connection, LLMStore]:
    conn = connect(None)
    assert migrate(conn) >= 6
    return conn, LLMStore(conn)


def state(**over: Any) -> GatewayState:
    base: dict[str, Any] = dict(
        time_ms=NOW - 1000,
        connected=True,
        trade_allowed=True,
        session="OPEN",
        entries_allowed=True,
        bid=4000.0,
        ask=4000.2,
        spread_pips=20.0,
        atr=40.0,
        tick_age_s=0.5,
        equity=5_120.5,
        balance=5_000.0,
        margin_free=4_900.0,
        margin_level=1500.0,
        open_positions=1,
        kill_switch=False,
        paused=False,
        owner_hash="abcd",
        spec_ok=True,
        incident_open=False,
        day_pnl=20.5,
        positions=[
            {
                "ticket": 4242,
                "side": "long",
                "volume": 0.01,
                "price_open": 3990.0,
                "sl": 3950.0,
                "tp": 4050.0,
                "profit": 10.0,
            }
        ],
    )
    base.update(over)
    return GatewayState(**base)


def features(**over: Any) -> LiveFeatures:
    base: dict[str, Any] = dict(
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


@dataclass
class FakeGateway:
    """Records intents; `state()` / `health()` return canned data. `down=True` → unreachable."""

    st: GatewayState | None = field(default_factory=state)
    intents: list[Intent] = field(default_factory=list)
    result_status: str = "executed"
    down: bool = False

    def state(self) -> GatewayState | None:
        return None if self.down else self.st

    def health(self) -> dict[str, Any]:
        if self.down:
            return {"error": "unreachable"}
        return {"started": True, "steps": 10, "errors": 0, "kill_switch": False}

    def send_intent(self, intent: Intent) -> IntentResult:
        self.intents.append(intent)
        return IntentResult(
            intent_id=intent.intent_id, status=self.result_status, detail="ok by fake gateway"
        )


def tool_ctx(conn: sqlite3.Connection, gw: FakeGateway, clock: Clock, config: Config | None = None):
    return ToolContext(conn, gw, features, config or cfg(), clock)


def action_ctx(
    conn: sqlite3.Connection,
    gw: FakeGateway,
    clock: Clock,
    config: Config | None = None,
    config_path: Path | None = None,
) -> ActionContext:
    return ActionContext(
        conn, gw, config or cfg(), clock, ConfirmStore(conn), config_path, ttl_s=120
    )


def insert_trade(
    conn: sqlite3.Connection,
    trade_id: str,
    net: float,
    *,
    exit_ms: int,
    spread: float = 0.2,
    slip: float = 0.1,
    commission: float = 0.0,
    swap: float = 0.0,
    r: float | None = None,
    position_id: int | None = None,
    proposal_id: str | None = None,
) -> None:
    gross = net - commission - swap
    conn.execute(
        "INSERT INTO trades(trade_id, position_id, bundle_id, proposal_id, side, volume, entry_ms, "
        "exit_ms, entry_price, exit_price, gross_pnl, spread_cost, slippage_cost, commission, swap, "
        "net_pnl, r_multiple, exit_reason, risk_pct_of_equity) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            trade_id,
            position_id,
            "b1",
            proposal_id,
            "long",
            0.01,
            exit_ms - HOUR,
            exit_ms,
            4000.0,
            4000.0 + gross,
            gross,
            spread,
            slip,
            commission,
            swap,
            net,
            r,
            "tp",
            0.5,
        ),
    )

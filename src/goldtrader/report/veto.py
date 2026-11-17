"""Do the AI agents' vetoes add value? (D-060)

Every decision cycle stores what it knew (`decision_snapshots`). For each cycle where the
deterministic signal wanted a trade, this report asks what happened to that signal:

  * `taken`      - the Strategy agent agreed, the code gates passed, an order went out;
  * `ai_veto`    - the Strategy agent said wait, or its confidence was below the threshold;
  * `code_block` - a code rule stopped it (session, spread, risk budget, news window, ...);
  * `ai_down`    - the agent was unavailable, so nothing was proposed.

and then plays the signal forward on the stored 1-minute bars as code alone would have traded it
(entry at the next bar's open on the real bid/ask, the strategy's stop and target, flattened at the
session break), giving a result in R (multiples of the amount risked).

If the vetoed signals would have lost on average, the vetoes are earning their cost; if they would
have won, the agent is filtering out good trades. With few signals the answer is noise, and the
report says how many it rests on.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from goldtrader.config.schema import Config, StrategyConfig
from goldtrader.db.repo import Repo
from goldtrader.session.state import SessionClock
from goldtrader.types import Bar, SessionState

REPORT_NAME = "veto"
MAX_HOLD_S = 2 * 86400
FLATTEN = frozenset(
    {
        SessionState.PRE_BREAK_BUFFER,
        SessionState.CLOSE_ONLY,
        SessionState.CLOSED,
        SessionState.STALE,
    }
)
AI_REASONS = ("wait", "confidence", "disagrees")
CLASSES = ("taken", "ai_veto", "code_block", "ai_down")
MIN_FOR_VERDICT = 30


def classify(status: str, gate_reason: str | None) -> str:
    reason = (gate_reason or "").lower()
    if status == "sent":
        return "taken"
    if status == "flat":
        return "ai_down"
    if status == "wait" or reason.startswith(AI_REASONS):
        return "ai_veto"
    return "code_block"


def simulate(
    side: int,
    at_s: int,
    atr: float | None,
    bars: list[Bar],
    stop_atr: float,
    target_atr: float,
    clock: SessionClock,
    events: list,
) -> dict[str, Any] | None:
    """Code-only outcome of a signal seen at `at_s`. None when it cannot be resolved yet."""
    if not atr or atr <= 0 or side == 0:
        return None
    later = [b for b in bars if at_s < b.time <= at_s + MAX_HOLD_S]
    if not later:
        return None
    first = later[0]
    if first.time - at_s > 600:  # the market was not trading right after the signal
        return None
    long_ = side > 0
    entry = first.ask_open if long_ else first.bid_open
    dist = stop_atr * atr
    sl = entry - dist if long_ else entry + dist
    tp = entry + target_atr * atr if long_ else entry - target_atr * atr

    def done(price: float, why: str, b: Bar) -> dict[str, Any]:
        r = ((price - entry) if long_ else (entry - price)) / dist
        return {"r": round(r, 3), "exit": why, "exit_s": b.time, "entry": entry}

    for i, b in enumerate(later):
        state = clock.state_at(datetime.fromtimestamp(b.time, UTC), events=events).state
        if state in FLATTEN:
            if i == 0:
                return None  # no entry was possible
            return done(b.bid_open if long_ else b.ask_open, "session", b)
        if long_:
            if b.bid_low <= sl:
                return done(min(sl, b.bid_open), "stop", b)
            if b.bid_high >= tp:
                return done(tp, "target", b)
        else:
            if b.ask_high >= sl:
                return done(max(sl, b.ask_open), "stop", b)
            if b.ask_low <= tp:
                return done(tp, "target", b)
    return None


def build(
    conn: sqlite3.Connection, cfg: Config, strat: StrategyConfig, now_ms: int
) -> dict[str, Any]:
    r = cfg.owner.risk
    stop_atr = min(max(float(strat.params.get("stop_atr", 1.0)), r.stop_atr_min), r.stop_atr_max)
    target_atr = max(float(strat.params.get("target_atr", 1.5)), stop_atr * r.min_reward_risk)
    rows = conn.execute(
        "SELECT s.cycle_id, s.at_ms, s.signal_side, s.atr, c.status, c.gate_reason "
        "FROM decision_snapshots s JOIN brain_cycles c ON c.cycle_id = s.cycle_id "
        "WHERE s.signal_side != 0 AND c.status != 'running' ORDER BY s.at_ms"
    ).fetchall()
    total = conn.execute("SELECT COUNT(*), MIN(at_ms) FROM decision_snapshots").fetchone()
    stats = {k: {"n": 0, "resolved": 0, "wins": 0, "sum_r": 0.0} for k in CLASSES}
    cases: list[dict[str, Any]] = []
    if rows:
        repo = Repo(conn)
        start_s = int(rows[0]["at_ms"]) // 1000
        bars = repo.load_bars(cfg.account.symbol, 60, start_s, int(now_ms // 1000) + 60)
        try:
            from goldtrader.feeds.calendar_store import CalendarStore

            events = CalendarStore(conn).between(start_s * 1000 - 86_400_000, now_ms + 86_400_000)
        except Exception:  # noqa: BLE001
            events = []
        clock = SessionClock(cfg.session)
        for row in rows:
            cls = classify(row["status"], row["gate_reason"])
            st = stats[cls]
            st["n"] += 1
            out = simulate(
                int(row["signal_side"]),
                int(row["at_ms"]) // 1000,
                row["atr"],
                bars,
                stop_atr,
                target_atr,
                clock,
                events,
            )
            if out is not None:
                st["resolved"] += 1
                st["sum_r"] += out["r"]
                st["wins"] += 1 if out["r"] > 0 else 0
            cases.append(
                {
                    "at_ms": int(row["at_ms"]),
                    "side": "long" if row["signal_side"] > 0 else "short",
                    "what": cls,
                    "reason": row["gate_reason"] or row["status"],
                    "r": out["r"] if out else None,
                    "exit": out["exit"] if out else None,
                }
            )
    for st in stats.values():
        st["sum_r"] = round(st["sum_r"], 3)
        st["mean_r"] = round(st["sum_r"] / st["resolved"], 3) if st["resolved"] else None
    veto = stats["ai_veto"]
    if veto["resolved"] == 0:
        verdict = "No vetoed signal has played out yet."
    else:
        saved = -veto["sum_r"]
        verdict = (
            f"The {veto['resolved']} vetoed signals would have made {veto['sum_r']:+.2f} R in total "
            f"as code-only trades, so the vetoes {'saved' if saved >= 0 else 'cost'} "
            f"{abs(saved):.2f} R."
        )
        if veto["resolved"] < MIN_FOR_VERDICT:
            verdict += f" Too few to judge (under {MIN_FOR_VERDICT})."
    return {
        "at_ms": now_ms,
        "strategy": strat.id,
        "stop_atr": stop_atr,
        "target_atr": target_atr,
        "decisions_recorded": int(total[0] or 0),
        "since_ms": total[1],
        "signals": len(rows),
        "classes": stats,
        "veto_value_r": round(-veto["sum_r"], 3) if veto["resolved"] else None,
        "verdict": verdict,
        "cases": cases[-30:][::-1],
    }


def store(conn: sqlite3.Connection, name: str, body: dict[str, Any], now_ms: int) -> None:
    conn.execute(
        "INSERT INTO stored_reports(name, at_ms, body_json) VALUES (?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET at_ms=excluded.at_ms, body_json=excluded.body_json",
        (name, now_ms, json.dumps(body)),
    )

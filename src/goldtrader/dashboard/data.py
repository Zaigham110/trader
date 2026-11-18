"""Read model for the dashboard: plain functions from a SQLite connection to JSON-ready dicts.

Everything here is a SELECT. No secrets and no config are returned. Telegram chat text is not
returned (only counts); `chat()` returns only the conversation typed into the dashboard itself
(chat id 0), which is stored redacted.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

STALE_AFTER_S = 60


# The same three helpers exist in `chat/tools.py`, but importing that module would pull gateway
# code into this process; the dashboard must import nothing that can reach the broker.
def rows(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    q = "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?"
    return conn.execute(q, (name,)).fetchone() is not None


def period_start_ms(period: str, now_ms: int) -> int:
    """UTC start of ``today`` or of this ISO ``week`` (Monday)."""
    day = datetime.fromtimestamp(now_ms / 1000, tz=UTC).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    if period == "week":
        day -= timedelta(days=day.weekday())
    return int(day.timestamp() * 1000)


RUNNING_WINDOW_MS = 180_000  # an unfinished run older than this is treated as abandoned

# (key in agent_runs / llm_calls, label, role). Order is the order of the cards.
LLM_AGENTS: tuple[tuple[str, str, str], ...] = (
    ("strategy", "Strategy", "Proposes enter / exit / wait each cycle"),
    ("risk_advisor", "Risk advisor", "Advises a risk budget (0-100) each cycle"),
    ("research", "Research", "Reads news and extracts gold-relevant events"),
    ("weekly_research", "Weekly research", "Proposes hypotheses to test"),
    ("review", "Review", "Post-mortem after each closed trade"),
    ("daily_brief", "Daily brief", "Writes the daily and weekly brief"),
    ("chat", "Chat", "Answers the owner here and on Telegram"),
)
CODE_AGENTS: tuple[tuple[str, str, str], ...] = (
    ("execution", "Execution", "Deterministic: the only component that reaches the broker"),
    ("risk_engine", "Risk engine", "Deterministic: hard limits on every order"),
    ("health", "Health", "Deterministic: heartbeats, alerts, incidents"),
    (
        "telegram_signals",
        "Telegram signals",
        "Reads the channels you follow, alerts you and shows fresh signals to Strategy",
    ),
)


def _loads(text: Any, default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _clip(text: Any, n: int = 400) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _one(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> dict[str, Any] | None:
    r = conn.execute(sql, args).fetchone()
    return dict(r) if r is not None else None


def summarise_output(agent: str, out: Any) -> str:
    """One or two sentences in words for an agent's last output (`agent_runs.output_json`)."""
    if not isinstance(out, dict):
        return ""
    if agent == "strategy":
        head = str(out.get("action") or "?").upper()
        if out.get("side"):
            head += f" {out['side']}"
        if out.get("confidence") is not None:
            head += f" (confidence {out['confidence']})"
        return _clip(f"{head}: {out.get('thesis') or ''}")
    if agent == "risk_advisor":
        reasons = out.get("reasons") or []
        return _clip(f"Budget {out.get('budget')}: " + "; ".join(str(r) for r in reasons))
    if agent == "research":
        events = out.get("events") or []
        if not events:
            return f"No relevant events; {len(out.get('dropped') or [])} items dropped"
        return _clip(
            f"{len(events)} event(s): "
            + "; ".join(str(e.get("summary") or "") for e in events if isinstance(e, dict))
        )
    if agent == "weekly_research":
        hyps = out.get("hypotheses") or []
        return _clip(
            f"{len(hyps)} hypothesis(es): "
            + "; ".join(str(h.get("title") or "") for h in hyps if isinstance(h, dict))
        )
    if agent == "review":
        lessons = out.get("lessons") or []
        return _clip(f"{out.get('outcome_class') or '?'}: " + "; ".join(str(x) for x in lessons))
    if agent == "chat":
        return "Answered the owner"  # the text itself stays in the chat
    for key in ("summary", "thesis", "text", "headline"):
        if out.get(key):
            return _clip(out[key])
    return ""


# --------------------------------------------------------------------------------------- parts
def processes(conn: sqlite3.Connection, now_ms: int) -> list[dict[str, Any]]:
    out = rows(conn.execute("SELECT process, last_ms, status FROM heartbeats ORDER BY process"))
    for h in out:
        h["age_s"] = round((now_ms - int(h["last_ms"])) / 1000, 1)
        h["stale"] = h["age_s"] > STALE_AFTER_S
    return out


def agents(conn: sqlite3.Connection, now_ms: int) -> list[dict[str, Any]]:
    day = period_start_ms("today", now_ms)
    breakers = {
        r["agent"]: r
        for r in rows(
            conn.execute("SELECT agent, state, failures, next_trial_ms FROM breaker_state")
        )
    }
    today = {
        r["agent"]: r
        for r in rows(
            conn.execute(
                "SELECT agent, COUNT(*) AS calls, ROUND(COALESCE(SUM(cost_usd),0), 4) AS cost_usd, "
                "SUM(CASE WHEN status='ok' THEN 0 ELSE 1 END) AS failed "
                "FROM llm_calls WHERE created_ms >= ? GROUP BY agent",
                (day,),
            )
        )
    }
    out: list[dict[str, Any]] = []
    for key, label, role in LLM_AGENTS:
        last = _one(
            conn,
            "SELECT run_id, started_ms, finished_ms, status, turns, cost_usd, degraded, "
            "failure_reason, output_json FROM agent_runs WHERE agent=? "
            "ORDER BY started_ms DESC LIMIT 1",
            (key,),
        )
        model = _one(
            conn, "SELECT model_id FROM llm_calls WHERE agent=? ORDER BY id DESC LIMIT 1", (key,)
        )
        running = False
        if last is not None:
            output = _loads(last.pop("output_json"), None)
            last["summary"] = summarise_output(key, output)
            last["failure_reason"] = _clip(last["failure_reason"], 200) or None
            last["degraded"] = bool(last["degraded"])
            running = (
                last["finished_ms"] is None and now_ms - last["started_ms"] < RUNNING_WINDOW_MS
            )
        t = today.get(key, {})
        b = breakers.get(key)
        out.append(
            {
                "key": key,
                "label": label,
                "role": role,
                "kind": "llm",
                "model": model["model_id"] if model else None,
                "running": running,
                "last": last,
                "breaker": b["state"] if b else "closed",
                "breaker_failures": b["failures"] if b else 0,
                "calls_today": int(t.get("calls") or 0),
                "failed_today": int(t.get("failed") or 0),
                "cost_today": float(t.get("cost_usd") or 0.0),
            }
        )
    out.extend(_code_agents(conn, now_ms, day))
    return out


def _code_agents(conn: sqlite3.Connection, now_ms: int, day: int) -> list[dict[str, Any]]:
    hb = {p["process"]: p for p in processes(conn, now_ms)}
    gw = hb.get("gateway")
    attempt = _one(
        conn,
        "SELECT sent_ms, kind, stage, retcode, risk_code FROM order_attempts "
        "ORDER BY sent_ms DESC LIMIT 1",
    )
    n_orders = conn.execute(
        "SELECT COUNT(*) FROM order_attempts WHERE stage='send' AND sent_ms >= ?", (day,)
    ).fetchone()[0]
    reject = _one(
        conn,
        "SELECT sent_ms, risk_code FROM order_attempts WHERE stage='risk' AND risk_code IS NOT NULL "
        "ORDER BY sent_ms DESC LIMIT 1",
    )
    n_rejects = conn.execute(
        "SELECT COUNT(*) FROM order_attempts WHERE stage='risk' AND risk_code IS NOT NULL "
        "AND sent_ms >= ?",
        (day,),
    ).fetchone()[0]
    n_alerts = conn.execute("SELECT COUNT(*) FROM alerts WHERE created_ms >= ?", (day,)).fetchone()[
        0
    ]
    n_inc = conn.execute("SELECT COUNT(*) FROM incidents WHERE closed_ms IS NULL").fetchone()[0]
    gw_ok = bool(gw) and not gw["stale"]
    info = {
        "execution": (
            gw_ok,
            attempt["sent_ms"] if attempt else (gw["last_ms"] if gw else None),
            f"{n_orders} order(s) sent today"
            + (f"; last: {attempt['kind']} ({attempt['stage']})" if attempt else ""),
        ),
        "risk_engine": (
            gw_ok,
            reject["sent_ms"] if reject else None,
            f"{n_rejects} rejection(s) today"
            + (f"; last reason: {reject['risk_code']}" if reject else ""),
        ),
        "health": (
            bool(hb)
            and not any(p["stale"] for k, p in hb.items() if k != "signals")  # optional
            and n_inc == 0,
            max((p["last_ms"] for p in hb.values()), default=None),
            f"{n_inc} open incident(s); {n_alerts} alert(s) today",
        ),
    }
    sg = hb.get("signals")
    n_sig, last_sig = (0, None)
    if table_exists(conn, "signals"):
        n_sig, last_sig = conn.execute(
            "SELECT COUNT(*), MAX(posted_ms) FROM signals WHERE posted_ms >= ?", (day,)
        ).fetchone()
    info["telegram_signals"] = (
        bool(sg) and not sg["stale"],
        last_sig or (sg["last_ms"] if sg else None),
        f"{n_sig} signal(s) read today" if sg else "not set up yet (goldctl signals-login)",
    )
    out = []
    for key, label, role in CODE_AGENTS:
        ok, at, summary = info[key]
        out.append(
            {
                "key": key,
                "label": label,
                "role": role,
                "kind": "code",
                "model": None,
                "running": ok,
                "last": {"started_ms": at, "finished_ms": at, "status": "ok" if ok else "down"}
                | {"summary": summary, "degraded": False, "failure_reason": None},
                "breaker": None,
                "breaker_failures": 0,
                "calls_today": 0,
                "failed_today": 0,
                "cost_today": 0.0,
            }
        )
    return out


def cycles(conn: sqlite3.Connection, limit: int = 30) -> list[dict[str, Any]]:
    """Latest decision cycles, newest first; the first one carries its steps."""
    out = rows(
        conn.execute(
            "SELECT cycle_id, started_ms, finished_ms, status, proposal_id, gate_reason, "
            "risk_budget, steps_json FROM brain_cycles ORDER BY started_ms DESC LIMIT ?",
            (limit,),
        )
    )
    for i, c in enumerate(out):
        steps = _loads(c.pop("steps_json"), [])
        if i == 0:
            c["steps"] = steps if isinstance(steps, list) else []
    return out


def proposals(conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    out = rows(
        conn.execute(
            "SELECT proposal_id, created_ms, bot, action, side, stop_atr, target_atr, confidence, "
            "thesis, invalidation, agrees_with_signal, gate_status, gate_reason FROM proposals "
            "ORDER BY created_ms DESC LIMIT ?",
            (limit,),
        )
    )
    for p in out:
        p["thesis"] = _clip(p["thesis"], 600)
        p["invalidation"] = _clip(p["invalidation"], 300)
    return out


def trades(conn: sqlite3.Connection, now_ms: int, limit: int = 20) -> dict[str, Any]:
    recent = rows(
        conn.execute(
            "SELECT trade_id, side, volume, entry_ms, exit_ms, entry_price, exit_price, net_pnl, "
            "r_multiple, exit_reason FROM trades ORDER BY exit_ms DESC LIMIT ?",
            (limit,),
        )
    )
    day = period_start_ms("today", now_ms)
    n, net = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(net_pnl),0) FROM trades WHERE exit_ms >= ?", (day,)
    ).fetchone()
    n_all, net_all, wins = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(net_pnl),0), "
        "COALESCE(SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END),0) FROM trades"
    ).fetchone()
    return {
        "recent": recent,
        "today": {"n": n, "net_pnl": round(net, 2)},
        "all": {
            "n": n_all,
            "net_pnl": round(net_all, 2),
            "hit_rate": round(wins / n_all, 3) if n_all else None,
        },
    }


def open_positions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Fallback when the gateway is unreachable: what the table last recorded as open."""
    return rows(
        conn.execute(
            "SELECT position_id, side, volume, price_open, sl, tp, opened_ms FROM positions "
            "WHERE closed_ms IS NULL AND status='open' ORDER BY opened_ms"
        )
    )


def equity_series(conn: sqlite3.Connection, max_points: int = 300) -> list[list[float]]:
    data = conn.execute(
        "SELECT read_ms, equity FROM (SELECT read_ms, equity FROM account_snapshots "
        "WHERE equity IS NOT NULL ORDER BY read_ms DESC LIMIT 5000) ORDER BY read_ms"
    ).fetchall()
    step = max(1, len(data) // max_points)
    picked = data[::step]
    if data and picked[-1] is not data[-1]:
        picked.append(data[-1])
    return [[r[0], r[1]] for r in picked]


def news(conn: sqlite3.Connection, limit: int = 8) -> list[dict[str, Any]]:
    return rows(
        conn.execute(
            "SELECT created_ms, relevance, event_type, direction_for_gold, magnitude, confidence, "
            "summary FROM news_events ORDER BY created_ms DESC LIMIT ?",
            (limit,),
        )
    )


def calendar(conn: sqlite3.Connection, now_ms: int, limit: int = 8) -> list[dict[str, Any]]:
    if not table_exists(conn, "events"):
        return []
    return rows(
        conn.execute(
            "SELECT time_ms, title, country, impact, forecast, previous FROM events "
            "WHERE time_ms >= ? ORDER BY time_ms LIMIT ?",
            (now_ms, limit),
        )
    )


def incidents(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return rows(
        conn.execute(
            "SELECT id, opened_ms, severity, kind, summary FROM incidents WHERE closed_ms IS NULL "
            "ORDER BY opened_ms DESC LIMIT 20"
        )
    )


def costs(conn: sqlite3.Connection, now_ms: int) -> dict[str, Any]:
    def total(since: int) -> float:
        return round(
            conn.execute(
                "SELECT COALESCE(SUM(cost_usd),0) FROM llm_calls WHERE created_ms >= ?", (since,)
            ).fetchone()[0],
            4,
        )

    return {
        "today": total(period_start_ms("today", now_ms)),
        "week": total(period_start_ms("week", now_ms)),
        "all": total(0),
    }


def feed(conn: sqlite3.Connection, limit: int = 40) -> list[dict[str, Any]]:
    """One merged timeline, newest first: agent runs, cycles, trades, alerts, news."""
    items: list[dict[str, Any]] = []
    for r in conn.execute(
        "SELECT agent, started_ms, status, failure_reason, output_json FROM agent_runs "
        "ORDER BY started_ms DESC LIMIT ?",
        (limit,),
    ):
        text = summarise_output(r["agent"], _loads(r["output_json"], None))
        if r["status"] != "ok" and r["failure_reason"]:
            text = _clip(r["failure_reason"], 160)
        items.append(
            {
                "at_ms": r["started_ms"],
                "kind": "agent",
                "who": r["agent"],
                "level": "ok" if r["status"] == "ok" else "warn",
                "text": _clip(text, 220),
            }
        )
    for r in conn.execute(
        "SELECT started_ms, status, gate_reason, risk_budget FROM brain_cycles "
        "ORDER BY started_ms DESC LIMIT ?",
        (limit,),
    ):
        items.append(
            {
                "at_ms": r["started_ms"],
                "kind": "cycle",
                "who": "cycle",
                "level": "ok" if r["status"] in ("sent", "wait", "gated") else "warn",
                "text": f"Decision: {r['status']}"
                + (
                    f" ({r['gate_reason']})"
                    if r["gate_reason"] and r["gate_reason"] != r["status"]
                    else ""
                )
                + (f", risk budget {r['risk_budget']}" if r["risk_budget"] is not None else ""),
            }
        )
    for r in conn.execute(
        "SELECT exit_ms, side, net_pnl, r_multiple, exit_reason FROM trades "
        "ORDER BY exit_ms DESC LIMIT ?",
        (limit,),
    ):
        items.append(
            {
                "at_ms": r["exit_ms"],
                "kind": "trade",
                "who": "execution",
                "level": "good" if r["net_pnl"] > 0 else "bad",
                "text": f"Closed {r['side']} {r['net_pnl']:+.2f} USD ({r['exit_reason']})",
            }
        )
    for r in conn.execute(
        "SELECT created_ms, severity, kind, message FROM alerts ORDER BY created_ms DESC LIMIT ?",
        (limit,),
    ):
        items.append(
            {
                "at_ms": r["created_ms"],
                "kind": "alert",
                "who": "health",
                "level": "warn" if r["severity"] in ("warning", "warn", "info") else "bad",
                "text": _clip(f"{r['kind']}: {r['message']}", 220),
            }
        )
    for r in conn.execute(
        "SELECT created_ms, direction_for_gold, summary FROM news_events "
        "ORDER BY created_ms DESC LIMIT ?",
        (limit,),
    ):
        items.append(
            {
                "at_ms": r["created_ms"],
                "kind": "news",
                "who": "research",
                "level": "ok",
                "text": _clip(f"[{r['direction_for_gold']}] {r['summary']}", 220),
            }
        )
    items.sort(key=lambda x: x["at_ms"] or 0, reverse=True)
    return items[:limit]


# ------------------------------------------------------------------------------------ top level
def snapshot(
    conn: sqlite3.Connection, gateway: Any, now_ms: int, symbol: str = "XAUUSDm"
) -> dict[str, Any]:
    """Everything the page needs except the bars. `gateway` is GatewayClient-like or None."""
    state = None
    if gateway is not None:
        try:
            st = gateway.state()
            state = st.model_dump(mode="json") if st is not None else None
        except Exception:  # noqa: BLE001 - an unreachable gateway is a fact, not a crash
            state = None
    if state is not None:
        state.pop("owner_hash", None)
    chat_n, chat_last = conn.execute(
        "SELECT COUNT(*), MAX(created_ms) FROM chat_messages WHERE created_ms >= ?",
        (period_start_ms("today", now_ms),),
    ).fetchone()
    return {
        "now_ms": now_ms,
        "symbol": symbol,
        "processes": processes(conn, now_ms),
        "gateway": state,
        "gateway_reachable": state is not None,
        "positions": state["positions"] if state is not None else open_positions(conn),
        "incidents": incidents(conn),
        "agents": agents(conn, now_ms),
        "cycles": cycles(conn),
        "proposals": proposals(conn),
        "trades": trades(conn, now_ms),
        "equity": equity_series(conn),
        "news": news(conn),
        "calendar": calendar(conn, now_ms),
        "costs": costs(conn, now_ms),
        "chat": {"messages_today": chat_n, "last_ms": chat_last},
        "feed": feed(conn),
        "paper": paper(conn),
        "positioning": positioning(conn, now_ms),
        "veto": stored_report(conn, "veto"),
        "signals": signal_channels(conn, now_ms),
        "learning": stored_report(conn, "learning"),
    }


def signal_channels(conn: sqlite3.Connection, now_ms: int) -> dict[str, Any] | None:
    """Telegram signal channels and how their signals played out on our prices (D-061)."""
    from goldtrader.signals.report import scorecard

    return scorecard(conn, now_ms)


TIMEFRAMES = (1, 5, 15, 60)  # minutes


def bars(
    conn: sqlite3.Connection, symbol: str, minutes: int = 1, limit: int = 240
) -> dict[str, Any]:
    """Bid candles built from the stored 1-minute bars, oldest first: [time_s, o, h, l, c]."""
    if minutes not in TIMEFRAMES:
        minutes = 1
    limit = max(10, min(int(limit), 600))
    span = minutes * 60
    raw = conn.execute(
        "SELECT time_s, bid_open, bid_high, bid_low, bid_close, ask_close FROM bars "
        "WHERE symbol=? AND seconds=60 ORDER BY time_s DESC LIMIT ?",
        (symbol, limit * minutes),
    ).fetchall()
    out: list[list[float]] = []
    for r in reversed(raw):
        t = r[0] - r[0] % span
        if out and out[-1][0] == t:
            c = out[-1]
            c[2] = max(c[2], r[2])
            c[3] = min(c[3], r[3])
            c[4] = r[4]
        else:
            out.append([t, r[1], r[2], r[3], r[4]])
    return {
        "symbol": symbol,
        "minutes": minutes,
        "candles": out[-limit:],
        "last_ask": raw[0][5] if raw else None,
    }


# ------------------------------------------------------------------------------------ live view
LOCAL_CHAT_ID = 0  # same value as goldtrader.chat.local.LOCAL_CHAT_ID (not imported: isolation)
CHAT_CURSOR = "dashboard_chat_offset"
DEFAULT_CYCLE_S = 300


def live(conn: sqlite3.Connection, gateway: Any, now_ms: int) -> dict[str, Any]:
    """The small, once-a-second part: who is working right now, the cycle in flight, the tick."""
    tick = None
    if gateway is not None:
        try:
            st = gateway.state()
        except Exception:  # noqa: BLE001
            st = None
        if st is not None:
            tick = {
                "bid": st.bid,
                "ask": st.ask,
                "spread_pips": st.spread_pips,
                "session": st.session,
                "equity": st.equity,
                "day_pnl": st.day_pnl,
            }
    runs = rows(
        conn.execute(
            "SELECT run_id, agent, started_ms, finished_ms, status, output_json FROM agent_runs "
            "WHERE started_ms >= ? ORDER BY started_ms",
            (now_ms - 15 * 60_000,),
        )
    )
    for r in runs:
        r["running"] = r["finished_ms"] is None and now_ms - r["started_ms"] < RUNNING_WINDOW_MS
        out = _loads(r.pop("output_json"), None)
        r["summary"] = "" if r["running"] else summarise_output(r["agent"], out)
    two = rows(
        conn.execute(
            "SELECT cycle_id, started_ms, finished_ms, status, gate_reason, risk_budget, steps_json "
            "FROM brain_cycles ORDER BY started_ms DESC LIMIT 2"
        )
    )
    cycle = None
    every_s = DEFAULT_CYCLE_S
    if two:
        cycle = two[0]
        steps = _loads(cycle.pop("steps_json"), [])
        cycle["steps"] = steps if isinstance(steps, list) else []
        cycle["running"] = (
            cycle["finished_ms"] is None and now_ms - cycle["started_ms"] < RUNNING_WINDOW_MS
        )
        if len(two) == 2:
            gap = round((two[0]["started_ms"] - two[1]["started_ms"]) / 1000)
            if 30 <= gap <= 3600:
                every_s = gap
    return {
        "now_ms": now_ms,
        "tick": tick,
        "processes": processes(conn, now_ms),
        "runs": runs,
        "cycle": cycle,
        "cycle_every_s": every_s,
    }


def chat(conn: sqlite3.Connection, after_id: int = 0, limit: int = 60) -> dict[str, Any]:
    """The dashboard's own conversation (chat id 0) and what is still waiting for the brain."""
    if not table_exists(conn, "dashboard_chat_inbox"):
        return {"ready": False, "messages": [], "waiting": [], "thinking": False}
    msgs = rows(
        conn.execute(
            "SELECT id, created_ms, role, text FROM (SELECT id, created_ms, role, text FROM "
            "chat_messages WHERE chat_id=? AND id > ? ORDER BY id DESC LIMIT ?) ORDER BY id",
            (LOCAL_CHAT_ID, int(after_id), int(limit)),
        )
    )
    cur = conn.execute("SELECT value FROM notify_cursors WHERE name=?", (CHAT_CURSOR,)).fetchone()
    start = int(cur[0]) if cur else 0
    queued = rows(
        conn.execute(
            "SELECT id, created_ms, text FROM dashboard_chat_inbox WHERE id >= ? ORDER BY id",
            (start,),
        )
    )
    # a question the brain has picked up is already in chat_messages while the model answers it
    taken = {
        r[0]
        for r in conn.execute(
            "SELECT update_id FROM chat_messages WHERE chat_id=? AND update_id >= ?",
            (LOCAL_CHAT_ID, start),
        )
    }
    return {
        "ready": True,
        "messages": msgs,
        "waiting": [q for q in queued if q["id"] not in taken],
        "thinking": any(q["id"] in taken for q in queued),
    }


# --------------------------------------------------------------------------------- time machine
TM_STALE_MS = 15 * 60_000  # a "running" row older than this lost its process


def timemachine_list(
    conn: sqlite3.Connection, now_ms: int, limit: int = 20
) -> list[dict[str, Any]]:
    """Newest runs first, with the headline numbers but without the heavy result body."""
    if not table_exists(conn, "timemachine_runs"):
        return []
    out = rows(
        conn.execute(
            "SELECT id, created_ms, finished_ms, status, params_json, error, "
            "json_extract(result_json, '$.metrics.n_trades') AS n_trades, "
            "json_extract(result_json, '$.metrics.net_pnl') AS net_pnl, "
            "json_extract(result_json, '$.metrics.net_return_pct') AS net_return_pct "
            "FROM timemachine_runs ORDER BY id DESC LIMIT ?",
            (limit,),
        )
    )
    for r in out:
        r["params"] = _loads(r.pop("params_json"), {})
        if r["status"] == "running" and now_ms - r["created_ms"] > TM_STALE_MS:
            r["status"], r["error"] = "error", "the run did not finish"
    return out


def timemachine_get(conn: sqlite3.Connection, run_id: int) -> dict[str, Any] | None:
    if not table_exists(conn, "timemachine_runs"):
        return None
    r = _one(
        conn,
        "SELECT id, created_ms, finished_ms, status, params_json, result_json, error "
        "FROM timemachine_runs WHERE id=?",
        (int(run_id),),
    )
    if r is None:
        return None
    r["params"] = _loads(r.pop("params_json"), {})
    r["result"] = _loads(r.pop("result_json"), None)
    return r


# ------------------------------------------------------------------------------- paper candidates
def paper(conn: sqlite3.Connection, limit: int = 30) -> list[dict[str, Any]]:
    """Strategies simulated on live bars next to the live system (D-059), with results so far."""
    if not table_exists(conn, "paper_candidates"):
        return []
    out = rows(
        conn.execute(
            "SELECT bundle_id, kind, params_json, label, created_ms, active FROM paper_candidates "
            "ORDER BY created_ms"
        )
    )
    for c in out:
        c["params"] = _loads(c.pop("params_json"), {})
        c["active"] = bool(c["active"])
        trades = rows(
            conn.execute(
                "SELECT side, entry_ms, exit_ms, entry_price, exit_price, sl, tp, net_pnl, "
                "r_multiple, exit_reason, status FROM shadow_trades WHERE bundle_id=? "
                "ORDER BY entry_ms DESC LIMIT ?",
                (c["bundle_id"], limit),
            )
        )
        n, net, wins, mean_r = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(net_pnl),0), "
            "COALESCE(SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END),0), AVG(r_multiple) "
            "FROM shadow_trades WHERE bundle_id=? AND status='closed'",
            (c["bundle_id"],),
        ).fetchone()
        c["closed"] = n
        c["net_pnl"] = round(net, 2)
        c["wins"] = wins
        c["mean_r"] = None if mean_r is None else round(mean_r, 3)
        c["open"] = [t for t in trades if t["status"] == "open"]
        c["recent"] = [t for t in trades if t["status"] == "closed"]
    return out


# ------------------------------------------------------------------- positioning and veto report
def positioning(conn: sqlite3.Connection, now_ms: int) -> dict[str, Any] | None:
    """Latest CFTC gold futures positioning against the last ~3 years (D-060)."""
    if not table_exists(conn, "cot_reports"):
        return None
    got = conn.execute(
        "SELECT report_date, mm_long, mm_short FROM cot_reports ORDER BY report_date DESC LIMIT 156"
    ).fetchall()
    if len(got) < 2:
        return None
    nets = [int(r[1]) - int(r[2]) for r in got]
    return {
        "report_date": got[0][0],
        "net": nets[0],
        "change": nets[0] - nets[1],
        "percentile": round(100.0 * sum(1 for n in nets if n < nets[0]) / (len(nets) - 1)),
        "reports": len(nets),
        "history": [[r[0], n] for r, n in zip(got, nets, strict=True)][:52][::-1],
    }


def stored_report(conn: sqlite3.Connection, name: str) -> dict[str, Any] | None:
    if not table_exists(conn, "stored_reports"):
        return None
    r = conn.execute("SELECT body_json FROM stored_reports WHERE name=?", (name,)).fetchone()
    return _loads(r[0], None) if r else None

"""Read-only tools of the Chat bot (AGENTS.md §3.5; PRD R-041; D-039).

Every handler returns ``{"data": ..., "provenance": "<table or MT5 call> as of <ISO UTC>"}`` so the
model can cite where a fact came from (R-041). Nothing here writes: the handlers run SELECTs, call
the gateway's read endpoints (``/state``, ``/health``) or an injected features callable. Secrets
are never part of any result (the config dump carries no credentials by construction, R-053).

Numbers the owner may ask about (scorecard, LLM costs) are computed in code, never by the model.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from goldtrader.bots.common import defang
from goldtrader.llm.provider import ToolSpec
from goldtrader.llm.runtime import Tool

MAX_TRADES = 50
MAX_PROPOSALS = 20
MAX_EVENTS = 10
MAX_MEMORY_HITS = 20

Period = str  # today | week | month | all
PERIODS: tuple[str, ...] = ("today", "week", "month", "all")

#: Tool names whose results are account / trade / system facts (the post-processor requires a
#: ``source:`` tag in the reply when any of them was called).
DATA_TOOLS: frozenset[str] = frozenset(
    {
        "get_positions",
        "get_account",
        "get_trades",
        "get_trade_detail",
        "get_proposals",
        "get_features",
        "get_events",
        "get_health",
        "get_config",
        "get_scorecard",
        "get_llm_costs",
    }
)


@dataclass
class ToolContext:
    """What the read-only tools may reach (AGENTS §4, Chat column: read everything but secrets)."""

    conn: sqlite3.Connection
    gateway: Any  # GatewayClient-like: state() / health()
    features_fn: Callable[[], Any] | None
    cfg: Any
    clock_ms: Callable[[], int]

    def now_iso(self) -> str:
        return iso(self.clock_ms())


# --- helpers ----------------------------------------------------------------------------------
def iso(ms: int | None) -> str:
    """Epoch ms → ``YYYY-MM-DDTHH:MM:SSZ``; empty for None."""
    if ms is None:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def provenance(source: str, at_ms: int) -> str:
    return f"{source} as of {iso(at_ms)}"


def result(data: Any, source: str, at_ms: int) -> dict[str, Any]:
    """The uniform tool envelope (R-041)."""
    return {"data": data, "provenance": provenance(source, at_ms)}


def rows(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def period_start_ms(period: str, now_ms: int) -> int:
    """UTC start of ``today`` / this ISO ``week`` (Monday) / this ``month``; 0 for ``all``."""
    d = datetime.fromtimestamp(now_ms / 1000, tz=UTC)
    day = d.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "today":
        return int(day.timestamp() * 1000)
    if period == "week":
        return int((day - timedelta(days=day.weekday())).timestamp() * 1000)
    if period == "month":
        return int(day.replace(day=1).timestamp() * 1000)
    if period == "all":
        return 0
    raise ValueError(f"unknown period {period!r}; use one of {', '.join(PERIODS)}")


def _clamp(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, str | int | float | bool):
        return v
    if hasattr(v, "model_dump"):
        return v.model_dump(mode="json")
    if is_dataclass(v) and not isinstance(v, type):
        return asdict(v)
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, list | tuple):
        return [_jsonable(x) for x in v]
    return str(v)


# --- scorecard ---------------------------------------------------------------------------------
def scorecard(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Code-computed performance numbers over closed trades (net of costs, R-054).

    * ``net_pnl``: Σ net_pnl; ``hit_rate``: share of trades with net_pnl > 0;
    * ``profit_factor``: Σ wins / |Σ losses| (``None`` when there are no losses);
    * ``costs``: Σ (spread_cost + slippage_cost + |commission| + |negative swap|);
    * ``max_drawdown``: largest peak-to-trough fall of the cumulative net P&L in trade order (USD).
    """
    ordered = sorted(trades, key=lambda t: (t.get("exit_ms") or 0, str(t.get("trade_id"))))
    n = len(ordered)
    net = sum(float(t["net_pnl"]) for t in ordered)
    wins = [float(t["net_pnl"]) for t in ordered if float(t["net_pnl"]) > 0]
    losses = [float(t["net_pnl"]) for t in ordered if float(t["net_pnl"]) < 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    costs = 0.0
    for t in ordered:
        costs += float(t.get("spread_cost") or 0.0) + float(t.get("slippage_cost") or 0.0)
        costs += abs(float(t.get("commission") or 0.0))
        swap = float(t.get("swap") or 0.0)
        if swap < 0:
            costs += -swap
    peak = 0.0
    equity = 0.0
    max_dd = 0.0
    for t in ordered:
        equity += float(t["net_pnl"])
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "n_trades": n,
        "net_pnl": round(net, 2),
        "hit_rate": round(len(wins) / n, 4) if n else None,
        "profit_factor": round(gross_win / gross_loss, 4) if gross_loss > 0 else None,
        "gross_wins": round(gross_win, 2),
        "gross_losses": round(-gross_loss, 2),
        "costs": round(costs, 4),
        "max_drawdown": round(max_dd, 2),
        "avg_r_multiple": (
            round(
                sum(float(t["r_multiple"]) for t in ordered if t.get("r_multiple") is not None)
                / max(1, sum(1 for t in ordered if t.get("r_multiple") is not None)),
                3,
            )
            if any(t.get("r_multiple") is not None for t in ordered)
            else None
        ),
    }


# --- handlers ----------------------------------------------------------------------------------
class ChatTools:
    """The handlers, bound to a :class:`ToolContext`. Each method takes the validated tool input."""

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx

    # gateway / MT5
    def _state(self) -> Any:
        try:
            return self.ctx.gateway.state()
        except Exception:  # noqa: BLE001 - an unreachable gateway is a fact, not a crash
            return None

    def get_positions(self, _: dict[str, Any]) -> dict[str, Any]:
        st = self._state()
        now = self.ctx.clock_ms()
        if st is None:
            return result(None, "gateway /state unavailable", now)
        return result(
            {"open_positions": st.open_positions, "positions": st.positions},
            "gateway /state (MT5 positions_get)",
            st.time_ms,
        )

    def get_account(self, _: dict[str, Any]) -> dict[str, Any]:
        st = self._state()
        now = self.ctx.clock_ms()
        if st is None:
            return result(None, "gateway /state unavailable", now)
        data = {
            "equity": st.equity,
            "balance": st.balance,
            "margin_free": st.margin_free,
            "margin_level_pct": st.margin_level,
            "day_pnl": st.day_pnl,
            "open_positions": st.open_positions,
            "connected": st.connected,
            "trade_allowed": st.trade_allowed,
            "session": st.session,
            "entries_allowed": st.entries_allowed,
            "kill_switch": st.kill_switch,
            "paused": st.paused,
            "incident_open": st.incident_open,
        }
        return result(data, "gateway /state (MT5 account_info)", st.time_ms)

    def get_features(self, _: dict[str, Any]) -> dict[str, Any]:
        now = self.ctx.clock_ms()
        if self.ctx.features_fn is None:
            return result(None, "features unavailable (no feature source)", now)
        try:
            f = self.ctx.features_fn()
        except Exception as e:  # noqa: BLE001
            return result(None, f"features unavailable ({type(e).__name__})", now)
        if f is None:
            return result(None, "features unavailable (no closed bar yet)", now)
        data = _jsonable(f)
        at = now
        if isinstance(data, dict) and isinstance(data.get("time_s"), int | float):
            at = int(data["time_s"] * 1000)
        return result(data, "brain features (bars table)", at)

    def get_health(self, _: dict[str, Any]) -> dict[str, Any]:
        now = self.ctx.clock_ms()
        try:
            health = self.ctx.gateway.health()
        except Exception as e:  # noqa: BLE001
            health = {"error": f"unreachable: {type(e).__name__}"}
        c = self.ctx.conn
        heartbeats = rows(c.execute("SELECT process, last_ms, status FROM heartbeats"))
        for h in heartbeats:
            h["age_s"] = round((now - int(h["last_ms"])) / 1000, 1)
        incidents = rows(
            c.execute(
                "SELECT id, opened_ms, severity, kind, summary, acknowledged_ms FROM incidents "
                "WHERE closed_ms IS NULL ORDER BY opened_ms DESC LIMIT 20"
            )
        )
        breakers = rows(
            c.execute("SELECT agent, state, failures, next_trial_ms FROM breaker_state")
        )
        data = {
            "gateway": _jsonable(health),
            "heartbeats": heartbeats,
            "open_incidents": incidents,
            "breakers": breakers,
        }
        return result(data, "gateway /health + heartbeats, incidents, breaker_state tables", now)

    # tables
    def get_trades(self, inp: dict[str, Any]) -> dict[str, Any]:
        n = _clamp(inp.get("n", 10), 1, MAX_TRADES, 10)
        data = rows(
            self.ctx.conn.execute(
                "SELECT trade_id, position_id, proposal_id, side, volume, entry_ms, exit_ms, "
                "entry_price, exit_price, gross_pnl, spread_cost, slippage_cost, commission, swap, "
                "net_pnl, r_multiple, exit_reason, risk_pct_of_equity FROM trades "
                "ORDER BY exit_ms DESC LIMIT ?",
                (n,),
            )
        )
        for t in data:
            t["entry_at"] = iso(t["entry_ms"])
            t["exit_at"] = iso(t["exit_ms"])
        return result(data, "trades table", self.ctx.clock_ms())

    def get_trade_detail(self, inp: dict[str, Any]) -> dict[str, Any]:
        trade_id = str(inp.get("trade_id", ""))
        c = self.ctx.conn
        now = self.ctx.clock_ms()
        trade = c.execute("SELECT * FROM trades WHERE trade_id=?", (trade_id,)).fetchone()
        if trade is None:
            return result({"error": f"trade {trade_id!r} not found"}, "trades table", now)
        t = dict(trade)
        t["entry_at"] = iso(t["entry_ms"])
        t["exit_at"] = iso(t["exit_ms"])
        proposal = None
        intents: list[dict[str, Any]] = []
        if t.get("proposal_id"):
            p = c.execute(
                "SELECT proposal_id, created_ms, bot, action, side, stop_atr, target_atr, "
                "confidence, thesis, invalidation, gate_status, gate_reason FROM proposals "
                "WHERE proposal_id=?",
                (t["proposal_id"],),
            ).fetchone()
            proposal = dict(p) if p else None
            intents = rows(
                c.execute(
                    "SELECT intent_id, created_ms, kind, status, risk_code, source FROM intents "
                    "WHERE proposal_id=? ORDER BY created_ms",
                    (t["proposal_id"],),
                )
            )
        fills: list[dict[str, Any]] = []
        if t.get("position_id") is not None:
            fills = rows(
                c.execute(
                    "SELECT deal_ticket, order_ticket, time_ms, type, entry, volume, price, "
                    "commission, swap, profit FROM fills WHERE position_id=? ORDER BY time_ms",
                    (t["position_id"],),
                )
            )
        data = {"trade": t, "proposal": proposal, "intents": intents, "fills": fills}
        return result(data, "trades + proposals + intents + fills tables", now)

    def get_proposals(self, inp: dict[str, Any]) -> dict[str, Any]:
        n = _clamp(inp.get("n", 5), 1, MAX_PROPOSALS, 5)
        data = rows(
            self.ctx.conn.execute(
                "SELECT proposal_id, created_ms, bot, action, side, stop_atr, target_atr, "
                "confidence, thesis, invalidation, agrees_with_signal, gate_status, gate_reason "
                "FROM proposals ORDER BY created_ms DESC LIMIT ?",
                (n,),
            )
        )
        for p in data:
            p["created_at"] = iso(p["created_ms"])
        return result(data, "proposals table", self.ctx.clock_ms())

    def get_events(self, inp: dict[str, Any]) -> dict[str, Any]:
        n = _clamp(inp.get("next_n", 5), 1, MAX_EVENTS, 5)
        now = self.ctx.clock_ms()
        if not table_exists(self.ctx.conn, "events"):
            return result([], "events table (absent)", now)
        cols = {r["name"] for r in self.ctx.conn.execute("PRAGMA table_info(events)").fetchall()}
        time_col = "time_ms" if "time_ms" in cols else None
        if time_col is None:
            data = rows(self.ctx.conn.execute("SELECT * FROM events LIMIT ?", (n,)))
        else:
            data = rows(
                self.ctx.conn.execute(
                    f"SELECT * FROM events WHERE {time_col} >= ? ORDER BY {time_col} LIMIT ?",
                    (now, n),
                )
            )
            for e in data:
                e["time_at"] = iso(e[time_col])
                e["minutes_from_now"] = round((int(e[time_col]) - now) / 60_000, 1)
        return result(data, "events table", now)

    def get_config(self, inp: dict[str, Any]) -> dict[str, Any]:
        section = str(inp.get("section", "")).strip()
        now = self.ctx.clock_ms()
        dump = self.ctx.cfg.model_dump(mode="json")
        if section in ("", "all", "*"):
            return result(dump, "config (in-memory, validated)", now)
        if section not in dump:
            return result(
                {"error": f"unknown section {section!r}", "sections": sorted(dump)},
                "config (in-memory, validated)",
                now,
            )
        note = (
            "owner section is owner-only: readable here, never changeable through chat"
            if section == "owner"
            else None
        )
        data: dict[str, Any] = {"section": section, "values": dump[section]}
        if note:
            data["note"] = note
        return result(data, "config (in-memory, validated)", now)

    def get_scorecard(self, inp: dict[str, Any]) -> dict[str, Any]:
        period = str(inp.get("period", "all"))
        now = self.ctx.clock_ms()
        since = period_start_ms(period, now)
        trades = rows(
            self.ctx.conn.execute(
                "SELECT trade_id, exit_ms, net_pnl, spread_cost, slippage_cost, commission, swap, "
                "r_multiple FROM trades WHERE exit_ms >= ? ORDER BY exit_ms",
                (since,),
            )
        )
        card = scorecard(trades)
        card["period"] = period
        card["since"] = iso(since) if since else "beginning"
        return result(card, "trades table (computed in code)", now)

    def search_memory(self, inp: dict[str, Any]) -> dict[str, Any]:
        query = defang(inp.get("query", ""), 200)
        limit = _clamp(inp.get("limit", 10), 1, MAX_MEMORY_HITS, 10)
        now = self.ctx.clock_ms()
        if not query:
            return result([], "lessons + postmortems + news_events tables", now)
        like = f"%{query}%"
        c = self.ctx.conn
        hits: list[dict[str, Any]] = []
        for r in c.execute(
            "SELECT id, created_ms, trade_id, text FROM lessons WHERE text LIKE ? "
            "ORDER BY id DESC LIMIT ?",
            (like, limit),
        ):
            hits.append(
                {
                    "kind": "lesson",
                    "id": r["id"],
                    "at": iso(r["created_ms"]),
                    "trade_id": r["trade_id"],
                    "text": r["text"],
                }
            )
        for r in c.execute(
            "SELECT id, created_ms, trade_id, outcome_class, confidence FROM postmortems "
            "WHERE outcome_class LIKE ? OR trade_id LIKE ? ORDER BY id DESC LIMIT ?",
            (like, like, limit),
        ):
            hits.append(
                {
                    "kind": "postmortem",
                    "id": r["id"],
                    "at": iso(r["created_ms"]),
                    "trade_id": r["trade_id"],
                    "text": f"{r['outcome_class']} (confidence {r['confidence']})",
                }
            )
        for r in c.execute(
            "SELECT id, created_ms, event_type, direction_for_gold, summary FROM news_events "
            "WHERE summary LIKE ? ORDER BY id DESC LIMIT ?",
            (like, limit),
        ):
            hits.append(
                {
                    "kind": "news_event",
                    "id": r["id"],
                    "at": iso(r["created_ms"]),
                    "text": f"[{r['event_type']}/{r['direction_for_gold']}] {r['summary']}",
                }
            )
        hits.sort(key=lambda h: h["at"], reverse=True)
        return result(hits[:limit], "lessons + postmortems + news_events tables", now)

    def get_llm_costs(self, inp: dict[str, Any]) -> dict[str, Any]:
        period = str(inp.get("period", "today"))
        now = self.ctx.clock_ms()
        since = period_start_ms(period, now)
        c = self.ctx.conn
        total = c.execute(
            "SELECT COUNT(*) AS calls, COALESCE(SUM(cost_usd),0) AS cost_usd, "
            "COALESCE(SUM(input_tokens),0) AS input_tokens, "
            "COALESCE(SUM(output_tokens),0) AS output_tokens FROM llm_calls WHERE created_ms >= ?",
            (since,),
        ).fetchone()
        by_agent = rows(
            c.execute(
                "SELECT agent, COUNT(*) AS calls, ROUND(COALESCE(SUM(cost_usd),0), 6) AS cost_usd "
                "FROM llm_calls WHERE created_ms >= ? GROUP BY agent ORDER BY cost_usd DESC",
                (since,),
            )
        )
        llm = self.ctx.cfg.llm
        data = {
            "period": period,
            "since": iso(since) if since else "beginning",
            "calls": int(total["calls"]),
            "cost_usd": round(float(total["cost_usd"]), 6),
            "input_tokens": int(total["input_tokens"]),
            "output_tokens": int(total["output_tokens"]),
            "by_agent": by_agent,
            "daily_budget_usd": llm.daily_budget_usd,
            "monthly_budget_usd": llm.monthly_budget_usd,
        }
        return result(data, "llm_calls table", now)


# --- specs --------------------------------------------------------------------------------------
def _obj(props: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props or {},
        "required": required or [],
        "additionalProperties": False,
    }


_PERIOD = {"type": "string", "enum": list(PERIODS)}


def chat_tool_specs() -> list[ToolSpec]:
    """Flat JSON-schema specs for every read-only tool (AGENTS §3.5 tool list)."""
    return [
        ToolSpec(
            "get_positions",
            "Open positions from the gateway (MT5 positions_get). Read-only.",
            _obj(),
        ),
        ToolSpec(
            "get_account",
            "Account equity, balance, free margin, margin level, day P&L and flags from the "
            "gateway (MT5 account_info). Read-only.",
            _obj(),
        ),
        ToolSpec(
            "get_trades",
            "Last n closed trades with costs and risk (trades table). Read-only.",
            _obj({"n": {"type": "integer", "minimum": 1, "maximum": MAX_TRADES}}),
        ),
        ToolSpec(
            "get_trade_detail",
            "One trade with its proposal thesis/invalidation, intents and fills. Read-only.",
            _obj({"trade_id": {"type": "string", "minLength": 1, "maxLength": 64}}, ["trade_id"]),
        ),
        ToolSpec(
            "get_proposals",
            "Last n strategy-bot proposals with gate status (proposals table). Read-only.",
            _obj({"n": {"type": "integer", "minimum": 1, "maximum": MAX_PROPOSALS}}),
        ),
        ToolSpec(
            "get_features",
            "Latest code-computed market features (regime, ATR, spread, trend). Read-only.",
            _obj(),
        ),
        ToolSpec(
            "get_events",
            "Next scheduled economic events (events table). Read-only.",
            _obj({"next_n": {"type": "integer", "minimum": 1, "maximum": MAX_EVENTS}}),
        ),
        ToolSpec(
            "get_health",
            "Gateway health, process heartbeats, open incidents and LLM breaker states. Read-only.",
            _obj(),
        ),
        ToolSpec(
            "get_config",
            "One config section as JSON (account, session, data, strategies, llm, feeds, chat, "
            "owner). The owner section is readable but can never be changed through chat.",
            _obj({"section": {"type": "string", "minLength": 1, "maxLength": 32}}, ["section"]),
        ),
        ToolSpec(
            "get_scorecard",
            "Code-computed performance for a period: net P&L, trades, hit rate, profit factor, "
            "costs, max drawdown. Read-only.",
            _obj({"period": _PERIOD}, ["period"]),
        ),
        ToolSpec(
            "search_memory",
            "Search lessons, post-mortems and news event summaries. Read-only.",
            _obj(
                {
                    "query": {"type": "string", "minLength": 1, "maxLength": 200},
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_MEMORY_HITS},
                },
                ["query"],
            ),
        ),
        ToolSpec(
            "get_llm_costs",
            "LLM spend for a period from the llm_calls ledger, with budgets. Read-only.",
            _obj({"period": _PERIOD}, ["period"]),
        ),
    ]


def chat_tools(ctx: ToolContext) -> list[Tool]:
    """The read-only tools bound to ``ctx`` (handlers from :class:`ChatTools`)."""
    handlers = ChatTools(ctx)
    return [Tool(spec, getattr(handlers, spec.name)) for spec in chat_tool_specs()]


def dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str)

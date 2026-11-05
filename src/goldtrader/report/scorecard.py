"""Scorecard and go-live checklist computed from the journal (R-030, R-054; TEST_PLAN §4).

Everything here is read-only SQL plus arithmetic over ``trades`` (closed round trips, net of
spread, slippage, commission and swap — R-054), ``llm_calls`` (what the AI cost), and for the
checklist ``account_snapshots``, ``positions``, ``fills``, ``process_exits`` and ``incidents``.
The journal (R-030) is the single source: no number is taken from a bot or from memory.

Honest framing (TEST_PLAN Phase 7 exit; RESEARCH §5.5): a few weeks of demo trading cannot
establish an edge, so :func:`render_scorecard_text` says so and reports costs explicitly.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from goldtrader.session.schedule import Schedule, ScheduleState

Period = Literal["today", "week", "month", "all"]

DAY_MS = 86_400_000
MINUTE_MS = 60_000
TELEGRAM_MAX_CHARS = 3500
COST_LINE = "net of spread, slippage, commission and swap"


# -- periods ----------------------------------------------------------------------------------------


def period_start_ms(period: Period, now_ms: int) -> int:
    """Start of the UTC day / ISO week (Monday) / calendar month containing ``now_ms``; 0 for all."""
    if period == "all":
        return 0
    now = datetime.fromtimestamp(now_ms / 1000, tz=UTC)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "today":
        start = day
    elif period == "week":
        start = day - timedelta(days=day.weekday())
    elif period == "month":
        start = day.replace(day=1)
    else:
        raise ValueError(f"unknown period {period!r}")
    return int(start.timestamp() * 1000)


# -- helpers ----------------------------------------------------------------------------------------


def _r2(x: float) -> float:
    return round(float(x) + 0.0, 2)


def _rows(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    cur = conn.execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _scalar(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(sql, args).fetchone()
    return None if row is None else row[0]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        _scalar(conn, "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,))
        is not None
    )


def max_drawdown(pnls: list[float]) -> float:
    """Largest peak-to-trough fall of the cumulative sum of ``pnls`` (peak starts at 0), ≥ 0."""
    peak = 0.0
    cum = 0.0
    worst = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        worst = max(worst, peak - cum)
    return worst


def _bucket(trades: list[dict[str, Any]], key: Callable[[dict[str, Any]], Any]) -> dict[Any, dict]:
    out: dict[Any, dict[str, Any]] = {}
    for t in trades:
        k = key(t)
        b = out.setdefault(k, {"n": 0, "wins": 0, "net_pnl": 0.0})
        b["n"] += 1
        b["wins"] += 1 if t["net_pnl"] > 0 else 0
        b["net_pnl"] += t["net_pnl"]
    for b in out.values():
        b["net_pnl"] = _r2(b["net_pnl"])
    return dict(sorted(out.items(), key=lambda kv: str(kv[0])))


def _regimes(conn: sqlite3.Connection, trades: list[dict[str, Any]]) -> dict[str, str]:
    """position_id → regime recorded on the entry's ``market_snapshots`` row (``ref_type='order'``)."""
    ids = [t["position_id"] for t in trades if t.get("position_id") is not None]
    if not ids:
        return {}
    out: dict[str, str] = {}
    for i in range(0, len(ids), 500):
        chunk = [str(x) for x in ids[i : i + 500]]
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(
            "SELECT ref_id, regime FROM market_snapshots WHERE ref_type='order' "
            f"AND ref_id IN ({marks}) ORDER BY id",
            chunk,
        ):
            if r[1]:
                out[str(r[0])] = str(r[1])
    return out


# -- scorecard --------------------------------------------------------------------------------------


def scorecard(conn: sqlite3.Connection, period: Period, now_ms: int) -> dict[str, Any]:
    """Performance over trades that closed in ``period`` (by ``exit_ms``), net of costs (R-054).

    Keys: ``period, start_ms, end_ms, n_trades, wins, losses, hit_rate, net_pnl, gross_pnl,
    costs{spread, slippage, commission, swap, total}, profit_factor, expectancy_usd, avg_r,
    max_drawdown_usd, worst_trade, best_trade, by_exit_reason, by_regime, by_hour, avg_risk_pct,
    max_risk_pct, llm_cost_usd, equity``. Money is in account USD rounded to 2 dp; ratios are None
    when undefined (no trades, no losing trade for ``profit_factor``, no R recorded for ``avg_r``).
    ``equity`` summarises ``account_snapshots`` in the period (first / last / min / max) or is None.
    """
    start = period_start_ms(period, now_ms)
    trades = _rows(
        conn,
        "SELECT * FROM trades WHERE exit_ms >= ? AND exit_ms <= ? ORDER BY exit_ms, trade_id",
        (start, now_ms),
    )
    n = len(trades)
    nets = [float(t["net_pnl"]) for t in trades]
    wins = sum(1 for x in nets if x > 0)
    losses = sum(1 for x in nets if x < 0)
    gross_win = sum(x for x in nets if x > 0)
    gross_loss = -sum(x for x in nets if x < 0)
    spread = sum(float(t["spread_cost"] or 0) for t in trades)
    slippage = sum(float(t["slippage_cost"] or 0) for t in trades)
    commission = sum(float(t["commission"] or 0) for t in trades)
    swap = sum(float(t["swap"] or 0) for t in trades)
    rs = [float(t["r_multiple"]) for t in trades if t.get("r_multiple") is not None]
    risks = [
        float(t["risk_pct_of_equity"]) for t in trades if t.get("risk_pct_of_equity") is not None
    ]
    regimes = _regimes(conn, trades)

    def _trade_ref(t: dict[str, Any]) -> dict[str, Any]:
        return {
            "trade_id": t["trade_id"],
            "net_pnl": _r2(t["net_pnl"]),
            "side": t["side"],
            "exit_reason": t["exit_reason"],
            "exit_ms": t["exit_ms"],
        }

    llm_cost = 0.0
    if _table_exists(conn, "llm_calls"):
        llm_cost = float(
            _scalar(
                conn,
                "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls "
                "WHERE created_ms >= ? AND created_ms <= ?",
                (start, now_ms),
            )
            or 0
        )

    return {
        "period": period,
        "start_ms": start,
        "end_ms": now_ms,
        "n_trades": n,
        "wins": wins,
        "losses": losses,
        "hit_rate": (wins / n) if n else None,
        "net_pnl": _r2(sum(nets)),
        "gross_pnl": _r2(sum(float(t["gross_pnl"]) for t in trades)),
        "costs": {
            "spread": _r2(spread),
            "slippage": _r2(slippage),
            # commission and swap are signed as the broker books them (negative = paid)
            "commission": _r2(commission),
            "swap": _r2(swap),
            "total": _r2(spread + slippage - commission - swap),
        },
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "expectancy_usd": _r2(sum(nets) / n) if n else None,
        "avg_r": (sum(rs) / len(rs)) if rs else None,
        "max_drawdown_usd": _r2(max_drawdown(nets)),
        "worst_trade": _trade_ref(min(trades, key=lambda t: t["net_pnl"])) if trades else None,
        "best_trade": _trade_ref(max(trades, key=lambda t: t["net_pnl"])) if trades else None,
        "by_exit_reason": _bucket(trades, lambda t: str(t["exit_reason"] or "unknown")),
        "by_regime": _bucket(trades, lambda t: regimes.get(str(t.get("position_id")), "unknown")),
        "by_hour": _bucket(
            trades, lambda t: datetime.fromtimestamp(t["entry_ms"] / 1000, tz=UTC).hour
        ),
        "avg_risk_pct": (sum(risks) / len(risks)) if risks else None,
        "max_risk_pct": max(risks) if risks else None,
        "llm_cost_usd": round(llm_cost, 4),
        "equity": _equity(conn, start, now_ms),
    }


def _equity(conn: sqlite3.Connection, start_ms: int, end_ms: int) -> dict[str, float] | None:
    if not _table_exists(conn, "account_snapshots"):
        return None
    row = conn.execute(
        "SELECT MIN(equity), MAX(equity), COUNT(*) FROM account_snapshots "
        "WHERE read_ms >= ? AND read_ms <= ? AND equity IS NOT NULL",
        (start_ms, end_ms),
    ).fetchone()
    if not row or not row[2]:
        return None
    first = _scalar(
        conn,
        "SELECT equity FROM account_snapshots WHERE read_ms >= ? AND read_ms <= ? "
        "AND equity IS NOT NULL ORDER BY read_ms, id LIMIT 1",
        (start_ms, end_ms),
    )
    last = _scalar(
        conn,
        "SELECT equity FROM account_snapshots WHERE read_ms >= ? AND read_ms <= ? "
        "AND equity IS NOT NULL ORDER BY read_ms DESC, id DESC LIMIT 1",
        (start_ms, end_ms),
    )
    return {
        "first": _r2(first),
        "last": _r2(last),
        "min": _r2(row[0]),
        "max": _r2(row[1]),
        "snapshots": int(row[2]),
    }


# -- rendering --------------------------------------------------------------------------------------


def _money(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.2f} USD"


def _pct(x: float | None, digits: int = 0) -> str:
    return "n/a" if x is None else f"{100 * x:.{digits}f}%"


def _ratio(x: float | None) -> str:
    if x is None or math.isinf(x):
        return "n/a"
    return f"{x:.2f}"


def _fmt_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d")


def render_scorecard_text(d: dict[str, Any]) -> str:
    """Plain-text scorecard for Telegram: ≤ 3,500 chars, every money figure at 2 dp, and the
    ``net of spread, slippage, commission and swap`` line (R-054)."""
    c = d["costs"]
    label = {"today": "Today", "week": "This week", "month": "This month", "all": "All time"}[
        d["period"]
    ]
    span = (
        f"{_fmt_day(d['start_ms'])} → {_fmt_day(d['end_ms'])}"
        if d["start_ms"]
        else f"up to {_fmt_day(d['end_ms'])}"
    )
    lines = [
        f"Scorecard — {label} ({span}, UTC)",
        f"Trades: {d['n_trades']}  wins {d['wins']}  losses {d['losses']}  "
        f"hit rate {_pct(d['hit_rate'])}",
        f"Net P&L: {_money(d['net_pnl'])} ({COST_LINE})",
        f"Gross P&L: {_money(d['gross_pnl'])}",
        f"Costs: spread {c['spread']:.2f}  slippage {c['slippage']:.2f}  "
        f"commission {c['commission']:.2f}  swap {c['swap']:.2f}  total {c['total']:.2f} USD",
        f"Profit factor {_ratio(d['profit_factor'])}  expectancy {_money(d['expectancy_usd'])}"
        f"  avg R {_ratio(d['avg_r'])}",
        f"Max drawdown: {d['max_drawdown_usd']:.2f} USD (cumulative net P&L)",
    ]
    if d["best_trade"] and d["worst_trade"]:
        lines.append(
            f"Best {d['best_trade']['trade_id']} {_money(d['best_trade']['net_pnl'])}  "
            f"worst {d['worst_trade']['trade_id']} {_money(d['worst_trade']['net_pnl'])}"
        )
    if d["avg_risk_pct"] is not None:
        lines.append(
            f"Risk per trade: avg {d['avg_risk_pct']:.2f}%  max {d['max_risk_pct']:.2f}% of equity"
        )
    if d.get("equity"):
        e = d["equity"]
        lines.append(
            f"Equity: {e['first']:.2f} → {e['last']:.2f} USD (min {e['min']:.2f}, max {e['max']:.2f})"
        )
    lines.append(f"AI cost: {d['llm_cost_usd']:.2f} USD")
    for title, key in (
        ("By exit", "by_exit_reason"),
        ("By regime", "by_regime"),
        ("By entry hour (UTC)", "by_hour"),
    ):
        buckets = d.get(key) or {}
        if buckets:
            parts = [f"{k}: {v['n']}× {v['net_pnl']:+.2f}" for k, v in buckets.items()]
            lines.append(f"{title}: " + "; ".join(parts))
    lines.append(
        "Note: a few weeks of demo trades cannot establish an edge; these are descriptive numbers."
    )
    text = "\n".join(lines)
    if len(text) > TELEGRAM_MAX_CHARS:
        text = text[: TELEGRAM_MAX_CHARS - 1].rstrip() + "…"
    return text


# -- go-live checklist ---------------------------------------------------------------------------

Check = tuple[str, bool, str]
CRASH_REASONS = ("crashed", "exception")


def _open_minutes(schedule: Schedule, start_ms: int, end_ms: int) -> list[int]:
    """Start (ms) of every whole minute in ``[start_ms, end_ms)`` the schedule marks OPEN."""
    out: list[int] = []
    t = start_ms - start_ms % MINUTE_MS
    while t + MINUTE_MS <= end_ms:
        dt = datetime.fromtimestamp(t / 1000, tz=UTC)
        if schedule.state_at(dt) == ScheduleState.OPEN:
            out.append(t)
        t += MINUTE_MS
    return out


def uptime_share(
    conn: sqlite3.Connection,
    schedule: Schedule,
    start_ms: int,
    end_ms: int,
    *,
    interval_ms: int = MINUTE_MS,
) -> tuple[float | None, int, int]:
    """Share of OPEN-hour ``interval_ms`` slots in ``[start_ms, end_ms)`` that hold a gateway
    ``account_snapshots`` row (the gateway writes one every 60 s while connected, ARCHITECTURE
    §2.1). Returns ``(share, present, expected)``; share is None when no slot was expected.

    ``heartbeats`` keeps only the latest row per process (migration 001), so it cannot give a
    history; the account snapshot cadence is the journal's uptime trace.
    """
    slots = _open_minutes(schedule, start_ms, end_ms)
    if not slots:
        return None, 0, 0
    wanted = {s // interval_ms for s in slots}
    present = {
        int(r[0]) // interval_ms
        for r in conn.execute(
            "SELECT read_ms FROM account_snapshots WHERE read_ms >= ? AND read_ms < ?",
            (start_ms, end_ms),
        )
    }
    hit = len(wanted & present)
    return hit / len(wanted), hit, len(wanted)


def _first_activity_ms(conn: sqlite3.Connection) -> int | None:
    candidates = [
        _scalar(conn, "SELECT MIN(read_ms) FROM account_snapshots"),
        _scalar(conn, "SELECT MIN(entry_ms) FROM trades"),
        _scalar(conn, "SELECT MIN(opened_ms) FROM positions"),
    ]
    if _table_exists(conn, "process_exits"):
        candidates.append(_scalar(conn, "SELECT MIN(started_ms) FROM process_exits"))
    values = [int(v) for v in candidates if v is not None]
    return min(values) if values else None


def go_live_checklist(
    conn: sqlite3.Connection,
    cfg: Any,
    now_ms: int,
    *,
    period_start_ms: int | None = None,
    min_days: int = 28,
    min_trades: int = 20,
    min_uptime: float = 0.99,
    reconcile_ms: int = 60_000,
    reconcile_tolerance_usd: float = 0.01,
) -> list[Check]:
    """TEST_PLAN §4 "Phase 7 exit" checks computable from the journal, as ``(name, ok, detail)``.

    * ``uptime``: :func:`uptime_share` over OPEN hours ≥ ``min_uptime`` (default 99%);
    * ``stops``: no position that lived longer than one reconcile cycle without a broker-side stop
      (``positions.sl`` NULL / 0 and observed lifetime > ``reconcile_ms``);
    * ``crashes``: no ``process_exits`` with reason ``crashed`` / ``exception…`` in the period;
    * ``incidents``: every incident acknowledged;
    * ``journal``: every trade carries a ``proposal_id`` and its position an ``intent_id``;
    * ``reconciliation``: each trade's ``net_pnl`` equals Σ fills ``profit + commission + swap`` of
      its position within ``reconcile_tolerance_usd``;
    * ``days`` / ``trades``: at least ``min_days`` of demo trading and ``min_trades`` closed trades.

    ``period_start_ms`` defaults to the first journal activity (snapshot, trade, position or
    process start). The owner's review and approval (TEST_PLAN) are not computable and not listed.
    """
    start = period_start_ms if period_start_ms is not None else _first_activity_ms(conn)
    if start is None:
        start = now_ms
    checks: list[Check] = []

    # uptime
    schedule = Schedule(cfg.session.hours, cfg.session.dst_mode)
    share, hit, expected = uptime_share(conn, schedule, start, now_ms)
    if share is None:
        checks.append(("uptime", False, "no OPEN hours in the period"))
    else:
        checks.append(
            (
                "uptime",
                share >= min_uptime,
                f"{100 * share:.2f}% of OPEN minutes have a gateway snapshot "
                f"({hit}/{expected}); need ≥ {100 * min_uptime:.0f}%",
            )
        )

    # positions without a stop beyond one reconcile cycle
    unprotected = _rows(
        conn,
        "SELECT position_id, opened_ms, closed_ms, last_seen_ms, status FROM positions "
        "WHERE (sl IS NULL OR sl = 0) AND opened_ms >= ? AND "
        "COALESCE(closed_ms, last_seen_ms, opened_ms) - opened_ms > ?",
        (start, reconcile_ms),
    )
    ids = [str(p["position_id"]) for p in unprotected]
    checks.append(
        (
            "stops",
            not ids,
            "every position had a broker-side stop within one reconcile cycle"
            if not ids
            else f"{len(ids)} position(s) without a stop beyond {reconcile_ms // 1000} s: "
            + ", ".join(ids[:10]),
        )
    )

    # unhandled crashes
    if _table_exists(conn, "process_exits"):
        crashes = _rows(
            conn,
            "SELECT process, exited_ms, reason FROM process_exits WHERE exited_ms >= ? AND "
            "(reason IN (?, ?) OR reason LIKE 'exception%' OR reason LIKE 'crash%')",
            (start, *CRASH_REASONS),
        )
        checks.append(
            (
                "crashes",
                not crashes,
                "no unhandled crashes"
                if not crashes
                else f"{len(crashes)} crash exit(s): "
                + ", ".join(f"{c['process']}@{_fmt_day(c['exited_ms'])}" for c in crashes[:5]),
            )
        )
    else:
        checks.append(("crashes", False, "process_exits table missing (migration 003)"))

    # incidents acknowledged
    open_inc = int(
        _scalar(conn, "SELECT COUNT(*) FROM incidents WHERE acknowledged_ms IS NULL") or 0
    )
    checks.append(
        (
            "incidents",
            open_inc == 0,
            "all incidents acknowledged" if open_inc == 0 else f"{open_inc} unacknowledged",
        )
    )

    # journal completeness
    trades = _rows(
        conn,
        "SELECT t.trade_id, t.position_id, t.proposal_id, t.net_pnl, p.intent_id FROM trades t "
        "LEFT JOIN positions p ON p.position_id = t.position_id "
        "WHERE t.exit_ms >= ? AND t.exit_ms <= ? ORDER BY t.exit_ms",
        (start, now_ms),
    )
    incomplete = [t["trade_id"] for t in trades if not t["proposal_id"] or not t["intent_id"]]
    checks.append(
        (
            "journal",
            not incomplete,
            f"{len(trades)} trade(s) all carry proposal_id and intent_id"
            if not incomplete
            else f"{len(incomplete)} trade(s) missing proposal_id or intent_id: "
            + ", ".join(incomplete[:10]),
        )
    )

    # reconciliation: net_pnl vs fills
    mismatched: list[str] = []
    for t in trades:
        if t["position_id"] is None:
            mismatched.append(t["trade_id"])
            continue
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(profit + commission + swap), 0) FROM fills "
            "WHERE position_id = ?",
            (t["position_id"],),
        ).fetchone()
        if not row or not row[0]:
            mismatched.append(t["trade_id"])
        elif abs(float(row[1]) - float(t["net_pnl"])) > reconcile_tolerance_usd + 1e-9:
            mismatched.append(t["trade_id"])
    checks.append(
        (
            "reconciliation",
            not mismatched,
            f"net P&L of {len(trades)} trade(s) matches deal history within "
            f"{reconcile_tolerance_usd:.2f} USD"
            if not mismatched
            else f"{len(mismatched)} trade(s) do not reconcile to fills: "
            + ", ".join(mismatched[:10]),
        )
    )

    # minimum duration and sample
    days = (now_ms - start) / DAY_MS
    checks.append(
        ("days", days >= min_days, f"{days:.1f} day(s) of demo trading; need ≥ {min_days}")
    )
    checks.append(
        (
            "trades",
            len(trades) >= min_trades,
            f"{len(trades)} closed trade(s); need ≥ {min_trades}",
        )
    )
    return checks


def render_checklist_text(checks: list[Check]) -> str:
    """One line per check for Telegram / the CLI."""
    lines = ["Go-live checklist (TEST_PLAN Phase 7 exit):"]
    for name, ok, detail in checks:
        lines.append(f"[{'OK' if ok else 'FAIL'}] {name}: {detail}")
    passed = sum(1 for _, ok, _ in checks if ok)
    lines.append(
        f"{passed}/{len(checks)} checks pass; owner review and approval are not automated."
    )
    return "\n".join(lines)

"""Scorecard (R-054 net of costs) and the Phase 7 go-live checklist on a hand-built journal."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from goldtrader.config.loader import parse_config
from goldtrader.db import connect, migrate
from goldtrader.report.scorecard import (
    COST_LINE,
    TELEGRAM_MAX_CHARS,
    go_live_checklist,
    max_drawdown,
    period_start_ms,
    render_checklist_text,
    render_scorecard_text,
    scorecard,
    uptime_share,
)
from goldtrader.session.schedule import Schedule

NOW = 1_793_887_200_000  # Thu 2026-11-05 14:00 UTC
HOUR = 3_600_000
DAY = 24 * HOUR
MONDAY = 1_793_577_600_000  # Mon 2026-11-02 00:00 UTC


def _db() -> sqlite3.Connection:
    conn = connect(None)
    assert migrate(conn) >= 7
    return conn


TRADES = [
    # trade_id, position_id, proposal, entry_ms, exit_ms, gross, spread, slip, comm, swap, net, r, reason, risk
    (
        "T1",
        1,
        "p1",
        NOW - 6 * HOUR,
        NOW - 5 * HOUR,
        30.0,
        1.0,
        0.5,
        -1.0,
        0.0,
        28.0,
        1.4,
        "tp",
        0.5,
    ),
    (
        "T2",
        2,
        "p2",
        NOW - 4 * HOUR,
        NOW - 3 * HOUR,
        -18.0,
        1.0,
        0.5,
        -1.0,
        -0.5,
        -19.5,
        -1.0,
        "sl",
        0.4,
    ),
    (
        "T3",
        3,
        "p3",
        NOW - 2 * HOUR,
        NOW - 1 * HOUR,
        12.0,
        1.0,
        0.0,
        -1.0,
        0.0,
        11.0,
        0.55,
        "tp",
        0.5,
    ),
    # yesterday (same ISO week), no R recorded
    (
        "T4",
        4,
        "p4",
        NOW - 30 * HOUR,
        NOW - 29 * HOUR,
        -9.0,
        1.0,
        1.0,
        -1.0,
        0.0,
        -10.0,
        None,
        "manual",
        0.3,
    ),
    # last week (previous month boundary is irrelevant here: 2026-10-29)
    (
        "T5",
        5,
        None,
        NOW - 7 * DAY,
        NOW - 7 * DAY + HOUR,
        50.0,
        2.0,
        0.0,
        -1.0,
        0.0,
        49.0,
        2.0,
        "tp",
        0.5,
    ),
]


def _seed_trades(conn: sqlite3.Connection, rows=TRADES) -> None:
    for tid, pid, prop, ems, xms, g, sp, sl, com, sw, net, r, reason, risk in rows:
        conn.execute(
            "INSERT INTO trades(trade_id, position_id, bundle_id, proposal_id, side, volume, "
            "entry_ms, exit_ms, entry_price, exit_price, gross_pnl, spread_cost, slippage_cost, "
            "commission, swap, net_pnl, r_multiple, exit_reason, risk_pct_of_equity) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                tid,
                pid,
                "b1",
                prop,
                "long",
                0.01,
                ems,
                xms,
                4000.0,
                4001.0,
                g,
                sp,
                sl,
                com,
                sw,
                net,
                r,
                reason,
                risk,
            ),
        )


def test_period_starts():
    assert period_start_ms("today", NOW) == NOW - 14 * HOUR
    assert period_start_ms("week", NOW) == MONDAY
    assert period_start_ms("month", NOW) == int(
        datetime(2026, 11, 1, tzinfo=UTC).timestamp() * 1000
    )
    assert period_start_ms("all", NOW) == 0


def test_max_drawdown():
    assert max_drawdown([]) == 0.0
    assert max_drawdown([10, -4, -8, 5]) == 12.0  # peak 10 → trough -2
    assert max_drawdown([-5, -5]) == 10.0  # from the 0 start
    assert max_drawdown([1, 2, 3]) == 0.0


def test_scorecard_exact_numbers_today():
    conn = _db()
    _seed_trades(conn)
    conn.execute(
        "INSERT INTO market_snapshots(time_ms, ref_type, ref_id, regime) VALUES (?,?,?,?)",
        (NOW - 6 * HOUR, "order", "1", "trend"),
    )
    conn.execute(
        "INSERT INTO market_snapshots(time_ms, ref_type, ref_id, regime) VALUES (?,?,?,?)",
        (NOW - 4 * HOUR, "order", "2", "range"),
    )
    conn.execute(
        "INSERT INTO llm_calls(created_ms, agent, run_id, model_id, prompt_hash, schema_version, "
        "cost_usd, status) VALUES (?,?,?,?,?,?,?,?)",
        (NOW - HOUR, "strategy", "r1", "m", "h", 1, 0.0125, "ok"),
    )
    conn.execute(
        "INSERT INTO llm_calls(created_ms, agent, run_id, model_id, prompt_hash, schema_version, "
        "cost_usd, status) VALUES (?,?,?,?,?,?,?,?)",
        (NOW - 2 * DAY, "strategy", "r0", "m", "h", 1, 5.0, "ok"),  # outside today
    )
    for i, eq in enumerate((1000.0, 1028.0, 1008.5, 1019.5)):
        conn.execute(
            "INSERT INTO account_snapshots(read_ms, login, equity) VALUES (?,?,?)",
            (NOW - 7 * HOUR + i * HOUR, 1, eq),
        )
    d = scorecard(conn, "today", NOW)
    assert d["n_trades"] == 3 and d["wins"] == 2 and d["losses"] == 1
    assert d["hit_rate"] == 2 / 3
    assert d["net_pnl"] == 19.5 and d["gross_pnl"] == 24.0
    assert d["costs"] == {
        "spread": 3.0,
        "slippage": 1.0,
        "commission": -3.0,
        "swap": -0.5,
        "total": 7.5,
    }
    assert d["profit_factor"] == 39.0 / 19.5 == 2.0
    assert d["expectancy_usd"] == 6.5
    assert abs(d["avg_r"] - (1.4 - 1.0 + 0.55) / 3) < 1e-12
    assert d["max_drawdown_usd"] == 19.5  # 28 → 8.5
    assert d["worst_trade"]["trade_id"] == "T2" and d["worst_trade"]["net_pnl"] == -19.5
    assert d["best_trade"]["trade_id"] == "T1" and d["best_trade"]["net_pnl"] == 28.0
    assert d["by_exit_reason"] == {
        "sl": {"n": 1, "wins": 0, "net_pnl": -19.5},
        "tp": {"n": 2, "wins": 2, "net_pnl": 39.0},
    }
    assert d["by_regime"] == {
        "range": {"n": 1, "wins": 0, "net_pnl": -19.5},
        "trend": {"n": 1, "wins": 1, "net_pnl": 28.0},
        "unknown": {"n": 1, "wins": 1, "net_pnl": 11.0},
    }
    assert d["by_hour"] == {
        8: {"n": 1, "wins": 1, "net_pnl": 28.0},
        10: {"n": 1, "wins": 0, "net_pnl": -19.5},
        12: {"n": 1, "wins": 1, "net_pnl": 11.0},
    }
    assert abs(d["avg_risk_pct"] - (0.5 + 0.4 + 0.5) / 3) < 1e-12 and d["max_risk_pct"] == 0.5
    assert d["llm_cost_usd"] == 0.0125
    assert d["equity"] == {
        "first": 1000.0,
        "last": 1019.5,
        "min": 1000.0,
        "max": 1028.0,
        "snapshots": 4,
    }


def test_scorecard_periods_and_empty():
    conn = _db()
    _seed_trades(conn)
    week = scorecard(conn, "week", NOW)
    assert week["n_trades"] == 4 and week["net_pnl"] == 9.5
    assert week["avg_r"] is not None  # T4 has no R and is skipped in the mean
    month = scorecard(conn, "month", NOW)
    assert month["n_trades"] == 4  # T5 closed on 2026-10-29
    everything = scorecard(conn, "all", NOW)
    assert everything["n_trades"] == 5 and everything["net_pnl"] == 58.5
    assert everything["profit_factor"] == 88.0 / 29.5
    empty = scorecard(conn, "today", NOW - 10 * DAY)
    assert empty["n_trades"] == 0 and empty["net_pnl"] == 0.0 and empty["hit_rate"] is None
    assert empty["profit_factor"] is None and empty["expectancy_usd"] is None
    assert empty["best_trade"] is None and empty["by_hour"] == {} and empty["equity"] is None
    only_wins = _db()
    _seed_trades(only_wins, [TRADES[0], TRADES[2]])
    assert scorecard(only_wins, "today", NOW)["profit_factor"] is None  # no losing trade


def test_render_scorecard_text_is_plain_short_and_two_decimal():
    conn = _db()
    _seed_trades(conn)
    text = render_scorecard_text(scorecard(conn, "today", NOW))
    assert len(text) <= TELEGRAM_MAX_CHARS
    assert COST_LINE in text
    assert "Net P&L: +19.50 USD" in text and "Gross P&L: +24.00 USD" in text
    assert "commission -3.00" in text and "swap -0.50" in text and "total 7.50 USD" in text
    assert "Max drawdown: 19.50 USD" in text
    assert "Best T1 +28.00 USD" in text and "worst T2 -19.50 USD" in text
    assert "AI cost: 0.00 USD" in text
    assert "By exit: sl: 1× -19.50; tp: 2× +39.00" in text
    assert "*" not in text and "<" not in text  # plain text, no markup
    for line in text.splitlines():
        for tok in line.replace(",", " ").split():
            if tok.replace("+", "").replace("-", "").replace(".", "").isdigit() and "." in tok:
                assert len(tok.split(".")[1]) == 2, tok
    empty = render_scorecard_text(scorecard(conn, "today", NOW - 10 * DAY))
    assert "Trades: 0" in empty and "n/a" in empty
    # a huge number of buckets still fits the Telegram cap
    big = scorecard(conn, "today", NOW)
    big["by_hour"] = {h: {"n": 1, "wins": 1, "net_pnl": 1.0} for h in range(2000)}
    assert len(render_scorecard_text(big)) <= TELEGRAM_MAX_CHARS


# --- go-live checklist -----------------------------------------------------------------------------


def _cfg():
    return parse_config({})


def _seed_journal(conn: sqlite3.Connection, *, start: int, now: int, break_stop: bool = False):
    """A healthy demo period: snapshots every minute, two reconciled trades with fills."""
    schedule = Schedule(_cfg().session.hours, _cfg().session.dst_mode)
    rows = []
    t = start
    while t < now:
        from goldtrader.session.schedule import ScheduleState

        if schedule.state_at(datetime.fromtimestamp(t / 1000, tz=UTC)) == ScheduleState.OPEN:
            rows.append((t + 5_000, 1, 1000.0))
        t += 60_000
    conn.executemany("INSERT INTO account_snapshots(read_ms, login, equity) VALUES (?,?,?)", rows)
    for pid, ems in ((1, start + 2 * DAY), (2, start + 3 * DAY)):
        conn.execute(
            "INSERT INTO positions(position_id, symbol, side, volume, price_open, sl, tp, "
            "opened_ms, closed_ms, status, last_seen_ms, proposal_id, intent_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                pid,
                "XAUUSDm",
                "long",
                0.01,
                4000.0,
                0.0 if (break_stop and pid == 2) else 3990.0,
                4020.0,
                ems,
                ems + HOUR,
                "closed",
                ems + HOUR,
                f"p{pid}",
                f"i{pid}",
            ),
        )
        conn.execute(
            "INSERT INTO fills(deal_ticket, order_ticket, position_id, time_ms, type, entry, "
            "volume, price, commission, swap, profit) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (pid * 10, pid * 10, pid, ems, 0, 0, 0.01, 4000.0, -0.5, 0.0, 0.0),
        )
        conn.execute(
            "INSERT INTO fills(deal_ticket, order_ticket, position_id, time_ms, type, entry, "
            "volume, price, commission, swap, profit) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (pid * 10 + 1, pid * 10 + 1, pid, ems + HOUR, 1, 1, 0.01, 4010.0, -0.5, -0.2, 10.0),
        )
        conn.execute(
            "INSERT INTO trades(trade_id, position_id, proposal_id, side, volume, entry_ms, "
            "exit_ms, entry_price, exit_price, gross_pnl, spread_cost, slippage_cost, commission, "
            "swap, net_pnl, r_multiple, exit_reason, risk_pct_of_equity) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"T{pid}",
                pid,
                f"p{pid}",
                "long",
                0.01,
                ems,
                ems + HOUR,
                4000.0,
                4010.0,
                10.0,
                0.4,
                0.0,
                -1.0,
                -0.2,
                8.8,
                1.0,
                "tp",
                0.5,
            ),
        )
    conn.execute(
        "INSERT INTO process_exits(process, pid, exit_code, started_ms, exited_ms, reason) "
        "VALUES (?,?,?,?,?,?)",
        ("gateway", 1, 0, start, start + DAY, "stopped"),
    )
    conn.execute(
        "INSERT INTO incidents(opened_ms, severity, kind, summary, acknowledged_ms, closed_ms) "
        "VALUES (?,?,?,?,?,?)",
        (start + DAY, "warning", "spread", "wide spread", start + DAY + 60_000, start + DAY + HOUR),
    )


def test_go_live_checklist_passes_on_a_clean_journal_and_flags_one_failure():
    conn = _db()
    start = NOW - 5 * DAY
    _seed_journal(conn, start=start, now=NOW)
    checks = go_live_checklist(conn, _cfg(), NOW, min_days=4, min_trades=2)
    names = [c[0] for c in checks]
    assert names == [
        "uptime",
        "stops",
        "crashes",
        "incidents",
        "journal",
        "reconciliation",
        "days",
        "trades",
    ]
    assert all(ok for _, ok, _ in checks), checks
    assert "100.00%" in checks[0][2]

    # one failing check: an unhandled crash
    conn.execute(
        "INSERT INTO process_exits(process, pid, exit_code, started_ms, exited_ms, reason) "
        "VALUES (?,?,?,?,?,?)",
        ("brain", 2, 1, start + DAY, start + 2 * DAY, "crashed"),
    )
    checks = go_live_checklist(conn, _cfg(), NOW, min_days=4, min_trades=2)
    failed = [c for c in checks if not c[1]]
    assert [c[0] for c in failed] == ["crashes"] and "brain@" in failed[0][2]
    text = render_checklist_text(checks)
    assert "[FAIL] crashes" in text and "7/8 checks pass" in text


def test_go_live_checklist_each_rule():
    conn = _db()
    start = NOW - 5 * DAY
    _seed_journal(conn, start=start, now=NOW, break_stop=True)
    # drop a third of the snapshots → uptime below 99%
    conn.execute("DELETE FROM account_snapshots WHERE id % 3 = 0")
    # a trade without proposal_id and a net_pnl that does not match its fills
    conn.execute("UPDATE trades SET proposal_id=NULL, net_pnl=9.5 WHERE trade_id='T1'")
    conn.execute(
        "INSERT INTO incidents(opened_ms, severity, kind, summary) VALUES (?,?,?,?)",
        (NOW - HOUR, "critical", "stop_missing", "no stop"),
    )
    by_name = {c[0]: c for c in go_live_checklist(conn, _cfg(), NOW, min_days=28, min_trades=20)}
    assert not by_name["uptime"][1] and "need ≥ 99%" in by_name["uptime"][2]
    assert not by_name["stops"][1] and "2" in by_name["stops"][2]  # position 2 never had a stop
    assert by_name["crashes"][1]
    assert not by_name["incidents"][1] and "1 unacknowledged" in by_name["incidents"][2]
    assert not by_name["journal"][1] and "T1" in by_name["journal"][2]
    assert not by_name["reconciliation"][1] and "T1" in by_name["reconciliation"][2]
    assert not by_name["days"][1] and "5.0 day(s)" in by_name["days"][2]
    assert not by_name["trades"][1] and "2 closed trade(s); need ≥ 20" in by_name["trades"][2]
    # tolerance: 0.01 USD per trade is accepted
    conn.execute("UPDATE trades SET net_pnl=8.81, proposal_id='p1' WHERE trade_id='T1'")
    by_name = {c[0]: c for c in go_live_checklist(conn, _cfg(), NOW)}
    assert by_name["reconciliation"][1] and by_name["journal"][1]


def test_uptime_share_counts_open_minutes_only():
    conn = _db()
    schedule = Schedule(_cfg().session.hours, _cfg().session.dst_mode)
    # Saturday: market closed → nothing expected
    sat = int(datetime(2026, 11, 7, 12, tzinfo=UTC).timestamp() * 1000)
    assert uptime_share(conn, schedule, sat, sat + HOUR) == (None, 0, 0)
    # Thursday 10:00–11:00 UTC: 60 open minutes, 30 snapshots present
    thu = int(datetime(2026, 11, 5, 10, tzinfo=UTC).timestamp() * 1000)
    conn.executemany(
        "INSERT INTO account_snapshots(read_ms, login) VALUES (?, 1)",
        [(thu + i * 120_000 + 1000,) for i in range(30)],
    )
    share, hit, expected = uptime_share(conn, schedule, thu, thu + HOUR)
    assert (hit, expected) == (30, 60) and abs(share - 0.5) < 1e-12
    checks = go_live_checklist(conn, _cfg(), thu + HOUR, period_start_ms=thu)
    assert checks[0][0] == "uptime" and not checks[0][1] and "50.00%" in checks[0][2]

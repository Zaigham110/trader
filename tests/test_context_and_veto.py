"""D-060: CFTC positioning context, decision snapshots / model inputs, and the veto report."""

from __future__ import annotations

import json
import socket
import zlib
from datetime import UTC, datetime

import pytest

from goldtrader.bots.common import BotContext
from goldtrader.bots.risk_advisor import build_risk_messages
from goldtrader.cli import _default_spec  # noqa: F401 - keeps the CLI importable with the report
from goldtrader.config import load_config
from goldtrader.dashboard import data
from goldtrader.db import connect, migrate
from goldtrader.feeds.cot import URL, CotRefresher, CotStore, parse
from goldtrader.llm.store import LLMStore
from goldtrader.notify import telegram as tg
from goldtrader.report import veto
from goldtrader.session.state import SessionClock
from goldtrader.types import Bar

NOW = int(datetime(2026, 11, 17, 12, tzinfo=UTC).timestamp() * 1000)


def _cot_json(weeks: int = 60) -> str:
    rows = []
    for i in range(weeks):  # newest first, like the API; net rises toward the present
        day = datetime.fromtimestamp(NOW / 1000 - (7 + 7 * i) * 86400, UTC).strftime("%Y-%m-%d")
        rows.append(
            {
                "report_date_as_yyyy_mm_dd": day + "T00:00:00.000",
                "m_money_positions_long_all": str(150_000 - 1_000 * i),
                "m_money_positions_short_all": "20000",
                "open_interest_all": "400000",
            }
        )
    rows.append({"report_date_as_yyyy_mm_dd": "garbage"})
    return json.dumps(rows)


def test_cot_is_fetched_stored_and_turned_into_one_honest_line():
    conn = connect(None)
    migrate(conn)
    store = CotStore(conn)
    asked: list[str] = []

    def fetch(url: str) -> str:
        asked.append(url)
        return _cot_json()

    assert CotRefresher(store, lambda: NOW, fetch=fetch).run_once() == (60, None)
    assert (
        asked == [URL] and "088691" in URL and URL.startswith("https://publicreporting.cftc.gov/")
    )
    assert CotRefresher(store, lambda: NOW, fetch=fetch).run_once() == (60, None)  # idempotent
    assert conn.execute("SELECT COUNT(*) FROM cot_reports").fetchone()[0] == 60
    s = store.summary(NOW)
    assert s["net"] == 130_000 and s["change"] == 1_000 and s["percentile"] == 100
    line = store.line(NOW)
    assert "net long 130,000" in line and "not a timing signal" in line
    assert store.line(NOW + 40 * 86_400_000) == ""  # a stale report is not shown as current

    def broken(url: str) -> str:
        raise OSError("network down")

    n, err = CotRefresher(store, lambda: NOW, fetch=broken).run_once()
    assert n == 0 and "network down" in err  # never raises into the scheduler
    assert parse("{}") == [] and len(parse(_cot_json(3))) == 3
    view = data.positioning(conn, NOW)
    assert view["net"] == 130_000 and len(view["history"]) == 52
    assert view["history"][-1][1] == 130_000  # oldest first for the chart
    # the agents see it, and say so when there is none
    ctx = BotContext(now_ms=NOW, cfg=load_config("config/demo.yaml"), positioning_line=line)
    assert line in build_risk_messages(ctx)[0]["content"]
    ctx.positioning_line = ""
    assert "Positioning: no positioning data" in build_risk_messages(ctx)[0]["content"]


def test_model_inputs_are_kept_compressed():
    conn = connect(None)
    migrate(conn)
    msgs = [{"role": "user", "content": "Features:\n" + "atr 1.5\n" * 200}]
    LLMStore(conn).record_input("run1", "strategy", NOW, msgs)
    row = conn.execute("SELECT agent, messages_z FROM agent_inputs WHERE run_id='run1'").fetchone()
    assert row["agent"] == "strategy" and len(row["messages_z"]) < 400
    assert json.loads(zlib.decompress(row["messages_z"])) == msgs


def _bar(t: int, lo: float, hi: float, o: float = 4000.0) -> Bar:
    return Bar(
        time=t, seconds=60, ticks=1,
        bid_open=o, bid_high=hi, bid_low=lo, bid_close=o,
        ask_open=o + 0.2, ask_high=hi + 0.2, ask_low=lo + 0.2, ask_close=o + 0.2,
    )  # fmt: skip


def test_code_only_outcome_of_a_signal():
    cfg = load_config("config/demo.yaml")
    clock = SessionClock(cfg.session)
    t0 = int(datetime(2026, 11, 17, 12, tzinfo=UTC).timestamp())
    flat = [_bar(t0 + 60 * i, 3999.5, 4000.5) for i in range(1, 6)]
    win = [*flat, _bar(t0 + 360, 3999.5, 4003.0)]
    out = veto.simulate(1, t0, 1.0, win, 1.0, 2.0, clock, [])
    assert out["exit"] == "target" and out["r"] == 2.0 and out["entry"] == 4000.2
    lose = [*flat, _bar(t0 + 360, 3997.5, 4000.5)]
    assert veto.simulate(1, t0, 1.0, lose, 1.0, 2.0, clock, [])["r"] == -1.0
    short = veto.simulate(-1, t0, 1.0, lose, 1.0, 2.0, clock, [])
    assert short["exit"] == "target" and short["r"] == 2.0
    assert veto.simulate(1, t0, 1.0, flat, 1.0, 2.0, clock, []) is None  # not finished yet
    assert veto.simulate(1, t0, None, win, 1.0, 2.0, clock, []) is None
    assert veto.simulate(1, t0 - 3600, 1.0, win, 1.0, 2.0, clock, []) is None  # no bar after it
    assert [veto.classify(s, r) for s, r in (
        ("sent", "executed"), ("wait", "wait"), ("gated", "confidence_0.50"),
        ("gated", "spread_above_cap"), ("flat", "breaker_open"),
    )] == ["taken", "ai_veto", "ai_veto", "code_block", "ai_down"]  # fmt: skip


def test_veto_report_counts_what_happened_to_each_signal():
    cfg = load_config("config/demo.yaml")
    conn = connect(None)
    migrate(conn)
    strat = cfg.strategies[0]
    t0 = int(datetime(2026, 11, 17, 12, tzinfo=UTC).timestamp())
    for i in range(1, 40):  # price falls after the signal: a long would have been stopped
        px = 4000.0 - 0.3 * i
        conn.execute(
            "INSERT INTO bars VALUES (?,60,?,?,?,?,?,?,?,?,?,1,'t')",
            (cfg.account.symbol, t0 + 60 * i, px, px + 0.2, px - 0.4, px, px + 0.2, px + 0.4)
            + (px - 0.2, px + 0.2),
        )
    z = zlib.compress(b"{}")
    for cid, side, status, reason in (
        ("c1", 1, "gated", "confidence_0.50"),
        ("c2", -1, "sent", "executed"),
        ("c3", 0, "wait", "wait"),
        ("c4", 1, "running", None),
    ):
        conn.execute(
            "INSERT INTO decision_snapshots VALUES (?,?,?,?,?,?,?)",
            (cid, t0 * 1000, side, 4000.0, 4000.2, 1.0, z),
        )
        conn.execute(
            "INSERT INTO brain_cycles VALUES (?,?,?,?,NULL,?,70,'[]')",
            (cid, t0 * 1000, t0 * 1000 + 5, status, reason),
        )
    rep = veto.build(conn, cfg, strat, (t0 + 3600) * 1000)
    json.dumps(rep)
    assert rep["decisions_recorded"] == 4 and rep["signals"] == 2  # flat and unfinished excluded
    v, taken = rep["classes"]["ai_veto"], rep["classes"]["taken"]
    assert (v["n"], v["resolved"], v["wins"]) == (1, 1, 0) and v["sum_r"] < 0
    assert (taken["n"], taken["resolved"], taken["wins"]) == (1, 1, 1)
    assert rep["veto_value_r"] > 0 and "saved" in rep["verdict"] and "Too few" in rep["verdict"]
    assert rep["cases"][0]["what"] in ("ai_veto", "taken") and len(rep["cases"]) == 2
    veto.store(conn, veto.REPORT_NAME, rep, 1)
    veto.store(conn, veto.REPORT_NAME, rep, 2)  # replaced, not duplicated
    assert conn.execute("SELECT COUNT(*) FROM stored_reports").fetchone()[0] == 1
    assert data.stored_report(conn, "veto")["signals"] == 2
    assert data.stored_report(conn, "missing") is None
    empty = connect(None)
    migrate(empty)
    assert "No vetoed signal" in veto.build(empty, cfg, strat, NOW)["verdict"]


def test_telegram_tries_every_address_and_remembers_the_family_that_worked(monkeypatch):
    v4 = (socket.AF_INET, ("149.154.1.1", 443))
    v6 = (socket.AF_INET6, ("2001:db8::1", 443, 0, 0))
    assert tg.order_addresses([v4, v6], None) == [v6, v4]  # IPv6 first by default
    assert tg.order_addresses([v4, v6], socket.AF_INET) == [v4, v6]
    tried: list[int] = []

    class Sock:
        def __init__(self, family, _type):
            self.family = family

        def settimeout(self, _t):
            pass

        def connect(self, _addr):
            tried.append(self.family)
            if self.family == socket.AF_INET6:
                raise ConnectionResetError("reset")

        def close(self):
            pass

    class Ctx:
        def wrap_socket(self, sock, server_hostname):
            return ("tls", sock.family, server_hostname)

    monkeypatch.setattr(tg, "_resolve", lambda host, port=443: [v4, v6])
    monkeypatch.setattr(tg.socket, "socket", Sock)
    monkeypatch.setattr(tg.ssl, "create_default_context", lambda: Ctx())
    monkeypatch.setattr(tg, "_last_good_family", {})
    import time

    got = tg._open_tls("api.telegram.org", time.monotonic() + 5)
    assert got == ("tls", socket.AF_INET, "api.telegram.org")
    assert tried == [socket.AF_INET6, socket.AF_INET]
    tried.clear()
    tg._open_tls("api.telegram.org", time.monotonic() + 5)
    assert tried == [socket.AF_INET]  # the family that worked goes first next time
    monkeypatch.setattr(tg, "_resolve", lambda host, port=443: [v6])
    with pytest.raises(OSError):
        tg._open_tls("api.telegram.org", time.monotonic() + 5)
    with pytest.raises(TimeoutError):
        tg._open_tls("api.telegram.org", time.monotonic() - 1)

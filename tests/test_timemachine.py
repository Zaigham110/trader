"""Time machine (D-058): replay of stored bars through the strategy, risk rules and costs."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from goldtrader.backtest import timemachine as tm
from goldtrader.backtest.strategies import SignalFrame
from goldtrader.cli import _default_spec
from goldtrader.config import load_config
from goldtrader.data.synthetic import generate_bars
from goldtrader.db import connect, migrate


@pytest.fixture(scope="module")
def setup(tmp_path_factory):
    cfg = load_config("config/demo.yaml")
    conn = connect(tmp_path_factory.mktemp("tm") / "t.sqlite")
    migrate(conn)
    df = generate_bars(datetime(2026, 9, 1, tzinfo=UTC), 12, 7)
    rows = [
        (cfg.account.symbol, 60, int(r.time), r.bid_open, r.bid_high, r.bid_low, r.bid_close)
        + (r.ask_open, r.ask_high, r.ask_low, r.ask_close, int(r.ticks), "synthetic")
        for r in df.itertuples(index=False)
    ]
    conn.executemany("INSERT INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return conn, cfg, _default_spec(cfg.account.symbol)


def test_replay_is_json_ready_and_consistent(setup):
    conn, cfg, spec = setup
    res = tm.run(conn, cfg, spec, days=0)
    json.dumps(res, allow_nan=False)  # NaN / inf never reach the page
    m = res["metrics"]
    assert res["params"]["strategy_id"] == cfg.strategies[0].id
    assert res["range"]["bars"] > 1000 and len(res["candles"]) <= tm.MAX_POINTS + 1
    assert len(res["equity"]) <= tm.MAX_POINTS and len(res["trades"]) == m["n_trades"]
    assert m["n_trades"] > 0 and res["signals_seen"] >= m["n_trades"]
    assert abs(sum(t["net_pnl"] for t in res["trades"]) - m["net_pnl"]) < 0.02 * max(
        1.0, abs(m["net_pnl"])
    ) or m.get("killed")
    assert all(t["exit_s"] >= t["entry_s"] for t in res["trades"])
    assert [t["entry_s"] for t in res["trades"]] == sorted(t["entry_s"] for t in res["trades"])
    assert set(res["gate"]) == {"passed", "checks"} and res["buy_and_hold_pct"] is not None


def test_five_minute_timing_sees_fewer_signals_and_a_shorter_period_fewer_bars(setup):
    conn, cfg, spec = setup
    every = tm.run(conn, cfg, spec, days=0, timing="every_bar")
    live = tm.run(conn, cfg, spec, days=0, timing="live_5min")
    assert 0 < live["signals_seen"] < every["signals_seen"]
    week = tm.run(conn, cfg, spec, days=7)
    assert week["range"]["bars"] < every["range"]["bars"]
    assert week["range"]["end_s"] == every["range"]["end_s"]


def test_live_sampled_keeps_only_the_bars_a_five_minute_cycle_reads():
    class Always:
        id, kind = "x", "x"

        def signals(self, df):
            return SignalFrame(np.ones(len(df), dtype=np.int8), 1.0, 1.5)

    df = pd.DataFrame({"time": [60 * i for i in range(20)]})
    side = tm.LiveSampled(Always()).signals(df).side
    assert list(np.nonzero(side)[0]) == [4, 9, 14, 19]


def test_bad_parameters_and_run_bookkeeping(setup):
    conn, cfg, spec = setup
    with pytest.raises(ValueError):
        tm.run(conn, cfg, spec, timing="sometimes")
    with pytest.raises(ValueError):
        tm.run(conn, cfg, spec, strategy_id="nope")
    rid = tm.start_run(conn, {"days": 7}, 1000)
    row = conn.execute("SELECT status FROM timemachine_runs WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "running"
    tm.finish_run(conn, rid, 2000, None, "ValueError: x")
    row = conn.execute("SELECT status, error FROM timemachine_runs WHERE id=?", (rid,)).fetchone()
    assert (row["status"], row["error"]) == ("error", "ValueError: x")

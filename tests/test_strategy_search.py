"""Higher-timeframe trend strategy and the held-out strategy search (D-059)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from goldtrader.backtest import search as ss
from goldtrader.backtest import timemachine as tm
from goldtrader.backtest.strategies import TrendHTF, build_strategy
from goldtrader.cli import _default_spec
from goldtrader.config import load_config
from goldtrader.data.synthetic import generate_bars
from goldtrader.db import connect, migrate


def _frame(mid: np.ndarray) -> pd.DataFrame:
    t = 1_794_000_000 - 1_794_000_000 % 900 + 60 * np.arange(len(mid))
    return pd.DataFrame({"time": t, "bid_close": mid - 0.1, "ask_close": mid + 0.1})


def test_htf_signals_only_on_completed_bars_and_never_look_ahead():
    rng = np.random.default_rng(3)
    mid = 4000 + np.cumsum(rng.normal(0, 0.6, 6000)) + 40 * np.sin(np.arange(6000) / 500)
    df = _frame(mid)
    s = TrendHTF(minutes=15, ema_fast=5, ema_slow=12, on_cross=True)
    side = s.signals(df).side
    hits = np.nonzero(side)[0]
    assert len(hits) >= 3
    assert all((df["time"].iloc[i] + 60) % 900 == 0 for i in hits)  # the bar that closes a group
    assert set(np.unique(side)) <= {-1, 0, 1}
    assert all(
        side[a] != side[b] for a, b in zip(hits, hits[1:], strict=False)
    )  # crosses alternate
    # causal: a signal already given does not change when later bars arrive
    cut = int(hits[len(hits) // 2]) + 1
    assert np.array_equal(s.signals(df.iloc[:cut]).side, side[:cut])
    state = TrendHTF(minutes=15, ema_fast=5, ema_slow=12, on_cross=False).signals(df).side
    assert np.count_nonzero(state) >= len(hits)
    short = TrendHTF(minutes=60).signals(df.iloc[:300]).side  # not enough bars for the slow EMA
    assert not short.any()
    built = build_strategy("trend_htf", "x", {"minutes": 30.0, "on_cross": 0.0, "stop_atr": 5.0})
    assert (built.minutes, built.on_cross, built.stop_atr) == (30, False, 5.0)


@pytest.fixture(scope="module")
def setup(tmp_path_factory):
    cfg = load_config("config/demo.yaml")
    conn = connect(tmp_path_factory.mktemp("ss") / "t.sqlite")
    migrate(conn)
    df = generate_bars(datetime(2026, 9, 1, tzinfo=UTC), 12, 11)
    rows = [
        (cfg.account.symbol, 60, int(r.time), r.bid_open, r.bid_high, r.bid_low, r.bid_close)
        + (r.ask_open, r.ask_high, r.ask_low, r.ask_close, int(r.ticks), "synthetic")
        for r in df.itertuples(index=False)
    ]
    conn.executemany("INSERT INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return conn, cfg, _default_spec(cfg.account.symbol)


def test_search_ranks_in_sample_and_touches_the_held_out_part_only_for_finalists(setup):
    conn, cfg, spec = setup
    cands = [c for c in ss.grid() if c[2]["minutes"] in (5.0, 15.0) and c[2]["stop_atr"] < 6][:6]
    seen: list[str] = []
    res = ss.run_search(
        conn, cfg, spec, candidates=cands, on_trial=lambda c, m, df: seen.append(c[1])
    )
    json.dumps(res, allow_nan=False)
    n = len(ss.unique(cands, cfg))  # entries that clamp to the same stop are one candidate
    assert 0 < n <= len(cands)
    assert res["n_tried"] == n and len(res["all"]) == n + 1
    assert seen[0] == res["baseline"]["candidate"][1] and len(seen) == n + 1
    r = res["range"]
    assert r["start_s"] < r["split_s"] < r["end_s"]
    assert res["bars"]["in_sample"] > res["bars"]["held_out"] > 0
    assert "held_out" in res["baseline"] and isinstance(res["baseline"]["holds"], bool)
    assert len(res["finalists"]) <= ss.FINALISTS
    for f in res["finalists"]:
        assert f["in_sample"]["net_return_pct"] > 0
        assert f["in_sample"]["n_trades"] >= ss.MIN_TRADES_IN_SAMPLE
        assert {"held_out", "held_out_stress", "holds"} <= set(f)
    assert all("held_out" not in row for row in res["all"])  # never reported for the rest
    with pytest.raises(ValueError):
        ss.run_search(conn, cfg, spec, holdout=0.9)


def test_grid_is_fixed_and_time_machine_replays_a_candidate(setup):
    conn, cfg, spec = setup
    g = ss.grid()
    assert len(g) == len(ss.MINUTES) * 2 * len(ss.STOP_K) * len(ss.REWARD)
    assert len({c[1] for c in g}) == len(g)
    assert all(c[2]["target_atr"] > c[2]["stop_atr"] for c in g)
    c = g[0]
    out = tm.run(conn, cfg, spec, days=0, candidate=c, label=ss.describe(c))
    assert out["params"]["strategy_id"] == c[1] and out["params"]["strategy_kind"] == "trend_htf"
    assert out["params"]["label"].startswith("5-minute bars")


def test_candidates_are_reported_with_the_stop_the_engine_really_uses(setup):
    _conn, cfg, _spec = setup
    r = cfg.owner.risk
    wide = (
        "trend_htf",
        "w",
        {"minutes": 60.0, "on_cross": 0.0, "stop_atr": 19.36, "target_atr": 29.0},
    )
    eff = ss.effective(wide, cfg)
    assert eff[2]["stop_atr"] == r.stop_atr_max and eff[2]["target_atr"] == 29.0
    assert f"stop {r.stop_atr_max} x 1-minute ATR" in ss.describe(eff)
    same = (
        "trend_htf",
        "w2",
        {"minutes": 60.0, "on_cross": 0.0, "stop_atr": 11.6, "target_atr": 29.0},
    )
    assert [c[1] for c in ss.unique([wide, same], cfg)] == ["w"]
    assert len(ss.unique(ss.grid(), cfg)) < len(ss.grid())

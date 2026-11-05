from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from goldtrader.backtest.costs import CostModel
from goldtrader.backtest.engine import run_backtest, size_position
from goldtrader.backtest.metrics import compute_metrics
from goldtrader.backtest.strategies import FlatStrategy, SignalFrame, TrendATR, build_strategy
from goldtrader.config.loader import parse_config
from goldtrader.data.features import add_features
from goldtrader.data.synthetic import generate_bars
from goldtrader.types import SymbolSpec

SPEC = SymbolSpec(
    name="XAUUSDm",
    digits=2,
    point=0.01,
    trade_tick_size=0.01,
    trade_tick_value=1.0,
    trade_contract_size=100.0,
    volume_min=0.01,
    volume_max=200.0,
    volume_step=0.01,
    volume_limit=0.0,
    trade_stops_level=0,
    trade_freeze_level=0,
    trade_mode=4,
    trade_exemode=2,
    filling_mode=3,
    swap_mode=1,
    swap_long=-30.0,
    swap_short=10.0,
    swap_rollover3days=3,
    margin_initial=0.0,
    margin_hedged=0.0,
    currency_base="XAU",
    currency_profit="USD",
    currency_margin="USD",
    spread=20,
    spread_float=True,
)
START = datetime(2026, 1, 12, 0, 0, tzinfo=UTC)  # a Monday in winter time


@pytest.fixture(scope="module")
def frame():
    df = generate_bars(START, 12, seed=7, seconds=60)
    return add_features(df)


def _cfg(profile="normal", **risk):
    base = {"owner": {"profile": profile, "risk": risk}, "session": {"dst_mode": "winter"}}
    return parse_config(base)


def test_smoke_trend_normal_profile(frame):
    cfg = _cfg(risk_per_trade_pct=0.5)
    res = run_backtest(frame, TrendATR(), cfg, SPEC, equity0=5000.0)
    assert res.n_trades > 5
    tr = res.trades
    # intraday only: every trade closes on the same trading day it opened (D-020)
    assert ((tr.exit_time - tr.entry_time) < 23 * 3600).all()
    # equity reconciles to the sum of net P&L (R-054)
    assert res.equity.iloc[-1] == pytest.approx(5000.0 + tr.net_pnl.sum(), abs=1e-6)
    # no entry inside the daily break or weekend
    hours = pd.to_datetime(tr.entry_time, unit="s", utc=True).dt.hour
    assert not (
        (hours == 22)
        | ((hours == 21) & (pd.to_datetime(tr.entry_time, unit="s", utc=True).dt.minute >= 43))
    ).any()
    # all costs positive, swap zero
    assert (tr.spread_cost > 0).all() and (tr.slippage_cost > 0).all() and (tr.swap == 0).all()
    m = compute_metrics(res)
    assert m["n_trades"] == res.n_trades and "sharpe" in m and m["cost_share_of_gross"] > 0


def test_no_lookahead_prefix_invariance(frame):
    cfg = _cfg(risk_per_trade_pct=0.5)
    full = run_backtest(frame, TrendATR(), cfg, SPEC, equity0=5000.0)
    k = len(frame) // 2
    cut_time = int(frame["time"].iloc[k])
    part = run_backtest(frame.iloc[:k], TrendATR(), cfg, SPEC, equity0=5000.0)
    a = full.trades[full.trades.exit_time < cut_time - 3600].reset_index(drop=True)
    b = part.trades[part.trades.exit_time < cut_time - 3600].reset_index(drop=True)
    pd.testing.assert_frame_equal(a, b)


def test_micro_live_profile_uses_minimum_lot_and_records_risk(frame):
    cfg = parse_config(
        {
            "owner": {
                "profile": "micro_live",
                "risk": {
                    "risk_per_trade_pct": 30,
                    "daily_loss_pct": 40,
                    "weekly_loss_pct": 60,
                    "max_drawdown_pct": 60,
                    "max_volume_lots": 0.01,
                    "stop_atr_min": 0.2,
                    "stop_atr_max": 0.6,
                },
            },
            "session": {"dst_mode": "winter"},
        }
    )
    res = run_backtest(frame, TrendATR(stop_atr=0.3, target_atr=0.6), cfg, SPEC, equity0=50.0)
    assert (res.trades.volume == 0.01).all()
    assert (res.trades.risk_pct <= 30.0 + 1e-9).all()
    assert res.rejected.get("risk_per_trade", 0) >= 0  # wide-ATR days are rejected, not oversized


def test_size_position_math():
    cfg = _cfg(risk_per_trade_pct=1.0, max_volume_lots=1.0)
    vol, risk = size_position(10_000.0, stop_distance=10.0, spec=SPEC, cfg=cfg)
    # 1% of 10k = 100 USD; 10 USD stop × 100 oz = 1000 USD per lot → 0.1 lot
    assert vol == pytest.approx(0.1) and risk == pytest.approx(1.0)
    vol, risk = size_position(50.0, stop_distance=20.0, spec=SPEC, cfg=cfg)
    assert vol == 0.01 and risk == pytest.approx(40.0)  # D-017 case: min lot, risk reported


class _Forced:
    """Signal on bar 0 only; fills at bar 1 open."""

    id = "forced"
    kind = "test"

    def __init__(self, side, stop_atr=1.0, target_atr=1.0):
        self.side, self.stop_atr, self.target_atr = side, stop_atr, target_atr

    def signals(self, df):
        s = np.zeros(len(df), dtype=np.int8)
        s[0] = self.side
        return SignalFrame(s, self.stop_atr, self.target_atr)


def _hand_frame(rows):
    """rows: list of (bid_open, bid_high, bid_low, bid_close); ask = bid + 0.20."""
    t0 = int(datetime(2026, 1, 13, 10, 0, tzinfo=UTC).timestamp())
    recs = []
    for i, (o, h, l, c) in enumerate(rows):
        recs.append(
            {
                "time": t0 + 60 * i,
                "bid_open": o,
                "bid_high": h,
                "bid_low": l,
                "bid_close": c,
                "ask_open": o + 0.2,
                "ask_high": h + 0.2,
                "ask_low": l + 0.2,
                "ask_close": c + 0.2,
                "ticks": 10,
                "atr": 10.0,
                "spread_pips": 20.0,
                "spread_regime": "normal",
                "regime": "normal",
                "hour_utc": 10,
                "trend_state": 0,
            }
        )
    return pd.DataFrame(recs)


def test_stop_fills_at_worse_of_level_and_gap_open():
    df = _hand_frame(
        [
            (4000, 4001, 3999, 4000),
            (4000, 4002, 3998, 4001),
            (3985, 3986, 3980, 3982),
            (3982, 3983, 3981, 3982),
        ]
    )
    cfg = _cfg(risk_per_trade_pct=2.0, max_volume_lots=1.0, stop_atr_max=3.0)
    cost = CostModel(slippage_points=0.0)
    res = run_backtest(
        df, _Forced(+1, stop_atr=1.0, target_atr=2.0), cfg, SPEC, cost=cost, equity0=100_000
    )
    assert res.n_trades == 1
    t = res.trades.iloc[0]
    assert t.entry_price == pytest.approx(4000.2)  # bar-1 ask open
    assert t.sl == pytest.approx(3990.2)  # 1 ATR below entry
    assert t.exit_reason == "stop" and t.exit_price == pytest.approx(
        3985.0
    )  # gap: filled at open, not at 3990.2
    assert t.net_pnl < 0


def test_target_fill_and_stop_priority_in_same_bar():
    df = _hand_frame([(4000, 4001, 3999, 4000), (4000, 4001, 3999, 4000), (4000, 4030, 3980, 4000)])
    cfg = _cfg(risk_per_trade_pct=2.0, max_volume_lots=1.0, stop_atr_max=3.0)
    res = run_backtest(
        df, _Forced(+1, 1.0, 1.0), cfg, SPEC, cost=CostModel(slippage_points=0), equity0=100_000
    )
    assert res.trades.iloc[0].exit_reason == "stop"  # both hit → stop first (conservative)
    df2 = _hand_frame(
        [(4000, 4001, 3999, 4000), (4000, 4001, 3999, 4000), (4000, 4030, 3995, 4000)]
    )
    res2 = run_backtest(
        df2, _Forced(+1, 1.0, 1.0), cfg, SPEC, cost=CostModel(slippage_points=0), equity0=100_000
    )
    assert res2.trades.iloc[0].exit_reason == "target" and res2.trades.iloc[0].net_pnl > 0


def test_entry_blocked_when_spread_wide_or_in_buffer():
    df = _hand_frame([(4000, 4001, 3999, 4000)] * 4)
    df.loc[1, "spread_regime"] = "wide"
    cfg = _cfg(max_volume_lots=1.0)
    res = run_backtest(df, _Forced(+1), cfg, SPEC, equity0=100_000)
    assert res.n_trades == 0 and res.rejected == {"spread": 1}
    # pre-break buffer: 21:50 UTC winter is inside the 15-minute buffer before 21:58
    t0 = int(datetime(2026, 1, 13, 21, 50, tzinfo=UTC).timestamp())
    df2 = _hand_frame([(4000, 4001, 3999, 4000)] * 4)
    df2["time"] = [t0 + 60 * i for i in range(4)]
    res2 = run_backtest(df2, _Forced(+1), cfg, SPEC, equity0=100_000)
    assert res2.n_trades == 0 and list(res2.rejected) == ["session_pre_break_buffer"]


def test_daily_loss_limit_blocks_further_entries():
    # two forced losing trades on one day; second must be rejected once the limit is hit
    rows = [
        (4000, 4001, 3999, 4000),
        (4000, 4001, 3999, 4000),
        (3900, 3901, 3899, 3900),
        (3900, 3901, 3899, 3900),
        (3900, 3901, 3899, 3900),
        (3800, 3801, 3799, 3800),
    ]

    class Twice(_Forced):
        def signals(self, df):
            s = np.zeros(len(df), dtype=np.int8)
            s[0] = 1
            s[3] = 1
            return SignalFrame(s, 1.0, 1.0)

    cfg = _cfg(
        risk_per_trade_pct=1.0, daily_loss_pct=5.0, max_drawdown_pct=20.0, max_volume_lots=1.0
    )
    res = run_backtest(
        _hand_frame(rows), Twice(+1), cfg, SPEC, cost=CostModel(slippage_points=0), equity0=10_000
    )
    assert res.n_trades == 1 and res.rejected.get("daily_loss") == 1


def test_flat_strategy_and_factory(frame):
    cfg = _cfg()
    res = run_backtest(frame, FlatStrategy(), cfg, SPEC)
    assert res.n_trades == 0 and compute_metrics(res)["n_trades"] == 0
    s = build_strategy("trend_atr", "x", {"stop_atr": 0.8, "target_atr": 1.2})
    assert isinstance(s, TrendATR) and s.stop_atr == 0.8
    with pytest.raises(ValueError):
        build_strategy("grid", "g", {})

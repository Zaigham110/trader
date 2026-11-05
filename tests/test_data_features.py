"""Features: causality (truncation test), ATR, trend state, spread regime, vol regime, ranges."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from goldtrader.data.features import (
    ATR_PCT_CALM_DAILY,
    ATR_PCT_STRESS_DAILY,
    FEATURE_COLUMNS,
    add_features,
    bars_per_day,
    bars_per_year,
    donchian,
    session_high_low,
    wilder_atr,
)
from goldtrader.data.resample import bars_to_frame
from goldtrader.data.synthetic import generate_bars
from goldtrader.types import Bar

T0 = 1_767_571_200  # 2026-01-05 00:00:00 UTC


def make_frame(
    mids: list[float],
    seconds: int = 60,
    spread: float | list[float] = 0.2,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    start: int = T0,
) -> pd.DataFrame:
    """Bars with bid = mid - spread/2 and ask = mid + spread/2; H/L default to the close."""
    spreads = [spread] * len(mids) if isinstance(spread, (int, float)) else list(spread)
    highs = highs or mids
    lows = lows or mids
    bars = []
    for i, (m, s, h, lo) in enumerate(zip(mids, spreads, highs, lows)):
        half = s / 2
        bars.append(
            Bar(
                time=start + i * seconds,
                seconds=seconds,
                bid_open=m - half,
                bid_high=h - half,
                bid_low=lo - half,
                bid_close=m - half,
                ask_open=m + half,
                ask_high=h + half,
                ask_low=lo + half,
                ask_close=m + half,
                ticks=1,
            )
        )
    return bars_to_frame(bars)


def _same(a, b) -> bool:
    if isinstance(a, str) or isinstance(b, str):
        return a == b
    if pd.isna(a) and pd.isna(b):
        return True
    return bool(np.isclose(a, b, rtol=0, atol=1e-12))


@pytest.fixture(scope="module")
def synthetic_features() -> tuple[pd.DataFrame, pd.DataFrame]:
    df = generate_bars(datetime(2026, 1, 5, tzinfo=UTC), 3, seed=11)  # ~3 trading days of 1m bars
    return df, add_features(df, spread_hist_days=1)


@pytest.mark.parametrize("k", [0, 1, 13, 14, 59, 60, 500, 1500, 2600, -1])
def test_every_feature_is_causal_under_truncation(synthetic_features, k):
    """The strongest look-ahead guard: features at row k must not change when rows > k vanish."""
    df, full = synthetic_features
    k = len(df) - 1 if k == -1 else k
    part = add_features(df.iloc[: k + 1], spread_hist_days=1)
    for col in FEATURE_COLUMNS:
        assert _same(full[col].iloc[k], part[col].iloc[k]), (col, k)
    # donchian and session ranges too
    pd.testing.assert_frame_equal(donchian(df, 20).iloc[: k + 1], donchian(df.iloc[: k + 1], 20))
    pd.testing.assert_frame_equal(
        session_high_low(df, 0, 7).iloc[: k + 1], session_high_low(df.iloc[: k + 1], 0, 7)
    )


def test_shifted_history_leaves_the_past_untouched(synthetic_features):
    """Appending wild bars must not alter any already-computed feature value (whole-column check)."""
    df, full = synthetic_features
    cut = len(df) - 100
    wild = df.iloc[cut:].copy()
    for c in (
        "bid_open",
        "bid_high",
        "bid_low",
        "bid_close",
        "ask_open",
        "ask_high",
        "ask_low",
        "ask_close",
    ):
        wild[c] = wild[c] * 1.25
    altered = add_features(pd.concat([df.iloc[:cut], wild]), spread_hist_days=1)
    for col in FEATURE_COLUMNS:
        a, b = full[col].iloc[:cut], altered[col].iloc[:cut]
        if a.dtype == object or str(a.dtype) in ("str", "string"):
            assert (a.to_numpy() == b.to_numpy()).all(), col
        else:
            np.testing.assert_allclose(
                a.to_numpy(dtype=float), b.to_numpy(dtype=float), rtol=0, atol=1e-12, err_msg=col
            )


def test_wilder_atr_on_known_series():
    # highs/lows/closes of a tiny series; period 3
    highs = [10.0, 11.0, 12.0, 11.5, 13.0, 12.5]
    lows = [9.0, 9.5, 10.5, 10.0, 11.0, 11.5]
    closes = [9.5, 10.5, 11.0, 11.0, 12.5, 12.0]
    df = make_frame(closes, highs=highs, lows=lows)
    f = add_features(df, atr_period=3, ema_fast=2, ema_slow=3, rv_window=2, spread_hist_days=1)
    # true ranges by hand: bar0 H-L; then max(H-L, |H-pc|, |L-pc|)
    tr = [
        1.0,
        max(1.5, 1.5, 0.0),
        max(1.5, 1.5, 0.0),
        max(1.5, 0.5, 1.0),
        max(2.0, 2.0, 0.0),
        max(1.0, 0.0, 1.0),
    ]
    assert np.allclose(f["tr"], tr)
    atr2 = sum(tr[:3]) / 3  # Wilder seed = SMA of the first n true ranges
    atr3 = (atr2 * 2 + tr[3]) / 3
    atr4 = (atr3 * 2 + tr[4]) / 3
    atr5 = (atr4 * 2 + tr[5]) / 3
    assert f["atr"].iloc[:2].isna().all()
    assert np.allclose(f["atr"].iloc[2:], [atr2, atr3, atr4, atr5])
    assert np.allclose(
        f["atr_pct"].iloc[2:], np.array([atr2, atr3, atr4, atr5]) / np.array(closes[2:])
    )
    assert wilder_atr(pd.Series([], dtype=float), 3).empty


def test_trend_state_logic():
    up = list(np.linspace(100, 130, 80))  # steadily rising: fast > slow and mid > fast
    down = list(np.linspace(130, 100, 80))
    flat = [100.0] * 80
    f_up = add_features(make_frame(up), ema_fast=5, ema_slow=10, spread_hist_days=1)
    f_down = add_features(make_frame(down), ema_fast=5, ema_slow=10, spread_hist_days=1)
    f_flat = add_features(make_frame(flat), ema_fast=5, ema_slow=10, spread_hist_days=1)
    assert (f_up["trend_state"].iloc[10:] == 1).all()
    assert (f_down["trend_state"].iloc[10:] == -1).all()
    assert (f_flat["trend_state"] == 0).all()
    # before the slow EMA exists the state is 0 (never NaN)
    assert (f_up["trend_state"].iloc[:9] == 0).all()
    assert f_up["trend_state"].dtype == np.int8
    # mixed: fast above slow but price dipped below the fast EMA → 0, not +1
    mids = list(np.linspace(100, 130, 60)) + [128.0]
    f = add_features(make_frame(mids), ema_fast=5, ema_slow=10, spread_hist_days=1)
    assert f["ema_fast"].iloc[-1] > f["ema_slow"].iloc[-1] and f["trend_state"].iloc[-1] == 0


def test_spread_median_hour_and_spread_regime_with_hourly_bars():
    # one bar per hour (bar_seconds=3600) for three days; day 2 hour 10 is wide
    mids = [100.0] * 72
    spreads = [0.2] * 72
    spreads[24 + 10] = 0.5  # day 2, 10:00 → 2.5 × median of the prior day's 10:00 bar
    spreads[48 + 10] = 0.39  # day 3, 10:00 → median of {0.2, 0.5} = 0.35 → 0.39 <= 0.70 normal
    f = add_features(
        make_frame(mids, seconds=3600, spread=spreads),
        ema_fast=2,
        ema_slow=3,
        rv_window=2,
        spread_hist_days=2,
    )
    assert f["hour_utc"].tolist() == list(range(24)) * 3
    assert np.allclose(f["spread_pips"].iloc[:3], 20.0)
    # day 1: no same-hour history → unknown
    assert f["spread_median_hour"].iloc[:24].isna().all()
    assert (f["spread_regime"].iloc[:24] == "unknown").all()
    # day 2: median of the previous day's same hour
    assert f["spread_median_hour"].iloc[24 + 10] == pytest.approx(0.2)
    assert f["spread_regime"].iloc[24 + 10] == "wide"
    assert f["spread_regime"].iloc[24 + 11] == "normal"
    # day 3: median over the two prior same-hour bars (0.2, 0.5) → 0.35; current bar excluded
    assert f["spread_median_hour"].iloc[48 + 10] == pytest.approx(0.35)
    assert f["spread_regime"].iloc[48 + 10] == "normal"
    # exactly 2× the median is still normal (<=)
    spreads2 = [0.2] * 48
    spreads2[24 + 5] = 0.4
    f2 = add_features(
        make_frame(mids[:48], seconds=3600, spread=spreads2),
        ema_fast=2,
        ema_slow=3,
        rv_window=2,
        spread_hist_days=2,
    )
    assert f2["spread_regime"].iloc[24 + 5] == "normal"


def test_volatility_regime_labels_from_daily_equivalent_atr():
    bpd = bars_per_day(60)
    assert bpd == pytest.approx(23 * 60)
    assert bars_per_year(60) == pytest.approx(23 * 60 * 5 * 52)
    price = 4000.0

    def frame_with_range(rng_usd: float) -> pd.DataFrame:
        n = 40
        mids = [price] * n
        return make_frame(mids, highs=[price + rng_usd / 2] * n, lows=[price - rng_usd / 2] * n)

    def label(rng_usd: float) -> str:
        f = add_features(frame_with_range(rng_usd), spread_hist_days=1)
        return str(f["regime"].iloc[-1])

    # per-bar TR = rng_usd → ATR = rng_usd; daily equivalent = rng/price * sqrt(bpd)
    calm_rng = price * ATR_PCT_CALM_DAILY / math.sqrt(bpd) * 0.5
    stress_rng = price * ATR_PCT_STRESS_DAILY / math.sqrt(bpd) * 1.5
    normal_rng = price * (ATR_PCT_CALM_DAILY + ATR_PCT_STRESS_DAILY) / 2 / math.sqrt(bpd)
    assert label(calm_rng) == "calm"
    assert label(normal_rng) == "normal"
    assert label(stress_rng) == "stress"
    f = add_features(frame_with_range(normal_rng), spread_hist_days=1)
    assert (f["regime"].iloc[:13] == "unknown").all()  # ATR(14) needs 14 bars
    assert f["atr_pct_daily"].iloc[-1] == pytest.approx(f["atr_pct"].iloc[-1] * math.sqrt(bpd))
    # 2% daily-equivalent (Nov-2026 reference, 01_gold_trading §3) is 'normal'
    assert label(price * 0.02 / math.sqrt(bpd)) == "normal"


def test_realised_vol_scaling_and_percentiles():
    n = 200
    r = 0.001
    logp = np.cumsum(np.array([r if i % 2 else -r for i in range(n)]))
    mids = list(100.0 * np.exp(logp))
    f = add_features(make_frame(mids), rv_window=20, spread_hist_days=1)
    expected = np.std([r, -r] * 10, ddof=1) * math.sqrt(bars_per_year(60))
    assert f["rv"].iloc[20:].to_numpy() == pytest.approx(expected, rel=1e-9)
    assert f["rv"].iloc[:20].isna().all()  # 20 returns need 21 bars (first log_ret is NaN)
    assert f["log_ret"].iloc[1] == pytest.approx(logp[1] - logp[0])
    assert f["rv_pctile"].dropna().between(0, 1).all()
    assert f["atr_pctile"].dropna().between(0, 1).all()


def test_donchian_excludes_the_current_bar():
    mids = [1.0, 2.0, 3.0, 10.0, 4.0, 5.0]
    d = donchian(make_frame(mids), 3)
    assert d["donchian_high"].iloc[:3].isna().all()
    assert d["donchian_high"].iloc[3] == 3.0, "bar 3's own spike (10) must not be in its channel"
    assert d["donchian_high"].iloc[4] == 10.0
    assert d["donchian_low"].iloc[3] == 1.0 and d["donchian_low"].iloc[5] == 3.0
    with pytest.raises(ValueError):
        donchian(make_frame(mids), 0)


def test_session_high_low_asian_range():
    # hourly bars over two days; highs/lows differ from close
    n = 48
    mids = [100.0] * n
    highs = [100.0 + (i % 24) for i in range(n)]  # hour h has high 100 + h
    lows = [100.0 - (i % 24) for i in range(n)]
    df = make_frame(mids, seconds=3600, highs=highs, lows=lows)
    s = session_high_low(df, 0, 7)
    # inside the window the range builds cumulatively
    assert s["session_high"].iloc[0] == 100.0 and s["session_high"].iloc[3] == 103.0
    assert s["session_low"].iloc[3] == 97.0
    assert not s["session_complete"].iloc[6] and s["session_complete"].iloc[7]
    # after 07:00 the completed 00–07 range (hours 0..6 → high 106 / low 94) is carried
    assert (s["session_high"].iloc[7:24] == 106.0).all()
    assert (s["session_low"].iloc[7:24] == 94.0).all()
    # the next day starts afresh
    assert s["session_high"].iloc[24] == 100.0 and s["session_high"].iloc[24 + 7] == 106.0
    # a window wrapping midnight (22 → 02) belongs to the day it ends on
    w = session_high_low(df, 22, 2)
    assert w["session_high"].iloc[0] == 100.0  # day 1: only the 00:00 bar so far
    assert w["session_high"].iloc[1] == 101.0  # 00:00 (100) and 01:00 (101) so far
    assert w["session_high"].iloc[24 + 1] == 123.0  # day 2 includes day 1 22:00/23:00 bars
    assert w["session_complete"].iloc[24 + 2] and not w["session_complete"].iloc[24 + 1]


def test_empty_frame_and_bad_parameters():
    empty = bars_to_frame([], seconds=60)
    f = add_features(empty)
    assert len(f) == 0 and set(FEATURE_COLUMNS) <= set(f.columns)
    with pytest.raises(ValueError):
        add_features(make_frame([1.0, 2.0]), ema_fast=10, ema_slow=5)


def test_add_features_does_not_modify_input_and_keeps_bar_columns(synthetic_features):
    df, full = synthetic_features
    assert "atr" not in df.columns
    assert set(df.columns) <= set(full.columns)
    assert full.attrs["bar_seconds"] == 60
    assert full["regime"].isin(["calm", "normal", "stress", "unknown"]).all()
    assert full["spread_regime"].isin(["normal", "wide", "unknown"]).all()
    assert full["trend_state"].isin([-1, 0, 1]).all()

"""Resampling: 1 s sampling, bar OHLC against hand-built ticks, alignment, gaps, round trips."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from goldtrader.data.resample import (
    BAR_COLUMNS,
    TickArrays,
    bars_from_arrays,
    bars_to_frame,
    build_bars,
    frame_to_bars,
    infer_bar_seconds,
    sample_1s,
)
from goldtrader.data.synthetic import generate_gold_ticks
from goldtrader.types import Bar, Tick

T0 = 1_767_571_200  # 2026-01-05 00:00:00 UTC (Monday)


def tick(offset_ms: int, bid: float, ask: float | None = None) -> Tick:
    return Tick(time_msc=T0 * 1000 + offset_ms, bid=bid, ask=ask if ask is not None else bid + 0.2)


def test_sample_1s_keeps_last_tick_per_second_and_floors_time():
    ticks = [tick(100, 1.0), tick(900, 2.0), tick(1_500, 3.0), tick(5_250, 4.0), tick(5_999, 5.0)]
    out = sample_1s(ticks)
    assert [(t.time_msc - T0 * 1000, t.bid) for t in out] == [(0, 2.0), (1000, 3.0), (5000, 5.0)]
    assert len(out) == 3, "seconds 2, 3 and 4 had no tick and must not be filled"


def test_sample_1s_sorts_unsorted_input_stably():
    ticks = [tick(900, 2.0), tick(100, 1.0), tick(100, 1.5)]  # same ms twice: last seen wins
    out = sample_1s(ticks)
    assert out == [Tick(time_msc=T0 * 1000, bid=2.0, ask=2.2)]
    assert sample_1s([]) == []


def test_build_bars_ohlc_matches_hand_built_ticks():
    ticks = [
        tick(0, 100.0, 100.3),
        tick(10_000, 101.0, 101.2),
        tick(20_000, 99.0, 99.5),
        tick(59_999, 100.5, 100.6),  # bar 0: 4 ticks
        tick(60_000, 102.0, 102.1),  # bar 1: exactly on the boundary, 1 tick
        tick(180_000, 98.0, 98.4),
        tick(181_000, 97.0, 97.1),  # bar 3 (bar 2 is a gap)
    ]
    bars = build_bars(ticks, 60)
    assert [b.time for b in bars] == [T0, T0 + 60, T0 + 180]
    b0, b1, b3 = bars
    assert (b0.bid_open, b0.bid_high, b0.bid_low, b0.bid_close) == (100.0, 101.0, 99.0, 100.5)
    assert (b0.ask_open, b0.ask_high, b0.ask_low, b0.ask_close) == (100.3, 101.2, 99.5, 100.6)
    assert b0.ticks == 4 and b0.seconds == 60
    assert b1 == Bar(T0 + 60, 60, 102.0, 102.0, 102.0, 102.0, 102.1, 102.1, 102.1, 102.1, 1)
    assert (b3.bid_open, b3.bid_close, b3.ticks) == (98.0, 97.0, 2)
    assert b3.spread_close == pytest.approx(0.1) and b3.mid_close == pytest.approx(97.05)


def test_bars_align_to_epoch_multiples_whatever_the_first_tick():
    ticks = [tick(37_000, 1.0), tick(299_000, 2.0), tick(300_000, 3.0)]
    bars = build_bars(ticks, 300)
    assert [b.time for b in bars] == [T0, T0 + 300]
    assert all(b.time % 300 == 0 for b in bars)
    assert bars[0].ticks == 2 and bars[1].ticks == 1


def test_no_bars_during_daily_break_or_weekend_and_no_filling():
    # Monday 2026-01-05 00:00 → 8 days covers five daily breaks and one weekend
    ticks = generate_gold_ticks(datetime(2026, 1, 5, tzinfo=UTC), 8, seed=3)
    bars = build_bars(ticks, 60)
    times = np.array([b.time for b in bars])
    sod = times % 86400
    weekday = (times // 86400 + 3) % 7
    in_break = (sod >= 21 * 3600 + 58 * 60) & (sod < 23 * 3600 + 2 * 60)
    assert not in_break.any(), "no bar may open inside 21:58–23:02 UTC"
    assert not (weekday == 5).any(), "no Saturday bars"
    assert not ((weekday == 6) & (sod < 23 * 3600 + 5 * 60)).any(), "Sunday opens 23:05"
    assert not ((weekday == 4) & (sod >= 21 * 3600 + 58 * 60)).any(), "Friday closes 21:58"
    # bar-open gaps: 21:57 → 23:02 daily (65 min), Friday 21:57 → Sunday 23:05
    gaps = np.diff(times)
    assert set(gaps[gaps > 60].tolist()) == {
        65 * 60,
        2 * 86400 + (23 * 3600 + 5 * 60) - (21 * 3600 + 57 * 60),
    }
    assert all(b.ticks >= 1 for b in bars)
    # every minute with a tick has a bar and vice versa
    minutes_with_ticks = {t.time_msc // 60_000 * 60 for t in ticks}
    assert minutes_with_ticks == set(times.tolist())


def test_bars_frame_round_trip_and_columns():
    ticks = [tick(0, 100.0), tick(30_000, 101.0), tick(120_000, 99.0)]
    bars = build_bars(ticks, 60)
    df = bars_to_frame(bars)
    assert list(df.columns) == list(BAR_COLUMNS)
    assert isinstance(df.index, pd.DatetimeIndex) and str(df.index.tz) == "UTC"
    assert df.index[0] == pd.Timestamp("2026-01-05 00:00:00", tz="UTC")
    assert df["time"].dtype == "int64" and df["ticks"].dtype == "int64"
    assert df.attrs["bar_seconds"] == 60
    assert np.allclose(df["mid_close"], [101.1, 99.1]) and np.allclose(df["spread_close"], 0.2)
    assert frame_to_bars(df) == bars
    assert infer_bar_seconds(df) == 60
    # inference without attrs falls back to the smallest gap
    naked = df.copy()
    naked.attrs = {}
    assert infer_bar_seconds(naked) == 120  # only bars at T0 and T0+120 exist
    assert frame_to_bars(naked, seconds=60) == bars


def test_empty_inputs():
    assert build_bars([], 60) == []
    df = bars_to_frame([], seconds=60)
    assert len(df) == 0 and list(df.columns) == list(BAR_COLUMNS) and df.attrs["bar_seconds"] == 60
    assert frame_to_bars(df) == []


def test_mixed_bar_lengths_and_bad_seconds_are_rejected():
    a = Bar(T0, 60, 1, 1, 1, 1, 1, 1, 1, 1, 1)
    b = Bar(T0, 300, 1, 1, 1, 1, 1, 1, 1, 1, 1)
    with pytest.raises(ValueError, match="mixed"):
        bars_to_frame([a, b])
    with pytest.raises(ValueError):
        build_bars([tick(0, 1.0)], 0)
    with pytest.raises(ValueError):
        TickArrays.from_arrays(np.array([1, 2]), np.array([1.0]), np.array([1.0, 2.0]))


def test_bars_from_arrays_matches_build_bars():
    ticks = generate_gold_ticks(datetime(2026, 1, 6, tzinfo=UTC), 0.5, seed=7)
    via_list = bars_to_frame(build_bars(ticks, 300))
    via_arrays = bars_from_arrays(TickArrays.from_ticks(ticks), 300)
    pd.testing.assert_frame_equal(via_list, via_arrays)

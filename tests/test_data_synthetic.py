"""Synthetic gold ticks: schedule gaps, spread by hour, determinism, sanity and speed."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from goldtrader.data.features import add_features
from goldtrader.data.synthetic import (
    BREAK_START_S,
    REOPEN_S,
    SUNDAY_OPEN_S,
    generate_bars,
    generate_gold_tick_arrays,
    generate_gold_ticks,
    is_open_winter,
    open_mask_winter,
)

MONDAY = datetime(2026, 1, 5, tzinfo=UTC)


@pytest.fixture(scope="module")
def week_arrays():
    return generate_gold_tick_arrays(MONDAY, 8, seed=42)


def test_deterministic_for_a_seed_and_different_across_seeds():
    a = generate_gold_ticks(MONDAY, 0.25, seed=1)
    b = generate_gold_ticks(MONDAY, 0.25, seed=1)
    c = generate_gold_ticks(MONDAY, 0.25, seed=2)
    assert a == b
    assert len(a) == len(c) and a != c
    arr = generate_gold_tick_arrays(MONDAY, 0.25, seed=1)
    assert arr.time_msc.tolist() == [t.time_msc for t in a]
    assert arr.bid.tolist() == [t.bid for t in a]


def test_no_ticks_in_daily_break_or_weekend(week_arrays):
    t = week_arrays.time_msc // 1000
    sod = t % 86400
    weekday = (t // 86400 + 3) % 7
    assert not ((sod >= BREAK_START_S) & (sod < REOPEN_S)).any()
    assert not (weekday == 5).any()
    assert not ((weekday == 6) & (sod < SUNDAY_OPEN_S)).any()
    assert not ((weekday == 4) & (sod >= BREAK_START_S)).any()
    # ticks exist right up to the break and from the reopen
    assert ((weekday == 0) & (sod == BREAK_START_S - 1)).any()
    assert ((weekday == 0) & (sod == REOPEN_S)).any()
    assert ((weekday == 6) & (sod == SUNDAY_OPEN_S)).any()
    # one tick per second while open, strictly increasing times
    assert (np.diff(week_arrays.time_msc) > 0).all()
    expected_open = open_mask_winter(
        np.arange(int(MONDAY.timestamp()), int(MONDAY.timestamp()) + 8 * 86400)
    ).sum()
    assert len(week_arrays) == expected_open


def test_scalar_schedule_matches_the_mask():
    assert is_open_winter(MONDAY + timedelta(hours=10))
    assert not is_open_winter(MONDAY + timedelta(hours=22))  # daily break
    assert is_open_winter(MONDAY + timedelta(hours=23, minutes=2))
    assert not is_open_winter(MONDAY + timedelta(days=5, hours=12))  # Saturday
    assert not is_open_winter(MONDAY + timedelta(days=6, hours=23, minutes=4))
    assert is_open_winter(MONDAY + timedelta(days=6, hours=23, minutes=5))  # Sunday open
    assert not is_open_winter(MONDAY + timedelta(days=4, hours=21, minutes=58))  # Friday close
    assert is_open_winter(datetime(2026, 1, 5, 10, 0))  # naive datetimes are UTC


def test_schedule_override_callable():
    always = generate_gold_tick_arrays(MONDAY, 1, seed=1, schedule=lambda dt: True)
    assert len(always) == 86400
    never = generate_gold_tick_arrays(MONDAY, 1, seed=1, schedule=lambda dt: False)
    assert len(never) == 0
    mornings = generate_gold_tick_arrays(MONDAY, 1, seed=1, schedule=lambda dt: dt.hour < 6)
    assert len(mornings) == 6 * 3600
    assert (((mornings.time_msc // 1000) % 86400) < 6 * 3600).all()


def test_quotes_are_sane(week_arrays):
    bid, ask = week_arrays.bid, week_arrays.ask
    assert (bid > 0).all() and (ask >= bid).all()
    assert np.allclose(bid, np.round(bid, 2)) and np.allclose(ask, np.round(ask, 2))
    mid = (bid + ask) / 2
    assert 2500 < mid.min() and mid.max() < 6500  # no blow-up over a week at 2026 vol
    assert abs(mid[0] - 4000.0) < 1.0


def test_spread_pattern_by_utc_hour(week_arrays):
    t = week_arrays.time_msc // 1000
    sod = t % 86400
    hour = sod // 3600
    spread_pips = np.round((week_arrays.ask - week_arrays.bid) / 0.01)
    med = {h: float(np.median(spread_pips[hour == h])) for h in np.unique(hour)}
    for h in range(7):
        assert 30 <= med[h] <= 40, (h, med[h])
    for h in range(7, 21):
        assert 13 <= med[h] <= 17, (h, med[h])
    after_reopen = (sod >= REOPEN_S) & (sod < REOPEN_S + 300) & (((t // 86400) + 3) % 7 != 6)
    assert np.median(spread_pips[after_reopen]) >= 60
    assert spread_pips[after_reopen].min() >= 60
    later = sod >= REOPEN_S + 600
    assert np.median(spread_pips[later]) < 40
    assert spread_pips.min() >= 1


def test_intraday_seasonality_and_release_spikes():
    arr = generate_gold_tick_arrays(MONDAY, 5, seed=5)
    t = arr.time_msc // 1000
    sod = t % 86400
    mid = (arr.bid + arr.ask) / 2
    ret = np.diff(np.log(mid))
    sod = sod[1:]
    asian = ret[(sod < 7 * 3600)].std()
    overlap = ret[(sod >= 12.5 * 3600) & (sod < 16 * 3600)].std()
    assert overlap > 1.5 * asian
    # 13:30 release window has far larger moves than the minutes around it
    rel = ret[(sod >= 13 * 3600 + 30 * 60) & (sod < 13 * 3600 + 30 * 60 + 30)]
    around = ret[(sod >= 13 * 3600 + 20 * 60) & (sod < 13 * 3600 + 30 * 60)]
    assert np.abs(rel).max() > 3 * np.abs(around).max()


def test_generate_bars_builds_aligned_one_minute_bars():
    df = generate_bars(MONDAY, 1, seed=9)
    assert df.attrs["bar_seconds"] == 60
    assert (df["time"] % 60 == 0).all()
    assert (df["ticks"] == 60).all(), "one tick per second → 60 ticks per bar"
    sod = df["time"] % 86400
    assert not ((sod >= BREAK_START_S) & (sod < REOPEN_S)).any()
    assert len(df) == 24 * 60 - 64
    f = add_features(df)
    rv = f["rv"].dropna()
    assert 0.08 < rv.median() < 0.8, "annualised realised vol in a plausible range"
    five = generate_bars(MONDAY, 1, seed=9, seconds=300)
    assert (five["time"] % 300 == 0).all() and five["ticks"].max() == 300


def test_bad_arguments():
    with pytest.raises(ValueError):
        generate_gold_ticks(MONDAY, 0, seed=1)
    with pytest.raises(ValueError):
        generate_gold_ticks(MONDAY, 1, seed=1, ticks_per_second=0)


def test_multiple_ticks_per_second():
    arr = generate_gold_tick_arrays(MONDAY, 0.1, seed=1, ticks_per_second=4)
    assert len(arr) == int(0.1 * 86400 * 4)
    assert set(np.diff(arr.time_msc).tolist()) == {250}


def test_thirty_days_generate_in_a_few_seconds():
    t0 = time.perf_counter()
    arr = generate_gold_tick_arrays(MONDAY, 30, seed=1)
    elapsed = time.perf_counter() - t0
    assert len(arr) > 1_700_000
    assert elapsed < 20, f"array generation took {elapsed:.1f}s"

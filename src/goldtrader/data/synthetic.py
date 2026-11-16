"""Deterministic synthetic XAUUSD ticks for tests and dry runs (TEST_PLAN §2, no network).

The generator reproduces the *shape* of Exness gold data documented in ``docs/research`` so the
resampler, features, risk engine and backtester can be exercised offline:

* geometric random walk on the mid price whose volatility follows a slow calm / normal / stress
  regime process (hourly Markov chain; multipliers 0.55 / 1.0 / 1.9 × ``daily_vol``; 01_gold_trading
  §3: realised vol 15 % in quiet years, 30 % in 2026, 48 % in Jan-2026);
* intraday seasonality (01_gold_trading §2.4): quiet Asian hours, more range at the London open, most
  in the London/NY overlap 12:30–16:00 UTC, and release-like 30-second spikes at 13:30 UTC (08:30 ET)
  on a random half of the weekdays;
* spread by UTC hour (Assumption; 02_exness_mt5 §5 "Standard from 0.2 pips", research §2.4 "lowest
  in European hours"): ≈ 15 pips 07:00–21:00, 30–40 pips 00:00–07:00, ≈ 22 pips into the break,
  60+ pips for five minutes after the 23:02 reopen (23:05 on Sunday), ≈ 28 pips afterwards;
* **no ticks** during the Exness winter daily break 21:58–23:02 UTC, nor from Friday 21:58 to Sunday
  23:05 UTC (HoursConfig defaults). A ``schedule`` callable ``is_open(dt_utc) -> bool`` overrides the
  built-in calendar; it is evaluated once per minute and broadcast to the ticks of that minute.

Everything is vectorised with numpy; 30 days at one tick per second (≈ 1.8 M open-market ticks)
takes a few seconds, most of it building the ``Tick`` objects. ``generate_gold_tick_arrays`` returns
the raw arrays when objects are not needed. The same ``seed`` always yields the same data.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from goldtrader.data.resample import TickArrays, bars_from_arrays
from goldtrader.types import Tick

PIP = 0.01
PRICE_DECIMALS = 2

# winter Exness gold schedule in seconds of the UTC day (HoursConfig defaults)
BREAK_START_S = 21 * 3600 + 58 * 60      # 21:58:00
REOPEN_S = 23 * 3600 + 2 * 60            # 23:02:00
SUNDAY_OPEN_S = 23 * 3600 + 5 * 60       # 23:05:00
WIDE_AFTER_REOPEN_S = 5 * 60             # 60+ pip spread for 5 minutes after a reopen

REGIME_NAMES = ("calm", "normal", "stress")
REGIME_VOL_MULT = np.array([0.55, 1.0, 1.9])
# hourly transition matrix: sticky regimes, a stress period lasts a few hours to a day
REGIME_TRANSITION = np.array([
    [0.985, 0.015, 0.000],
    [0.010, 0.980, 0.010],
    [0.000, 0.030, 0.970],
])

Schedule = Callable[[datetime], bool]


# --------------------------------------------------------------------------------------------------
# schedule


def open_mask_winter(t_sec: np.ndarray) -> np.ndarray:
    """Vectorised built-in winter schedule: True where the market quotes at epoch second ``t_sec``."""
    t_sec = np.asarray(t_sec, dtype="int64")
    day = t_sec // 86400
    weekday = (day + 3) % 7            # 1970-01-01 was a Thursday; Monday = 0
    sod = t_sec % 86400
    in_break = (sod >= BREAK_START_S) & (sod < REOPEN_S)
    is_open = np.ones(len(t_sec), dtype=bool)
    is_open &= ~((weekday <= 3) & in_break)                 # Mon–Thu daily break
    is_open &= ~((weekday == 4) & (sod >= BREAK_START_S))   # Friday close
    is_open &= weekday != 5                                 # Saturday
    is_open &= ~((weekday == 6) & (sod < SUNDAY_OPEN_S))    # Sunday before the open
    return is_open


def is_open_winter(dt_utc: datetime) -> bool:
    """Scalar form of :func:`open_mask_winter` (the default ``schedule`` contract)."""
    return bool(open_mask_winter(np.array([_to_epoch_s(dt_utc)]))[0])


def _to_epoch_s(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())


def _schedule_mask(t_sec: np.ndarray, schedule: Schedule | None) -> np.ndarray:
    if schedule is None:
        return open_mask_winter(t_sec)
    minutes = t_sec // 60
    uniq, inverse = np.unique(minutes, return_inverse=True)
    per_minute = np.fromiter(
        (bool(schedule(datetime.fromtimestamp(int(m) * 60, tz=UTC))) for m in uniq),
        dtype=bool, count=len(uniq))
    return per_minute[inverse]


# --------------------------------------------------------------------------------------------------
# regime, seasonality, spread


def _regime_path(n_hours: int, rng: np.random.Generator) -> np.ndarray:
    """Hourly regime index path (0 calm, 1 normal, 2 stress) from the Markov chain."""
    states = np.empty(n_hours, dtype="int8")
    state = 1
    u = rng.random(n_hours)
    cum = np.cumsum(REGIME_TRANSITION, axis=1)
    for i in range(n_hours):
        state = int(np.searchsorted(cum[state], u[i]))
        state = min(state, 2)
        states[i] = state
    return states


def _season_mult(sod: np.ndarray) -> np.ndarray:
    """Intraday volatility multiplier by second of the UTC day."""
    h = sod / 3600.0
    return np.select(
        [h < 7.0, h < 9.0, h < 12.5, h < 16.0, h < 21.0, h < 23.0],
        [0.60, 1.30, 1.00, 1.60, 0.90, 0.70],
        default=0.80,
    )


def _release_mask(t_sec: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """30-second window from 13:30:00 UTC on a random half of the weekdays."""
    day = t_sec // 86400
    sod = t_sec % 86400
    days = np.unique(day)
    weekday = (days + 3) % 7
    hit = (rng.random(len(days)) < 0.5) & (weekday <= 4)
    hit_days = days[hit]
    in_window = (sod >= 13 * 3600 + 30 * 60) & (sod < 13 * 3600 + 30 * 60 + 30)
    return in_window & np.isin(day, hit_days)


def _spread(t_sec: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Spread in USD by UTC hour with small noise, rounded to a pip (always >= 1 pip)."""
    sod = t_sec % 86400
    weekday = ((t_sec // 86400) + 3) % 7
    h = sod / 3600.0
    base = np.select(
        [h < 7.0, h < 21.0, sod < BREAK_START_S, sod < REOPEN_S],
        [0.35, 0.15, 0.22, 0.22],
        default=0.28,
    )
    after_reopen = ((weekday != 6) & (sod >= REOPEN_S) & (sod < REOPEN_S + WIDE_AFTER_REOPEN_S)) | (
        (weekday == 6) & (sod >= SUNDAY_OPEN_S) & (sod < SUNDAY_OPEN_S + WIDE_AFTER_REOPEN_S))
    base = np.where(after_reopen, 0.70, base)
    # Asian hours wander between 30 and 40 pips; elsewhere ± 1–2 pips of noise
    asian_noise = rng.uniform(-0.05, 0.05, len(t_sec))
    other_noise = rng.normal(0.0, 0.012, len(t_sec))
    spread = base + np.where(h < 7.0, asian_noise, other_noise)
    spread = np.where(after_reopen, base + np.abs(rng.normal(0.0, 0.08, len(t_sec))), spread)
    spread = np.round(spread / PIP) * PIP
    return np.maximum(spread, PIP)


# --------------------------------------------------------------------------------------------------
# public API


def generate_gold_tick_arrays(
    start_utc: datetime,
    days: float,
    seed: int,
    *,
    base_price: float = 4000.0,
    daily_vol: float = 0.018,
    ticks_per_second: int = 1,
    schedule: Schedule | None = None,
) -> TickArrays:
    """Fast core of :func:`generate_gold_ticks`: returns sorted tick arrays (no ``Tick`` objects)."""
    if days <= 0 or ticks_per_second < 1:
        raise ValueError("days must be > 0 and ticks_per_second >= 1")
    rng = np.random.default_rng(seed)
    start_s = _to_epoch_s(start_utc)
    n = round(days * 86400 * ticks_per_second)
    step_ms = 1000.0 / ticks_per_second
    time_msc = start_s * 1000 + np.floor(np.arange(n) * step_ms).astype("int64")
    t_sec = time_msc // 1000

    # draw every random stream for the full grid first so results do not depend on the schedule
    hours = (t_sec - start_s) // 3600
    regimes = _regime_path(int(hours.max()) + 1, rng)[hours]
    release = _release_mask(t_sec, rng)
    season = _season_mult(t_sec % 86400) * np.where(release, 10.0, 1.0)
    ticks_per_day = 23.0 * 3600.0 * ticks_per_second
    sigma = daily_vol * REGIME_VOL_MULT[regimes] * season / math.sqrt(ticks_per_day)
    z = rng.standard_normal(n)
    spread = _spread(t_sec, rng)

    is_open = _schedule_mask(t_sec, schedule)
    time_msc, sigma, z, spread = time_msc[is_open], sigma[is_open], z[is_open], spread[is_open]
    log_mid = math.log(base_price) + np.cumsum(sigma * z)
    mid = np.exp(log_mid)
    bid = np.round(mid - spread / 2.0, PRICE_DECIMALS)
    ask = np.round(bid + spread, PRICE_DECIMALS)
    return TickArrays.from_arrays(time_msc, bid, ask)


def generate_gold_ticks(
    start_utc: datetime,
    days: float,
    seed: int,
    *,
    base_price: float = 4000.0,
    daily_vol: float = 0.018,
    ticks_per_second: int = 1,
    schedule: Schedule | None = None,
) -> list[Tick]:
    """Deterministic synthetic gold ticks from ``start_utc`` for ``days`` days (see module doc).

    ``daily_vol`` is the one-day standard deviation of log returns in the *normal* regime (0.018 ≈
    29 % annualised, the 2026 level). ``schedule`` overrides the built-in winter calendar.
    """
    arr = generate_gold_tick_arrays(start_utc, days, seed, base_price=base_price,
                                    daily_vol=daily_vol, ticks_per_second=ticks_per_second,
                                    schedule=schedule)
    times = arr.time_msc.tolist()
    bids = arr.bid.tolist()
    asks = arr.ask.tolist()
    return [Tick(time_msc=t, bid=b, ask=a) for t, b, a in zip(times, bids, asks)]


def generate_bars(
    start_utc: datetime,
    days: float,
    seed: int,
    *,
    seconds: int = 60,
    base_price: float = 4000.0,
    daily_vol: float = 0.018,
    ticks_per_second: int = 1,
    schedule: Schedule | None = None,
) -> pd.DataFrame:
    """Synthetic bars frame (default 1 m) built with the production resampler from synthetic ticks."""
    arr = generate_gold_tick_arrays(start_utc, days, seed, base_price=base_price,
                                    daily_vol=daily_vol, ticks_per_second=ticks_per_second,
                                    schedule=schedule)
    return bars_from_arrays(arr, seconds)

"""Deterministic market features on the bars frame (ARCHITECTURE §2.1 ``market``, §3.1 step 1).

**Causality rule (TEST_PLAN §2 look-ahead guard, learning-pipeline leakage rule).** Every value in row
``k`` is computed from rows ``<= k`` only: rolling windows end at the current bar, EMAs are recursive,
and the per-hour spread history is shifted by one bar. Consequently truncating the frame at any row
and recomputing leaves the features of the kept rows unchanged; ``tests/test_data_features.py``
checks exactly that. Never add a feature here that uses ``shift(-n)``, centred windows, full-sample
statistics (``df.x.mean()``, quantiles of the whole column) or anything else that peeks forward.

Time scaling (Assumption): bars per trading day = 23 h × 3600 / bar_seconds and bars per year =
23 h × 5 days × 52 weeks, i.e. 5,980 trading hours; the Exness gold week is Sun 23:05 → Fri 21:58 UTC
with a 64-minute break each day (docs/research/02_exness_mt5.md).

Regime thresholds (Assumption, docs/RESEARCH.md §1.4 and docs/research/01_gold_trading.md §3.3):
ATR(14)/price daily-equivalent ``< 1.2 %`` → ``calm``, ``> 2.5 %`` → ``stress``, else ``normal``
(Nov-2026 reference: ATR14 ≈ 81 USD ≈ 2.0 % of price, realised vol ≈ 30 %). An intrabar ATR is
converted to a daily-equivalent by ``sqrt(bars_per_day)`` scaling (random-walk assumption). A causal
rolling percentile of the same quantity, ``atr_pctile``, is provided alongside for percentile-based
gating (ARCHITECTURE §2.2 ``features``: "regime labels (ATR percentile, realised-vol percentile,
trend state)").
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from goldtrader.data.resample import infer_bar_seconds

TRADING_HOURS_PER_DAY = 23.0
TRADING_DAYS_PER_WEEK = 5
WEEKS_PER_YEAR = 52
TRADING_SECONDS_PER_YEAR = TRADING_HOURS_PER_DAY * 3600 * TRADING_DAYS_PER_WEEK * WEEKS_PER_YEAR
PIP = 0.01                       # XAUUSD pip size on Exness (Verified, 02_exness_mt5.md §5)

ATR_PCT_CALM_DAILY = 0.012       # Assumption: < 1.2 % of price per day → calm
ATR_PCT_STRESS_DAILY = 0.025     # Assumption: > 2.5 % of price per day → stress
SPREAD_WIDE_MULTIPLE = 2.0       # spread_regime: wide when > 2 × same-hour median (RiskLimits default)

FEATURE_COLUMNS: tuple[str, ...] = (
    "mid_close", "mid_high", "mid_low", "tr", "atr", "atr_pct", "atr_pct_daily", "atr_pctile",
    "ema_fast", "ema_slow", "trend_state", "log_ret", "rv", "rv_pctile", "spread_pips", "hour_utc",
    "spread_median_hour", "spread_regime", "regime",
)


def bars_per_day(bar_seconds: int) -> float:
    """Bars in one 23-hour Exness trading day."""
    return TRADING_HOURS_PER_DAY * 3600.0 / bar_seconds


def bars_per_year(bar_seconds: int) -> float:
    """Bars in one trading year (23 h × 5 days × 52 weeks)."""
    return TRADING_SECONDS_PER_YEAR / bar_seconds


def _mid_columns(df: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid_close = (df["bid_close"] + df["ask_close"]) / 2.0
    mid_high = (df["bid_high"] + df["ask_high"]) / 2.0
    mid_low = (df["bid_low"] + df["ask_low"]) / 2.0
    return mid_close, mid_high, mid_low


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """Wilder true range; the first bar (no previous close) uses ``high - low``. Causal."""
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(
        axis=1)
    tr.iloc[:1] = (high - low).iloc[:1]
    return tr


def wilder_atr(tr: pd.Series, period: int) -> pd.Series:
    """Wilder ATR: SMA of the first ``period`` true ranges, then ``atr = (prev*(n-1) + tr)/n``.

    NaN for the first ``period - 1`` bars. Causal (recursive).
    """
    if period < 1:
        raise ValueError("atr_period must be >= 1")
    if len(tr) == 0:
        return tr.astype("float64")
    seed = tr.rolling(period, min_periods=period).mean()
    s = tr.astype("float64").copy()
    s.iloc[: period - 1] = np.nan
    if len(s) >= period:
        s.iloc[period - 1] = seed.iloc[period - 1]
    return s.ewm(alpha=1.0 / period, adjust=False, ignore_na=False).mean()


def ema(x: pd.Series, span: int) -> pd.Series:
    """Exponential moving average (``alpha = 2/(span+1)``), recursive form, NaN before ``span`` bars."""
    return x.ewm(span=span, adjust=False, min_periods=span).mean()


def trend_state(mid: pd.Series, ema_fast_: pd.Series, ema_slow_: pd.Series) -> pd.Series:
    """+1 if ``ema_fast > ema_slow`` and ``mid > ema_fast``; -1 for the mirror; 0 otherwise / NaN."""
    up = (ema_fast_ > ema_slow_) & (mid > ema_fast_)
    down = (ema_fast_ < ema_slow_) & (mid < ema_fast_)
    state = np.where(up, 1, np.where(down, -1, 0)).astype("int8")
    return pd.Series(state, index=mid.index)


def rolling_pctile(x: pd.Series, window: int, min_periods: int) -> pd.Series:
    """Percentile rank (0–1) of the current value within the trailing ``window`` values. Causal."""
    return x.rolling(window, min_periods=min_periods).rank(pct=True)


def spread_median_by_hour(spread: pd.Series, hour: pd.Series, window_bars: int,
                          min_periods: int) -> pd.Series:
    """Rolling median of ``spread`` over *prior* bars that share the same UTC hour.

    For each hour-of-day the series is shifted by one bar (the current bar is excluded) and a
    trailing median over ``window_bars`` same-hour bars is taken, so a value at row ``k`` uses rows
    ``< k`` only. NaN until ``min_periods`` same-hour bars have been seen.
    """
    def _per_hour(s: pd.Series) -> pd.Series:
        return s.shift(1).rolling(window_bars, min_periods=min_periods).median()

    return spread.groupby(hour.to_numpy(), group_keys=False).transform(_per_hour)


def add_features(
    df: pd.DataFrame,
    *,
    atr_period: int = 14,
    ema_fast: int = 20,
    ema_slow: int = 50,
    rv_window: int = 60,
    spread_hist_days: int = 20,
    bar_seconds: int | None = None,
) -> pd.DataFrame:
    """Return a copy of the bars frame with the :data:`FEATURE_COLUMNS` added. All features causal.

    Columns:
      * ``mid_close``, ``mid_high``, ``mid_low`` — mid prices from the bid/ask OHLC.
      * ``tr``, ``atr`` — true range on mid prices and Wilder ATR(``atr_period``).
      * ``atr_pct`` — ``atr / mid_close``; ``atr_pct_daily`` — the same scaled to a daily-equivalent
        by ``sqrt(bars_per_day)``; ``atr_pctile`` — causal rolling percentile of ``atr_pct`` over the
        trailing ``spread_hist_days`` of bars.
      * ``ema_fast``, ``ema_slow``, ``trend_state`` (+1 / 0 / −1, see :func:`trend_state`).
      * ``log_ret`` — ``ln(mid_close / prev mid_close)``; ``rv`` — annualised realised volatility,
        ``std(log_ret) over rv_window × sqrt(bars_per_year)``; ``rv_pctile`` — causal rolling
        percentile of ``rv`` over the trailing ``spread_hist_days`` of bars.
      * ``spread_pips`` — ``spread_close / 0.01``; ``hour_utc``; ``spread_median_hour`` — median
        spread of prior same-hour bars over the trailing ``spread_hist_days`` sessions;
        ``spread_regime`` — ``'normal'`` if ``spread_close <= 2 × spread_median_hour``, ``'wide'``
        otherwise, ``'unknown'`` while the median has no history (fail closed: callers must treat
        ``unknown`` like ``wide``).
      * ``regime`` — ``'calm'`` / ``'normal'`` / ``'stress'`` from ``atr_pct_daily`` and the module
        thresholds, ``'unknown'`` while ATR is NaN.

    ``bar_seconds`` is taken from ``df.attrs['bar_seconds']`` or inferred from the bar times when not
    given. The input frame is not modified.
    """
    if ema_fast >= ema_slow:
        raise ValueError("ema_fast must be shorter than ema_slow")
    out = df.copy()
    if len(out) == 0:
        for c in FEATURE_COLUMNS:
            out[c] = pd.Series([], dtype="float64")
        return out
    secs = int(bar_seconds) if bar_seconds is not None else infer_bar_seconds(out)
    bpd = bars_per_day(secs)
    bph = 3600.0 / secs
    hist_window = max(round(spread_hist_days * bpd), 2)

    mid_close, mid_high, mid_low = _mid_columns(out)
    out["mid_close"] = mid_close
    out["mid_high"] = mid_high
    out["mid_low"] = mid_low

    out["tr"] = true_range(mid_high, mid_low, mid_close)
    out["atr"] = wilder_atr(out["tr"], atr_period)
    out["atr_pct"] = out["atr"] / mid_close
    out["atr_pct_daily"] = out["atr_pct"] * math.sqrt(bpd)
    out["atr_pctile"] = rolling_pctile(out["atr_pct"], hist_window,
                                       min_periods=max(atr_period, 2))

    out["ema_fast"] = ema(mid_close, ema_fast)
    out["ema_slow"] = ema(mid_close, ema_slow)
    out["trend_state"] = trend_state(mid_close, out["ema_fast"], out["ema_slow"])

    out["log_ret"] = np.log(mid_close).diff()
    out["rv"] = out["log_ret"].rolling(rv_window, min_periods=rv_window).std(ddof=1) * math.sqrt(
        bars_per_year(secs))
    out["rv_pctile"] = rolling_pctile(out["rv"], hist_window, min_periods=max(rv_window, 2))

    spread = out["ask_close"] - out["bid_close"]
    out["spread_close"] = spread
    out["spread_pips"] = spread / PIP
    hour = _hour_utc(out)
    out["hour_utc"] = hour
    per_hour_window = max(round(spread_hist_days * bph), 1)
    out["spread_median_hour"] = spread_median_by_hour(spread, hour, per_hour_window,
                                                      min_periods=max(int(bph), 1))
    med = out["spread_median_hour"]
    wide_at = SPREAD_WIDE_MULTIPLE * med * (1.0 + 1e-9)   # tolerance: exactly 2x is still normal
    regime_spread = np.where(med.isna(), "unknown",
                             np.where(spread <= wide_at, "normal", "wide"))
    out["spread_regime"] = pd.Series(regime_spread, index=out.index, dtype="str")

    daily = out["atr_pct_daily"]
    regime = np.where(daily.isna(), "unknown",
                      np.where(daily < ATR_PCT_CALM_DAILY, "calm",
                               np.where(daily > ATR_PCT_STRESS_DAILY, "stress", "normal")))
    out["regime"] = pd.Series(regime, index=out.index, dtype="str")
    out.attrs["bar_seconds"] = secs
    return out


def _hour_utc(df: pd.DataFrame) -> pd.Series:
    if isinstance(df.index, pd.DatetimeIndex):
        idx = df.index.tz_convert("UTC") if df.index.tz is not None else df.index.tz_localize("UTC")
        return pd.Series(idx.hour.to_numpy(dtype="int8"), index=df.index)
    secs = df["time"].to_numpy(dtype="int64")
    return pd.Series(((secs % 86400) // 3600).astype("int8"), index=df.index)


def donchian(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """Highest mid high / lowest mid low of the *prior* ``n`` bars (current bar excluded).

    Returns a frame with ``donchian_high`` and ``donchian_low`` aligned to ``df``; the first ``n``
    rows are NaN. Causal: ``rolling(n)`` then ``shift(1)``.
    """
    if n < 1:
        raise ValueError("n must be >= 1")
    _, mid_high, mid_low = _mid_columns(df)
    return pd.DataFrame({
        "donchian_high": mid_high.rolling(n, min_periods=n).max().shift(1),
        "donchian_low": mid_low.rolling(n, min_periods=n).min().shift(1),
    }, index=df.index)


def session_high_low(df: pd.DataFrame, start_hour: int, end_hour: int) -> pd.DataFrame:
    """Running high / low of the ``[start_hour, end_hour)`` UTC window of each trading day.

    Built for an Asian-range breakout feature (e.g. ``start_hour=0, end_hour=7``). For every bar:
      * ``session_high`` / ``session_low`` — cumulative max / min of the mid highs / lows of the bars
        of that day's window seen *so far* (including the current bar when it is inside the window);
        after ``end_hour`` the completed range is carried for the rest of the day; NaN before the
        window has produced a bar;
      * ``session_complete`` — True once the bar's hour is ``>= end_hour`` (the range is final).

    A window that wraps midnight (``start_hour > end_hour``) is attributed to the day it *ends* on.
    Causal: only cumulative statistics within the day are used.
    """
    if not (0 <= start_hour <= 23 and 0 <= end_hour <= 24):
        raise ValueError("hours must be within 0..23 / 0..24")
    _, mid_high, mid_low = _mid_columns(df)
    hour = _hour_utc(df).to_numpy()
    if "time" in df.columns:
        day = df["time"].to_numpy(dtype="int64") // 86400
    else:
        day = (df.index.tz_convert("UTC").asi8 // 1_000_000_000) // 86400
    if start_hour <= end_hour:
        in_window = (hour >= start_hour) & (hour < end_hour)
        complete = hour >= end_hour
        session_day = day
    else:  # wraps midnight: bars at/after start belong to the next calendar day's session
        in_window = (hour >= start_hour) | (hour < end_hour)
        complete = (hour >= end_hour) & (hour < start_hour)
        session_day = np.where(hour >= start_hour, day + 1, day)
    hi = mid_high.where(pd.Series(in_window, index=df.index))
    lo = mid_low.where(pd.Series(in_window, index=df.index))
    keys = pd.Series(session_day, index=df.index)
    out = pd.DataFrame({
        "session_high": hi.groupby(keys.to_numpy(), group_keys=False).cummax(),
        "session_low": lo.groupby(keys.to_numpy(), group_keys=False).cummin(),
        "session_complete": complete,
    }, index=df.index)
    # cummax/cummin skip NaN but leave NaN where the running value does not exist yet; carry the
    # completed range forward within the day.
    out["session_high"] = out["session_high"].groupby(keys.to_numpy(), group_keys=False).ffill()
    out["session_low"] = out["session_low"].groupby(keys.to_numpy(), group_keys=False).ffill()
    return out

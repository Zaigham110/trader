"""Live market data for the gateway: tick polling → 1 s samples → 1 m bars → causal features.

Mirrors the batch pipeline (`goldtrader.data`) so backtest and live use the same feature code
(TEST_PLAN §2). Bars are persisted through the repo; features are recomputed on the trailing window
after each completed minute (cheap at 1 m resolution).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import pandas as pd

from goldtrader.data.features import add_features
from goldtrader.data.resample import TickArrays, bars_from_arrays
from goldtrader.db.repo import Repo
from goldtrader.types import Bar, Tick


@dataclass
class LiveFeatures:
    time_s: int
    atr: float | None
    atr_pct: float | None
    spread_pips: float
    spread_regime: str
    regime: str
    trend_state: int
    rv: float | None
    rv_pctile: float | None
    hour_utc: int


class LiveMarket:
    """Accumulates ticks, closes 1-minute bars, keeps a rolling window of bars with features."""

    def __init__(
        self,
        symbol: str,
        repo: Repo | None,
        *,
        window_bars: int = 20 * 23 * 60,
        bar_seconds: int = 60,
        source: str = "live",
    ) -> None:
        self.symbol = symbol
        self.repo = repo
        self.bar_seconds = bar_seconds
        self.source = source
        self._ticks_current: list[Tick] = []
        self._current_bucket: int | None = None
        self._bars: deque[Bar] = deque(maxlen=window_bars)
        self._features: pd.DataFrame | None = None
        self.last_tick: Tick | None = None
        self.last_tick_wall_ms: int | None = None

    # --- warm start ---------------------------------------------------------------------
    def preload(self, bars: list[Bar]) -> None:
        for b in bars:
            self._bars.append(b)
        self._recompute()

    # --- ticks --------------------------------------------------------------------------
    def on_tick(self, tick: Tick, wall_ms: int) -> Bar | None:
        """Feed one tick (time in ms UTC). Returns the bar closed by this tick, if any."""
        if (
            self.last_tick is not None
            and tick.time_msc == self.last_tick.time_msc
            and tick.bid == self.last_tick.bid
            and tick.ask == self.last_tick.ask
        ):
            self.last_tick_wall_ms = wall_ms
            return None
        self.last_tick = tick
        self.last_tick_wall_ms = wall_ms
        bucket = (tick.time_msc // 1000) // self.bar_seconds
        closed: Bar | None = None
        if self._current_bucket is not None and bucket != self._current_bucket:
            closed = self._close_bucket()
        self._current_bucket = bucket
        self._ticks_current.append(tick)
        return closed

    def flush_if_stale(self, wall_ms: int) -> Bar | None:
        """Close the current bar when the clock has moved past its end (no tick arrived to do it)."""
        if self._current_bucket is None or not self._ticks_current:
            return None
        end_ms = (self._current_bucket + 1) * self.bar_seconds * 1000
        if wall_ms >= end_ms + 2000:
            closed = self._close_bucket()
            self._current_bucket = None
            return closed
        return None

    def _close_bucket(self) -> Bar | None:
        if not self._ticks_current:
            return None
        arr = TickArrays.from_ticks(self._ticks_current)
        df = bars_from_arrays(arr, self.bar_seconds)
        self._ticks_current = []
        if df.empty:
            return None
        row = df.iloc[0]
        bar = Bar(
            time=int(row["time"]),
            seconds=self.bar_seconds,
            bid_open=float(row["bid_open"]),
            bid_high=float(row["bid_high"]),
            bid_low=float(row["bid_low"]),
            bid_close=float(row["bid_close"]),
            ask_open=float(row["ask_open"]),
            ask_high=float(row["ask_high"]),
            ask_low=float(row["ask_low"]),
            ask_close=float(row["ask_close"]),
            ticks=int(row["ticks"]),
        )
        self._bars.append(bar)
        if self.repo is not None:
            self.repo.upsert_bars(self.symbol, [bar], self.source)
        self._recompute()
        return bar

    # --- features -----------------------------------------------------------------------
    def _recompute(self) -> None:
        if len(self._bars) < 3:
            self._features = None
            return
        from goldtrader.data.resample import bars_to_frame

        df = bars_to_frame(list(self._bars), self.bar_seconds)
        self._features = add_features(df)

    @property
    def bars(self) -> list[Bar]:
        return list(self._bars)

    def features(self) -> LiveFeatures | None:
        """Features of the last *closed* bar; live spread from the last tick."""
        if self._features is None or self._features.empty or self.last_tick is None:
            return None
        row = self._features.iloc[-1]

        def f(name: str) -> float | None:
            v = row.get(name)
            try:
                return None if v is None or pd.isna(v) else float(v)
            except TypeError:
                return None

        spread_pips = (self.last_tick.ask - self.last_tick.bid) / 0.01
        median = f("spread_median_hour")
        if median is None or median <= 0:
            regime = "unknown"
        else:
            regime = (
                "normal" if (self.last_tick.ask - self.last_tick.bid) <= 2.0 * median else "wide"
            )
        ts = f("trend_state")
        return LiveFeatures(
            time_s=int(row["time"]),
            atr=f("atr"),
            atr_pct=f("atr_pct"),
            spread_pips=spread_pips,
            spread_regime=regime,
            regime=str(row.get("regime", "unknown")),
            trend_state=int(ts) if ts is not None else 0,
            rv=f("rv"),
            rv_pctile=f("rv_pctile"),
            hour_utc=int(row.get("hour_utc", 0)),
        )

    def tick_age_s(self, wall_ms: int) -> float:
        if self.last_tick is None or self.last_tick_wall_ms is None:
            return float("inf")
        return max(0.0, (wall_ms - self.last_tick_wall_ms) / 1000.0)

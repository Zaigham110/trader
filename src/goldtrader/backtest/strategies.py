"""Deterministic strategy signals (ARCHITECTURE §3.1 step 3; AGENTS §3.2 'deterministic signal').

A strategy turns the causal features frame into a per-bar signal: desired side (+1 long, −1 short,
0 flat) plus stop and target distances in ATR multiples. Signals at bar i use only data up to and
including bar i's close; the engine acts at bar i+1's open (TEST_PLAN §2 look-ahead rule).
Martingale, grid and averaging-down are not expressible here (D-008): one signal, one position.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np
import pandas as pd

from goldtrader.data.features import session_high_low


@dataclass(frozen=True)
class SignalFrame:
    """Per-bar arrays aligned with the features frame."""

    side: np.ndarray  # int8: +1, -1, 0
    stop_atr: float
    target_atr: float


class Strategy(Protocol):
    id: str
    kind: str

    def signals(self, df: pd.DataFrame) -> SignalFrame: ...


@dataclass(frozen=True)
class FlatStrategy:
    id: str = "flat"
    kind: str = "flat"

    def signals(self, df: pd.DataFrame) -> SignalFrame:
        return SignalFrame(np.zeros(len(df), dtype=np.int8), 1.0, 1.0)


@dataclass(frozen=True)
class TrendATR:
    """EMA trend-state entries with ATR-scaled stop and target (TSMOM family, RESEARCH §1.7).

    Enter long when trend_state turns +1 (fast EMA above slow and price above fast), short on −1.
    `min_atr_pct` skips entries in dead markets; `max_atr_pctile` skips the stress regime.
    """

    id: str = "trend_atr_v1"
    kind: str = "trend_atr"
    stop_atr: float = 1.0
    target_atr: float = 1.5
    min_atr_pct: float = 0.0
    max_atr_pctile: float = 1.0
    entry_on_flip_only: bool = True

    def signals(self, df: pd.DataFrame) -> SignalFrame:
        ts = df["trend_state"].to_numpy(dtype=float)
        ts = np.nan_to_num(ts, nan=0.0).astype(np.int8)
        side = ts.copy()
        if self.entry_on_flip_only:
            prev = np.roll(ts, 1)
            prev[0] = 0
            side = np.where(ts != prev, ts, 0).astype(np.int8)
        if "atr_pct" in df:
            ok = df["atr_pct"].to_numpy(dtype=float) >= self.min_atr_pct
            side = np.where(ok, side, 0).astype(np.int8)
        if "atr_pctile" in df and self.max_atr_pctile < 1.0:
            ok = (
                np.nan_to_num(df["atr_pctile"].to_numpy(dtype=float), nan=1.0)
                <= self.max_atr_pctile
            )
            side = np.where(ok, side, 0).astype(np.int8)
        return SignalFrame(side, self.stop_atr, self.target_atr)


@dataclass(frozen=True)
class BreakoutSession:
    """Asian-range breakout: trade the first close beyond the [start,end) UTC range of the day.

    Evidence is weak (RESEARCH §1.7: vendor backtests only); included so the pipeline can test it
    as a pre-registered hypothesis under a clock-aware cost model, not because it is expected to work.
    """

    id: str = "breakout_session_v1"
    kind: str = "breakout_session"
    range_start_hour: int = 0
    range_end_hour: int = 7
    stop_atr: float = 1.0
    target_atr: float = 1.0
    trade_start_hour: int = 7
    trade_end_hour: int = 12

    def signals(self, df: pd.DataFrame) -> SignalFrame:
        rng = session_high_low(df, self.range_start_hour, self.range_end_hour)
        hi = rng.iloc[:, 0].to_numpy(dtype=float)
        lo = rng.iloc[:, 1].to_numpy(dtype=float)
        mid = df["mid_close"].to_numpy(dtype=float)
        hour = df["hour_utc"].to_numpy(dtype=int)
        in_window = (hour >= self.trade_start_hour) & (hour < self.trade_end_hour)
        side = np.zeros(len(df), dtype=np.int8)
        side[(mid > hi) & in_window] = 1
        side[(mid < lo) & in_window] = -1
        # first breakout of the day only
        day = df["time"].to_numpy(dtype=np.int64) // 86400
        seen_day = -1
        for i in range(len(side)):
            if side[i] != 0:
                if day[i] == seen_day:
                    side[i] = 0
                else:
                    seen_day = day[i]
        return SignalFrame(side, self.stop_atr, self.target_atr)


@dataclass(frozen=True)
class TrendHTF:
    """Trend entries decided on N-minute bars, executed on the 1-minute frame (D-059 research).

    The 1-minute mid closes are grouped into `minutes`-long bars; EMAs are computed on the closes
    of *completed* groups only, and a signal appears on the 1-minute bar that completes a group, so
    nothing looks ahead. `on_cross=True` signals only when the fast EMA crosses the slow one;
    otherwise it uses the same trend state as `TrendATR` (EMA order and price beyond the fast EMA).

    `stop_atr` / `target_atr` are multiples of the frame's own (1-minute) ATR, which is what the
    engine sizes with; a stop meant for an N-minute bar is roughly sqrt(N) times larger.
    """

    id: str = "trend_htf_v1"
    kind: str = "trend_htf"
    minutes: int = 15
    ema_fast: int = 20
    ema_slow: int = 50
    stop_atr: float = 4.0
    target_atr: float = 8.0
    on_cross: bool = True

    def signals(self, df: pd.DataFrame) -> SignalFrame:
        n = len(df)
        side = np.zeros(n, dtype=np.int8)
        span = int(self.minutes) * 60
        if n < 2 or span < 60:
            return SignalFrame(side, self.stop_atr, self.target_atr)
        t = df["time"].to_numpy(dtype=np.int64)
        mid = (df["bid_close"].to_numpy(dtype=float) + df["ask_close"].to_numpy(dtype=float)) / 2.0
        bucket = t // span
        # last 1-minute bar of each group; the final group counts only if it is complete
        last = np.nonzero(bucket[1:] != bucket[:-1])[0]
        if (t[-1] + 60) % span == 0:
            last = np.append(last, n - 1)
        if len(last) <= self.ema_slow:
            return SignalFrame(side, self.stop_atr, self.target_atr)
        close = pd.Series(mid[last])
        fast = close.ewm(span=self.ema_fast, adjust=False, min_periods=self.ema_fast).mean()
        slow = close.ewm(span=self.ema_slow, adjust=False, min_periods=self.ema_slow).mean()
        if self.on_cross:
            state = np.sign((fast - slow).to_numpy())
        else:
            up = (fast > slow) & (close > fast)
            down = (fast < slow) & (close < fast)
            state = np.where(up, 1, np.where(down, -1, 0)).astype(float)
        state = np.nan_to_num(state, nan=0.0).astype(np.int8)
        prev = np.roll(state, 1)
        prev[0] = 0
        flip = (state != prev) & (state != 0) & (prev != 0 if self.on_cross else True)
        side[last[flip]] = state[flip]
        return SignalFrame(side, self.stop_atr, self.target_atr)


def build_strategy(kind: str, strategy_id: str, params: dict[str, float]) -> Strategy:
    """Factory from config (StrategyConfig.kind/params). Unknown kinds raise."""
    if kind == "trend_htf":
        return TrendHTF(
            id=strategy_id,
            minutes=int(params.get("minutes", 15)),
            ema_fast=int(params.get("ema_fast", 20)),
            ema_slow=int(params.get("ema_slow", 50)),
            stop_atr=float(params.get("stop_atr", 4.0)),
            target_atr=float(params.get("target_atr", 8.0)),
            on_cross=bool(params.get("on_cross", 1.0)),
        )
    if kind == "flat":
        return FlatStrategy(id=strategy_id)
    if kind == "trend_atr":
        return TrendATR(
            id=strategy_id,
            stop_atr=float(params.get("stop_atr", 1.0)),
            target_atr=float(params.get("target_atr", 1.5)),
            min_atr_pct=float(params.get("min_atr_pct", 0.0)),
            max_atr_pctile=float(params.get("max_atr_pctile", 1.0)),
            entry_on_flip_only=bool(params.get("entry_on_flip_only", 1.0)),
        )
    if kind == "breakout_session":
        return BreakoutSession(
            id=strategy_id,
            range_start_hour=int(params.get("range_start_hour", 0)),
            range_end_hour=int(params.get("range_end_hour", 7)),
            stop_atr=float(params.get("stop_atr", 1.0)),
            target_atr=float(params.get("target_atr", 1.0)),
            trade_start_hour=int(params.get("trade_start_hour", 7)),
            trade_end_hour=int(params.get("trade_end_hour", 12)),
        )
    raise ValueError(f"unknown strategy kind {kind!r}")

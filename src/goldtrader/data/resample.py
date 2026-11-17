"""Tick → 1 s samples → bars, bid and ask kept separately (ARCHITECTURE §2.1 ``market``, TEST_PLAN §2).

Gap policy: **nothing is filled**. A 1 s sample exists only for seconds that had a tick and a bar
exists only if at least one tick fell inside it. The Exness daily break (21:58–23:02 UTC winter), the
weekend and any feed outage therefore show up as missing rows, never as flat synthetic bars. Bars
are aligned to epoch multiples of their length (a 60 s bar opens at ``time % 60 == 0``), so bars from
different runs and sources line up exactly.

All functions are pure and vectorised with numpy; input ticks need not be sorted (a stable sort is
applied so that same-millisecond ticks keep file order and "last" means the last one seen).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from goldtrader.types import Bar, Tick

BAR_COLUMNS: tuple[str, ...] = (
    "time",
    "bid_open",
    "bid_high",
    "bid_low",
    "bid_close",
    "ask_open",
    "ask_high",
    "ask_low",
    "ask_close",
    "ticks",
    "mid_close",
    "spread_close",
)


@dataclass(frozen=True, slots=True)
class TickArrays:
    """Column view of a tick list (sorted stably by time)."""

    time_msc: np.ndarray  # int64
    bid: np.ndarray  # float64
    ask: np.ndarray  # float64

    @classmethod
    def from_ticks(cls, ticks: list[Tick]) -> TickArrays:
        n = len(ticks)
        t = np.fromiter((x.time_msc for x in ticks), dtype="int64", count=n)
        b = np.fromiter((x.bid for x in ticks), dtype="float64", count=n)
        a = np.fromiter((x.ask for x in ticks), dtype="float64", count=n)
        return cls.from_arrays(t, b, a)

    @classmethod
    def from_arrays(cls, time_msc: np.ndarray, bid: np.ndarray, ask: np.ndarray) -> TickArrays:
        time_msc = np.asarray(time_msc, dtype="int64")
        bid = np.asarray(bid, dtype="float64")
        ask = np.asarray(ask, dtype="float64")
        if not (len(time_msc) == len(bid) == len(ask)):
            raise ValueError("time_msc, bid and ask must have the same length")
        if len(time_msc) > 1 and np.any(np.diff(time_msc) < 0):
            order = np.argsort(time_msc, kind="stable")
            time_msc, bid, ask = time_msc[order], bid[order], ask[order]
        return cls(time_msc, bid, ask)

    def __len__(self) -> int:
        return len(self.time_msc)


# --------------------------------------------------------------------------------------------------
# 1 s sampling


def sample_1s(ticks: list[Tick]) -> list[Tick]:
    """Last tick of every second that had one; ``time_msc`` floored to the second. No gap filling."""
    arr = TickArrays.from_ticks(ticks)
    if len(arr) == 0:
        return []
    sec = arr.time_msc // 1000
    last = np.flatnonzero(np.diff(sec) != 0)
    last = np.append(last, len(sec) - 1)
    times = (sec[last] * 1000).tolist()
    bids = arr.bid[last].tolist()
    asks = arr.ask[last].tolist()
    return [Tick(time_msc=t, bid=b, ask=a) for t, b, a in zip(times, bids, asks)]


# --------------------------------------------------------------------------------------------------
# bars


def _group_bounds(bucket: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Start indices of each run of equal values in a sorted array and the run values."""
    starts = np.flatnonzero(np.r_[True, np.diff(bucket) != 0])
    return starts, bucket[starts]


def bars_from_arrays(arr: TickArrays, seconds: int) -> pd.DataFrame:
    """Vectorised bar builder on tick arrays → bars frame (see :func:`bars_to_frame`)."""
    if seconds <= 0:
        raise ValueError("seconds must be positive")
    n = len(arr)
    if n == 0:
        return _empty_frame(seconds)
    bucket = (arr.time_msc // 1000) // seconds * seconds
    starts, times = _group_bounds(bucket)
    ends = np.append(starts[1:], n) - 1
    counts = np.diff(np.append(starts, n))
    data = {
        "time": times.astype("int64"),
        "bid_open": arr.bid[starts],
        "bid_high": np.maximum.reduceat(arr.bid, starts),
        "bid_low": np.minimum.reduceat(arr.bid, starts),
        "bid_close": arr.bid[ends],
        "ask_open": arr.ask[starts],
        "ask_high": np.maximum.reduceat(arr.ask, starts),
        "ask_low": np.minimum.reduceat(arr.ask, starts),
        "ask_close": arr.ask[ends],
        "ticks": counts.astype("int64"),
    }
    df = pd.DataFrame(data)
    df["mid_close"] = (df["ask_close"] + df["bid_close"]) / 2.0
    df["spread_close"] = df["ask_close"] - df["bid_close"]
    return _finish_frame(df, seconds)


def build_bars(ticks: list[Tick], seconds: int) -> list[Bar]:
    """Bars of ``seconds`` length aligned to epoch multiples; bid OHLC, ask OHLC and tick count.

    No gap filling: a bar exists only when at least one tick fell in ``[time, time + seconds)``.
    """
    df = bars_from_arrays(TickArrays.from_ticks(ticks), seconds)
    return frame_to_bars(df, seconds=seconds)


def _empty_frame(seconds: int) -> pd.DataFrame:
    df = pd.DataFrame(
        {
            c: pd.Series([], dtype="int64" if c in ("time", "ticks") else "float64")
            for c in BAR_COLUMNS
        }
    )
    return _finish_frame(df, seconds)


def _finish_frame(df: pd.DataFrame, seconds: int) -> pd.DataFrame:
    idx = pd.to_datetime(df["time"].to_numpy(dtype="int64"), unit="s", utc=True)
    df.index = pd.DatetimeIndex(idx, name="dt")
    df.attrs["bar_seconds"] = int(seconds)
    return df


def bars_to_frame(bars: list[Bar], seconds: int | None = None) -> pd.DataFrame:
    """Bars → frame with the :data:`BAR_COLUMNS` and a UTC ``DatetimeIndex`` named ``dt``.

    ``df.attrs["bar_seconds"]`` records the bar length (taken from the bars, or ``seconds`` when the
    list is empty).
    """
    if bars:
        secs = {b.seconds for b in bars}
        if len(secs) != 1:
            raise ValueError(f"mixed bar lengths: {sorted(secs)}")
        bar_seconds = secs.pop()
        if seconds is not None and seconds != bar_seconds:
            raise ValueError(f"bars are {bar_seconds}s but seconds={seconds}")
    else:
        bar_seconds = seconds if seconds is not None else 60
        return _empty_frame(bar_seconds)
    n = len(bars)
    cols: dict[str, np.ndarray] = {
        "time": np.fromiter((b.time for b in bars), dtype="int64", count=n),
        "ticks": np.fromiter((b.ticks for b in bars), dtype="int64", count=n),
    }
    for name in (
        "bid_open",
        "bid_high",
        "bid_low",
        "bid_close",
        "ask_open",
        "ask_high",
        "ask_low",
        "ask_close",
    ):
        cols[name] = np.fromiter((getattr(b, name) for b in bars), dtype="float64", count=n)
    df = pd.DataFrame(cols)
    df["mid_close"] = (df["ask_close"] + df["bid_close"]) / 2.0
    df["spread_close"] = df["ask_close"] - df["bid_close"]
    df = df[list(BAR_COLUMNS)]
    return _finish_frame(df, bar_seconds)


def infer_bar_seconds(df: pd.DataFrame) -> int:
    """Bar length from ``df.attrs['bar_seconds']``, else the smallest positive gap between bar times."""
    attr = df.attrs.get("bar_seconds")
    if attr:
        return int(attr)
    times = _times_array(df)
    if len(times) < 2:
        raise ValueError("cannot infer bar length from fewer than two bars; pass seconds=")
    diffs = np.diff(np.unique(times))
    diffs = diffs[diffs > 0]
    if len(diffs) == 0:
        raise ValueError("cannot infer bar length: all bar times equal")
    return int(diffs.min())


def _times_array(df: pd.DataFrame) -> np.ndarray:
    if "time" in df.columns:
        return df["time"].to_numpy(dtype="int64")
    if isinstance(df.index, pd.DatetimeIndex):
        return (df.index.tz_convert("UTC").asi8 // 1_000_000_000).astype("int64")
    raise ValueError("bars frame needs a 'time' column or a DatetimeIndex")


def frame_to_bars(df: pd.DataFrame, seconds: int | None = None) -> list[Bar]:
    """Inverse of :func:`bars_to_frame`. Extra (feature) columns are ignored."""
    if len(df) == 0:
        return []
    bar_seconds = int(seconds) if seconds is not None else infer_bar_seconds(df)
    times = _times_array(df).tolist()
    cols = {
        name: df[name].to_numpy(dtype="float64").tolist()
        for name in (
            "bid_open",
            "bid_high",
            "bid_low",
            "bid_close",
            "ask_open",
            "ask_high",
            "ask_low",
            "ask_close",
        )
    }
    ticks = df["ticks"].to_numpy(dtype="int64").tolist()
    return [
        Bar(
            time=times[i],
            seconds=bar_seconds,
            bid_open=cols["bid_open"][i],
            bid_high=cols["bid_high"][i],
            bid_low=cols["bid_low"][i],
            bid_close=cols["bid_close"][i],
            ask_open=cols["ask_open"][i],
            ask_high=cols["ask_high"][i],
            ask_low=cols["ask_low"][i],
            ask_close=cols["ask_close"][i],
            ticks=ticks[i],
        )
        for i in range(len(times))
    ]

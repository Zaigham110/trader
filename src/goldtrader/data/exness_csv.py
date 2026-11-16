"""Exness Personal Area tick-history CSV loader (docs/research/02_exness_mt5.md §9).

File format (Verified): columns ``"Exness","Symbol","Timestamp","Bid","Ask"``; one CSV per day for the
current month, monthly files for earlier months, yearly files before that. The timestamp *format* is
not documented (Unknown), so the parser accepts ISO 8601 with or without ``T`` / ``Z`` / an offset,
``YYYY-MM-DD HH:MM:SS[.fff]``, the MT4/MT5 style ``YYYY.MM.DD HH:MM:SS``, and epoch seconds /
milliseconds / microseconds / nanoseconds (chosen by digit count). Naive timestamps are UTC (Exness
servers are UTC+0 year-round; Verified); aware timestamps are converted to UTC.

Robustness rules (TEST_PLAN §2 "first available timestamp recorded", data quality):
  * rows with an unparseable timestamp, a non-numeric / non-positive bid or ask, or the wrong number
    of fields are skipped and counted (``malformed``);
  * rows with ``bid > ask`` are dropped and counted (``crossed``);
  * output is sorted by time (stable) and exact duplicate rows (same time, bid, ask) are removed and
    counted (``duplicates``). Two different quotes in the same millisecond are both kept.

Accepted containers: plain ``.csv``, ``.csv.gz`` and ``.zip`` holding one or more CSV members (members
are concatenated). No network access; everything is read from the local file.
"""
from __future__ import annotations

import gzip
import io
import logging
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from goldtrader.types import Tick

log = logging.getLogger(__name__)

_TIME_NAMES = ("timestamp", "time", "datetime", "date", "time_msc")
_BID_NAMES = ("bid",)
_ASK_NAMES = ("ask",)


@dataclass(slots=True)
class ReadReport:
    """Result of :func:`load_exness_ticks`: clean ticks plus data-quality counters."""

    ticks: list[Tick] = field(default_factory=list)
    rows_total: int = 0        # data lines seen (header excluded)
    malformed: int = 0         # unparseable timestamp / price, bad field count
    crossed: int = 0           # bid > ask
    duplicates: int = 0        # exact duplicate rows removed
    first_time_msc: int | None = None
    last_time_msc: int | None = None
    symbols: tuple[str, ...] = ()

    @property
    def kept(self) -> int:
        return len(self.ticks)


# --------------------------------------------------------------------------------------------------
# container handling


def _read_csv_members(path: Path) -> list[bytes]:
    """Return the raw bytes of every CSV contained in ``path`` (csv, csv.gz or zip)."""
    suffixes = [s.lower() for s in path.suffixes]
    if suffixes and suffixes[-1] == ".zip":
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist()
                     if not n.endswith("/") and not Path(n).name.startswith(".")
                     and Path(n).suffix.lower() in (".csv", ".txt", ".gz")]
            if not names:
                raise ValueError(f"{path}: zip archive contains no CSV member")
            out: list[bytes] = []
            for name in sorted(names):
                data = zf.read(name)
                if name.lower().endswith(".gz"):
                    data = gzip.decompress(data)
                out.append(data)
            return out
    if suffixes and suffixes[-1] in (".gz", ".gzip"):
        with gzip.open(path, "rb") as fh:
            return [fh.read()]
    return [path.read_bytes()]


def _count_data_lines(raw: bytes) -> int:
    """Number of non-blank lines in ``raw`` (used to count rows the CSV parser had to skip)."""
    if not raw:
        return 0
    lines = raw.split(b"\n")
    return sum(1 for ln in lines if ln.strip(b" \t\r"))


# --------------------------------------------------------------------------------------------------
# timestamp parsing


def _looks_numeric(values: pd.Series) -> pd.Series:
    """True where the (string) value is an integer or decimal number."""
    return values.str.fullmatch(r"\s*-?\d+(\.\d+)?\s*").fillna(False).astype(bool)


def _epoch_to_ms(values: pd.Series) -> np.ndarray:
    """Convert numeric epoch strings to int64 ms, picking the unit from the digit count.

    ``<= 10`` integer digits → seconds, ``11–13`` → milliseconds, ``14–16`` → microseconds,
    ``> 16`` → nanoseconds. Fractions are honoured (e.g. ``1730716200.123`` seconds).
    """
    nums = pd.to_numeric(values.str.strip(), errors="coerce").to_numpy(dtype="float64")
    digits = np.floor(np.log10(np.maximum(np.abs(nums), 1.0))).astype("int64") + 1
    scale = np.where(digits <= 10, 1_000.0,
                     np.where(digits <= 13, 1.0,
                              np.where(digits <= 16, 1e-3, 1e-6)))
    out = np.rint(nums * scale)
    return out


def parse_timestamps(values: pd.Series) -> np.ndarray:
    """Parse a column of timestamp strings into epoch milliseconds (float64, NaN = malformed).

    Vectorised fast paths: numeric epochs and strict ISO 8601 (``pd.to_datetime(format="ISO8601")``).
    Rows that fail the strict path are retried with a permissive mixed-format parse so files that
    mix ``YYYY-MM-DD HH:MM:SS.fff`` with ``YYYY.MM.DD HH:MM:SS`` or ``T``/``Z`` variants still load.
    Naive timestamps are treated as UTC; aware ones are converted to UTC.
    """
    s = values.astype("string").str.strip()
    out = np.full(len(s), np.nan, dtype="float64")
    present = s.notna().to_numpy()
    if not present.any():
        return out
    numeric = _looks_numeric(s).to_numpy() & present
    if numeric.any():
        out[numeric] = _epoch_to_ms(s[numeric])
    textual = present & ~numeric
    if textual.any():
        sub = s[textual]
        parsed = pd.to_datetime(sub, format="ISO8601", utc=True, errors="coerce")
        bad = parsed.isna().to_numpy()
        if bad.any():
            # second chance: dotted dates, odd separators, two-digit years etc.
            retry = sub[bad].str.replace(".", "-", regex=False, n=2)
            parsed_retry = pd.to_datetime(retry, format="mixed", utc=True, errors="coerce")
            parsed = parsed.copy()
            parsed.loc[bad] = parsed_retry
        ns = parsed.to_numpy(dtype="datetime64[ns]").astype("int64").astype("float64")
        ns[parsed.isna().to_numpy()] = np.nan
        out[textual] = np.floor(ns / 1_000_000.0)
    return out


# --------------------------------------------------------------------------------------------------
# frame parsing


def _pick_columns(df: pd.DataFrame) -> tuple[str, str, str, str | None]:
    """Return the (time, bid, ask, symbol-or-None) column names, header or positional."""
    lower = {str(c).strip().lower(): c for c in df.columns}
    time_col = next((lower[n] for n in _TIME_NAMES if n in lower), None)
    bid_col = next((lower[n] for n in _BID_NAMES if n in lower), None)
    ask_col = next((lower[n] for n in _ASK_NAMES if n in lower), None)
    sym_col = lower.get("symbol")
    if time_col is not None and bid_col is not None and ask_col is not None:
        return time_col, bid_col, ask_col, sym_col
    # headerless: Exness layout (vendor, symbol, timestamp, bid, ask) or a bare (timestamp, bid, ask)
    cols = list(df.columns)
    if len(cols) >= 5:
        return cols[2], cols[3], cols[4], cols[1]
    if len(cols) == 3:
        return cols[0], cols[1], cols[2], None
    raise ValueError(f"cannot locate Timestamp/Bid/Ask columns in {cols!r}")


def _has_header(raw: bytes) -> bool:
    first = raw.split(b"\n", 1)[0].decode("utf-8", errors="replace").lower()
    return "bid" in first and "ask" in first


def parse_exness_csv_bytes(raw: bytes) -> tuple[pd.DataFrame, int, int]:
    """Parse one CSV payload → (frame[time_msc, bid, ask, symbol], rows_total, malformed).

    The frame still contains crossed quotes and duplicates; :func:`load_exness_ticks` removes them.
    """
    if not raw.strip():
        return pd.DataFrame({"time_msc": pd.Series([], dtype="int64"), "bid": [], "ask": [],
                             "symbol": pd.Series([], dtype="string")}), 0, 0
    header = _has_header(raw)
    rows_total = _count_data_lines(raw) - (1 if header else 0)
    df = pd.read_csv(
        io.BytesIO(raw), header=0 if header else None, dtype="string", skip_blank_lines=True,
        on_bad_lines="skip", skipinitialspace=True, keep_default_na=False, na_values=[""],
        encoding="utf-8", encoding_errors="replace",
    )
    time_col, bid_col, ask_col, sym_col = _pick_columns(df)
    time_ms = parse_timestamps(df[time_col])
    bid = pd.to_numeric(df[bid_col].astype("string").str.strip(), errors="coerce").to_numpy("float64")
    ask = pd.to_numeric(df[ask_col].astype("string").str.strip(), errors="coerce").to_numpy("float64")
    ok = (~np.isnan(time_ms)) & np.isfinite(bid) & np.isfinite(ask) & (bid > 0) & (ask > 0)
    symbol = (df[sym_col].astype("string").str.strip() if sym_col is not None
              else pd.Series(pd.NA, index=df.index, dtype="string"))
    out = pd.DataFrame({
        "time_msc": time_ms[ok].astype("int64"),
        "bid": bid[ok],
        "ask": ask[ok],
        "symbol": symbol[ok].reset_index(drop=True),
    })
    malformed = rows_total - int(ok.sum())
    return out, rows_total, max(malformed, 0)


def load_exness_ticks(path: str | Path) -> ReadReport:
    """Load an Exness tick CSV (``.csv`` / ``.csv.gz`` / ``.zip``) with data-quality counters."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(p)
    frames: list[pd.DataFrame] = []
    report = ReadReport()
    for raw in _read_csv_members(p):
        frame, total, bad = parse_exness_csv_bytes(raw)
        frames.append(frame)
        report.rows_total += total
        report.malformed += bad
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if df.empty:
        return report

    crossed = df["bid"].to_numpy() > df["ask"].to_numpy()
    report.crossed = int(crossed.sum())
    df = df.loc[~crossed]

    df = df.sort_values("time_msc", kind="stable")
    before = len(df)
    df = df.drop_duplicates(subset=("time_msc", "bid", "ask"), keep="first")
    report.duplicates = before - len(df)

    syms = df["symbol"].dropna().unique().tolist()
    report.symbols = tuple(str(s) for s in syms)
    if report.symbols and len(report.symbols) > 1:
        log.warning("%s: %d symbols in one file: %s", p, len(report.symbols), report.symbols)

    times = df["time_msc"].to_numpy().tolist()
    bids = df["bid"].to_numpy().tolist()
    asks = df["ask"].to_numpy().tolist()
    report.ticks = [Tick(time_msc=t, bid=b, ask=a) for t, b, a in zip(times, bids, asks)]
    if report.ticks:
        report.first_time_msc = report.ticks[0].time_msc
        report.last_time_msc = report.ticks[-1].time_msc
    if report.malformed or report.crossed or report.duplicates:
        log.info("%s: kept %d of %d rows (malformed %d, crossed %d, duplicates %d)", p,
                 report.kept, report.rows_total, report.malformed, report.crossed,
                 report.duplicates)
    return report


def read_exness_ticks(path: str | Path) -> list[Tick]:
    """Read an Exness tick CSV and return clean, time-sorted, de-duplicated ticks.

    Thin wrapper over :func:`load_exness_ticks`; use that when you need the skip counters.
    """
    return load_exness_ticks(path).ticks


def ticks_to_frame(ticks: list[Tick]) -> pd.DataFrame:
    """Ticks → frame with columns ``time_msc`` (int64), ``bid``, ``ask``, ``spread`` (float64)."""
    n = len(ticks)
    time_msc = np.fromiter((t.time_msc for t in ticks), dtype="int64", count=n)
    bid = np.fromiter((t.bid for t in ticks), dtype="float64", count=n)
    ask = np.fromiter((t.ask for t in ticks), dtype="float64", count=n)
    return pd.DataFrame({"time_msc": time_msc, "bid": bid, "ask": ask, "spread": ask - bid})


def frame_to_ticks(df: pd.DataFrame) -> list[Tick]:
    """Inverse of :func:`ticks_to_frame` (extra columns are ignored)."""
    times = df["time_msc"].to_numpy(dtype="int64").tolist()
    bids = df["bid"].to_numpy(dtype="float64").tolist()
    asks = df["ask"].to_numpy(dtype="float64").tolist()
    return [Tick(time_msc=t, bid=b, ask=a) for t, b, a in zip(times, bids, asks)]

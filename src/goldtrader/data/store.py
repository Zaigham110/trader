"""Local Parquet cache for ticks and bars plus data snapshot ids (R-031).

Layout (relative to ``DataConfig.tick_dir`` / ``DataConfig.bars_dir``, both gitignored under
``data/``)::

    data/ticks/<SYMBOL>/<YYYY-MM-DD>.parquet            frames from ticks_to_frame
    data/bars/<SYMBOL>/<seconds>s/<YYYY-MM-DD>_<YYYY-MM-DD>.parquet   frames from bars_to_frame

Format: Parquet through ``pandas.to_parquet`` (engine pyarrow, declared in ``pyproject.toml``).
If pyarrow cannot be imported the functions fall back to gzip-compressed CSV (``*.csv.gz``) and keep
working; the returned path tells the caller which format was written. ``load_*`` reads either.

Snapshot ids: ``snapshot_id(...)`` is the first 16 hex digits of a SHA-256 over the *content* — the
bytes of one or more files (in the given order), or the values, column names and dtypes of a frame.
Every trial-ledger row records it so a backtest can be tied to the exact data it saw (R-031,
LEARNING_SYSTEM §1). Two frames with identical content have the same id whatever their file name.
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from goldtrader.config.schema import DataConfig
from goldtrader.data.resample import BAR_COLUMNS, infer_bar_seconds

PARQUET_AVAILABLE: bool = importlib.util.find_spec("pyarrow") is not None
_SNAPSHOT_HEX = 16
_CHUNK = 1 << 20


# --------------------------------------------------------------------------------------------------
# snapshot ids


def _hash_file(h: hashlib._Hash, path: Path) -> None:
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)


def _hash_frame(h: hashlib._Hash, df: pd.DataFrame) -> None:
    h.update(repr(list(map(str, df.columns))).encode())
    h.update(repr([str(t) for t in df.dtypes]).encode())
    h.update(str(len(df)).encode())
    if len(df):
        row_hashes = pd.util.hash_pandas_object(df, index=False).to_numpy()
        h.update(row_hashes.tobytes())
        idx = df.index
        if isinstance(idx, pd.DatetimeIndex):
            h.update(idx.asi8.tobytes())
        elif not isinstance(idx, pd.RangeIndex):
            h.update(pd.util.hash_pandas_object(idx.to_series(), index=False).to_numpy().tobytes())


def snapshot_id(source: str | Path | pd.DataFrame | Iterable[str | Path]) -> str:
    """16-hex SHA-256 of the data content: file bytes (one path or several, in order) or a frame."""
    h = hashlib.sha256()
    if isinstance(source, pd.DataFrame):
        _hash_frame(h, source)
    elif isinstance(source, (str, Path)):
        _hash_file(h, Path(source))
    else:
        paths = [Path(p) for p in source]
        if not paths:
            raise ValueError("snapshot_id needs at least one path")
        for p in paths:
            _hash_file(h, p)
    return h.hexdigest()[:_SNAPSHOT_HEX]


# --------------------------------------------------------------------------------------------------
# paths


def _slug(symbol: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", symbol)


def _ext() -> str:
    return ".parquet" if PARQUET_AVAILABLE else ".csv.gz"


def bars_path(
    cfg: DataConfig,
    symbol: str,
    seconds: int,
    start: datetime,
    end: datetime,
    root: str | Path | None = None,
) -> Path:
    """Canonical bars file path for ``symbol`` / ``seconds`` / date range under ``cfg.bars_dir``."""
    base = Path(root) / cfg.bars_dir if root is not None else Path(cfg.bars_dir)
    name = f"{_date(start)}_{_date(end)}{_ext()}"
    return base / _slug(symbol) / f"{seconds}s" / name


def ticks_path(cfg: DataConfig, symbol: str, day: datetime, root: str | Path | None = None) -> Path:
    """Canonical tick file path for one UTC day under ``cfg.tick_dir``."""
    base = Path(root) / cfg.tick_dir if root is not None else Path(cfg.tick_dir)
    return base / _slug(symbol) / f"{_date(day)}{_ext()}"


def _date(dt: datetime) -> str:
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%d")


# --------------------------------------------------------------------------------------------------
# save / load


def _is_parquet(path: Path) -> bool:
    return path.suffix.lower() == ".parquet"


def _resolve_write_path(path: Path) -> Path:
    """Switch a ``.parquet`` target to ``.csv.gz`` when pyarrow is missing (and say so)."""
    if _is_parquet(path) and not PARQUET_AVAILABLE:
        return path.with_suffix(".csv.gz")
    return path


def _write(df: pd.DataFrame, path: Path) -> Path:
    path = _resolve_write_path(Path(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    if _is_parquet(path):
        df.to_parquet(tmp, engine="pyarrow", index=False)
    else:
        df.to_csv(tmp, index=False, compression="gzip")
    tmp.replace(path)
    return path


def _read(path: Path) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        alt = path.with_suffix(".csv.gz") if _is_parquet(path) else path
        if alt != path and alt.exists():
            path = alt
        else:
            raise FileNotFoundError(path)
    if _is_parquet(path):
        return pd.read_parquet(path, engine="pyarrow")
    return pd.read_csv(path, compression="gzip")


def save_bars(df: pd.DataFrame, path: str | Path) -> Path:
    """Write a bars frame (the :data:`BAR_COLUMNS`; feature columns are kept too). Returns the path.

    The UTC index is not stored (it is rebuilt from ``time``); ``bar_seconds`` is stored as a column
    so the frame round-trips through either format. Writes atomically via a temp file.
    """
    out = df.reset_index(drop=True) if isinstance(df.index, pd.DatetimeIndex) else df.copy()
    missing = [c for c in BAR_COLUMNS if c not in out.columns]
    if missing:
        raise ValueError(f"bars frame is missing columns {missing}")
    out["bar_seconds"] = int(df.attrs.get("bar_seconds") or infer_bar_seconds(df))
    return _write(out, Path(path))


def load_bars(path: str | Path) -> pd.DataFrame:
    """Read a bars frame written by :func:`save_bars`; restores the UTC index and ``attrs``."""
    df = _read(Path(path))
    seconds = int(df["bar_seconds"].iloc[0]) if "bar_seconds" in df.columns and len(df) else None
    df = df.drop(columns=["bar_seconds"], errors="ignore")
    df["time"] = df["time"].astype("int64")
    df["ticks"] = df["ticks"].astype("int64")
    df.index = pd.DatetimeIndex(
        pd.to_datetime(df["time"].to_numpy(), unit="s", utc=True), name="dt"
    )
    if seconds is not None:
        df.attrs["bar_seconds"] = seconds
    return df


def save_ticks(df: pd.DataFrame, path: str | Path) -> Path:
    """Write a tick frame (``time_msc``, ``bid``, ``ask``[, ``spread``]). Returns the path."""
    for c in ("time_msc", "bid", "ask"):
        if c not in df.columns:
            raise ValueError(f"tick frame is missing column {c!r}")
    return _write(df.reset_index(drop=True), Path(path))


def load_ticks(path: str | Path) -> pd.DataFrame:
    """Read a tick frame written by :func:`save_ticks` (``spread`` is recomputed if absent)."""
    df = _read(Path(path))
    df["time_msc"] = df["time_msc"].astype("int64")
    if "spread" not in df.columns:
        df["spread"] = df["ask"] - df["bid"]
    return df

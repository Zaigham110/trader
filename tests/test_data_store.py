"""Parquet/CSV cache and data snapshot ids (R-031)."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from goldtrader.config.schema import DataConfig
from goldtrader.data import store
from goldtrader.data.exness_csv import ticks_to_frame
from goldtrader.data.features import add_features
from goldtrader.data.resample import bars_to_frame, build_bars, frame_to_bars
from goldtrader.data.store import (
    PARQUET_AVAILABLE,
    bars_path,
    load_bars,
    load_ticks,
    save_bars,
    save_ticks,
    snapshot_id,
    ticks_path,
)
from goldtrader.data.synthetic import generate_bars, generate_gold_ticks

MONDAY = datetime(2026, 1, 5, tzinfo=UTC)


@pytest.fixture(scope="module")
def bars() -> pd.DataFrame:
    return generate_bars(MONDAY, 0.5, seed=3)


def test_pyarrow_is_installed():
    assert PARQUET_AVAILABLE, "pyarrow is a declared dependency; parquet must be the default"


def test_snapshot_id_is_stable_and_content_sensitive(bars, tmp_path: Path):
    a = snapshot_id(bars)
    assert len(a) == 16 and int(a, 16) >= 0
    assert snapshot_id(bars.copy()) == a
    assert snapshot_id(generate_bars(MONDAY, 0.5, seed=3)) == a, "same data, same id"
    changed = bars.copy()
    changed.iloc[5, changed.columns.get_loc("bid_close")] += 0.01
    assert snapshot_id(changed) != a
    assert snapshot_id(bars.iloc[:-1]) != a
    renamed = bars.rename(columns={"ticks": "n"})
    assert snapshot_id(renamed) != a
    # files: hash of bytes, order of several files matters, name does not
    p1 = tmp_path / "one.csv"
    p2 = tmp_path / "two.csv"
    p1.write_bytes(b"a,b\n1,2\n")
    p2.write_bytes(b"a,b\n1,2\n")
    assert snapshot_id(p1) == snapshot_id(p2) == snapshot_id(str(p1))
    p3 = tmp_path / "three.csv"
    p3.write_bytes(b"a,b\n1,3\n")
    assert snapshot_id([p1, p3]) != snapshot_id([p3, p1]) != snapshot_id(p1)
    with pytest.raises(ValueError):
        snapshot_id([])


def test_save_and_load_bars_round_trip(bars, tmp_path: Path):
    path = tmp_path / "bars" / "XAUUSDm" / "60s" / "x.parquet"
    written = save_bars(bars, path)
    assert written == path and path.exists()
    loaded = load_bars(path)
    assert loaded.attrs["bar_seconds"] == 60
    assert isinstance(loaded.index, pd.DatetimeIndex) and str(loaded.index.tz) == "UTC"
    pd.testing.assert_frame_equal(loaded, bars)
    assert frame_to_bars(loaded) == frame_to_bars(bars)
    assert snapshot_id(loaded) == snapshot_id(bars)
    # feature columns (incl. strings) survive as well
    feats = add_features(bars, spread_hist_days=1)
    save_bars(feats, tmp_path / "feats.parquet")
    back = load_bars(tmp_path / "feats.parquet")
    assert back["regime"].tolist() == feats["regime"].tolist()
    assert back["trend_state"].tolist() == feats["trend_state"].tolist()


def test_save_bars_rejects_frames_without_bar_columns(tmp_path: Path):
    with pytest.raises(ValueError, match="missing columns"):
        save_bars(pd.DataFrame({"time": [1, 2]}), tmp_path / "bad.parquet")


def test_save_and_load_ticks_round_trip(tmp_path: Path):
    ticks = generate_gold_ticks(MONDAY, 0.01, seed=1)
    df = ticks_to_frame(ticks)
    p = save_ticks(df, tmp_path / "t.parquet")
    back = load_ticks(p)
    pd.testing.assert_frame_equal(back, df)
    assert build_bars(ticks, 60) == frame_to_bars(bars_to_frame(build_bars(ticks, 60)))
    with pytest.raises(ValueError):
        save_ticks(pd.DataFrame({"bid": [1.0]}), tmp_path / "bad.parquet")


def test_csv_gz_fallback_when_pyarrow_missing(bars, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(store, "PARQUET_AVAILABLE", False)
    target = tmp_path / "fallback.parquet"
    written = save_bars(bars, target)
    assert written == tmp_path / "fallback.csv.gz" and written.exists() and not target.exists()
    loaded = load_bars(written)
    pd.testing.assert_frame_equal(loaded, bars, check_exact=False)
    assert loaded.attrs["bar_seconds"] == 60
    # asking for the .parquet name finds the fallback file
    pd.testing.assert_frame_equal(load_bars(target), bars, check_exact=False)
    assert bars_path(DataConfig(), "XAUUSDm", 60, MONDAY, MONDAY).suffix == ".gz"


def test_canonical_paths():
    cfg = DataConfig()
    p = bars_path(cfg, "XAUUSDm", 60, MONDAY, datetime(2026, 2, 1, tzinfo=UTC), root="/r")
    assert p == Path("/r/data/bars/XAUUSDm/60s/2026-01-05_2026-02-01.parquet")
    t = ticks_path(cfg, "XAU/USD", MONDAY)
    assert t == Path("data/ticks/XAU_USD/2026-01-05.parquet")
    assert load_bars.__doc__ and "save_bars" in load_bars.__doc__
    with pytest.raises(FileNotFoundError):
        load_bars(Path("/nonexistent/file.parquet"))

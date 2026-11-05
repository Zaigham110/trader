"""Exness tick CSV loader: timestamp formats, malformed rows, crossed quotes, containers."""

from __future__ import annotations

import gzip
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from goldtrader.data.exness_csv import (
    frame_to_ticks,
    load_exness_ticks,
    parse_timestamps,
    read_exness_ticks,
    ticks_to_frame,
)
from goldtrader.types import Tick

HEADER = '"Exness","Symbol","Timestamp","Bid","Ask"\n'
T0_MS = 1_762_215_600_000  # 2025-11-04 00:20:00 UTC


def _write(path: Path, body: str, header: str = HEADER) -> Path:
    path.write_text(header + body, encoding="utf-8")
    return path


def test_timestamp_formats_all_map_to_the_same_millisecond():
    base = "2025-11-04 00:20:00"
    variants = pd.Series(
        [
            base,  # space separator, no fraction
            base + ".000",  # fractional seconds
            "2025-11-04T00:20:00",  # ISO with T
            "2025-11-04T00:20:00Z",  # ISO with Z
            "2025-11-04T00:20:00.000Z",  # ISO with fraction and Z
            "2025-11-04T02:20:00+02:00",  # aware, converted to UTC
            "2025.11.04 00:20:00",  # MT4/MT5 dotted date
            str(T0_MS),  # epoch milliseconds
            str(T0_MS // 1000),  # epoch seconds
            str(T0_MS * 1000),  # epoch microseconds
            f"{T0_MS / 1000:.3f}",  # fractional epoch seconds
        ]
    )
    out = parse_timestamps(variants)
    assert np.all(out == T0_MS), out


def test_fractional_milliseconds_are_kept():
    out = parse_timestamps(pd.Series(["2025-11-04 00:20:00.123", "2025-11-04T00:20:00.456Z"]))
    assert out.tolist() == [T0_MS + 123, T0_MS + 456]


def test_unparseable_timestamps_become_nan():
    out = parse_timestamps(pd.Series(["not a date", "", None, "2025-13-45 99:99:99"]))
    assert np.all(np.isnan(out))


def test_reads_exness_layout_sorted_and_typed(tmp_path: Path):
    p = _write(
        tmp_path / "XAUUSDm_2025_11_04.csv",
        (
            '"Exness","XAUUSDm","2025-11-04 00:20:01.000","4001.10","4001.30"\n'
            '"Exness","XAUUSDm","2025-11-04 00:20:00.500","4001.00","4001.20"\n'
            '"Exness","XAUUSDm","2025-11-04 00:20:00.000","4000.90","4001.10"\n'
        ),
    )
    ticks = read_exness_ticks(p)
    assert [t.time_msc for t in ticks] == [T0_MS, T0_MS + 500, T0_MS + 1000]
    assert ticks[0] == Tick(time_msc=T0_MS, bid=4000.90, ask=4001.10)
    assert all(isinstance(t.time_msc, int) and isinstance(t.bid, float) for t in ticks)
    rep = load_exness_ticks(p)
    assert rep.symbols == ("XAUUSDm",)
    assert (rep.first_time_msc, rep.last_time_msc) == (T0_MS, T0_MS + 1000)
    assert rep.rows_total == 3 and rep.malformed == 0


def test_malformed_rows_are_skipped_and_counted(tmp_path: Path):
    p = _write(
        tmp_path / "bad.csv",
        (
            "Exness,XAUUSDm,2025-11-04 00:20:00,4000.90,4001.10\n"
            "Exness,XAUUSDm,garbage,4000.90,4001.10\n"  # bad timestamp
            "Exness,XAUUSDm,2025-11-04 00:20:02,abc,4001.10\n"  # non-numeric bid
            "Exness,XAUUSDm,2025-11-04 00:20:03,4000.90\n"  # missing field
            "Exness,XAUUSDm,2025-11-04 00:20:04,4000.90,4001.10,extra,fields\n"  # too many fields
            "\n"  # blank line (ignored, not counted)
            "Exness,XAUUSDm,2025-11-04 00:20:05,0,4001.10\n"  # non-positive price
            "Exness,XAUUSDm,2025-11-04 00:20:06,4000.90,\n"  # empty ask
            "Exness,XAUUSDm,2025-11-04 00:20:07,4000.95,4001.15\n"
        ),
    )
    rep = load_exness_ticks(p)
    assert rep.kept == 2
    assert rep.rows_total == 8
    assert rep.malformed == 6
    assert [t.time_msc for t in rep.ticks] == [T0_MS, T0_MS + 7000]


def test_crossed_quotes_are_dropped_and_counted(tmp_path: Path):
    p = _write(
        tmp_path / "crossed.csv",
        (
            "Exness,XAUUSDm,2025-11-04 00:20:00,4000.90,4001.10\n"
            "Exness,XAUUSDm,2025-11-04 00:20:01,4001.50,4001.10\n"  # bid > ask
            "Exness,XAUUSDm,2025-11-04 00:20:02,4001.10,4001.10\n"  # bid == ask is allowed
        ),
    )
    rep = load_exness_ticks(p)
    assert rep.crossed == 1 and rep.kept == 2
    assert all(t.bid <= t.ask for t in rep.ticks)


def test_exact_duplicates_removed_but_same_ms_different_quotes_kept(tmp_path: Path):
    p = _write(
        tmp_path / "dups.csv",
        (
            "Exness,XAUUSDm,2025-11-04 00:20:00.000,4000.90,4001.10\n"
            "Exness,XAUUSDm,2025-11-04 00:20:00.000,4000.90,4001.10\n"  # exact duplicate
            "Exness,XAUUSDm,2025-11-04 00:20:00.000,4000.95,4001.15\n"  # same ms, new quote
        ),
    )
    rep = load_exness_ticks(p)
    assert rep.duplicates == 1 and rep.kept == 2
    assert [t.bid for t in rep.ticks] == [4000.90, 4000.95]  # stable order preserved


def test_headerless_and_three_column_files(tmp_path: Path):
    five = _write(
        tmp_path / "h5.csv", "Exness,XAUUSDm,2025-11-04 00:20:00,4000.90,4001.10\n", header=""
    )
    three = _write(tmp_path / "h3.csv", "2025-11-04 00:20:00,4000.90,4001.10\n", header="")
    assert (
        read_exness_ticks(five)
        == read_exness_ticks(three)
        == [Tick(time_msc=T0_MS, bid=4000.90, ask=4001.10)]
    )


def test_zip_and_gzip_containers(tmp_path: Path):
    body = HEADER + (
        "Exness,XAUUSDm,2025-11-04 00:20:00,4000.90,4001.10\n"
        "Exness,XAUUSDm,2025-11-04 00:20:01,4000.95,4001.15\n"
    )
    body2 = HEADER + "Exness,XAUUSDm,2025-11-04 00:20:02,4001.00,4001.20\n"
    gz = tmp_path / "ticks.csv.gz"
    with gzip.open(gz, "wt", encoding="utf-8") as fh:
        fh.write(body)
    zp = tmp_path / "ticks.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("2025-11-04b.csv", body2)
        zf.writestr("2025-11-04a.csv", body)
        zf.writestr("README.md", "not data")
    assert len(read_exness_ticks(gz)) == 2
    ticks = read_exness_ticks(zp)
    assert [t.time_msc for t in ticks] == [T0_MS, T0_MS + 1000, T0_MS + 2000]


def test_zip_without_csv_is_an_error(tmp_path: Path):
    zp = tmp_path / "empty.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("notes.md", "x")
    with pytest.raises(ValueError, match="no CSV member"):
        read_exness_ticks(zp)


def test_empty_and_missing_files(tmp_path: Path):
    empty = tmp_path / "empty.csv"
    empty.write_text("")
    assert read_exness_ticks(empty) == []
    header_only = _write(tmp_path / "header.csv", "")
    rep = load_exness_ticks(header_only)
    assert rep.kept == 0 and rep.rows_total == 0 and rep.malformed == 0
    with pytest.raises(FileNotFoundError):
        read_exness_ticks(tmp_path / "nope.csv")


def test_ticks_to_frame_and_back():
    ticks = [
        Tick(time_msc=T0_MS, bid=4000.9, ask=4001.1),
        Tick(time_msc=T0_MS + 1, bid=4000.8, ask=4001.0),
    ]
    df = ticks_to_frame(ticks)
    assert list(df.columns) == ["time_msc", "bid", "ask", "spread"]
    assert df["time_msc"].dtype == "int64"
    assert np.allclose(df["spread"], [0.2, 0.2])
    assert frame_to_ticks(df) == ticks
    assert ticks_to_frame([]).shape == (0, 4)

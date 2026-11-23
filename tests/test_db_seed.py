"""Seeding a new instance from demo copies what was learned and never an account record."""

import sqlite3

import pytest

from goldtrader.db import connect, migrate
from goldtrader.db.seed import ACCOUNT_TABLES, GROUPS, main, seed, seed_files


def _demo(path):
    c = connect(path)
    migrate(c)
    c.execute("INSERT INTO bars VALUES ('XAUUSDm', 60, 1000, 1,2,0,1, 1.2,2.2,0.2,1.2, 5, 'live')")
    c.execute("INSERT INTO cot_reports VALUES ('2026-11-17', 10, 5, 100, 1)")
    c.execute(
        "INSERT INTO lessons(created_ms, postmortem_id, trade_id, text, norm_text) "
        "VALUES (1, 7, 'T1', 'Wide stops survive noise', 'wide stops survive noise')"
    )
    c.execute(
        "INSERT INTO playbook(night, created_ms, tag, text, evidence_n, active) "
        "VALUES ('2026-11-20', 1, 'hours', 'Asia longs did better', 12, 1)"
    )
    c.execute(
        "INSERT INTO decision_scores(cycle_id, at_ms, side, what) VALUES ('c1', 1, 1, 'taken')"
    )
    c.execute(
        "INSERT INTO strategy_ideas(night, created_ms, kind, params_json, key, what, rationale, "
        "status, paper_bundle_id) VALUES ('n', 1, 'trend_htf', '{}', 'k1', 'w', 'r', 'holds', 'p1')"
    )
    c.execute("INSERT INTO paper_candidates VALUES ('p1', 'trend_htf', '{}', 'label', 1, 1)")
    for sid, status in (("s1", "closed"), ("s2", "open")):
        c.execute(
            "INSERT INTO shadow_trades(shadow_id, experiment_id, bundle_id, side, volume, signal_ms, "
            "entry_ms, entry_price, sl, status) VALUES (?, 'paper', 'p1', 'long', 0.01, 1, 1, 1, 0, ?)",
            (sid, status),
        )
    # account records that must stay behind
    c.execute(
        "INSERT INTO trades(trade_id, side, volume, entry_ms, exit_ms, entry_price, exit_price, "
        "gross_pnl, spread_cost, slippage_cost, commission, swap, net_pnl, exit_reason) "
        "VALUES ('T1', 'long', 0.01, 1, 2, 1, 2, 1, 0, 0, 0, 0, 1, 'stop')"
    )
    c.execute(
        "INSERT INTO incidents(opened_ms, severity, kind, summary) VALUES (1, 'critical', 'x', 'y')"
    )
    c.close()


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM [{table}]").fetchone()[0]


def test_no_account_table_is_in_any_group():
    assert not ACCOUNT_TABLES.intersection(t for g in GROUPS.values() for t in g)


def test_seed_copies_learning_and_market_and_leaves_the_account_behind(tmp_path):
    src_path, dst_path = tmp_path / "demo.sqlite", tmp_path / "live" / "live.sqlite"
    _demo(src_path)
    dry = seed_files(src_path, dst_path, dry_run=True)
    assert dry["bars"] == 1 and dry["lessons"] == 1
    dst = connect(dst_path)
    assert _count(dst, "bars") == 0  # a dry run keeps nothing
    dst.close()

    out = seed_files(src_path, dst_path)
    assert out == {
        "bars": 1,
        "cot_reports": 1,
        "events": 0,
        "lessons": 1,
        "playbook": 1,
        "decision_scores": 1,
        "strategy_ideas": 1,
    }
    dst = connect(dst_path)
    for t in ACCOUNT_TABLES:
        assert _count(dst, t) == 0, t
    assert _count(dst, "paper_candidates") == 0 and _count(dst, "shadow_trades") == 0
    lesson = dst.execute("SELECT postmortem_id, trade_id, text FROM lessons").fetchone()
    assert tuple(lesson) == (None, "demo:T1", "Wide stops survive noise")
    assert dst.execute("SELECT paper_bundle_id FROM strategy_ideas").fetchone()[0] is None
    dst.close()
    # running it again adds nothing
    assert set(seed_files(src_path, dst_path).values()) == {0}
    # the source was only read
    src = sqlite3.connect(src_path)
    assert _count(src, "trades") == 1 and _count(src, "lessons") == 1


def test_paper_group_is_opt_in_and_takes_closed_paper_trades_only(tmp_path):
    src_path, dst_path = tmp_path / "demo.sqlite", tmp_path / "live.sqlite"
    _demo(src_path)
    out = seed_files(src_path, dst_path, ("market", "learning", "paper"))
    assert out["paper_candidates"] == 1 and out["shadow_trades"] == 1
    dst = connect(dst_path)
    assert dst.execute("SELECT shadow_id FROM shadow_trades").fetchall()[0][0] == "s1"
    assert dst.execute("SELECT paper_bundle_id FROM strategy_ideas").fetchone()[0] == "p1"


def test_refusals(tmp_path, capsys):
    src_path = tmp_path / "demo.sqlite"
    _demo(src_path)
    with pytest.raises(ValueError):
        seed_files(src_path, src_path)
    with pytest.raises(FileNotFoundError):
        seed_files(tmp_path / "nope.sqlite", tmp_path / "live.sqlite")
    with pytest.raises(ValueError):
        seed(connect(None), connect(None), ("trades",))
    assert main(["--from", str(src_path), "--to", str(src_path)]) == 1
    assert "not seeded" in capsys.readouterr().out

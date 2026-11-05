"""LearningStore: champions keep the previous pointer, experiments CRUD, append-only events,
bundle_state, allocation_log and the read helpers over trades / shadow_trades / monitor_events
(migration 008; LEARNING_SYSTEM §3.5, D-046, D-047)."""

from __future__ import annotations

import sqlite3

import pytest

from goldtrader.db import connect, migrate
from goldtrader.learning.store import LearningStore
from goldtrader.learning.types import Change, Experiment, GateCheck, Stage, StageDecision

NOW = 1_762_300_000_000
DAY = 86_400_000


def store() -> tuple[sqlite3.Connection, LearningStore]:
    conn = connect(None)
    assert migrate(conn) >= 8
    return conn, LearningStore(conn)


def experiment(
    eid: str = "e1", stage: Stage = Stage.PROPOSED, created: int = NOW, **over
) -> Experiment:
    base = dict(
        experiment_id=eid,
        strategy_id="t1",
        candidate_bundle_id=f"cand-{eid}",
        champion_bundle_id="champ",
        change=Change("param", "stop_atr", 1.2, 1.0),
        pre_registered_metric="sharpe",
        stage=stage,
        stage_entered_ms=created,
        created_ms=created,
        hypothesis_id="7",
        regime_scope="all",
    )
    base.update(over)
    return Experiment(**base)


def insert_trade(conn, tid, bundle, exit_ms, net, slip=0.5, vol=0.01, r=None):
    conn.execute(
        "INSERT INTO trades(trade_id, position_id, bundle_id, proposal_id, side, volume, entry_ms, "
        "exit_ms, entry_price, exit_price, gross_pnl, spread_cost, slippage_cost, commission, swap, "
        "net_pnl, r_multiple, exit_reason, risk_pct_of_equity) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            tid,
            1,
            bundle,
            f"p-{tid}",
            "long",
            vol,
            exit_ms - 3_600_000,
            exit_ms,
            4000.0,
            4001.0,
            net + 1.0,
            0.5,
            slip,
            0.0,
            0.0,
            net,
            r if r is not None else net / 10.0,
            "tp",
            0.5,
        ),
    )


def insert_shadow(conn, sid, bundle, exp_id, exit_ms, net, slip=1.0, vol=0.01, status="closed"):
    conn.execute(
        "INSERT INTO shadow_trades(shadow_id, experiment_id, bundle_id, side, volume, signal_ms, "
        "entry_ms, exit_ms, entry_price, exit_price, sl, tp, net_pnl, r_multiple, exit_reason, "
        "spread_cost, slippage_cost, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            sid,
            exp_id,
            bundle,
            "long",
            vol,
            exit_ms - 3_700_000,
            exit_ms - 3_600_000,
            exit_ms if status == "closed" else None,
            4000.0,
            4001.0,
            3990.0,
            4015.0,
            net,
            net / 10.0,
            "tp",
            0.5,
            slip,
            status,
        ),
    )


def insert_pause(conn, bundle, at_ms, level="pause", monitor="cusum"):
    conn.execute(
        "INSERT INTO monitor_events(at_ms, bundle_id, monitor, level, statistic, threshold, detail) "
        "VALUES (?,?,?,?,?,?,?)",
        (at_ms, bundle, monitor, level, 4.2, 4.0, "test"),
    )


def test_champion_set_keeps_previous_and_bundle_state_defaults_active():
    _, s = store()
    assert s.get_champion("t1") is None
    s.set_champion("t1", "b1", "initial", NOW)
    c = s.get_champion("t1")
    assert c["bundle_id"] == "b1" and c["previous_bundle_id"] is None and c["since_ms"] == NOW
    s.set_champion("t1", "b2", "promoted", NOW + DAY)
    c = s.get_champion("t1")
    assert (c["bundle_id"], c["previous_bundle_id"], c["reason"]) == ("b2", "b1", "promoted")
    assert s.status("b2") == "active"
    s.set_status("b2", "paused", NOW, "cusum")
    assert s.status("b2") == "paused"
    with pytest.raises(ValueError):
        s.set_status("b2", "bogus", NOW)


def test_experiment_crud_and_events_are_append_only():
    conn, s = store()
    e = experiment()
    s.create(e)
    got = s.get("e1")
    assert got == e
    assert [x.experiment_id for x in s.active("t1")] == ["e1"]
    s.update_stage("e1", Stage.OFFLINE, NOW + 1)
    assert s.get("e1").stage is Stage.OFFLINE and s.get("e1").stage_entered_ms == NOW + 1
    merged = s.set_stats("e1", {"offline": {"perm_p": 0.02}})
    merged = s.set_stats("e1", {"other": 1})
    assert merged == {"offline": {"perm_p": 0.02}, "other": 1}
    assert s.get("e1").stats["other"] == 1
    d = StageDecision("e1", Stage.OFFLINE, Stage.DEMO, [GateCheck("min_trades", True, "5 >= 5")])
    s.log_event(d, NOW + 2)
    s.log_event(StageDecision("e1", Stage.DEMO, Stage.DEMO, []), NOW + 3)
    ev = s.events("e1")
    assert [x["to_stage"] for x in ev] == ["demo", "demo"]
    assert ev[0]["gate"]["checks"][0]["name"] == "min_trades"
    assert ev[0]["actor"] == "pipeline"
    s.close("e1", Stage.REJECTED, "offline: pbo", NOW + 4)
    got = s.get("e1")
    assert (
        got.stage is Stage.REJECTED
        and got.closed_ms == NOW + 4
        and got.close_reason == "offline: pbo"
    )
    assert s.active("t1") == []
    with pytest.raises(ValueError):
        s.close("e1", Stage.DEMO, "nope", NOW)
    # no code path deletes events: the table only grows
    assert conn.execute("SELECT COUNT(*) FROM experiment_events").fetchone()[0] == 2
    assert s.get("missing") is None
    with pytest.raises(KeyError):
        s.set_stats("missing", {})


def test_in_stage_promoted_for_and_weekly_count():
    _, s = store()
    s.create(experiment("a", Stage.LIMITED_LIVE, NOW - 2 * DAY))
    s.create(experiment("b", Stage.SHADOW, NOW - 6 * DAY))
    s.create(experiment("c", Stage.PROPOSED, NOW - 8 * DAY))
    assert [e.experiment_id for e in s.in_stage("t1", Stage.LIMITED_LIVE)] == ["a"]
    assert s.weekly_proposal_count("t1", NOW) == 2
    assert s.weekly_proposal_count("other", NOW) == 0
    s.close("a", Stage.PROMOTED, "promoted", NOW)
    assert s.promoted_for("cand-a").experiment_id == "a"
    assert s.promoted_for("cand-b") is None


def test_allocation_log_counts_and_is_idempotent():
    _, s = store()
    s.route("p1", "e1", "challenger", NOW)
    s.route("p1", "e1", "champion", NOW)  # same proposal: first answer stands
    s.route("p2", "e1", "champion", NOW)
    assert s.counts("e1") == {"champion": 1, "challenger": 1}
    assert s.counts("none") == {"champion": 0, "challenger": 0}
    with pytest.raises(ValueError):
        s.route("p3", "e1", "nobody", NOW)


def test_read_helpers_over_other_tables():
    conn, s = store()
    insert_trade(conn, "t1", "b1", NOW - 3 * DAY, 10.0)
    insert_trade(conn, "t2", "b1", NOW - DAY, -4.0)
    insert_trade(conn, "t3", "b2", NOW, 1.0)
    assert [t["trade_id"] for t in s.trades_for_bundle("b1")] == ["t1", "t2"]
    assert [t["trade_id"] for t in s.trades_for_bundle("b1", since_ms=NOW - 2 * DAY)] == ["t2"]
    insert_shadow(conn, "s1", "b2", "e1", NOW - DAY, 3.0)
    insert_shadow(conn, "s2", "b2", "e1", NOW, 0.0, status="open")
    assert [t["shadow_id"] for t in s.shadow_trades_for("b2")] == ["s1"]
    insert_pause(conn, "b1", NOW - DAY)
    insert_pause(conn, "b1", NOW, level="warn")
    insert_pause(conn, "b1", NOW + 1, level="retire")
    pauses = s.monitor_pauses("b1", NOW - 2 * DAY)
    assert [p["level"] for p in pauses] == ["pause", "retire"]
    assert s.monitor_pauses("b1", NOW + 2) == []
    assert s.bundle("nope") is None

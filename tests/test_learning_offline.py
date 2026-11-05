"""Offline stage (LEARNING_SYSTEM §3.2, R-031): decision shape, trial-ledger growth (every
neighbourhood run and the champion count toward N), Benjamini–Hochberg across the week's batch,
and the audit trail PROPOSED → OFFLINE → decision."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

from goldtrader.config.loader import config_hash, parse_config
from goldtrader.data.features import add_features
from goldtrader.data.synthetic import generate_bars
from goldtrader.db import Repo, connect, migrate
from goldtrader.learning.gates import LearningGates
from goldtrader.learning.intake import propose
from goldtrader.learning.offline import neighbourhood, run_offline
from goldtrader.learning.store import LearningStore
from goldtrader.learning.types import Change, Experiment, Stage
from tests.test_backtest_engine import SPEC

NOW = 1_762_300_000_000
PARAMS = {"stop_atr": 1.0, "target_atr": 1.5}


@pytest.fixture(scope="module")
def df():
    return add_features(generate_bars(datetime(2026, 1, 12, tzinfo=UTC), 15, seed=3, seconds=60))


def setup():
    cfg = parse_config(
        {
            "session": {"dst_mode": "winter"},
            "strategies": [{"id": "t1", "kind": "trend_atr", "enabled": True, "params": PARAMS}],
        }
    )
    conn = connect(None)
    migrate(conn)
    return cfg, cfg.strategies[0], conn, Repo(conn), LearningStore(conn), config_hash(cfg)


def hyp(change: str, metric: str = "sharpe", hid: int | None = None) -> dict:
    return {
        "id": hid,
        "title": "h",
        "change_type": change.split(":")[0],
        "change": change,
        "rationale": "r" * 12,
        "pre_registered_metric": metric,
        "regime_scope": "all",
        "flagged_lookahead": 0,
    }


def test_neighbourhood_grid():
    e = Experiment(
        "e",
        "t1",
        "c",
        "p",
        Change("param", "stop_atr", 1.2, 1.0),
        "sharpe",
        Stage.OFFLINE,
        NOW,
        NOW,
    )
    g = neighbourhood(e, {"stop_atr": 1.2, "target_atr": 1.5})
    assert g["target_atr"] == [1.5]
    assert g["stop_atr"] == pytest.approx([0.6, 0.9, 1.2, 1.5, 1.8])
    e_int = Experiment(
        "e",
        "t1",
        "c",
        "p",
        Change("param", "ema_fast", 10.0, 20.0),
        "sharpe",
        Stage.OFFLINE,
        NOW,
        NOW,
    )
    assert neighbourhood(e_int, {"ema_fast": 10.0})["ema_fast"] == [5.0, 8.0, 10.0, 12.0, 15.0]
    e_rule = Experiment(
        "e",
        "t1",
        "c",
        "p",
        Change("rule", "kind", "breakout_session", "trend_atr"),
        "sharpe",
        Stage.OFFLINE,
        NOW,
        NOW,
    )
    assert neighbourhood(e_rule, PARAMS) == {"stop_atr": [1.0], "target_atr": [1.5]}


def test_offline_decision_ledger_growth_and_events(df):
    cfg, strat, conn, repo, store, chash = setup()
    exp = propose(store, repo, cfg, strat, hyp("param:stop_atr=1.2"), NOW, chash)
    assert exp is not None and repo.trial_count() == 0
    d = run_offline(exp, store, repo, cfg, SPEC, df, rng=np.random.default_rng(1), now_ms=NOW + 1)
    # shape: from OFFLINE, to DEMO or REJECTED, every check present
    assert d.experiment_id == exp.experiment_id and d.from_stage is Stage.OFFLINE
    assert d.to_stage in (Stage.DEMO, Stage.REJECTED)
    names = {c.name for c in d.checks}
    assert names >= {
        "min_trades",
        "min_days",
        "min_sharpe",
        "min_backtest_length",
        "beats_champion",
        "pre_registered_metric",
        "bh_fdr",
    }
    # 15 days of random-walk data cannot pass a gate built for 120 days of real edge
    assert d.to_stage is Stage.REJECTED and not d.advanced
    assert not next(c for c in d.checks if c.name == "min_days").ok
    # N: 5 neighbourhood runs (incl. the candidate) + the champion, all in the ledger
    assert repo.trial_count() == 6
    rows = conn.execute(
        "SELECT bundle_id, change_type, status FROM trial_ledger ORDER BY id"
    ).fetchall()
    assert [r["change_type"] for r in rows].count("baseline") == 1
    cand_rows = [r for r in rows if r["bundle_id"] == exp.candidate_bundle_id]
    assert len(cand_rows) == 5 and cand_rows[-1]["status"] == "failed_offline"
    assert {r["status"] for r in cand_rows[:-1]} == {"recorded"}
    # experiment closed with the failed checks, stats recorded, events: intake, →offline, →rejected
    got = store.get(exp.experiment_id)
    assert got.stage is Stage.REJECTED and got.close_reason.startswith("offline: ")
    off = got.stats["offline"]
    assert off["n_runs"] == 6 and off["n_trials_counted"] >= 6
    assert "candidate" in off and "champion" in off and "sharpe" in off["candidate"]
    ev = store.events(exp.experiment_id)
    assert [e["to_stage"] for e in ev] == ["proposed", "offline", "rejected"]
    assert ev[-1]["gate"]["checks"]


def test_rule_change_runs_candidate_and_champion_only(df):
    cfg, strat, conn, repo, store, chash = setup()
    exp = propose(
        store, repo, cfg, strat, hyp("rule:kind=breakout_session", "drawdown"), NOW, chash
    )
    d = run_offline(exp, store, repo, cfg, SPEC, df, rng=np.random.default_rng(2), now_ms=NOW + 1)
    assert repo.trial_count() == 2
    assert exp.pre_registered_metric == "max_drawdown_pct"
    m = next(c for c in d.checks if c.name == "pre_registered_metric")
    assert "max_drawdown_pct" in m.detail
    # an unrecognised pre-registered metric is itself a failing check
    exp2 = propose(store, repo, cfg, strat, hyp("param:target_atr=2.0", "hit rate"), NOW + 2, chash)
    d2 = run_offline(exp2, store, repo, cfg, SPEC, df, rng=np.random.default_rng(2), now_ms=NOW + 3)
    m2 = next(c for c in d2.checks if c.name == "pre_registered_metric")
    assert not m2.ok and "hit rate" in m2.detail


def test_bh_applied_across_the_weeks_batch(df):
    cfg, strat, conn, repo, store, chash = setup()
    gates = LearningGates(offline_fdr=0.10)
    e1 = propose(store, repo, cfg, strat, hyp("param:stop_atr=1.2"), NOW, chash, gates=gates)
    e2 = propose(store, repo, cfg, strat, hyp("param:stop_atr=0.8"), NOW + 1, chash, gates=gates)
    d1 = run_offline(
        e1, store, repo, cfg, SPEC, df, rng=np.random.default_rng(5), now_ms=NOW + 2, gates=gates
    )
    d2 = run_offline(
        e2, store, repo, cfg, SPEC, df, rng=np.random.default_rng(6), now_ms=NOW + 3, gates=gates
    )
    s1, s2 = store.get(e1.experiment_id).stats, store.get(e2.experiment_id).stats
    bh1 = next(c for c in d1.checks if c.name == "bh_fdr")
    bh2 = next(c for c in d2.checks if c.name == "bh_fdr")
    if s1["offline"]["perm_p"] is None or s2["offline"]["perm_p"] is None:
        pytest.skip("too few trades on this synthetic sample for a permutation p-value")
    assert "over 1 this week" in bh1.detail and "over 2 this week" in bh2.detail
    # BH over the batch {p1, p2}: the adjusted p of the second is >= its raw p and <= 1
    raw2, adj2 = s2["offline"]["perm_p"], s2["offline_bh_adjusted_p"]
    assert adj2 is not None and raw2 <= adj2 <= 1.0
    assert bh2.ok == (adj2 <= gates.offline_fdr)

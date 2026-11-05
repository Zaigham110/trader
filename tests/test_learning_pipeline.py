"""Pipeline stage gates (LEARNING_SYSTEM §3.3–§3.5; D-046, D-047): sample-size waits, CI and pause
rejections, time limit, promotion with superseding, rollback within the window, deterministic
limited-live routing, and the owner-hash guard."""

from __future__ import annotations

import numpy as np
import pytest

from goldtrader.backtest.gate import bundle_id
from goldtrader.config.loader import config_hash, parse_config
from goldtrader.db import Repo, connect, migrate
from goldtrader.learning.gates import LearningGates
from goldtrader.learning.intake import ensure_champion
from goldtrader.learning.pipeline import Pipeline, allocation_bucket
from goldtrader.learning.store import LearningStore
from goldtrader.learning.types import Change, Experiment, Stage
from tests.test_backtest_engine import SPEC
from tests.test_learning_store import insert_pause, insert_shadow, insert_trade

NOW = 1_762_300_000_000
DAY = 86_400_000
PARAMS = {"stop_atr": 1.0, "target_atr": 1.5}
GATES = LearningGates(
    demo_min_days=3,
    demo_min_trades=5,
    shadow_min_days=2,
    shadow_min_trades=3,
    live_min_days=3,
    live_min_trades=5,
    max_experiment_days=30,
    rollback_window_days=30,
    challenger_share=0.25,
)


class World:
    def __init__(self):
        self.cfg = parse_config(
            {
                "session": {"dst_mode": "winter"},
                "strategies": [
                    {"id": "t1", "kind": "trend_atr", "enabled": True, "params": PARAMS}
                ],
            }
        )
        self.strat = self.cfg.strategies[0]
        self.conn = connect(None)
        migrate(self.conn)
        self.repo, self.store = Repo(self.conn), LearningStore(self.conn)
        self.chash = config_hash(self.cfg)
        self.clock = [NOW]
        self.champion = ensure_champion(
            self.store, self.repo, self.strat, self.cfg, self.chash, NOW
        )
        self.pipe = Pipeline(
            self.store,
            self.repo,
            self.cfg,
            self.strat,
            lambda: self.clock[0],
            gates=GATES,
            spec=SPEC,
            rng=np.random.default_rng(0),
        )

    def experiment(self, eid: str, stage: Stage, value: float = 1.2, created=None) -> Experiment:
        params = {**PARAMS, "stop_atr": value}
        cand = bundle_id("t1", "trend_atr", params, self.chash, parent=self.champion)
        self.repo.record_bundle(cand, "t1", "trend_atr", params, self.chash, parent=self.champion)
        exp = Experiment(
            experiment_id=eid,
            strategy_id="t1",
            candidate_bundle_id=cand,
            champion_bundle_id=self.champion,
            change=Change("param", "stop_atr", value, 1.0),
            pre_registered_metric="sharpe",
            stage=stage,
            stage_entered_ms=created if created is not None else NOW,
            created_ms=created if created is not None else NOW,
        )
        self.store.create(exp)
        return exp

    def trades(self, bundle: str, pnls, start=NOW + DAY, per_day=2, slip=0.02, tag="t"):
        for i, p in enumerate(pnls):
            insert_trade(
                self.conn,
                f"{tag}-{bundle[:6]}-{i}",
                bundle,
                start + (i // per_day) * DAY + i,
                p,
                slip=slip,
            )

    def shadow(self, exp: Experiment, pnls, start=NOW + DAY, per_day=2, slip=0.02):
        for i, p in enumerate(pnls):
            insert_shadow(
                self.conn,
                f"s-{exp.experiment_id}-{i}",
                exp.candidate_bundle_id,
                exp.experiment_id,
                start + (i // per_day) * DAY + i,
                p,
                slip=slip,
            )


def check(d, name):
    return next(c for c in d.checks if c.name == name)


def test_demo_gate_waits_then_advances_on_ci():
    w = World()
    e = w.experiment("demo1", Stage.DEMO)
    w.clock[0] = NOW + 2 * DAY
    d = w.pipe.advance(e)
    assert d.to_stage is Stage.DEMO and not d.advanced and not check(d, "demo_min_trades").ok
    assert "pending" in check(d, "expectancy_ci").detail
    assert [x["to_stage"] for x in w.store.events("demo1")] == ["demo"]  # the wait is logged
    w.trades(e.candidate_bundle_id, [10.0, 12.0, 8.0, 11.0, 9.0, 10.5])  # 6 trades over 3 days
    w.clock[0] = NOW + 5 * DAY
    d = w.pipe.advance(e)
    assert d.to_stage is Stage.SHADOW and d.advanced
    assert check(d, "demo_min_days").ok and check(d, "expectancy_ci").ok and check(d, "no_pause").ok
    got = w.store.get("demo1")
    assert got.stage is Stage.SHADOW and got.stage_entered_ms == NOW + 5 * DAY


def test_demo_gate_rejects_on_ci_or_pause():
    w = World()
    e = w.experiment("ci", Stage.DEMO)
    w.trades(e.candidate_bundle_id, [10.0, -12.0, 8.0, -11.0, 9.0, -10.0])
    w.clock[0] = NOW + 5 * DAY
    d = w.pipe.advance(e)
    assert d.to_stage is Stage.REJECTED and not check(d, "expectancy_ci").ok
    assert w.store.get("ci").close_reason == "demo: expectancy_ci"
    e2 = w.experiment("paused", Stage.DEMO, value=1.3)
    insert_pause(w.conn, e2.candidate_bundle_id, NOW + DAY)
    d2 = w.pipe.advance(e2)
    assert d2.to_stage is Stage.REJECTED and not check(d2, "no_pause").ok
    with pytest.raises(ValueError):
        w.pipe.advance(e2)  # closed experiments cannot advance


def test_time_limit_rejects_in_any_stage():
    w = World()
    e = w.experiment("old", Stage.SHADOW, created=NOW - 31 * DAY)
    d = w.pipe.advance(e)
    assert d.to_stage is Stage.REJECTED and check(d, "max_experiment_days").ok is False
    assert w.store.get("old").close_reason.startswith("shadow: max_experiment_days")


def test_offline_stage_needs_inputs():
    w = World()
    e = w.experiment("prop", Stage.PROPOSED)
    with pytest.raises(ValueError, match="df_features"):
        w.pipe.advance(e)


def test_shadow_gate_slippage_and_evalue_state():
    w = World()
    e = w.experiment("sh", Stage.SHADOW)
    w.clock[0] = NOW + 3 * DAY
    d = w.pipe.advance(e)
    assert d.to_stage is Stage.SHADOW and "pending" in check(d, "slippage_ratio").detail
    assert "evalue_shadow" in w.store.get("sh").stats
    # expected slippage per trade: 2 × 1 point × 0.01 × 0.01 lot × 100 oz = 0.02 USD
    w.shadow(e, [3.0, 2.0, 4.0, 1.0], slip=0.02)
    d = w.pipe.advance(e)
    assert d.to_stage is Stage.LIMITED_LIVE and check(d, "slippage_ratio").ok
    assert check(d, "evalue_recorded").ok and w.store.get("sh").stats["evalue_shadow"]["n"] == 2
    e2 = w.experiment("slip", Stage.SHADOW, value=1.3)
    w.shadow(e2, [3.0, 2.0, 4.0, 1.0], slip=0.05)  # 2.5 × the model > 1.25
    d2 = w.pipe.advance(e2)
    assert d2.to_stage is Stage.REJECTED and "2.50" in check(d2, "slippage_ratio").detail


def test_live_gate_promotes_sets_champion_and_supersedes():
    w = World()
    e = w.experiment("live", Stage.LIMITED_LIVE)
    sibling = w.experiment("sib", Stage.DEMO, value=1.3)
    w.trades(e.candidate_bundle_id, [10.0, 12.0, 8.0, 11.0, 9.0, 10.5])
    w.trades(w.champion, [1.0, 1.0, 1.0, 1.0], tag="c")
    w.clock[0] = NOW + 5 * DAY
    d = w.pipe.advance(e)
    assert d.to_stage is Stage.PROMOTED and d.advanced and check(d, "promoted").ok
    champ = w.store.get_champion("t1")
    assert champ["bundle_id"] == e.candidate_bundle_id and champ["previous_bundle_id"] == w.champion
    assert w.store.get("live").stage is Stage.PROMOTED
    sib = w.store.get("sib")
    assert sib.stage is Stage.REJECTED and sib.close_reason == "superseded"
    assert w.store.events("sib")[-1]["gate"]["checks"][0]["name"] == "superseded"
    assert w.store.active("t1") == [] and w.store.status(e.candidate_bundle_id) == "active"
    assert w.pipe.strategy_for_bundle(e.candidate_bundle_id).params["stop_atr"] == 1.2
    assert w.pipe.strategy_for_bundle("missing") == w.strat
    _ = sibling


def test_live_gate_waits_without_evidence_and_mirror_rejects():
    w = World()
    e = w.experiment("flat", Stage.LIMITED_LIVE)
    w.trades(e.candidate_bundle_id, [1.0, -1.0, 1.0, -1.0, 1.0, -1.0])
    w.clock[0] = NOW + 5 * DAY
    d = w.pipe.advance(e)
    assert d.to_stage is Stage.LIMITED_LIVE and not check(d, "evalue").ok
    assert not check(d, "expectancy_ci").ok
    e2 = w.experiment("worse", Stage.LIMITED_LIVE, value=1.3)
    rng = np.random.default_rng(1)
    w.trades(e2.candidate_bundle_id, list(rng.normal(-1.0, 0.3, 25)), per_day=1)
    w.trades(w.champion, [0.5] * 25, per_day=1, tag="c")
    w.clock[0] = NOW + 30 * DAY
    d2 = w.pipe.advance(e2)
    assert d2.to_stage is Stage.REJECTED and not check(d2, "mirror_eprocess").ok
    assert w.store.get("worse").stats["evalue_limited_live"]["decision"] == "reject"


def test_rollback_within_window_on_monitor_pause():
    w = World()
    e = w.experiment("rb", Stage.LIMITED_LIVE)
    w.clock[0] = NOW + 5 * DAY
    w.pipe.promote(e)
    assert w.store.get_champion("t1")["bundle_id"] == e.candidate_bundle_id
    assert w.pipe.check_rollback_triggers() is None  # no pause yet
    insert_pause(w.conn, e.candidate_bundle_id, NOW + 6 * DAY, monitor="mc_drawdown")
    w.clock[0] = NOW + 7 * DAY
    d = w.pipe.check_rollback_triggers()
    assert d is not None and d.to_stage is Stage.ROLLED_BACK and d.actor == "monitor"
    assert "mc_drawdown pause" in check(d, "rollback_trigger").detail
    champ = w.store.get_champion("t1")
    assert champ["bundle_id"] == w.champion and champ["previous_bundle_id"] == e.candidate_bundle_id
    assert champ["reason"].startswith("rollback:")
    assert w.store.get("rb").stage is Stage.ROLLED_BACK
    assert w.store.status(e.candidate_bundle_id) == "paused"
    # a pause outside the window does not roll back; nor does a pause on the initial champion
    e2 = w.experiment("rb2", Stage.LIMITED_LIVE, value=1.3)
    w.pipe.promote(e2)
    insert_pause(w.conn, e2.candidate_bundle_id, NOW + 40 * DAY)
    w.clock[0] = NOW + 41 * DAY
    assert w.pipe.check_rollback_triggers() is None
    w2 = World()
    insert_pause(w2.conn, w2.champion, NOW + DAY)
    w2.clock[0] = NOW + 2 * DAY
    assert w2.pipe.check_rollback_triggers() is None


def test_route_share_deterministic_and_shadow_never_routed():
    w = World()
    assert w.pipe.route("p0") == (w.champion, None)  # no experiment: champion
    sh = w.experiment("shadow", Stage.SHADOW)
    assert all(w.pipe.route(f"s{i}")[0] == w.champion for i in range(50))
    assert w.store.counts("shadow") == {"champion": 0, "challenger": 0}
    w.store.close("shadow", Stage.REJECTED, "test", NOW)
    e = w.experiment("ll", Stage.LIMITED_LIVE, value=1.3)
    ids = [f"prop-{i}" for i in range(1000)]
    answers = [w.pipe.route(pid) for pid in ids]
    n_ch = sum(a[0] == e.candidate_bundle_id for a in answers)
    assert 200 <= n_ch <= 300  # ≈ 25 % (D-047)
    assert all(a[1] == "ll" for a in answers if a[0] == e.candidate_bundle_id)
    assert all(a == (w.champion, None) for a in answers if a[0] != e.candidate_bundle_id)
    assert w.store.counts("ll") == {"champion": 1000 - n_ch, "challenger": n_ch}
    assert [w.pipe.route(pid) for pid in ids] == answers  # deterministic
    assert w.store.counts("ll") == {"champion": 1000 - n_ch, "challenger": n_ch}
    assert (
        allocation_bucket("prop-1") == allocation_bucket("prop-1")
        and 0 <= allocation_bucket("x") < 100
    )
    # a paused candidate is never routed to
    w.store.set_status(e.candidate_bundle_id, "paused", NOW, "cusum")
    assert all(w.pipe.route(f"later-{i}") == (w.champion, None) for i in range(50))
    _ = sh


def test_guard_blocks_owner_change_during_pipeline_calls():
    w = World()
    e = w.experiment("g", Stage.DEMO)
    w.pipe.cfg = parse_config(
        {"session": {"dst_mode": "winter"}, "owner": {"risk": {"daily_loss_pct": 1.0}}}
    )
    with pytest.raises(PermissionError):
        w.pipe.advance(e)
    with pytest.raises(PermissionError):
        w.pipe.promote(e)
    assert w.store.get("g").stage is Stage.DEMO  # nothing happened

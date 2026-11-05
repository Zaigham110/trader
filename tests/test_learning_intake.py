"""Hypothesis intake (LEARNING_SYSTEM §3.1, §2; D-046): champion creation, change parsing and
every refusal (look-ahead flag, weekly cap, multi-change, out-of-bounds, unknown kind, no-op)."""

from __future__ import annotations

from goldtrader.backtest.gate import bundle_id
from goldtrader.config.loader import config_hash, owner_hash, parse_config
from goldtrader.db import Repo, connect, migrate
from goldtrader.learning.gates import LearningGates, gates_for
from goldtrader.learning.intake import (
    ensure_champion,
    hypothesis_to_change,
    normalise_metric,
    param_violation,
    parse_change,
    propose,
)
from goldtrader.learning.store import LearningStore
from goldtrader.learning.types import Change, Stage

NOW = 1_762_300_000_000
PARAMS = {"stop_atr": 1.0, "target_atr": 1.5}


def setup():
    cfg = parse_config(
        {
            "session": {"dst_mode": "winter"},
            "strategies": [{"id": "t1", "kind": "trend_atr", "enabled": True, "params": PARAMS}],
        }
    )
    conn = connect(None)
    migrate(conn)
    repo, store = Repo(conn), LearningStore(conn)
    return cfg, cfg.strategies[0], conn, repo, store, config_hash(cfg)


def hyp(change="param:stop_atr=1.2", **over):
    h = {
        "id": 1,
        "created_ms": NOW,
        "run_id": "r1",
        "title": "Wider stop",
        "change_type": change.split(":")[0],
        "change": change,
        "rationale": "ATR stops at 1.0 are hit by noise in the London open.",
        "pre_registered_metric": "Sharpe after costs",
        "regime_scope": "all",
        "flagged_lookahead": 0,
        "status": "proposed",
    }
    h.update(over)
    return h


def insert_hypothesis(conn, h):
    conn.execute(
        "INSERT INTO hypotheses(id, created_ms, run_id, title, change_type, change, rationale, "
        "pre_registered_metric, regime_scope, flagged_lookahead, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            h["id"],
            h["created_ms"],
            h["run_id"],
            h["title"],
            h["change_type"],
            h["change"],
            h["rationale"],
            h["pre_registered_metric"],
            h["regime_scope"],
            h["flagged_lookahead"],
            h["status"],
        ),
    )


def test_parse_change_forms_and_refusals():
    assert parse_change("param:stop_atr=1.2") == ("param", "stop_atr", "1.2")
    assert parse_change(" rule: kind = breakout_session ") == ("rule", "kind", "breakout_session")
    assert parse_change("prompt:strategy.system=3") == ("prompt", "strategy.system", "3")
    for bad in (
        "param:stop_atr=1.2;param:target_atr=2",  # two changes
        "param:stop_atr=1.2, target_atr=2",
        "set stop_atr to 1.2",  # free text
        "param:stop_atr",  # no value
        "model:tier=deep",  # not a learnable change type
        "param:stop_atr=1.2 and target_atr=2",
    ):
        assert parse_change(bad) is None, bad


def test_hypothesis_to_change_types_values():
    ch = hypothesis_to_change(hyp(), PARAMS, "trend_atr")
    assert ch == Change("param", "stop_atr", 1.2, 1.0)
    assert hypothesis_to_change(hyp("param:ema_fast=12"), PARAMS, "trend_atr").value == 12.0
    assert hypothesis_to_change(hyp("param:ema_fast=12.5"), PARAMS, "trend_atr") is None
    assert hypothesis_to_change(hyp("param:stop_atr=nan"), PARAMS, "trend_atr") is None
    assert hypothesis_to_change(hyp("rule:kind=breakout_session"), PARAMS, "trend_atr") == Change(
        "rule", "kind", "breakout_session", "trend_atr"
    )
    assert hypothesis_to_change(hyp("rule:name=breakout_session"), PARAMS, "trend_atr") is None
    assert hypothesis_to_change(hyp("prompt:strategy.system=4"), PARAMS, "trend_atr") == Change(
        "prompt", "strategy.system", 4, None
    )
    assert hypothesis_to_change(hyp("prompt:strategy.system=0"), PARAMS, "trend_atr") is None
    # declared change_type must agree with the text
    assert (
        hypothesis_to_change(hyp("param:stop_atr=1.2", change_type="rule"), PARAMS, "trend_atr")
        is None
    )
    assert normalise_metric("Sharpe after costs") == "sharpe"
    assert normalise_metric("expectancy in R") == "expectancy_r"
    assert normalise_metric("max drawdown") == "max_drawdown_pct"
    assert normalise_metric("hit rate") == "hit rate"


def test_param_violation_bounds():
    cfg, strat, *_ = setup()
    assert param_violation("trend_atr", {"stop_atr": 1.2, "target_atr": 1.5}, cfg, strat) is None
    assert "stop_atr" in param_violation("trend_atr", {"stop_atr": 2.0}, cfg, strat)  # > 1.5
    assert "stop_atr" in param_violation("trend_atr", {"stop_atr": 0.1}, cfg, strat)  # < 0.3
    assert "target_atr" in param_violation(
        "trend_atr", {"stop_atr": 1.0, "target_atr": 0.5}, cfg, strat
    )
    assert "unknown parameter" in param_violation("trend_atr", {"lookback": 5.0}, cfg, strat)
    assert "ema_fast" in param_violation("trend_atr", {"ema_fast": 60, "ema_slow": 50}, cfg, strat)
    assert "not in the rule library" in param_violation("flat", {}, cfg, strat)
    assert "hour" in param_violation("breakout_session", {"trade_end_hour": 25}, cfg, strat)
    assert "start < end" in param_violation(
        "breakout_session", {"range_start_hour": 8, "range_end_hour": 7}, cfg, strat
    )


def test_ensure_champion_creates_bundle_once():
    cfg, strat, conn, repo, store, chash = setup()
    bid = ensure_champion(store, repo, strat, cfg, chash, NOW)
    assert bid == bundle_id("t1", "trend_atr", PARAMS, chash)
    assert store.get_champion("t1")["bundle_id"] == bid
    assert store.bundle(bid)["params"] == PARAMS and store.status(bid) == "active"
    assert ensure_champion(store, repo, strat, cfg, chash, NOW + 1) == bid
    assert conn.execute("SELECT COUNT(*) FROM bundles").fetchone()[0] == 1


def test_propose_creates_experiment_and_logs():
    cfg, strat, conn, repo, store, chash = setup()
    insert_hypothesis(conn, hyp())
    exp = propose(store, repo, cfg, strat, store.hypothesis(1), NOW, chash)
    assert exp is not None and exp.stage is Stage.PROPOSED
    assert exp.change == Change("param", "stop_atr", 1.2, 1.0)
    assert exp.pre_registered_metric == "sharpe" and exp.hypothesis_id == "1"
    champ = store.get_champion("t1")["bundle_id"]
    assert exp.champion_bundle_id == champ and exp.candidate_bundle_id != champ
    cand = store.bundle(exp.candidate_bundle_id)
    assert cand["params"] == {"stop_atr": 1.2, "target_atr": 1.5}
    assert cand["parent_bundle_id"] == champ
    assert store.get(exp.experiment_id) == exp
    ev = store.events(exp.experiment_id)
    assert len(ev) == 1 and ev[0]["actor"] == "intake" and ev[0]["to_stage"] == "proposed"
    assert store.hypothesis(1)["status"] == f"experiment:{exp.experiment_id}"
    # a rule change drops parameters the new kind does not know and keeps the rest
    insert_hypothesis(conn, hyp("rule:kind=breakout_session", id=2, change_type="rule"))
    exp2 = propose(store, repo, cfg, strat, store.hypothesis(2), NOW + 1, chash)
    assert (
        exp2 is not None
        and store.bundle(exp2.candidate_bundle_id)["strategy_kind"] == "breakout_session"
    )
    insert_hypothesis(conn, hyp("prompt:strategy.system=5", id=3, change_type="prompt"))
    exp3 = propose(store, repo, cfg, strat, store.hypothesis(3), NOW + 2, chash)
    assert exp3 is not None and exp3.change.change_type == "prompt"
    assert len({e.candidate_bundle_id for e in (exp, exp2, exp3)}) == 3
    # the owner section was never touched (R-034): config object unchanged
    assert config_hash(cfg) == chash


def test_propose_refusals():
    cfg, strat, conn, repo, store, chash = setup()
    reasons: list[str] = []

    def go(h, now=NOW, gates=None):
        return propose(store, repo, cfg, strat, h, now, chash, gates=gates, reasons=reasons)

    insert_hypothesis(conn, hyp(id=10, flagged_lookahead=1))
    assert go(store.hypothesis(10)) is None and "flagged_lookahead" in reasons[-1]
    assert store.hypothesis(10)["status"].startswith("refused")
    assert go(hyp("param:stop_atr=1.2;param:target_atr=2.0", id=None)) is None
    assert "single" in reasons[-1]
    assert go(hyp("param:stop_atr=1.0", id=None)) is None and "no-op" in reasons[-1]
    assert go(hyp("param:stop_atr=2.5", id=None)) is None and "outside" in reasons[-1]
    assert go(hyp("param:lookback=5", id=None)) is None and "unknown parameter" in reasons[-1]
    assert go(hyp("rule:kind=martingale", id=None, change_type="rule")) is None
    assert "not in the library" in reasons[-1]
    assert go(hyp("rule:kind=trend_atr", id=None, change_type="rule")) is None
    assert "no-op" in reasons[-1]
    assert store.active("t1") == []  # nothing was created by a refusal
    # weekly cap: with max 2, the third proposal in the week is refused, one a week later is not
    gates = LearningGates(max_proposals_per_week=2)
    assert go(hyp("param:stop_atr=1.1", id=None), gates=gates) is not None
    assert go(hyp("param:stop_atr=1.2", id=None), gates=gates) is not None
    assert go(hyp("param:stop_atr=1.3", id=None), gates=gates) is None
    assert "weekly cap" in reasons[-1]
    assert go(hyp("param:stop_atr=1.3", id=None), now=NOW + 8 * 86_400_000, gates=gates) is not None


def test_gates_are_read_from_the_owner_section_and_never_written():
    cfg, *_ = setup()
    g = gates_for(cfg)
    assert isinstance(g, LearningGates) and g.max_proposals_per_week == 10
    cfg2 = parse_config(
        {"session": {"dst_mode": "winter"}, "owner": {"learning": {"max_proposals_per_week": 3}}}
    )
    assert gates_for(cfg2).max_proposals_per_week == 3
    assert gates_for(cfg2, LearningGates(max_proposals_per_week=1)).max_proposals_per_week == 1
    assert owner_hash(cfg2) != owner_hash(cfg)  # the gates are part of the hashed owner section

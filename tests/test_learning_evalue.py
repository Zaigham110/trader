"""Anytime-valid e-process (research/04 §4.2; LEARNING_SYSTEM §3.3–§3.4): false promotions under H0
stay rare at any stopping time, a clear shift is detected quickly, the mirrored process rejects a
negative shift, and `daily_pairs` pairs trades by exit day."""

from __future__ import annotations

import math

import numpy as np
import pytest

from goldtrader.learning.evalue import DAY_MS, EProcess, daily_pairs

SEEDS = 50
STEPS = 200


def _run(seed: int, shift: float, dist: str = "normal") -> EProcess:
    rng = np.random.default_rng(seed)
    draws = rng.normal(shift, 1.0, STEPS) if dist == "normal" else rng.standard_t(3, STEPS) + shift
    return EProcess.from_pairs(draws)


def test_h0_false_promotions_are_rare_at_any_stopping_time():
    # Ville: P(sup W >= 20) <= 5 %; the sticky decision IS the stopping rule. Tolerance 15 %.
    false_promotions = sum(_run(s, 0.0).decision() == "promote" for s in range(SEEDS))
    assert false_promotions <= math.ceil(0.15 * SEEDS)
    heavy = sum(_run(s, 0.0, "t").decision() != "continue" for s in range(SEEDS))
    assert heavy <= math.ceil(0.15 * SEEDS)  # both directions, heavy tails: winsorisation holds


def test_clear_positive_shift_promotes_quickly():
    crossed = []
    for seed in range(20):
        rng = np.random.default_rng(seed)
        ep = EProcess()
        for i in range(STEPS):
            if ep.update(rng.normal(1.0, 1.0)) == "promote":
                crossed.append(i + 1)
                break
    assert len(crossed) == 20 and np.median(crossed) < 60
    ep = _run(3, 0.5)
    assert ep.decision() == "promote" and ep.e_value >= 20.0


def test_mirrored_process_rejects_negative_shift_and_decision_is_sticky():
    ep = _run(1, -1.0)
    assert ep.decision() == "reject" and ep.mirror_e_value >= 20.0 and ep.e_value < 20.0
    before = ep.state()
    ep.extend([5.0] * 50)  # evidence after the stop does not reopen the decision
    assert ep.decision() == "reject" and ep.state()["e_value"] == before["e_value"]


def test_warmup_scale_and_bet_rules():
    ep = EProcess(threshold=20.0, cap=0.5)
    for d in (1.0, 1.0, 1.0, 1.0):
        ep.update(d)
    assert ep.bet() == 0.0 and ep.e_value == 1.0  # no bets before 5 observations
    ep.update(1.0)
    assert ep.n == 5 and ep.bet() == 0.0  # degenerate (zero variance) history: no bet
    ep2 = EProcess.from_pairs([1.0, -1.0, 2.0, -2.0, 0.5, 3.0, 1.0])
    assert -0.5 <= ep2.bet() <= 0.5
    assert ep2.e_value > 0.0 and ep2.mirror_e_value > 0.0  # wealth stays strictly positive
    st = ep2.state()
    assert set(st) == {"n", "e_value", "mirror_e_value", "threshold", "cap", "decision"}
    with pytest.raises(ValueError):
        EProcess(threshold=1.0)
    with pytest.raises(ValueError):
        EProcess(cap=0.0)
    with pytest.raises(ValueError):
        ep2.update(float("nan"))


def test_daily_pairs_by_exit_day():
    d0 = 1_762_300_000_000 // DAY_MS * DAY_MS
    a = [
        {"exit_ms": d0 + 1000, "net_pnl": 5.0, "r_multiple": 0.5},
        {"exit_ms": d0 + 5000, "net_pnl": 3.0, "r_multiple": 0.3},
        {"exit_ms": d0 + 2 * DAY_MS, "net_pnl": -2.0, "r_multiple": -0.2},
        {"exit_ms": None, "net_pnl": 99.0},  # open trade: ignored
    ]
    b = [
        {"exit_ms": d0 + 7000, "net_pnl": 4.0, "r_multiple": 0.4},
        {"exit_ms": d0 + DAY_MS, "net_pnl": 1.0, "r_multiple": 0.1},
    ]
    assert daily_pairs(a, b) == [4.0, -1.0, -2.0]
    assert daily_pairs(a, b, key="r_multiple") == pytest.approx([0.4, -0.1, -0.2])
    assert daily_pairs([], []) == []

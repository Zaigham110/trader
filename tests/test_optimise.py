"""Nightly strategy optimiser (D-069): sampler, fold score, PBO and the search itself."""

from __future__ import annotations

import random

import pandas as pd
import pytest

from goldtrader.backtest import optimise as opt


def _frame(n=9000):
    return pd.DataFrame({"time": [1_790_000_000 + 60 * i for i in range(n)]})


def _validate(idea):
    if idea.get("kind") not in opt.SPACE:
        return None, "kind not in the rule library"
    params = {k: v for k, v in idea.items() if k != "kind"}
    return (idea["kind"], "id", params), ""


def _key(c):
    return c[0] + repr(sorted(c[2].items()))


def test_fold_score_rewards_steady_profit_not_one_lucky_fold():
    steady, ok1 = opt.fold_score([0.5, 0.4, 0.6, 0.5], [8, 8, 8, 8])
    lucky, ok2 = opt.fold_score([4.0, -0.7, -0.6, -0.7], [8, 8, 8, 8])
    assert ok1 and not ok2  # the same total, but only one profitable fold
    assert steady > 0.4 and steady > lucky
    assert opt.fold_score([0.5] * 4, [8, 8, 8, 2])[1] is False  # a fold with almost no trades
    assert opt.fold_score([0.5] * 4, [4, 4, 4, 4])[1] is False  # too few trades overall


def test_pbo_separates_a_real_edge_from_noise():
    assert opt.pbo([[0.1, 0.2, 0.1, 0.2]] * 3) is None  # too few trials to say anything
    edge = [[1.0, 1.1, 0.9, 1.0]] + [[-0.2, -0.1, -0.3, -0.2]] * 5
    assert opt.pbo(edge) == 0.0  # the best on any half is the best on the other half too
    rng = random.Random(1)
    noise = [[rng.gauss(0, 1) for _ in range(4)] for _ in range(40)]
    assert opt.pbo(noise) >= 0.4  # the winner on one half is a coin flip on the other


def test_sampler_is_reproducible_and_moves_toward_what_worked():
    def history(n):
        rng = random.Random(7)
        out = []
        for _ in range(n):
            idea = opt.random_idea(rng)
            out.append(
                (idea, 2.0 if idea["kind"] == "channel_break" and idea["minutes"] == 60 else -1.0)
            )
        return out

    a = [opt.Sampler(5, history(60)).propose() for _ in range(1)]
    b = [opt.Sampler(5, history(60)).propose() for _ in range(1)]
    assert a == b  # same seed and history: same proposal
    s = opt.Sampler(11, history(120))
    props = [s.propose() for _ in range(40)]
    hits = sum(1 for p in props if p["kind"] == "channel_break" and p["minutes"] == 60)
    assert hits >= 15  # far above the 1-in-24 chance of a random draw
    assert any(p["kind"] != "channel_break" for p in props)  # every fifth trial still explores
    fresh = opt.Sampler(3)
    assert all(p["kind"] in opt.SPACE for p in (fresh.propose() for _ in range(8)))
    for kind, space in opt.SPACE.items():  # every value in the space is one the rules accept
        assert all(len(v) >= 2 for v in space.values()), kind


def test_search_scores_on_folds_and_touches_the_blind_period_once():
    seen: list[tuple[int, float]] = []

    def evaluate(df, c, spread_mult=1.0):
        seen.append((len(df), spread_mult))
        good = c[0] == "trend_htf"
        return {
            "n_trades": 12,
            "net_return_pct": 0.6 if good else -0.3,
            "net_pnl": 30 if good else -15,
        }

    # one known-good candidate is seeded so the test does not depend on the random draw
    good_seed = [
        {"kind": "trend_htf", "minutes": 15, "on_cross": False, "stop_atr": 1.5, "target_atr": 9.0}
    ]
    res = opt.run(
        _frame(), seed=1, validate=_validate, key_of=_key, evaluate=evaluate, trials=16,
        seeds=good_seed,
    )  # fmt: skip
    assert len(res["trials"]) == 16 and len({t["key"] for t in res["trials"]}) == 16
    assert res["best"]["candidate"][0] == "trend_htf" and res["best"]["holds"] is True
    assert res["pbo"] == 0.0 and res["eligible"] >= 1
    blind = [x for x in seen if x[0] == 3000]
    assert blind == [(3000, 1.0), (3000, 1.5)]  # the last third, once at each spread
    assert len(seen) == 16 * opt.FOLDS + 2 and {x[0] for x in seen} == {1500, 3000}
    # seeds go first; refused and already-tested seeds are reported, never evaluated
    seeds = [{"kind": "martingale"}, {"kind": "trend_htf", "minutes": 5, "on_cross": True,
             "stop_atr": 1.0, "target_atr": 3.0}]  # fmt: skip
    skip = {_key(_validate(seeds[1])[0])}
    res2 = opt.run(_frame(), seed=1, validate=_validate, key_of=_key, evaluate=evaluate,
                   seeds=seeds, skip_keys=skip, trials=6)  # fmt: skip
    assert [r["why"] for r in res2["refused"]] == ["kind not in the rule library", "already tested"]
    assert not any(t["seeded"] for t in res2["trials"]) and skip.isdisjoint(
        t["key"] for t in res2["trials"]
    )


def test_nothing_is_promoted_when_the_search_looks_overfitted_or_nothing_is_steady():
    rng = random.Random(4)

    def noisy(df, c, spread_mult=1.0):
        r = rng.gauss(0.0, 1.0)
        return {"n_trades": 12, "net_return_pct": r, "net_pnl": r * 50}

    res = opt.run(_frame(), seed=2, validate=_validate, key_of=_key, evaluate=noisy, trials=24)
    best = res["best"]
    assert best is None or best["holds"] is False or res["pbo"] <= opt.MAX_PBO
    if best is not None and res["pbo"] is not None and res["pbo"] > opt.MAX_PBO:
        assert best["held_out"] is None and "overfitted" in res["verdict"]

    def losing(df, c, spread_mult=1.0):
        return {"n_trades": 12, "net_return_pct": -0.2, "net_pnl": -10}

    res = opt.run(_frame(), seed=2, validate=_validate, key_of=_key, evaluate=losing, trials=8)
    assert res["best"] is None and res["eligible"] == 0 and "no trial" in res["verdict"]
    with pytest.raises(ValueError):
        opt.run(_frame(1500), seed=1, validate=_validate, key_of=_key, evaluate=losing)

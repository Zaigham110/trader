"""Nightly strategy optimiser (D-069): a disciplined search over the rule library.

Before this, the nightly research tested at most three variants that a model proposed by hand.
This searches the same rule library with a fixed trial budget and stricter evidence, following the
shape of the search in QuantDinger's strategy-evolution module (read, not copied):

  * the search space is discrete and fixed in code (`SPACE`); every candidate still goes through
    `validate`, so nothing outside the rule library or the owner's limits can be tried;
  * sampling starts at random and then favours values that the better trials used
    (a small tree-structured Parzen estimator over categorical values); every fifth trial stays
    random, and results from earlier nights are fed back in as history;
  * a trial is scored on `FOLDS` consecutive slices of the in-sample period, never on one number:
    score = mean fold return minus half its spread across folds, so one lucky stretch cannot carry it;
  * the probability of backtest overfitting (PBO) is computed over the night's trials: how often
    the best rule on some folds is below the median on the others. A high value means the search
    is fitting noise, and then nothing is promoted that night;
  * the blind held-out period is touched once, for the single best trial, at the recorded spread
    and at 1.5 times the spread.

Passing all of that earns paper trading, never the account. No model call happens here.
"""

from __future__ import annotations

import itertools
import math
import random
from collections.abc import Callable
from typing import Any

Idea = dict[str, Any]

MINUTES = (5, 15, 30, 60, 120, 240)
STOPS = (1.0, 1.5, 3.0, 4.5, 6.0)
SPACE: dict[str, dict[str, tuple]] = {
    "trend_htf": {
        "minutes": MINUTES,
        "on_cross": (True, False),
        "stop_atr": STOPS,
        "target_atr": (1.5, 3.0, 4.5, 6.0, 9.0, 12.0, 20.0, 30.0),
    },
    "channel_break": {
        "minutes": MINUTES,
        "lookback": (10, 20, 30, 40, 55, 80),
        "stop_atr": STOPS,
        "target_atr": (3.0, 4.5, 6.0, 9.0, 12.0, 20.0, 30.0),
    },
    "range_fade": {
        "minutes": (5, 15, 30, 60),
        "lookback": (10, 20, 30, 50),
        "z_entry": (1.5, 2.0, 2.5, 3.0),
        "stop_atr": STOPS,
        "target_atr": (1.5, 3.0, 4.5, 6.0, 9.0, 12.0),
    },
    "pullback_htf": {
        "minutes": (5, 15, 30),
        "trend_minutes": (60, 120),
        "stop_atr": STOPS,
        "target_atr": (1.5, 3.0, 4.5, 6.0, 9.0, 12.0, 18.0),
    },
    "breakout_session": {
        "session": ("asia", "london"),
        "stop_atr": STOPS,
        "target_atr": (1.5, 3.0, 4.5, 7.5, 12.0, 18.0),
    },
}

TRIALS = 24
FOLDS = 4
HOLDOUT = 1 / 3
STARTUP = 8  # random trials before the sampler starts to favour what worked
GOOD_SHARE = 0.25
CANDIDATES_PER_STEP = 32
EXPLORE_EVERY = 5
MIN_TRADES = 20
MIN_TRADES_PER_FOLD = 3
MIN_POSITIVE_FOLDS = 3
MIN_TRADES_HELD_OUT = 10
MAX_PBO = 0.5
MIN_FOR_PBO = 4


def random_idea(rng: random.Random) -> Idea:
    kind = rng.choice(sorted(SPACE))
    return {"kind": kind, **{k: rng.choice(v) for k, v in SPACE[kind].items()}}


class Sampler:
    """Random at first, then values the better trials used. `history` is (idea, score) pairs."""

    def __init__(self, seed: int, history: list[tuple[Idea, float]] | None = None) -> None:
        self.rng = random.Random(seed)
        self.history: list[tuple[Idea, float]] = list(history or [])
        self.n = 0

    def tell(self, idea: Idea, score: float) -> None:
        self.history.append((idea, score))

    def _weights(self, group: list[Idea], kind: str | None, key: str, values: tuple) -> list[float]:
        pool = [i for i in group if kind is None or i.get("kind") == kind]
        return [1.0 + sum(1 for i in pool if i.get(key) == v) for v in values]  # +1 smoothing

    def propose(self) -> Idea:
        self.n += 1
        if len(self.history) < STARTUP or self.n % EXPLORE_EVERY == 0:
            return random_idea(self.rng)
        ranked = sorted(self.history, key=lambda h: h[1], reverse=True)
        n_good = max(3, int(len(ranked) * GOOD_SHARE))
        good, bad = [i for i, _ in ranked[:n_good]], [i for i, _ in ranked[n_good:]]
        kinds = tuple(sorted(SPACE))
        best: tuple[float, Idea] | None = None
        for _ in range(CANDIDATES_PER_STEP):
            kind = self.rng.choices(kinds, self._weights(good, None, "kind", kinds))[0]
            idea: Idea = {"kind": kind}
            for key, values in SPACE[kind].items():
                idea[key] = self.rng.choices(values, self._weights(good, kind, key, values))[0]
            gain = self._ratio(good, bad, None, "kind", kinds, kind)
            for key, values in SPACE[kind].items():
                gain += self._ratio(good, bad, kind, key, values, idea[key])
            if best is None or gain > best[0]:
                best = (gain, idea)
        assert best is not None
        return best[1]

    def _ratio(self, good, bad, kind, key, values, value) -> float:
        i = values.index(value)
        g, b = self._weights(good, kind, key, values), self._weights(bad, kind, key, values)
        return math.log((g[i] / sum(g)) / (b[i] / sum(b)))


def fold_score(returns: list[float], trades: list[int]) -> tuple[float, bool]:
    """(score, eligible). Mean fold return minus half its spread; eligible only with enough
    trades overall and in every fold, and profit in most folds."""
    n = len(returns)
    mean = sum(returns) / n
    spread = math.sqrt(sum((r - mean) ** 2 for r in returns) / n)
    ok = (
        sum(trades) >= MIN_TRADES
        and min(trades) >= MIN_TRADES_PER_FOLD
        and sum(1 for r in returns if r > 0) >= min(MIN_POSITIVE_FOLDS, n)
    )
    return round(mean - 0.5 * spread, 4), ok


def pbo(matrix: list[list[float]]) -> float | None:
    """Probability of backtest overfitting over rows = trials, columns = folds. For every way of
    choosing half the folds, take the trial that is best on them and see where it ranks on the
    other half; PBO is the share of splits where it lands in the bottom half."""
    rows = [r for r in matrix if len(r) >= 4]
    if len(rows) < MIN_FOR_PBO:
        return None
    k = len(rows[0])
    below = total = 0
    for train in itertools.combinations(range(k), k // 2):
        test = [j for j in range(k) if j not in train]
        best = max(range(len(rows)), key=lambda i: sum(rows[i][j] for j in train))
        out = [sum(r[j] for j in test) for r in rows]
        rank = sum(1 for v in out if v < out[best]) + 0.5 * (
            sum(1 for v in out if v == out[best]) - 1
        )
        below += 1 if rank / (len(rows) - 1) <= 0.5 else 0
        total += 1
    return round(below / total, 3)


def split(frame: Any, holdout: float = HOLDOUT, folds: int = FOLDS) -> tuple[list[Any], Any, int]:
    """(in-sample folds, held-out frame, split time). Folds are consecutive, equal in time."""
    t0, t1 = int(frame["time"].iloc[0]), int(frame["time"].iloc[-1])
    cut = int(t0 + (t1 - t0) * (1.0 - holdout))
    ins, held = frame[frame["time"] < cut], frame[frame["time"] >= cut]
    if len(ins) < 2000 or len(held) < 500:
        raise ValueError("not enough stored history to search")
    edges = [t0 + (cut - t0) * i // folds for i in range(folds + 1)]
    parts = [ins[(ins["time"] >= a) & (ins["time"] < b)] for a, b in itertools.pairwise(edges)]
    if min(len(p) for p in parts) < 300:
        raise ValueError("not enough stored history in every fold")
    return parts, held, cut


def run(
    frame: Any,
    *,
    seed: int,
    validate: Callable[[Idea], tuple[Any | None, str]],
    key_of: Callable[[Any], str],
    evaluate: Callable[..., dict[str, Any]],
    skip_keys: set[str] | None = None,
    history: list[tuple[Idea, float]] | None = None,
    seeds: list[Idea] | None = None,
    trials: int = TRIALS,
) -> dict[str, Any]:
    """Search, score on folds, compute PBO, and test the single best on the held-out period.

    `validate(idea)` returns (candidate, why_not); `evaluate(frame, candidate, spread_mult=...)`
    returns metrics with `n_trades`, `net_return_pct` and `net_pnl`. `seeds` are tried first
    (the research agent's ideas). Candidates in `skip_keys` were tested on an earlier night."""
    parts, held, cut = split(frame)
    sampler = Sampler(seed, history)
    seen = set(skip_keys or ())
    out: list[dict[str, Any]] = []
    refused: list[dict[str, str]] = []
    queue = list(seeds or [])
    attempts = 0
    while len(out) < trials and attempts < trials * 30:
        attempts += 1
        seeded = bool(queue)
        idea = queue.pop(0) if queue else sampler.propose()
        cand, why = validate(idea)
        if cand is None:
            if seeded:
                refused.append({"kind": str(idea.get("kind"))[:30], "why": why})
            continue
        key = key_of(cand)
        if key in seen:
            if seeded:
                refused.append({"kind": cand[0], "why": "already tested"})
            continue
        seen.add(key)
        folds = [evaluate(p, cand) for p in parts]
        rets = [float(m.get("net_return_pct") or 0.0) for m in folds]
        trades = [int(m.get("n_trades") or 0) for m in folds]
        score, ok = fold_score(rets, trades)
        sampler.tell(idea, score if sum(trades) >= MIN_TRADES else min(score, -1.0))
        out.append(
            {
                "candidate": cand,
                "key": key,
                "idea": idea,
                "seeded": seeded,
                "fold_returns": [round(r, 3) for r in rets],
                "fold_trades": trades,
                "n_trades": sum(trades),
                "net_return_pct": round(sum(rets), 3),
                "score": score,
                "eligible": ok,
            }
        )
    eligible = [t for t in out if t["eligible"]]
    overfit = pbo([t["fold_returns"] for t in out if t["n_trades"] >= MIN_TRADES])
    best = max(eligible, key=lambda t: t["score"]) if eligible else None
    verdict = "no trial was profitable across the folds with enough trades"
    if best is not None and best["score"] <= 0:
        best, verdict = None, "the best trial did not stay positive after the stability penalty"
    if best is not None and overfit is not None and overfit > MAX_PBO:
        verdict = f"search looks overfitted (PBO {overfit:.2f}): nothing is promoted tonight"
        best["held_out"] = None
        best["holds"] = False
    elif best is not None:  # the blind period is touched here, once, for one candidate
        h1 = evaluate(held, best["candidate"])
        h2 = evaluate(held, best["candidate"], spread_mult=1.5)
        best["held_out"], best["held_out_stress"] = h1, h2
        best["holds"] = bool(
            (h1.get("net_pnl") or 0) > 0
            and (h2.get("net_pnl") or 0) > 0
            and (h1.get("n_trades") or 0) >= MIN_TRADES_HELD_OUT
        )
        verdict = (
            "the best trial also held on the blind period"
            if best["holds"]
            else "the best trial failed on the blind period"
        )
    return {
        "trials": out,
        "refused": refused,
        "eligible": len(eligible),
        "pbo": overfit,
        "best": best,
        "verdict": verdict,
        "split_s": cut,
    }

"""The five gates a rule must pass, and the notebook they are recorded in (D-085).

    1 trades      enough trades to say anything: at least MIN_TRADES in all, MIN_PER_PERIOD each
    2 periods     a positive average result (in R) and a positive net in each of three periods
    3 robust      still positive with the spread 1.5 times wider, and with the stop and the
                  target each nudged 20% up and down
    4 luck        four checks (D-086): better than random entries in the same hours with the
                  same exits in all 20 runs; the entry alone beats random bars matched for
                  hour and volatility; the average per trade stays above zero when whole
                  trading days are resampled; and the result is still credible after counting
                  every rule ever tested (deflated Sharpe ratio)
    5 blind       positive on data no experiment has touched

All thresholds are constants in this file; no agent can change one. Gates 1 to 4 use the
*discovery window*, a fixed stretch of history. Gate 5 uses only bars outside it: older history
when it is added, and every day that passes. Until those hold enough trades the rule waits as
`awaiting_blind`; it is never judged on data it could have been fitted to.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import UTC, datetime
from typing import Any

import numpy as np

from goldtrader.backtest import stats
from goldtrader.backtest.costs import CostModel
from goldtrader.backtest.engine import run_backtest
from goldtrader.research import rules

# The discovery window. Everything stored up to 2026-11-24 had already been used by earlier
# studies when the lab was built, so none of it can serve as a blind test.
DISCOVERY_FROM_S = int(datetime(2026, 8, 6, tzinfo=UTC).timestamp())
DISCOVERY_TO_S = int(datetime(2026, 11, 24, tzinfo=UTC).timestamp())

MIN_TRADES = 100
MIN_PER_PERIOD = 25
STRESS_SPREAD = 1.5
NUDGE = 0.2
RANDOM_RUNS = 20
# All of them: the rule ranks first of 21, a chance of 1 in 21 (4.8%) if it were no better than
# random entries. 19 of 20 would be about 1 in 10 (D-086).
RANDOM_MUST_BEAT = 20
ENTRY_HORIZONS = (15, 60, 120)  # minutes after a signal at which the entry alone is judged
ENTRY_SIMS = 1000
ENTRY_MAX_P = 0.05  # after allowing for the three horizons
BOOT_SIMS = 2000
MIN_DSR = 0.90
MIN_BLIND_TRADES = 30
GATES = ("trades", "periods", "robust", "luck", "blind")


class RandomEntries:
    """The benchmark of gate 4: as many signals as the rule gave, in the same hours of the day and
    with the same sides, at random bars. Run through the same exits, it shows what the exits and
    the choice of hours alone are worth; the rule has to add something on top."""

    kind = "random"

    def __init__(self, like: Any, hours: Any, sides: Any, seed: int) -> None:
        self.like, self.seed, self.id = like, seed, "r"
        self.hours = np.asarray(hours, dtype=int)
        self.sides = np.asarray(sides, dtype=np.int8)

    def signals(self, df: Any) -> Any:
        from dataclasses import replace

        base = self.like.signals(df)
        rng = np.random.default_rng(self.seed)
        side = np.zeros(len(df), dtype=np.int8)
        hour = df["hour_utc"].to_numpy()[:-1]
        sides = rng.permutation(self.sides)
        k = 0
        for h in np.unique(self.hours):
            pool = np.flatnonzero(hour == h)
            n = min(int((self.hours == h).sum()), len(pool))
            if n:
                side[rng.choice(pool, size=n, replace=False)] = sides[k : k + n]
            k += n
        return replace(base, side=side)


def day_bootstrap_lower(rs: list[float], days: list[int], sims: int = BOOT_SIMS) -> float:
    """The 5th percentile of the average result per trade when whole trading days are resampled.
    Trades of one day share that day's market, so days, not trades, are what is independent."""
    if not rs:
        return 0.0
    by: dict[int, list[float]] = {}
    for r, d in zip(rs, days, strict=True):
        by.setdefault(int(d), []).append(float(r))
    sums = np.array([sum(v) for v in by.values()])
    counts = np.array([len(v) for v in by.values()])
    rng = np.random.default_rng(12345)
    pick = rng.integers(0, len(sums), size=(sims, len(sums)))
    means = sums[pick].sum(axis=1) / counts[pick].sum(axis=1)
    return float(np.quantile(means, 0.05))


class Lab:
    """Holds the price history and runs rules through the gates."""

    def __init__(self, frame: Any, cfg: Any, spec: Any, events: list | None = None) -> None:
        self.cfg, self.spec, self.events = cfg, spec, list(events or [])
        self.stop_max = float(cfg.owner.risk.stop_atr_max)
        self.full = frame
        t = frame["time"]
        self.discovery = (t >= DISCOVERY_FROM_S) & (t < DISCOVERY_TO_S)
        d = t[self.discovery]
        if len(d) < 3000:
            raise ValueError("not enough stored history inside the discovery window")
        a, b = int(d.iloc[0]), int(d.iloc[-1]) + 1
        self.cuts = [a, a + (b - a) // 3, a + 2 * (b - a) // 3, b]

    # ------------------------------------------------------------------ one backtest
    def _run(self, df: Any, strategy: Any, opts: dict[str, Any], spread: float = 1.0) -> dict:
        if len(df) < 500:
            return {
                "n": 0,
                "mean_r": 0.0,
                "net": 0.0,
                "win": 0.0,
                "rs": [],
                "signals": 0,
                "longs": 0.5,
                "days": [],
            }
        cost = CostModel(
            spread_multiplier=spread,
            commission_per_lot_per_side_usd=self.cfg.account.commission_per_lot_per_side_usd,
        )
        res = run_backtest(
            df, strategy, self.cfg, self.spec, cost=cost, events=self.events, equity0=5000.0, **opts
        )
        tr = res.trades
        rs = [float(x) for x in tr["r_multiple"]] if len(tr) else []
        return {
            "n": len(tr),
            "mean_r": round(float(np.mean(rs)), 4) if rs else 0.0,
            "net": round(float(tr["net_pnl"].sum()), 2) if len(tr) else 0.0,
            "win": round(float((tr["net_pnl"] > 0).mean()), 3) if len(tr) else 0.0,
            "rs": rs,
            "longs": float((tr["side"] == "long").mean()) if len(tr) else 0.5,
            "days": [int(x) // 86400 for x in tr["entry_time"]] if len(tr) else [],
        }

    def entry_test(self, df: Any, strategy: Any) -> dict[str, Any]:
        """Does the entry alone say anything? For every signal the rule gives (also those it could
        not trade because a position was open), the price move in the signal's direction over the
        next 15, 60 and 120 minutes, in ATR and after the spread, against random bars from the same
        hour of the day and the same third of volatility, with the same sides. No stop, no target.
        `p` is the share of ENTRY_SIMS random draws that did at least as well; `p_adjusted` is the
        smallest p times the number of horizons."""
        side = strategy.signals(df).side
        mid = df["mid_close"].to_numpy(dtype=float)
        atr = df["atr"].to_numpy(dtype=float)
        cost = (df["spread_close"] / df["atr"]).to_numpy(dtype=float)
        strata = df["hour_utc"].to_numpy(dtype=int) * 3 + np.minimum(
            2, (df["atr_pctile"].fillna(0.5).to_numpy(dtype=float) * 3).astype(int)
        )
        ok = np.isfinite(atr) & (atr > 0) & np.isfinite(cost)
        out: dict[str, Any] = {"signals": int((side != 0).sum()), "horizons": {}}
        rng = np.random.default_rng(2026)
        best_p = 1.0
        for h in ENTRY_HORIZONS:
            n = len(mid) - h
            if n < 100:
                continue
            move = np.full(len(mid), np.nan)
            move[:n] = (mid[h:] - mid[:n]) / atr[:n]
            valid = ok & np.isfinite(move)
            sig = np.flatnonzero((side != 0) & valid)
            if len(sig) < 30:
                continue
            s_side = side[sig].astype(float)
            real = float(np.mean(s_side * move[sig] - cost[sig]))
            pools = {k: np.flatnonzero(valid & (strata == k)) for k in np.unique(strata[sig])}
            groups = [(pools[k], int((strata[sig] == k).sum())) for k in pools]
            rand = np.empty(ENTRY_SIMS)
            for i in range(ENTRY_SIMS):
                idx = np.concatenate([rng.choice(pool, size=k) for pool, k in groups])
                rand[i] = np.mean(rng.permutation(s_side) * move[idx] - cost[idx])
            pv = float((1 + (rand >= real).sum()) / (ENTRY_SIMS + 1))
            best_p = min(best_p, pv)
            out["horizons"][str(h)] = {
                "n": int(len(sig)),
                "mean_atr": round(real, 4),
                "random_mean_atr": round(float(rand.mean()), 4),
                "p": round(pv, 4),
            }
        out["p_adjusted"] = round(min(1.0, best_p * max(1, len(out["horizons"]))), 4)
        return out

    def _part(self, df: Any, lo: int, hi: int) -> Any:
        return df[(df["time"] >= lo) & (df["time"] < hi)]

    # ------------------------------------------------------------------ the gates
    def evaluate(
        self,
        spec: dict[str, Any],
        *,
        n_trials: int = 1,
        stop_after: int = 5,
        trial_sr_var: float | None = None,
    ) -> dict:
        """Run a validated rule through the gates in order, stopping at the first it fails.
        Returns {gate, failed_at, status, ...numbers per gate}."""
        strat, opts = rules.compile_rule(spec)
        df = rules.prepare(self.full, spec)
        out: dict[str, Any] = {"gate": 0, "failed_at": None, "status": "failed"}

        def fail(name: str, why: str) -> dict:
            out["failed_at"], out["why"] = name, why
            return out

        # 1 and 2: three separate periods of the discovery window
        periods = [
            self._run(self._part(df, self.cuts[i], self.cuts[i + 1]), strat, opts) for i in range(3)
        ]
        rs = [r for p in periods for r in p["rs"]]
        out["periods"] = [{k: p[k] for k in ("n", "mean_r", "net", "win")} for p in periods]
        out["n"] = len(rs)
        out["mean_r"] = round(float(np.mean(rs)), 4) if rs else 0.0
        out["net"] = round(sum(p["net"] for p in periods), 2)
        if out["n"] < MIN_TRADES or min(p["n"] for p in periods) < MIN_PER_PERIOD:
            return fail(
                "trades",
                f"{out['n']} trades ({', '.join(str(p['n']) for p in periods)} per period); "
                f"needs {MIN_TRADES} in all and {MIN_PER_PERIOD} in each",
            )
        out["gate"] = 1
        disc = self._part(df, self.cuts[0], self.cuts[3])
        out["entry"] = self.entry_test(disc, strat)
        bad = [i for i, p in enumerate(periods) if p["mean_r"] <= 0 or p["net"] <= 0]
        if bad:
            return fail("periods", "not positive in period " + ", ".join("ABC"[i] for i in bad))
        out["gate"] = 2
        if stop_after <= 2:
            out["status"] = "pending"  # the costly gates run later, for the best only
            return out

        # 3: wider spread, nudged stop and target, over the whole discovery window
        checks: dict[str, float] = {}
        checks[f"spread x{STRESS_SPREAD:g}"] = self._run(disc, strat, opts, spread=STRESS_SPREAD)[
            "net"
        ]
        ex = spec["exit"]
        for name, factor in (
            ("stop", 1 - NUDGE),
            ("stop", 1 + NUDGE),
            ("target", 1 - NUDGE),
            ("target", 1 + NUDGE),
        ):
            e2 = dict(ex)
            if name == "stop":
                e2["stop_atr"] = round(min(self.stop_max, max(0.5, ex["stop_atr"] * factor)), 3)
            else:
                e2["target_atr"] = round(min(40.0, ex["target_atr"] * factor), 3)
            e2["target_atr"] = max(e2["target_atr"], e2["stop_atr"])
            s2, o2 = rules.compile_rule({**spec, "exit": e2})
            checks[f"{name} x{factor:g}"] = self._run(disc, s2, o2)["net"]
        out["robust"] = checks
        weak = [k for k, v in checks.items() if v <= 0]
        if weak:
            return fail("robust", "loses with " + ", ".join(weak))
        out["gate"] = 3

        # 4: better than luck, four ways (D-086)
        base = self._run(disc, strat, opts)
        sig_side = strat.signals(disc).side
        at = np.flatnonzero(sig_side != 0)
        hours = disc["hour_utc"].to_numpy()[at]
        beaten = 0
        rnd = []
        for seed in range(RANDOM_RUNS):
            r = self._run(disc, RandomEntries(strat, hours, sig_side[at], 1000 + seed), opts)
            rnd.append(r["mean_r"])
            beaten += base["mean_r"] > r["mean_r"]
        arr = np.asarray(base["rs"], dtype=float)
        sd = float(arr.std(ddof=1)) if len(arr) > 2 else 0.0
        sr = float(arr.mean() / sd) if sd > 0 else 0.0
        var = trial_sr_var if trial_sr_var and trial_sr_var > 0 else 1.0 / max(2, len(arr))
        skew = float(((arr - arr.mean()) ** 3).mean() / sd**3) if sd > 0 else 0.0
        kurt = float(((arr - arr.mean()) ** 4).mean() / sd**4) if sd > 0 else 3.0
        dsr = float(stats.deflated_sharpe(sr, max(1, n_trials), var, len(arr), skew, kurt))
        lower = day_bootstrap_lower(base["rs"], base["days"])
        entry_p = float(out["entry"].get("p_adjusted", 1.0))
        out["luck"] = {
            "beats_random": beaten,
            "of": RANDOM_RUNS,
            "random_mean_r": round(float(np.mean(rnd)), 4),
            "entry_p": entry_p,
            "by_day_lower_r": round(lower, 4),
            "trading_days": len(set(base["days"])),
            "sharpe_per_trade": round(sr, 4),
            "deflated_sharpe": round(dsr, 4) if math.isfinite(dsr) else 0.0,
            "rules_counted": n_trials,
        }
        if beaten < RANDOM_MUST_BEAT:
            return fail(
                "luck",
                f"beat random entries in the same hours in only {beaten} of {RANDOM_RUNS} runs "
                f"(needs all {RANDOM_MUST_BEAT})",
            )
        if entry_p > ENTRY_MAX_P:
            return fail(
                "luck",
                "the entry alone does no better than random bars from the same hours and "
                f"volatility (p {entry_p:.2f}, needs {ENTRY_MAX_P} or less)",
            )
        if lower <= 0:
            return fail(
                "luck",
                f"resampling whole trading days, the average per trade can be {lower:+.2f} R "
                "or worse: not reliably above zero",
            )
        if not math.isfinite(dsr) or dsr < MIN_DSR:
            return fail(
                "luck",
                f"after counting {n_trials} rules tried, the result is not credible "
                f"(deflated Sharpe {out['luck']['deflated_sharpe']:.2f}, needs {MIN_DSR})",
            )
        out["gate"] = 4

        # 5: data outside the discovery window, if there is enough of it yet
        blind = [
            self._part(df, int(df["time"].iloc[0]), self.cuts[0]),
            self._part(df, self.cuts[3], int(df["time"].iloc[-1]) + 1),
        ]
        bres = [self._run(b, strat, opts) for b in blind]
        bn, bnet = sum(b["n"] for b in bres), sum(b["net"] for b in bres)
        brs = [r for b in bres for r in b["rs"]]
        out["blind"] = {
            "n": bn,
            "net": round(bnet, 2),
            "mean_r": round(float(np.mean(brs)), 4) if brs else 0.0,
        }
        if bn < MIN_BLIND_TRADES:
            out["status"] = "awaiting_blind"
            out["why"] = (
                f"only {bn} trades outside the discovery window so far; needs {MIN_BLIND_TRADES}"
            )
            return out
        if bnet <= 0 or out["blind"]["mean_r"] <= 0:
            return fail("blind", "lost on the data it had never seen")
        out["gate"], out["status"] = 5, "passed"
        return out


# ------------------------------------------------------------------ the notebook
def propose(
    conn: sqlite3.Connection,
    spec: Any,
    *,
    source: str,
    rationale: str = "",
    expectation: str = "",
    now_ms: int,
    stop_atr_max: float = 10.0,
) -> tuple[int | None, str]:
    """Write a rule into the notebook before it is tested. Returns (id, "") or (None, reason):
    an invalid rule and one already in the notebook are both refused."""
    try:
        canon = rules.validate(spec, stop_atr_max)
    except rules.RuleError as e:
        return None, str(e)
    k = rules.key(canon)
    if conn.execute("SELECT 1 FROM research_ideas WHERE key=?", (k,)).fetchone():
        return None, "already in the notebook"
    cur = conn.execute(
        "INSERT INTO research_ideas(key, created_ms, source, spec_json, what, rationale, expectation) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            k,
            now_ms,
            source,
            json.dumps(canon, sort_keys=True),
            rules.describe(canon),
            " ".join(str(rationale).split())[:600],
            " ".join(str(expectation).split())[:300],
        ),
    )
    return int(cur.lastrowid), ""


def record(conn: sqlite3.Connection, idea_id: int, result: dict[str, Any], now_ms: int) -> None:
    conn.execute(
        "UPDATE research_ideas SET status=?, gate=?, failed_at=?, results_json=?, tested_ms=? WHERE id=?",
        (
            result["status"],
            result["gate"],
            result.get("failed_at"),
            json.dumps(result),
            now_ms,
            idea_id,
        ),
    )


def trial_stats(conn: sqlite3.Connection) -> tuple[int, float | None]:
    """(rules tested so far, variance of their per-trade Sharpe ratios) for the luck gate."""
    n = conn.execute("SELECT COUNT(*) FROM research_ideas WHERE tested_ms IS NOT NULL").fetchone()[
        0
    ]
    srs = []
    for (js,) in conn.execute(
        "SELECT results_json FROM research_ideas WHERE tested_ms IS NOT NULL AND results_json IS NOT NULL"
    ):
        try:
            r = json.loads(js)
            if r.get("n", 0) >= 30 and "mean_r" in r:
                srs.append(float(r["mean_r"]) / 1.6)  # R per trade over a typical spread of R
        except (ValueError, TypeError):
            continue
    return int(n), (float(np.var(srs, ddof=1)) if len(srs) >= 10 else None)


def notebook(conn: sqlite3.Connection, limit: int = 60) -> list[dict[str, Any]]:
    """The latest ideas with their outcome, for the agent's next round and for the dashboard."""
    out = []
    for r in conn.execute(
        "SELECT id, created_ms, source, spec_json, what, rationale, expectation, status, gate, "
        "failed_at, results_json, lesson, paper_bundle FROM research_ideas ORDER BY id DESC LIMIT ?",
        (limit,),
    ):
        d = dict(
            zip(
                (
                    "id",
                    "created_ms",
                    "source",
                    "spec",
                    "what",
                    "rationale",
                    "expectation",
                    "status",
                    "gate",
                    "failed_at",
                    "results",
                    "lesson",
                    "paper_bundle",
                ),
                r,
                strict=True,
            )
        )
        d["spec"] = json.loads(d["spec"])
        d["results"] = json.loads(d["results"]) if d["results"] else None
        out.append(d)
    return out

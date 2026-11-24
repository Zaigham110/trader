"""The five gates a rule must pass, and the notebook they are recorded in (D-085).

    1 trades      enough trades to say anything: at least MIN_TRADES in all, MIN_PER_PERIOD each
    2 periods     a positive average result (in R) and a positive net in each of three periods
    3 robust      still positive with the spread 1.5 times wider, and with the stop and the
                  target each nudged 20% up and down
    4 luck        better than the same exits with random entries, and still credible after
                  counting every rule ever tested (deflated Sharpe ratio)
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
RANDOM_MUST_BEAT = 19  # of RANDOM_RUNS: about the 95th percentile
MIN_DSR = 0.90
MIN_BLIND_TRADES = 30
GATES = ("trades", "periods", "robust", "luck", "blind")


class RandomEntries:
    """The benchmark of gate 4: as many signals as the rule gave, with the same share of longs,
    at random bars. Run through the same exits, it shows what the exits alone are worth."""

    kind = "random"

    def __init__(self, like: Any, n_signals: int, long_share: float, seed: int) -> None:
        self.like, self.n, self.long_share, self.seed, self.id = (
            like,
            n_signals,
            long_share,
            seed,
            "r",
        )

    def signals(self, df: Any) -> Any:
        from dataclasses import replace

        base = self.like.signals(df)
        rng = np.random.default_rng(self.seed)
        side = np.zeros(len(df), dtype=np.int8)
        n = min(self.n, len(df) - 2)
        if n > 0:
            idx = rng.choice(len(df) - 1, size=n, replace=False)
            side[idx] = np.where(rng.random(n) < self.long_share, 1, -1)
        return replace(base, side=side)


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
        }

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
        bad = [i for i, p in enumerate(periods) if p["mean_r"] <= 0 or p["net"] <= 0]
        if bad:
            return fail("periods", "not positive in period " + ", ".join("ABC"[i] for i in bad))
        out["gate"] = 2
        if stop_after <= 2:
            out["status"] = "pending"  # the costly gates run later, for the best only
            return out

        # 3: wider spread, nudged stop and target, over the whole discovery window
        disc = self._part(df, self.cuts[0], self.cuts[3])
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

        # 4: better than luck
        base = self._run(disc, strat, opts)
        n_sig = int((strat.signals(disc).side != 0).sum())
        beaten = 0
        rnd = []
        for seed in range(RANDOM_RUNS):
            r = self._run(disc, RandomEntries(strat, n_sig, base["longs"], 1000 + seed), opts)
            rnd.append(r["mean_r"])
            beaten += base["mean_r"] > r["mean_r"]
        arr = np.asarray(base["rs"], dtype=float)
        sd = float(arr.std(ddof=1)) if len(arr) > 2 else 0.0
        sr = float(arr.mean() / sd) if sd > 0 else 0.0
        var = trial_sr_var if trial_sr_var and trial_sr_var > 0 else 1.0 / max(2, len(arr))
        skew = float(((arr - arr.mean()) ** 3).mean() / sd**3) if sd > 0 else 0.0
        kurt = float(((arr - arr.mean()) ** 4).mean() / sd**4) if sd > 0 else 3.0
        dsr = float(stats.deflated_sharpe(sr, max(1, n_trials), var, len(arr), skew, kurt))
        out["luck"] = {
            "beats_random": beaten,
            "of": RANDOM_RUNS,
            "random_mean_r": round(float(np.mean(rnd)), 4),
            "sharpe_per_trade": round(sr, 4),
            "deflated_sharpe": round(dsr, 4) if math.isfinite(dsr) else 0.0,
            "rules_counted": n_trials,
        }
        if beaten < RANDOM_MUST_BEAT:
            return fail("luck", f"beat random entries in only {beaten} of {RANDOM_RUNS} runs")
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

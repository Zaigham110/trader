"""Strategy search with a held-out period (D-059).

Question: is there a version of the trend idea that survives the real, recorded trading costs?
The one-minute `trend_atr` does not (D-058). Here the same idea is decided on slower bars
(`TrendHTF`) with wider stops, over a small fixed grid.

Honesty rules, all in code:
  * The stored history is split by time. Candidates are ranked on the earlier part only
    (in-sample). The later part (held out) is used once, for the few best, never for ranking.
  * Every candidate tried is returned and can be written to the trial ledger, so later
    significance checks (deflated Sharpe) count all of them.
  * A finalist "holds" only if it is profitable on the held-out part at the recorded spread AND
    at 1.5 times the spread, with enough trades to mean something.

Sixty days of data cannot prove an edge; a finalist that holds is a candidate for demo trading in
the learning pipeline, nothing more.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable
from typing import Any

import pandas as pd

from goldtrader.backtest.costs import CostModel
from goldtrader.backtest.engine import run_backtest
from goldtrader.backtest.metrics import compute_metrics
from goldtrader.backtest.strategies import build_strategy
from goldtrader.backtest.timemachine import _clean, _events, load_frame
from goldtrader.config.schema import Config
from goldtrader.types import SymbolSpec

Candidate = tuple[str, str, dict[str, float]]  # kind, id, params

MINUTES = (5, 15, 30, 60)
STOP_K = (1.0, 1.5, 2.5)  # stop = k * sqrt(minutes) one-minute ATRs
REWARD = (1.5, 2.5)  # target = stop * reward
MIN_TRADES_IN_SAMPLE = 20
MIN_TRADES_HELD_OUT = 10
FINALISTS = 5
KEEP = ("n_trades", "net_pnl", "net_return_pct", "hit_rate", "profit_factor", "expectancy_r")
KEEP += ("max_drawdown_pct", "sharpe", "total_costs_usd", "cost_share_of_gross", "killed")


def grid() -> list[Candidate]:
    out: list[Candidate] = []
    for minutes in MINUTES:
        for on_cross in (1.0, 0.0):
            for k in STOP_K:
                for rr in REWARD:
                    stop = round(k * math.sqrt(minutes), 2)
                    params = {
                        "minutes": float(minutes),
                        "on_cross": on_cross,
                        "stop_atr": stop,
                        "target_atr": round(stop * rr, 2),
                    }
                    name = f"htf_m{minutes}_{'cross' if on_cross else 'state'}_s{k}_r{rr}"
                    out.append(("trend_htf", name, params))
    return out


def effective(c: Candidate, cfg: Config) -> Candidate:
    """The candidate as the engine, the shadow ledger and the live gate will actually run it:
    the stop is clamped into the owner's limits and the target is at least stop x minimum
    reward/risk (`engine.py`). Two grid entries that clamp to the same thing are one candidate."""
    kind, sid, p = c
    r = cfg.owner.risk
    q = dict(p)
    if "stop_atr" in q:
        q["stop_atr"] = min(max(float(q["stop_atr"]), r.stop_atr_min), r.stop_atr_max)
        q["target_atr"] = max(float(q.get("target_atr", 0.0)), q["stop_atr"] * r.min_reward_risk)
    return kind, sid, q


def describe(c: Candidate) -> str:
    kind, sid, p = c
    if kind != "trend_htf":
        return f"{sid} (current live strategy, 1-minute bars)"
    how = "EMA cross" if p.get("on_cross") else "trend state"
    return (
        f"{int(p['minutes'])}-minute bars, {how}, stop {p['stop_atr']} x 1-minute ATR, "
        f"target {p['target_atr']} x 1-minute ATR"
    )


def unique(cands: list[Candidate], cfg: Config) -> list[Candidate]:
    seen: set[str] = set()
    out: list[Candidate] = []
    for c in cands:
        e = effective(c, cfg)
        key = e[0] + repr(sorted(e[2].items()))
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


def evaluate(
    df: pd.DataFrame,
    c: Candidate,
    cfg: Config,
    spec: SymbolSpec,
    events: list,
    *,
    spread_mult: float = 1.0,
    equity: float = 5000.0,
) -> dict[str, Any]:
    cost = CostModel(
        spread_multiplier=spread_mult,
        commission_per_lot_per_side_usd=cfg.account.commission_per_lot_per_side_usd,
    )
    res = run_backtest(df, build_strategy(*c), cfg, spec, cost=cost, events=events, equity0=equity)
    m = compute_metrics(res, equity)
    out = {k: m.get(k) for k in KEEP}
    out["n_days"] = m.get("n_days")
    return out


def run_search(
    conn: sqlite3.Connection,
    cfg: Config,
    spec: SymbolSpec,
    *,
    holdout: float = 1 / 3,
    candidates: list[Candidate] | None = None,
    on_trial: Callable[[Candidate, dict[str, Any], pd.DataFrame], None] | None = None,
    frame: pd.DataFrame | None = None,
) -> dict[str, Any]:
    if not 0.1 <= holdout <= 0.5:
        raise ValueError("holdout must be between 0.1 and 0.5")
    full = frame if frame is not None else load_frame(conn, cfg)
    t0, t1 = int(full["time"].iloc[0]), int(full["time"].iloc[-1])
    cut = int(t0 + (t1 - t0) * (1.0 - holdout))
    ins, held = full[full["time"] < cut], full[full["time"] >= cut]
    if len(ins) < 1000 or len(held) < 500:
        raise ValueError("not enough stored history to split into two periods")
    ev_in, ev_out = _events(conn, t0, cut), _events(conn, cut, t1)

    live = [s for s in cfg.strategies if s.enabled] or list(cfg.strategies)
    baseline: Candidate = (live[0].kind, live[0].id, dict(live[0].params))
    tried: list[dict[str, Any]] = []
    pool = unique(list(candidates) if candidates is not None else grid(), cfg)
    for c in [effective(baseline, cfg), *pool]:
        m = evaluate(ins, c, cfg, spec, ev_in)
        if on_trial is not None:
            on_trial(c, m, ins)
        tried.append({"candidate": c, "what": describe(c), "in_sample": m})

    def rank_key(row: dict[str, Any]) -> float:
        return float(row["in_sample"].get("net_return_pct") or -1e9)

    ranked = sorted(
        (r for r in tried[1:] if (r["in_sample"]["n_trades"] or 0) >= MIN_TRADES_IN_SAMPLE),
        key=rank_key,
        reverse=True,
    )
    finalists = [r for r in ranked[:FINALISTS] if rank_key(r) > 0]
    for r in [tried[0], *finalists]:  # the held-out period is touched here and only here
        r["held_out"] = evaluate(held, r["candidate"], cfg, spec, ev_out)
        r["held_out_stress"] = evaluate(held, r["candidate"], cfg, spec, ev_out, spread_mult=1.5)
        r["holds"] = bool(
            (r["held_out"]["net_pnl"] or 0) > 0
            and (r["held_out_stress"]["net_pnl"] or 0) > 0
            and (r["held_out"]["n_trades"] or 0) >= MIN_TRADES_HELD_OUT
        )
    return _clean(
        {
            "range": {"start_s": t0, "split_s": cut, "end_s": t1},
            "bars": {"in_sample": len(ins), "held_out": len(held)},
            "n_tried": len(tried) - 1,
            "n_profitable_in_sample": sum(1 for r in tried[1:] if rank_key(r) > 0),
            "baseline": tried[0],
            "finalists": finalists,
            "all": [
                {"id": r["candidate"][1], "what": r["what"], "in_sample": r["in_sample"]}
                for r in tried
            ],
        }
    )

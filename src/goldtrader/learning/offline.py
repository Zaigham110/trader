"""Offline gate stage (LEARNING_SYSTEM §3.2; R-031; research/04 §1).

`run_offline` backtests the candidate bundle and its single-parameter neighbourhood (±25 %, ±50 %
of the changed numeric parameter → 5 runs including the candidate; a rule or prompt change has no
numeric neighbourhood, so just the candidate) plus the champion on the same features frame, records
EVERY run in the append-only trial ledger (they all count toward N in the Deflated Sharpe Ratio —
"a demo period is not a free lunch", research/04 §7), then applies the backtest gate
(`backtest.gate.offline_gate`: trades, days, Sharpe, DSR with N = full ledger, minimum backtest
length, PBO over the neighbourhood + champion matrix, Harvey–Liu haircut, random-timing permutation)
with thresholds from the owner's learning gates, and three pipeline checks on top:

* `beats_champion` — candidate Sharpe ≥ champion Sharpe + `min_sharpe_gain` on the same data;
* `pre_registered_metric` — the metric named at intake is one of sharpe / net_return_pct /
  expectancy_r / max_drawdown_pct and improved versus the champion (lower drawdown = improvement);
* `bh_fdr` — Benjamini–Hochberg at `offline_fdr` across the week's batch of candidate permutation
  p-values (`stats.multiple_testing_adjust`, Harvey & Liu 2015); the candidate's adjusted p must be
  ≤ the FDR.

The decision (DEMO or REJECTED, every check listed) is applied to the experiment and logged.
Nothing here writes config (R-034).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import Any

import numpy as np
import pandas as pd

from goldtrader.backtest.costs import CostModel
from goldtrader.backtest.engine import BacktestResult, run_backtest
from goldtrader.backtest.gate import GateReport, offline_gate, sweep
from goldtrader.backtest.metrics import compute_metrics
from goldtrader.backtest.stats import multiple_testing_adjust
from goldtrader.backtest.strategies import build_strategy
from goldtrader.db.repo import Repo
from goldtrader.learning.gates import LearningGates, gates_for
from goldtrader.learning.intake import METRICS
from goldtrader.learning.store import WEEK_MS, LearningStore
from goldtrader.learning.types import Experiment, GateCheck, Stage, StageDecision

NEIGHBOURHOOD = (0.5, 0.75, 1.0, 1.25, 1.5)
"""Multipliers of the changed numeric parameter (±25 %, ±50 % around the candidate)."""

_INT_PARAMS = frozenset(
    {
        "ema_fast",
        "ema_slow",
        "atr_period",
        "range_start_hour",
        "range_end_hour",
        "trade_start_hour",
        "trade_end_hour",
        "entry_on_flip_only",
    }
)


def neighbourhood(exp: Experiment, candidate_params: Mapping[str, float]) -> dict[str, list[float]]:
    """Sweep grid for the candidate: the changed parameter at NEIGHBOURHOOD × its value (deduped,
    integer parameters rounded, non-positive values dropped), every other parameter fixed."""
    grid: dict[str, list[float]] = {k: [float(v)] for k, v in candidate_params.items()}
    ch = exp.change
    if ch.change_type != "param" or ch.key not in candidate_params:
        return grid
    base = float(candidate_params[ch.key])
    values: list[float] = []
    for m in NEIGHBOURHOOD:
        v = base * m
        if ch.key in _INT_PARAMS:
            v = float(round(v))
        if v > 0.0 and v not in values:
            values.append(v)
    if base not in values:
        values.append(base)
    grid[ch.key] = sorted(values)
    return grid


def _ledger_row(
    repo: Repo,
    bundle: str,
    change_type: str,
    description: str,
    m: Mapping[str, Any],
    cost: CostModel,
    snapshot: str,
    status: str,
    **extra: Any,
) -> None:
    calmar = m.get("calmar")
    repo.append_trial(
        bundle_id=bundle,
        change_type=change_type,
        description=description,
        data_snapshot_id=snapshot,
        cost_model_json=cost.as_dict(),
        n_trades=int(m.get("n_trades", 0)),
        n_days=int(m.get("n_days", 0)),
        sharpe=m.get("sharpe"),
        sortino=m.get("sortino"),
        calmar=calmar if calmar is not None and np.isfinite(calmar) else None,
        net_return_pct=m.get("net_return_pct"),
        max_drawdown_pct=m.get("max_drawdown_pct"),
        worst_day_pct=m.get("worst_day_pct"),
        skew=m.get("skew", 0.0),
        kurt=m.get("kurt", 0.0),
        created_by="pipeline",
        status=status,
        **extra,
    )


def _metric_improved(
    metric: str, cand: Mapping[str, Any], champ: Mapping[str, Any]
) -> tuple[bool, str]:
    a, b = cand.get(metric), champ.get(metric)
    if a is None or b is None or not (np.isfinite(a) and np.isfinite(b)):
        return False, f"{metric}: unavailable"
    if metric == "max_drawdown_pct":
        return float(a) < float(b), f"{metric}: {a:.4g} < champion {b:.4g}"
    return float(a) > float(b), f"{metric}: {a:.4g} > champion {b:.4g}"


def _clean(d: Mapping[str, Any]) -> dict[str, Any]:
    """Metrics → JSON-safe scalars (drop nested tables, NaN → None)."""
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, (int, float, np.integer, np.floating)):
            f = float(v)
            out[k] = None if not math.isfinite(f) else f
        elif isinstance(v, (str, bool)) or v is None:
            out[k] = v
    return out


def run_offline(
    experiment: Experiment,
    store: LearningStore,
    repo: Repo,
    cfg: Any,
    spec: Any,
    df_features: pd.DataFrame,
    *,
    equity0: float = 5000.0,
    rng: np.random.Generator | None = None,
    now_ms: int | Callable[[], int] | None = None,
    data_snapshot_id: str = "unknown",
    gates: LearningGates | None = None,
    events: Any = (),
) -> StageDecision:
    """Run the offline gate for `experiment` and apply the decision (see module doc)."""
    g = gates_for(cfg, gates)
    rng = rng or np.random.default_rng(0)
    now = int(now_ms() if callable(now_ms) else (now_ms if now_ms is not None else 0))
    exp = experiment
    if exp.stage == Stage.PROPOSED:
        store.update_stage(exp.experiment_id, Stage.OFFLINE, now)
        store.log_event(
            StageDecision(exp.experiment_id, Stage.PROPOSED, Stage.OFFLINE, [], actor="pipeline"),
            now,
        )
        exp.stage, exp.stage_entered_ms = Stage.OFFLINE, now

    cand = store.bundle(exp.candidate_bundle_id)
    champ = store.bundle(exp.champion_bundle_id)
    if cand is None or champ is None:
        return _decide(
            store,
            exp,
            Stage.REJECTED,
            [GateCheck("bundles", False, "candidate or champion bundle missing from ledger")],
            now,
        )
    cand_params = {k: float(v) for k, v in cand["params"].items()}
    champ_params = {k: float(v) for k, v in champ["params"].items()}
    cost = CostModel(
        spread_multiplier=g.spread_multiplier,
        commission_per_lot_per_side_usd=cfg.account.commission_per_lot_per_side_usd,
    )

    # 1. candidate neighbourhood sweep (not recorded by sweep: we record every run ourselves so
    #    the candidate's row carries the gate statistics)
    grid = neighbourhood(exp, cand_params)
    sw = sweep(
        df_features,
        str(cand["strategy_kind"]),
        exp.strategy_id,
        grid,
        cfg,
        spec,
        repo=None,
        cost=cost,
        equity0=equity0,
        events=events,
    )
    cand_idx = next(
        (i for i, (p, _, _) in enumerate(sw.runs) if p == cand_params),
        sw.best_index,
    )
    # 2. champion on the same data
    champ_strat = build_strategy(str(champ["strategy_kind"]), exp.strategy_id, champ_params)
    champ_res: BacktestResult = run_backtest(
        df_features, champ_strat, cfg, spec, cost=cost, equity0=equity0, events=events
    )
    champ_m = compute_metrics(champ_res, equity0)
    matrix = sw.daily_matrix.copy()
    matrix[matrix.shape[1]] = champ_res.daily_returns
    matrix = matrix.fillna(0.0)

    # 3. ledger: neighbourhood + champion first (they count toward N for the DSR)
    for i, (p, _, m) in enumerate(sw.runs):
        if i == cand_idx:
            continue
        _ledger_row(
            repo,
            exp.candidate_bundle_id,
            "param",
            f"neighbourhood of {exp.experiment_id}: {p}",
            m,
            cost,
            data_snapshot_id,
            "recorded",
        )
    _ledger_row(
        repo,
        exp.champion_bundle_id,
        "baseline",
        f"champion for {exp.experiment_id}",
        champ_m,
        cost,
        data_snapshot_id,
        "recorded",
    )

    # 4. backtest gate with N = the whole ledger (including the rows just written)
    _, cand_res, cand_m = sw.runs[cand_idx]
    report: GateReport = offline_gate(
        cand_res,
        cand_m,
        df_features,
        thresholds=g.offline_thresholds(),
        ledger_sharpes=repo.trial_sharpes() + [float(cand_m.get("sharpe", 0.0))],
        daily_matrix=matrix,
        rng=rng,
    )
    checks = [GateCheck(k, ok, note) for k, (ok, note) in report.checks.items()]

    # 5. pipeline checks
    cand_sr, champ_sr = float(cand_m.get("sharpe", 0.0)), float(champ_m.get("sharpe", 0.0))
    checks.append(
        GateCheck(
            "beats_champion",
            cand_sr >= champ_sr + g.min_sharpe_gain,
            f"sharpe {cand_sr:.2f} >= champion {champ_sr:.2f} + {g.min_sharpe_gain}",
        )
    )
    metric = exp.pre_registered_metric
    if metric in METRICS:
        ok, note = _metric_improved(metric, cand_m, champ_m)
        checks.append(GateCheck("pre_registered_metric", ok, note))
    else:
        checks.append(
            GateCheck("pre_registered_metric", False, f"{metric!r} not in {list(METRICS)}")
        )

    perm_p = report.perm_p
    store.set_stats(
        exp.experiment_id,
        {
            "offline": {
                "at_ms": now,
                "perm_p": perm_p,
                "pbo": report.pbo,
                "dsr": report.dsr,
                "haircut_sr": report.haircut_sr,
                "mc_dd_p95": report.mc_dd_p95,
                "n_trials_counted": report.n_trials_counted,
                "n_runs": len(sw.runs) + 1,
                "candidate": _clean(cand_m),
                "champion": _clean(champ_m),
            }
        },
    )
    batch = store.offline_batch(now - WEEK_MS)
    pvals = {e.experiment_id: e.stats["offline"]["perm_p"] for e in batch}
    if perm_p is not None:
        pvals[exp.experiment_id] = perm_p
    usable = {k: float(v) for k, v in pvals.items() if v is not None}
    if perm_p is None:
        checks.append(GateCheck("bh_fdr", False, "no permutation p-value (too few trades)"))
        adj_p = None
    else:
        ids = sorted(usable)
        adj = multiple_testing_adjust([usable[i] for i in ids], method="bh")
        adj_p = float(adj[ids.index(exp.experiment_id)])
        checks.append(
            GateCheck(
                "bh_fdr",
                adj_p <= g.offline_fdr,
                f"BH-adjusted p {adj_p:.3f} <= {g.offline_fdr} over {len(ids)} this week",
            )
        )
    passed = all(c.ok for c in checks)
    _ledger_row(
        repo,
        exp.candidate_bundle_id,
        exp.change.change_type,
        f"candidate of {exp.experiment_id}: {exp.change.key}={exp.change.value}",
        cand_m,
        cost,
        data_snapshot_id,
        "passed_offline" if passed else "failed_offline",
        pbo=report.pbo,
        dsr=report.dsr,
        haircut_sr=report.haircut_sr,
        perm_p=perm_p,
    )
    store.set_stats(exp.experiment_id, {"offline_bh_adjusted_p": adj_p})
    return _decide(store, exp, Stage.DEMO if passed else Stage.REJECTED, checks, now)


def _decide(
    store: LearningStore, exp: Experiment, to: Stage, checks: list[GateCheck], now: int
) -> StageDecision:
    d = StageDecision(exp.experiment_id, exp.stage, to, checks, actor="pipeline")
    if to == Stage.REJECTED:
        failed = ", ".join(c.name for c in checks if not c.ok) or "offline gate"
        store.close(exp.experiment_id, Stage.REJECTED, f"offline: {failed}", now)
    else:
        store.update_stage(exp.experiment_id, to, now)
    store.log_event(d, now)
    exp.stage, exp.stage_entered_ms = to, now
    return d

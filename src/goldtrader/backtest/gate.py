"""Offline gate and trial ledger (LEARNING_SYSTEM §3.2, R-031).

`sweep()` runs a bundle over a parameter grid and records every run in the trial ledger.
`offline_gate()` evaluates the selected run against the gate using the full ledger count for the
Deflated Sharpe Ratio and the sweep's daily-return matrix for the Probability of Backtest
Overfitting. Thresholds are the LEARNING_SYSTEM defaults and are passed in explicitly so they can
live in the owner-only config section (not learnable).
"""

from __future__ import annotations

import hashlib
import itertools
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from goldtrader.backtest.costs import CostModel
from goldtrader.backtest.engine import BacktestResult, run_backtest
from goldtrader.backtest.metrics import TRADING_DAYS, compute_metrics
from goldtrader.backtest.stats import (
    deflated_sharpe,
    haircut_sharpe,
    min_backtest_length_years,
    monte_carlo_drawdown,
    pbo_cscv,
    permutation_test_random_timing,
    sharpe,
    skew_kurt,
)
from goldtrader.backtest.strategies import build_strategy
from goldtrader.config.schema import Config
from goldtrader.db.repo import Repo
from goldtrader.types import SymbolSpec


@dataclass(frozen=True)
class GateThresholds:
    min_trades: int = 100
    min_days: int = 120
    min_sharpe: float = 0.8
    max_pbo: float = 0.05
    min_dsr: float = 0.95
    min_haircut_sr: float = 0.5
    max_perm_p: float = 0.05
    spread_multiplier: float = 1.5


@dataclass
class GateReport:
    passed: bool
    checks: dict[str, tuple[bool, str]] = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    pbo: float | None = None
    dsr: float | None = None
    haircut_sr: float | None = None
    perm_p: float | None = None
    mc_dd_p95: float | None = None
    n_trials_counted: int = 0

    def as_dict(self) -> dict:
        d = asdict(self)
        d["checks"] = {k: {"ok": v[0], "note": v[1]} for k, v in self.checks.items()}
        return d


def bundle_id(
    strategy_id: str, kind: str, params: dict, config_hash: str, parent: str | None = None
) -> str:
    body = json.dumps(
        {"s": strategy_id, "k": kind, "p": params, "c": config_hash, "parent": parent},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def param_grid(grid: dict[str, list[float]]) -> list[dict[str, float]]:
    keys = sorted(grid)
    return [dict(zip(keys, combo)) for combo in itertools.product(*(grid[k] for k in keys))]


@dataclass
class SweepResult:
    runs: list[tuple[dict, BacktestResult, dict]]  # params, result, metrics
    daily_matrix: pd.DataFrame  # T × N daily returns (columns = run index)
    best_index: int


def sweep(
    df: pd.DataFrame,
    kind: str,
    strategy_id: str,
    grid: dict[str, list[float]],
    cfg: Config,
    spec: SymbolSpec,
    *,
    repo: Repo | None = None,
    data_snapshot_id: str = "unknown",
    config_hash: str = "",
    cost: CostModel | None = None,
    equity0: float = 5000.0,
    created_by: str = "owner",
    events=(),
) -> SweepResult:
    runs: list[tuple[dict, BacktestResult, dict]] = []
    cols: dict[int, pd.Series] = {}
    for i, params in enumerate(param_grid(grid)):
        strat = build_strategy(kind, strategy_id, params)
        res = run_backtest(df, strat, cfg, spec, cost=cost, equity0=equity0, events=events)
        m = compute_metrics(res, equity0)
        runs.append((params, res, m))
        cols[i] = res.daily_returns
        if repo is not None:
            bid = bundle_id(strategy_id, kind, params, config_hash)
            repo.record_bundle(bid, strategy_id, kind, params, config_hash)
            sk, ku = (m.get("skew", 0.0), m.get("kurt", 0.0))
            repo.append_trial(
                bundle_id=bid,
                change_type="param",
                description=f"sweep {kind} {params}",
                data_snapshot_id=data_snapshot_id,
                cost_model_json=(cost or CostModel()).as_dict(),
                n_trades=m["n_trades"],
                n_days=m["n_days"],
                sharpe=m.get("sharpe"),
                sortino=m.get("sortino"),
                calmar=m.get("calmar") if np.isfinite(m.get("calmar", float("nan"))) else None,
                net_return_pct=m.get("net_return_pct"),
                max_drawdown_pct=m.get("max_drawdown_pct"),
                worst_day_pct=m.get("worst_day_pct"),
                skew=sk,
                kurt=ku,
                created_by=created_by,
                status="recorded",
            )
    matrix = pd.DataFrame(cols).fillna(0.0)
    sharpes = [m.get("sharpe", 0.0) for _, _, m in runs]
    best = int(np.argmax(sharpes)) if sharpes else 0
    return SweepResult(runs=runs, daily_matrix=matrix, best_index=best)


def offline_gate(
    res: BacktestResult,
    metrics: dict,
    df: pd.DataFrame,
    *,
    thresholds: GateThresholds,
    ledger_sharpes: list[float],
    daily_matrix: pd.DataFrame | None = None,
    rng: np.random.Generator | None = None,
) -> GateReport:
    rng = rng or np.random.default_rng(0)
    checks: dict[str, tuple[bool, str]] = {}
    n_trades, n_days = metrics.get("n_trades", 0), metrics.get("n_days", 0)
    checks["min_trades"] = (
        n_trades >= thresholds.min_trades,
        f"{n_trades} >= {thresholds.min_trades}",
    )
    checks["min_days"] = (n_days >= thresholds.min_days, f"{n_days} >= {thresholds.min_days}")
    sr = float(metrics.get("sharpe", 0.0))
    checks["min_sharpe"] = (sr >= thresholds.min_sharpe, f"{sr:.2f} >= {thresholds.min_sharpe}")

    # Deflated Sharpe: N = every trial recorded in the ledger (honest trial accounting)
    n_trials = max(len(ledger_sharpes), 1)
    var_sr = float(np.var(ledger_sharpes, ddof=1)) if len(ledger_sharpes) > 1 else 1.0
    var_sr = max(var_sr, 1e-6)
    dsr = None
    if n_days >= 3:
        d = res.daily_returns.to_numpy()
        sk, ku = skew_kurt(d)
        sr_daily = sharpe(d, 1.0)
        dsr = float(deflated_sharpe(sr_daily, n_trials, var_sr / TRADING_DAYS, n_days, sk, ku))
        checks["dsr"] = (
            dsr >= thresholds.min_dsr,
            f"{dsr:.3f} >= {thresholds.min_dsr} (N={n_trials})",
        )
    mbtl = min_backtest_length_years(n_trials, target_max_sr=1.0)
    years = n_days / TRADING_DAYS
    checks["min_backtest_length"] = (years >= mbtl, f"{years:.2f}y >= {mbtl:.2f}y for N={n_trials}")

    pbo = None
    if daily_matrix is not None and daily_matrix.shape[1] >= 2 and daily_matrix.shape[0] >= 32:
        s = 16 if daily_matrix.shape[0] >= 16 * 8 else 8
        pr = pbo_cscv(daily_matrix.to_numpy(), s=s, rng=rng)
        pbo = float(pr.pbo)
        checks["pbo"] = (pbo <= thresholds.max_pbo, f"{pbo:.3f} <= {thresholds.max_pbo}")

    hc = None
    if n_days >= 21:
        months = max(int(n_days / 21), 1)
        hr = haircut_sharpe(sr, months, n_trials, method="bonferroni")
        hc = float(hr.sr_haircut)
        checks["haircut_sr"] = (
            hc >= thresholds.min_haircut_sr,
            f"{hc:.2f} >= {thresholds.min_haircut_sr}",
        )

    perm_p = None
    if n_trades >= 5 and "mid_close" in df:
        bar_ret = np.diff(df["mid_close"].to_numpy(dtype=float)) * 100.0  # USD per lot per bar
        tr = res.trades
        holding = np.maximum(tr.bars_held.to_numpy(dtype=int), 1)
        sides = np.where(tr.side == "long", 1, -1)
        pnl_per_lot = (tr.net_pnl / tr.volume).to_numpy(dtype=float)
        pres = permutation_test_random_timing(
            pnl_per_lot, bar_ret, holding, n_perm=500, rng=rng, sides=sides
        )
        perm_p = float(pres.p_value)
        checks["perm_p"] = (
            perm_p <= thresholds.max_perm_p,
            f"{perm_p:.3f} <= {thresholds.max_perm_p}",
        )

    mc95 = None
    if n_trades >= 5:
        mc = monte_carlo_drawdown(res.trades.net_pnl.to_numpy(), n_perm=2000, rng=rng)
        mc95 = float(mc.dd_p95)

    passed = all(ok for ok, _ in checks.values())
    return GateReport(
        passed=passed,
        checks=checks,
        metrics=metrics,
        pbo=pbo,
        dsr=dsr,
        haircut_sr=hc,
        perm_p=perm_p,
        mc_dd_p95=mc95,
        n_trials_counted=n_trials,
    )


def render_report(report: GateReport, title: str) -> str:
    lines = [
        f"# Offline gate — {title}",
        "",
        f"Generated {datetime.now(UTC).isoformat(timespec='seconds')}",
        "",
        f"**Result: {'PASS' if report.passed else 'FAIL'}**",
        "",
        "| Check | OK | Detail |",
        "| --- | --- | --- |",
    ]
    for k, (ok, note) in report.checks.items():
        lines.append(f"| {k} | {'yes' if ok else 'no'} | {note} |")
    m = report.metrics
    lines += [
        "",
        "## Metrics (net of spread, slippage, commission; swap = 0 intraday)",
        "",
        "| Metric | Value |",
        "| --- | --- |",
    ]
    for k in (
        "n_trades",
        "n_days",
        "net_return_pct",
        "sharpe",
        "sortino",
        "calmar",
        "max_drawdown_pct",
        "worst_day_pct",
        "cvar99_pct",
        "hit_rate",
        "profit_factor",
        "expectancy_usd",
        "expectancy_r",
        "cost_share_of_gross",
        "avg_risk_pct",
        "max_risk_pct",
        "top5_trades_share",
    ):
        v = m.get(k)
        if isinstance(v, float):
            v = f"{v:.4g}"
        lines.append(f"| {k} | {v} |")
    if m.get("by_regime"):
        lines += [
            "",
            "## By regime",
            "",
            "| Regime | n | net | mean |",
            "| --- | --- | --- | --- |",
        ]
        for reg, row in m["by_regime"].items():
            lines.append(f"| {reg} | {row['n']} | {row['net']:.2f} | {row['mean']:.2f} |")
    if m.get("rejected"):
        lines += ["", f"Rejected entries: {m['rejected']}"]
    if report.mc_dd_p95 is not None:
        lines += [
            "",
            f"Monte Carlo drawdown p95 (trade-order permutation): {report.mc_dd_p95:.2f} USD",
        ]
    return "\n".join(lines) + "\n"

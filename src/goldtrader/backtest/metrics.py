"""Performance metrics after all costs (RESEARCH §1.8, PRD R-054). Pure functions on BacktestResult."""
from __future__ import annotations

import numpy as np
import pandas as pd

from goldtrader.backtest.engine import BacktestResult
from goldtrader.backtest.stats import max_drawdown, sharpe, skew_kurt

TRADING_DAYS = 252


def compute_metrics(res: BacktestResult, equity0: float | None = None) -> dict:
    eq = res.equity
    e0 = float(equity0 if equity0 is not None else (eq.iloc[0] if len(eq) else 0.0))
    tr = res.trades
    d = res.daily_returns
    out: dict = {"n_trades": len(tr), "n_days": len(d), "equity0": e0}
    if len(eq) == 0:
        return out
    final = float(eq.iloc[-1])
    out["net_pnl"] = final - e0
    out["net_return_pct"] = (final / e0 - 1.0) * 100.0 if e0 else float("nan")
    out["max_drawdown_pct"] = float(max_drawdown(eq.to_numpy(), cumulative=True) / max(eq.cummax().max(), 1e-9) * 100.0)
    if len(d) >= 2:
        out["sharpe"] = sharpe(d.to_numpy(), TRADING_DAYS)
        downside = d[d < 0].to_numpy()
        dd_std = float(np.sqrt(np.mean(downside ** 2))) if len(downside) else 0.0
        out["sortino"] = float(d.mean() / dd_std * np.sqrt(TRADING_DAYS)) if dd_std > 0 else 0.0
        years = len(d) / TRADING_DAYS
        cagr = (final / e0) ** (1 / years) - 1.0 if years > 0 and e0 > 0 and final > 0 else float("nan")
        out["cagr_pct"] = cagr * 100.0 if np.isfinite(cagr) else float("nan")
        out["calmar"] = (cagr * 100.0 / out["max_drawdown_pct"]) if out["max_drawdown_pct"] > 0 and np.isfinite(cagr) else float("nan")
        out["worst_day_pct"] = float(d.min() * 100.0)
        q = np.quantile(d.to_numpy(), 0.01)
        tail = d[d <= q].to_numpy()
        out["cvar99_pct"] = float(tail.mean() * 100.0) if len(tail) else float(q * 100.0)
        if len(d) >= 3:
            out["skew"], out["kurt"] = skew_kurt(d.to_numpy())
        else:
            out["skew"], out["kurt"] = 0.0, 3.0
    else:
        out.update(sharpe=0.0, sortino=0.0, cagr_pct=float("nan"), calmar=float("nan"),
                   worst_day_pct=0.0, cvar99_pct=0.0, skew=0.0, kurt=0.0)
    if len(tr):
        wins = tr[tr.net_pnl > 0]
        losses = tr[tr.net_pnl <= 0]
        gp, gl = float(wins.net_pnl.sum()), float(-losses.net_pnl.sum())
        out["hit_rate"] = len(wins) / len(tr)
        out["profit_factor"] = gp / gl if gl > 0 else float("inf")
        out["expectancy_usd"] = float(tr.net_pnl.mean())
        out["expectancy_r"] = float(tr.r_multiple.mean())
        out["avg_win_usd"] = float(wins.net_pnl.mean()) if len(wins) else 0.0
        out["avg_loss_usd"] = float(losses.net_pnl.mean()) if len(losses) else 0.0
        costs = float((tr.spread_cost + tr.slippage_cost + tr.commission + tr.swap).sum())
        gross = float(tr.gross_pnl.abs().sum())
        out["total_costs_usd"] = costs
        out["cost_share_of_gross"] = costs / gross if gross > 0 else float("nan")
        out["avg_risk_pct"] = float(tr.risk_pct.mean())
        out["max_risk_pct"] = float(tr.risk_pct.max())
        out["avg_bars_held"] = float(tr.bars_held.mean())
        out["lots_traded"] = float(tr.volume.sum() * 2)
        out["exit_reasons"] = tr.exit_reason.value_counts().to_dict()
        out["by_regime"] = regime_table(tr)
        worst5 = tr.net_pnl.nlargest(5).sum()
        out["top5_trades_share"] = float(worst5 / tr.net_pnl.sum()) if tr.net_pnl.sum() > 0 else float("nan")
    out["rejected"] = dict(res.rejected)
    out["killed"] = res.killed_at is not None
    return out


def regime_table(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {}
    g = trades.groupby("regime")["net_pnl"]
    return {k: {"n": int(v.count()), "net": float(v.sum()), "mean": float(v.mean())}
            for k, v in g}


def trades_per_year(res: BacktestResult) -> float:
    if res.n_trades == 0 or len(res.daily_returns) == 0:
        return 0.0
    return res.n_trades / (len(res.daily_returns) / TRADING_DAYS)

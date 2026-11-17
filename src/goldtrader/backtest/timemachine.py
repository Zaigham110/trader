"""Time machine (D-058): replay the stored 1-minute history through the real strategy, session
rules, risk sizing, loss limits and costs, and return everything the dashboard needs to show it.

What is replayed is code only. The LLM agents are not: a model asked about a past date remembers
what happened next, so its "decision" would be dishonest. Their value is measured going forward.

`timing` answers one question the plain backtester cannot: `every_bar` acts on every closed
1-minute bar (what `run_backtest` assumes); `live_5min` keeps a signal only on the bars a 5-minute
decision cycle would have looked at, which is how the brain behaved before D-058.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from goldtrader.backtest.costs import CostModel
from goldtrader.backtest.engine import run_backtest
from goldtrader.backtest.gate import GateThresholds, offline_gate
from goldtrader.backtest.metrics import compute_metrics
from goldtrader.backtest.strategies import SignalFrame, Strategy, build_strategy
from goldtrader.config.schema import Config
from goldtrader.data.features import add_features
from goldtrader.data.resample import bars_to_frame
from goldtrader.db.repo import Repo
from goldtrader.types import SymbolSpec

DAYS_CHOICES = (7, 14, 30, 60, 0)  # 0 = everything stored
TIMINGS = ("every_bar", "live_5min")
SPREAD_CHOICES = (1.0, 1.5, 2.0)
MAX_POINTS = 600
CYCLE_BARS = 5


@dataclass(frozen=True)
class LiveSampled:
    """A strategy seen only at the end of every 5th minute, like a 5-minute decision cycle."""

    inner: Strategy

    @property
    def id(self) -> str:
        return self.inner.id

    @property
    def kind(self) -> str:
        return self.inner.kind

    def signals(self, df: pd.DataFrame) -> SignalFrame:
        sig = self.inner.signals(df)
        minute = df["time"].to_numpy(dtype=np.int64) // 60
        seen = (minute % CYCLE_BARS) == CYCLE_BARS - 1
        return SignalFrame(
            np.where(seen, sig.side, 0).astype(np.int8), sig.stop_atr, sig.target_atr
        )


def _clean(v: Any) -> Any:
    """JSON-ready: numpy scalars to Python, NaN/inf to None, recursively."""
    if isinstance(v, dict):
        return {str(k): _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    if isinstance(v, (np.bool_, bool)):
        return bool(v)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        f = float(v)
        return f if math.isfinite(f) else None
    return v


def _thin(n: int, limit: int = MAX_POINTS) -> np.ndarray:
    if n <= limit:
        return np.arange(n)
    idx = np.unique(np.linspace(0, n - 1, limit).astype(int))
    return idx


def _candles(df: pd.DataFrame) -> tuple[int, list[list[float]]]:
    """Bid candles for the period, aggregated so that at most ~MAX_POINTS are returned."""
    n = len(df)
    span = 60
    for minutes in (1, 5, 15, 60, 240, 1440):
        span = minutes * 60
        if n / minutes <= MAX_POINTS:
            break
    t = df["time"].to_numpy(dtype=np.int64)
    o, h = df["bid_open"].to_numpy(dtype=float), df["bid_high"].to_numpy(dtype=float)
    lo, c = df["bid_low"].to_numpy(dtype=float), df["bid_close"].to_numpy(dtype=float)
    out: list[list[float]] = []
    for i in range(n):
        key = int(t[i] - t[i] % span)
        if out and out[-1][0] == key:
            row = out[-1]
            row[2] = max(row[2], h[i])
            row[3] = min(row[3], lo[i])
            row[4] = c[i]
        else:
            out.append([key, o[i], h[i], lo[i], c[i]])
    return span // 60, out


def _events(conn: sqlite3.Connection, start_s: int, end_s: int) -> list:
    try:
        from goldtrader.feeds.calendar_store import CalendarStore

        return CalendarStore(conn).between(start_s * 1000, end_s * 1000)
    except Exception:  # noqa: BLE001 - no calendar stored is not a reason to fail the replay
        return []


def load_frame(conn: sqlite3.Connection, cfg: Config) -> pd.DataFrame:
    """All stored 1-minute bars with features (the same path the live brain uses)."""
    bars = Repo(conn).load_bars(cfg.account.symbol, 60)
    if len(bars) < 500:
        raise ValueError(f"only {len(bars)} one-minute bars stored; run `goldctl backfill` first")
    return add_features(bars_to_frame(bars, 60))


def run(
    conn: sqlite3.Connection,
    cfg: Config,
    spec: SymbolSpec,
    *,
    days: int = 60,
    strategy_id: str | None = None,
    timing: str = "every_bar",
    spread_mult: float = 1.0,
    equity: float = 5000.0,
    candidate: tuple[str, str, dict[str, float]] | None = None,
    frame: pd.DataFrame | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """Replay the last `days` of stored bars (0 = all). Raises ValueError for bad parameters or
    when there is not enough history."""
    if timing not in TIMINGS:
        raise ValueError(f"timing must be one of {TIMINGS}")
    if days < 0:
        raise ValueError("days must be >= 0")
    if candidate is not None:  # a research candidate that is not in the config (D-059)
        kind, sid, params = candidate
    else:
        chosen = [s for s in cfg.strategies if strategy_id is None or s.id == strategy_id]
        if strategy_id is None:
            chosen = [s for s in chosen if s.enabled] or chosen
        if not chosen:
            raise ValueError(f"no strategy {strategy_id!r} in the config")
        kind, sid, params = chosen[0].kind, chosen[0].id, dict(chosen[0].params)
    repo = Repo(conn)
    # features are causal, so slicing after computing them is safe
    full = frame if frame is not None else load_frame(conn, cfg)
    df = full
    if days:
        cut = int(full["time"].iloc[-1]) - days * 86400
        df = full[full["time"] >= cut]
    if len(df) < 200:
        raise ValueError("not enough bars in the chosen period")
    start_s, end_s = int(df["time"].iloc[0]), int(df["time"].iloc[-1])

    strat: Strategy = build_strategy(kind, sid, params)
    if timing == "live_5min":
        strat = LiveSampled(strat)
    cost = CostModel(
        spread_multiplier=spread_mult,
        commission_per_lot_per_side_usd=cfg.account.commission_per_lot_per_side_usd,
    )
    events = _events(conn, start_s, end_s)
    res = run_backtest(df, strat, cfg, spec, cost=cost, events=events, equity0=equity)
    m = compute_metrics(res, equity)
    try:
        ledger = repo.trial_sharpes()
    except Exception:  # noqa: BLE001
        ledger = []
    gate = offline_gate(
        res,
        m,
        df,
        thresholds=GateThresholds(spread_multiplier=spread_mult),
        ledger_sharpes=ledger,
        rng=np.random.default_rng(0),
    )

    eq = res.equity
    pick = _thin(len(eq))
    eq_t = (eq.index.asi8 // 1_000_000_000) if len(eq) else np.array([], dtype=np.int64)
    equity_curve = [[int(eq_t[i]), float(eq.iloc[i])] for i in pick]
    tr = res.trades
    trades = [
        {
            "entry_s": int(pd.Timestamp(r.entry_time).timestamp())
            if not isinstance(r.entry_time, (int, np.integer))
            else int(r.entry_time),
            "exit_s": int(pd.Timestamp(r.exit_time).timestamp())
            if not isinstance(r.exit_time, (int, np.integer))
            else int(r.exit_time),
            "side": r.side,
            "volume": float(r.volume),
            "entry_price": float(r.entry_price),
            "exit_price": float(r.exit_price),
            "net_pnl": float(r.net_pnl),
            "r_multiple": float(r.r_multiple),
            "exit_reason": r.exit_reason,
            "costs": float(r.spread_cost + r.slippage_cost + r.commission + r.swap),
        }
        for r in tr.itertuples(index=False)
    ]
    first_mid = (float(df["bid_open"].iloc[0]) + float(df["ask_open"].iloc[0])) / 2.0
    last_mid = (float(df["bid_close"].iloc[-1]) + float(df["ask_close"].iloc[-1])) / 2.0
    minutes, candles = _candles(df)
    signals = int(np.count_nonzero(strat.signals(df).side))
    return _clean(
        {
            "params": {
                "days": days,
                "strategy_id": sid,
                "strategy_kind": kind,
                "strategy_params": params,
                "label": label,
                "timing": timing,
                "spread_mult": spread_mult,
                "equity": equity,
            },
            "range": {"start_s": start_s, "end_s": end_s, "bars": len(df)},
            "metrics": {k: v for k, v in m.items() if k != "by_regime"},
            "by_regime": m.get("by_regime", {}),
            "gate": {
                "passed": gate.passed,
                "checks": {k: {"ok": ok, "note": note} for k, (ok, note) in gate.checks.items()},
            },
            "signals_seen": signals,
            "events_applied": len(events),
            "buy_and_hold_pct": (last_mid / first_mid - 1.0) * 100.0 if first_mid else None,
            "equity": equity_curve,
            "trades": trades,
            "candle_minutes": minutes,
            "candles": candles,
        }
    )


# ------------------------------------------------------------------------------------- storage
def start_run(conn: sqlite3.Connection, params: dict[str, Any], now_ms: int) -> int:
    cur = conn.execute(
        "INSERT INTO timemachine_runs(created_ms, status, params_json) VALUES (?, 'running', ?)",
        (now_ms, json.dumps(params, sort_keys=True)),
    )
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    now_ms: int,
    result: dict[str, Any] | None,
    error: str | None = None,
) -> None:
    conn.execute(
        "UPDATE timemachine_runs SET status=?, finished_ms=?, result_json=?, error=? WHERE id=?",
        (
            "done" if error is None else "error",
            now_ms,
            json.dumps(result) if result is not None else None,
            error,
            run_id,
        ),
    )

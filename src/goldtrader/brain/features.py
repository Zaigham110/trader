"""Brain-side features from the shared database (ARCHITECTURE §3.1 step 1).

The gateway writes 1-minute bars; the brain reads the trailing window, runs the same causal
`add_features` as the backtester, and derives the deterministic strategy signal. Live spread comes
from the gateway state so the brain never touches MT5 (AGENTS §1).
"""

from __future__ import annotations

import time

import pandas as pd

from goldtrader.backtest.strategies import Strategy, build_strategy
from goldtrader.config.schema import StrategyConfig
from goldtrader.contracts import GatewayState
from goldtrader.data.features import add_features
from goldtrader.data.resample import bars_to_frame
from goldtrader.db.repo import Repo
from goldtrader.gateway.market import LiveFeatures


class DbFeatures:
    def __init__(
        self,
        repo: Repo,
        symbol: str,
        strat: StrategyConfig,
        *,
        window_bars: int = 20 * 23 * 60,
        ttl_s: float = 20.0,
        clock=time.time,
    ) -> None:
        self.repo = repo
        self.symbol = symbol
        self.strategy: Strategy = build_strategy(strat.kind, strat.id, strat.params)
        self.window_bars = window_bars
        self.ttl_s = ttl_s
        self.clock = clock
        self._df: pd.DataFrame | None = None
        self._loaded_at = 0.0
        self._last_time: int | None = None

    def _frame(self) -> pd.DataFrame | None:
        if self._df is not None and self.clock() - self._loaded_at < self.ttl_s:
            return self._df
        bars = self.repo.load_bars(self.symbol, 60)
        bars = bars[-self.window_bars :]
        if len(bars) < 3:
            self._df = None
        else:
            self._df = add_features(bars_to_frame(bars, 60))
            self._last_time = int(self._df["time"].iloc[-1])
        self._loaded_at = self.clock()
        return self._df

    def features(self, state: GatewayState | None) -> LiveFeatures | None:
        df = self._frame()
        if df is None or df.empty:
            return None
        row = df.iloc[-1]

        def f(name):
            v = row.get(name)
            return None if v is None or pd.isna(v) else float(v)

        bid, ask = (state.bid, state.ask) if state and state.bid and state.ask else (None, None)
        spread_pips = (ask - bid) / 0.01 if bid is not None else (f("spread_pips") or float("inf"))
        median = f("spread_median_hour")
        if median is None or median <= 0:
            regime = "unknown"
        elif bid is not None:
            regime = "normal" if (ask - bid) <= 2.0 * median else "wide"
        else:
            regime = str(row.get("spread_regime", "unknown"))
        ts = f("trend_state")
        return LiveFeatures(
            time_s=int(row["time"]),
            atr=f("atr"),
            atr_pct=f("atr_pct"),
            spread_pips=spread_pips,
            spread_regime=regime,
            regime=str(row.get("regime", "unknown")),
            trend_state=int(ts) if ts is not None else 0,
            rv=f("rv"),
            rv_pctile=f("rv_pctile"),
            hour_utc=int(row.get("hour_utc", 0)),
        )

    def signal(self) -> int:
        df = self._frame()
        if df is None or df.empty:
            return 0
        sig = self.strategy.signals(df)
        return int(sig.side[-1])

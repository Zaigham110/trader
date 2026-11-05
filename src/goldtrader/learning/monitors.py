"""Production decay monitors and the pause / retire state of a live bundle.

LEARNING_SYSTEM.md §4 (monitors table) and docs/research/04_learning_and_validation.md §3.1 / §3.2:

| monitor          | statistic                                          | level            |
| ---------------- | -------------------------------------------------- | ---------------- |
| ``cusum``        | lower CUSUM on per-trade R, mu0 = backtest          | ``pause``        |
|                  | expectancy, k = k_sigma·σ, h = h_sigma·σ            |                  |
| ``rolling_sharpe`` | rolling SR over a window of trades vs the Lo (2002)| ``pause`` (two   |
|                  | lower band of the backtest SR                       | windows below)   |
| ``mc_drawdown``  | live max DD vs permuted-order DD of the backtest    | ``pause`` > p95, |
|                  | trade P&Ls (fall back to the live list)             | ``retire`` > p99 |
| ``pchart``       | hit rate vs the backtest hit rate, lower 3σ limit   | ``warn``         |
| ``fill_drift``   | mean live slippage ÷ model over the last 50 trades  | ``pause``        |

Monitors run only once the bundle has ``monitor_min_trades`` live trades (else one ``info`` result,
``insufficient_sample``). Every result is appended to ``monitor_events``; the highest level moves
``bundle_state``: ``active → paused → retired``, never automatically back. Un-pausing is an owner
action (:meth:`Monitors.unpause`, Telegram-confirmed upstream) that is itself logged and acts as the
"explicit rule reset" of the research doc: later runs evaluate only trades closed after it. A
second pause within ``monitor_repause_days`` (90) of a previous one retires the bundle.

The gates object is duck-typed: the owner-only ``LearningGates`` (``owner.learning``, D-045) is used
when present; :class:`MonitorGates` carries the §4 defaults for callers and tests that do not have
it. ``risk`` (the owner's :class:`~goldtrader.config.schema.RiskLimits`) is accepted for parity with
the other learning modules and is never written (R-034): monitors decide *when* to pause, never what
to change (LEARNING_SYSTEM §7).
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from goldtrader.backtest.stats import (
    cusum,
    hit_rate_pchart,
    max_drawdown,
    monte_carlo_drawdown,
    rolling_sharpe_bands,
)
from goldtrader.config.schema import RiskLimits

LEVELS: tuple[str, ...] = ("info", "warn", "pause", "retire")
_RANK = {lvl: i for i, lvl in enumerate(LEVELS)}
STATE_MONITOR = "bundle_state"
"""``monitor_events.monitor`` value used for state transitions (pause / retire / un-pause)."""


@dataclass(frozen=True)
class MonitorGates:
    """LEARNING_SYSTEM §4 / research §3.2 defaults; the field names are those of
    ``LearningGates.monitor_*`` (``owner.learning``), so either object can be passed as ``gates``."""

    monitor_min_trades: int = 50
    monitor_cusum_k_sigma: float = 0.5
    monitor_cusum_h_sigma: float = 4.0
    monitor_sharpe_window_trades: int = 100
    monitor_mc_pause_pctile: float = 95.0
    monitor_mc_retire_pctile: float = 99.0
    monitor_pchart_sigma: float = 3.0
    monitor_fill_drift_ratio: float = 1.5
    monitor_fill_drift_trades: int = 50
    monitor_repause_days: int = 90


_DEFAULTS = MonitorGates()


@dataclass(frozen=True)
class MonitorResult:
    monitor: str  # cusum | rolling_sharpe | mc_drawdown | pchart | fill_drift | sample
    level: str  # info | warn | pause | retire
    statistic: float | None
    threshold: float | None
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "monitor": self.monitor,
            "level": self.level,
            "statistic": self.statistic,
            "threshold": self.threshold,
            "detail": self.detail,
        }


def highest_level(results: Sequence[MonitorResult]) -> str:
    """The most severe level among ``results`` (``info`` when empty)."""
    if not results:
        return "info"
    return max((r.level for r in results), key=lambda lvl: _RANK[lvl])


def _finite(x: float) -> float | None:
    return float(x) if x is not None and math.isfinite(x) else None


class Monitors:
    """Run the decay monitors on a bundle's live trades and keep its pause / retire state."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        gates: Any,
        risk: RiskLimits,
        clock_ms: Callable[[], int],
        *,
        n_perm: int = 2000,
        seed: int = 20261105,
    ) -> None:
        self.c = conn
        self.gates = gates if gates is not None else MonitorGates()
        self.risk = risk
        self.clock_ms = clock_ms
        self.n_perm = n_perm
        self.seed = seed

    def _g(self, name: str) -> Any:
        """Gate value by name: the configured gates object, else the §4 default."""
        return getattr(self.gates, name, getattr(_DEFAULTS, name))

    # --- state -----------------------------------------------------------------------------
    def status(self, bundle_id: str) -> str:
        """``active`` | ``paused`` | ``retired`` (a bundle without a row is active)."""
        row = self.c.execute(
            "SELECT status FROM bundle_state WHERE bundle_id=?", (bundle_id,)
        ).fetchone()
        return str(row["status"]) if row is not None else "active"

    def events(self, bundle_id: str, since_ms: int = 0) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.c.execute(
                "SELECT * FROM monitor_events WHERE bundle_id=? AND at_ms>=? ORDER BY at_ms, id",
                (bundle_id, int(since_ms)),
            )
        ]

    def unpause(self, bundle_id: str, actor: str = "owner", reason: str | None = None) -> bool:
        """Owner action: ``paused → active`` with an audit event. Never called by code paths that
        react to monitors (LEARNING_SYSTEM §4: "un-pausing is never automatic").

        Returns False when the bundle is already active; raises for a retired bundle (it needs a
        fresh promotion cycle) or an actor other than the owner.
        """
        if actor != "owner":
            raise ValueError("only the owner may un-pause a bundle")
        status = self.status(bundle_id)
        if status == "retired":
            raise ValueError(f"bundle {bundle_id} is retired; a fresh cycle is required")
        if status != "paused":
            return False
        now = self.clock_ms()
        self._set_state(bundle_id, "active", now, f"un-paused by {actor}: {reason or ''}".strip())
        self._event(
            bundle_id,
            now,
            MonitorResult(
                STATE_MONITOR,
                "info",
                None,
                None,
                f"paused->active by {actor}: {reason or 'owner review'}",
            ),
        )
        return True

    # --- run -------------------------------------------------------------------------------
    def run(
        self,
        bundle_id: str,
        *,
        backtest_expectancy_r: float | None,
        backtest_sharpe: float | None,
        backtest_trade_pnls: Sequence[float] | None,
        expected_slippage_usd: float | None,
        backtest_trade_rs: Sequence[float] | None = None,
    ) -> list[MonitorResult]:
        """Evaluate every monitor on the bundle's live ``trades`` (ordered by ``exit_ms``), journal
        the results and apply the highest level to ``bundle_state``.

        ``backtest_expectancy_r`` is the CUSUM reference mean (0 when unknown); the CUSUM σ comes
        from ``backtest_trade_rs`` when given, else from the live R sample. ``backtest_sharpe`` is
        the per-trade Sharpe of the offline R distribution; ``backtest_trade_pnls`` feeds the
        Monte Carlo drawdown null and the p-chart's reference hit rate; ``expected_slippage_usd`` is
        the cost model's round-trip slippage per trade at the traded volume.
        """
        now = self.clock_ms()
        since = self._last_reset_ms(bundle_id)
        trades = self._trades(bundle_id, since)
        n = len(trades)
        min_n = int(self._g("monitor_min_trades"))
        if n < min_n:
            results = [
                MonitorResult(
                    "sample",
                    "info",
                    float(n),
                    float(min_n),
                    f"insufficient_sample: n={n} < {min_n}",
                )
            ]
        else:
            rs = np.array([t["r_multiple"] for t in trades if t["r_multiple"] is not None], float)
            pnls = np.array([t["net_pnl"] for t in trades], float)
            slips = np.array([t["slippage_cost"] for t in trades], float)
            results = [
                self._cusum(rs, backtest_expectancy_r, backtest_trade_rs),
                self._rolling_sharpe(rs, backtest_sharpe),
                self._mc_drawdown(pnls, backtest_trade_pnls),
                self._pchart(pnls, backtest_trade_pnls),
                self._fill_drift(slips, expected_slippage_usd),
            ]
        for res in results:
            self._event(bundle_id, now, res)
        self._apply(bundle_id, results, now)
        return results

    # --- the five monitors -----------------------------------------------------------------
    def _cusum(
        self, rs: np.ndarray, mu0: float | None, backtest_rs: Sequence[float] | None
    ) -> MonitorResult:
        if rs.size < 2:
            return MonitorResult("cusum", "info", None, None, "no_r_multiples")
        ref = np.asarray(backtest_rs, float) if backtest_rs is not None else None
        if ref is not None and ref.size >= 2:
            sigma, src = float(ref.std(ddof=1)), "backtest"
        else:
            sigma, src = float(rs.std(ddof=1)), "live"
        if not math.isfinite(sigma) or sigma <= 0:
            return MonitorResult("cusum", "info", None, None, f"sigma_zero ({src})")
        mu = float(mu0) if mu0 is not None else 0.0
        k = float(self._g("monitor_cusum_k_sigma")) * sigma
        h = float(self._g("monitor_cusum_h_sigma")) * sigma
        res = cusum(rs, mu, k, h)
        stat = float(np.max(res.statistic_path))
        level = "pause" if res.breached else "info"
        detail = (
            f"mu0={mu:.4f} sigma={sigma:.4f} ({src}) k={k:.4f} h={h:.4f} n={rs.size} "
            f"breached_at={res.breached_at}"
        )
        return MonitorResult("cusum", level, stat, h, detail)

    def _rolling_sharpe(self, rs: np.ndarray, sr_ref: float | None) -> MonitorResult:
        window = int(self._g("monitor_sharpe_window_trades"))
        if sr_ref is None or not math.isfinite(sr_ref):
            return MonitorResult("rolling_sharpe", "info", None, None, "no_reference_sharpe")
        if rs.size < window or window < 2:
            return MonitorResult(
                "rolling_sharpe", "info", None, None, f"window_not_full: n={rs.size} < {window}"
            )
        res = rolling_sharpe_bands(rs, window, float(sr_ref))
        valid = res.rolling_sr[~np.isnan(res.rolling_sr)]
        last = float(valid[-1]) if valid.size else None
        level = "pause" if res.pause else "info"
        detail = (
            f"window={window} sr_ref={sr_ref:.4f} lower={res.lower:.4f} "
            f"trailing_below={res.trailing_below} need={2 * window}"
        )
        return MonitorResult("rolling_sharpe", level, last, float(res.lower), detail)

    def _mc_drawdown(
        self, pnls: np.ndarray, backtest_pnls: Sequence[float] | None
    ) -> MonitorResult:
        if pnls.size < 2:
            return MonitorResult("mc_drawdown", "info", None, None, "too_few_trades")
        ref = np.asarray(backtest_pnls, float) if backtest_pnls is not None else None
        if ref is None or ref.size < 2:
            ref, src = pnls, "live"
        else:
            src = "backtest"
        live_dd = max_drawdown(pnls)
        mc = monte_carlo_drawdown(ref, n_perm=self.n_perm, rng=np.random.default_rng(self.seed))
        p_pause = float(np.percentile(mc.null_dist, float(self._g("monitor_mc_pause_pctile"))))
        p_retire = float(np.percentile(mc.null_dist, float(self._g("monitor_mc_retire_pctile"))))
        if live_dd > p_retire:
            level, thr = "retire", p_retire
        elif live_dd > p_pause:
            level, thr = "pause", p_pause
        else:
            level, thr = "info", p_pause
        detail = (
            f"live_dd={live_dd:.4f} p_pause={p_pause:.4f} p_retire={p_retire:.4f} "
            f"null={src} n_perm={self.n_perm}"
        )
        return MonitorResult("mc_drawdown", level, float(live_dd), thr, detail)

    def _pchart(self, pnls: np.ndarray, backtest_pnls: Sequence[float] | None) -> MonitorResult:
        if backtest_pnls is None or len(backtest_pnls) == 0:
            return MonitorResult("pchart", "info", None, None, "no_reference_hit_rate")
        ref = np.asarray(backtest_pnls, float)
        p0 = float(np.mean(ref > 0))
        if not 0.0 < p0 < 1.0:
            return MonitorResult(
                "pchart", "info", None, None, f"reference_hit_rate_degenerate {p0}"
            )
        n = int(pnls.size)
        wins = int(np.sum(pnls > 0))
        res = hit_rate_pchart(
            wins,
            n,
            p0,
            sigma=float(self._g("monitor_pchart_sigma")),
            min_n=int(self._g("monitor_min_trades")),
        )
        level = "warn" if res.breached else "info"
        detail = f"p_hat={res.p_hat:.4f} p0={p0:.4f} lcl={res.lcl:.4f} n={n}"
        return MonitorResult("pchart", level, float(res.p_hat), float(res.lcl), detail)

    def _fill_drift(self, slips: np.ndarray, expected_usd: float | None) -> MonitorResult:
        if expected_usd is None or not math.isfinite(expected_usd) or expected_usd <= 0:
            return MonitorResult("fill_drift", "info", None, None, "no_expected_slippage")
        k = int(self._g("monitor_fill_drift_trades"))
        window = slips[-k:] if k > 0 else slips
        if window.size == 0:
            return MonitorResult("fill_drift", "info", None, None, "no_trades")
        ratio = float(np.mean(window)) / float(expected_usd)
        limit = float(self._g("monitor_fill_drift_ratio"))
        level = "pause" if ratio > limit else "info"
        detail = f"mean_slippage={np.mean(window):.4f} expected={expected_usd:.4f} n={window.size}"
        return MonitorResult("fill_drift", level, _finite(ratio), limit, detail)

    # --- persistence -----------------------------------------------------------------------
    def _trades(self, bundle_id: str, since_ms: int) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.c.execute(
                "SELECT trade_id, exit_ms, net_pnl, r_multiple, slippage_cost, volume FROM trades "
                "WHERE bundle_id=? AND exit_ms>? ORDER BY exit_ms, trade_id",
                (bundle_id, int(since_ms)),
            )
        ]

    def _last_reset_ms(self, bundle_id: str) -> int:
        """Time of the last owner un-pause (the explicit rule reset); 0 when never un-paused."""
        row = self.c.execute(
            "SELECT MAX(at_ms) FROM monitor_events WHERE bundle_id=? AND monitor=? AND level='info' "
            "AND detail LIKE 'paused->active%'",
            (bundle_id, STATE_MONITOR),
        ).fetchone()
        return int(row[0]) if row is not None and row[0] is not None else 0

    def _event(self, bundle_id: str, at_ms: int, res: MonitorResult) -> None:
        self.c.execute(
            "INSERT INTO monitor_events(at_ms, bundle_id, monitor, level, statistic, threshold, "
            "detail) VALUES (?,?,?,?,?,?,?)",
            (at_ms, bundle_id, res.monitor, res.level, res.statistic, res.threshold, res.detail),
        )

    def _set_state(self, bundle_id: str, status: str, at_ms: int, reason: str) -> None:
        self.c.execute(
            "INSERT INTO bundle_state(bundle_id, status, since_ms, reason) VALUES (?,?,?,?) "
            "ON CONFLICT(bundle_id) DO UPDATE SET status=excluded.status, "
            "since_ms=excluded.since_ms, reason=excluded.reason",
            (bundle_id, status, at_ms, reason),
        )

    def _recent_pauses(self, bundle_id: str, since_ms: int, before_ms: int) -> int:
        row = self.c.execute(
            "SELECT COUNT(*) FROM monitor_events WHERE bundle_id=? AND monitor=? AND level='pause' "
            "AND at_ms>=? AND at_ms<?",
            (bundle_id, STATE_MONITOR, int(since_ms), int(before_ms)),
        ).fetchone()
        return int(row[0])

    def _apply(self, bundle_id: str, results: Sequence[MonitorResult], now: int) -> None:
        """active → paused → retired; never back. A second pause within the re-pause window
        retires (research §3.2)."""
        level = highest_level(results)
        if level not in ("pause", "retire"):
            return
        status = self.status(bundle_id)
        if status == "retired":
            return
        culprits = ",".join(sorted({r.monitor for r in results if r.level == level}))
        if level == "pause":
            if status == "paused":
                return  # already paused: no new entries are being taken, nothing to escalate
            window_ms = int(self._g("monitor_repause_days")) * 86_400_000
            if self._recent_pauses(bundle_id, now - window_ms, now) > 0:
                level = "retire"
                culprits = (
                    f"{culprits} (second pause within {self._g('monitor_repause_days')} days)"
                )
        new_status = "retired" if level == "retire" else "paused"
        self._set_state(bundle_id, new_status, now, culprits)
        self._event(
            bundle_id,
            now,
            MonitorResult(STATE_MONITOR, level, None, None, f"{status}->{new_status}: {culprits}"),
        )

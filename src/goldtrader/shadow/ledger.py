"""Shadow ledger: one candidate bundle simulated on live 1-minute bars, orders logged only.

LEARNING_SYSTEM.md §3.3 (shadow gate: ≥ 30 trading days and ≥ 50 trades on live quotes with orders
*logged only*; measured slippage ≤ 1.25 × cost model; signal-time to order-time drift < 1 bar) and
D-047 ("shadow-stage candidates are simulated on live bars with the backtester's fill model and
never sent to the gateway"). This module therefore imports nothing from ``goldtrader.mt5``,
``goldtrader.execution`` or ``goldtrader.gateway.state`` and has no code path that builds an order.

The fill model is the backtester's (:mod:`goldtrader.backtest.engine`), bar by bar, so a shadow
P&L is directly comparable to both the offline backtest and the champion's live fills (TEST_PLAN §2):

* the deterministic signal of bar *i* (``build_strategy(...).signals(frame)`` on the trailing frame,
  last row = bar *i*) is acted on at bar *i+1*'s open: long at ask, short at bid, plus the cost
  model's slippage; the bar's own bid/ask are used, never a fixed spread;
* entry filters are the live path's (``brain/gates.py`` and the risk engine): session ``OPEN`` with
  entries allowed, spread regime ``normal`` and spread within ``max_spread_pips`` (the frame's own
  regime at the fill bar, exactly as the backtester, *and* the live ``LiveFeatures`` regime when the
  brain passes one), one position per bundle, ATR known, and the backtester's consecutive-loss
  cooldown; stop / target ATR multiples are clamped to the owner's risk limits like the engine;
* SL/TP are checked intrabar on the bid (long) / ask (short) extremes, stop first when both are hit,
  stops gap-fill at the worse of level and open; any open position is flattened at the first bar in
  ``PRE_BREAK_BUFFER`` / ``CLOSE_ONLY`` / ``CLOSED`` / ``STALE`` (intraday only, D-020);
* volume is ``spec.volume_min`` (the 0.01-lot reality of the demo and micro-live profiles).

Rows go to ``shadow_trades`` (migration 008): ``open`` on entry, ``closed`` with ``net_pnl``,
``r_multiple``, ``exit_reason``, ``spread_cost`` and ``slippage_cost`` on exit. ``shadow_id`` is
``<bundle_id>:<fill bar time>`` so a replayed bar is idempotent; a bar at or before the last one
seen by a bundle is ignored.

Features are recomputed on the trailing frame after every closed bar (the same causal
``add_features`` the live market uses). When the caller already holds that frame — the brain's
``LiveMarket`` does — pass ``frame_provider`` and the ledger uses it instead of keeping its own bars.
Differences from the backtester, accepted and documented: no equity, hence no daily / weekly loss
limit or drawdown kill switch (those belong to the account's risk engine, not to a candidate), and
no end-of-data close (a shadow position stays open until the next bar closes it).
"""

from __future__ import annotations

import math
import sqlite3
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from goldtrader.backtest.costs import CostModel
from goldtrader.backtest.strategies import Strategy, build_strategy
from goldtrader.config.schema import Config
from goldtrader.data.features import add_features
from goldtrader.data.resample import bars_to_frame
from goldtrader.types import Bar, SessionState

if TYPE_CHECKING:  # annotation only: the ledger must not import the gateway package (D-047)
    from goldtrader.gateway.market import LiveFeatures

FLATTEN_STATES: frozenset[SessionState] = frozenset(
    {
        SessionState.PRE_BREAK_BUFFER,
        SessionState.CLOSE_ONLY,
        SessionState.CLOSED,
        SessionState.STALE,
    }
)
"""States at which an open shadow position is flattened (engine.py step 2)."""

REQUIRED_COLUMNS: frozenset[str] = frozenset(
    {
        "time",
        "bid_open",
        "bid_high",
        "bid_low",
        "ask_open",
        "ask_high",
        "ask_low",
        "atr",
        "spread_pips",
        "spread_regime",
    }
)

_BAR_FIELDS = (
    "bid_open",
    "bid_high",
    "bid_low",
    "bid_close",
    "ask_open",
    "ask_high",
    "ask_low",
    "ask_close",
)


@dataclass
class ShadowPosition:
    """An open simulated position (mirror of the engine's ``_Open``)."""

    shadow_id: str
    experiment_id: str
    bundle_id: str
    side: str
    volume: float
    signal_ms: int
    entry_ms: int
    entry_price: float
    sl: float
    tp: float
    stop_distance: float
    spread_cost_entry: float
    slip_cost_entry: float
    bars_held: int = 0


@dataclass
class _Bundle:
    experiment_id: str
    bundle_id: str
    kind: str
    params: dict[str, float]
    strategy: Strategy
    pending: int = 0  # signal side from the previous bar, filled at this bar's open
    pending_atr: float = math.nan  # ATR of the signal bar (engine: atr[i-1])
    pending_signal_s: int | None = None
    stop_atr: float = 1.0
    target_atr: float = 1.0
    position: ShadowPosition | None = None
    last_bar_s: int | None = None
    consecutive_losses: int = 0
    cooldown_until_s: int = 0


class ShadowLedger:
    """Simulate registered candidate bundles on closed bars and journal them (D-047).

    Args:
        conn: SQLite connection with migration 008 applied.
        cfg: the running :class:`~goldtrader.config.schema.Config` (owner risk limits are read for
            the stop clamps, the spread cap and the cooldown; nothing is written).
        spec: anything with ``point``, ``digits``, ``trade_contract_size`` and ``volume_min``
            (a :class:`~goldtrader.types.SymbolSpec` in production).
        clock_ms: wall clock in ms (journal timestamps only; bar times drive the simulation).
        cost: fill model; defaults to the account's commission with the real spread and the
            standard slippage allowance (the 1.5 × spread stress is the offline gate's, not the
            shadow stage's).
        frame_provider: optional callable returning the current features frame whose last row is
            the bar just closed; when ``None`` the ledger keeps ``frame_bars`` bars itself.
        frame_bars: length of the ledger's own trailing frame.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        cfg: Config,
        spec: Any,
        clock_ms: Callable[[], int],
        *,
        cost: CostModel | None = None,
        frame_provider: Callable[[], pd.DataFrame | None] | None = None,
        frame_bars: int = 3 * 23 * 60,
    ) -> None:
        self.c = conn
        self.cfg = cfg
        self.spec = spec
        self.clock_ms = clock_ms
        self.cost = cost or CostModel(
            commission_per_lot_per_side_usd=cfg.account.commission_per_lot_per_side_usd
        )
        self.frame_provider = frame_provider
        self._bars: deque[Bar] = deque(maxlen=frame_bars)
        self._bundles: dict[str, _Bundle] = {}

    # --- registration ----------------------------------------------------------------------
    def register(
        self, experiment_id: str, bundle_id: str, kind: str, params: Mapping[str, float]
    ) -> None:
        """Start simulating ``bundle_id`` (``build_strategy(kind, bundle_id, params)``).

        Re-registering an existing bundle is a no-op. An ``open`` ``shadow_trades`` row left by a
        previous process is restored so the position keeps being managed after a restart (its stop
        distance is recovered as ``|entry - sl|``, which differs from the unrounded original by at
        most half a point).
        """
        if bundle_id in self._bundles:
            return
        strategy = build_strategy(kind, bundle_id, dict(params))
        b = _Bundle(experiment_id, bundle_id, kind, dict(params), strategy)
        row = self.c.execute(
            "SELECT * FROM shadow_trades WHERE bundle_id=? AND status='open' "
            "ORDER BY entry_ms DESC LIMIT 1",
            (bundle_id,),
        ).fetchone()
        if row is not None:
            b.position = ShadowPosition(
                shadow_id=row["shadow_id"],
                experiment_id=row["experiment_id"],
                bundle_id=bundle_id,
                side=row["side"],
                volume=float(row["volume"]),
                signal_ms=int(row["signal_ms"]),
                entry_ms=int(row["entry_ms"]),
                entry_price=float(row["entry_price"]),
                sl=float(row["sl"]),
                tp=float(row["tp"]) if row["tp"] is not None else math.nan,
                stop_distance=abs(float(row["entry_price"]) - float(row["sl"])),
                spread_cost_entry=float(row["spread_cost"]),
                slip_cost_entry=float(row["slippage_cost"]),
            )
        last = self.c.execute(
            "SELECT MAX(COALESCE(exit_ms, entry_ms)) FROM shadow_trades WHERE bundle_id=?",
            (bundle_id,),
        ).fetchone()[0]
        if last is not None:
            b.last_bar_s = int(last) // 1000
        self._bundles[bundle_id] = b

    def unregister(self, bundle_id: str) -> None:
        """Stop simulating a bundle (its open row, if any, stays ``open`` for a later restore)."""
        self._bundles.pop(bundle_id, None)

    @property
    def bundles(self) -> list[str]:
        return list(self._bundles)

    def preload(self, bars: Iterable[Bar]) -> None:
        """Warm the ledger's own trailing frame with history (ignored with ``frame_provider``)."""
        for b in bars:
            self._bars.append(b)

    # --- the per-bar step ------------------------------------------------------------------
    def on_bar(
        self,
        bar: Bar | Mapping[str, Any] | sqlite3.Row | pd.Series,
        features: LiveFeatures | None,
        session_state: SessionState | str,
        entries_allowed: bool,
    ) -> list[dict[str, Any]]:
        """Process one closed bar for every registered bundle; returns journal events.

        ``session_state`` / ``entries_allowed`` are the session decision for this bar (the state
        the gateway published for the minute). Order per bundle, exactly as the backtester: fill
        the pending signal at this bar's open, manage the open position on this bar's range, then
        compute this bar's signal for the next bar. Bars at or before a bundle's last bar are
        ignored (idempotent replay).
        """
        b = self._as_bar(bar)
        state = self._state(session_state)
        due = [
            bd for bd in self._bundles.values() if bd.last_bar_s is None or b.time > bd.last_bar_s
        ]
        frame = self._frame_for(b, compute=bool(due))
        if not due or frame is None or frame.empty:
            return []
        missing = REQUIRED_COLUMNS - set(frame.columns)
        if missing:
            raise ValueError(f"features frame missing columns: {sorted(missing)}")
        if int(frame["time"].iloc[-1]) != b.time:
            raise ValueError("frame_provider's last row is not the bar just closed")
        row = frame.iloc[-1]
        events: list[dict[str, Any]] = []
        for bd in due:
            bd.last_bar_s = b.time
            self._step(bd, b, frame, row, features, state, bool(entries_allowed), events)
        return events

    def _step(
        self,
        bd: _Bundle,
        b: Bar,
        frame: pd.DataFrame,
        row: pd.Series,
        features: LiveFeatures | None,
        state: SessionState,
        entries_allowed: bool,
        events: list[dict[str, Any]],
    ) -> None:
        r = self.cfg.owner.risk
        # 1. fill the pending entry at this bar's open
        if bd.pending != 0 and bd.position is None:
            side = "long" if bd.pending > 0 else "short"
            why = self._entry_block(bd, b, row, features, state, entries_allowed)
            if why is None:
                self._open(bd, b, side)
                assert bd.position is not None
                events.append(
                    {
                        "bundle_id": bd.bundle_id,
                        "event": "opened",
                        "side": side,
                        "price": bd.position.entry_price,
                        "bar_s": b.time,
                    }
                )
            else:
                events.append(
                    {"bundle_id": bd.bundle_id, "event": "skipped", "reason": why, "bar_s": b.time}
                )
        bd.pending = 0
        bd.pending_atr = math.nan
        bd.pending_signal_s = None

        # 2. manage the open position on this bar
        pos = bd.position
        if pos is not None:
            pos.bars_held += 1
            slip = self.cost.slippage_points * self.spec.point
            if state in FLATTEN_STATES:
                px = self.cost.exit_price(pos.side, b.bid_open, b.ask_open, self.spec)
                self._close(bd, b, px, "session_flatten", events)
            elif pos.side == "long":
                if b.bid_low <= pos.sl:
                    px = min(pos.sl, b.bid_open) if self.cost.stop_gap_fill else pos.sl
                    self._close(bd, b, px - slip, "stop", events)
                elif b.bid_high >= pos.tp:
                    self._close(bd, b, pos.tp, "target", events)
            else:
                if b.ask_high >= pos.sl:
                    px = max(pos.sl, b.ask_open) if self.cost.stop_gap_fill else pos.sl
                    self._close(bd, b, px + slip, "stop", events)
                elif b.ask_low <= pos.tp:
                    self._close(bd, b, pos.tp, "target", events)

        # 3. this bar's signal, acted on at the next bar's open
        if bd.position is None:
            sig = bd.strategy.signals(frame)
            side_i = int(sig.side[-1]) if len(sig.side) else 0
            bd.stop_atr = min(max(float(sig.stop_atr), r.stop_atr_min), r.stop_atr_max)
            bd.target_atr = max(float(sig.target_atr), bd.stop_atr * r.min_reward_risk)
            if side_i != 0:
                bd.pending = side_i
                bd.pending_atr = float(row["atr"])
                bd.pending_signal_s = b.time
                events.append(
                    {"bundle_id": bd.bundle_id, "event": "signal", "side": side_i, "bar_s": b.time}
                )

    def _entry_block(
        self,
        bd: _Bundle,
        b: Bar,
        row: pd.Series,
        features: LiveFeatures | None,
        state: SessionState,
        entries_allowed: bool,
    ) -> str | None:
        """The live path's entry filters (gates.py / risk engine) in the backtester's order."""
        r = self.cfg.owner.risk
        if state is not SessionState.OPEN or not entries_allowed:
            return f"session_{state.value.lower()}"
        if not np.isfinite(bd.pending_atr) or bd.pending_atr <= 0:
            return "no_atr"
        if float(row["spread_pips"]) > r.max_spread_pips or str(row["spread_regime"]) in (
            "wide",
            "unknown",
        ):
            return "spread"
        if features is not None and (
            features.spread_regime != "normal" or features.spread_pips > r.max_spread_pips
        ):
            return "spread_live"
        if b.time < bd.cooldown_until_s:
            return "cooldown"
        return None

    def _open(self, bd: _Bundle, b: Bar, side: str) -> None:
        spec = self.spec
        a = bd.pending_atr
        stop_distance = bd.stop_atr * a
        entry = self.cost.entry_price(side, b.bid_open, b.ask_open, spec)
        vol = float(spec.volume_min)
        contract = float(spec.trade_contract_size)
        mid = (b.bid_open + b.ask_open) / 2.0
        sl = entry - stop_distance if side == "long" else entry + stop_distance
        tp = entry + bd.target_atr * a if side == "long" else entry - bd.target_atr * a
        signal_s = bd.pending_signal_s if bd.pending_signal_s is not None else b.time - b.seconds
        pos = ShadowPosition(
            shadow_id=f"{bd.bundle_id}:{b.time}",
            experiment_id=bd.experiment_id,
            bundle_id=bd.bundle_id,
            side=side,
            volume=vol,
            signal_ms=int(signal_s) * 1000,
            entry_ms=int(b.time) * 1000,
            entry_price=entry,
            sl=round(sl, spec.digits),
            tp=round(tp, spec.digits),
            stop_distance=stop_distance,
            spread_cost_entry=abs(entry - mid) * vol * contract,
            slip_cost_entry=self.cost.slippage_points * spec.point * vol * contract,
        )
        self.c.execute(
            "INSERT OR IGNORE INTO shadow_trades(shadow_id, experiment_id, bundle_id, side, volume, "
            "signal_ms, entry_ms, entry_price, sl, tp, spread_cost, slippage_cost, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'open')",
            (
                pos.shadow_id,
                pos.experiment_id,
                pos.bundle_id,
                pos.side,
                pos.volume,
                pos.signal_ms,
                pos.entry_ms,
                pos.entry_price,
                pos.sl,
                pos.tp,
                pos.spread_cost_entry,
                pos.slip_cost_entry,
            ),
        )
        bd.position = pos

    def _close(
        self, bd: _Bundle, b: Bar, price: float, reason: str, events: list[dict[str, Any]]
    ) -> None:
        pos = bd.position
        assert pos is not None
        spec = self.spec
        contract = float(spec.trade_contract_size)
        direction = 1.0 if pos.side == "long" else -1.0
        gross = direction * (price - pos.entry_price) * pos.volume * contract
        mid_exit = (b.bid_open + b.ask_open) / 2.0
        spread_exit = abs(price - mid_exit) * pos.volume * contract
        slip_exit = self.cost.slippage_points * spec.point * pos.volume * contract
        commission = self.cost.commission(pos.volume)
        net = gross - commission
        spread_cost = pos.spread_cost_entry + spread_exit
        slippage_cost = pos.slip_cost_entry + slip_exit
        risk_usd = pos.stop_distance * pos.volume * contract
        r_multiple = net / risk_usd if risk_usd > 0 else 0.0
        self.c.execute(
            "UPDATE shadow_trades SET exit_ms=?, exit_price=?, net_pnl=?, r_multiple=?, "
            "exit_reason=?, spread_cost=?, slippage_cost=?, status='closed' WHERE shadow_id=?",
            (
                int(b.time) * 1000,
                price,
                net,
                r_multiple,
                reason,
                spread_cost,
                slippage_cost,
                pos.shadow_id,
            ),
        )
        r = self.cfg.owner.risk
        if net < 0:
            bd.consecutive_losses += 1
            if bd.consecutive_losses >= r.consecutive_loss_pause:
                bd.cooldown_until_s = int(b.time) + r.cooldown_min * 60
                bd.consecutive_losses = 0
        else:
            bd.consecutive_losses = 0
        events.append(
            {
                "bundle_id": bd.bundle_id,
                "event": "closed",
                "reason": reason,
                "price": price,
                "net_pnl": net,
                "r_multiple": r_multiple,
                "bar_s": b.time,
            }
        )
        bd.position = None

    # --- frames ----------------------------------------------------------------------------
    def _frame_for(self, b: Bar, compute: bool = True) -> pd.DataFrame | None:
        """The features frame ending at ``b``: the provider's, or the ledger's own trailing bars
        (a replayed or out-of-order bar leaves them unchanged). ``compute=False`` only records
        the bar."""
        if self.frame_provider is not None:
            return self.frame_provider() if compute else None
        if not self._bars or b.time > self._bars[-1].time:
            self._bars.append(b)
        if not compute or len(self._bars) < 3:
            return None
        return add_features(bars_to_frame(list(self._bars)))

    # --- queries ---------------------------------------------------------------------------
    def open_positions(self) -> list[dict[str, Any]]:
        """Open ``shadow_trades`` rows (all bundles), oldest first."""
        return [
            dict(r)
            for r in self.c.execute(
                "SELECT * FROM shadow_trades WHERE status='open' ORDER BY entry_ms"
            )
        ]

    def closed(self, bundle_id: str, since_ms: int = 0) -> list[dict[str, Any]]:
        """Closed rows of a bundle with ``exit_ms >= since_ms``, in exit order."""
        return [
            dict(r)
            for r in self.c.execute(
                "SELECT * FROM shadow_trades WHERE bundle_id=? AND status='closed' AND exit_ms>=? "
                "ORDER BY exit_ms, shadow_id",
                (bundle_id, int(since_ms)),
            )
        ]

    def slippage_ratio(self, bundle_id: str, expected_points: float) -> float | None:
        """Mean realised ``slippage_cost`` per closed trade ÷ the cost model's expectation.

        Expected per trade = ``expected_points × point × volume × contract × 2`` (two fills).
        LEARNING_SYSTEM §3.3: the shadow gate needs this ≤ 1.25. ``None`` without closed trades.
        """
        if expected_points <= 0:
            return None
        rows = self.closed(bundle_id)
        if not rows:
            return None
        contract = float(self.spec.trade_contract_size)
        ratios = [
            float(r["slippage_cost"])
            / (expected_points * self.spec.point * float(r["volume"]) * contract * 2.0)
            for r in rows
            if float(r["volume"]) > 0
        ]
        return float(np.mean(ratios)) if ratios else None

    def summary(self, bundle_id: str) -> dict[str, Any]:
        """Counts for the shadow gate: closed trades, distinct trading days, net P&L, mean R."""
        rows = self.closed(bundle_id)
        days = {int(r["exit_ms"]) // 86_400_000 for r in rows}
        net = [float(r["net_pnl"]) for r in rows]
        rs = [float(r["r_multiple"]) for r in rows if r["r_multiple"] is not None]
        return {
            "bundle_id": bundle_id,
            "n_closed": len(rows),
            "trading_days": len(days),
            "net_pnl": float(sum(net)),
            "mean_r": float(np.mean(rs)) if rs else None,
            "open": bd.position is not None
            if (bd := self._bundles.get(bundle_id)) is not None
            else False,
        }

    # --- helpers ---------------------------------------------------------------------------
    @staticmethod
    def _state(value: SessionState | str) -> SessionState:
        if isinstance(value, SessionState):
            return value
        try:
            return SessionState(str(value))
        except ValueError:
            return SessionState.CLOSED  # fail closed

    @staticmethod
    def _as_bar(bar: Bar | Mapping[str, Any] | sqlite3.Row | pd.Series) -> Bar:
        if isinstance(bar, Bar):
            return bar
        get: Callable[[str, Any], Any]
        if isinstance(bar, sqlite3.Row):
            keys = set(bar.keys())

            def get(k: str, default: Any = None) -> Any:
                return bar[k] if k in keys else default

        else:

            def get(k: str, default: Any = None) -> Any:
                try:
                    v = bar[k]
                except (KeyError, IndexError):
                    return default
                return default if v is None else v

        time_s = get("time", get("time_s"))
        if time_s is None:
            raise ValueError("bar needs a 'time' (epoch s) field")
        values = {f: float(get(f)) for f in _BAR_FIELDS}
        return Bar(
            time=int(time_s),
            seconds=int(get("seconds", 60)),
            ticks=int(get("ticks", 0)),
            **values,
        )

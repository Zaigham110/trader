"""Event-driven single-position backtester on bid/ask bars (TEST_PLAN §2, D-009, D-014, D-020).

Rules (all enforced in code, none configurable away):
  * Decision at bar i uses features/signals of bar i (causal); the order is filled at bar i+1's
    open (long at ask, short at bid) plus stressed spread and slippage — never at bar i's close.
  * Session state from `SessionClock` at the fill bar's open time: entries only when
    `entries_allowed`; any open position is flattened at the first bar whose state is
    PRE_BREAK_BUFFER, CLOSE_ONLY or CLOSED (intraday only, D-020).
  * SL/TP are checked intrabar on the bar's bid (long) or ask (short) extremes. If both are hit in
    one bar the stop is assumed first (conservative). Gaps fill at the worse of level and open.
  * Costs: spread crossed at entry and exit from the bar's own bid/ask, slippage, commission;
    swap is 0 by construction (no overnight holds).
  * Risk: one position; volume from risk per trade and stop distance, rounded down to volume_step,
    clamped to [volume_min, max_volume_lots]; the realised risk % is recorded and the entry is
    rejected if it exceeds the configured risk per trade; daily and weekly loss limits block
    entries; the drawdown limit stops the run (kill switch). Spread cap and 'wide' regime block.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from goldtrader.backtest.costs import CostModel
from goldtrader.backtest.strategies import Strategy
from goldtrader.config.schema import Config
from goldtrader.session.calendar import Event
from goldtrader.session.state import SessionClock
from goldtrader.types import SessionState, SymbolSpec

TRADE_COLUMNS = [
    "entry_time",
    "exit_time",
    "side",
    "volume",
    "entry_price",
    "exit_price",
    "sl",
    "tp",
    "gross_pnl",
    "spread_cost",
    "slippage_cost",
    "commission",
    "swap",
    "net_pnl",
    "r_multiple",
    "exit_reason",
    "risk_pct",
    "stop_distance",
    "atr_at_entry",
    "regime",
    "session_hour",
    "bars_held",
]


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    equity: pd.Series  # equity marked at every bar close (index = bar time, UTC)
    daily_returns: pd.Series  # simple returns per trading day
    rejected: dict[str, int] = field(default_factory=dict)
    killed_at: datetime | None = None
    config_summary: dict = field(default_factory=dict)

    @property
    def n_trades(self) -> int:
        return len(self.trades)


@dataclass
class _Open:
    side: str
    volume: float
    entry_price: float
    entry_time: int
    sl: float
    tp: float
    stop_distance: float
    risk_pct: float
    atr: float
    regime: str
    spread_cost_entry: float
    slip_cost_entry: float
    bars_held: int = 0


def _round_volume(v: float, spec: SymbolSpec) -> float:
    steps = math.floor(v / spec.volume_step + 1e-9)
    return round(steps * spec.volume_step, 8)


def size_position(
    equity: float, stop_distance: float, spec: SymbolSpec, cfg: Config
) -> tuple[float, float]:
    """Return (volume, risk_pct). Risk-based sizing clamped to [volume_min, max_volume_lots].

    If the risk-based volume is below volume_min, the minimum is used and the realised risk % is
    returned so the caller can reject it against the owner's limit (this is exactly the D-017 case).
    """
    risk_usd = equity * cfg.owner.risk.risk_per_trade_pct / 100.0
    per_lot_loss = stop_distance * spec.trade_contract_size
    if per_lot_loss <= 0:
        return 0.0, float("inf")
    vol = _round_volume(risk_usd / per_lot_loss, spec)
    vol = min(vol, cfg.owner.risk.max_volume_lots, spec.volume_max)
    vol = max(vol, spec.volume_min)
    risk_pct = vol * per_lot_loss / equity * 100.0 if equity > 0 else float("inf")
    return vol, risk_pct


def run_backtest(
    df: pd.DataFrame,
    strategy: Strategy,
    cfg: Config,
    spec: SymbolSpec,
    *,
    cost: CostModel | None = None,
    clock: SessionClock | None = None,
    events: list[Event] | tuple[Event, ...] = (),
    equity0: float = 5000.0,
) -> BacktestResult:
    cost = cost or CostModel(
        commission_per_lot_per_side_usd=cfg.account.commission_per_lot_per_side_usd
    )
    clock = clock or SessionClock(cfg.session)
    required = {
        "time",
        "bid_open",
        "bid_high",
        "bid_low",
        "ask_open",
        "ask_high",
        "ask_low",
        "bid_close",
        "ask_close",
        "atr",
        "spread_pips",
        "spread_regime",
        "regime",
        "hour_utc",
    }
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"features frame missing columns: {sorted(missing)}")
    if len(df) < 3:
        raise ValueError("need at least 3 bars")

    sig = strategy.signals(df)
    n = len(df)
    t = df["time"].to_numpy(dtype=np.int64)
    bo, bh, bl, bc = (
        df[c].to_numpy(dtype=float) for c in ("bid_open", "bid_high", "bid_low", "bid_close")
    )
    ao, ah, al, ac = (
        df[c].to_numpy(dtype=float) for c in ("ask_open", "ask_high", "ask_low", "ask_close")
    )
    atr = df["atr"].to_numpy(dtype=float)
    spread_pips = df["spread_pips"].to_numpy(dtype=float)
    spread_regime = df["spread_regime"].astype(str).to_numpy()
    regime = df["regime"].astype(str).to_numpy()
    r = cfg.owner.risk

    equity = equity0
    peak = equity0
    equity_curve = np.empty(n)
    trades: list[dict] = []
    rejected: dict[str, int] = {}
    pos: _Open | None = None
    killed_at: datetime | None = None
    day_pnl: dict[int, float] = {}
    week_pnl: dict[int, float] = {}
    consecutive_losses = 0
    cooldown_until = 0
    state_cache: dict[int, tuple[SessionState, bool]] = {}

    def reject(code: str) -> None:
        rejected[code] = rejected.get(code, 0) + 1

    def session(i: int) -> tuple[SessionState, bool]:
        key = int(t[i] // 60)
        if key not in state_cache:
            d = clock.state_at(datetime.fromtimestamp(int(t[i]), UTC), events=events)
            state_cache[key] = (d.state, d.entries_allowed)
        return state_cache[key]

    def close_position(i: int, price: float, reason: str) -> None:
        nonlocal pos, equity, consecutive_losses, cooldown_until
        assert pos is not None
        contract = spec.trade_contract_size
        direction = 1.0 if pos.side == "long" else -1.0
        gross = direction * (price - pos.entry_price) * pos.volume * contract
        # exit costs: half the (stressed) spread crossed at exit plus slippage, measured vs mid
        mid_exit = (bo[i] + ao[i]) / 2.0
        spread_exit = abs(price - mid_exit) * pos.volume * contract
        slip_exit = cost.slippage_points * spec.point * pos.volume * contract
        commission = cost.commission(pos.volume)
        # gross is already net of spread/slippage (prices are fill prices); report components
        spread_cost = pos.spread_cost_entry + spread_exit
        slippage_cost = pos.slip_cost_entry + slip_exit
        net = gross - commission
        equity += net
        day = int(t[i] // 86400)
        week = int((t[i] + 3 * 86400) // (7 * 86400))  # weeks start Monday
        day_pnl[day] = day_pnl.get(day, 0.0) + net
        week_pnl[week] = week_pnl.get(week, 0.0) + net
        risk_usd = pos.stop_distance * pos.volume * contract
        trades.append(
            {
                "entry_time": pos.entry_time,
                "exit_time": int(t[i]),
                "side": pos.side,
                "volume": pos.volume,
                "entry_price": pos.entry_price,
                "exit_price": price,
                "sl": pos.sl,
                "tp": pos.tp,
                "gross_pnl": gross + spread_cost + slippage_cost,
                "spread_cost": spread_cost,
                "slippage_cost": slippage_cost,
                "commission": commission,
                "swap": 0.0,
                "net_pnl": net,
                "r_multiple": net / risk_usd if risk_usd > 0 else 0.0,
                "exit_reason": reason,
                "risk_pct": pos.risk_pct,
                "stop_distance": pos.stop_distance,
                "atr_at_entry": pos.atr,
                "regime": pos.regime,
                "session_hour": int(datetime.fromtimestamp(pos.entry_time, UTC).hour),
                "bars_held": pos.bars_held,
            }
        )
        if net < 0:
            consecutive_losses += 1
            if consecutive_losses >= r.consecutive_loss_pause:
                cooldown_until = int(t[i]) + r.cooldown_min * 60
                consecutive_losses = 0
        else:
            consecutive_losses = 0
        pos = None

    pending: int = 0  # signal side carried from bar i-1 to be filled at bar i open

    for i in range(n):
        state, entries_ok = session(i)
        day = int(t[i] // 86400)
        week = int((t[i] + 3 * 86400) // (7 * 86400))

        # 1. fill the pending entry at this bar's open
        if pending != 0 and pos is None and killed_at is None:
            side = "long" if pending > 0 else "short"
            why = None
            if not entries_ok or state is not SessionState.OPEN:
                why = f"session_{state.value.lower()}"
            elif not np.isfinite(atr[i - 1]) or atr[i - 1] <= 0:
                why = "no_atr"
            elif spread_pips[i] > r.max_spread_pips or spread_regime[i] in ("wide", "unknown"):
                why = "spread"
            elif equity < r.min_equity_to_enter_usd:
                why = "min_equity"
            elif day_pnl.get(day, 0.0) <= -equity0 * r.daily_loss_pct / 100.0:
                why = "daily_loss"
            elif week_pnl.get(week, 0.0) <= -equity0 * r.weekly_loss_pct / 100.0:
                why = "weekly_loss"
            elif t[i] < cooldown_until:
                why = "cooldown"
            if why is None:
                a = atr[i - 1]
                stop_atr = min(max(sig.stop_atr, r.stop_atr_min), r.stop_atr_max)
                target_atr = max(sig.target_atr, stop_atr * r.min_reward_risk)
                stop_distance = stop_atr * a
                entry = cost.entry_price(side, bo[i], ao[i], spec)
                vol, risk_pct = size_position(equity, stop_distance, spec, cfg)
                if vol < spec.volume_min or risk_pct > r.risk_per_trade_pct + 1e-9:
                    reject("risk_per_trade")
                else:
                    mid = (bo[i] + ao[i]) / 2.0
                    contract = spec.trade_contract_size
                    sl = entry - stop_distance if side == "long" else entry + stop_distance
                    tp = entry + target_atr * a if side == "long" else entry - target_atr * a
                    pos = _Open(
                        side=side,
                        volume=vol,
                        entry_price=entry,
                        entry_time=int(t[i]),
                        sl=round(sl, spec.digits),
                        tp=round(tp, spec.digits),
                        stop_distance=stop_distance,
                        risk_pct=risk_pct,
                        atr=float(a),
                        regime=str(regime[i - 1]),
                        spread_cost_entry=abs(entry - mid) * vol * contract,
                        slip_cost_entry=cost.slippage_points * spec.point * vol * contract,
                    )
            else:
                reject(why)
        pending = 0

        # 2. manage the open position on this bar
        if pos is not None:
            pos.bars_held += 1
            if state in (
                SessionState.PRE_BREAK_BUFFER,
                SessionState.CLOSE_ONLY,
                SessionState.CLOSED,
                SessionState.STALE,
            ):
                close_position(i, cost.exit_price(pos.side, bo[i], ao[i], spec), "session_flatten")
            else:
                if pos.side == "long":
                    hit_sl = bl[i] <= pos.sl
                    hit_tp = bh[i] >= pos.tp
                    if hit_sl:
                        px = min(pos.sl, bo[i]) if cost.stop_gap_fill else pos.sl
                        close_position(i, px - cost.slippage_points * spec.point, "stop")
                    elif hit_tp:
                        close_position(i, pos.tp, "target")
                else:
                    hit_sl = ah[i] >= pos.sl
                    hit_tp = al[i] <= pos.tp
                    if hit_sl:
                        px = max(pos.sl, ao[i]) if cost.stop_gap_fill else pos.sl
                        close_position(i, px + cost.slippage_points * spec.point, "stop")
                    elif hit_tp:
                        close_position(i, pos.tp, "target")

        # 3. mark equity, check drawdown kill switch
        floating = 0.0
        if pos is not None:
            px = bc[i] if pos.side == "long" else ac[i]
            direction = 1.0 if pos.side == "long" else -1.0
            floating = direction * (px - pos.entry_price) * pos.volume * spec.trade_contract_size
        marked = equity + floating
        equity_curve[i] = marked
        peak = max(peak, marked)
        if killed_at is None and peak > 0 and (peak - marked) / peak * 100.0 >= r.max_drawdown_pct:
            killed_at = datetime.fromtimestamp(int(t[i]), UTC)
            if pos is not None:
                close_position(i, cost.exit_price(pos.side, bc[i], ac[i], spec), "kill_switch")
                equity_curve[i] = equity

        # 4. decide the next entry from this bar's signal (acted on at bar i+1 open)
        if killed_at is None and pos is None and i + 1 < n and sig.side[i] != 0:
            pending = int(sig.side[i])

    if pos is not None:  # end of data: close at last close (reported as 'end_of_data')
        close_position(n - 1, cost.exit_price(pos.side, bc[-1], ac[-1], spec), "end_of_data")
        equity_curve[-1] = equity

    idx = pd.to_datetime(t, unit="s", utc=True)
    eq = pd.Series(equity_curve, index=idx, name="equity")
    daily = eq.groupby(eq.index.floor("D")).last()
    daily_ret = daily.pct_change().dropna()
    trades_df = pd.DataFrame(trades, columns=TRADE_COLUMNS)
    return BacktestResult(
        trades=trades_df,
        equity=eq,
        daily_returns=daily_ret,
        rejected=rejected,
        killed_at=killed_at,
        config_summary={
            "profile": cfg.owner.profile,
            "risk": cfg.owner.risk.model_dump(),
            "cost": cost.as_dict(),
            "strategy": getattr(strategy, "id", "?"),
            "equity0": equity0,
        },
    )

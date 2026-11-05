"""Deterministic risk engine (RISK_POLICY.md §1–§4, R-003, R-010, R-050, D-006, D-032).

Pure functions: no I/O, no clock, no randomness. Every input comes in as data so the same code runs
in the gateway before `order_send`, in the brain's gate, in the backtester and in the property test.
Checks run in a fixed order and the first failure is returned; the order is part of the contract
because operators read the reject code.
"""

from __future__ import annotations

import math

from goldtrader.config.schema import Config
from goldtrader.mt5 import constants as K
from goldtrader.risk.types import (
    AccountState,
    Approved,
    EntryRequest,
    MarketState,
    RejectCode,
    Rejected,
    RiskContext,
    StopMoveRequest,
    Verdict,
)
from goldtrader.types import SessionState

MIN_STOP_POINTS = 2  # Exness: stops cannot sit inside the spread; keep ≥ 2 points clear


def _round_step(v: float, step: float) -> float:
    return round(math.floor(v / step + 1e-9) * step, 8)


def _valid_volume(v: float, m: MarketState) -> bool:
    if v < m.volume_min - 1e-12 or v > m.volume_max + 1e-12:
        return False
    steps = v / m.volume_step
    return abs(steps - round(steps)) < 1e-6


def _drawdown_pct(acct: AccountState, cfg: Config) -> float:
    base = acct.equity_peak
    if cfg.owner.profile == "micro_live":
        base = max(acct.live_start_equity, 0.0)
    if base <= 0:
        return 0.0
    return max(0.0, (base - acct.equity) / base * 100.0)


def size_for_request(
    equity: float, stop_distance: float, m: MarketState, cfg: Config
) -> tuple[float, float]:
    """Risk-based volume rounded down to the step and clamped to [volume_min, max_volume_lots].

    Returns (volume, risk_pct). When the risk-based volume is below the minimum lot the minimum is
    returned with its true risk percentage; the caller rejects it against the owner's limit (D-017).
    """
    r = cfg.owner.risk
    per_lot_loss = stop_distance * m.contract_size
    if per_lot_loss <= 0 or equity <= 0:
        return 0.0, float("inf")
    vol = _round_step(equity * r.risk_per_trade_pct / 100.0 / per_lot_loss, m.volume_step)
    vol = min(vol, r.max_volume_lots, m.volume_max)
    vol = max(vol, m.volume_min)
    return vol, vol * per_lot_loss / equity * 100.0


def _global_blocks(
    acct: AccountState, m: MarketState, cfg: Config, ctx: RiskContext
) -> Rejected | None:
    r = cfg.owner.risk
    if ctx.kill_switch:
        return Rejected(RejectCode.KILL_SWITCH)
    if ctx.paused:
        return Rejected(RejectCode.PAUSED)
    if not ctx.owner_hash_ok:
        return Rejected(RejectCode.OWNER_HASH_MISMATCH)
    if not ctx.spec_ok:
        return Rejected(RejectCode.SPEC_CRITICAL_CHANGE)
    if ctx.incident_open:
        return Rejected(RejectCode.INCIDENT_OPEN)
    if acct.day_pnl <= -acct.start_equity_day * r.daily_loss_pct / 100.0:
        return Rejected(RejectCode.DAILY_LOSS, f"day_pnl={acct.day_pnl:.2f}")
    if acct.week_pnl <= -acct.start_equity_week * r.weekly_loss_pct / 100.0:
        return Rejected(RejectCode.WEEKLY_LOSS, f"week_pnl={acct.week_pnl:.2f}")
    dd = _drawdown_pct(acct, cfg)
    if dd >= r.max_drawdown_pct:
        return Rejected(RejectCode.DRAWDOWN, f"drawdown={dd:.2f}%")
    return None


def check_entry(
    req: EntryRequest, acct: AccountState, m: MarketState, cfg: Config, ctx: RiskContext
) -> Verdict:
    r = cfg.owner.risk
    blocked = _global_blocks(acct, m, cfg, ctx)
    if blocked is not None:
        return blocked
    if m.session is not SessionState.OPEN:
        return Rejected(RejectCode.SESSION_CLOSED, m.session.value)
    if not m.entries_allowed:
        return Rejected(RejectCode.ENTRIES_NOT_ALLOWED)
    if m.trade_mode != K.SYMBOL_TRADE_MODE_FULL:
        if not (
            (m.trade_mode == K.SYMBOL_TRADE_MODE_LONGONLY and req.side == "long")
            or (m.trade_mode == K.SYMBOL_TRADE_MODE_SHORTONLY and req.side == "short")
        ):
            return Rejected(RejectCode.NOT_FULL_TRADE_MODE, str(m.trade_mode))
    if m.tick_age_s > cfg.session.stale_tick_seconds:
        return Rejected(RejectCode.STALE_DATA, f"{m.tick_age_s:.1f}s")
    if not (math.isfinite(m.bid) and math.isfinite(m.ask) and 0 < m.bid <= m.ask):
        return Rejected(RejectCode.INVALID_PRICES)
    if m.spread_pips > r.max_spread_pips:
        return Rejected(RejectCode.SPREAD_TOO_WIDE, f"{m.spread_pips:.1f} pips")
    if m.spread_regime != "normal":
        return Rejected(RejectCode.SPREAD_REGIME_WIDE, m.spread_regime)
    if m.atr is None or not math.isfinite(m.atr) or m.atr <= 0:
        return Rejected(RejectCode.NO_ATR)
    if acct.open_positions >= r.max_positions:
        return Rejected(RejectCode.MAX_POSITIONS, str(acct.open_positions))
    if ctx.now_ms < acct.cooldown_until_ms:
        return Rejected(RejectCode.COOLDOWN)
    if acct.orders_last_minute >= r.max_orders_per_minute:
        return Rejected(RejectCode.ORDER_RATE)
    if acct.equity < r.min_equity_to_enter_usd or acct.equity <= 0:
        return Rejected(RejectCode.MIN_EQUITY, f"equity={acct.equity:.2f}")

    entry = m.ask if req.side == "long" else m.bid
    clear = max(m.stops_level_points, MIN_STOP_POINTS) * m.point
    if not math.isfinite(req.stop_price) or req.stop_price <= 0:
        return Rejected(RejectCode.INVALID_PRICES, "stop")
    if req.side == "long":
        if req.stop_price >= m.bid:
            return Rejected(RejectCode.STOP_WRONG_SIDE)
        if m.bid - req.stop_price < clear:
            return Rejected(RejectCode.STOP_INSIDE_SPREAD)
        stop_distance = entry - req.stop_price
    else:
        if req.stop_price <= m.ask:
            return Rejected(RejectCode.STOP_WRONG_SIDE)
        if req.stop_price - m.ask < clear:
            return Rejected(RejectCode.STOP_INSIDE_SPREAD)
        stop_distance = req.stop_price - entry
    atr_mult = stop_distance / m.atr
    if atr_mult < r.stop_atr_min:
        return Rejected(RejectCode.STOP_TOO_TIGHT, f"{atr_mult:.2f} ATR")
    if atr_mult > r.stop_atr_max:
        return Rejected(RejectCode.STOP_TOO_WIDE, f"{atr_mult:.2f} ATR")

    reward_risk: float | None = None
    if req.target_price is not None:
        if not math.isfinite(req.target_price):
            return Rejected(RejectCode.INVALID_PRICES, "target")
        if req.side == "long" and req.target_price <= m.ask:
            return Rejected(RejectCode.TARGET_WRONG_SIDE)
        if req.side == "short" and req.target_price >= m.bid:
            return Rejected(RejectCode.TARGET_WRONG_SIDE)
        reward = abs(req.target_price - entry)
        reward_risk = reward / stop_distance
        if reward_risk < r.min_reward_risk:
            return Rejected(RejectCode.REWARD_RISK, f"{reward_risk:.2f}")

    if req.volume is None:
        volume, risk_pct = size_for_request(acct.equity, stop_distance, m, cfg)
    else:
        volume = req.volume
        if not _valid_volume(volume, m):
            return Rejected(RejectCode.VOLUME_INVALID, str(volume))
        risk_pct = volume * stop_distance * m.contract_size / acct.equity * 100.0
    if volume > r.max_volume_lots + 1e-12:
        return Rejected(RejectCode.VOLUME_ABOVE_MAX, str(volume))
    if not _valid_volume(volume, m):
        return Rejected(RejectCode.VOLUME_INVALID, str(volume))
    if risk_pct > r.risk_per_trade_pct + 1e-9:
        return Rejected(RejectCode.RISK_PER_TRADE, f"{risk_pct:.2f}% > {r.risk_per_trade_pct}%")

    if m.margin_per_lot is not None:
        required = m.margin_per_lot * volume
        if required > acct.margin_free:
            return Rejected(RejectCode.MARGIN_INSUFFICIENT, f"need {required:.2f}")
        used = acct.equity / acct.margin_level * 100.0 if acct.margin_level else 0.0
        projected = (
            acct.equity / (used + required) * 100.0 if (used + required) > 0 else float("inf")
        )
        if projected < r.min_margin_level_pct:
            return Rejected(RejectCode.MARGIN_LEVEL, f"{projected:.0f}%")

    notes = []
    if risk_pct > 2.0:
        notes.append(f"owner exception D-017: risk {risk_pct:.1f}% of equity")
    return Approved(
        volume=volume,
        stop_price=round(req.stop_price, m.digits),
        target_price=round(req.target_price, m.digits) if req.target_price is not None else None,
        risk_usd=volume * stop_distance * m.contract_size,
        risk_pct=risk_pct,
        stop_distance=stop_distance,
        reward_risk=reward_risk,
        notes=tuple(notes),
    )


def check_stop_move(req: StopMoveRequest, m: MarketState, ctx: RiskContext) -> Verdict:
    """Stops move only toward profit and must stay on the losing side of the current price."""
    if not ctx.owner_hash_ok:
        return Rejected(RejectCode.OWNER_HASH_MISMATCH)
    if not (math.isfinite(req.new_stop) and req.new_stop > 0):
        return Rejected(RejectCode.INVALID_PRICES, "new_stop")
    clear = max(m.stops_level_points, MIN_STOP_POINTS) * m.point
    freeze = m.freeze_level_points * m.point
    if req.side == "long":
        if req.new_stop <= req.current_stop:
            return Rejected(RejectCode.STOP_MOVE_NOT_TOWARD_PROFIT)
        if req.new_stop > m.bid - clear:
            return Rejected(RejectCode.STOP_MOVE_ACROSS_PRICE)
        if freeze > 0 and m.bid - req.current_stop <= freeze:
            return Rejected(RejectCode.STOP_FROZEN)
    else:
        if req.new_stop >= req.current_stop:
            return Rejected(RejectCode.STOP_MOVE_NOT_TOWARD_PROFIT)
        if req.new_stop < m.ask + clear:
            return Rejected(RejectCode.STOP_MOVE_ACROSS_PRICE)
        if freeze > 0 and req.current_stop - m.ask <= freeze:
            return Rejected(RejectCode.STOP_FROZEN)
    stop_distance = abs(req.price_open - req.new_stop)
    return Approved(
        volume=0.0,
        stop_price=round(req.new_stop, m.digits),
        target_price=None,
        risk_usd=0.0,
        risk_pct=0.0,
        stop_distance=stop_distance,
        reward_risk=None,
    )


def check_close(position_known: bool, m: MarketState, ctx: RiskContext) -> Verdict:
    """Closes are always allowed when the position exists (even in CLOSE_ONLY or with kill switch)."""
    if not position_known:
        return Rejected(RejectCode.POSITION_UNKNOWN)
    if m.trade_mode == K.SYMBOL_TRADE_MODE_DISABLED:
        return Rejected(RejectCode.NOT_FULL_TRADE_MODE, "broker disabled trading; retry later")
    return Approved(
        volume=0.0,
        stop_price=0.0,
        target_price=None,
        risk_usd=0.0,
        risk_pct=0.0,
        stop_distance=0.0,
        reward_risk=None,
    )

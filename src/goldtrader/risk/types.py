"""Risk engine contracts (RISK_POLICY.md, R-003, R-050). Pure data; the engine is a pure function.

The LLM never constructs these directly: the brain's `proposals` module turns a gated proposal
into an `EntryRequest`, and the gateway's execution engine calls `check_entry` again before
`order_send` (defence in depth, D-006).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from goldtrader.types import SessionState, Side


class RejectCode(StrEnum):
    KILL_SWITCH = "kill_switch"
    PAUSED = "paused"
    OWNER_HASH_MISMATCH = "owner_hash_mismatch"
    SPEC_CRITICAL_CHANGE = "spec_critical_change"
    INCIDENT_OPEN = "incident_open"
    DAILY_LOSS = "daily_loss"
    WEEKLY_LOSS = "weekly_loss"
    DRAWDOWN = "drawdown"
    SESSION_CLOSED = "session_closed"
    ENTRIES_NOT_ALLOWED = "entries_not_allowed"
    STALE_DATA = "stale_data"
    SPREAD_TOO_WIDE = "spread_too_wide"
    SPREAD_REGIME_WIDE = "spread_regime_wide"
    NO_ATR = "no_atr"
    MAX_POSITIONS = "max_positions"
    COOLDOWN = "cooldown"
    ORDER_RATE = "order_rate"
    MIN_EQUITY = "min_equity"
    INVALID_PRICES = "invalid_prices"
    STOP_WRONG_SIDE = "stop_wrong_side"
    STOP_INSIDE_SPREAD = "stop_inside_spread"
    STOP_TOO_TIGHT = "stop_too_tight"
    STOP_TOO_WIDE = "stop_too_wide"
    TARGET_WRONG_SIDE = "target_wrong_side"
    REWARD_RISK = "reward_risk"
    VOLUME_INVALID = "volume_invalid"
    VOLUME_ABOVE_MAX = "volume_above_max"
    RISK_PER_TRADE = "risk_per_trade"
    MARGIN_LEVEL = "margin_level"
    MARGIN_INSUFFICIENT = "margin_insufficient"
    PROHIBITED_LOGIC = "prohibited_logic"
    STOP_MOVE_NOT_TOWARD_PROFIT = "stop_move_not_toward_profit"
    STOP_MOVE_ACROSS_PRICE = "stop_move_across_price"
    STOP_FROZEN = "stop_frozen"
    POSITION_UNKNOWN = "position_unknown"
    NOT_FULL_TRADE_MODE = "not_full_trade_mode"


@dataclass(frozen=True, slots=True)
class EntryRequest:
    side: Side
    stop_price: float
    target_price: float | None
    bundle_id: str
    proposal_id: str
    volume: float | None = None  # None → size from risk; a value is validated, never raised
    reason: str = ""


@dataclass(frozen=True, slots=True)
class StopMoveRequest:
    position_id: int
    side: Side
    new_stop: float
    current_stop: float
    price_open: float


@dataclass(frozen=True, slots=True)
class AccountState:
    equity: float
    balance: float
    margin_free: float
    margin_level: float | None  # None when no margin is used
    leverage: int
    open_positions: int
    day_pnl: float  # realised + floating since the trading day started
    week_pnl: float
    equity_peak: float  # for drawdown; the owner can reset it
    orders_last_minute: int
    consecutive_losses: int
    cooldown_until_ms: int
    start_equity_day: float
    start_equity_week: float
    live_start_equity: float  # drawdown base for micro_live kill switch


@dataclass(frozen=True, slots=True)
class MarketState:
    bid: float
    ask: float
    point: float
    digits: int
    atr: float | None
    spread_pips: float
    spread_regime: str  # 'normal' | 'wide' | 'unknown'
    session: SessionState
    entries_allowed: bool
    tick_age_s: float
    trade_mode: int
    contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level_points: int
    freeze_level_points: int
    margin_per_lot: float | None  # from order_calc_margin at current price, None if unknown


@dataclass(frozen=True, slots=True)
class RiskContext:
    kill_switch: bool = False
    paused: bool = False
    owner_hash_ok: bool = True
    spec_ok: bool = True
    incident_open: bool = False
    now_ms: int = 0


@dataclass(frozen=True, slots=True)
class Approved:
    volume: float
    stop_price: float
    target_price: float | None
    risk_usd: float
    risk_pct: float
    stop_distance: float
    reward_risk: float | None
    notes: tuple[str, ...] = field(default_factory=tuple)

    ok: bool = True


@dataclass(frozen=True, slots=True)
class Rejected:
    code: RejectCode
    detail: str = ""
    ok: bool = False


Verdict = Approved | Rejected

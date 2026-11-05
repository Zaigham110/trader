"""Shared value types. Pure data, no I/O. All times are UTC.

Field names mirror the MetaTrader5 Python package structures so that the real adapter is a
straight copy and FakeMT5 produces identical shapes (docs/research/02_exness_mt5.md §1a).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

Side = Literal["long", "short"]


class SessionState(str, Enum):
    OPEN = "OPEN"                      # trading allowed, no restriction
    PRE_BREAK_BUFFER = "PRE_BREAK_BUFFER"  # inside the pre-break buffer: flatten, no entries
    CLOSE_ONLY = "CLOSE_ONLY"          # broker allows closes only
    CLOSED = "CLOSED"                  # market closed (daily break, weekend, maintenance)
    NEWS_WINDOW = "NEWS_WINDOW"        # high-impact release window: no entries
    FIX_WINDOW = "FIX_WINDOW"          # LBMA fix window: no entries
    STALE = "STALE"                    # no fresh ticks while schedule says open: treat as closed


@dataclass(frozen=True, slots=True)
class Tick:
    time_msc: int          # epoch milliseconds UTC
    bid: float
    ask: float
    last: float = 0.0
    volume: int = 0
    flags: int = 0

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def mid(self) -> float:
        return (self.ask + self.bid) / 2.0


@dataclass(frozen=True, slots=True)
class Bar:
    """One bar built from ticks; bid and ask kept separately (costs are modelled from both)."""
    time: int              # epoch seconds UTC of bar open
    seconds: int           # bar length (1, 60, ...)
    bid_open: float
    bid_high: float
    bid_low: float
    bid_close: float
    ask_open: float
    ask_high: float
    ask_low: float
    ask_close: float
    ticks: int

    @property
    def spread_close(self) -> float:
        return self.ask_close - self.bid_close

    @property
    def mid_close(self) -> float:
        return (self.ask_close + self.bid_close) / 2.0


@dataclass(frozen=True, slots=True)
class SymbolSpec:
    """Subset of symbol_info() the system depends on. Read live at startup (KU-001)."""
    name: str
    digits: int
    point: float
    trade_tick_size: float
    trade_tick_value: float
    trade_contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    volume_limit: float
    trade_stops_level: int
    trade_freeze_level: int
    trade_mode: int            # SYMBOL_TRADE_MODE_*
    trade_exemode: int         # SYMBOL_TRADE_EXECUTION_*
    filling_mode: int          # bitmask SYMBOL_FILLING_*
    swap_mode: int
    swap_long: float
    swap_short: float
    swap_rollover3days: int
    margin_initial: float
    margin_hedged: float
    currency_base: str
    currency_profit: str
    currency_margin: str
    spread: int
    spread_float: bool
    description: str = ""
    path: str = ""

    # Fields whose change must fail closed (docs/ARCHITECTURE.md §5)
    CRITICAL: tuple[str, ...] = field(default=(
        "trade_contract_size", "volume_min", "volume_step", "digits", "point",
        "trade_mode", "trade_exemode", "filling_mode", "currency_profit",
    ), repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class AccountInfo:
    login: int
    trade_mode: int            # ACCOUNT_TRADE_MODE_* (0 demo, 1 contest, 2 real)
    leverage: int
    limit_orders: int
    margin_so_mode: int
    trade_allowed: bool
    trade_expert: bool
    margin_mode: int           # ACCOUNT_MARGIN_MODE_* (2 = retail hedging)
    currency_digits: int
    balance: float
    credit: float
    profit: float
    equity: float
    margin: float
    margin_free: float
    margin_level: float
    margin_so_call: float
    margin_so_so: float
    currency: str
    server: str
    company: str
    name: str = ""


@dataclass(frozen=True, slots=True)
class TerminalInfo:
    connected: bool
    trade_allowed: bool
    tradeapi_disabled: bool
    build: int
    maxbars: int
    ping_last: int
    dlls_allowed: bool = False
    company: str = ""
    name: str = ""
    path: str = ""


@dataclass(slots=True)
class OrderRequest:
    """MqlTradeRequest as a Python dict-able object (docs/research/02_exness_mt5.md §1a)."""
    action: int
    symbol: str
    volume: float
    type: int
    price: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    deviation: int = 10
    magic: int = 0
    order: int = 0
    position: int = 0
    position_by: int = 0
    stoplimit: float = 0.0
    type_filling: int = 0
    type_time: int = 0
    expiration: int = 0
    comment: str = ""

    def to_mt5(self) -> dict:
        return {
            "action": self.action, "symbol": self.symbol, "volume": self.volume, "type": self.type,
            "price": self.price, "sl": self.sl, "tp": self.tp, "deviation": self.deviation,
            "magic": self.magic, "order": self.order, "position": self.position,
            "position_by": self.position_by, "stoplimit": self.stoplimit,
            "type_filling": self.type_filling, "type_time": self.type_time,
            "expiration": self.expiration, "comment": self.comment,
        }


@dataclass(frozen=True, slots=True)
class OrderResult:
    retcode: int
    deal: int = 0
    order: int = 0
    volume: float = 0.0
    price: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    comment: str = ""
    request_id: int = 0
    retcode_external: int = 0


@dataclass(frozen=True, slots=True)
class OrderCheckResult:
    retcode: int
    balance: float
    equity: float
    profit: float
    margin: float
    margin_free: float
    margin_level: float
    comment: str = ""


@dataclass(frozen=True, slots=True)
class Position:
    ticket: int
    time: int
    time_msc: int
    time_update: int
    type: int                  # 0 buy, 1 sell
    magic: int
    identifier: int
    reason: int
    volume: float
    price_open: float
    sl: float
    tp: float
    price_current: float
    swap: float
    profit: float
    symbol: str
    comment: str = ""

    @property
    def side(self) -> Side:
        return "long" if self.type == 0 else "short"


@dataclass(frozen=True, slots=True)
class Deal:
    ticket: int
    order: int
    time: int
    time_msc: int
    type: int                  # 0 buy, 1 sell, others (balance etc.)
    entry: int                 # 0 in, 1 out, 2 inout, 3 out_by
    magic: int
    position_id: int
    reason: int
    volume: float
    price: float
    commission: float
    swap: float
    profit: float
    fee: float
    symbol: str
    comment: str = ""


@dataclass(frozen=True, slots=True)
class PendingOrder:
    ticket: int
    time_setup: int
    type: int
    state: int
    magic: int
    volume_initial: float
    volume_current: float
    price_open: float
    sl: float
    tp: float
    symbol: str
    comment: str = ""

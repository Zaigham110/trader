"""`MT5Client` protocol: the MetaTrader5 package surface the system uses, typed with our dataclasses.

Both `goldtrader.mt5.real.RealMT5` (Windows, wraps the `MetaTrader5` package) and
`goldtrader.mt5.fake.FakeMT5` (in-memory double for tests and Linux development, D-028) implement
this protocol so that the gateway (docs/ARCHITECTURE.md §2.1 `mt5_client`) never imports the
Windows-only package directly (R-001).

Conventions shared by every implementation (docs/research/02_exness_mt5.md §1a):

* All datetimes are UTC-aware; epoch fields are UTC seconds / milliseconds.
* `copy_rates_*` return `Bar` objects. MT5 bars are **bid-based**: `open/high/low/close` are bid
  prices and `spread` is the bar's spread in points. Adapters map bid OHLC = rates and
  ask OHLC = bid + spread × point; `Bar.ticks` = `tick_volume`.
* Trade requests are `OrderRequest`; results are `OrderResult` / `OrderCheckResult` with the
  trade-server retcodes from `goldtrader.mt5.constants`.
* A function the package would answer with `None` (no data, not connected) returns `None` here
  too; the caller consults `last_error()`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from goldtrader.mt5 import constants as K
from goldtrader.types import (
    AccountInfo,
    Bar,
    Deal,
    OrderCheckResult,
    OrderRequest,
    OrderResult,
    PendingOrder,
    Position,
    SymbolSpec,
    TerminalInfo,
    Tick,
)

RetcodeClass = Literal["ok", "transient", "fatal"]


# --------------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------------


class MT5Error(Exception):
    """Base class for every error raised by the MT5 access layer."""


class MT5NotConnected(MT5Error):
    """The terminal is not connected to the trade server (retcode 10031, `connected == False`)."""


class MT5TradeDisabled(MT5Error):
    """Trading is disabled: AutoTrading off (10026/10027), investor password or account flag (10017),
    symbol trade_mode not FULL (10018/10044)."""


class MT5OrderRejected(MT5Error):
    """The trade server refused a request. Carries the retcode and the server comment."""

    def __init__(self, retcode: int, comment: str = "") -> None:
        self.retcode = retcode
        self.comment = comment
        super().__init__(f"{K.retcode_name(retcode)} ({retcode}): {comment}".rstrip(": "))

    @property
    def classification(self) -> RetcodeClass:
        return classify_retcode(self.retcode)


def classify_retcode(code: int) -> RetcodeClass:
    """Map a trade-server retcode to 'ok' | 'transient' | 'fatal'.

    'transient' codes may get one bounded retry after backoff (docs/MT5_EXNESS_INTEGRATION.md §4);
    'fatal' codes are never retried blindly (docs/ARCHITECTURE.md §5: 10019, 10016, ...). Unknown
    codes are treated as fatal so that a new server behaviour fails closed.
    """
    if code in K.RETCODE_OK:
        return "ok"
    if code in K.RETCODE_TRANSIENT:
        return "transient"
    return "fatal"


# --------------------------------------------------------------------------------------------
# Protocol
# --------------------------------------------------------------------------------------------


@runtime_checkable
class MT5Client(Protocol):
    """The subset of the MetaTrader5 package we use, returning `goldtrader.types` dataclasses."""

    # -- connection -------------------------------------------------------------------------
    def initialize(
        self,
        path: str | None = None,
        login: int | None = None,
        password: str | None = None,
        server: str | None = None,
        timeout: int = 60000,
        portable: bool = False,
    ) -> bool:
        """Connect to the terminal (launching it if needed). Never log `password`."""
        ...

    def shutdown(self) -> None:
        """Close the connection to the terminal."""
        ...

    def login(self, login: int, password: str, server: str) -> bool:
        """Log in to a trade account on an already-initialized terminal."""
        ...

    def version(self) -> tuple | None:
        """`(terminal_version, build, release_date)` or None."""
        ...

    def last_error(self) -> tuple[int, str]:
        """`(code, description)` of the last package-level error (`RES_E_*`)."""
        ...

    # -- account / terminal -----------------------------------------------------------------
    def account_info(self) -> AccountInfo | None: ...

    def terminal_info(self) -> TerminalInfo | None: ...

    # -- symbols ----------------------------------------------------------------------------
    def symbols_get(self, group: str | None = None) -> list[str]:
        """Symbol names, optionally filtered with an MT5 group mask such as `*XAU*`."""
        ...

    def symbol_info(self, symbol: str) -> SymbolSpec | None: ...

    def symbol_select(self, symbol: str, enable: bool = True) -> bool:
        """Add (or remove) the symbol in Market Watch; required before ticks/rates/orders."""
        ...

    def symbol_info_tick(self, symbol: str) -> Tick | None: ...

    # -- history ----------------------------------------------------------------------------
    def copy_rates_from(
        self, symbol: str, timeframe: int, date_from: datetime, count: int
    ) -> list[Bar] | None:
        """Up to `count` bid-based bars ending at or before `date_from` (UTC), oldest first."""
        ...

    def copy_rates_range(
        self, symbol: str, timeframe: int, date_from: datetime, date_to: datetime
    ) -> list[Bar] | None:
        """Bid-based bars with open time in `[date_from, date_to]` (UTC), oldest first."""
        ...

    def copy_ticks_range(
        self, symbol: str, date_from: datetime, date_to: datetime, flags: int
    ) -> list[Tick] | None: ...

    def copy_ticks_from(
        self, symbol: str, date_from: datetime, count: int, flags: int
    ) -> list[Tick] | None: ...

    # -- trading ----------------------------------------------------------------------------
    def order_calc_margin(
        self, action: int, symbol: str, volume: float, price: float
    ) -> float | None:
        """Margin in account currency for an order of `volume` lots; `action` is an ORDER_TYPE_*."""
        ...

    def order_check(self, request: OrderRequest) -> OrderCheckResult | None:
        """Pre-check a request without sending it (margin, stops, volume)."""
        ...

    def order_send(self, request: OrderRequest) -> OrderResult | None:
        """Send a trade request. Only the execution engine may call this (R-003)."""
        ...

    def positions_get(
        self, symbol: str | None = None, ticket: int | None = None
    ) -> list[Position]: ...

    def orders_get(self, symbol: str | None = None) -> list[PendingOrder]: ...

    def history_deals_get(
        self, date_from: datetime, date_to: datetime, position: int | None = None
    ) -> list[Deal]:
        """Deals in `[date_from, date_to]`; with `position`, every deal of that position id."""
        ...


__all__ = [
    "MT5Client",
    "MT5Error",
    "MT5NotConnected",
    "MT5OrderRejected",
    "MT5TradeDisabled",
    "RetcodeClass",
    "classify_retcode",
]

"""`RealMT5`: adapter over the official `MetaTrader5` package (Windows only, R-001, D-028).

The package is imported lazily inside `__init__`, so this module can be imported everywhere and
only *constructing* `RealMT5` requires Windows + a running terminal. On Linux use
`goldtrader.mt5.fake.FakeMT5`.

Field mapping (docs/research/02_exness_mt5.md §1a): every package structure is a named tuple with
`_asdict()`; we copy only the fields declared on our dataclasses in `goldtrader.types` and ignore
the rest (`fifo_close`, `assets`, `external_id`, `community_*`, ...). Rates come back as a numpy
structured array with columns `time, open, high, low, close, tick_volume, spread, real_volume`;
MT5 bars are **bid-based**, so `Bar.bid_*` = OHLC and `Bar.ask_* = bid_* + spread × point`
(`spread` is in points), `Bar.ticks = tick_volume`. Ticks have `time, bid, ask, last, volume,
time_msc, flags, volume_real`.

Times: the package takes and returns UTC (`copy_*` "in UTC time zone (without the shift)"), so
every datetime passed in is converted to an aware UTC datetime first; naive datetimes are assumed
to be UTC. Passwords are never logged or stored.
"""

from __future__ import annotations

import logging
from dataclasses import MISSING, fields
from datetime import UTC, datetime
from typing import Any

from goldtrader.mt5.fake import timeframe_seconds
from goldtrader.mt5.protocol import MT5Error
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

log = logging.getLogger(__name__)
_UTC = UTC


def to_utc(dt: datetime) -> datetime:
    """Return an aware UTC datetime; a naive input is taken as already UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_UTC)
    return dt.astimezone(_UTC)


def _asdict(obj: Any) -> dict:
    """`_asdict()` for named tuples, `__dict__`/`vars` otherwise, plain dicts pass through."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "_asdict"):
        return obj._asdict()
    return {
        k: getattr(obj, k)
        for k in dir(obj)
        if not k.startswith("_") and not callable(getattr(obj, k))
    }


def _defaults(cls: type) -> set[str]:
    return {
        f.name for f in fields(cls) if f.default is not MISSING or f.default_factory is not MISSING
    }


def _build(cls: type, row: dict, **overrides: Any):
    names = {f.name for f in fields(cls) if f.init} - {"CRITICAL"}
    data = {k: v for k, v in row.items() if k in names}
    data.update(overrides)
    missing = names - set(data) - _defaults(cls)
    if missing:
        raise MT5Error(f"{cls.__name__}: fields missing from MT5 structure: {sorted(missing)}")
    return cls(**data)


def bar_from_rate(rate: Any, point: float, seconds: int) -> Bar:
    """Map one MT5 rate row (numpy structured record or mapping) to a `Bar` (bid-based, see above)."""
    get = rate.__getitem__ if not hasattr(rate, "_asdict") else rate._asdict().__getitem__
    o, h, lo, c = float(get("open")), float(get("high")), float(get("low")), float(get("close"))
    spread = float(get("spread")) * point
    return Bar(
        time=int(get("time")),
        seconds=seconds,
        bid_open=o,
        bid_high=h,
        bid_low=lo,
        bid_close=c,
        ask_open=o + spread,
        ask_high=h + spread,
        ask_low=lo + spread,
        ask_close=c + spread,
        ticks=int(get("tick_volume")),
    )


def tick_from_row(row: Any) -> Tick:
    get = row.__getitem__ if not hasattr(row, "_asdict") else row._asdict().__getitem__
    return Tick(
        time_msc=int(get("time_msc")),
        bid=float(get("bid")),
        ask=float(get("ask")),
        last=float(get("last")),
        volume=int(get("volume")),
        flags=int(get("flags")),
    )


class RealMT5:
    """Thin wrapper over the `MetaTrader5` package; implements `goldtrader.mt5.protocol.MT5Client`."""

    def __init__(self) -> None:
        try:
            import MetaTrader5 as mt5  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - exercised only off Windows
            raise MT5Error("MetaTrader5 package is Windows-only; use FakeMT5 on Linux") from exc
        self._mt5 = mt5
        self._point_cache: dict[str, float] = {}

    # ------------------------------------------------------------------ connection
    def initialize(
        self, path=None, login=None, password=None, server=None, timeout=60000, portable=False
    ) -> bool:
        kwargs: dict[str, Any] = {"timeout": timeout, "portable": portable}
        if login is not None:
            kwargs.update(login=int(login), password=password, server=server)
        ok = bool(self._mt5.initialize(path, **kwargs) if path else self._mt5.initialize(**kwargs))
        log.info("mt5 initialize path=%s login=%s server=%s ok=%s", path, login, server, ok)
        return ok

    def shutdown(self) -> None:
        self._mt5.shutdown()

    def login(self, login: int, password: str, server: str) -> bool:
        ok = bool(self._mt5.login(int(login), password=password, server=server))
        log.info("mt5 login login=%s server=%s ok=%s", login, server, ok)
        return ok

    def version(self) -> tuple | None:
        v = self._mt5.version()
        return tuple(v) if v else None

    def last_error(self) -> tuple[int, str]:
        code, text = self._mt5.last_error()
        return int(code), str(text)

    # ------------------------------------------------------------------ account / terminal
    def account_info(self) -> AccountInfo | None:
        raw = self._mt5.account_info()
        return None if raw is None else _build(AccountInfo, _asdict(raw))

    def terminal_info(self) -> TerminalInfo | None:
        raw = self._mt5.terminal_info()
        return None if raw is None else _build(TerminalInfo, _asdict(raw))

    # ------------------------------------------------------------------ symbols
    def symbols_get(self, group: str | None = None) -> list[str]:
        raw = self._mt5.symbols_get(group=group) if group else self._mt5.symbols_get()
        return [s.name for s in (raw or ())]

    def symbol_info(self, symbol: str) -> SymbolSpec | None:
        raw = self._mt5.symbol_info(symbol)
        if raw is None:
            return None
        spec = _build(SymbolSpec, _asdict(raw))
        self._point_cache[symbol] = spec.point
        return spec

    def symbol_select(self, symbol: str, enable: bool = True) -> bool:
        return bool(self._mt5.symbol_select(symbol, enable))

    def symbol_info_tick(self, symbol: str) -> Tick | None:
        raw = self._mt5.symbol_info_tick(symbol)
        return None if raw is None else tick_from_row(raw)

    # ------------------------------------------------------------------ history
    def _point(self, symbol: str) -> float:
        if symbol not in self._point_cache and self.symbol_info(symbol) is None:
            raise MT5Error(f"symbol_info({symbol!r}) failed: {self.last_error()}")
        return self._point_cache[symbol]

    def copy_rates_from(
        self, symbol: str, timeframe: int, date_from: datetime, count: int
    ) -> list[Bar] | None:
        rates = self._mt5.copy_rates_from(symbol, timeframe, to_utc(date_from), int(count))
        return self._bars(symbol, timeframe, rates)

    def copy_rates_range(
        self, symbol: str, timeframe: int, date_from: datetime, date_to: datetime
    ) -> list[Bar] | None:
        rates = self._mt5.copy_rates_range(symbol, timeframe, to_utc(date_from), to_utc(date_to))
        return self._bars(symbol, timeframe, rates)

    def _bars(self, symbol: str, timeframe: int, rates: Any) -> list[Bar] | None:
        if rates is None:
            return None
        point = self._point(symbol)
        seconds = timeframe_seconds(timeframe)
        return [bar_from_rate(r, point, seconds) for r in rates]

    def copy_ticks_range(
        self, symbol: str, date_from: datetime, date_to: datetime, flags: int
    ) -> list[Tick] | None:
        raw = self._mt5.copy_ticks_range(symbol, to_utc(date_from), to_utc(date_to), int(flags))
        return None if raw is None else [tick_from_row(t) for t in raw]

    def copy_ticks_from(
        self, symbol: str, date_from: datetime, count: int, flags: int
    ) -> list[Tick] | None:
        raw = self._mt5.copy_ticks_from(symbol, to_utc(date_from), int(count), int(flags))
        return None if raw is None else [tick_from_row(t) for t in raw]

    # ------------------------------------------------------------------ trading
    def order_calc_margin(
        self, action: int, symbol: str, volume: float, price: float
    ) -> float | None:
        m = self._mt5.order_calc_margin(int(action), symbol, float(volume), float(price))
        return None if m is None else float(m)

    def order_check(self, request: OrderRequest) -> OrderCheckResult | None:
        raw = self._mt5.order_check(request.to_mt5())
        return None if raw is None else _build(OrderCheckResult, _asdict(raw))

    def order_send(self, request: OrderRequest) -> OrderResult | None:
        """The only call that reaches the trade server with a write (R-003 callers only)."""
        raw = self._mt5.order_send(request.to_mt5())
        return None if raw is None else _build(OrderResult, _asdict(raw))

    def positions_get(self, symbol: str | None = None, ticket: int | None = None) -> list[Position]:
        if ticket is not None:
            raw = self._mt5.positions_get(ticket=int(ticket))
        elif symbol is not None:
            raw = self._mt5.positions_get(symbol=symbol)
        else:
            raw = self._mt5.positions_get()
        return [_build(Position, _asdict(p)) for p in (raw or ())]

    def orders_get(self, symbol: str | None = None) -> list[PendingOrder]:
        raw = self._mt5.orders_get(symbol=symbol) if symbol else self._mt5.orders_get()
        return [_build(PendingOrder, _asdict(o)) for o in (raw or ())]

    def history_deals_get(
        self, date_from: datetime, date_to: datetime, position: int | None = None
    ) -> list[Deal]:
        if position is not None:
            raw = self._mt5.history_deals_get(position=int(position))
        else:
            raw = self._mt5.history_deals_get(to_utc(date_from), to_utc(date_to))
        return [_build(Deal, _asdict(d)) for d in (raw or ())]


__all__ = ["RealMT5", "bar_from_rate", "tick_from_row", "to_utc"]

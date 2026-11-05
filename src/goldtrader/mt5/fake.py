"""`FakeMT5`: an in-memory MetaTrader 5 double for tests and Linux development (D-028).

It implements `goldtrader.mt5.protocol.MT5Client` with the documented MT5 return codes and data
shapes (docs/research/02_exness_mt5.md §1a, §4, §10) so that the gateway, the execution engine
(R-003, R-004) and the on-account suite can be exercised without a terminal:

* a configurable `SymbolSpec` (default: Exness Standard `XAUUSDm`) and `AccountInfo` (default: a
  5,000 USD hedging demo account at 1:500);
* a tick feed (`feed()` / `set_price()`); SL/TP and pending orders are evaluated on every tick;
* market execution: BUY fills at ask (+ `slippage_points`), SELL at bid (− slippage); closes by
  opposite `TRADE_ACTION_DEAL` with `position=`, partial closes, `TRADE_ACTION_SLTP`,
  `TRADE_ACTION_CLOSE_BY`, pending orders (`TRADE_ACTION_PENDING` / `MODIFY` / `REMOVE`);
* every validation retcode the engine handles (10014, 10016, 10017, 10018, 10019, 10024, 10027,
  10030, 10031, 10038, 10040, ...) plus `reject_next()` for scripted rejections;
* deals (entry IN / OUT / OUT_BY) with profit and commission, balance/equity/margin math.

Simplifications, on purpose: margin is the plain sum of per-position
`volume × contract × price / leverage` (Exness charges 0 on the hedged part — the fake is
conservative); no swap is ever booked (intraday system; swap fields are only reported); no
stop-out; the Exness slippage-free rule for pending orders is modelled as "fill at the requested
price if the gap is below 3 × spread, else at market"; `[SL violated]` cancels a pending order whose
fill would already be beyond its own SL/TP.

Everything is deterministic: tickets come from one counter, times from the fed ticks and the
rate limiter uses an injectable `clock`.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from fnmatch import fnmatchcase

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

# ENUM_DEAL_REASON / ENUM_POSITION_REASON values used for bookkeeping (MQL5 docs)
DEAL_REASON_CLIENT = 0
DEAL_REASON_EXPERT = 3
DEAL_REASON_SL = 4
DEAL_REASON_TP = 5
ORDER_STATE_PLACED = 1

_PENDING_TYPES = {
    K.ORDER_TYPE_BUY_LIMIT,
    K.ORDER_TYPE_SELL_LIMIT,
    K.ORDER_TYPE_BUY_STOP,
    K.ORDER_TYPE_SELL_STOP,
}
_BUY_TYPES = {K.ORDER_TYPE_BUY, K.ORDER_TYPE_BUY_LIMIT, K.ORDER_TYPE_BUY_STOP}
_FILL_FLAG = {
    K.ORDER_FILLING_FOK: K.SYMBOL_FILLING_FOK,
    K.ORDER_FILLING_IOC: K.SYMBOL_FILLING_IOC,
    K.ORDER_FILLING_BOC: K.SYMBOL_FILLING_BOC,
    # ORDER_FILLING_RETURN is not allowed on market execution (KU-012): no flag → 10030
}

_UTC = UTC

DEFAULT_SPEC = SymbolSpec(
    name="XAUUSDm",
    digits=2,
    point=0.01,
    trade_tick_size=0.01,
    trade_tick_value=1.0,
    trade_contract_size=100.0,
    volume_min=0.01,
    volume_max=200.0,
    volume_step=0.01,
    volume_limit=0.0,
    trade_stops_level=0,
    trade_freeze_level=0,
    trade_mode=K.SYMBOL_TRADE_MODE_FULL,
    trade_exemode=K.SYMBOL_TRADE_EXECUTION_MARKET,
    filling_mode=K.SYMBOL_FILLING_FOK | K.SYMBOL_FILLING_IOC,
    swap_mode=1,
    swap_long=-30.0,
    swap_short=10.0,
    swap_rollover3days=3,
    margin_initial=0.0,
    margin_hedged=0.0,
    currency_base="XAU",
    currency_profit="USD",
    currency_margin="USD",
    spread=20,
    spread_float=True,
    description="Gold vs US Dollar",
    path="Metals\\XAUUSDm",
)

DEFAULT_ACCOUNT = AccountInfo(
    login=10000001,
    trade_mode=K.ACCOUNT_TRADE_MODE_DEMO,
    leverage=500,
    limit_orders=1000,
    margin_so_mode=0,
    trade_allowed=True,
    trade_expert=True,
    margin_mode=K.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING,
    currency_digits=2,
    balance=5000.0,
    credit=0.0,
    profit=0.0,
    equity=5000.0,
    margin=0.0,
    margin_free=5000.0,
    margin_level=0.0,
    margin_so_call=60.0,
    margin_so_so=0.0,
    currency="USD",
    server="Exness-MT5Trial",
    company="Exness Technologies Ltd",
    name="Fake Trader",
)


def timeframe_seconds(timeframe: int) -> int:
    """Seconds per bar for the MetaTrader5 package timeframe encoding (M1=1 ... H1=16385, D1=16408)."""
    if timeframe <= 0:
        raise ValueError(f"invalid timeframe {timeframe}")
    if timeframe < 0x4000:
        return timeframe * 60
    if timeframe < 0x8000:
        return (timeframe - 0x4000) * 3600
    if timeframe < 0xC000:
        return (timeframe - 0x8000) * 7 * 86400
    return (timeframe - 0xC000) * 30 * 86400


def bars_from_ticks(ticks: list[Tick], seconds: int) -> list[Bar]:
    """Aggregate ticks (time-ordered) into fixed-length bid/ask bars."""
    bars: list[Bar] = []
    cur: dict | None = None
    for t in ticks:
        open_time = (t.time_msc // 1000) // seconds * seconds
        if cur is None or cur["time"] != open_time:
            if cur is not None:
                bars.append(_close_bar(cur))
            cur = {
                "time": open_time,
                "bo": t.bid,
                "bh": t.bid,
                "bl": t.bid,
                "bc": t.bid,
                "ao": t.ask,
                "ah": t.ask,
                "al": t.ask,
                "ac": t.ask,
                "n": 1,
            }
            continue
        cur["bh"] = max(cur["bh"], t.bid)
        cur["bl"] = min(cur["bl"], t.bid)
        cur["bc"] = t.bid
        cur["ah"] = max(cur["ah"], t.ask)
        cur["al"] = min(cur["al"], t.ask)
        cur["ac"] = t.ask
        cur["n"] += 1
    if cur is not None:
        bars.append(_close_bar(cur))
    # seconds is folded in here because _close_bar does not know it
    return [replace(b, seconds=seconds) for b in bars]


def _close_bar(c: dict) -> Bar:
    return Bar(
        time=c["time"],
        seconds=0,
        bid_open=c["bo"],
        bid_high=c["bh"],
        bid_low=c["bl"],
        bid_close=c["bc"],
        ask_open=c["ao"],
        ask_high=c["ah"],
        ask_low=c["al"],
        ask_close=c["ac"],
        ticks=c["n"],
    )


def _ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_UTC)
    return int(dt.timestamp() * 1000)


@dataclass
class _Pos:
    """Mutable position record; `Position` snapshots are built on read."""

    ticket: int
    time_msc: int
    time_update: int
    type: int
    magic: int
    volume: float
    price_open: float
    sl: float
    tp: float
    symbol: str
    comment: str


@dataclass
class _Order:
    ticket: int
    time_setup: int
    type: int
    magic: int
    volume: float
    price: float
    sl: float
    tp: float
    symbol: str
    comment: str


class FakeMT5:
    """In-memory MT5 terminal + trade server. See the module docstring."""

    def __init__(
        self,
        spec: SymbolSpec | None = None,
        account: AccountInfo | None = None,
        *,
        symbols: list[str] | None = None,
        slippage_points: int = 0,
        commission_per_lot_per_side: float = 0.0,
        max_positions: int | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        spec = spec or DEFAULT_SPEC
        account = account or DEFAULT_ACCOUNT
        self.specs: dict[str, SymbolSpec] = {spec.name: spec}
        self.symbols: list[str] = symbols or [spec.name, "XAUUSD247m", "XAUEURm", "EURUSDm"]
        self.selected: set[str] = set()
        self._acct = account
        self.balance: float = account.balance
        self.credit: float = account.credit
        # toggles
        self.initialized: bool = True
        self.init_ok: bool = True
        self.connected: bool = True
        self.autotrading_enabled: bool = True
        self.slippage_points: int = slippage_points
        self.commission_per_lot_per_side: float = commission_per_lot_per_side
        self.max_positions: int | None = max_positions
        self.build: int = 5120
        self.ping_last: int = 45000  # microseconds, like TERMINAL_PING_LAST
        self.clock: Callable[[], float] = clock or time.monotonic
        # market state
        self.ticks: list[Tick] = []
        self.tick: Tick | None = None
        # books
        self._positions: dict[int, _Pos] = {}
        self._orders: dict[int, _Order] = {}
        self.deals: list[Deal] = []
        self.requests: list[OrderRequest] = []
        self.results: list[OrderResult] = []
        self.cancelled_orders: list[tuple[int, str]] = []  # (ticket, reason) e.g. "[SL violated]"
        # scripting
        self._reject_next: list[int] = []
        self._rate_limit: int | None = None
        self._request_times: deque[float] = deque()
        self._ticket = 1000
        self._request_id = 0
        self._last_error: tuple[int, str] = (K.RES_S_OK, "Success")

    # ------------------------------------------------------------------ scripting helpers
    @property
    def spec(self) -> SymbolSpec:
        return next(iter(self.specs.values()))

    def set_spec(self, spec: SymbolSpec) -> None:
        """Replace the spec of the (single) traded symbol; e.g. to simulate a contract change."""
        self.specs = {spec.name: spec}
        if spec.name not in self.symbols:
            self.symbols.insert(0, spec.name)

    def set_trade_mode(self, mode: int) -> None:
        """Simulate the daily break: SYMBOL_TRADE_MODE_CLOSEONLY / DISABLED / FULL."""
        self.set_spec(replace(self.spec, trade_mode=mode))

    def set_account(self, **fields) -> None:
        """Override static account fields (trade_mode, trade_allowed, leverage, ...)."""
        if "balance" in fields:
            self.balance = float(fields.pop("balance"))
        self._acct = replace(self._acct, **fields)

    def reject_next(self, retcode: int, comment: str = "") -> None:
        """Script the next `order_send` to fail with `retcode` (queued, FIFO)."""
        self._reject_next.append(retcode)
        self._reject_comment = comment

    def rate_limit_after(self, n: int | None) -> None:
        """Return 10024 once more than `n` trade requests arrive within one second (None = off)."""
        self._rate_limit = n
        self._request_times.clear()

    def feed(self, tick: Tick) -> None:
        """Push a tick; updates P&L, then evaluates SL/TP and pending orders at this tick."""
        self.ticks.append(tick)
        self.tick = tick
        self._process_tick(tick)

    def set_price(self, bid: float, ask: float, time_msc: int | None = None) -> Tick:
        """Convenience wrapper around `feed`; time advances 1 s from the last tick when omitted."""
        if time_msc is None:
            time_msc = (self.tick.time_msc + 1000) if self.tick else 1_700_000_000_000
        tick = Tick(time_msc=time_msc, bid=bid, ask=ask, last=0.0, volume=0, flags=6)
        self.feed(tick)
        return tick

    # ------------------------------------------------------------------ connection
    def initialize(
        self, path=None, login=None, password=None, server=None, timeout=60000, portable=False
    ) -> bool:
        del path, login, password, server, timeout, portable  # never logged
        if not self.init_ok:
            self._last_error = (K.RES_E_INTERNAL_FAIL_INIT, "Internal IPC initialization failed")
            return False
        self.initialized = True
        self._last_error = (K.RES_S_OK, "Success")
        return True

    def shutdown(self) -> None:
        self.initialized = False

    def login(self, login: int, password: str, server: str) -> bool:
        del password
        if not self.initialized or not self.connected:
            self._last_error = (K.RES_E_INTERNAL_FAIL_CONNECT, "No connection")
            return False
        self._acct = replace(self._acct, login=login, server=server)
        return True

    def version(self) -> tuple | None:
        if not self.initialized:
            return None
        return (500, self.build, "fake")

    def last_error(self) -> tuple[int, str]:
        return self._last_error

    # ------------------------------------------------------------------ account / terminal
    def account_info(self) -> AccountInfo | None:
        if not self.initialized:
            self._last_error = (K.RES_E_INTERNAL_FAIL_INIT, "Not initialized")
            return None
        profit = sum(self._profit(p) for p in self._positions.values())
        margin = self._margin_used()
        equity = self.balance + self.credit + profit
        return replace(
            self._acct,
            balance=round(self.balance, 2),
            credit=self.credit,
            profit=round(profit, 2),
            equity=round(equity, 2),
            margin=round(margin, 2),
            margin_free=round(equity - margin, 2),
            margin_level=round(equity / margin * 100.0, 2) if margin > 0 else 0.0,
        )

    def terminal_info(self) -> TerminalInfo | None:
        if not self.initialized:
            return None
        return TerminalInfo(
            connected=self.connected,
            trade_allowed=self.autotrading_enabled,
            tradeapi_disabled=False,
            build=self.build,
            maxbars=100000,
            ping_last=self.ping_last,
            dlls_allowed=False,
            company="MetaQuotes Ltd. (fake)",
            name="MetaTrader 5 (fake)",
            path="C:\\fake\\terminal64.exe",
        )

    # ------------------------------------------------------------------ symbols
    def symbols_get(self, group: str | None = None) -> list[str]:
        if not self.initialized:
            return []
        if not group:
            return list(self.symbols)
        patterns = [p.strip() for p in group.split(",") if p.strip()]
        include = [p for p in patterns if not p.startswith("!")]
        exclude = [p[1:] for p in patterns if p.startswith("!")]
        out = []
        for s in self.symbols:
            if include and not any(fnmatchcase(s, p) for p in include):
                continue
            if any(fnmatchcase(s, p) for p in exclude):
                continue
            out.append(s)
        return out

    def symbol_info(self, symbol: str) -> SymbolSpec | None:
        spec = self.specs.get(symbol) if self.initialized else None
        if spec is None:
            self._last_error = (K.RES_E_NOT_FOUND, f"symbol {symbol} not found")
            return None
        if self.tick is not None:
            spec = replace(spec, spread=round(self.tick.spread / spec.point))
        return spec

    def symbol_select(self, symbol: str, enable: bool = True) -> bool:
        if not self.initialized or symbol not in self.symbols:
            return False
        (self.selected.add if enable else self.selected.discard)(symbol)
        return True

    def symbol_info_tick(self, symbol: str) -> Tick | None:
        if not self.initialized or symbol not in self.specs or self.tick is None:
            self._last_error = (K.RES_E_NOT_FOUND, "no tick")
            return None
        return self.tick

    # ------------------------------------------------------------------ history
    def copy_rates_from(
        self, symbol: str, timeframe: int, date_from: datetime, count: int
    ) -> list[Bar] | None:
        bars = self._bars(symbol, timeframe)
        if bars is None:
            return None
        end = _ms(date_from) // 1000
        sel = [b for b in bars if b.time <= end][-count:] if count > 0 else []
        return self._or_none(sel)

    def copy_rates_range(
        self, symbol: str, timeframe: int, date_from: datetime, date_to: datetime
    ) -> list[Bar] | None:
        bars = self._bars(symbol, timeframe)
        if bars is None:
            return None
        lo, hi = _ms(date_from) // 1000, _ms(date_to) // 1000
        return self._or_none([b for b in bars if lo <= b.time <= hi])

    def copy_ticks_range(
        self, symbol: str, date_from: datetime, date_to: datetime, flags: int
    ) -> list[Tick] | None:
        del flags
        if not self.initialized or symbol not in self.specs:
            self._last_error = (K.RES_E_NOT_FOUND, "symbol not found")
            return None
        lo, hi = _ms(date_from), _ms(date_to)
        return self._or_none([t for t in self.ticks if lo <= t.time_msc <= hi])

    def copy_ticks_from(
        self, symbol: str, date_from: datetime, count: int, flags: int
    ) -> list[Tick] | None:
        del flags
        if not self.initialized or symbol not in self.specs:
            self._last_error = (K.RES_E_NOT_FOUND, "symbol not found")
            return None
        lo = _ms(date_from)
        return self._or_none([t for t in self.ticks if t.time_msc >= lo][:count])

    def _bars(self, symbol: str, timeframe: int) -> list[Bar] | None:
        if not self.initialized or symbol not in self.specs:
            self._last_error = (K.RES_E_NOT_FOUND, "symbol not found")
            return None
        return bars_from_ticks(self.ticks, timeframe_seconds(timeframe))

    def _or_none(self, rows: list):
        if not rows:
            self._last_error = (K.RES_E_NOT_FOUND, "No history")
            return None
        self._last_error = (K.RES_S_OK, "Success")
        return rows

    # ------------------------------------------------------------------ trading: reads
    def order_calc_margin(
        self, action: int, symbol: str, volume: float, price: float
    ) -> float | None:
        spec = self.specs.get(symbol)
        if spec is None or not self.initialized:
            self._last_error = (K.RES_E_INVALID_PARAMS, "unknown symbol")
            return None
        del action
        return round(volume * spec.trade_contract_size * price / self._acct.leverage, 2)

    def positions_get(self, symbol: str | None = None, ticket: int | None = None) -> list[Position]:
        out = []
        for p in self._positions.values():
            if symbol is not None and p.symbol != symbol:
                continue
            if ticket is not None and p.ticket != ticket:
                continue
            out.append(self._snapshot(p))
        return out

    def orders_get(self, symbol: str | None = None) -> list[PendingOrder]:
        return [
            PendingOrder(
                ticket=o.ticket,
                time_setup=o.time_setup // 1000,
                type=o.type,
                state=ORDER_STATE_PLACED,
                magic=o.magic,
                volume_initial=o.volume,
                volume_current=o.volume,
                price_open=o.price,
                sl=o.sl,
                tp=o.tp,
                symbol=o.symbol,
                comment=o.comment,
            )
            for o in self._orders.values()
            if symbol is None or o.symbol == symbol
        ]

    def history_deals_get(
        self, date_from: datetime, date_to: datetime, position: int | None = None
    ) -> list[Deal]:
        if position is not None:
            return [d for d in self.deals if d.position_id == position]
        lo, hi = _ms(date_from), _ms(date_to)
        return [d for d in self.deals if lo <= d.time_msc <= hi]

    # ------------------------------------------------------------------ trading: writes
    def order_check(self, request: OrderRequest) -> OrderCheckResult | None:
        """Validate like the server would; retcode 0 means the request would be accepted."""
        self.requests.append(request)
        if not self.initialized:
            return None
        acct = self.account_info()
        assert acct is not None
        code, comment, margin = self._validate(request, check_only=True)
        if code == K.TRADE_RETCODE_DONE:
            code = 0
            comment = "Done"
        free = acct.margin_free - margin
        total = acct.margin + margin
        return OrderCheckResult(
            retcode=code,
            balance=acct.balance,
            equity=acct.equity,
            profit=acct.profit,
            margin=round(total, 2),
            margin_free=round(free, 2),
            margin_level=round(acct.equity / total * 100.0, 2) if total > 0 else 0.0,
            comment=comment,
        )

    def order_send(self, request: OrderRequest) -> OrderResult | None:
        self.requests.append(request)
        if not self.initialized:
            return None
        self._request_id += 1
        if self._reject_next:
            code = self._reject_next.pop(0)
            return self._result(code, getattr(self, "_reject_comment", "") or "Rejected (scripted)")
        code, comment, _ = self._validate(request, check_only=False)
        if code != K.TRADE_RETCODE_DONE:
            return self._result(code, comment)
        action = request.action
        if action == K.TRADE_ACTION_DEAL:
            res = self._close(request) if request.position else self._open(request)
        elif action == K.TRADE_ACTION_SLTP:
            res = self._sltp(request)
        elif action == K.TRADE_ACTION_CLOSE_BY:
            res = self._close_by(request)
        elif action == K.TRADE_ACTION_PENDING:
            res = self._place_pending(request)
        elif action == K.TRADE_ACTION_MODIFY:
            res = self._modify_pending(request)
        else:  # TRADE_ACTION_REMOVE
            del self._orders[request.order]
            res = self._result(K.TRADE_RETCODE_DONE, "Request executed", order=request.order)
        return res

    # ------------------------------------------------------------------ validation
    def _validate(self, r: OrderRequest, *, check_only: bool) -> tuple[int, str, float]:
        """Return (retcode, comment, required_margin). TRADE_RETCODE_DONE means acceptable."""
        if self._rate_limited():
            return K.TRADE_RETCODE_TOO_MANY_REQUESTS, "Too many requests", 0.0
        if not self.connected:
            return K.TRADE_RETCODE_CONNECTION, "No connection", 0.0
        if not self.autotrading_enabled:
            return K.TRADE_RETCODE_CLIENT_DISABLES_AT, "AutoTrading disabled by client", 0.0
        if not self._acct.trade_allowed or not self._acct.trade_expert:
            return K.TRADE_RETCODE_TRADE_DISABLED, "Trade is disabled", 0.0
        if r.action in (K.TRADE_ACTION_MODIFY, K.TRADE_ACTION_REMOVE):
            o = self._orders.get(r.order)
            if o is None:
                return K.TRADE_RETCODE_INVALID_ORDER, "Order not found", 0.0
            if r.action == K.TRADE_ACTION_MODIFY:
                code = self._check_pending_levels(o.type, r.price or o.price, r.sl, r.tp)
                return code, "Invalid stops" if code != K.TRADE_RETCODE_DONE else "", 0.0
            return K.TRADE_RETCODE_DONE, "", 0.0
        spec = self.specs.get(r.symbol)
        if spec is None:
            return K.TRADE_RETCODE_INVALID, "Unknown symbol", 0.0
        if r.action == K.TRADE_ACTION_SLTP:
            p = self._positions.get(r.position)
            if p is None:
                return K.TRADE_RETCODE_POSITION_CLOSED, "Position not found", 0.0
            if self.tick is None:
                return K.TRADE_RETCODE_PRICE_OFF, "No quotes", 0.0
            if r.sl == p.sl and r.tp == p.tp:
                return K.TRADE_RETCODE_NO_CHANGES, "No changes", 0.0
            code = self._check_stops(p.type, r.sl, r.tp, spec)
            return code, "Invalid stops" if code != K.TRADE_RETCODE_DONE else "", 0.0
        if r.action == K.TRADE_ACTION_CLOSE_BY:
            a, b = self._positions.get(r.position), self._positions.get(r.position_by)
            if a is None or b is None:
                return K.TRADE_RETCODE_POSITION_CLOSED, "Position not found", 0.0
            if self._acct.margin_mode != K.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING:
                return K.TRADE_RETCODE_HEDGE_PROHIBITED, "Hedging prohibited", 0.0
            if a.symbol != b.symbol or a.type == b.type:
                return K.TRADE_RETCODE_INVALID, "Positions are not opposite", 0.0
            if spec.trade_mode == K.SYMBOL_TRADE_MODE_DISABLED:
                return K.TRADE_RETCODE_MARKET_CLOSED, "Market closed", 0.0
            return K.TRADE_RETCODE_DONE, "", 0.0
        if r.action not in (K.TRADE_ACTION_DEAL, K.TRADE_ACTION_PENDING):
            return K.TRADE_RETCODE_INVALID, "Unsupported action", 0.0
        # ---- volume
        if not self._volume_ok(r.volume, spec):
            return K.TRADE_RETCODE_INVALID_VOLUME, "Invalid volume", 0.0
        if self.tick is None:
            return K.TRADE_RETCODE_PRICE_OFF, "No quotes", 0.0
        is_close = r.action == K.TRADE_ACTION_DEAL and r.position != 0
        # ---- symbol trade mode
        mode = spec.trade_mode
        if mode == K.SYMBOL_TRADE_MODE_DISABLED:
            return K.TRADE_RETCODE_MARKET_CLOSED, "Market closed", 0.0
        if not is_close:
            if mode == K.SYMBOL_TRADE_MODE_CLOSEONLY:
                return K.TRADE_RETCODE_MARKET_CLOSED, "Market closed (close only)", 0.0
            if mode == K.SYMBOL_TRADE_MODE_LONGONLY and r.type not in _BUY_TYPES:
                return K.TRADE_RETCODE_LONG_ONLY, "Long only", 0.0
            if mode == K.SYMBOL_TRADE_MODE_SHORTONLY and r.type in _BUY_TYPES:
                return K.TRADE_RETCODE_SHORT_ONLY, "Short only", 0.0
        if r.action == K.TRADE_ACTION_PENDING:
            if r.type not in _PENDING_TYPES:
                return K.TRADE_RETCODE_INVALID_ORDER, "Invalid order type", 0.0
            if len(self._orders) >= self._acct.limit_orders:
                return K.TRADE_RETCODE_LIMIT_ORDERS, "Pending order limit", 0.0
            code = self._check_pending_price(r.type, r.price, spec)
            if code != K.TRADE_RETCODE_DONE:
                return code, "Invalid price", 0.0
            code = self._check_pending_levels(r.type, r.price, r.sl, r.tp)
            if code != K.TRADE_RETCODE_DONE:
                return code, "Invalid stops", 0.0
            margin = self._required_margin(spec, r.volume, r.price)
            if self._too_many_positions():
                return K.TRADE_RETCODE_LIMIT_POSITIONS, "Position limit", 0.0
            return K.TRADE_RETCODE_DONE, "", margin
        # ---- market deal
        if r.type not in (K.ORDER_TYPE_BUY, K.ORDER_TYPE_SELL):
            return K.TRADE_RETCODE_INVALID_ORDER, "Invalid order type for DEAL", 0.0
        if _FILL_FLAG.get(r.type_filling, 0) & spec.filling_mode == 0:
            return K.TRADE_RETCODE_INVALID_FILL, "Unsupported filling mode", 0.0
        if is_close:
            p = self._positions.get(r.position)
            if p is None:
                return K.TRADE_RETCODE_POSITION_CLOSED, "Position not found", 0.0
            if p.type == r.type:
                return K.TRADE_RETCODE_INVALID, "Close requires the opposite type", 0.0
            if r.volume > p.volume + 1e-9:
                return K.TRADE_RETCODE_INVALID_CLOSE_VOLUME, "Close volume exceeds position", 0.0
            return K.TRADE_RETCODE_DONE, "", 0.0
        code = self._check_stops(r.type, r.sl, r.tp, spec)
        if code != K.TRADE_RETCODE_DONE:
            return code, "Invalid stops", 0.0
        if self._too_many_positions():
            return K.TRADE_RETCODE_LIMIT_POSITIONS, "Position limit", 0.0
        price = self.tick.ask if r.type == K.ORDER_TYPE_BUY else self.tick.bid
        margin = self._required_margin(spec, r.volume, price)
        acct = self.account_info()
        assert acct is not None
        if acct.margin_free < margin:
            return K.TRADE_RETCODE_NO_MONEY, "Not enough money", margin
        del check_only
        return K.TRADE_RETCODE_DONE, "", margin

    def _rate_limited(self) -> bool:
        if self._rate_limit is None:
            return False
        now = self.clock()
        self._request_times.append(now)
        while self._request_times and now - self._request_times[0] >= 1.0:
            self._request_times.popleft()
        return len(self._request_times) > self._rate_limit

    @staticmethod
    def _volume_ok(volume: float, spec: SymbolSpec) -> bool:
        if volume < spec.volume_min - 1e-9 or volume > spec.volume_max + 1e-9:
            return False
        steps = volume / spec.volume_step
        return abs(steps - round(steps)) < 1e-6

    def _check_stops(self, side: int, sl: float, tp: float, spec: SymbolSpec) -> int:
        """SL/TP rule for a market position (Exness: stops may not sit inside the spread).

        A long SL fires on bid <= sl, so it must be below bid; its TP fires on bid >= tp, so it
        must be above bid (a limit-type TP may be inside the spread). Mirror image for a short
        (SL above ask, TP below ask). `trade_stops_level` points of extra distance are enforced.
        """
        assert self.tick is not None
        # at least one point away (strictly outside the spread) or trade_stops_level points
        gap = max(spec.trade_stops_level, 1) * spec.point
        eps = spec.point * 1e-6
        bid, ask = self.tick.bid, self.tick.ask
        if side == K.ORDER_TYPE_BUY:
            if sl and sl > bid - gap + eps:
                return K.TRADE_RETCODE_INVALID_STOPS
            if tp and tp < bid + gap - eps:
                return K.TRADE_RETCODE_INVALID_STOPS
        else:
            if sl and sl < ask + gap - eps:
                return K.TRADE_RETCODE_INVALID_STOPS
            if tp and tp > ask - gap + eps:
                return K.TRADE_RETCODE_INVALID_STOPS
        return K.TRADE_RETCODE_DONE

    def _check_pending_price(self, otype: int, price: float, spec: SymbolSpec) -> int:
        assert self.tick is not None
        bid, ask = self.tick.bid, self.tick.ask
        if price <= 0:
            return K.TRADE_RETCODE_INVALID_PRICE
        ok = {
            K.ORDER_TYPE_BUY_LIMIT: price < ask,
            K.ORDER_TYPE_BUY_STOP: price > ask,
            K.ORDER_TYPE_SELL_LIMIT: price > bid,
            K.ORDER_TYPE_SELL_STOP: price < bid,
        }[otype]
        del spec
        return K.TRADE_RETCODE_DONE if ok else K.TRADE_RETCODE_INVALID_PRICE

    @staticmethod
    def _check_pending_levels(otype: int, price: float, sl: float, tp: float) -> int:
        buy = otype in _BUY_TYPES
        if sl and ((buy and sl >= price) or (not buy and sl <= price)):
            return K.TRADE_RETCODE_INVALID_STOPS
        if tp and ((buy and tp <= price) or (not buy and tp >= price)):
            return K.TRADE_RETCODE_INVALID_STOPS
        return K.TRADE_RETCODE_DONE

    def _too_many_positions(self) -> bool:
        # hedging accounts count pending orders too (ENUM_TRADE_RETCODE docs for 10040)
        return (
            self.max_positions is not None
            and len(self._positions) + len(self._orders) >= self.max_positions
        )

    def _required_margin(self, spec: SymbolSpec, volume: float, price: float) -> float:
        return volume * spec.trade_contract_size * price / self._acct.leverage

    # ------------------------------------------------------------------ execution
    def _open(
        self,
        r: OrderRequest,
        *,
        fill_price: float | None = None,
        reason: int = DEAL_REASON_EXPERT,
        comment: str | None = None,
    ) -> OrderResult:
        assert self.tick is not None
        spec = self.specs[r.symbol]
        slip = self.slippage_points * spec.point
        if fill_price is None:
            fill_price = (
                self.tick.ask + slip if r.type == K.ORDER_TYPE_BUY else self.tick.bid - slip
            )
        fill_price = round(fill_price, spec.digits)
        ticket = self._next_ticket()
        pos = _Pos(
            ticket=ticket,
            time_msc=self.tick.time_msc,
            time_update=self.tick.time_msc // 1000,
            type=r.type,
            magic=r.magic,
            volume=r.volume,
            price_open=fill_price,
            sl=r.sl,
            tp=r.tp,
            symbol=r.symbol,
            comment=comment if comment is not None else r.comment,
        )
        self._positions[ticket] = pos
        deal = self._book_deal(
            order=ticket,
            dtype=r.type,
            entry=K.DEAL_ENTRY_IN,
            position_id=ticket,
            volume=r.volume,
            price=fill_price,
            profit=0.0,
            magic=r.magic,
            symbol=r.symbol,
            comment=pos.comment,
            reason=reason,
        )
        return self._result(
            K.TRADE_RETCODE_DONE,
            "Request executed",
            deal=deal.ticket,
            order=ticket,
            volume=r.volume,
            price=fill_price,
        )

    def _close(self, r: OrderRequest) -> OrderResult:
        p = self._positions[r.position]
        assert self.tick is not None
        spec = self.specs[p.symbol]
        slip = self.slippage_points * spec.point
        # closing a long sells at bid; closing a short buys at ask
        price = self.tick.bid - slip if p.type == K.POSITION_TYPE_BUY else self.tick.ask + slip
        return self._close_at(
            p, r.volume, round(price, spec.digits), DEAL_REASON_EXPERT, comment=r.comment
        )

    def _close_at(
        self,
        p: _Pos,
        volume: float,
        price: float,
        reason: int,
        *,
        comment: str = "",
        entry: int = K.DEAL_ENTRY_OUT,
        profit: float | None = None,
    ) -> OrderResult:
        spec = self.specs[p.symbol]
        volume = min(volume, p.volume)
        if profit is None:
            profit = self._pnl(p, volume, price)
        order_ticket = self._next_ticket()
        close_type = K.DEAL_TYPE_SELL if p.type == K.POSITION_TYPE_BUY else K.DEAL_TYPE_BUY
        deal = self._book_deal(
            order=order_ticket,
            dtype=close_type,
            entry=entry,
            position_id=p.ticket,
            volume=volume,
            price=price,
            profit=profit,
            magic=p.magic,
            symbol=p.symbol,
            comment=comment,
            reason=reason,
        )
        remaining = round(p.volume - volume, 8)
        if remaining < spec.volume_step / 2:
            del self._positions[p.ticket]
        else:
            p.volume = remaining
            p.time_update = self.tick.time_msc // 1000 if self.tick else p.time_update
        return self._result(
            K.TRADE_RETCODE_DONE,
            "Request executed",
            deal=deal.ticket,
            order=order_ticket,
            volume=volume,
            price=price,
        )

    def _sltp(self, r: OrderRequest) -> OrderResult:
        p = self._positions[r.position]
        p.sl, p.tp = r.sl, r.tp
        p.time_update = self.tick.time_msc // 1000 if self.tick else p.time_update
        return self._result(K.TRADE_RETCODE_DONE, "Request executed", order=p.ticket)

    def _close_by(self, r: OrderRequest) -> OrderResult:
        """Close two opposite positions against each other at their open prices (no spread paid).

        The whole P&L is booked on the first leg's OUT_BY deal, the second leg's shows 0 — the
        way MT5 reports a close-by.
        """
        a, b = self._positions[r.position], self._positions[r.position_by]
        spec = self.specs[a.symbol]
        vol = min(a.volume, b.volume)
        buy, sell = (a, b) if a.type == K.POSITION_TYPE_BUY else (b, a)
        pnl = (sell.price_open - buy.price_open) * vol * spec.trade_contract_size
        first = self._close_at(
            a,
            vol,
            b.price_open,
            DEAL_REASON_EXPERT,
            entry=K.DEAL_ENTRY_OUT_BY,
            profit=pnl,
            comment=r.comment,
        )
        self._close_at(
            b,
            vol,
            a.price_open,
            DEAL_REASON_EXPERT,
            entry=K.DEAL_ENTRY_OUT_BY,
            profit=0.0,
            comment=r.comment,
        )
        return first

    def _place_pending(self, r: OrderRequest) -> OrderResult:
        assert self.tick is not None
        ticket = self._next_ticket()
        self._orders[ticket] = _Order(
            ticket=ticket,
            time_setup=self.tick.time_msc,
            type=r.type,
            magic=r.magic,
            volume=r.volume,
            price=r.price,
            sl=r.sl,
            tp=r.tp,
            symbol=r.symbol,
            comment=r.comment,
        )
        return self._result(
            K.TRADE_RETCODE_PLACED, "Request placed", order=ticket, volume=r.volume, price=r.price
        )

    def _modify_pending(self, r: OrderRequest) -> OrderResult:
        o = self._orders[r.order]
        if r.price:
            o.price = r.price
        o.sl, o.tp = r.sl, r.tp
        return self._result(K.TRADE_RETCODE_DONE, "Request executed", order=o.ticket)

    # ------------------------------------------------------------------ tick processing
    def _process_tick(self, tick: Tick) -> None:
        for p in list(self._positions.values()):
            if p.ticket not in self._positions:
                continue
            spec = self.specs.get(p.symbol)
            if spec is None:
                continue
            if p.type == K.POSITION_TYPE_BUY:
                if p.sl and tick.bid <= p.sl:
                    self._close_at(p, p.volume, tick.bid, DEAL_REASON_SL, comment="[sl]")
                elif p.tp and tick.bid >= p.tp:
                    self._close_at(p, p.volume, tick.bid, DEAL_REASON_TP, comment="[tp]")
            else:
                if p.sl and tick.ask >= p.sl:
                    self._close_at(p, p.volume, tick.ask, DEAL_REASON_SL, comment="[sl]")
                elif p.tp and tick.ask <= p.tp:
                    self._close_at(p, p.volume, tick.ask, DEAL_REASON_TP, comment="[tp]")
        for o in list(self._orders.values()):
            self._maybe_trigger(o, tick)

    def _maybe_trigger(self, o: _Order, tick: Tick) -> None:
        spec = self.specs.get(o.symbol)
        if spec is None or spec.trade_mode != K.SYMBOL_TRADE_MODE_FULL:
            if spec is not None and spec.trade_mode == K.SYMBOL_TRADE_MODE_CLOSEONLY:
                # Exness cancels pending orders during close-only periods
                del self._orders[o.ticket]
            return
        buy = o.type in _BUY_TYPES
        market = tick.ask if buy else tick.bid
        hit = {
            K.ORDER_TYPE_BUY_LIMIT: market <= o.price,
            K.ORDER_TYPE_BUY_STOP: market >= o.price,
            K.ORDER_TYPE_SELL_LIMIT: market >= o.price,
            K.ORDER_TYPE_SELL_STOP: market <= o.price,
        }[o.type]
        if not hit:
            return
        del self._orders[o.ticket]
        # Exness slippage-free range for XAUUSD: gap < 3 × spread → requested price, else market
        fill = o.price if abs(market - o.price) < 3 * tick.spread else market
        # "[SL violated]": a stop order whose fill is already beyond its TP, or a limit order
        # already beyond its SL, is cancelled instead of opened (research §4)
        is_stop = o.type in (K.ORDER_TYPE_BUY_STOP, K.ORDER_TYPE_SELL_STOP)
        violated = (
            is_stop and o.tp and ((buy and fill >= o.tp) or (not buy and fill <= o.tp))
        ) or (not is_stop and o.sl and ((buy and fill <= o.sl) or (not buy and fill >= o.sl)))
        if violated:
            self.cancelled_orders.append((o.ticket, "[SL violated]"))
            return
        margin = self._required_margin(spec, o.volume, fill)
        acct = self.account_info()
        if acct is None or acct.margin_free < margin:
            self.cancelled_orders.append((o.ticket, "[no money]"))
            return
        req = OrderRequest(
            action=K.TRADE_ACTION_DEAL,
            symbol=o.symbol,
            volume=o.volume,
            type=K.ORDER_TYPE_BUY if buy else K.ORDER_TYPE_SELL,
            price=o.price,
            sl=o.sl,
            tp=o.tp,
            magic=o.magic,
            comment=o.comment,
        )
        self._open(req, fill_price=fill, reason=DEAL_REASON_EXPERT)

    # ------------------------------------------------------------------ math
    def _profit(self, p: _Pos) -> float:
        if self.tick is None:
            return 0.0
        return self._pnl(
            p, p.volume, self.tick.bid if p.type == K.POSITION_TYPE_BUY else self.tick.ask
        )

    def _pnl(self, p: _Pos, volume: float, price: float) -> float:
        spec = self.specs[p.symbol]
        diff = price - p.price_open if p.type == K.POSITION_TYPE_BUY else p.price_open - price
        return round(diff * volume * spec.trade_contract_size, 2)

    def _margin_used(self) -> float:
        return sum(
            self._required_margin(self.specs[p.symbol], p.volume, p.price_open)
            for p in self._positions.values()
            if p.symbol in self.specs
        )

    def _snapshot(self, p: _Pos) -> Position:
        price_current = 0.0
        if self.tick is not None:
            price_current = self.tick.bid if p.type == K.POSITION_TYPE_BUY else self.tick.ask
        return Position(
            ticket=p.ticket,
            time=p.time_msc // 1000,
            time_msc=p.time_msc,
            time_update=p.time_update,
            type=p.type,
            magic=p.magic,
            identifier=p.ticket,
            reason=3,
            volume=p.volume,
            price_open=p.price_open,
            sl=p.sl,
            tp=p.tp,
            price_current=price_current,
            swap=0.0,
            profit=self._profit(p),
            symbol=p.symbol,
            comment=p.comment,
        )

    def _book_deal(
        self,
        *,
        order: int,
        dtype: int,
        entry: int,
        position_id: int,
        volume: float,
        price: float,
        profit: float,
        magic: int,
        symbol: str,
        comment: str,
        reason: int,
    ) -> Deal:
        assert self.tick is not None
        commission = -round(self.commission_per_lot_per_side * volume, 2)
        deal = Deal(
            ticket=self._next_ticket(),
            order=order,
            time=self.tick.time_msc // 1000,
            time_msc=self.tick.time_msc,
            type=dtype,
            entry=entry,
            magic=magic,
            position_id=position_id,
            reason=reason,
            volume=volume,
            price=price,
            commission=commission,
            swap=0.0,
            profit=profit,
            fee=0.0,
            symbol=symbol,
            comment=comment,
        )
        self.deals.append(deal)
        self.balance = round(self.balance + profit + commission, 2)
        return deal

    def _result(
        self,
        retcode: int,
        comment: str,
        *,
        deal: int = 0,
        order: int = 0,
        volume: float = 0.0,
        price: float = 0.0,
    ) -> OrderResult:
        bid = self.tick.bid if self.tick else 0.0
        ask = self.tick.ask if self.tick else 0.0
        res = OrderResult(
            retcode=retcode,
            deal=deal,
            order=order,
            volume=volume,
            price=price,
            bid=bid,
            ask=ask,
            comment=comment,
            request_id=self._request_id,
        )
        self.results.append(res)
        return res

    def _next_ticket(self) -> int:
        self._ticket += 1
        return self._ticket


__all__ = ["DEFAULT_ACCOUNT", "DEFAULT_SPEC", "FakeMT5", "bars_from_ticks", "timeframe_seconds"]

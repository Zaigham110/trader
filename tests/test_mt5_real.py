"""RealMT5 conversions, exercised with a stub `MetaTrader5` module (no Windows needed).

The stub mimics the package's named tuples and numpy rate/tick arrays so the field mapping in
`goldtrader.mt5.real` is verified on Linux; the real package is only ever imported on Windows.
"""
from __future__ import annotations

import sys
import types
from collections import namedtuple
from datetime import UTC, datetime, timedelta, timezone

import numpy as np
import pytest

from goldtrader.mt5 import constants as K
from goldtrader.mt5.protocol import MT5Client, MT5Error
from goldtrader.mt5.real import RealMT5, bar_from_rate, tick_from_row, to_utc
from goldtrader.types import OrderRequest

AccountTuple = namedtuple("AccountInfo", [
    "login", "trade_mode", "leverage", "limit_orders", "margin_so_mode", "trade_allowed",
    "trade_expert", "margin_mode", "currency_digits", "fifo_close", "balance", "credit", "profit",
    "equity", "margin", "margin_free", "margin_level", "margin_so_call", "margin_so_so",
    "margin_initial", "margin_maintenance", "assets", "liabilities", "commission_blocked",
    "name", "server", "currency", "company",
])
SymbolTuple = namedtuple("SymbolInfo", [
    "name", "digits", "point", "trade_tick_size", "trade_tick_value", "trade_contract_size",
    "volume_min", "volume_max", "volume_step", "volume_limit", "trade_stops_level",
    "trade_freeze_level", "trade_mode", "trade_exemode", "filling_mode", "swap_mode", "swap_long",
    "swap_short", "swap_rollover3days", "margin_initial", "margin_hedged", "currency_base",
    "currency_profit", "currency_margin", "spread", "spread_float", "description", "path",
    "session_deals", "custom",
])
ResultTuple = namedtuple("OrderSendResult", [
    "retcode", "deal", "order", "volume", "price", "bid", "ask", "comment", "request_id",
    "retcode_external", "request",
])
PositionTuple = namedtuple("TradePosition", [
    "ticket", "time", "time_msc", "time_update", "time_update_msc", "type", "magic", "identifier",
    "reason", "volume", "price_open", "sl", "tp", "price_current", "swap", "profit", "symbol",
    "comment", "external_id",
])
TickTuple = namedtuple("Tick", ["time", "bid", "ask", "last", "volume", "time_msc", "flags",
                                "volume_real"])


def make_stub() -> types.ModuleType:
    stub = types.ModuleType("MetaTrader5")
    stub.calls = []
    for name, value in vars(K).items():
        if name.isupper() and isinstance(value, int):
            setattr(stub, name, value)

    def record(name):
        def fn(*a, **kw):
            stub.calls.append((name, a, kw))
            return stub.responses.get(name)
        return fn

    for name in ("initialize", "shutdown", "login", "version", "last_error", "account_info",
                 "terminal_info", "symbols_get", "symbol_info", "symbol_select",
                 "symbol_info_tick", "copy_rates_from", "copy_rates_range", "copy_ticks_range",
                 "copy_ticks_from", "order_calc_margin", "order_check", "order_send",
                 "positions_get", "orders_get", "history_deals_get"):
        setattr(stub, name, record(name))
    rates = np.array(
        [(1_700_000_000, 4000.0, 4001.0, 3999.0, 4000.5, 120, 20, 0),
         (1_700_000_060, 4000.5, 4002.0, 4000.0, 4001.5, 80, 25, 0)],
        dtype=[("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"),
               ("close", "<f8"), ("tick_volume", "<u8"), ("spread", "<i4"),
               ("real_volume", "<u8")])
    ticks = np.array([(1_700_000_000, 4000.0, 4000.2, 0.0, 0, 1_700_000_000_123, 6, 0.0)],
                     dtype=[("time", "<i8"), ("bid", "<f8"), ("ask", "<f8"), ("last", "<f8"),
                            ("volume", "<u8"), ("time_msc", "<i8"), ("flags", "<u4"),
                            ("volume_real", "<f8")])
    stub.responses = {
        "initialize": True, "login": True, "version": (500, 5120, "01 Jan 2026"),
        "last_error": (1, "Success"),
        "account_info": AccountTuple(1, 0, 500, 1000, 0, True, True, 2, 2, False, 5000.0, 0.0,
                                     0.0, 5000.0, 0.0, 5000.0, 0.0, 60.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                                     0.0, "Owner", "Exness-MT5Trial", "USD", "Exness"),
        "symbols_get": (SymbolTuple._make(["XAUUSDm"] + [0] * 29),
                        SymbolTuple._make(["XAUUSD247m"] + [0] * 29)),
        "symbol_info": SymbolTuple("XAUUSDm", 2, 0.01, 0.01, 1.0, 100.0, 0.01, 200.0, 0.01, 0.0,
                                   0, 0, 4, 2, 3, 1, -30.0, 10.0, 3, 0.0, 0.0, "XAU", "USD", "USD",
                                   20, True, "Gold", "Metals", 0, False),
        "symbol_select": True, "symbol_info_tick": ticks[0],
        "copy_rates_from": rates, "copy_rates_range": rates, "copy_ticks_range": ticks,
        "copy_ticks_from": ticks, "order_calc_margin": 8.0,
        "order_check": types.SimpleNamespace(_asdict=lambda: {
            "retcode": 0, "balance": 5000.0, "equity": 5000.0, "profit": 0.0, "margin": 8.0,
            "margin_free": 4992.0, "margin_level": 62500.0, "comment": "Done", "request": None}),
        "order_send": ResultTuple(10009, 11, 12, 0.01, 4000.2, 4000.0, 4000.2, "Request executed",
                                  1, 0, None),
        "positions_get": (PositionTuple(12, 1_700_000_000, 1_700_000_000_000, 1_700_000_000, 0, 0,
                                        7, 12, 3, 0.01, 4000.2, 0.0, 0.0, 4000.0, 0.0, -0.2,
                                        "XAUUSDm", "", ""),),
        "orders_get": None, "history_deals_get": None,
    }
    return stub


@pytest.fixture
def real(monkeypatch) -> RealMT5:
    stub = make_stub()
    monkeypatch.setitem(sys.modules, "MetaTrader5", stub)
    client = RealMT5()
    client.stub = stub  # type: ignore[attr-defined]
    return client


def test_import_error_is_a_clear_mt5error(monkeypatch):
    monkeypatch.setitem(sys.modules, "MetaTrader5", None)  # forces ImportError
    with pytest.raises(MT5Error, match="Windows-only"):
        RealMT5()


def test_real_satisfies_protocol_and_maps_structures(real):
    assert isinstance(real, MT5Client)
    assert real.initialize(login=1, password="s3cret", server="Exness-MT5Trial") is True
    name, args, kwargs = real.stub.calls[-1]
    assert name == "initialize" and kwargs["password"] == "s3cret" and args == ()
    acct = real.account_info()
    assert acct.login == 1 and acct.margin_mode == 2 and acct.company == "Exness" \
        and acct.name == "Owner"
    spec = real.symbol_info("XAUUSDm")
    assert spec.trade_contract_size == 100.0 and spec.filling_mode == 3 and spec.path == "Metals"
    assert real.symbols_get("*XAU*") == ["XAUUSDm", "XAUUSD247m"]
    tick = real.symbol_info_tick("XAUUSDm")
    assert tick.time_msc == 1_700_000_000_123 and tick.ask == 4000.2 and tick.flags == 6
    res = real.order_send(OrderRequest(action=1, symbol="XAUUSDm", volume=0.01, type=0))
    assert res.retcode == 10009 and res.deal == 11 and res.order == 12
    assert real.stub.calls[-1][1][0]["action"] == 1  # request passed as a dict
    chk = real.order_check(OrderRequest(action=1, symbol="XAUUSDm", volume=0.01, type=0))
    assert chk.retcode == 0 and chk.margin == 8.0
    pos = real.positions_get(ticket=12)
    assert len(pos) == 1 and pos[0].identifier == 12 and pos[0].side == "long"
    assert real.stub.calls[-1][2] == {"ticket": 12}
    assert real.orders_get() == [] and real.history_deals_get(datetime.now(UTC),
                                                              datetime.now(UTC)) == []
    assert real.order_calc_margin(0, "XAUUSDm", 0.01, 4000.0) == 8.0
    assert real.version() == (500, 5120, "01 Jan 2026") and real.last_error() == (1, "Success")


def test_rates_are_bid_based_and_ask_adds_spread(real):
    real.symbol_info("XAUUSDm")  # caches point
    bars = real.copy_rates_from("XAUUSDm", K.TIMEFRAME_M1, datetime(2023, 11, 14, tzinfo=UTC), 2)
    assert len(bars) == 2 and bars[0].seconds == 60 and bars[0].ticks == 120
    b = bars[0]
    assert (b.bid_open, b.bid_high, b.bid_low, b.bid_close) == (4000.0, 4001.0, 3999.0, 4000.5)
    assert b.ask_close == pytest.approx(4000.5 + 20 * 0.01)
    assert b.ask_high == pytest.approx(4001.0 + 0.20)
    ticks = real.copy_ticks_range("XAUUSDm", datetime(2023, 11, 1, tzinfo=UTC),
                                  datetime(2023, 12, 1, tzinfo=UTC), K.COPY_TICKS_ALL)
    assert ticks[0].bid == 4000.0 and ticks[0].time_msc == 1_700_000_000_123
    real.stub.responses["copy_rates_from"] = None
    assert real.copy_rates_from("XAUUSDm", K.TIMEFRAME_M1, datetime.now(UTC), 1) is None


def test_datetimes_are_passed_as_aware_utc(real):
    real.symbol_info("XAUUSDm")
    local = datetime(2026, 1, 2, 12, 0, tzinfo=timezone(timedelta(hours=5)))
    real.copy_rates_range("XAUUSDm", K.TIMEFRAME_M1, local, datetime(2026, 1, 3))
    _, args, _ = real.stub.calls[-1]
    assert args[2] == datetime(2026, 1, 2, 7, 0, tzinfo=UTC) and args[2].tzinfo is UTC
    assert args[3].tzinfo is UTC and args[3].hour == 0  # naive input taken as UTC
    assert to_utc(datetime(2026, 1, 1)).tzinfo is UTC


def test_row_helpers_accept_mappings():
    bar = bar_from_rate({"time": 60, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
                         "tick_volume": 3, "spread": 10, "real_volume": 0}, 0.01, 60)
    assert bar.ask_open == pytest.approx(1.1) and bar.ticks == 3
    tick = tick_from_row(TickTuple(1, 1.0, 1.1, 0.0, 0, 1000, 6, 0.0))
    assert tick.spread == pytest.approx(0.1) and tick.time_msc == 1000


def test_missing_field_fails_closed(real):
    real.stub.responses["account_info"] = types.SimpleNamespace(_asdict=lambda: {"login": 1})
    with pytest.raises(MT5Error, match="fields missing"):
        real.account_info()

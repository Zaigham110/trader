"""On-account verification suite MT5-T00 … MT5-T16 (docs/research/02_exness_mt5.md §(a), D-028).

Each `tNN_*` function takes an `MT5Client` and returns a `TestResult`; `run_suite` runs them in a
safe order and `render_markdown` produces the table pasted into docs/MT5_EXNESS_INTEGRATION.md §8.

Safety rules (the suite is the one place outside the execution engine that sends orders, and only
as a diagnostic — R-003 still holds for the trading system itself):

* every order is 0.01 lot, except T07 (partial close) which needs 0.02 to have something to split;
* every position the suite opens is closed in the test's own `finally` and once more by
  `run_suite`'s `finally` (any position or pending order carrying `SUITE_MAGIC`);
* trading tests are refused (reported as `skip`) on a real account unless `allow_real=True`;
* nothing here reads or prints passwords.

The suite runs end to end against `FakeMT5` in CI; the same code runs on the owner's Windows PC
through `RealMT5` (`goldctl mt5-tests`).
"""
from __future__ import annotations

import statistics
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from goldtrader.mt5 import constants as K
from goldtrader.mt5.protocol import MT5Client, MT5Error
from goldtrader.mt5.spec import derive_money, find_gold_symbol, read_spec
from goldtrader.types import OrderRequest, OrderResult, Position, SymbolSpec

Status = Literal["pass", "fail", "skip", "info"]

SUITE_MAGIC = 777001
SUITE_COMMENT = "mt5-tests"
LOT = 0.01


@dataclass
class TestResult:
    __test__ = False  # not a pytest test class

    id: str
    name: str
    status: Status
    details: dict = field(default_factory=dict)
    notes: str = ""


# --------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------


def _filling_for(spec: SymbolSpec) -> int:
    """First filling mode the symbol advertises (KU-012): FOK, else IOC, else BOC."""
    for mode, flag in ((K.ORDER_FILLING_FOK, K.SYMBOL_FILLING_FOK),
                       (K.ORDER_FILLING_IOC, K.SYMBOL_FILLING_IOC),
                       (K.ORDER_FILLING_BOC, K.SYMBOL_FILLING_BOC)):
        if spec.filling_mode & flag:
            return mode
    return K.ORDER_FILLING_FOK


def _market_request(client: MT5Client, symbol: str, otype: int, volume: float = LOT, *,
                    sl: float = 0.0, tp: float = 0.0, filling: int | None = None,
                    position: int = 0, comment: str = SUITE_COMMENT) -> OrderRequest:
    spec = client.symbol_info(symbol)
    tick = client.symbol_info_tick(symbol)
    if spec is None or tick is None:
        raise MT5Error(f"no spec/tick for {symbol}: {client.last_error()}")
    price = tick.ask if otype == K.ORDER_TYPE_BUY else tick.bid
    return OrderRequest(action=K.TRADE_ACTION_DEAL, symbol=symbol, volume=volume, type=otype,
                        price=price, sl=sl, tp=tp, deviation=20, magic=SUITE_MAGIC,
                        position=position, type_filling=filling if filling is not None
                        else _filling_for(spec), type_time=K.ORDER_TIME_GTC, comment=comment)


def _send(client: MT5Client, request: OrderRequest) -> OrderResult:
    res = client.order_send(request)
    if res is None:
        raise MT5Error(f"order_send returned None: {client.last_error()}")
    return res


def _open(client: MT5Client, symbol: str, otype: int, volume: float = LOT, **kw) -> OrderResult:
    return _send(client, _market_request(client, symbol, otype, volume, **kw))


def _position_of(client: MT5Client, result: OrderResult, symbol: str) -> Position | None:
    """Find the position a DEAL opened (by `result.order` ticket, else the newest suite position)."""
    if result.order:
        found = client.positions_get(ticket=result.order)
        if found:
            return found[0]
    ours = [p for p in client.positions_get(symbol=symbol) if p.magic == SUITE_MAGIC]
    return max(ours, key=lambda p: p.time_msc) if ours else None


def _close(client: MT5Client, pos: Position, volume: float | None = None) -> OrderResult:
    otype = K.ORDER_TYPE_SELL if pos.type == K.POSITION_TYPE_BUY else K.ORDER_TYPE_BUY
    return _open(client, pos.symbol, otype, volume or pos.volume, position=pos.ticket)


def cleanup(client: MT5Client, symbol: str) -> dict:
    """Close every position and delete every pending order carrying `SUITE_MAGIC`."""
    closed, failed, removed = [], [], []
    for pos in client.positions_get(symbol=symbol):
        if pos.magic != SUITE_MAGIC:
            continue
        res = client.order_send(_market_request(
            client, symbol, K.ORDER_TYPE_SELL if pos.type == K.POSITION_TYPE_BUY
            else K.ORDER_TYPE_BUY, pos.volume, position=pos.ticket, comment="mt5-tests cleanup"))
        (closed if res and res.retcode in K.RETCODE_OK else failed).append(pos.ticket)
    for order in client.orders_get(symbol=symbol):
        if order.magic != SUITE_MAGIC:
            continue
        res = client.order_send(OrderRequest(action=K.TRADE_ACTION_REMOVE, symbol=symbol,
                                             volume=0.0, type=order.type, order=order.ticket))
        (removed if res and res.retcode in K.RETCODE_OK else failed).append(order.ticket)
    return {"closed": closed, "removed": removed, "failed": failed}


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, round(q * (len(s) - 1))))
    return s[idx]


def _mode_name(mode: int) -> str:
    return {0: "DISABLED", 1: "LONGONLY", 2: "SHORTONLY", 3: "CLOSEONLY", 4: "FULL"}.get(
        mode, str(mode))


# --------------------------------------------------------------------------------------------
# T00 – T03: identity, discovery, spec
# --------------------------------------------------------------------------------------------


def t00_constants(client: MT5Client | None = None) -> TestResult:
    """Compare every constant in `goldtrader.mt5.constants` with the MetaTrader5 package."""
    del client
    try:
        import MetaTrader5 as mt5  # type: ignore[import-not-found]
    except ImportError:
        return TestResult("MT5-T00", "constants vs package", "skip",
                          notes="MetaTrader5 package not importable (Linux); run on Windows")
    mismatches, missing, checked = {}, [], 0
    for name, value in vars(K).items():
        if not name.isupper() or not isinstance(value, int) or isinstance(value, bool):
            continue
        if not hasattr(mt5, name):
            missing.append(name)
            continue
        checked += 1
        if getattr(mt5, name) != value:
            mismatches[name] = (value, getattr(mt5, name))
    status: Status = "fail" if mismatches else "pass"
    return TestResult("MT5-T00", "constants vs package", status,
                      {"checked": checked, "mismatches": mismatches, "missing": missing},
                      "mismatch fails closed (D-028)" if mismatches else "")


def t01_connectivity(client: MT5Client) -> TestResult:
    version = client.version()
    term = client.terminal_info()
    acct = client.account_info()
    if term is None or acct is None:
        return TestResult("MT5-T01", "connectivity & identity", "fail",
                          {"last_error": client.last_error()}, "terminal_info/account_info None")
    details = {
        "version": version, "connected": term.connected, "trade_allowed": term.trade_allowed,
        "tradeapi_disabled": term.tradeapi_disabled, "build": term.build, "maxbars": term.maxbars,
        "ping_last": term.ping_last, "company": acct.company, "server": acct.server,
        "currency": acct.currency, "leverage": acct.leverage, "limit_orders": acct.limit_orders,
        "margin_so_mode": acct.margin_so_mode, "margin_so_call": acct.margin_so_call,
        "margin_so_so": acct.margin_so_so, "margin_mode": acct.margin_mode,
        "account_trade_mode": acct.trade_mode, "trade_expert": acct.trade_expert,
        "login": acct.login,
    }
    notes = []
    if acct.margin_mode != K.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING:
        notes.append("margin_mode is not RETAIL_HEDGING (expected 2)")
    if acct.margin_so_call not in (60.0, 30.0):
        notes.append(f"margin_so_call {acct.margin_so_call} (expected 60 or 30)")
    if acct.margin_so_so not in (0.0, 20.0):
        notes.append(f"margin_so_so {acct.margin_so_so} (expected 0 or 20)")
    ok = term.connected and term.trade_allowed and not term.tradeapi_disabled
    return TestResult("MT5-T01", "connectivity & identity", "pass" if ok else "fail", details,
                      "; ".join(notes))


def t02_symbol_discovery(client: MT5Client, symbol: str) -> TestResult:
    candidates = client.symbols_get(group="*XAU*")
    try:
        found = find_gold_symbol(client, preferred=symbol)
    except MT5Error as exc:
        return TestResult("MT5-T02", "symbol discovery", "fail",
                          {"candidates": candidates}, str(exc))
    selected = client.symbol_select(found, True)
    details = {"candidates": candidates, "found": found, "selected": selected,
               "has_247": any(c.startswith("XAUUSD247") for c in candidates)}
    if not selected:
        return TestResult("MT5-T02", "symbol discovery", "fail", details, "symbol_select failed")
    if found != symbol:
        return TestResult("MT5-T02", "symbol discovery", "info", details,
                          f"configured {symbol!r} not found; account has {found!r} (KU-019)")
    return TestResult("MT5-T02", "symbol discovery", "pass", details)


def t03_spec_dump(client: MT5Client, symbol: str) -> TestResult:
    spec = read_spec(client, symbol)
    details = {k: v for k, v in asdict(spec).items() if k != "CRITICAL"}
    problems = []
    if spec.trade_contract_size != 100:
        problems.append(f"trade_contract_size {spec.trade_contract_size} != 100")
    if spec.volume_min != 0.01:
        problems.append(f"volume_min {spec.volume_min} != 0.01")
    if spec.trade_exemode != K.SYMBOL_TRADE_EXECUTION_MARKET:
        problems.append(f"trade_exemode {spec.trade_exemode} != MARKET (2)")
    notes = list(problems)
    if spec.volume_max != 200:
        notes.append(f"volume_max {spec.volume_max} (published 200)")
    if spec.swap_rollover3days != 3:
        notes.append(f"swap_rollover3days {spec.swap_rollover3days} (expected 3 = Wednesday)")
    acct = client.account_info()
    if acct is not None:
        tick = client.symbol_info_tick(symbol)
        price = tick.ask if tick else 4000.0
        details["money_0.01_lot"] = derive_money(spec, price, LOT, acct.leverage)
    return TestResult("MT5-T03", "contract spec dump", "fail" if problems else "pass", details,
                      "; ".join(notes))


# --------------------------------------------------------------------------------------------
# T04 – T09: trading probes (0.01 lot, closed in finally)
# --------------------------------------------------------------------------------------------


def t04_filling_probe(client: MT5Client, symbol: str) -> TestResult:
    """Send 0.01 lot with FOK / IOC / RETURN; expect 10009 where the flag is set, else 10030."""
    spec = read_spec(client, symbol)
    expected = {
        "FOK": K.TRADE_RETCODE_DONE if spec.filling_mode & K.SYMBOL_FILLING_FOK
        else K.TRADE_RETCODE_INVALID_FILL,
        "IOC": K.TRADE_RETCODE_DONE if spec.filling_mode & K.SYMBOL_FILLING_IOC
        else K.TRADE_RETCODE_INVALID_FILL,
        "RETURN": K.TRADE_RETCODE_INVALID_FILL,  # not allowed on market execution (KU-012)
    }
    modes = {"FOK": K.ORDER_FILLING_FOK, "IOC": K.ORDER_FILLING_IOC,
             "RETURN": K.ORDER_FILLING_RETURN}
    observed: dict[str, int] = {}
    try:
        for name, mode in modes.items():
            res = _open(client, symbol, K.ORDER_TYPE_BUY, filling=mode)
            observed[name] = res.retcode
            if res.retcode in K.RETCODE_OK:
                pos = _position_of(client, res, symbol)
                if pos:
                    _close(client, pos)
    finally:
        cleanup(client, symbol)
    accepted = [n for n, c in observed.items() if c in K.RETCODE_OK]
    details = {"filling_mode_bitmask": spec.filling_mode, "expected": expected,
               "observed": observed, "accepted": accepted}
    mismatch = {n: (expected[n], observed.get(n)) for n in modes if expected[n] != observed.get(n)}
    if mismatch:
        return TestResult("MT5-T04", "filling-mode probe", "fail", details,
                          f"expectation mismatch {mismatch}; fix the mode in config (KU-012)")
    return TestResult("MT5-T04", "filling-mode probe", "pass", details,
                      f"use {accepted[0]}" if accepted else "no filling mode accepted")


def t05_margin_precheck(client: MT5Client, symbol: str) -> TestResult:
    """order_calc_margin(0.1 lot) vs volume×contract×ask/leverage, then order_check(0.01)."""
    spec = read_spec(client, symbol)
    tick = client.symbol_info_tick(symbol)
    acct = client.account_info()
    if tick is None or acct is None:
        return TestResult("MT5-T05", "margin pre-check", "fail", {}, "no tick / account")
    calc = client.order_calc_margin(K.ORDER_TYPE_BUY, symbol, 0.1, tick.ask)
    expected = 0.1 * spec.trade_contract_size * tick.ask / acct.leverage
    check = client.order_check(_market_request(client, symbol, K.ORDER_TYPE_BUY))
    details = {"ask": tick.ask, "leverage": acct.leverage, "calc_margin_0.1": calc,
               "expected_0.1": round(expected, 2),
               "check_retcode": None if check is None else check.retcode,
               "check_margin_total": None if check is None else check.margin,
               "check_margin_free": None if check is None else check.margin_free,
               "check_comment": "" if check is None else check.comment}
    if calc is None or check is None:
        return TestResult("MT5-T05", "margin pre-check", "fail", details,
                          f"order_calc_margin/order_check None: {client.last_error()}")
    ratio = calc / expected if expected else 0.0
    details["calc_over_expected"] = round(ratio, 3)
    if abs(ratio - 1.0) > 0.02:
        return TestResult("MT5-T05", "margin pre-check", "info", details,
                          "margin differs from account-leverage formula: HMR or dynamic leverage"
                          " in effect (research §4); repeat 10 min before a high-impact release")
    if check.retcode not in (0, K.TRADE_RETCODE_DONE):
        return TestResult("MT5-T05", "margin pre-check", "fail", details,
                          f"order_check retcode {check.retcode} {K.retcode_name(check.retcode)}")
    return TestResult("MT5-T05", "margin pre-check", "pass", details)


def t06_stops_probe(client: MT5Client, symbol: str) -> TestResult:
    """Valid SL/TP → 10009; SL at the bid (inside the spread) → 10016."""
    spec = read_spec(client, symbol)
    tick = client.symbol_info_tick(symbol)
    if tick is None:
        return TestResult("MT5-T06", "stops rule probe", "fail", {}, "no tick")
    gap = max(spec.trade_stops_level, 2) * spec.point  # engine rule, integration doc §4
    details: dict = {"trade_stops_level": spec.trade_stops_level, "spread": tick.spread,
                     "gap_used": gap}
    try:
        valid = _open(client, symbol, K.ORDER_TYPE_BUY,
                      sl=round(tick.bid - gap, spec.digits), tp=round(tick.bid + gap, spec.digits))
        details["valid_retcode"] = valid.retcode
        pos = _position_of(client, valid, symbol) if valid.retcode in K.RETCODE_OK else None
        if pos:
            details["valid_sl"], details["valid_tp"] = pos.sl, pos.tp
            _close(client, pos)
        tick2 = client.symbol_info_tick(symbol) or tick
        inside = _open(client, symbol, K.ORDER_TYPE_BUY, sl=round(tick2.bid, spec.digits))
        details["inside_spread_retcode"] = inside.retcode
        details["inside_spread_comment"] = inside.comment
        if inside.retcode in K.RETCODE_OK:
            pos = _position_of(client, inside, symbol)
            if pos:
                _close(client, pos)
    finally:
        cleanup(client, symbol)
    ok = (details.get("valid_retcode") in K.RETCODE_OK
          and details.get("inside_spread_retcode") == K.TRADE_RETCODE_INVALID_STOPS)
    notes = "" if ok else "expected valid→10009 and SL at bid→10016"
    return TestResult("MT5-T06", "stops rule probe", "pass" if ok else "fail", details, notes)


def t07_partial_close(client: MT5Client, symbol: str, volume: float = 0.02) -> TestResult:
    """Open `volume`, close half → 10009 and remaining half; over-close → 10038. Needs 0.02 lot."""
    spec = read_spec(client, symbol)
    if volume < 2 * spec.volume_min:
        return TestResult("MT5-T07", "partial close", "skip", {},
                          f"volume {volume} cannot be split (volume_min {spec.volume_min})")
    half = round(volume / 2, 8)
    details: dict = {"opened": volume, "partial": half}
    try:
        opened = _open(client, symbol, K.ORDER_TYPE_BUY, volume)
        details["open_retcode"] = opened.retcode
        if opened.retcode not in K.RETCODE_OK:
            return TestResult("MT5-T07", "partial close", "fail", details, opened.comment)
        pos = _position_of(client, opened, symbol)
        if pos is None:
            return TestResult("MT5-T07", "partial close", "fail", details, "position not found")
        part = _close(client, pos, half)
        details["partial_retcode"] = part.retcode
        after = client.positions_get(ticket=pos.ticket)
        details["remaining_volume"] = after[0].volume if after else 0.0
        over = _close(client, pos, round(volume * 2.5, 8))
        details["overclose_retcode"] = over.retcode
        still = client.positions_get(ticket=pos.ticket)
        if still:
            _close(client, still[0])
    finally:
        cleanup(client, symbol)
    ok = (details.get("partial_retcode") in K.RETCODE_OK
          and abs(details.get("remaining_volume", 0.0) - half) < 1e-9
          and details.get("overclose_retcode") == K.TRADE_RETCODE_INVALID_CLOSE_VOLUME)
    return TestResult("MT5-T07", "partial close", "pass" if ok else "fail", details,
                      "" if ok else "expected 10009 + half remaining, then 10038")


def t08_hedge_closeby(client: MT5Client, symbol: str) -> TestResult:
    """0.01 buy + 0.01 sell coexist (hedging) and TRADE_ACTION_CLOSE_BY closes both."""
    details: dict = {}
    try:
        buy = _open(client, symbol, K.ORDER_TYPE_BUY)
        sell = _open(client, symbol, K.ORDER_TYPE_SELL)
        details["buy_retcode"], details["sell_retcode"] = buy.retcode, sell.retcode
        ours = [p for p in client.positions_get(symbol=symbol) if p.magic == SUITE_MAGIC]
        details["open_positions"] = len(ours)
        buys = [p for p in ours if p.type == K.POSITION_TYPE_BUY]
        sells = [p for p in ours if p.type == K.POSITION_TYPE_SELL]
        if not buys or not sells:
            return TestResult("MT5-T08", "hedge + close-by", "fail", details,
                              "opposite positions did not coexist")
        res = _send(client, OrderRequest(action=K.TRADE_ACTION_CLOSE_BY, symbol=symbol,
                                         volume=LOT, type=K.ORDER_TYPE_CLOSE_BY,
                                         position=buys[0].ticket, position_by=sells[0].ticket,
                                         magic=SUITE_MAGIC, comment=SUITE_COMMENT))
        details["closeby_retcode"], details["closeby_comment"] = res.retcode, res.comment
        left = [p for p in client.positions_get(symbol=symbol) if p.magic == SUITE_MAGIC]
        details["positions_after"] = len(left)
    finally:
        cleanup(client, symbol)
    if details.get("closeby_retcode") == K.TRADE_RETCODE_HEDGE_PROHIBITED:
        return TestResult("MT5-T08", "hedge + close-by", "fail", details,
                          "10046: hedging prohibited — contradicts the Exness hedging article")
    ok = details.get("closeby_retcode") in K.RETCODE_OK and details.get("positions_after") == 0
    return TestResult("MT5-T08", "hedge + close-by", "pass" if ok else "fail", details)


def t09_pending_limits(client: MT5Client, symbol: str) -> TestResult:
    """Place a buy stop above the ask with a TP above it; expect 10008 (or an [SL violated]-style
    rejection under gap conditions); record account_info().limit_orders; remove the order."""
    spec = read_spec(client, symbol)
    tick = client.symbol_info_tick(symbol)
    acct = client.account_info()
    if tick is None or acct is None:
        return TestResult("MT5-T09", "pending limit & SL-violated", "fail", {}, "no tick")
    gap = max(spec.trade_stops_level, 2) * spec.point
    price = round(tick.ask + 2 * gap, spec.digits)
    details: dict = {"limit_orders": acct.limit_orders, "ask": tick.ask, "buy_stop": price,
                     "tp": round(price + 2 * gap, spec.digits)}
    try:
        res = _send(client, OrderRequest(action=K.TRADE_ACTION_PENDING, symbol=symbol, volume=LOT,
                                         type=K.ORDER_TYPE_BUY_STOP, price=price,
                                         tp=details["tp"], magic=SUITE_MAGIC,
                                         type_filling=_filling_for(spec),
                                         type_time=K.ORDER_TIME_GTC, comment=SUITE_COMMENT))
        details["place_retcode"], details["place_comment"] = res.retcode, res.comment
        pending = [o for o in client.orders_get(symbol=symbol) if o.magic == SUITE_MAGIC]
        details["pending_count"] = len(pending)
        if pending:
            rem = _send(client, OrderRequest(action=K.TRADE_ACTION_REMOVE, symbol=symbol,
                                             volume=0.0, type=K.ORDER_TYPE_BUY_STOP,
                                             order=pending[0].ticket))
            details["remove_retcode"] = rem.retcode
    finally:
        cleanup(client, symbol)
    ok = details.get("place_retcode") == K.TRADE_RETCODE_PLACED and details.get("pending_count") == 1
    notes = "" if ok else "expected 10008 and one pending order; see place_comment for [SL violated]"
    if acct.limit_orders not in (0, 1000):
        notes = (notes + "; " if notes else "") + f"limit_orders {acct.limit_orders} (expect 1000)"
    return TestResult("MT5-T09", "pending limit & SL-violated", "pass" if ok else "fail",
                      details, notes)


# --------------------------------------------------------------------------------------------
# T10 – T16
# --------------------------------------------------------------------------------------------


def t10_slippage_log(client: MT5Client, symbol: str, n: int = 20) -> TestResult:
    """n × (0.01 buy at ask, close at bid): fill − request price in points, per leg."""
    spec = read_spec(client, symbol)
    entry_pts: list[float] = []
    exit_pts: list[float] = []
    failures: list[int] = []
    try:
        for _ in range(n):
            tick = client.symbol_info_tick(symbol)
            if tick is None:
                break
            res = _open(client, symbol, K.ORDER_TYPE_BUY)
            if res.retcode not in K.RETCODE_OK:
                failures.append(res.retcode)
                continue
            entry_pts.append(round((res.price - tick.ask) / spec.point, 2))
            pos = _position_of(client, res, symbol)
            if pos is None:
                continue
            tick2 = client.symbol_info_tick(symbol) or tick
            out = _close(client, pos)
            if out.retcode in K.RETCODE_OK:
                exit_pts.append(round((tick2.bid - out.price) / spec.point, 2))  # + = against us
            else:
                failures.append(out.retcode)
    finally:
        cleanup(client, symbol)

    def stats(xs: list[float]) -> dict:
        if not xs:
            return {"n": 0}
        return {"n": len(xs), "mean": round(statistics.fmean(xs), 2), "p50": _pct(xs, 0.5),
                "p95": _pct(xs, 0.95), "max": max(xs)}

    details = {"entry_slippage_points": stats(entry_pts), "exit_slippage_points": stats(exit_pts),
               "failures": failures, "requested": n}
    status: Status = "pass" if not failures and len(entry_pts) == n else "fail"
    return TestResult("MT5-T10", "slippage log", status, details,
                      "demo slippage is a lower bound; repeat on live (Assumption)")


def t11_swap_booking(client: MT5Client, symbol: str) -> TestResult:
    """Informational: reads the swap fields. Booking needs an overnight hold (not automated)."""
    spec = read_spec(client, symbol)
    details = {"swap_mode": spec.swap_mode, "swap_long": spec.swap_long,
               "swap_short": spec.swap_short, "swap_rollover3days": spec.swap_rollover3days,
               "swap_free_hint": spec.swap_long == 0 and spec.swap_short == 0}
    return TestResult("MT5-T11", "swap booking", "info", details,
                      "hold 0.01 long + short across 22:00 UTC (winter) on a Tue and a Wed and"
                      " read positions_get().swap; intraday system, so informational only")


def t12_commission_booking(client: MT5Client, symbol: str,
                           expected_per_lot_per_side: float | None = None) -> TestResult:
    """Round trip 0.01 lot, sum `commission` over the IN and OUT deals of the position."""
    details: dict = {}
    try:
        res = _open(client, symbol, K.ORDER_TYPE_BUY)
        details["open_retcode"] = res.retcode
        if res.retcode not in K.RETCODE_OK:
            return TestResult("MT5-T12", "commission booking", "fail", details, res.comment)
        pos = _position_of(client, res, symbol)
        if pos is None:
            return TestResult("MT5-T12", "commission booking", "fail", details, "no position")
        out = _close(client, pos)
        details["close_retcode"] = out.retcode
        now = datetime.now(UTC)
        deals = client.history_deals_get(now - timedelta(days=1), now + timedelta(days=1),
                                         position=pos.identifier or pos.ticket)
    finally:
        cleanup(client, symbol)
    details["deals"] = len(deals)
    details["entries"] = [d.entry for d in deals]
    details["commission_sum"] = round(sum(d.commission for d in deals), 2)
    details["profit_sum"] = round(sum(d.profit for d in deals), 2)
    if expected_per_lot_per_side is not None:
        details["expected_sum"] = round(-expected_per_lot_per_side * LOT * 2, 2)
    ok = len(deals) >= 2 and K.DEAL_ENTRY_IN in details["entries"] and (
        K.DEAL_ENTRY_OUT in details["entries"] or K.DEAL_ENTRY_OUT_BY in details["entries"])
    if ok and expected_per_lot_per_side is not None:
        ok = abs(details["commission_sum"] - details["expected_sum"]) < 0.005
    return TestResult("MT5-T12", "commission booking", "pass" if ok else "fail", details,
                      "Standard: expect 0; Raw 3.5 / Zero 5.5 USD per lot per side")


def t13_session_poll(client: MT5Client, symbol: str) -> TestResult:
    """Short version: read trade_mode now. The long poll (21:55–23:05 UTC every 10 s) is manual."""
    spec = read_spec(client, symbol)
    tick = client.symbol_info_tick(symbol)
    details = {"trade_mode": spec.trade_mode, "trade_mode_name": _mode_name(spec.trade_mode),
               "last_tick_utc": None if tick is None else
               datetime.fromtimestamp(tick.time_msc / 1000, UTC).isoformat()}
    return TestResult("MT5-T13", "session / hours", "info", details,
                      "poll symbol_info().trade_mode every 10 s across 21:55–23:05 UTC and record"
                      " FULL → CLOSEONLY/DISABLED → FULL against the published winter break")


def t14_history_depth(client: MT5Client, symbol: str, now: datetime | None = None,
                      months_back: int = 36) -> TestResult:
    """Earliest bar per timeframe and the earliest month with ticks (stepping back monthly)."""
    now = now or datetime.now(UTC)
    earliest: dict[str, str | None] = {}
    counts: dict[str, int] = {}
    for name, tf in (("M1", K.TIMEFRAME_M1), ("M5", K.TIMEFRAME_M5), ("M15", K.TIMEFRAME_M15),
                     ("H1", K.TIMEFRAME_H1), ("D1", K.TIMEFRAME_D1)):
        bars = client.copy_rates_from(symbol, tf, now, 5_000_000)
        counts[name] = 0 if not bars else len(bars)
        earliest[name] = None if not bars else datetime.fromtimestamp(bars[0].time, UTC).isoformat()
    tick_month: str | None = None
    end = now
    for _ in range(months_back):
        start = end - timedelta(days=30)
        ticks = client.copy_ticks_range(symbol, start, end, K.COPY_TICKS_ALL)
        if not ticks:
            break
        tick_month = datetime.fromtimestamp(ticks[0].time_msc / 1000, UTC).isoformat()
        end = start
    details = {"earliest_bar": earliest, "bar_counts": counts, "earliest_tick": tick_month,
               "last_error": client.last_error()}
    status: Status = "pass" if counts["M1"] > 0 else "fail"
    return TestResult("MT5-T14", "history depth", status, details,
                      "compare M1 count for one day with a PA tick-history download (KU-003)")


def t15_failure_drills(client: MT5Client, symbol: str, burst: int = 20) -> TestResult:
    """Automatable drills: investor-password detection (trade_allowed) and an order_check burst.

    AutoTrading off and network disconnect need manual action on the terminal and are listed in
    the notes for the owner.
    """
    acct = client.account_info()
    term = client.terminal_info()
    details: dict = {"account_trade_allowed": None if acct is None else acct.trade_allowed,
                     "account_trade_expert": None if acct is None else acct.trade_expert,
                     "terminal_trade_allowed": None if term is None else term.trade_allowed,
                     "terminal_connected": None if term is None else term.connected}
    req = _market_request(client, symbol, K.ORDER_TYPE_BUY)
    codes: list[int] = []
    t0 = time.perf_counter()
    for _ in range(burst):
        chk = client.order_check(req)
        codes.append(-1 if chk is None else chk.retcode)
    details["burst_seconds"] = round(time.perf_counter() - t0, 3)
    details["burst_retcodes"] = sorted(set(codes))
    details["burst_10024_count"] = codes.count(K.TRADE_RETCODE_TOO_MANY_REQUESTS)
    if acct is not None and not acct.trade_allowed:
        details["investor_password_detected"] = True
        expected_17 = codes and all(c == K.TRADE_RETCODE_TRADE_DISABLED for c in codes)
        return TestResult("MT5-T15", "failure drills", "pass" if expected_17 else "fail", details,
                          "trade_allowed=False (investor password or account restriction);"
                          " order_check should return 10017")
    ok = acct is not None and term is not None and term.connected
    return TestResult("MT5-T15", "failure drills", "pass" if ok else "fail", details,
                      "manual: toggle AutoTrading off → trade_allowed False, 10027, last_error −8;"
                      " unplug network → connected False, 10031")


def t16_latency(client: MT5Client, symbol: str, n: int = 20) -> TestResult:
    """Round-trip time of n × (open 0.01, close) order_send calls; p50/p95 in ms vs ping_last."""
    samples_ms: list[float] = []
    failures: list[int] = []
    try:
        for _ in range(n):
            t0 = time.perf_counter()
            res = _open(client, symbol, K.ORDER_TYPE_BUY)
            samples_ms.append((time.perf_counter() - t0) * 1000)
            if res.retcode not in K.RETCODE_OK:
                failures.append(res.retcode)
                continue
            pos = _position_of(client, res, symbol)
            if pos is None:
                continue
            t0 = time.perf_counter()
            out = _close(client, pos)
            samples_ms.append((time.perf_counter() - t0) * 1000)
            if out.retcode not in K.RETCODE_OK:
                failures.append(out.retcode)
    finally:
        cleanup(client, symbol)
    term = client.terminal_info()
    details = {"n_sends": len(samples_ms), "p50_ms": round(_pct(samples_ms, 0.5), 3),
               "p95_ms": round(_pct(samples_ms, 0.95), 3),
               "max_ms": round(max(samples_ms), 3) if samples_ms else 0.0,
               "ping_last": None if term is None else term.ping_last, "failures": failures}
    status: Status = "pass" if not failures and samples_ms else "fail"
    return TestResult("MT5-T16", "latency", status, details,
                      "ping_last units are not documented on the Python page (likely µs)")


# --------------------------------------------------------------------------------------------
# suite
# --------------------------------------------------------------------------------------------

READ_ONLY_TESTS: tuple[str, ...] = ("T00", "T01", "T02", "T03", "T05", "T11", "T13", "T14", "T15")
TRADING_TESTS: tuple[str, ...] = ("T04", "T06", "T07", "T08", "T09", "T10", "T12", "T16")


def run_suite(client: MT5Client, symbol: str, allow_trading: bool, allow_real: bool = False, *,
              n_slippage: int = 20, n_latency: int = 20, now: datetime | None = None,
              commission_per_lot_per_side: float | None = None) -> list[TestResult]:
    """Run MT5-T00 … T16 and return their results.

    Trading tests run only when `allow_trading` is True **and** the account is not a real account
    (`account_info().trade_mode == ACCOUNT_TRADE_MODE_REAL`) unless `allow_real=True`; refused
    tests are reported as `skip` with the reason. Every position the suite opens is closed in each
    test's `finally` and again in this function's `finally` (`cleanup`).
    """
    results: list[TestResult] = []
    acct = client.account_info()
    is_real = acct is not None and acct.trade_mode == K.ACCOUNT_TRADE_MODE_REAL
    refused = allow_trading and is_real and not allow_real
    trade = allow_trading and not refused

    def guard(fn: Callable[[], TestResult], tid: str, name: str) -> None:
        try:
            results.append(fn())
        except Exception as exc:  # noqa: BLE001 - a broken test must not stop the suite/cleanup
            results.append(TestResult(f"MT5-{tid}", name, "fail", {"exception": repr(exc)},
                                      "unexpected exception"))

    def skipped(tid: str, name: str) -> TestResult:
        reason = ("REFUSED: real account (ACCOUNT_TRADE_MODE_REAL) and allow_real=False"
                  if refused else "trading tests disabled (allow_trading=False)")
        return TestResult(f"MT5-{tid}", name, "skip", {}, reason)

    try:
        guard(lambda: t00_constants(client), "T00", "constants vs package")
        guard(lambda: t01_connectivity(client), "T01", "connectivity & identity")
        guard(lambda: t02_symbol_discovery(client, symbol), "T02", "symbol discovery")
        guard(lambda: t03_spec_dump(client, symbol), "T03", "contract spec dump")
        trading: list[tuple[str, str, Callable[[], TestResult]]] = [
            ("T04", "filling-mode probe", lambda: t04_filling_probe(client, symbol)),
        ]
        guard(lambda: t05_margin_precheck(client, symbol), "T05", "margin pre-check")
        trading += [
            ("T06", "stops rule probe", lambda: t06_stops_probe(client, symbol)),
            ("T07", "partial close", lambda: t07_partial_close(client, symbol)),
            ("T08", "hedge + close-by", lambda: t08_hedge_closeby(client, symbol)),
            ("T09", "pending limit & SL-violated", lambda: t09_pending_limits(client, symbol)),
            ("T10", "slippage log", lambda: t10_slippage_log(client, symbol, n_slippage)),
        ]
        guard(lambda: t11_swap_booking(client, symbol), "T11", "swap booking")
        trading.append(("T12", "commission booking",
                        lambda: t12_commission_booking(client, symbol,
                                                       commission_per_lot_per_side)))
        guard(lambda: t13_session_poll(client, symbol), "T13", "session / hours")
        guard(lambda: t14_history_depth(client, symbol, now), "T14", "history depth")
        guard(lambda: t15_failure_drills(client, symbol), "T15", "failure drills")
        trading.append(("T16", "latency", lambda: t16_latency(client, symbol, n_latency)))
        for tid, name, fn in trading:
            if trade:
                guard(fn, tid, name)
            else:
                results.append(skipped(tid, name))
    finally:
        if allow_trading:
            cleanup(client, symbol)
    results.sort(key=lambda r: r.id)
    return results


def _fmt(value) -> str:
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {_fmt(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list | tuple):
        return "[" + ", ".join(_fmt(v) for v in value) + "]"
    return str(value)


def render_markdown(results: Iterable[TestResult], title: str = "On-account test results",
                    max_details: int = 160) -> str:
    """Markdown table for docs/MT5_EXNESS_INTEGRATION.md §8 (pipes in cells are escaped)."""
    results = list(results)
    lines = [f"### {title}", "", "| ID | Test | Status | Details | Notes |",
             "| --- | --- | --- | --- | --- |"]
    for r in results:
        det = "; ".join(f"{k}={_fmt(v)}" for k, v in r.details.items())
        if len(det) > max_details:
            det = det[: max_details - 1] + "…"
        cells = [r.id, r.name, r.status.upper(), det, r.notes]
        lines.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
    counts = {s: sum(1 for r in results if r.status == s) for s in ("pass", "fail", "skip", "info")}
    lines += ["", "Summary: " + ", ".join(f"{k} {v}" for k, v in counts.items()) + "."]
    return "\n".join(lines)


__all__ = [
    "LOT",
    "READ_ONLY_TESTS",
    "SUITE_MAGIC",
    "TRADING_TESTS",
    "TestResult",
    "cleanup",
    "render_markdown",
    "run_suite",
    "t00_constants",
    "t01_connectivity",
    "t02_symbol_discovery",
    "t03_spec_dump",
    "t04_filling_probe",
    "t05_margin_precheck",
    "t06_stops_probe",
    "t07_partial_close",
    "t08_hedge_closeby",
    "t09_pending_limits",
    "t10_slippage_log",
    "t11_swap_booking",
    "t12_commission_booking",
    "t13_session_poll",
    "t14_history_depth",
    "t15_failure_drills",
    "t16_latency",
]

"""FakeMT5 behaviour: fills, stops, closes, every documented retcode, account math, history."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from goldtrader.mt5 import constants as K
from goldtrader.mt5.fake import DEFAULT_SPEC, FakeMT5, bars_from_ticks, timeframe_seconds
from goldtrader.mt5.protocol import MT5Client, MT5OrderRejected, classify_retcode
from goldtrader.types import OrderRequest, Tick

BID, ASK = 4000.00, 4000.20


def deal(symbol="XAUUSDm", otype=K.ORDER_TYPE_BUY, volume=0.01, **kw) -> OrderRequest:
    kw.setdefault("type_filling", K.ORDER_FILLING_FOK)
    return OrderRequest(action=K.TRADE_ACTION_DEAL, symbol=symbol, volume=volume, type=otype, **kw)


@pytest.fixture
def fake() -> FakeMT5:
    f = FakeMT5()
    f.set_price(BID, ASK)
    return f


def open_pos(fake: FakeMT5, otype=K.ORDER_TYPE_BUY, volume=0.01, **kw):
    res = fake.order_send(deal(otype=otype, volume=volume, **kw))
    assert res is not None and res.retcode == K.TRADE_RETCODE_DONE, res
    return fake.positions_get(ticket=res.order)[0]


# ------------------------------------------------------------------------------ protocol


def test_fake_satisfies_protocol(fake):
    assert isinstance(fake, MT5Client)


def test_classify_retcode():
    assert classify_retcode(K.TRADE_RETCODE_DONE) == "ok"
    assert classify_retcode(K.TRADE_RETCODE_PLACED) == "ok"
    assert classify_retcode(K.TRADE_RETCODE_DONE_PARTIAL) == "ok"
    assert classify_retcode(K.TRADE_RETCODE_REQUOTE) == "transient"
    assert classify_retcode(K.TRADE_RETCODE_TOO_MANY_REQUESTS) == "transient"
    assert classify_retcode(K.TRADE_RETCODE_CONNECTION) == "transient"
    assert classify_retcode(K.TRADE_RETCODE_NO_MONEY) == "fatal"
    assert classify_retcode(K.TRADE_RETCODE_INVALID_STOPS) == "fatal"
    assert classify_retcode(99999) == "fatal"  # unknown fails closed
    err = MT5OrderRejected(K.TRADE_RETCODE_INVALID_STOPS, "Invalid stops")
    assert err.classification == "fatal" and "INVALID_STOPS" in str(err) and err.retcode == 10016


# ------------------------------------------------------------------------------ fills


def test_buy_fills_at_ask_and_sell_at_bid(fake):
    buy = fake.order_send(deal())
    sell = fake.order_send(deal(otype=K.ORDER_TYPE_SELL))
    assert buy.retcode == K.TRADE_RETCODE_DONE and buy.price == ASK
    assert sell.retcode == K.TRADE_RETCODE_DONE and sell.price == BID
    assert buy.bid == BID and buy.ask == ASK and buy.deal and buy.order
    assert len(fake.positions_get()) == 2  # hedging: opposite positions coexist
    assert len(fake.requests) == 2


def test_slippage_applies_against_the_trader(fake):
    fake.slippage_points = 3
    buy = fake.order_send(deal())
    sell = fake.order_send(deal(otype=K.ORDER_TYPE_SELL))
    assert buy.price == pytest.approx(ASK + 0.03)
    assert sell.price == pytest.approx(BID - 0.03)
    pos = fake.positions_get(ticket=buy.order)[0]
    close = fake.order_send(deal(otype=K.ORDER_TYPE_SELL, position=pos.ticket))
    assert close.price == pytest.approx(BID - 0.03)


def test_open_creates_position_and_in_deal(fake):
    pos = open_pos(fake, sl=3990.0, tp=4010.0, magic=42, comment="p1")
    assert pos.type == K.POSITION_TYPE_BUY and pos.volume == 0.01 and pos.price_open == ASK
    assert pos.sl == 3990.0 and pos.tp == 4010.0 and pos.magic == 42 and pos.comment == "p1"
    assert pos.identifier == pos.ticket and pos.side == "long"
    deals = fake.history_deals_get(datetime(2000, 1, 1, tzinfo=UTC), datetime(2100, 1, 1, tzinfo=UTC))
    assert len(deals) == 1 and deals[0].entry == K.DEAL_ENTRY_IN and deals[0].profit == 0.0
    assert deals[0].position_id == pos.ticket and deals[0].type == K.DEAL_TYPE_BUY


# ------------------------------------------------------------------------------ stops


def test_long_stop_loss_triggers_on_bid_and_books_out_deal(fake):
    pos = open_pos(fake, sl=3995.0, tp=4010.0)
    fake.set_price(3996.0, 3996.2)  # not yet
    assert fake.positions_get(ticket=pos.ticket)
    fake.set_price(3994.5, 3994.7)  # bid <= sl → closes at this tick's bid
    assert not fake.positions_get(ticket=pos.ticket)
    out = [d for d in fake.history_deals_get(None, None, position=pos.ticket)
           if d.entry == K.DEAL_ENTRY_OUT]
    assert len(out) == 1
    assert out[0].price == 3994.5 and out[0].type == K.DEAL_TYPE_SELL
    assert out[0].profit == pytest.approx((3994.5 - ASK) * 0.01 * 100)
    assert out[0].comment == "[sl]"
    assert fake.account_info().balance == pytest.approx(5000 + out[0].profit)


def test_long_take_profit_triggers_on_bid(fake):
    pos = open_pos(fake, tp=4005.0)
    fake.set_price(4004.9, 4005.1)  # ask above tp but bid below: no trigger
    assert fake.positions_get(ticket=pos.ticket)
    fake.set_price(4005.0, 4005.2)
    assert not fake.positions_get(ticket=pos.ticket)
    out = fake.history_deals_get(None, None, position=pos.ticket)[-1]
    assert out.entry == K.DEAL_ENTRY_OUT and out.price == 4005.0
    assert out.profit == pytest.approx((4005.0 - ASK) * 1.0)


def test_short_stop_and_take_profit_use_ask(fake):
    short = open_pos(fake, otype=K.ORDER_TYPE_SELL, sl=4003.0, tp=3990.0)
    fake.set_price(4002.7, 4002.9)  # ask < sl
    assert fake.positions_get(ticket=short.ticket)
    fake.set_price(4002.9, 4003.1)  # ask >= sl → close at ask
    assert not fake.positions_get(ticket=short.ticket)
    out = fake.history_deals_get(None, None, position=short.ticket)[-1]
    assert out.type == K.DEAL_TYPE_BUY and out.price == 4003.1
    assert out.profit == pytest.approx((BID - 4003.1) * 1.0)

    short2 = open_pos(fake, otype=K.ORDER_TYPE_SELL, tp=3990.0)
    fake.set_price(3989.5, 3989.9)
    assert not fake.positions_get(ticket=short2.ticket)
    out2 = fake.history_deals_get(None, None, position=short2.ticket)[-1]
    assert out2.price == 3989.9 and out2.profit == pytest.approx((4002.9 - 3989.9) * 1.0)


def test_sltp_modify(fake):
    pos = open_pos(fake)
    ok = fake.order_send(OrderRequest(action=K.TRADE_ACTION_SLTP, symbol="XAUUSDm", volume=0,
                                      type=0, position=pos.ticket, sl=3990.0, tp=4010.0))
    assert ok.retcode == K.TRADE_RETCODE_DONE
    p = fake.positions_get(ticket=pos.ticket)[0]
    assert (p.sl, p.tp) == (3990.0, 4010.0)
    same = fake.order_send(OrderRequest(action=K.TRADE_ACTION_SLTP, symbol="XAUUSDm", volume=0,
                                        type=0, position=pos.ticket, sl=3990.0, tp=4010.0))
    assert same.retcode == K.TRADE_RETCODE_NO_CHANGES
    bad = fake.order_send(OrderRequest(action=K.TRADE_ACTION_SLTP, symbol="XAUUSDm", volume=0,
                                       type=0, position=pos.ticket, sl=4000.10, tp=4010.0))
    assert bad.retcode == K.TRADE_RETCODE_INVALID_STOPS
    gone = fake.order_send(OrderRequest(action=K.TRADE_ACTION_SLTP, symbol="XAUUSDm", volume=0,
                                        type=0, position=999999, sl=3990.0))
    assert gone.retcode == K.TRADE_RETCODE_POSITION_CLOSED


# ------------------------------------------------------------------------------ closes


def test_partial_close_and_overclose(fake):
    pos = open_pos(fake, volume=0.02)
    part = fake.order_send(deal(otype=K.ORDER_TYPE_SELL, volume=0.01, position=pos.ticket))
    assert part.retcode == K.TRADE_RETCODE_DONE and part.volume == 0.01 and part.price == BID
    left = fake.positions_get(ticket=pos.ticket)[0]
    assert left.volume == pytest.approx(0.01)
    over = fake.order_send(deal(otype=K.ORDER_TYPE_SELL, volume=0.05, position=pos.ticket))
    assert over.retcode == K.TRADE_RETCODE_INVALID_CLOSE_VOLUME
    rest = fake.order_send(deal(otype=K.ORDER_TYPE_SELL, volume=0.01, position=pos.ticket))
    assert rest.retcode == K.TRADE_RETCODE_DONE
    assert not fake.positions_get(ticket=pos.ticket)
    entries = [d.entry for d in fake.history_deals_get(None, None, position=pos.ticket)]
    assert entries == [K.DEAL_ENTRY_IN, K.DEAL_ENTRY_OUT, K.DEAL_ENTRY_OUT]
    closed = fake.order_send(deal(otype=K.ORDER_TYPE_SELL, volume=0.01, position=pos.ticket))
    assert closed.retcode == K.TRADE_RETCODE_POSITION_CLOSED


def test_close_with_same_type_is_invalid(fake):
    pos = open_pos(fake)
    res = fake.order_send(deal(otype=K.ORDER_TYPE_BUY, position=pos.ticket))
    assert res.retcode == K.TRADE_RETCODE_INVALID


def test_close_by(fake):
    buy = open_pos(fake)                                   # at 4000.20
    fake.set_price(4001.00, 4001.20)
    sell = open_pos(fake, otype=K.ORDER_TYPE_SELL)         # at 4001.00
    res = fake.order_send(OrderRequest(action=K.TRADE_ACTION_CLOSE_BY, symbol="XAUUSDm",
                                       volume=0.01, type=K.ORDER_TYPE_CLOSE_BY,
                                       position=buy.ticket, position_by=sell.ticket))
    assert res.retcode == K.TRADE_RETCODE_DONE
    assert fake.positions_get() == []
    out_by = [d for d in fake.deals if d.entry == K.DEAL_ENTRY_OUT_BY]
    assert len(out_by) == 2
    assert sum(d.profit for d in out_by) == pytest.approx((4001.00 - 4000.20) * 1.0)
    assert fake.account_info().balance == pytest.approx(5000.80)


def test_close_by_rejected_on_netting_account(fake):
    fake.set_account(margin_mode=K.ACCOUNT_MARGIN_MODE_RETAIL_NETTING)
    a = open_pos(fake)
    b = open_pos(fake, otype=K.ORDER_TYPE_SELL)
    res = fake.order_send(OrderRequest(action=K.TRADE_ACTION_CLOSE_BY, symbol="XAUUSDm",
                                       volume=0.01, type=K.ORDER_TYPE_CLOSE_BY,
                                       position=a.ticket, position_by=b.ticket))
    assert res.retcode == K.TRADE_RETCODE_HEDGE_PROHIBITED


# ------------------------------------------------------------------------------ retcodes


@pytest.mark.parametrize("volume", [0.005, 0.015, 300.0, 0.0])
def test_invalid_volume(fake, volume):
    assert fake.order_send(deal(volume=volume)).retcode == K.TRADE_RETCODE_INVALID_VOLUME


@pytest.mark.parametrize("otype,sl,tp", [
    (K.ORDER_TYPE_BUY, 4000.10, 0.0),    # SL inside the spread
    (K.ORDER_TYPE_BUY, 4000.00, 0.0),    # SL at the bid
    (K.ORDER_TYPE_BUY, 4005.00, 0.0),    # SL on the wrong side
    (K.ORDER_TYPE_BUY, 0.0, 3999.00),    # TP on the wrong side
    (K.ORDER_TYPE_SELL, 4000.10, 0.0),   # short SL inside the spread
    (K.ORDER_TYPE_SELL, 3990.00, 0.0),   # short SL wrong side
    (K.ORDER_TYPE_SELL, 0.0, 4005.00),   # short TP wrong side
])
def test_invalid_stops(fake, otype, sl, tp):
    assert fake.order_send(deal(otype=otype, sl=sl, tp=tp)).retcode == K.TRADE_RETCODE_INVALID_STOPS


def test_limit_type_tp_may_sit_inside_the_spread(fake):
    assert fake.order_send(deal(tp=4000.10)).retcode == K.TRADE_RETCODE_DONE
    assert fake.order_send(deal(otype=K.ORDER_TYPE_SELL, tp=4000.10)).retcode == K.TRADE_RETCODE_DONE


def test_stops_level_distance_enforced(fake):
    fake.set_spec(replace(DEFAULT_SPEC, trade_stops_level=10))
    assert fake.order_send(deal(sl=3999.95)).retcode == K.TRADE_RETCODE_INVALID_STOPS
    assert fake.order_send(deal(sl=3999.90)).retcode == K.TRADE_RETCODE_DONE


def test_trade_mode_disabled_and_closeonly(fake):
    pos = open_pos(fake)
    fake.set_trade_mode(K.SYMBOL_TRADE_MODE_CLOSEONLY)
    assert fake.order_send(deal()).retcode == K.TRADE_RETCODE_MARKET_CLOSED
    assert fake.order_send(deal(otype=K.ORDER_TYPE_SELL, position=pos.ticket)).retcode \
        == K.TRADE_RETCODE_DONE
    pos2 = None
    fake.set_trade_mode(K.SYMBOL_TRADE_MODE_FULL)
    pos2 = open_pos(fake)
    fake.set_trade_mode(K.SYMBOL_TRADE_MODE_DISABLED)
    assert fake.order_send(deal()).retcode == K.TRADE_RETCODE_MARKET_CLOSED
    assert fake.order_send(deal(otype=K.ORDER_TYPE_SELL, position=pos2.ticket)).retcode \
        == K.TRADE_RETCODE_MARKET_CLOSED
    fake.set_trade_mode(K.SYMBOL_TRADE_MODE_LONGONLY)
    assert fake.order_send(deal(otype=K.ORDER_TYPE_SELL)).retcode == K.TRADE_RETCODE_LONG_ONLY


def test_account_trade_disabled(fake):
    fake.set_account(trade_allowed=False)
    assert fake.order_send(deal()).retcode == K.TRADE_RETCODE_TRADE_DISABLED
    assert fake.order_check(deal()).retcode == K.TRADE_RETCODE_TRADE_DISABLED


def test_autotrading_and_connection_toggles(fake):
    fake.autotrading_enabled = False
    assert fake.terminal_info().trade_allowed is False
    assert fake.order_send(deal()).retcode == K.TRADE_RETCODE_CLIENT_DISABLES_AT
    fake.autotrading_enabled = True
    fake.connected = False
    assert fake.terminal_info().connected is False
    assert fake.order_send(deal()).retcode == K.TRADE_RETCODE_CONNECTION
    assert fake.account_info() is not None  # cached account info still readable
    fake.connected = True
    assert fake.order_send(deal()).retcode == K.TRADE_RETCODE_DONE


def test_no_money(fake):
    fake.set_account(balance=1.0)
    res = fake.order_send(deal(volume=0.01))  # margin 8 USD > 1 USD
    assert res.retcode == K.TRADE_RETCODE_NO_MONEY
    chk = fake.order_check(deal(volume=0.01))
    assert chk.retcode == K.TRADE_RETCODE_NO_MONEY and chk.margin_free < 0


def test_invalid_fill(fake):
    assert fake.order_send(deal(type_filling=K.ORDER_FILLING_RETURN)).retcode \
        == K.TRADE_RETCODE_INVALID_FILL
    assert fake.order_send(deal(type_filling=K.ORDER_FILLING_IOC)).retcode == K.TRADE_RETCODE_DONE
    fake.set_spec(replace(DEFAULT_SPEC, filling_mode=K.SYMBOL_FILLING_FOK))
    assert fake.order_send(deal(type_filling=K.ORDER_FILLING_IOC)).retcode \
        == K.TRADE_RETCODE_INVALID_FILL
    assert fake.order_send(deal(type_filling=K.ORDER_FILLING_FOK)).retcode == K.TRADE_RETCODE_DONE


def test_position_limit(fake):
    fake.max_positions = 1
    open_pos(fake)
    assert fake.order_send(deal()).retcode == K.TRADE_RETCODE_LIMIT_POSITIONS


def test_rate_limit_uses_injected_clock():
    now = [100.0]
    fake = FakeMT5(clock=lambda: now[0])
    fake.set_price(BID, ASK)
    fake.rate_limit_after(2)
    assert fake.order_check(deal()).retcode == 0
    assert fake.order_check(deal()).retcode == 0
    assert fake.order_send(deal()).retcode == K.TRADE_RETCODE_TOO_MANY_REQUESTS
    now[0] += 1.5
    assert fake.order_send(deal()).retcode == K.TRADE_RETCODE_DONE


def test_reject_next_is_scripted_and_fifo(fake):
    fake.reject_next(K.TRADE_RETCODE_REQUOTE, "Requote")
    res = fake.order_send(deal())
    assert res.retcode == K.TRADE_RETCODE_REQUOTE and res.comment == "Requote"
    assert fake.positions_get() == []
    assert fake.order_send(deal()).retcode == K.TRADE_RETCODE_DONE


def test_no_tick_means_price_off():
    fake = FakeMT5()
    assert fake.order_send(deal()).retcode == K.TRADE_RETCODE_PRICE_OFF
    assert fake.symbol_info_tick("XAUUSDm") is None


def test_unknown_symbol_is_invalid(fake):
    assert fake.order_send(deal(symbol="EURUSDm")).retcode == K.TRADE_RETCODE_INVALID
    assert fake.symbol_info("NOPE") is None and fake.last_error()[0] == K.RES_E_NOT_FOUND


# ------------------------------------------------------------------------------ account math


def test_margin_equity_and_profit_follow_the_tick(fake):
    before = fake.account_info()
    assert before.equity == 5000.0 and before.margin == 0.0 and before.margin_free == 5000.0
    pos = open_pos(fake)
    acct = fake.account_info()
    margin = 0.01 * 100 * ASK / 500
    assert acct.margin == pytest.approx(margin, abs=0.01)
    assert acct.profit == pytest.approx((BID - ASK) * 1.0)            # -0.20
    assert acct.equity == pytest.approx(5000 + acct.profit)
    assert acct.margin_free == pytest.approx(acct.equity - acct.margin)
    assert acct.margin_level == pytest.approx(acct.equity / acct.margin * 100, rel=1e-3)
    fake.set_price(4010.0, 4010.2)
    acct2 = fake.account_info()
    assert acct2.profit == pytest.approx((4010.0 - ASK) * 1.0)
    assert fake.positions_get(ticket=pos.ticket)[0].profit == acct2.profit
    assert fake.positions_get(ticket=pos.ticket)[0].price_current == 4010.0
    assert fake.order_calc_margin(K.ORDER_TYPE_BUY, "XAUUSDm", 0.1, 4000.0) == pytest.approx(80.0)


def test_commission_is_booked_on_both_deals(fake):
    fake.commission_per_lot_per_side = 3.5
    pos = open_pos(fake, volume=0.1)
    fake.order_send(deal(otype=K.ORDER_TYPE_SELL, volume=0.1, position=pos.ticket))
    deals = fake.history_deals_get(None, None, position=pos.ticket)
    assert [d.commission for d in deals] == [-0.35, -0.35]
    assert fake.account_info().balance == pytest.approx(5000 - 0.70 + (BID - ASK) * 10)


def test_order_check_reports_margin_without_trading(fake):
    chk = fake.order_check(deal())
    assert chk.retcode == 0 and chk.margin == pytest.approx(8.0, abs=0.01)
    assert chk.margin_free == pytest.approx(5000 - 8.0, abs=0.01)
    assert fake.positions_get() == [] and fake.deals == []
    assert fake.order_check(deal(volume=0.003)).retcode == K.TRADE_RETCODE_INVALID_VOLUME


# ------------------------------------------------------------------------------ history


def test_history_deals_filter_by_time_and_position(fake):
    a = open_pos(fake)
    fake.set_price(BID, ASK, time_msc=1_700_000_120_000)
    b = open_pos(fake)
    assert [d.position_id for d in fake.history_deals_get(None, None, position=a.ticket)] \
        == [a.ticket]
    window = fake.history_deals_get(datetime.fromtimestamp(1_700_000_100, UTC),
                                    datetime.fromtimestamp(1_700_000_200, UTC))
    assert [d.position_id for d in window] == [b.ticket]


def test_copy_rates_builds_minute_bars_from_fed_ticks():
    fake = FakeMT5()
    t0 = 1_700_000_000_000 - (1_700_000_000_000 % 60_000)  # minute boundary
    fake.feed(Tick(t0, 4000.0, 4000.2))
    fake.feed(Tick(t0 + 10_000, 4001.0, 4001.3))
    fake.feed(Tick(t0 + 50_000, 3999.5, 3999.7))
    fake.feed(Tick(t0 + 60_000, 4002.0, 4002.2))
    fake.feed(Tick(t0 + 125_000, 4003.0, 4003.2))
    now = datetime.fromtimestamp((t0 + 125_000) / 1000, UTC)
    bars = fake.copy_rates_from("XAUUSDm", K.TIMEFRAME_M1, now, 100)
    assert [b.time for b in bars] == [t0 // 1000, t0 // 1000 + 60, t0 // 1000 + 120]
    first = bars[0]
    assert (first.bid_open, first.bid_high, first.bid_low, first.bid_close) == (
        4000.0, 4001.0, 3999.5, 3999.5)
    assert (first.ask_open, first.ask_high, first.ask_low, first.ask_close) == (
        4000.2, 4001.3, 3999.7, 3999.7)
    assert first.ticks == 3 and first.seconds == 60
    assert fake.copy_rates_from("XAUUSDm", K.TIMEFRAME_M1, now, 2) == bars[1:]
    minute2 = datetime.fromtimestamp(t0 / 1000 + 60, UTC)
    rng = fake.copy_rates_range("XAUUSDm", K.TIMEFRAME_M1, minute2, minute2)
    assert rng == [bars[1]]
    m5 = fake.copy_rates_from("XAUUSDm", K.TIMEFRAME_M5, now, 10)
    assert sum(b.ticks for b in m5) == 5 and all(b.seconds == 300 for b in m5)
    ticks = fake.copy_ticks_range("XAUUSDm", datetime.fromtimestamp(t0 / 1000, UTC),
                                  datetime.fromtimestamp(t0 / 1000 + 60, UTC), K.COPY_TICKS_ALL)
    assert len(ticks) == 4
    assert len(fake.copy_ticks_from("XAUUSDm", datetime.fromtimestamp(t0 / 1000 + 60, UTC), 1,
                                    K.COPY_TICKS_ALL)) == 1
    # no history → None + RES_E_NOT_FOUND, like the package
    assert fake.copy_rates_from("XAUUSDm", K.TIMEFRAME_M1, datetime(2000, 1, 1, tzinfo=UTC), 10) \
        is None
    assert fake.last_error()[0] == K.RES_E_NOT_FOUND


def test_timeframe_encoding_and_bar_builder():
    assert timeframe_seconds(K.TIMEFRAME_M1) == 60
    assert timeframe_seconds(K.TIMEFRAME_M15) == 900
    assert timeframe_seconds(K.TIMEFRAME_H1) == 3600
    assert timeframe_seconds(K.TIMEFRAME_D1) == 86400
    with pytest.raises(ValueError):
        timeframe_seconds(0)
    assert bars_from_ticks([], 60) == []


# ------------------------------------------------------------------------------ pending orders


def test_pending_buy_stop_places_triggers_and_can_be_removed(fake):
    place = fake.order_send(OrderRequest(action=K.TRADE_ACTION_PENDING, symbol="XAUUSDm",
                                         volume=0.01, type=K.ORDER_TYPE_BUY_STOP, price=4001.0,
                                         tp=4010.0, type_filling=K.ORDER_FILLING_FOK))
    assert place.retcode == K.TRADE_RETCODE_PLACED and len(fake.orders_get("XAUUSDm")) == 1
    bad = fake.order_send(OrderRequest(action=K.TRADE_ACTION_PENDING, symbol="XAUUSDm",
                                       volume=0.01, type=K.ORDER_TYPE_BUY_STOP, price=3999.0))
    assert bad.retcode == K.TRADE_RETCODE_INVALID_PRICE
    fake.set_price(4000.9, 4001.1)  # ask >= 4001.0, gap 0.1 < 3×spread → fills at requested price
    assert fake.orders_get() == []
    pos = fake.positions_get()
    assert len(pos) == 1 and pos[0].price_open == 4001.0 and pos[0].tp == 4010.0
    # removal path
    p2 = fake.order_send(OrderRequest(action=K.TRADE_ACTION_PENDING, symbol="XAUUSDm",
                                      volume=0.01, type=K.ORDER_TYPE_SELL_LIMIT, price=4005.0))
    rem = fake.order_send(OrderRequest(action=K.TRADE_ACTION_REMOVE, symbol="XAUUSDm", volume=0,
                                       type=K.ORDER_TYPE_SELL_LIMIT, order=p2.order))
    assert rem.retcode == K.TRADE_RETCODE_DONE and fake.orders_get() == []


def test_pending_sl_violated_on_gap_is_cancelled(fake):
    res = fake.order_send(OrderRequest(action=K.TRADE_ACTION_PENDING, symbol="XAUUSDm",
                                       volume=0.01, type=K.ORDER_TYPE_BUY_STOP, price=4001.0,
                                       tp=4001.5))
    assert res.retcode == K.TRADE_RETCODE_PLACED
    fake.set_price(4009.8, 4010.0)  # gap: fills at market 4010 which is beyond the TP
    assert fake.orders_get() == [] and fake.positions_get() == []
    assert fake.cancelled_orders == [(res.order, "[SL violated]")]


def test_pending_orders_cancelled_in_close_only(fake):
    fake.order_send(OrderRequest(action=K.TRADE_ACTION_PENDING, symbol="XAUUSDm", volume=0.01,
                                 type=K.ORDER_TYPE_SELL_LIMIT, price=4005.0))
    fake.set_trade_mode(K.SYMBOL_TRADE_MODE_CLOSEONLY)
    fake.set_price(BID, ASK)
    assert fake.orders_get() == []


# ------------------------------------------------------------------------------ misc


def test_symbols_get_group_mask_and_select(fake):
    assert fake.symbols_get("*XAU*") == ["XAUUSDm", "XAUUSD247m", "XAUEURm"]
    assert fake.symbols_get("*XAU*,!*247*") == ["XAUUSDm", "XAUEURm"]
    assert fake.symbol_select("XAUUSDm") is True and "XAUUSDm" in fake.selected
    assert fake.symbol_select("NOPE") is False


def test_initialize_shutdown_and_version(fake):
    assert fake.initialize(login=1, password="secret", server="Exness-MT5Trial") is True
    assert fake.version()[0] == 500
    fake.shutdown()
    assert fake.account_info() is None and fake.version() is None
    fake.init_ok = False
    assert fake.initialize() is False and fake.last_error()[0] == K.RES_E_INTERNAL_FAIL_INIT


def test_symbol_info_reports_live_spread(fake):
    assert fake.symbol_info("XAUUSDm").spread == 20
    fake.set_price(4000.0, 4000.35)
    assert fake.symbol_info("XAUUSDm").spread == 35

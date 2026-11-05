"""ExecutionEngine against FakeMT5 + an in-memory journal (R-003, R-004, R-007, R-054).

The harness (`Harness`, `mk_*`) is shared with test_execution_reconciler.py and
test_execution_intents.py.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from goldtrader.config.loader import parse_config
from goldtrader.db import connect, migrate
from goldtrader.execution import ExecutionEngine, Journal, Throttle, magic_from_bundle
from goldtrader.execution.engine import filling_for
from goldtrader.mt5 import constants as K
from goldtrader.mt5.fake import DEFAULT_SPEC, FakeMT5
from goldtrader.risk import AccountState, EntryRequest, MarketState, RejectCode, RiskContext
from goldtrader.types import OrderRequest, OrderResult, SessionState

BID, ASK = 4000.00, 4000.20
T0 = 1_700_000_000_000
STOP_LONG, TARGET_LONG = 3980.0, 4040.0
STOP_SHORT, TARGET_SHORT = 4020.2, 3960.0


class FakeClock:
    """Millisecond clock that only moves when told (or when the throttle sleeps)."""

    def __init__(self, start_ms: int = T0) -> None:
        self.ms = start_ms

    def __call__(self) -> int:
        return self.ms

    def sleep(self, seconds: float) -> None:
        self.ms += int(seconds * 1000)

    def advance(self, ms: int) -> None:
        self.ms += ms


def mk_cfg(**risk):
    return parse_config({"owner": {"profile": "normal", "risk": risk}})


def mk_acct(**over) -> AccountState:
    base = dict(
        equity=5000.0,
        balance=5000.0,
        margin_free=5000.0,
        margin_level=None,
        leverage=500,
        open_positions=0,
        day_pnl=0.0,
        week_pnl=0.0,
        equity_peak=5000.0,
        orders_last_minute=0,
        consecutive_losses=0,
        cooldown_until_ms=0,
        start_equity_day=5000.0,
        start_equity_week=5000.0,
        live_start_equity=5000.0,
    )
    base.update(over)
    return AccountState(**base)


def mk_market(**over) -> MarketState:
    base = dict(
        bid=BID,
        ask=ASK,
        point=0.01,
        digits=2,
        atr=40.0,
        spread_pips=20.0,
        spread_regime="normal",
        session=SessionState.OPEN,
        entries_allowed=True,
        tick_age_s=0.5,
        trade_mode=K.SYMBOL_TRADE_MODE_FULL,
        contract_size=100.0,
        volume_min=0.01,
        volume_max=200.0,
        volume_step=0.01,
        stops_level_points=0,
        freeze_level_points=0,
        margin_per_lot=800.04,
    )
    base.update(over)
    return MarketState(**base)


def mk_req(side="long", stop=None, target=None, volume=None, proposal="prop-1", bundle="bundle-1"):
    if stop is None:
        stop = STOP_LONG if side == "long" else STOP_SHORT
    if target is None:
        target = TARGET_LONG if side == "long" else TARGET_SHORT
    return EntryRequest(
        side=side,
        stop_price=stop,
        target_price=target,
        bundle_id=bundle,
        proposal_id=proposal,
        volume=volume,
    )


@dataclass
class Harness:
    fake: FakeMT5
    journal: Journal
    engine: ExecutionEngine
    clock: FakeClock
    throttle: Throttle
    cfg: object

    def ctx(self, **over) -> RiskContext:
        return RiskContext(now_ms=self.clock(), **over)

    def enter(self, req=None, acct=None, market=None, ctx=None, **kw):
        return self.engine.enter(
            req or mk_req(), acct or mk_acct(), market or mk_market(), ctx or self.ctx(), **kw
        )

    def sends(self, action: int | None = None) -> list[OrderRequest]:
        """Requests that reached order_send (order_check calls are excluded)."""
        checks = set(getattr(self.fake, "_check_idx", []))
        out = [r for i, r in enumerate(self.fake.requests) if i not in checks]
        return out if action is None else [r for r in out if r.action == action]


def mk_harness(fake: FakeMT5 | None = None, cfg=None, **throttle_kw) -> Harness:
    fake = fake or FakeMT5()
    if fake.tick is None:
        fake.set_price(BID, ASK, T0)
    # remember which requests were order_check calls so tests can count sends only
    fake._check_idx = []  # type: ignore[attr-defined]
    orig_check = fake.order_check

    def check(req):
        fake._check_idx.append(len(fake.requests))  # type: ignore[attr-defined]
        return orig_check(req)

    fake.order_check = check  # type: ignore[method-assign]
    cfg = cfg or mk_cfg()
    conn = connect(None)
    migrate(conn)
    clock = FakeClock()
    journal = Journal(conn, clock=clock, point=fake.spec.point, digits=2, contract_size=100.0)
    throttle = Throttle(clock=clock, sleep=clock.sleep, **throttle_kw)
    engine = ExecutionEngine(fake, fake.spec, cfg, journal, clock=clock, throttle=throttle)
    return Harness(fake, journal, engine, clock, throttle, cfg)


@pytest.fixture
def h() -> Harness:
    return mk_harness()


def deal_sum(fake: FakeMT5, position_id: int) -> float:
    """R-054 reconciliation target: what history_deals_get says the position made."""
    return round(
        sum(d.profit + d.commission + d.swap for d in fake.deals if d.position_id == position_id),
        2,
    )


# ============================================================================ identity helpers


def test_magic_is_stable_and_31_bit():
    a, b = magic_from_bundle("bundle-1"), magic_from_bundle("bundle-1")
    assert a == b and 0 < a < 2**31
    assert magic_from_bundle("bundle-2") != a


def test_filling_mode_choice():
    assert filling_for(DEFAULT_SPEC) == K.ORDER_FILLING_FOK
    assert (
        filling_for(replace(DEFAULT_SPEC, filling_mode=K.SYMBOL_FILLING_IOC)) == K.ORDER_FILLING_IOC
    )
    assert filling_for(replace(DEFAULT_SPEC, filling_mode=0)) == K.ORDER_FILLING_RETURN


# ============================================================================ happy entries


def test_long_entry_places_sl_tp_and_journals(h: Harness):
    res = h.enter()
    assert res.status == "executed", res
    assert res.position_id and res.filled_price == ASK and res.volume == pytest.approx(0.01)
    pos = h.fake.positions_get(ticket=res.position_id)[0]
    assert pos.type == K.POSITION_TYPE_BUY
    assert pos.sl == STOP_LONG and pos.tp == TARGET_LONG
    assert pos.magic == magic_from_bundle("bundle-1") and pos.comment == "prop-1"
    sent = h.sends(K.TRADE_ACTION_DEAL)
    assert len(sent) == 1
    r = sent[0]
    assert r.type == K.ORDER_TYPE_BUY and r.price == ASK and r.deviation == 10
    assert r.type_filling == K.ORDER_FILLING_FOK and r.type_time == K.ORDER_TIME_GTC
    # journal: orders, fills, positions, market snapshot
    o = h.journal.c.execute(
        "SELECT * FROM orders WHERE order_ticket=?", (res.order_ticket,)
    ).fetchone()
    assert o["requested_price"] == ASK and o["filled_price"] == ASK and o["slippage_points"] == 0
    assert o["bundle_id"] == "bundle-1" and o["kind"] == "entry"
    f = h.journal.fills_for(res.position_id)
    assert len(f) == 1 and f[0]["entry"] == K.DEAL_ENTRY_IN and f[0]["price"] == ASK
    assert f[0]["bid_at_fill"] == BID and f[0]["ask_at_fill"] == ASK
    p = h.journal.position(res.position_id)
    assert p["status"] == "open" and p["side"] == "long" and p["initial_stop"] == STOP_LONG
    assert p["proposal_id"] == "prop-1" and p["risk_pct"] == pytest.approx(0.404)
    snap = h.journal.c.execute("SELECT * FROM market_snapshots").fetchall()
    assert len(snap) == 1 and snap[0]["ref_id"] == str(res.position_id) and snap[0]["atr"] == 40.0


def test_short_entry(h: Harness):
    res = h.enter(mk_req("short"))
    assert res.status == "executed", res
    pos = h.fake.positions_get(ticket=res.position_id)[0]
    assert pos.type == K.POSITION_TYPE_SELL and res.filled_price == BID
    assert pos.sl == STOP_SHORT and pos.tp == TARGET_SHORT
    assert h.journal.position(res.position_id)["side"] == "short"


def test_slippage_is_recorded_against_requested_price():
    h = mk_harness(FakeMT5(slippage_points=5))
    res = h.enter()
    assert res.status == "executed" and res.filled_price == pytest.approx(4000.25)
    o = h.journal.c.execute(
        "SELECT * FROM orders WHERE order_ticket=?", (res.order_ticket,)
    ).fetchone()
    assert o["requested_price"] == ASK and o["slippage_points"] == pytest.approx(5.0)


# ============================================================================ rejections


@pytest.mark.parametrize(
    "ctx_over,market_over,code",
    [
        ({"kill_switch": True}, {}, RejectCode.KILL_SWITCH),
        ({}, {"session": SessionState.CLOSED}, RejectCode.SESSION_CLOSED),
        ({}, {"atr": None}, RejectCode.NO_ATR),
    ],
)
def test_risk_rejection_never_reaches_broker(h: Harness, ctx_over, market_over, code):
    res = h.enter(market=mk_market(**market_over), ctx=h.ctx(**ctx_over))
    assert res.status == "rejected" and res.risk_code == code.value
    assert h.fake.requests == []  # neither order_check nor order_send
    row = h.journal.c.execute("SELECT * FROM order_attempts").fetchone()
    assert row["stage"] == "risk" and row["risk_code"] == code.value


def test_stop_wrong_side_rejected(h: Harness):
    res = h.enter(mk_req(stop=4010.0))
    assert res.status == "rejected" and res.risk_code == RejectCode.STOP_WRONG_SIDE.value
    assert h.fake.requests == []


def test_margin_calc_none_rejects(h: Harness):
    h.fake.order_calc_margin = lambda *a, **k: None  # type: ignore[method-assign]
    res = h.enter()
    assert res.status == "rejected" and res.risk_code == RejectCode.MARGIN_INSUFFICIENT.value
    assert h.fake.requests == []


def test_margin_level_rule_uses_fresh_number(h: Harness):
    # order_calc_margin says 4000 USD for 0.01 lot → projected level 125% < 300% floor
    h.fake.order_calc_margin = lambda *a, **k: 4000.0  # type: ignore[method-assign]
    res = h.enter()
    assert res.status == "rejected" and res.risk_code == RejectCode.MARGIN_LEVEL.value


def test_order_check_failure_is_rejected_without_send(h: Harness):
    h.fake.set_account(trade_allowed=False)
    res = h.enter()
    assert res.status == "rejected" and "TRADE_DISABLED" in res.detail
    assert len(h.fake.requests) == 1 and h.sends() == []
    assert h.fake.positions_get() == []


# ============================================================================ retcodes


def test_fatal_retcode_fails_and_alerts(h: Harness):
    h.fake.reject_next(K.TRADE_RETCODE_NO_MONEY, "Not enough money")
    res = h.enter()
    assert res.status == "failed" and "NO_MONEY" in res.detail
    assert len(h.sends()) == 1  # no blind retry
    assert h.fake.positions_get() == []
    alerts = h.journal.alerts("order_rejected")
    assert len(alerts) == 1 and "10019" in alerts[0]["message"]
    attempts = h.journal.c.execute(
        "SELECT retcode FROM order_attempts WHERE stage='send'"
    ).fetchall()
    assert [a[0] for a in attempts] == [K.TRADE_RETCODE_NO_MONEY]


def test_invalid_stops_fatal_alerts(h: Harness):
    h.fake.reject_next(K.TRADE_RETCODE_INVALID_STOPS)
    res = h.enter()
    assert res.status == "failed" and h.journal.alerts("order_rejected")


def test_transient_requote_retried_once_then_filled(h: Harness):
    h.fake.reject_next(K.TRADE_RETCODE_REQUOTE, "Requote")
    h.fake.set_price(4000.10, 4000.30, T0 + 100)  # price moves before the retry
    res = h.enter()
    assert res.status == "executed", res
    sends = h.sends(K.TRADE_ACTION_DEAL)
    assert len(sends) == 2 and sends[1].price == 4000.30  # refreshed price on the retry
    assert res.filled_price == 4000.30
    assert 0.5 in h.throttle.waits  # transient pause
    assert h.journal.alerts("order_rejected") == []
    attempts = h.journal.c.execute(
        "SELECT attempt, retcode FROM order_attempts WHERE stage='send' ORDER BY id"
    ).fetchall()
    assert [tuple(a) for a in attempts] == [(1, 10004), (2, 10009)]


def test_transient_twice_fails(h: Harness):
    h.fake.reject_next(K.TRADE_RETCODE_REQUOTE)
    h.fake.reject_next(K.TRADE_RETCODE_REQUOTE)
    res = h.enter()
    assert res.status == "failed" and "REQUOTE" in res.detail
    assert len(h.sends()) == 2 and h.fake.positions_get() == []


def test_too_many_requests_backs_off_then_succeeds(h: Harness):
    h.fake.reject_next(K.TRADE_RETCODE_TOO_MANY_REQUESTS)
    res = h.enter()
    assert res.status == "executed"
    assert 1.0 in h.throttle.waits  # exponential backoff starts at 1 s
    assert h.throttle.rate_limit_hits == 0  # reset after the successful send


def test_backoff_sequence_is_exponential_and_capped():
    clock = FakeClock()
    t = Throttle(clock=clock, sleep=clock.sleep)
    waits = [t.backoff(10024) for _ in range(8)]
    assert waits == [1, 2, 4, 8, 16, 32, 60, 60]
    t.reset_backoff()
    assert t.backoff(10024) == 1


def test_throttle_token_bucket_paces_requests():
    clock = FakeClock()
    t = Throttle(clock=clock, sleep=clock.sleep, burst=2)
    assert t.acquire() == 0.0 and t.acquire() == 0.0
    assert t.acquire() == pytest.approx(1.0)  # third request waits for a token
    assert clock() == T0 + 1000
    clock.advance(5000)
    assert t.acquire() == 0.0


def test_partial_fill_journals_actual_volume():
    class PartialFake(FakeMT5):
        def order_send(self, request):
            if request.action == K.TRADE_ACTION_DEAL and not request.position:
                half = replace(request, volume=round(request.volume / 2, 2))
                res = super().order_send(half)
                self.requests[-1] = request  # what we asked for
                return replace(res, retcode=K.TRADE_RETCODE_DONE_PARTIAL)
            return super().order_send(request)

    h = mk_harness(PartialFake(), cfg=mk_cfg(risk_per_trade_pct=1.0, max_volume_lots=0.05))
    res = h.enter(mk_req(volume=0.02))
    assert res.status == "executed" and res.detail == "partial"
    assert res.volume == pytest.approx(0.01)
    pos = h.fake.positions_get(ticket=res.position_id)[0]
    assert pos.volume == pytest.approx(0.01) and pos.sl == STOP_LONG
    o = h.journal.c.execute("SELECT volume FROM orders WHERE order_ticket=?", (res.order_ticket,))
    assert o.fetchone()[0] == pytest.approx(0.01)
    assert h.journal.position(res.position_id)["volume"] == pytest.approx(0.01)


# ============================================================================ R-004: stop after fill


def test_failed_stop_closes_position_immediately(h: Harness):
    fake = h.fake
    orig_pos, orig_send = fake.positions_get, fake.order_send

    def positions_no_sl(symbol=None, ticket=None):  # the broker "lost" the stop
        return [replace(p, sl=0.0) for p in orig_pos(symbol=symbol, ticket=ticket)]

    def send(req):
        if req.action == K.TRADE_ACTION_SLTP:  # and the repair is refused
            fake.requests.append(req)
            return OrderResult(retcode=K.TRADE_RETCODE_INVALID_STOPS, comment="Invalid stops")
        return orig_send(req)

    fake.positions_get = positions_no_sl  # type: ignore[method-assign]
    fake.order_send = send  # type: ignore[method-assign]
    res = h.enter()
    assert res.status == "failed" and res.detail == "stop_failed_closed"
    assert orig_pos() == []  # closed at once
    actions = [r.action for r in h.sends()]
    assert actions == [K.TRADE_ACTION_DEAL, K.TRADE_ACTION_SLTP, K.TRADE_ACTION_DEAL]
    inc = h.journal.open_incident("stop_failed")
    assert inc is not None and inc["severity"] == "critical"
    assert h.journal.alerts("stop_failed")
    trade = h.journal.trade(res.position_id)
    assert trade is not None and trade["exit_reason"] == "no_stop"
    assert h.journal.position(res.position_id)["status"] == "closed"


def test_ensure_stop_repairs_and_is_idempotent(h: Harness):
    res = h.enter()
    h.fake._positions[res.position_id].sl = 0.0
    n = len(h.fake.requests)
    assert h.engine.ensure_stop(res.position_id, STOP_LONG)
    assert h.fake.positions_get(ticket=res.position_id)[0].sl == STOP_LONG
    assert len(h.fake.requests) == n + 1
    assert h.engine.ensure_stop(res.position_id, STOP_LONG)  # already there: no request
    assert len(h.fake.requests) == n + 1
    assert not h.engine.ensure_stop(999_999, STOP_LONG)


# ============================================================================ stop moves


def test_move_stop_toward_profit_ok_away_rejected(h: Harness):
    res = h.enter()
    pid = res.position_id
    ok = h.engine.move_stop(pid, 3990.0, mk_market(), h.ctx())
    assert ok.status == "executed", ok
    assert h.fake.positions_get(ticket=pid)[0].sl == 3990.0
    assert h.fake.positions_get(ticket=pid)[0].tp == TARGET_LONG  # tp kept
    assert h.journal.position(pid)["sl"] == 3990.0
    h.clock.advance(2000)
    bad = h.engine.move_stop(pid, 3985.0, mk_market(), h.ctx())
    assert bad.status == "rejected"
    assert bad.risk_code == RejectCode.STOP_MOVE_NOT_TOWARD_PROFIT.value
    assert h.fake.positions_get(ticket=pid)[0].sl == 3990.0
    across = h.engine.move_stop(pid, 4000.5, mk_market(), h.ctx())
    assert across.risk_code == RejectCode.STOP_MOVE_ACROSS_PRICE.value
    unknown = h.engine.move_stop(424242, 3990.0, mk_market(), h.ctx())
    assert unknown.risk_code == RejectCode.POSITION_UNKNOWN.value


def test_repeated_sltp_within_a_second_is_coalesced(h: Harness):
    pid = h.enter().position_id
    n = len(h.fake.requests)
    first = h.engine.move_stop(pid, 3990.0, mk_market(), h.ctx())
    second = h.engine.move_stop(pid, 3990.0, mk_market(), h.ctx())
    assert first.status == "executed" and second.status == "ignored"
    assert second.detail == "coalesced"
    assert len(h.fake.requests) == n + 1
    h.clock.advance(1500)  # outside the window, a different level goes through
    third = h.engine.move_stop(pid, 3992.0, mk_market(), h.ctx())
    assert third.status == "executed" and len(h.fake.requests) == n + 2


# ============================================================================ closes


def test_close_books_trade_reconciled_to_deals(h: Harness):
    pid = h.enter().position_id
    h.fake.set_price(4010.00, 4010.20)
    res = h.engine.close(pid, "manual", mk_market(bid=4010.0, ask=4010.2), h.ctx())
    assert res.status == "executed" and res.filled_price == 4010.0
    assert h.fake.positions_get() == []
    trade = h.journal.trade(pid)
    assert trade is not None
    assert trade["exit_reason"] == "manual" and trade["side"] == "long"
    assert trade["entry_price"] == ASK and trade["exit_price"] == 4010.0
    assert trade["gross_pnl"] == pytest.approx(9.8)
    assert trade["net_pnl"] == pytest.approx(deal_sum(h.fake, pid))  # R-054
    assert trade["spread_cost"] == pytest.approx(0.2)  # half-spread 0.10 at each end × 0.01 × 100
    assert trade["slippage_cost"] == 0.0
    assert trade["r_multiple"] == pytest.approx(9.8 / 20.2)
    assert trade["risk_pct_of_equity"] == pytest.approx(0.404)
    p = h.journal.position(pid)
    assert p["status"] == "closed" and p["exit_reason"] == "manual"
    fills = h.journal.fills_for(pid)
    assert [f["entry"] for f in fills] == [K.DEAL_ENTRY_IN, K.DEAL_ENTRY_OUT]


def test_costs_attribution_with_slippage_and_commission():
    h = mk_harness(FakeMT5(slippage_points=5, commission_per_lot_per_side=3.5))
    pid = h.enter().position_id
    h.fake.set_price(4010.00, 4010.20)
    res = h.engine.close(pid, "manual", mk_market(bid=4010.0, ask=4010.2), h.ctx())
    assert res.status == "executed" and res.filled_price == pytest.approx(4009.95)
    t = h.journal.trade(pid)
    assert t["gross_pnl"] == pytest.approx(9.7)  # (4009.95 − 4000.25) × 0.01 × 100
    deals = [d for d in h.fake.deals if d.position_id == pid]
    assert t["commission"] == pytest.approx(sum(d.commission for d in deals))
    assert t["commission"] < 0
    assert t["net_pnl"] == pytest.approx(t["gross_pnl"] + t["commission"])
    assert t["net_pnl"] == pytest.approx(deal_sum(h.fake, pid))  # R-054
    assert t["slippage_cost"] == pytest.approx(0.10)
    assert t["spread_cost"] == pytest.approx(0.20)
    # mid-to-mid "signal" P&L = gross + spread paid + adverse slippage = 10.00
    assert t["gross_pnl"] + t["spread_cost"] + t["slippage_cost"] == pytest.approx(10.0)


def test_close_unknown_position_rejected(h: Harness):
    res = h.engine.close(777, "manual", mk_market(), h.ctx())
    assert res.status == "rejected" and res.risk_code == RejectCode.POSITION_UNKNOWN.value
    assert h.sends() == []


def test_close_all_and_cancel_pending():
    h = mk_harness(cfg=mk_cfg(max_positions=2))
    a = h.enter(mk_req(), acct=mk_acct(open_positions=0))
    b = h.enter(mk_req("short", proposal="prop-2"), acct=mk_acct(open_positions=1))
    assert a.status == b.status == "executed"
    pend = h.fake.order_send(
        OrderRequest(
            action=K.TRADE_ACTION_PENDING,
            symbol="XAUUSDm",
            volume=0.01,
            type=K.ORDER_TYPE_BUY_LIMIT,
            price=3900.0,
            type_filling=K.ORDER_FILLING_FOK,
        )
    )
    assert pend.retcode == K.TRADE_RETCODE_PLACED
    results = h.engine.close_all("flatten")
    assert [r.status for r in results] == ["executed", "executed"]
    assert h.fake.positions_get() == []
    assert h.journal.trade(a.position_id)["exit_reason"] == "flatten"
    cancelled = h.engine.cancel_pending()
    assert len(cancelled) == 1 and cancelled[0].status == "executed"
    assert h.fake.orders_get() == []


# ============================================================================ kill switch


def test_kill_closes_everything_blocks_entries_and_persists():
    h = mk_harness(cfg=mk_cfg(max_positions=2))
    h.enter()
    h.enter(mk_req("short", proposal="prop-2"), acct=mk_acct(open_positions=1))
    results = h.engine.kill("drawdown breach")
    assert len(results) == 2 and all(r.status == "executed" for r in results)
    assert h.fake.positions_get() == [] and h.engine.killed
    blocked = h.enter(mk_req(proposal="prop-3"))
    assert blocked.status == "rejected" and blocked.risk_code == RejectCode.KILL_SWITCH.value
    assert h.journal.kill_active() and h.journal.alerts("kill_switch")
    # a restart from the same journal starts killed
    again = ExecutionEngine(h.fake, h.fake.spec, h.cfg, h.journal, clock=h.clock)
    assert again.killed
    assert not h.engine.reset_kill(owner_token_ok=False) and h.engine.killed
    assert h.engine.reset_kill(owner_token_ok=True) and not h.engine.killed
    assert not h.journal.kill_active()
    assert h.enter(mk_req(proposal="prop-4")).status == "executed"

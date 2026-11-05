"""Reconciler against FakeMT5: stop repair, adoption, broker-side closes, cursors (R-004, R-008, R-054)."""

from __future__ import annotations

import pytest

from goldtrader.execution import Reconciler
from goldtrader.execution.reconciler import DEALS_CURSOR, exit_reason_from_deals
from goldtrader.mt5 import constants as K
from goldtrader.types import Deal, OrderRequest
from tests.test_execution_engine import (
    ASK,
    STOP_LONG,
    TARGET_LONG,
    Harness,
    deal_sum,
    mk_cfg,
    mk_harness,
    mk_req,
)


@pytest.fixture
def h() -> Harness:
    return mk_harness()


def mk_reconciler(h: Harness) -> Reconciler:
    return Reconciler(h.fake, h.fake.spec, h.journal, h.engine)


def open_manual(fake, otype=K.ORDER_TYPE_BUY, sl=0.0, tp=0.0) -> int:
    """A position opened outside the engine (the owner clicking in the terminal)."""
    res = fake.order_send(
        OrderRequest(
            action=K.TRADE_ACTION_DEAL,
            symbol="XAUUSDm",
            volume=0.01,
            type=otype,
            sl=sl,
            tp=tp,
            type_filling=K.ORDER_FILLING_FOK,
            comment="manual",
        )
    )
    assert res.retcode == K.TRADE_RETCODE_DONE
    return res.order


# ============================================================================ clean state


def test_clean_run_reports_nothing(h: Harness):
    pid = h.enter().position_id
    rep = mk_reconciler(h).run_once(h.clock())
    assert rep.positions_checked == 1 and rep.discrepancies == []
    assert rep.stops_repaired == rep.unknown_positions == rep.closed_detected == 0
    assert rep.deals_ingested == 0  # the entry deal was journaled by the engine already
    assert h.journal.get_cursor(DEALS_CURSOR) > 0
    assert h.journal.position(pid)["last_seen_ms"] == h.clock()


# ============================================================================ stop repair (R-004)


def test_repairs_missing_stop_from_journal(h: Harness):
    pid = h.enter().position_id
    h.fake._positions[pid].sl = 0.0  # the broker "lost" the stop
    assert h.fake.positions_get(ticket=pid)[0].sl == 0.0
    rep = mk_reconciler(h).run_once(h.clock())
    assert rep.stops_repaired == 1 and rep.positions_closed_no_stop == 0
    pos = h.fake.positions_get(ticket=pid)[0]
    assert pos.sl == STOP_LONG and pos.tp == TARGET_LONG
    assert any("repaired" in d for d in rep.discrepancies)
    assert h.journal.alerts("reconcile_discrepancy")


def test_closes_position_when_no_stop_is_known(h: Harness):
    pid = h.enter().position_id
    h.fake._positions[pid].sl = 0.0
    h.journal.set_position_stop(pid, None, TARGET_LONG)  # journal has no stop either
    rep = mk_reconciler(h).run_once(h.clock())
    assert rep.positions_closed_no_stop == 1 and rep.stops_repaired == 0
    assert h.fake.positions_get() == []
    inc = h.journal.open_incident("stop_missing")
    assert inc is not None and inc["severity"] == "critical"
    assert h.journal.alerts("stop_missing")
    t = h.journal.trade(pid)
    assert t is not None and t["exit_reason"] == "no_stop"


def test_broker_sl_differs_from_journal_updates_journal(h: Harness):
    pid = h.enter().position_id
    h.fake._positions[pid].sl = 3985.0  # moved by hand in the terminal
    rep = mk_reconciler(h).run_once(h.clock())
    assert any("broker sl 3985.0" in d for d in rep.discrepancies)
    assert h.journal.position(pid)["sl"] == 3985.0


# ============================================================================ unknown positions


def test_adopts_unknown_position_with_protective_stop(h: Harness):
    ticket = open_manual(h.fake)
    rep = mk_reconciler(h).run_once(h.clock(), atr=40.0)
    assert rep.unknown_positions == 1 and rep.stops_repaired == 1
    inc = h.journal.open_incident("unknown_position")
    assert inc is not None and str(ticket) in inc["summary"]
    row = h.journal.position(ticket)
    assert row is not None and row["source"] == "adopted" and row["status"] == "open"
    # stop_atr_max (1.5) × atr (40) below the open price
    expected = round(ASK - 1.5 * 40.0, 2)
    assert h.fake.positions_get(ticket=ticket)[0].sl == expected
    assert row["sl"] == expected
    # its entry deal was ingested
    assert len(h.journal.fills_for(ticket)) == 1 and rep.deals_ingested >= 0
    # second run: already known, nothing to adopt
    rep2 = mk_reconciler(h).run_once(h.clock() + 60_000, atr=40.0)
    assert rep2.unknown_positions == 0 and rep2.positions_checked == 1


def test_adopts_unknown_short_with_stop_above(h: Harness):
    ticket = open_manual(h.fake, otype=K.ORDER_TYPE_SELL)
    mk_reconciler(h).run_once(h.clock(), atr=40.0)
    assert h.fake.positions_get(ticket=ticket)[0].sl == round(4000.0 + 60.0, 2)


def test_unknown_position_without_atr_alerts_only(h: Harness):
    ticket = open_manual(h.fake)
    rep = mk_reconciler(h).run_once(h.clock())
    assert rep.unknown_positions == 1 and rep.stops_repaired == 0
    assert h.fake.positions_get(ticket=ticket)[0].sl == 0.0
    assert h.journal.alerts("unknown_position_no_stop")
    assert any("NO STOP" in d for d in rep.discrepancies)


def test_unknown_position_with_its_own_stop_is_just_adopted(h: Harness):
    ticket = open_manual(h.fake, sl=3950.0)
    rep = mk_reconciler(h).run_once(h.clock(), atr=40.0)
    assert rep.unknown_positions == 1 and rep.stops_repaired == 0
    assert h.fake.positions_get(ticket=ticket)[0].sl == 3950.0
    assert h.journal.position(ticket)["sl"] == 3950.0


# ============================================================================ broker-side closes


def test_detects_sl_close_and_books_trade(h: Harness):
    pid = h.enter().position_id
    h.fake.set_price(3979.00, 3979.20)  # bid <= 3980 → broker SL fires at bid 3979
    assert h.fake.positions_get() == []
    h.clock.advance(60_000)
    rep = mk_reconciler(h).run_once(h.clock())
    assert rep.closed_detected == 1 and rep.deals_ingested == 1
    t = h.journal.trade(pid)
    assert t is not None and t["exit_reason"] == "stop"
    assert t["exit_price"] == 3979.0 and t["entry_price"] == ASK
    expected = round((3979.0 - ASK) * 0.01 * 100, 2)  # −21.2
    assert t["gross_pnl"] == pytest.approx(expected)
    assert t["net_pnl"] == pytest.approx(deal_sum(h.fake, pid))  # R-054
    assert t["r_multiple"] == pytest.approx(expected / (20.2))
    # slippage vs the SL level: filled 3979 vs requested 3980 → 1.00 × 0.01 × 100
    assert t["slippage_cost"] == pytest.approx(1.0)
    assert t["spread_cost"] == pytest.approx(0.2)  # entry half-spread + current spread / 2
    p = h.journal.position(pid)
    assert p["status"] == "closed" and p["exit_reason"] == "stop"
    fills = h.journal.fills_for(pid)
    assert [f["entry"] for f in fills] == [K.DEAL_ENTRY_IN, K.DEAL_ENTRY_OUT]
    # idempotent
    rep2 = mk_reconciler(h).run_once(h.clock() + 60_000)
    assert rep2.closed_detected == 0 and rep2.deals_ingested == 0 and rep2.discrepancies == []


def test_detects_tp_close(h: Harness):
    pid = h.enter().position_id
    h.fake.set_price(4041.00, 4041.20)
    rep = mk_reconciler(h).run_once(h.clock())
    assert rep.closed_detected == 1
    t = h.journal.trade(pid)
    assert t["exit_reason"] == "target" and t["net_pnl"] == pytest.approx(deal_sum(h.fake, pid))
    assert t["r_multiple"] > 0


def test_short_sl_close(h: Harness):
    pid = h.enter(mk_req("short")).position_id
    h.fake.set_price(4021.00, 4021.20)  # ask >= 4020.2 → SL
    rep = mk_reconciler(h).run_once(h.clock())
    assert rep.closed_detected == 1
    t = h.journal.trade(pid)
    assert t["exit_reason"] == "stop" and t["side"] == "short"
    assert t["net_pnl"] == pytest.approx(deal_sum(h.fake, pid)) and t["net_pnl"] < 0


def test_vanished_position_without_deals_is_a_discrepancy(h: Harness):
    pid = h.enter().position_id
    del h.fake._positions[pid]  # gone with no closing deal at all
    rep = mk_reconciler(h).run_once(h.clock())
    assert rep.closed_detected == 0
    assert any("no exit deal" in d for d in rep.discrepancies)
    assert h.journal.position(pid)["status"] == "closed"
    assert h.journal.position(pid)["exit_reason"] == "unknown"


# ============================================================================ pending orders, cursor


def test_pending_orders_are_reported(h: Harness):
    h.fake.order_send(
        OrderRequest(
            action=K.TRADE_ACTION_PENDING,
            symbol="XAUUSDm",
            volume=0.01,
            type=K.ORDER_TYPE_BUY_LIMIT,
            price=3900.0,
            type_filling=K.ORDER_FILLING_FOK,
        )
    )
    rep = mk_reconciler(h).run_once(h.clock())
    assert any("pending order" in d for d in rep.discrepancies)


def test_deal_cursor_advances_and_ingests_manual_deals():
    h = mk_harness(cfg=mk_cfg(max_positions=2))
    r = mk_reconciler(h)
    r.run_once(h.clock())
    c0 = h.journal.get_cursor(DEALS_CURSOR)
    ticket = open_manual(h.fake, sl=3950.0)
    h.clock.advance(5_000)
    rep = r.run_once(h.clock())
    assert rep.unknown_positions == 1
    assert len(h.journal.fills_for(ticket)) == 1
    assert h.journal.get_cursor(DEALS_CURSOR) > c0


def test_exit_reason_inference():
    def d(entry, comment):
        return Deal(1, 1, 0, 0, 1, entry, 0, 1, 0, 0.01, 1.0, 0.0, 0.0, 0.0, 0.0, "X", comment)

    assert exit_reason_from_deals([d(K.DEAL_ENTRY_IN, ""), d(K.DEAL_ENTRY_OUT, "[sl]")]) == "stop"
    assert exit_reason_from_deals([d(K.DEAL_ENTRY_OUT, "[tp 4040.00]")]) == "target"
    assert exit_reason_from_deals([d(K.DEAL_ENTRY_OUT, "manual")]) == "closed"
    assert exit_reason_from_deals([d(K.DEAL_ENTRY_IN, "[sl]")]) == "closed"  # IN deals ignored

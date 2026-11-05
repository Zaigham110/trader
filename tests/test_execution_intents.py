"""IntentHandler: kinds, validation, idempotency, owner-only guard, pause (ARCHITECTURE §2.1 `api`)."""

from __future__ import annotations

import pytest

from goldtrader.contracts import Intent, IntentResult
from goldtrader.execution import IntentHandler
from goldtrader.mt5 import constants as K
from goldtrader.risk import RejectCode
from tests.test_execution_engine import (
    STOP_LONG,
    TARGET_LONG,
    Harness,
    mk_acct,
    mk_cfg,
    mk_harness,
    mk_market,
)


def mk_handler(h: Harness, open_positions: int = 0) -> IntentHandler:
    def provider():
        return mk_acct(open_positions=open_positions), mk_market(), h.ctx()

    return IntentHandler(h.engine, provider)


def enter_intent(iid="i-1", **over) -> Intent:
    base = dict(
        intent_id=iid,
        kind="enter",
        created_ms=1,
        proposal_id="prop-1",
        bundle_id="bundle-1",
        side="long",
        stop_price=STOP_LONG,
        target_price=TARGET_LONG,
    )
    base.update(over)
    return Intent(**base)


@pytest.fixture
def h() -> Harness:
    return mk_harness()


# ============================================================================ kinds


def test_enter_executes_and_journals_intent(h: Harness):
    hd = mk_handler(h)
    res = hd.handle(enter_intent())
    assert res.status == "executed" and res.intent_id == "i-1" and res.position_id
    pos = h.fake.positions_get(ticket=res.position_id)[0]
    assert pos.sl == STOP_LONG
    row = h.journal.c.execute("SELECT * FROM intents WHERE intent_id='i-1'").fetchone()
    assert (
        row["status"] == "executed" and row["kind"] == "enter" and row["source"] == "strategy_bot"
    )
    assert IntentResult.model_validate_json(row["result_json"]) == res
    assert h.journal.position(res.position_id)["intent_id"] == "i-1"


def test_enter_missing_fields_rejected(h: Harness):
    hd = mk_handler(h)
    res = hd.handle(Intent(intent_id="i-bad", kind="enter", created_ms=1))
    assert res.status == "rejected" and res.detail.startswith("invalid_intent")
    assert h.fake.requests == []


def test_invalid_payload_rejected(h: Harness):
    hd = mk_handler(h)
    res = hd.handle({"intent_id": "x", "kind": "teleport", "created_ms": 1})
    assert res.status == "rejected" and "invalid_intent" in res.detail and res.intent_id == "x"
    res2 = hd.handle({"intent_id": "y", "kind": "enter", "created_ms": 1, "extra": 1})
    assert res2.status == "rejected"  # extra="forbid"


def test_exit_move_stop_and_close_all(h: Harness):
    hd = mk_handler(h)
    pid = hd.handle(enter_intent()).position_id
    mv = hd.handle(
        Intent(
            intent_id="i-2", kind="move_stop", created_ms=2, position_id=pid, new_stop_price=3990.0
        )
    )
    assert mv.status == "executed" and h.fake.positions_get(ticket=pid)[0].sl == 3990.0
    bad = hd.handle(
        Intent(
            intent_id="i-3", kind="move_stop", created_ms=3, position_id=pid, new_stop_price=3985.0
        )
    )
    assert bad.status == "rejected"
    assert bad.risk_code == RejectCode.STOP_MOVE_NOT_TOWARD_PROFIT.value
    assert hd.handle(Intent(intent_id="i-4", kind="exit", created_ms=4)).status == "rejected"
    ex = hd.handle(
        Intent(intent_id="i-5", kind="exit", created_ms=5, position_id=pid, reason="thesis gone")
    )
    assert ex.status == "executed" and h.fake.positions_get() == []
    assert h.journal.trade(pid)["exit_reason"] == "thesis gone"
    ca = hd.handle(Intent(intent_id="i-6", kind="close_all", created_ms=6, source="system"))
    assert ca.status == "executed" and ca.detail == "closed 0 of 0"


def test_close_all_closes_every_position():
    h = mk_harness(cfg=mk_cfg(max_positions=2))
    hd = mk_handler(h)
    assert hd.handle(enter_intent("a")).status == "executed"
    hd2 = mk_handler(h, open_positions=1)
    hd2._results = hd._results
    assert (
        hd2.handle(
            enter_intent(
                "b", proposal_id="prop-2", side="short", stop_price=4020.2, target_price=3960.0
            )
        ).status
        == "executed"
    )
    res = hd2.handle(Intent(intent_id="c", kind="close_all", created_ms=3, source="system"))
    assert res.status == "executed" and res.detail == "closed 2 of 2"
    assert h.fake.positions_get() == []


# ============================================================================ idempotency


def test_repeated_intent_id_returns_stored_result(h: Harness):
    hd = mk_handler(h)
    first = hd.handle(enter_intent())
    n = len(h.fake.requests)
    second = hd.handle(enter_intent())  # same id → no second order
    assert second == first and len(h.fake.requests) == n
    assert len(h.fake.positions_get()) == 1
    # a fresh handler (restart) finds the result in the journal
    hd2 = mk_handler(h)
    third = hd2.handle(enter_intent(side="short"))  # same id, different body: still the stored one
    assert third == first and len(h.fake.requests) == n


def test_repeated_rejection_is_also_stored(h: Harness):
    hd = mk_handler(h)
    bad = enter_intent("r-1", stop_price=4010.0)  # stop on the wrong side
    first = hd.handle(bad)
    assert first.status == "rejected"
    assert hd.handle(bad) == first
    assert h.fake.requests == []


# ============================================================================ owner-only guard


def test_kill_from_strategy_bot_is_owner_only(h: Harness):
    hd = mk_handler(h)
    pid = hd.handle(enter_intent()).position_id
    res = hd.handle(Intent(intent_id="k-1", kind="kill", created_ms=2, source="strategy_bot"))
    assert res.status == "rejected" and res.detail == "owner_only"
    assert not h.engine.killed and h.fake.positions_get(ticket=pid)
    row = h.journal.c.execute("SELECT status FROM intents WHERE intent_id='k-1'").fetchone()
    assert row["status"] == "rejected"


def test_kill_from_owner_closes_and_blocks(h: Harness):
    hd = mk_handler(h)
    hd.handle(enter_intent())
    res = hd.handle(
        Intent(intent_id="k-2", kind="kill", created_ms=2, source="owner_confirmed", reason="/kill")
    )
    assert res.status == "executed" and h.engine.killed
    assert h.fake.positions_get() == []
    after = hd.handle(enter_intent("i-after", proposal_id="prop-9"))
    assert after.status == "rejected" and after.risk_code == RejectCode.KILL_SWITCH.value


def test_kill_from_system_allowed(h: Harness):
    hd = mk_handler(h)
    res = hd.handle(Intent(intent_id="k-3", kind="kill", created_ms=2, source="system"))
    assert res.status == "executed" and h.engine.killed


def test_resume_is_owner_only_and_pause_blocks_entries(h: Harness):
    hd = mk_handler(h)
    assert not hd.paused
    p = hd.handle(Intent(intent_id="p-1", kind="pause", created_ms=1))  # any source may pause
    assert p.status == "executed" and hd.paused
    blocked = hd.handle(enter_intent("e-1"))
    assert blocked.status == "rejected" and blocked.risk_code == RejectCode.PAUSED.value
    assert h.fake.requests == []
    denied = hd.handle(Intent(intent_id="r-1", kind="resume", created_ms=2, source="strategy_bot"))
    assert denied.status == "rejected" and denied.detail == "owner_only" and hd.paused
    ok = hd.handle(Intent(intent_id="r-2", kind="resume", created_ms=3, source="owner_confirmed"))
    assert ok.status == "executed" and not hd.paused
    assert hd.handle(enter_intent("e-2")).status == "executed"
    assert len(h.fake.positions_get()) == 1
    assert h.fake.requests[-1].action == K.TRADE_ACTION_DEAL

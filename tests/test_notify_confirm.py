"""Confirm-button state machine: create / confirm / cancel / expire, token hygiene, races."""

import threading

import pytest

from goldtrader.db import connect, migrate
from goldtrader.notify.confirm import (
    ConfirmError,
    ConfirmStore,
    cancel_callback,
    confirm_callback,
    parse_callback,
)

T0 = 1_793_000_000_000


def _store(path=None):
    conn = connect(path)
    migrate(conn)
    return ConfirmStore(conn)


def test_create_and_buttons_never_leak_token_into_summary():
    s = _store()
    a = s.create("kill", {"reason": "owner"}, "Kill switch: close all positions", now_ms=T0)
    assert a.status == "pending"
    assert a.expires_ms == T0 + 120_000
    assert len(a.token) == 16 and int(a.token, 16) >= 0
    assert a.token not in a.summary
    assert a.params == {"reason": "owner"}
    rows = a.buttons()
    assert [b.text for b in rows[0]] == ["Confirm", "Cancel"]
    assert rows[0][0].callback_data == f"cfm:{a.action_id}:{a.token}"
    assert rows[0][1].callback_data == f"cxl:{a.action_id}"
    assert len(rows[0][0].callback_data.encode()) <= 64
    assert s.list_pending(T0) == [a]
    with pytest.raises(ValueError):
        s.create("pause", {}, "x", now_ms=None)
    with pytest.raises(ValueError):
        s.create("pause", {}, "x", ttl_s=0, now_ms=T0)


def test_parse_callback():
    assert parse_callback(confirm_callback("ab12", "ff" * 8)) == ("confirm", "ab12", "ff" * 8)
    assert parse_callback(cancel_callback("ab12")) == ("cancel", "ab12", None)
    for bad in ("apr:1:approve", "cfm:1", "cxl:", "cfm::tok", "", "cxl:1:2"):
        with pytest.raises(ValueError):
            parse_callback(bad)


def test_confirm_happy_path_and_second_press_returns_already():
    s = _store()
    a = s.create("pause", {}, "Pause new entries", now_ms=T0)
    first = s.confirm(a.action_id, a.token, now_ms=T0 + 1000)
    assert first.already is False
    assert first.action.status == "confirmed"
    assert first.action.resolved_ms == T0 + 1000
    second = s.confirm(a.action_id, a.token, now_ms=T0 + 2000)
    assert second.already is True
    assert second.action.resolved_ms == T0 + 1000  # unchanged: nothing re-executed


def test_confirm_rejects_bad_token_unknown_cancelled_and_expired():
    s = _store()
    a = s.create("close_position", {"position_id": 5}, "Close position 5", now_ms=T0)
    with pytest.raises(ConfirmError) as ei:
        s.confirm(a.action_id, "0" * 16, now_ms=T0 + 1)
    assert ei.value.reason == "bad_token"
    assert s.get(a.action_id).status == "pending"  # a wrong token changes nothing
    with pytest.raises(ConfirmError) as ei:
        s.confirm("nope", "0" * 16, now_ms=T0 + 1)
    assert ei.value.reason == "unknown"
    # expired by the clock even though the sweep has not run yet
    with pytest.raises(ConfirmError) as ei:
        s.confirm(a.action_id, a.token, now_ms=T0 + 120_000)
    assert ei.value.reason == "expired"
    assert s.get(a.action_id).status == "expired"
    b = s.create("resume", {}, "Resume", now_ms=T0)
    assert s.cancel(b.action_id, now_ms=T0 + 5).status == "cancelled"
    assert s.cancel(b.action_id, now_ms=T0 + 6).status == "cancelled"  # idempotent
    with pytest.raises(ConfirmError) as ei:
        s.confirm(b.action_id, b.token, now_ms=T0 + 7)
    assert ei.value.reason == "cancelled"
    with pytest.raises(ConfirmError):
        s.cancel("nope", now_ms=T0)


def test_expire_due_only_touches_pending_past_ttl():
    s = _store()
    old = s.create("pause", {}, "old", ttl_s=60, now_ms=T0)
    fresh = s.create("pause", {}, "fresh", ttl_s=600, now_ms=T0)
    done = s.create("pause", {}, "done", ttl_s=60, now_ms=T0)
    s.confirm(done.action_id, done.token, now_ms=T0 + 1)
    assert s.expire_due(T0 + 60_000) == 1
    assert s.get(old.action_id).status == "expired"
    assert s.get(fresh.action_id).status == "pending"
    assert s.get(done.action_id).status == "confirmed"
    assert s.expire_due(T0 + 60_000) == 0


def test_set_message_round_trip():
    s = _store()
    a = s.create("kill", {}, "Kill", now_ms=T0)
    s.set_message(a.action_id, 4242, "77")
    got = s.get(a.action_id)
    assert (got.chat_id, got.message_id) == (4242, "77")


def test_concurrent_confirms_execute_exactly_once(tmp_path):
    """Many threads press Confirm at once; exactly one caller gets already=False."""
    db = tmp_path / "c.sqlite"
    conn = connect(db)
    migrate(conn)
    a = ConfirmStore(conn).create("kill", {}, "Kill", now_ms=T0)
    results: list[bool] = []
    errors: list[Exception] = []
    start = threading.Barrier(8)

    def press():
        c = connect(db)
        store = ConfirmStore(c)
        start.wait()
        try:
            results.append(store.confirm(a.action_id, a.token, now_ms=T0 + 10).already)
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finally:
            c.close()

    threads = [threading.Thread(target=press) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert errors == []
    assert results.count(False) == 1
    assert results.count(True) == 7

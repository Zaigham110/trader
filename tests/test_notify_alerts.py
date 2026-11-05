"""Alert outbox: quiet hours, critical bypass, delivery marking, backoff + attempt cap, trade text."""

from datetime import UTC, datetime, timedelta

import pytest

from goldtrader.db import Repo, connect, migrate
from goldtrader.notify.alerts import (
    AlertDispatcher,
    backoff_seconds,
    format_alert,
    format_trade_alert,
    in_quiet_hours,
)
from goldtrader.notify.redact import Redactor
from goldtrader.notify.telegram import FakeTelegramSender

MT5_PASSWORD = "Gold!Pass#2026"


def _db():
    conn = connect(None)
    migrate(conn)
    return conn, Repo(conn)


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 5, hour, minute, tzinfo=UTC)


def test_migration_creates_notify_tables():
    conn, _ = _db()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "notify_cursors",
        "alert_deliveries",
        "pending_confirmations",
        "process_exits",
    } <= tables


def test_in_quiet_hours_wraps_midnight():
    assert not in_quiet_hours(_at(12), None)
    assert not in_quiet_hours(_at(12), (3, 3))
    assert in_quiet_hours(_at(23), (22, 6))
    assert in_quiet_hours(_at(2), (22, 6))
    assert not in_quiet_hours(_at(6), (22, 6))
    assert in_quiet_hours(_at(10), (9, 11))
    assert not in_quiet_hours(_at(11), (9, 11))
    naive = datetime(2026, 11, 5, 23, 0)
    assert in_quiet_hours(naive, (22, 6))


def test_dispatch_marks_delivered_in_id_order_and_updates_cursor():
    conn, repo = _db()
    sender = FakeTelegramSender()
    a = repo.alert("warning", "spread", "spread 45 pips", {"spread_pips": 45.0, "cap": 30})
    b = repo.alert("info", "heartbeat", "gateway up")
    d = AlertDispatcher(conn, sender, owner_chat_id=99, quiet_hours=None, critical_kinds=set())
    assert d.dispatch_pending(_at(12)) == 2
    assert sender.texts[0].startswith("[WARNING] spread: spread 45 pips\n")
    assert "cap=30" in sender.texts[0] and "spread_pips=45" in sender.texts[0]
    assert sender.texts[1] == "[INFO] heartbeat: gateway up"
    assert all(m.chat_id == 99 for m in sender.sent)
    rows = conn.execute("SELECT id, delivered_ms FROM alerts ORDER BY id").fetchall()
    assert [r[0] for r in rows] == [a, b]
    assert all(r[1] is not None for r in rows)
    assert d.cursor("alerts_last_id") == b
    # nothing left
    assert d.dispatch_pending(_at(12, 1)) == 0
    assert len(sender.sent) == 2


def test_quiet_hours_hold_non_critical_and_pass_critical():
    conn, repo = _db()
    sender = FakeTelegramSender()
    repo.alert("info", "heartbeat", "all good")
    repo.alert("critical", "stop_missing", "position 5 has no stop")
    repo.alert("warning", "kill_switch", "kill switch armed")
    d = AlertDispatcher(conn, sender, 99, quiet_hours=(22, 6), critical_kinds={"kill_switch"})
    assert d.dispatch_pending(_at(23)) == 2
    assert [t.split("]")[0] for t in sender.texts] == ["[CRITICAL", "[WARNING"]
    pending = conn.execute("SELECT kind FROM alerts WHERE delivered_ms IS NULL").fetchall()
    assert [r[0] for r in pending] == ["heartbeat"]
    # window ends → the held alert goes out
    assert d.dispatch_pending(_at(6)) == 1
    assert sender.texts[-1] == "[INFO] heartbeat: all good"


def test_backoff_and_attempt_cap():
    conn, repo = _db()
    sender = FakeTelegramSender()
    aid = repo.alert("error", "order_rejected", "retcode 10019")
    d = AlertDispatcher(conn, sender, 99, max_attempts=8, backoff_base_s=30)
    now = _at(12)
    attempts = 0
    for _ in range(7):
        sender.fail_next = 1
        assert d.dispatch_pending(now) == 0
        attempts += 1
        assert d.attempts(aid) == attempts
        assert not d.gave_up(aid)
        # still inside the backoff window → not retried
        sender.fail_next = 0
        assert d.dispatch_pending(now + timedelta(seconds=1)) == 0
        assert len(sender.sent) == 0
        now += timedelta(seconds=backoff_seconds(attempts, 30) + 1)
    sender.fail_next = 1
    assert d.dispatch_pending(now) == 0
    assert d.attempts(aid) == 8
    assert d.gave_up(aid)
    assert d.pending() == []
    # even when Telegram works again, the given-up alert is not resent
    assert d.dispatch_pending(now + timedelta(hours=5)) == 0
    assert conn.execute("SELECT delivered_ms FROM alerts WHERE id=?", (aid,)).fetchone()[0] is None


def test_backoff_schedule():
    assert backoff_seconds(0) == 0
    assert backoff_seconds(1, 30) == 30
    assert backoff_seconds(2, 30) == 60
    assert backoff_seconds(3, 30) == 120
    assert backoff_seconds(20, 30) == 3600


def test_failure_stops_the_pass_and_recovers_later():
    conn, repo = _db()
    sender = FakeTelegramSender()
    repo.alert("info", "a", "first")
    repo.alert("info", "b", "second")
    d = AlertDispatcher(conn, sender, 99, backoff_base_s=10)
    sender.fail_next = 1
    assert d.dispatch_pending(_at(12)) == 0
    assert sender.sent == []
    # first alert waits for its backoff; the second is untouched (no attempt burnt)
    assert d.dispatch_pending(_at(12, 0) + timedelta(seconds=1)) == 1
    assert sender.texts == ["[INFO] b: second"]
    assert d.dispatch_pending(_at(12, 1)) == 1
    assert sender.texts[-1] == "[INFO] a: first"


def test_outgoing_text_is_redacted():
    conn, repo = _db()
    sender = FakeTelegramSender()
    repo.alert("error", "mt5", f"login failed for pw {MT5_PASSWORD}", {"pw": MT5_PASSWORD})
    d = AlertDispatcher(conn, sender, 99, redactor=Redactor([MT5_PASSWORD]))
    assert d.dispatch_pending(_at(12)) == 1
    assert MT5_PASSWORD not in sender.texts[0]
    assert "***" in sender.texts[0]


def test_format_alert_truncates_details():
    text = format_alert(
        {
            "severity": "info",
            "kind": "k",
            "message": "m",
            "details_json": '{"x": "' + "y" * 500 + '"}',
        }
    )
    first, second = text.split("\n")
    assert first == "[INFO] k: m"
    assert len(second) <= 200 and second.endswith("…")
    assert format_alert({"severity": None, "kind": "k", "message": "m", "details_json": "{}"}) == (
        "[INFO] k: m"
    )


def test_format_trade_alert_has_risk_and_provenance_lines():
    trade = {
        "mode": "demo",
        "side": "buy",
        "symbol": "XAUUSDm",
        "volume": 0.01,
        "entry_price": 2345.6,
        "sl": 2330.6,
        "tp": 2375.6,
        "stop_atr": 1.5,
        "target_atr": 3.0,
        "risk_pct_of_equity": 12.5,
        "risk_amount": 6.25,
        "equity": 50.0,
        "bundle_id": "b-abc123",
        "order_ticket": 123456,
        "source": "MT5 order_send / history_deals_get",
        "as_of_ms": 1_793_000_000_000,
    }
    text = format_trade_alert(trade)
    lines = text.split("\n")
    assert lines[0] == "[DEMO] Filled BUY 0.01 lot XAUUSDm @ 2345.60"
    assert lines[1] == "stop 2330.60 (1.5 ATR) · target 2375.60 (3.0 ATR)"
    assert "risk 12.5% of equity (6.25 USD of 50.00 USD)" in lines
    assert "bundle b-abc123 · ticket 123456" in lines
    assert lines[-1].startswith("source: MT5 order_send / history_deals_get, as of 2026-")
    with pytest.raises(ValueError):
        format_trade_alert({"side": "sell"})
    minimal = format_trade_alert({"risk_pct_of_equity": 2, "provenance": "fills table, 10:00 UTC"})
    assert "risk 2.0% of equity" in minimal
    assert minimal.endswith("source: fills table, 10:00 UTC")
    assert "stop MISSING" in minimal

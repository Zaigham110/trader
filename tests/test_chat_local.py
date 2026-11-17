"""Dashboard chat (D-057): the brain answers questions queued by the local page; no actions."""

from __future__ import annotations

from goldtrader.chat.local import LOCAL_CHAT_ID, DashboardChatService
from goldtrader.llm.fake import FakeProvider
from goldtrader.llm.provider import ToolCall
from goldtrader.notify.confirm import ConfirmStore
from tests.test_chat_helpers import Clock, FakeGateway, cfg, db, features
from tests.test_chat_loop import GOOD_REPLY


def _service():
    conn, store = db()
    gw, fake = FakeGateway(), FakeProvider()
    svc = DashboardChatService(cfg(), conn, fake, store, gw, Clock(), features)
    return svc, conn, gw, fake


def _ask(conn, text):
    conn.execute("INSERT INTO dashboard_chat_inbox(created_ms, text) VALUES (1, ?)", (text,))


def _texts(conn):
    q = "SELECT role, text FROM chat_messages WHERE chat_id=? ORDER BY id"
    return [(r["role"], r["text"]) for r in conn.execute(q, (LOCAL_CHAT_ID,))]


def test_question_is_answered_once_and_stored_under_the_local_chat():
    svc, conn, gw, fake = _service()
    assert svc.poll_once(timeout_s=0) == []
    fake.script("standard", [ToolCall("t1", "get_account", {}), GOOD_REPLY])
    _ask(conn, "how is the account doing?")
    assert svc.poll_once(timeout_s=0) == ["chat:ok"]
    assert _texts(conn) == [
        ("owner", "how is the account doing?"),
        ("assistant", GOOD_REPLY["text"]),
    ]
    assert svc.poll_once(timeout_s=0) == []  # the cursor moved; nothing is answered twice
    # its cursor is its own: the Telegram offset is untouched
    names = {r["name"] for r in conn.execute("SELECT name FROM notify_cursors")}
    assert names == {"dashboard_chat_offset"}


def test_actions_are_refused_from_the_dashboard():
    svc, conn, gw, fake = _service()
    _ask(conn, "/kill")
    _ask(conn, "/pause")
    assert svc.poll_once(timeout_s=0) == ["command:/kill:prepared", "command:/pause:prepared"]
    assert ConfirmStore(conn).list_pending() == [] and gw.intents == []
    replies = [t for role, t in _texts(conn) if role == "assistant"]
    assert len(replies) == 2 and all("Nothing was done" in t for t in replies)
    # an action the model prepares is cancelled the same way
    reply = dict(GOOD_REPLY, text="A confirmation was prepared.", used_tools=["prepare_action"])
    fake.script("standard", [ToolCall("t1", "prepare_action", {"type": "pause"}), reply])
    _ask(conn, "please pause trading")
    svc.poll_once(timeout_s=0)
    assert ConfirmStore(conn).list_pending() == [] and gw.intents == []
    assert "Nothing was done" in _texts(conn)[-1][1]


def test_pending_telegram_confirmations_are_left_alone():
    svc, conn, gw, fake = _service()
    store = ConfirmStore(conn)
    store.create("pause", {}, "Pause", ttl_s=1, now_ms=0, chat_id=777)
    svc.poll_once(timeout_s=0)
    assert [a.status for a in store.list_pending()] == ["pending"]  # not swept, not reported
    assert _texts(conn) == []

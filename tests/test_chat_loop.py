"""ChatService long-polling loop (R-040, R-043, D-039): owner-only, code commands, model turns with
tools and the post-processor, confirm / cancel / expire / double-press, degraded mode, storage."""

from __future__ import annotations

import json

from goldtrader.chat.agent import ChatReply, postprocess
from goldtrader.chat.telegram_loop import (
    HELP_TEXT,
    ChatService,
    FakeTelegramUpdates,
    HttpTelegramUpdates,
    status_text,
)
from goldtrader.llm.fake import FakeProvider
from goldtrader.llm.provider import ToolCall
from goldtrader.notify.confirm import ConfirmStore
from goldtrader.notify.telegram import FakeTelegramSender
from tests.test_chat_helpers import NOW, OWNER, STRANGER, Clock, FakeGateway, cfg, db, features

GOOD_REPLY = {
    "text": "Facts (source: gateway /state (MT5 account_info) as of 2025-11-04T23:46:39Z): "
    "equity 5120.50, day P&L +20.50.\nOpinion: fine.\nForecast (uncertainty): none.",
    "used_tools": ["get_account"],
    "prepared_action_id": None,
}


def _service(provider="fake", config=None, **over):
    conn, store = db()
    gw = FakeGateway()
    clock = Clock()
    sender = FakeTelegramSender()
    updates = FakeTelegramUpdates()
    fake = FakeProvider() if provider == "fake" else None
    svc = ChatService(
        config or cfg(),
        conn,
        fake,
        store,
        gw,
        sender,
        updates,
        OWNER,
        clock,
        features,
        secrets=["sk-ant-realsecret-0123456789", "hunter2pass"],
        **over,
    )
    return svc, conn, gw, clock, sender, updates, fake


def _pending(conn):
    return ConfirmStore(conn).list_pending()


def _messages(conn):
    return [
        dict(r)
        for r in conn.execute("SELECT role, text, tool_names_json, run_id FROM chat_messages")
    ]


# --- owner gate and code commands -----------------------------------------------------------------
def test_non_owner_updates_are_dropped_and_offset_advances():
    svc, conn, gw, clock, sender, updates, fake = _service()
    updates.push_message(STRANGER, "/status")
    updates.push_message(STRANGER, "hello")
    updates.push_callback(STRANGER, "cfm:abc:def")
    out = svc.poll_once()
    assert out == ["ignored:non_owner"] * 3
    assert sender.sent == [] and gw.intents == [] and fake.requests == []
    assert _messages(conn) == []  # nothing stored for strangers
    assert svc.offset() == 4 and svc.ignored_total == 3
    assert updates.offsets == [None]
    svc.poll_once()
    assert updates.offsets[-1] == 4


def test_status_and_help_are_code_only():
    svc, conn, gw, clock, sender, updates, fake = _service()
    updates.push_message(OWNER, "/status")
    updates.push_message(OWNER, "/help")
    assert svc.poll_once() == ["command:/status", "command:/help"]
    assert fake.requests == []  # no model call
    st = sender.texts[0]
    assert st.startswith("Status (source: gateway /state as of ")
    assert "equity 5120.50" in st and "#4242 long 0.01" in st and "flags: none" in st
    assert sender.texts[1] == HELP_TEXT
    gw.down = True
    updates.push_message(OWNER, "/status")
    svc.poll_once()
    assert "Gateway unavailable" in sender.texts[-1]
    assert "KILL SWITCH ON" in status_text(gw.st.model_copy(update={"kill_switch": True}), NOW)
    roles = [m["role"] for m in _messages(conn)]
    assert roles == ["owner", "assistant", "owner", "assistant", "owner", "assistant"]


# --- model turns ------------------------------------------------------------------------------------
def test_free_text_runs_tool_then_reply_with_source():
    svc, conn, gw, clock, sender, updates, fake = _service()
    fake.script("standard", [ToolCall("t1", "get_account", {}), GOOD_REPLY])
    updates.push_message(OWNER, "how is the account doing?")
    assert svc.poll_once() == ["chat:ok"]
    assert len(fake.requests) == 2
    assert fake.requests[0].tier == "standard"
    tool_names = {t.name for t in fake.requests[0].tools}
    assert "get_account" in tool_names and "prepare_action" in tool_names
    # the tool result the model saw carried the provenance envelope
    result_block = fake.requests[1].messages[-1]["content"][0]
    payload = json.loads(result_block["content"])
    assert payload["data"]["equity"] == 5120.5 and "as of" in payload["provenance"]
    assert sender.texts[-1] == GOOD_REPLY["text"] and "source:" in sender.texts[-1]
    msgs = _messages(conn)
    assert msgs[-1]["role"] == "assistant"
    assert json.loads(msgs[-1]["tool_names_json"]) == ["get_account"]
    assert msgs[-1]["run_id"] and gw.intents == []


def test_reply_without_source_after_data_tool_is_replaced():
    svc, conn, gw, clock, sender, updates, fake = _service()
    fake.script(
        "standard",
        [
            ToolCall("t1", "get_account", {}),
            {
                "text": "Equity is 5120.50, nice day.",
                "used_tools": ["get_account"],
                "prepared_action_id": None,
            },
        ],
    )
    updates.push_message(OWNER, "equity?")
    assert svc.poll_once() == ["chat:rejected"]
    assert "could not produce a verified answer" in sender.texts[-1]
    assert "without a source tag" in sender.texts[-1]
    assert "5120.50" not in sender.texts[-1]


def test_reply_without_tools_needs_no_source():
    svc, conn, gw, clock, sender, updates, fake = _service()
    fake.script(
        "standard",
        [{"text": "Hello! Ask me anything.", "used_tools": [], "prepared_action_id": None}],
    )
    updates.push_message(OWNER, "hi")
    assert svc.poll_once() == ["chat:ok"]
    assert sender.texts[-1] == "Hello! Ask me anything."


def test_secret_and_account_number_are_scrubbed():
    svc, conn, gw, clock, sender, updates, fake = _service()
    fake.script(
        "standard",
        [
            {
                "text": "Facts (source: x): the key is sk-ant-realsecret-0123456789.",
                "used_tools": [],
                "prepared_action_id": None,
            },
            {
                "text": "Facts (source: config): login 12345678901 is set.",
                "used_tools": [],
                "prepared_action_id": None,
            },
        ],
    )
    updates.push_message(OWNER, "what is the api key?")
    updates.push_message(OWNER, "what is my login?")
    assert svc.poll_once() == ["chat:rejected", "chat:rejected"]
    assert "sk-ant" not in sender.texts[-2] and "secret-shaped" in sender.texts[-2]
    assert "12345678901" not in sender.texts[-1] and "account id" in sender.texts[-1]
    # stored text is redacted too (R-053)
    assert all("sk-ant-realsecret" not in m["text"] for m in _messages(conn))


def test_postprocess_rules_directly():
    ok = ChatReply(text="Facts (source: trades table as of 2025-11-04T00:00:00Z): 3 trades.")
    assert postprocess(ok, ["get_trades"]) == (ok, None)
    bad, why = postprocess(ChatReply(text="3 trades."), ["get_trades"])
    assert why and "source" in why and bad.text != "3 trades."
    _, why = postprocess(ChatReply(text="password hunter2pass"), [], ["hunter2pass"])
    assert why and "secret" in why
    _, why = postprocess(ChatReply(text="I paused the system."), ["prepare_action"])
    assert why and "prepared" in why
    fine, why = postprocess(
        ChatReply(text="A confirmation to pause was prepared; press Confirm."), ["prepare_action"]
    )
    assert why is None
    # ISO timestamps and prices are not "account numbers"
    assert postprocess(ChatReply(text="at 2025-11-04T23:46:40Z price 4000.25"), [])[1] is None


def test_deep_prefix_uses_deep_tier_and_history_is_replayed():
    svc, conn, gw, clock, sender, updates, fake = _service()
    fake.script("standard", [{"text": "Hi.", "used_tools": [], "prepared_action_id": None}])
    fake.script("deep", [{"text": "Deep thoughts.", "used_tools": [], "prepared_action_id": None}])
    updates.push_message(OWNER, "hello")
    updates.push_message(OWNER, "/deep why gold?")
    assert svc.poll_once() == ["chat:ok", "chat:ok"]
    assert fake.requests[1].tier == "deep"
    msgs = fake.requests[1].messages
    assert msgs[0]["role"] == "user" and "hello" in msgs[0]["content"]
    assert msgs[1]["role"] == "assistant" and msgs[1]["content"] == "Hi."
    assert msgs[2]["role"] == "user" and msgs[2]["content"].endswith("why gold?")
    assert "/deep" not in msgs[2]["content"]


def test_deep_tier_can_be_disabled_in_config():
    svc, conn, gw, clock, sender, updates, fake = _service(
        config=cfg(chat={"deep_tier_on_request": False})
    )
    fake.script("standard", [{"text": "Std.", "used_tools": [], "prepared_action_id": None}])
    updates.push_message(OWNER, "/deep hmm")
    assert svc.poll_once() == ["chat:ok"] and fake.requests[0].tier == "standard"


# --- prepare / confirm / cancel / expire ------------------------------------------------------------
def test_pause_command_prepares_buttons_without_executing():
    svc, conn, gw, clock, sender, updates, fake = _service()
    updates.push_message(OWNER, "/pause")
    assert svc.poll_once() == ["command:/pause:prepared"]
    assert fake.requests == [] and gw.intents == []
    (action,) = _pending(conn)
    assert action.kind == "pause" and action.chat_id == OWNER and action.message_id == "1"
    m = sender.sent[-1]
    assert "Confirm within 120 s" in m.text and "Pause: block new entries" in m.text
    assert [b.text for b in m.buttons[0]] == ["Confirm", "Cancel"]
    assert m.buttons[0][0].callback_data == f"cfm:{action.action_id}:{action.token}"
    assert action.token not in m.text
    row = conn.execute("SELECT * FROM chat_actions").fetchone()
    assert row["kind"] == "pause" and row["prepared_by"] == "command" and row["executed_ms"] is None


def test_confirm_executes_once_edits_buttons_and_refuses_second_press():
    svc, conn, gw, clock, sender, updates, fake = _service()
    updates.push_message(OWNER, "/pause")
    svc.poll_once()
    (action,) = _pending(conn)
    data = f"cfm:{action.action_id}:{action.token}"
    updates.push_callback(OWNER, data, message_id=1, callback_id="c1")
    assert svc.poll_once() == ["callback:confirmed"]
    assert len(gw.intents) == 1
    it = gw.intents[0]
    assert it.kind == "pause" and it.source == "owner_confirmed"
    assert sender.edits[-1].message_id == "1" and sender.edits[-1].buttons is None
    assert sender.texts[-1].startswith("Confirmed → pause: executed")
    assert updates.answered[-1] == ("c1", "Confirmed.")
    row = conn.execute("SELECT executed_ms, result FROM chat_actions").fetchone()
    assert row["executed_ms"] == NOW and "executed" in row["result"]
    # second press: already done, no second intent
    updates.push_callback(OWNER, data, message_id=1, callback_id="c2")
    assert svc.poll_once() == ["callback:already"]
    assert len(gw.intents) == 1
    assert "not executing twice" in sender.texts[-1]
    assert updates.answered[-1] == ("c2", "Already done.")


def test_cancel_and_bad_token():
    svc, conn, gw, clock, sender, updates, fake = _service()
    updates.push_message(OWNER, "/resume")
    svc.poll_once()
    (action,) = _pending(conn)
    updates.push_callback(OWNER, f"cfm:{action.action_id}:deadbeefdeadbeef", callback_id="c0")
    assert svc.poll_once() == ["callback:bad_token"]
    assert gw.intents == [] and "refused (bad_token)" in sender.texts[-1]
    assert ConfirmStore(conn).get(action.action_id).status == "pending"
    updates.push_callback(OWNER, f"cxl:{action.action_id}", message_id=1, callback_id="c1")
    assert svc.poll_once() == ["callback:cancelled"]
    assert gw.intents == [] and "Nothing was done" in sender.texts[-1]
    assert sender.edits[-1].buttons is None
    assert ConfirmStore(conn).get(action.action_id).status == "cancelled"
    # confirming a cancelled action is refused
    updates.push_callback(OWNER, f"cfm:{action.action_id}:{action.token}", callback_id="c2")
    assert svc.poll_once() == ["callback:cancelled"]
    assert gw.intents == []
    updates.push_callback(OWNER, "nonsense", callback_id="c3")
    assert svc.poll_once() == ["callback:unknown"]


def test_expired_confirmation_is_reported_and_never_executes():
    svc, conn, gw, clock, sender, updates, fake = _service()
    updates.push_message(OWNER, "/kill")
    svc.poll_once()
    (action,) = _pending(conn)
    assert "KILL SWITCH" in sender.texts[-1] and gw.intents == []
    clock.advance(121_000)
    updates.push_callback(OWNER, f"cfm:{action.action_id}:{action.token}", callback_id="c1")
    assert svc.poll_once() == ["callback:expired"]
    assert gw.intents == []
    assert ConfirmStore(conn).get(action.action_id).status == "expired"
    assert "Expired without confirmation" in " | ".join(sender.texts)
    assert "expired" in sender.texts[-1]
    assert sender.edits and sender.edits[-1].buttons is None


def test_sweep_reports_expiry_without_a_press():
    svc, conn, gw, clock, sender, updates, fake = _service()
    updates.push_message(OWNER, "/pause")
    svc.poll_once()
    clock.advance(200_000)
    assert svc.poll_once() == []
    assert _pending(conn) == [] and gw.intents == []
    assert "Expired without confirmation" in sender.texts[-1]
    assert sender.edits[-1].message_id == "1" and sender.edits[-1].buttons is None


def test_kill_requires_confirmation_and_is_sent_owner_confirmed():
    svc, conn, gw, clock, sender, updates, fake = _service()
    updates.push_message(OWNER, "/kill")
    svc.poll_once()
    assert gw.intents == []
    (action,) = _pending(conn)
    updates.push_callback(OWNER, f"cfm:{action.action_id}:{action.token}", message_id=1)
    assert svc.poll_once() == ["callback:confirmed"]
    assert [(i.kind, i.source) for i in gw.intents] == [("kill", "owner_confirmed")]


def test_close_command_validates_position_and_sends_exit():
    svc, conn, gw, clock, sender, updates, fake = _service()
    updates.push_message(OWNER, "/close")
    updates.push_message(OWNER, "/close 99")
    updates.push_message(OWNER, "/close 4242")
    assert svc.poll_once() == [
        "command:/close:usage",
        "command:/close_position:refused",
        "command:/close_position:prepared",
    ]
    assert "not open" in sender.texts[1]
    (action,) = _pending(conn)
    updates.push_callback(OWNER, f"cfm:{action.action_id}:{action.token}", message_id=3)
    svc.poll_once()
    it = gw.intents[-1]
    assert it.kind == "exit" and it.position_id == 4242 and it.source == "owner_confirmed"


def test_model_prepared_action_gets_buttons_and_no_execution():
    svc, conn, gw, clock, sender, updates, fake = _service()

    def final(_: object = None):
        return {
            "text": "A confirmation to pause was prepared; press Confirm to apply it.",
            "used_tools": ["prepare_action"],
            "prepared_action_id": None,
        }

    fake.script(
        "standard", [ToolCall("t1", "prepare_action", {"type": "pause", "params": {}}), final()]
    )
    updates.push_message(OWNER, "please pause trading")
    assert svc.poll_once() == ["chat:ok"]
    assert gw.intents == []
    (action,) = _pending(conn)
    assert action.kind == "pause" and action.chat_id == OWNER
    tool_result = json.loads(fake.requests[1].messages[-1]["content"][0]["content"])
    assert tool_result["status"] == "pending_confirmation"
    assert action.token not in json.dumps(tool_result)  # the model never sees the token
    buttons_msg = sender.sent[-1]
    assert buttons_msg.buttons and buttons_msg.buttons[0][0].callback_data.startswith("cfm:")
    row = conn.execute("SELECT prepared_by FROM chat_actions").fetchone()
    assert row["prepared_by"].startswith("agent:")


def test_model_cannot_fake_a_prepared_action_id():
    svc, conn, gw, clock, sender, updates, fake = _service()
    fake.script(
        "standard",
        [
            {"text": "Prepared.", "used_tools": [], "prepared_action_id": "made-up"},
            {"text": "Nothing prepared.", "used_tools": [], "prepared_action_id": None},
        ],
    )
    updates.push_message(OWNER, "pause")
    assert svc.poll_once() == ["chat:ok"]
    assert len(fake.requests) == 2  # the invalid output was resubmitted
    assert "resubmit" in fake.requests[1].messages[-1]["content"]
    assert sender.texts[-1] == "Nothing prepared."


# --- degraded mode -----------------------------------------------------------------------------------
def test_degraded_without_provider_commands_still_work():
    svc, conn, gw, clock, sender, updates, fake = _service(provider=None)
    updates.push_message(OWNER, "what is my equity?")
    updates.push_message(OWNER, "/status")
    updates.push_message(OWNER, "/pause")
    assert svc.poll_once() == ["chat:unavailable", "command:/status", "command:/pause:prepared"]
    assert "unavailable right now (no model provider configured)" in sender.texts[0]
    assert "/pause" in sender.texts[0] and "/kill" in sender.texts[0]
    assert sender.texts[1].startswith("Status (source:")
    assert len(_pending(conn)) == 1


def test_degraded_when_breaker_open_and_on_run_failure():
    svc, conn, gw, clock, sender, updates, fake = _service()
    for _ in range(3):
        svc.breaker.record_failure(NOW, "test")
    assert svc.breaker.state == "open"
    updates.push_message(OWNER, "hello?")
    assert svc.poll_once() == ["chat:unavailable"]
    assert "circuit breaker is open" in sender.texts[-1] and fake.requests == []
    # a failing run (provider error) → unavailable text, breaker counts it
    svc2, conn2, gw2, clock2, sender2, updates2, fake2 = _service()
    fake2.script("standard", ["not json"] * 3)
    updates2.push_message(OWNER, "hi")
    out = svc2.poll_once()
    assert out == ["chat:invalid"]
    assert "unavailable right now (run invalid)" in sender2.texts[-1]
    assert svc2.breaker.failures == 1


def test_get_updates_failure_is_contained_and_http_transport_shapes():
    svc, conn, gw, clock, sender, updates, fake = _service()

    class Boom:
        def get_updates(self, offset, timeout_s):
            raise OSError("network down")

        def answer_callback(self, cb, text=""):
            pass

    svc.updates = Boom()
    assert svc.poll_once() == ["error:get_updates"]
    calls = []

    def transport(method, payload):
        calls.append((method, payload))
        if method == "getUpdates":
            return {"ok": True, "result": [{"update_id": 7, "message": {}}, "junk"]}
        return {"ok": True, "result": True}

    http = HttpTelegramUpdates("123456789:" + "A" * 35, transport=transport, long_poll_s=25)
    assert "***" in repr(http) and "A" * 35 not in repr(http)
    assert http.get_updates(5, 60) == [{"update_id": 7, "message": {}}]
    assert calls[0] == (
        "getUpdates",
        {"timeout": 25, "allowed_updates": ["message", "callback_query"], "offset": 5},
    )
    http.answer_callback("cb9", "x" * 300)
    assert calls[1][0] == "answerCallbackQuery" and len(calls[1][1]["text"]) == 200


def test_chat_package_has_no_import_path_to_mt5_or_execution():
    """AGENTS §1 least privilege: the chat reaches the broker only through gateway intents."""
    import json as _json
    import subprocess
    import sys
    from pathlib import Path

    code = (
        "import json, sys\n"
        "import goldtrader.chat\n"
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('goldtrader'))))\n"
    )
    src = Path(__file__).resolve().parents[1] / "src"
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=str(src)
    )
    loaded = _json.loads(out.stdout.strip().splitlines()[-1])
    forbidden = ("goldtrader.mt5", "goldtrader.execution", "goldtrader.gateway.state")
    assert not [m for m in loaded if m.startswith(forbidden)], loaded

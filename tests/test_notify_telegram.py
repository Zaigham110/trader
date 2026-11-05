"""Telegram sender: chunking, HTML escaping, button payloads, token redaction in errors."""

import pytest

from goldtrader.notify.redact import scan_for_secrets
from goldtrader.notify.telegram import (
    Button,
    FakeTelegramSender,
    HttpTelegramSender,
    TelegramError,
    chunk_text,
    inline_keyboard,
)

TOKEN = "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ012345678"


class RecordingTransport:
    def __init__(self, fail_with: Exception | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.fail_with = fail_with
        self._id = 100

    def __call__(self, method: str, payload: dict) -> dict:
        self.calls.append((method, payload))
        if self.fail_with is not None:
            raise self.fail_with
        self._id += 1
        return {"ok": True, "result": {"message_id": self._id}}


def test_button_validates_callback_size():
    Button("ok", "x" * 64)
    with pytest.raises(ValueError):
        Button("too long", "x" * 65)
    with pytest.raises(ValueError):
        Button("multibyte", "é" * 33)  # 66 bytes
    with pytest.raises(ValueError):
        Button("", "cfm:1:2")
    assert inline_keyboard(None) is None
    assert inline_keyboard([[Button("A", "a"), Button("B", "b")]]) == {
        "inline_keyboard": [
            [{"text": "A", "callback_data": "a"}, {"text": "B", "callback_data": "b"}]
        ]
    }


def test_chunk_text_prefers_line_boundaries_and_hard_splits_long_lines():
    text = "\n".join(f"line {i}" for i in range(10))
    chunks = chunk_text(text, 30)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")
    assert all(len(c) <= 30 for c in chunks)
    assert all(not c.startswith("\n") for c in chunks)
    long = "x" * 95
    assert chunk_text(long, 40) == ["x" * 40, "x" * 40, "x" * 15]
    assert chunk_text("", 10) == [""]
    assert chunk_text("short", 10) == ["short"]


def test_send_chunks_escapes_html_and_attaches_buttons_to_last_chunk():
    t = RecordingTransport()
    s = HttpTelegramSender(TOKEN, transport=t, max_chars=50)
    body = "a<b>&c\n" * 20  # escaped each line is 'a&lt;b&gt;&amp;c' (16 chars)
    mid = s.send(42, body, buttons=[[Button("Confirm", "cfm:1:tok")]])
    methods = {m for m, _ in t.calls}
    assert methods == {"sendMessage"}
    assert len(t.calls) > 1
    for i, (_, payload) in enumerate(t.calls):
        assert payload["chat_id"] == 42
        assert payload["parse_mode"] == "HTML"
        assert len(payload["text"]) <= 50
        assert "<b>" not in payload["text"] and "&lt;b&gt;" in payload["text"]
        if i == len(t.calls) - 1:
            assert payload["reply_markup"] == {
                "inline_keyboard": [[{"text": "Confirm", "callback_data": "cfm:1:tok"}]]
            }
        else:
            assert "reply_markup" not in payload
    assert mid == str(100 + len(t.calls))


def test_edit_buttons_payload():
    t = RecordingTransport()
    s = HttpTelegramSender(TOKEN, transport=t)
    s.edit_buttons(42, "17", None)
    method, payload = t.calls[0]
    assert method == "editMessageReplyMarkup"
    assert payload == {"chat_id": 42, "message_id": 17, "reply_markup": {"inline_keyboard": []}}


def test_errors_never_contain_the_token():
    t = RecordingTransport(fail_with=RuntimeError(f"https://api.telegram.org/bot{TOKEN}/x boom"))
    s = HttpTelegramSender(TOKEN, transport=t)
    with pytest.raises(TelegramError) as ei:
        s.send(1, "hi")
    assert scan_for_secrets(str(ei.value), [TOKEN]) == []
    assert TOKEN not in repr(s)
    with pytest.raises(ValueError):
        HttpTelegramSender("")


def test_fake_sender_records_and_can_fail():
    f = FakeTelegramSender()
    mid = f.send(7, "hello", buttons=[[Button("x", "cxl:1")]])
    assert mid == "1"
    assert f.texts == ["hello"]
    assert f.sent[0].buttons == [[Button("x", "cxl:1")]]
    f.fail_next = 1
    with pytest.raises(TelegramError):
        f.send(7, "again")
    assert f.send(7, "ok") == "2"
    f.edit_buttons(7, "2", None)
    assert f.edits[0].message_id == "2"

"""Minimal Telegram Bot API sender (stdlib only) with a fake for tests.

ARCHITECTURE.md §2.2 ``telegram`` row and §6: owner chat only, buttons for state changes, token
never logged (R-053). Network code lives in exactly one function, :func:`urllib_transport`, so
tests inject a fake transport and never touch the network. Text is sent with ``parse_mode=HTML``
after escaping, split into ≤ 4,000-character chunks (Telegram's limit is 4,096).
"""

from __future__ import annotations

import html
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from goldtrader.notify.redact import MASK, Redactor

MAX_MESSAGE_CHARS = 4000
MAX_CALLBACK_BYTES = 64
API_BASE = "https://api.telegram.org"

#: ``transport(method, payload) -> parsed JSON response``. Raises :class:`TelegramError`.
Transport = Callable[[str, dict[str, Any]], dict[str, Any]]


class TelegramError(RuntimeError):
    """A Bot API call failed. The message never contains the bot token."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after_s: float | None = None,
        description: str | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.retry_after_s = retry_after_s
        self.description = description


@dataclass(frozen=True)
class Button:
    """One inline-keyboard button. ``callback_data`` must fit Telegram's 64-byte limit."""

    text: str
    callback_data: str

    def __post_init__(self) -> None:
        n = len(self.callback_data.encode("utf-8"))
        if n == 0 or n > MAX_CALLBACK_BYTES:
            raise ValueError(f"callback_data must be 1..{MAX_CALLBACK_BYTES} bytes, got {n}")
        if not self.text:
            raise ValueError("button text must not be empty")

    def to_api(self) -> dict[str, str]:
        return {"text": self.text, "callback_data": self.callback_data}


def inline_keyboard(buttons: Sequence[Sequence[Button]] | None) -> dict[str, Any] | None:
    """Build the ``reply_markup`` object; ``None`` / empty → ``None`` (no keyboard)."""
    if not buttons:
        return None
    rows = [[b.to_api() for b in row] for row in buttons if row]
    return {"inline_keyboard": rows} if rows else None


def chunk_text(
    text: str, limit: int = MAX_MESSAGE_CHARS, measure: Callable[[str], int] = len
) -> list[str]:
    """Split ``text`` into pieces whose ``measure`` is ≤ ``limit``, preferring line boundaries.

    ``measure`` lets the caller count the *escaped* length so HTML entities never straddle a
    chunk boundary. An empty text yields one empty chunk.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    if measure(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = line if not current else f"{current}\n{line}"
        if measure(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if measure(line) <= limit:
            current = line
            continue
        # A single over-long line: hard split by characters under the measure.
        piece = ""
        for ch in line:
            if measure(piece + ch) > limit:
                chunks.append(piece)
                piece = ch
            else:
                piece += ch
        current = piece
    if current or not chunks:
        chunks.append(current)
    return chunks


class TelegramSender(Protocol):
    """What the outbox and the confirm layer need from a sender."""

    def send(
        self, chat_id: int | str, text: str, *, buttons: Sequence[Sequence[Button]] | None = None
    ) -> str:
        """Send ``text`` (chunked if needed) and return the id of the last message sent."""
        ...

    def edit_buttons(
        self, chat_id: int | str, message_id: str, buttons: Sequence[Sequence[Button]] | None
    ) -> None:
        """Replace (or with ``None`` remove) the inline keyboard of an existing message."""
        ...


def urllib_transport(token: str, timeout_s: float = 10.0) -> Transport:
    """The only function that talks to the network. Errors come back as :class:`TelegramError`.

    The returned closure holds the token; nothing it raises or returns contains it.
    """
    redactor = Redactor([token])

    def call(method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{API_BASE}/bot{token}/{method}"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            raw_err = ""
            try:
                raw_err = e.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 - best effort body read
                pass
            desc, retry = _parse_error_body(raw_err)
            raise TelegramError(
                redactor.redact(f"telegram {method} failed: HTTP {e.code} {desc or ''}".strip()),
                status=e.code,
                retry_after_s=retry,
                description=redactor.redact(desc) if desc else None,
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise TelegramError(
                redactor.redact(f"telegram {method} failed: {type(e).__name__}: {e}")
            ) from None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise TelegramError(f"telegram {method} returned non-JSON body") from None
        if not isinstance(data, dict) or not data.get("ok"):
            desc = str(data.get("description", "")) if isinstance(data, dict) else ""
            raise TelegramError(
                redactor.redact(f"telegram {method} not ok: {desc}"), description=desc
            )
        return data

    return call


def _parse_error_body(raw: str) -> tuple[str | None, float | None]:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return (raw[:200] or None), None
    desc = data.get("description") if isinstance(data, dict) else None
    retry = None
    params = data.get("parameters") if isinstance(data, dict) else None
    if isinstance(params, dict) and "retry_after" in params:
        try:
            retry = float(params["retry_after"])
        except (TypeError, ValueError):
            retry = None
    return (str(desc) if desc else None), retry


class HttpTelegramSender:
    """Bot API sender. ``transport`` defaults to :func:`urllib_transport`; tests pass a fake.

    ``escape_html=True`` (default) HTML-escapes the text so arbitrary alert content cannot break
    the HTML parse mode; pass ``False`` only for text that is already valid Telegram HTML.
    """

    def __init__(
        self,
        token: str,
        *,
        transport: Transport | None = None,
        timeout_s: float = 10.0,
        escape_html: bool = True,
        max_chars: int = MAX_MESSAGE_CHARS,
    ):
        if not token:
            raise ValueError("telegram token is required")
        self._transport = transport or urllib_transport(token, timeout_s)
        self._redactor = Redactor([token])
        self.escape_html = escape_html
        self.max_chars = max_chars

    def __repr__(self) -> str:  # never show the token
        return f"HttpTelegramSender(token={MASK!r})"

    def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._transport(method, payload)
        except TelegramError:
            raise
        except Exception as e:  # noqa: BLE001 - any transport failure becomes TelegramError
            raise TelegramError(
                self._redactor.redact(f"telegram {method} failed: {type(e).__name__}: {e}")
            ) from None

    def _prepare(self, text: str) -> list[str]:
        if self.escape_html:

            def measure(s: str) -> int:
                return len(html.escape(s, quote=False))

            return [html.escape(c, quote=False) for c in chunk_text(text, self.max_chars, measure)]
        return chunk_text(text, self.max_chars)

    def send(
        self, chat_id: int | str, text: str, *, buttons: Sequence[Sequence[Button]] | None = None
    ) -> str:
        chunks = self._prepare(text)
        markup = inline_keyboard(buttons)
        message_id = ""
        for i, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if markup is not None and i == len(chunks) - 1:
                payload["reply_markup"] = markup
            data = self._call("sendMessage", payload)
            result = data.get("result") or {}
            message_id = str(result.get("message_id", ""))
        return message_id

    def edit_buttons(
        self, chat_id: int | str, message_id: str, buttons: Sequence[Sequence[Button]] | None
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": int(message_id) if str(message_id).isdigit() else message_id,
            "reply_markup": inline_keyboard(buttons) or {"inline_keyboard": []},
        }
        self._call("editMessageReplyMarkup", payload)


@dataclass
class SentMessage:
    chat_id: int | str
    text: str
    buttons: list[list[Button]] | None
    message_id: str


@dataclass
class ButtonEdit:
    chat_id: int | str
    message_id: str
    buttons: list[list[Button]] | None


@dataclass
class FakeTelegramSender:
    """Records every send / edit. Set ``fail_next`` to make the next ``send`` raise."""

    sent: list[SentMessage] = field(default_factory=list)
    edits: list[ButtonEdit] = field(default_factory=list)
    fail_next: int = 0  # number of upcoming sends that raise TelegramError
    _next_id: int = 1

    def send(
        self, chat_id: int | str, text: str, *, buttons: Sequence[Sequence[Button]] | None = None
    ) -> str:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise TelegramError("fake transport failure", status=502)
        mid = str(self._next_id)
        self._next_id += 1
        self.sent.append(
            SentMessage(chat_id, text, [list(r) for r in buttons] if buttons else None, mid)
        )
        return mid

    def edit_buttons(
        self, chat_id: int | str, message_id: str, buttons: Sequence[Sequence[Button]] | None
    ) -> None:
        self.edits.append(
            ButtonEdit(chat_id, message_id, [list(r) for r in buttons] if buttons else None)
        )

    @property
    def texts(self) -> list[str]:
        return [m.text for m in self.sent]

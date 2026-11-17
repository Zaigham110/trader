"""Minimal Telegram Bot API sender (stdlib only) with a fake for tests.

ARCHITECTURE.md §2.2 ``telegram`` row and §6: owner chat only, buttons for state changes, token
never logged (R-053). Network code lives in exactly one function, :func:`urllib_transport`, so
tests inject a fake transport and never touch the network. Text is sent with ``parse_mode=HTML``
after escaping, split into ≤ 4,000-character chunks (Telegram's limit is 4,096).
"""

from __future__ import annotations

import html
import http.client
import json
import socket
import ssl
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from goldtrader.notify.redact import MASK, Redactor

MAX_MESSAGE_CHARS = 4000
MAX_CALLBACK_BYTES = 64
API_BASE = "https://api.telegram.org"
API_HOST = "api.telegram.org"
CONNECT_TIMEOUT_S = 3.0
CONNECT_ROUNDS = 2

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


# --- connection --------------------------------------------------------------------------------
# Some networks reset the TLS handshake to api.telegram.org on one address family and let the
# other through (seen on the owner's line: IPv4 0 of 8, IPv6 5 of 8). The default client tries one
# address and gives up. Here every resolved address is tried, the family that worked last goes
# first, and the list is walked twice, all inside the caller's time limit.
_last_good_family: dict[str, int] = {}


def _resolve(host: str, port: int = 443) -> list[tuple[int, tuple]]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    seen: set[tuple] = set()
    out: list[tuple[int, tuple]] = []
    for family, _type, _proto, _canon, addr in infos:
        if addr not in seen:
            seen.add(addr)
            out.append((family, addr))
    return out


def order_addresses(
    addrs: list[tuple[int, tuple]], preferred: int | None
) -> list[tuple[int, tuple]]:
    """Preferred family first (the one that worked last, else IPv6), keeping resolver order."""
    first = preferred if preferred is not None else socket.AF_INET6
    return sorted(addrs, key=lambda a: 0 if a[0] == first else 1)


def _open_tls(host: str, deadline: float) -> ssl.SSLSocket:
    ctx = ssl.create_default_context()
    last: Exception = OSError("no address for " + host)
    addrs = order_addresses(_resolve(host), _last_good_family.get(host))
    for _round in range(CONNECT_ROUNDS):
        for family, addr in addrs:
            left = deadline - time.monotonic()
            if left <= 0.2:
                raise TimeoutError(f"no connection within the time limit (last: {last!r})")
            sock = socket.socket(family, socket.SOCK_STREAM)
            try:
                sock.settimeout(min(CONNECT_TIMEOUT_S, left))
                sock.connect(addr)
                tls = ctx.wrap_socket(sock, server_hostname=host)  # the handshake happens here
            except (OSError, ssl.SSLError) as e:
                last = e
                sock.close()
                continue
            _last_good_family[host] = family
            return tls
    raise last


def _post(host: str, path: str, body: bytes, timeout_s: float) -> tuple[int, str]:
    """One HTTPS POST of a JSON body. Returns (status, response text)."""
    deadline = time.monotonic() + timeout_s
    tls = _open_tls(host, deadline)
    conn = http.client.HTTPSConnection(host, timeout=max(1.0, deadline - time.monotonic()))
    conn.sock = tls
    try:
        tls.settimeout(max(1.0, deadline - time.monotonic()))
        conn.request(
            "POST",
            path,
            body=body,
            headers={"Content-Type": "application/json", "Connection": "close"},
        )
        resp = conn.getresponse()
        return resp.status, resp.read().decode("utf-8", "replace")
    finally:
        conn.close()


def urllib_transport(token: str, timeout_s: float = 10.0) -> Transport:
    """The only function that talks to the network. Errors come back as :class:`TelegramError`.

    The returned closure holds the token; nothing it raises or returns contains it.
    """
    redactor = Redactor([token])

    def call(method: str, payload: dict[str, Any]) -> dict[str, Any]:
        path = f"/bot{token}/{method}"
        body = json.dumps(payload).encode("utf-8")
        try:
            status, raw = _post(API_HOST, path, body, timeout_s)
        except (TimeoutError, OSError, http.client.HTTPException) as e:
            raise TelegramError(
                redactor.redact(f"telegram {method} failed: {type(e).__name__}: {e}")
            ) from None
        if status >= 400:
            desc, retry = _parse_error_body(raw)
            raise TelegramError(
                redactor.redact(f"telegram {method} failed: HTTP {status} {desc or ''}".strip()),
                status=status,
                retry_after_s=retry,
                description=redactor.redact(desc) if desc else None,
            )
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

"""The Telegram side of the signal reader (D-061): the owner's own account through Telethon.

A bot cannot read channels it was not added to, so this logs in as the owner (Telegram's official
API for third-party apps). The login file (`state/telegram_user.session`) is equivalent to the
account: it stays in the git-ignored state directory and is never printed or copied.

Read-only by construction: this module only connects, lists chats and listens. It contains no call
that posts, replies, forwards, reacts, joins, leaves or marks anything as read.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from goldtrader.signals.service import SignalService

log = logging.getLogger(__name__)

SESSION_NAME = "telegram_user"


class NotLoggedIn(RuntimeError):
    pass


def _client(state_dir: Path, api_id: int, api_hash: str, *, ipv6: bool = False) -> Any:
    from telethon import TelegramClient

    return TelegramClient(
        str(state_dir / SESSION_NAME),
        api_id,
        api_hash,
        use_ipv6=ipv6,
        device_model="goldtrader signal reader",
        app_version="1",
        receive_updates=True,
    )


def _ms(dt: Any) -> int:
    return int(dt.timestamp() * 1000)


def login(state_dir: Path, api_id: int, api_hash: str, *, ipv6: bool = False) -> str:
    """Interactive, in the owner's own terminal: Telethon asks for the phone number, the code
    Telegram sends and, if set, the two-step password. Returns the account's display name."""

    async def go() -> str:
        client = _client(state_dir, api_id, api_hash, ipv6=ipv6)
        await client.start()
        try:
            me = await client.get_me()
            return str(getattr(me, "first_name", None) or "account")
        finally:
            await client.disconnect()

    return asyncio.run(go())


def list_chats(
    state_dir: Path, api_id: int, api_hash: str, *, ipv6: bool = False
) -> list[dict[str, Any]]:
    """Channels and groups the account is in: id, name, username, kind."""

    async def go() -> list[dict[str, Any]]:
        client = _client(state_dir, api_id, api_hash, ipv6=ipv6)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise NotLoggedIn("run `goldctl signals-login` first")
            out = []
            async for d in client.iter_dialogs():
                if not (d.is_channel or d.is_group):
                    continue
                out.append(
                    {
                        "id": int(d.id),
                        "name": str(d.name or "").strip() or "(no name)",
                        "username": getattr(d.entity, "username", None),
                        "kind": "group" if d.is_group else "channel",
                    }
                )
            return out
        finally:
            await client.disconnect()

    return asyncio.run(go())


def run(
    svc: SignalService,
    state_dir: Path,
    api_id: int,
    api_hash: str,
    *,
    heartbeat: Callable[[str], None],
    ipv6: bool = False,
    minutes: float | None = None,
    tick_s: float = 30.0,
    poller: Any = None,
) -> None:
    """Listen until stopped. Never prompts: an expired login raises NotLoggedIn."""

    async def go() -> None:
        from telethon import events

        client = _client(state_dir, api_id, api_hash, ipv6=ipv6)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise NotLoggedIn("run `goldctl signals-login` first")
        watched = svc.watched()

        @client.on(events.NewMessage())
        async def _new(e: Any) -> None:
            if e.chat_id in watched:
                _safe(
                    svc.on_message,
                    int(e.chat_id),
                    int(e.id),
                    _ms(e.message.date),
                    e.raw_text or "",
                    edited=e.message.edit_date is not None,
                )

        @client.on(events.MessageEdited())
        async def _edit(e: Any) -> None:
            if e.chat_id in watched:
                _safe(svc.on_edit, int(e.chat_id), int(e.id), e.raw_text or "", _ms(e.message.date))

        @client.on(events.MessageDeleted())
        async def _del(e: Any) -> None:
            chat = int(e.chat_id) if e.chat_id is not None else None
            if chat is None or chat in watched:
                _safe(svc.on_delete, chat, [int(i) for i in e.deleted_ids])

        try:
            await client.catch_up()  # messages posted while the reader was off (kept, not scored)
        except Exception as ex:  # noqa: BLE001
            log.info("catch-up skipped: %s", type(ex).__name__)
        loop = asyncio.get_running_loop()
        end = loop.time() + minutes * 60 if minutes else None
        try:
            while end is None or loop.time() < end:
                watched.clear()
                watched.update(svc.watched())
                if poller is not None and poller.due():  # web sources (D-071), read-only
                    try:
                        fetched = await asyncio.to_thread(poller.fetch_all)
                        _safe(poller.apply, fetched)
                    except Exception as ex:  # noqa: BLE001
                        log.warning("web source poll failed: %s", type(ex).__name__)
                _safe(svc.score)
                ok = client.is_connected()
                _safe(heartbeat, "ok" if ok else "degraded")
                if not ok:
                    try:
                        await client.connect()
                    except Exception as ex:  # noqa: BLE001
                        log.warning("reconnect failed: %s", type(ex).__name__)
                await asyncio.sleep(tick_s)
        finally:
            await client.disconnect()

    asyncio.run(go())


def _safe(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    try:
        fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 - one bad message must not stop the reader
        log.exception("signal reader step failed: %s", getattr(fn, "__name__", "?"))

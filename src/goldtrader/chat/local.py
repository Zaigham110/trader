"""Chat from the local dashboard (D-057): the same Chat bot as Telegram, questions only.

The dashboard process cannot call a model or the gateway. It only INSERTs the owner's text into
``dashboard_chat_inbox``. The brain polls that table through :class:`InboxUpdates`, runs the normal
:class:`ChatService` pipeline (history, tools, breaker, budget, post-processor) and the reply lands
in ``chat_messages`` under :data:`LOCAL_CHAT_ID`, where the dashboard reads it.

Nothing can be changed from the dashboard: a prepared action (``/pause``, ``/kill`` or one the
model prepared) is cancelled at once and the owner is told to use Telegram or ``goldctl``.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from goldtrader.chat.telegram_loop import ChatService
from goldtrader.notify.confirm import PendingAction

LOCAL_CHAT_ID = 0  # Telegram never uses chat id 0
MAX_BATCH = 5
NO_ACTIONS_TEXT = (
    "Not available here: {summary}\nThe dashboard can only answer questions. Nothing was done. "
    "To do this, use the same command in Telegram or `goldctl` on this PC."
)


class InboxUpdates:
    """``dashboard_chat_inbox`` rows shaped like Telegram updates (update_id = row id)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.c = conn

    def get_updates(self, offset: int | None, timeout_s: int) -> list[dict[str, Any]]:
        rows = self.c.execute(
            "SELECT id, text FROM dashboard_chat_inbox WHERE id >= ? ORDER BY id LIMIT ?",
            (int(offset or 0), MAX_BATCH),
        ).fetchall()
        return [
            {
                "update_id": int(r["id"]),
                "message": {
                    "message_id": int(r["id"]),
                    "chat": {"id": LOCAL_CHAT_ID, "type": "private"},
                    "from": {"id": LOCAL_CHAT_ID, "is_bot": False},
                    "text": r["text"],
                },
            }
            for r in rows
        ]

    def answer_callback(self, callback_id: str, text: str = "") -> None:  # no buttons here
        return None


class NullSender:
    """Replies are stored in ``chat_messages`` by ChatService; there is nothing to transmit."""

    def send(self, chat_id: int | str, text: str, *, buttons: Any = None) -> str:
        return ""

    def edit_buttons(self, chat_id: int | str, message_id: str, buttons: Any) -> None:
        return None


class DashboardChatService(ChatService):
    """ChatService over the dashboard inbox, with every state change refused."""

    offset_cursor = "dashboard_chat_offset"

    def __init__(
        self,
        cfg: Any,
        conn: sqlite3.Connection,
        provider: Any,
        store: Any,
        gateway: Any,
        clock_ms: Any,
        features_fn: Any = None,
        **kw: Any,
    ) -> None:
        super().__init__(
            cfg,
            conn,
            provider,
            store,
            gateway,
            NullSender(),  # type: ignore[arg-type]
            InboxUpdates(conn),
            LOCAL_CHAT_ID,
            clock_ms,
            features_fn,
            **kw,
        )

    def _sweep_expired(self, now: int) -> None:
        return None  # pending Telegram confirmations belong to the Telegram loop

    def _send_buttons(self, chat_id: int, action: PendingAction) -> None:
        self.confirm.cancel(action.action_id, self.clock_ms())
        self._send(chat_id, NO_ACTIONS_TEXT.format(summary=action.summary))

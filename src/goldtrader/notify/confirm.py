"""Confirm-button state machine primitives (AGENTS.md §3.5, ARCHITECTURE.md §6).

Every state-changing owner command — ``/kill`` and its reset (RISK_POLICY.md §3, R-051), pause,
resume, close position, config value — is first written as a :class:`PendingAction` and executed
only after the owner presses Confirm within the TTL (2 minutes by default). The button carries
``cfm:<action_id>:<token>``; the token is random, never part of the summary text and never shown to
a model. :meth:`ConfirmStore.confirm` is a single conditional ``UPDATE`` so a race between the
expiry sweep and a button press, or two presses, can never execute an action twice: the second
caller gets ``already=True`` and must not act.
"""

from __future__ import annotations

import hmac
import json
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

from goldtrader.notify.telegram import Button

DEFAULT_TTL_S = 120
Status = Literal["pending", "confirmed", "cancelled", "expired"]
CallbackKind = Literal["confirm", "cancel"]

_CONFIRM_PREFIX = "cfm"
_CANCEL_PREFIX = "cxl"


@dataclass(frozen=True)
class PendingAction:
    action_id: str
    kind: str
    params_json: str
    summary: str
    created_ms: int
    expires_ms: int
    status: str
    token: str
    resolved_ms: int | None = None
    chat_id: int | None = None
    message_id: str | None = None

    @property
    def params(self) -> dict[str, Any]:
        return json.loads(self.params_json)

    def buttons(self) -> list[list[Button]]:
        """Confirm / Cancel row for the Telegram message. Only here does the token leave the DB."""
        return [
            [
                Button("Confirm", confirm_callback(self.action_id, self.token)),
                Button("Cancel", cancel_callback(self.action_id)),
            ]
        ]


@dataclass(frozen=True)
class ConfirmResult:
    """Outcome of :meth:`ConfirmStore.confirm`. Execute only when ``already`` is ``False``."""

    action: PendingAction
    already: bool


class ConfirmError(RuntimeError):
    """A confirmation could not be applied. ``reason`` ∈ unknown | bad_token | expired | cancelled."""

    def __init__(self, reason: str, action: PendingAction | None = None):
        super().__init__(f"confirmation refused: {reason}")
        self.reason = reason
        self.action = action


def confirm_callback(action_id: str, token: str) -> str:
    return f"{_CONFIRM_PREFIX}:{action_id}:{token}"


def cancel_callback(action_id: str) -> str:
    return f"{_CANCEL_PREFIX}:{action_id}"


def parse_callback(data: str) -> tuple[CallbackKind, str, str | None]:
    """Parse button callback data into ``('confirm' | 'cancel', action_id, token | None)``.

    Raises ``ValueError`` for anything else, so unrelated callbacks are ignored by the caller.
    """
    parts = data.split(":")
    if len(parts) == 3 and parts[0] == _CONFIRM_PREFIX and parts[1] and parts[2]:
        return "confirm", parts[1], parts[2]
    if len(parts) == 2 and parts[0] == _CANCEL_PREFIX and parts[1]:
        return "cancel", parts[1], None
    raise ValueError(f"not a confirmation callback: {data!r}")


def _row_to_action(row: sqlite3.Row) -> PendingAction:
    return PendingAction(
        action_id=row["action_id"],
        kind=row["kind"],
        params_json=row["params_json"],
        summary=row["summary"],
        created_ms=int(row["created_ms"]),
        expires_ms=int(row["expires_ms"]),
        status=row["status"],
        token=row["token"],
        resolved_ms=row["resolved_ms"],
        chat_id=row["chat_id"],
        message_id=row["message_id"],
    )


class ConfirmStore:
    """``pending_confirmations`` access (migration 003). All times are epoch ms passed by the caller."""

    def __init__(self, conn: sqlite3.Connection):
        self.c = conn

    def create(
        self,
        kind: str,
        params: dict[str, Any],
        summary: str,
        ttl_s: int = DEFAULT_TTL_S,
        now_ms: int | None = None,
        *,
        chat_id: int | None = None,
    ) -> PendingAction:
        """Write a new pending action. ``summary`` is the owner-facing text; it never holds the token."""
        if now_ms is None:
            raise ValueError("now_ms is required (callers pass the clock explicitly)")
        if ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        action_id = secrets.token_hex(6)
        token = secrets.token_hex(8)  # 16 hex chars
        params_json = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
        if token in summary or token in params_json:
            raise ValueError("token leaked into summary/params")  # pragma: no cover - random
        with self.c:
            self.c.execute(
                "INSERT INTO pending_confirmations(action_id, kind, params_json, summary, created_ms, "
                "expires_ms, status, token, chat_id) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    action_id,
                    kind,
                    params_json,
                    summary,
                    now_ms,
                    now_ms + ttl_s * 1000,
                    "pending",
                    token,
                    chat_id,
                ),
            )
        action = self.get(action_id)
        assert action is not None
        return action

    def get(self, action_id: str) -> PendingAction | None:
        row = self.c.execute(
            "SELECT * FROM pending_confirmations WHERE action_id=?", (action_id,)
        ).fetchone()
        return _row_to_action(row) if row else None

    def set_message(self, action_id: str, chat_id: int, message_id: str) -> None:
        """Remember where the buttons were rendered so they can be removed after resolution."""
        with self.c:
            self.c.execute(
                "UPDATE pending_confirmations SET chat_id=?, message_id=? WHERE action_id=?",
                (chat_id, message_id, action_id),
            )

    def confirm(self, action_id: str, token: str, now_ms: int) -> ConfirmResult:
        """Atomically move ``pending`` → ``confirmed`` when the token matches and the TTL holds.

        Returns ``ConfirmResult(action, already=False)`` exactly once per action; a repeated press
        on an already-confirmed action returns ``already=True``. A wrong token, a cancelled,
        expired or unknown action raises :class:`ConfirmError`. The caller executes the action only
        on ``already=False``.
        """
        with self.c:
            cur = self.c.execute(
                "UPDATE pending_confirmations SET status='confirmed', resolved_ms=? "
                "WHERE action_id=? AND token=? AND status='pending' AND expires_ms > ?",
                (now_ms, action_id, token, now_ms),
            )
            won = cur.rowcount == 1
        action = self.get(action_id)
        if action is None:
            raise ConfirmError("unknown")
        if not hmac.compare_digest(action.token, token):
            raise ConfirmError("bad_token", action)
        if won:
            return ConfirmResult(action, already=False)
        if action.status == "confirmed":
            return ConfirmResult(action, already=True)
        if action.status == "pending" and action.expires_ms <= now_ms:
            self.expire_due(now_ms)
            action = self.get(action_id) or action
            raise ConfirmError("expired", action)
        raise ConfirmError(action.status, action)

    def cancel(self, action_id: str, now_ms: int) -> PendingAction:
        """Cancel a pending action (no token needed: cancelling is always safe). Idempotent."""
        with self.c:
            self.c.execute(
                "UPDATE pending_confirmations SET status='cancelled', resolved_ms=? "
                "WHERE action_id=? AND status='pending'",
                (now_ms, action_id),
            )
        action = self.get(action_id)
        if action is None:
            raise ConfirmError("unknown")
        return action

    def expire_due(self, now_ms: int) -> int:
        """Mark every pending action past its TTL as ``expired``; return how many."""
        with self.c:
            cur = self.c.execute(
                "UPDATE pending_confirmations SET status='expired', resolved_ms=? "
                "WHERE status='pending' AND expires_ms <= ?",
                (now_ms, now_ms),
            )
        return int(cur.rowcount)

    def list_pending(self, now_ms: int | None = None) -> list[PendingAction]:
        q = "SELECT * FROM pending_confirmations WHERE status='pending'"
        args: list[Any] = []
        if now_ms is not None:
            q += " AND expires_ms > ?"
            args.append(now_ms)
        q += " ORDER BY created_ms"
        return [_row_to_action(r) for r in self.c.execute(q, args)]

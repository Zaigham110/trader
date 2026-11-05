"""Telegram long-polling loop of the chat assistant (ARCHITECTURE.md §2.2 ``telegram`` row, §6;
AGENTS.md §3.5; PRD R-040, R-043; D-021, D-039).

* :class:`HttpTelegramUpdates` adds ``getUpdates`` / ``answerCallbackQuery`` on top of the existing
  Bot API transport (``goldtrader.notify.telegram.urllib_transport``, the one function that talks
  to the network); :class:`FakeTelegramUpdates` is the test double.
* :class:`ChatService.poll_once` drains one batch of updates. Anything not from the owner chat is
  dropped (only a count is logged, R-040). Slash commands are code (no model): ``/status``,
  ``/help``, ``/pause``, ``/resume``, ``/kill``, ``/close <position_id>``. Free text runs the Chat
  bot under its own circuit breaker and the shared LLM budget; the reply passes
  :func:`goldtrader.chat.agent.postprocess` before it is sent. Button presses (``cfm:`` / ``cxl:``)
  go through :class:`ConfirmStore` and, once confirmed, :func:`execute_confirmed`; the buttons are
  edited away and the result is sent. Expired actions are reported as expired.
* Degraded mode: no provider or an open breaker → commands keep working (they are code); free text
  gets a fixed message listing the commands.
* Every inbound and outbound message is stored in ``chat_messages`` with role, redacted text, the
  tools used and the run id. The getUpdates offset lives in ``notify_cursors``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from goldtrader.chat.actions import ActionContext, ActionError, execute_confirmed, prepare_action
from goldtrader.chat.agent import (
    ChatReply,
    ChatTurn,
    ToolTracker,
    chat_spec,
    is_deep_request,
    load_history,
    postprocess,
)
from goldtrader.chat.tools import ToolContext, iso
from goldtrader.llm.breaker import CircuitBreaker
from goldtrader.llm.budget import Budget
from goldtrader.llm.provider import ModelProvider
from goldtrader.llm.runtime import run_agent
from goldtrader.llm.store import LLMStore
from goldtrader.notify.confirm import ConfirmError, ConfirmStore, PendingAction, parse_callback
from goldtrader.notify.redact import Redactor
from goldtrader.notify.telegram import TelegramSender, Transport, urllib_transport

log = logging.getLogger("goldtrader.chat")

OFFSET_CURSOR = "chat_update_offset"
LONG_POLL_S = 25

HELP_TEXT = (
    "Commands (code, no AI):\n"
    "/status — account, session and safety flags from the gateway\n"
    "/pause — prepare: block new entries (confirm button)\n"
    "/resume — prepare: allow entries again (confirm button)\n"
    "/kill — prepare: KILL SWITCH, close everything (confirm button)\n"
    "/close <position_id> — prepare: close one position (confirm button)\n"
    "/deep <question> — ask with the deep model tier\n"
    "/help — this list\n"
    "Any other text is answered by the assistant from read-only data with sources. "
    "Nothing changes without a Confirm press within the timeout."
)
UNAVAILABLE_TEXT = (
    "The assistant is unavailable right now ({reason}). Commands still work:\n" + HELP_TEXT
)


# --- transport ------------------------------------------------------------------------------------
class TelegramUpdates(Protocol):
    """What the loop needs from the Bot API beyond sending."""

    def get_updates(self, offset: int | None, timeout_s: int) -> list[dict[str, Any]]:
        """``getUpdates`` with long polling; returns the raw update dicts."""
        ...

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        """``answerCallbackQuery`` so the button stops spinning."""
        ...


class HttpTelegramUpdates:
    """Bot API ``getUpdates`` / ``answerCallbackQuery``. ``transport`` defaults to the stdlib one in
    ``goldtrader.notify.telegram`` (network code stays in that single function)."""

    def __init__(
        self, token: str, *, transport: Transport | None = None, long_poll_s: int = LONG_POLL_S
    ) -> None:
        if not token:
            raise ValueError("telegram token is required")
        self.long_poll_s = long_poll_s
        self._transport = transport or urllib_transport(token, timeout_s=long_poll_s + 10.0)

    def __repr__(self) -> str:  # never show the token
        return "HttpTelegramUpdates(token='***')"

    def get_updates(self, offset: int | None, timeout_s: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": int(max(0, min(timeout_s, self.long_poll_s))),
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = int(offset)
        data = self._transport("getUpdates", payload)
        res = data.get("result") or []
        return [u for u in res if isinstance(u, dict)]

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:200]
        self._transport("answerCallbackQuery", payload)


@dataclass
class FakeTelegramUpdates:
    """Test double: queue updates with :meth:`push`; records offsets and answered callbacks."""

    queue: list[dict[str, Any]] = field(default_factory=list)
    offsets: list[int | None] = field(default_factory=list)
    answered: list[tuple[str, str]] = field(default_factory=list)
    _next_update_id: int = 1

    def push(self, update: dict[str, Any]) -> int:
        update = dict(update)
        update.setdefault("update_id", self._next_update_id)
        self._next_update_id = int(update["update_id"]) + 1
        self.queue.append(update)
        return int(update["update_id"])

    def push_message(self, chat_id: int, text: str, message_id: int = 100) -> int:
        return self.push(
            {
                "message": {
                    "message_id": message_id,
                    "chat": {"id": chat_id, "type": "private"},
                    "from": {"id": chat_id, "is_bot": False},
                    "text": text,
                }
            }
        )

    def push_callback(
        self, chat_id: int, data: str, message_id: str | int = 1, callback_id: str = "cb1"
    ) -> int:
        return self.push(
            {
                "callback_query": {
                    "id": callback_id,
                    "from": {"id": chat_id, "is_bot": False},
                    "message": {"message_id": message_id, "chat": {"id": chat_id}},
                    "data": data,
                }
            }
        )

    def get_updates(self, offset: int | None, timeout_s: int) -> list[dict[str, Any]]:
        self.offsets.append(offset)
        out = [u for u in self.queue if offset is None or int(u["update_id"]) >= offset]
        self.queue = [u for u in self.queue if u not in out]
        return out

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.answered.append((callback_id, text))


# --- service ----------------------------------------------------------------------------------------
def status_text(state: Any, now_ms: int) -> str:
    """Code-only ``/status`` summary from the gateway state (no model, R-041 source line)."""
    if state is None:
        return f"Gateway unavailable (as of {iso(now_ms)}). No account data; alerts still run."

    def n(v: Any, d: int = 2) -> str:
        return "n/a" if v is None else f"{v:.{d}f}"

    flags = []
    if state.kill_switch:
        flags.append("KILL SWITCH ON")
    if state.paused:
        flags.append("paused")
    if state.incident_open:
        flags.append("incident open")
    if not state.spec_ok:
        flags.append("spec changed")
    lines = [
        f"Status (source: gateway /state as of {iso(state.time_ms)})",
        f"session {state.session} · entries {'allowed' if state.entries_allowed else 'blocked'}"
        f" · connected {state.connected} · trade_allowed {state.trade_allowed}",
        f"equity {n(state.equity)} · balance {n(state.balance)} · day P&L {n(state.day_pnl)}"
        f" · margin level {n(state.margin_level, 0)}%",
        f"bid/ask {n(state.bid)} / {n(state.ask)} · spread {n(state.spread_pips, 1)} pips"
        f" · tick age {n(state.tick_age_s, 1)} s",
        f"open positions {state.open_positions}",
    ]
    for p in state.positions[:5]:
        lines.append(
            f"  #{p.get('ticket')} {p.get('side')} {p.get('volume')} @ {p.get('price_open')}"
            f" sl {p.get('sl')} tp {p.get('tp')} P&L {p.get('profit')}"
        )
    lines.append("flags: " + (", ".join(flags) if flags else "none"))
    return "\n".join(lines)


class ChatService:
    """Owner-only Telegram chat loop (see module docstring)."""

    def __init__(
        self,
        cfg: Any,
        conn: sqlite3.Connection,
        provider: ModelProvider | None,
        store: LLMStore,
        gateway: Any,
        sender: TelegramSender,
        updates: TelegramUpdates,
        owner_chat_id: int | None,
        clock_ms: Callable[[], int],
        features_fn: Callable[[], Any] | None = None,
        *,
        config_path: str | Path | None = None,
        secrets: list[str] | tuple[str, ...] = (),
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.c = conn
        self.provider = provider
        self.store = store
        self.gateway = gateway
        self.sender = sender
        self.updates = updates
        self.owner_chat_id = int(owner_chat_id) if owner_chat_id is not None else None
        self.clock_ms = clock_ms
        self.sleep = sleep
        self.secrets = tuple(s for s in secrets if s)
        self.redactor = Redactor(self.secrets)
        self.confirm = ConfirmStore(conn)
        self.tool_ctx = ToolContext(conn, gateway, features_fn, cfg, clock_ms)
        self.action_ctx = ActionContext(
            conn,
            gateway,
            cfg,
            clock_ms,
            self.confirm,
            Path(config_path) if config_path is not None else None,
            ttl_s=int(cfg.chat.confirm_ttl_s),
        )
        self.tracker = ToolTracker()
        self.breaker = CircuitBreaker("chat", store=store)
        self.budget = Budget(cfg.llm.daily_budget_usd, cfg.llm.monthly_budget_usd)
        self.specs = {
            False: chat_spec(cfg, self.tool_ctx, self.action_ctx, tracker=self.tracker),
            True: chat_spec(cfg, self.tool_ctx, self.action_ctx, deep=True, tracker=self.tracker),
        }
        store.record_prompt(self.specs[False].system_prompt)
        self.ignored_total = 0

    # --- offset cursor -----------------------------------------------------------------------------
    def offset(self) -> int | None:
        row = self.c.execute(
            "SELECT value FROM notify_cursors WHERE name=?", (OFFSET_CURSOR,)
        ).fetchone()
        return int(row["value"]) if row else None

    def _set_offset(self, value: int) -> None:
        self.c.execute(
            "INSERT INTO notify_cursors(name, value, updated_ms) VALUES (?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_ms=excluded.updated_ms",
            (OFFSET_CURSOR, int(value), self.clock_ms()),
        )

    # --- storage -------------------------------------------------------------------------------------
    def _record(
        self,
        chat_id: int,
        role: str,
        text: str,
        *,
        tools: list[str] | None = None,
        run_id: str | None = None,
        update_id: int | None = None,
    ) -> None:
        self.c.execute(
            "INSERT INTO chat_messages(created_ms, chat_id, role, text, tool_names_json, run_id, "
            "update_id) VALUES (?,?,?,?,?,?,?)",
            (
                self.clock_ms(),
                chat_id,
                role,
                self.redactor.redact(text),
                json.dumps(tools or []),
                run_id,
                update_id,
            ),
        )

    def _send(
        self,
        chat_id: int,
        text: str,
        *,
        buttons: Any = None,
        tools: list[str] | None = None,
        run_id: str | None = None,
    ) -> str:
        safe = self.redactor.redact(text)
        mid = self.sender.send(chat_id, safe, buttons=buttons)
        self._record(chat_id, "assistant", safe, tools=tools, run_id=run_id)
        return mid

    def _record_action(self, action: PendingAction, chat_id: int, prepared_by: str) -> None:
        self.c.execute(
            "INSERT OR IGNORE INTO chat_actions(action_id, created_ms, chat_id, kind, prepared_by, "
            "summary) VALUES (?,?,?,?,?,?)",
            (action.action_id, self.clock_ms(), chat_id, action.kind, prepared_by, action.summary),
        )

    # --- polling --------------------------------------------------------------------------------------
    def poll_once(self, timeout_s: int | None = None) -> list[str]:
        """Fetch one batch of updates and handle each; returns one outcome tag per update."""
        now = self.clock_ms()
        self._sweep_expired(now)
        wait = int(self.cfg.chat.poll_seconds if timeout_s is None else timeout_s)
        try:
            batch = self.updates.get_updates(self.offset(), wait)
        except Exception as e:  # noqa: BLE001 - network trouble is logged, never raised
            log.warning("chat getUpdates failed: %s", self.redactor.redact(repr(e)))
            return ["error:get_updates"]
        outcomes: list[str] = []
        ignored = 0
        last_id: int | None = None
        for upd in batch:
            uid = upd.get("update_id")
            if isinstance(uid, int):
                last_id = uid if last_id is None else max(last_id, uid)
            try:
                tag = self._handle(upd)
            except Exception as e:  # noqa: BLE001 - one bad update must not stall the loop
                log.exception("chat update failed: %s", self.redactor.redact(repr(e)))
                tag = "error:handler"
            if tag == "ignored:non_owner":
                ignored += 1
            outcomes.append(tag)
        if last_id is not None:
            self._set_offset(last_id + 1)
        if ignored:
            self.ignored_total += ignored
            log.info("chat: ignored %d update(s) not from the owner chat", ignored)
        return outcomes

    def _sweep_expired(self, now: int) -> None:
        due = [a for a in self.confirm.list_pending() if a.expires_ms <= now]
        if not due:
            return
        self.confirm.expire_due(now)
        for a in due:
            chat = a.chat_id if a.chat_id is not None else self.owner_chat_id
            if chat is None:
                continue
            if a.message_id:
                try:
                    self.sender.edit_buttons(chat, a.message_id, None)
                except Exception as e:  # noqa: BLE001
                    log.warning("edit_buttons failed: %s", self.redactor.redact(repr(e)))
            try:
                self._send(chat, f"Expired without confirmation: {a.summary} Nothing was done.")
            except Exception as e:  # noqa: BLE001
                log.warning("send failed: %s", self.redactor.redact(repr(e)))

    # --- dispatch -------------------------------------------------------------------------------------
    def _handle(self, upd: dict[str, Any]) -> str:
        update_id = upd.get("update_id") if isinstance(upd.get("update_id"), int) else None
        if "callback_query" in upd:
            return self._handle_callback(upd["callback_query"], update_id)
        msg = upd.get("message") or upd.get("edited_message")
        if not isinstance(msg, dict):
            return "ignored:unsupported"
        chat_id = _chat_id(msg)
        if chat_id is None or chat_id != self.owner_chat_id:
            return "ignored:non_owner"
        text = msg.get("text")
        if not isinstance(text, str) or not text.strip():
            self._send(chat_id, "I only read text messages.")
            return "ignored:non_text"
        text = text.strip()
        # History is read before the current message is stored so it is not replayed twice.
        history = load_history(self.c, chat_id, int(self.cfg.chat.history_turns))
        self._record(chat_id, "owner", text, update_id=update_id)
        if text.startswith("/") and not is_deep_request(text):
            return self._handle_command(chat_id, text)
        return self._handle_chat(chat_id, text, history)

    def _handle_command(self, chat_id: int, text: str) -> str:
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        args = parts[1:]
        if cmd == "/status":
            try:
                st = self.gateway.state()
            except Exception:  # noqa: BLE001
                st = None
            self._send(chat_id, status_text(st, self.clock_ms()))
            return "command:/status"
        if cmd in ("/help", "/start"):
            self._send(chat_id, HELP_TEXT)
            return "command:/help"
        if cmd in ("/pause", "/resume", "/kill"):
            return self._prepare_from_command(chat_id, cmd[1:], {})
        if cmd == "/close":
            if not args:
                self._send(chat_id, "Usage: /close <position_id>")
                return "command:/close:usage"
            return self._prepare_from_command(chat_id, "close_position", {"position_id": args[0]})
        self._send(chat_id, "Unknown command.\n" + HELP_TEXT)
        return "command:unknown"

    def _prepare_from_command(self, chat_id: int, kind: str, params: dict[str, Any]) -> str:
        try:
            prepared = prepare_action(self.action_ctx, kind, params, chat_id=chat_id)
        except ActionError as e:
            self._send(chat_id, f"Refused: {e}")
            return f"command:/{kind}:refused"
        action = self.confirm.get(prepared.action_id)
        assert action is not None
        self._record_action(action, chat_id, "command")
        self._send_buttons(chat_id, action)
        return f"command:/{kind}:prepared"

    def _send_buttons(self, chat_id: int, action: PendingAction) -> None:
        ttl = max(1, (action.expires_ms - self.clock_ms()) // 1000)
        text = f"Confirm within {ttl} s:\n{action.summary}"
        mid = self._send(chat_id, text, buttons=action.buttons())
        if mid:
            self.confirm.set_message(action.action_id, chat_id, mid)

    # --- free text → model ----------------------------------------------------------------------------
    def available(self, now_ms: int) -> tuple[bool, str]:
        if self.provider is None:
            return False, "no model provider configured"
        b = self.breaker
        if b.state != "closed" and not (
            b.state == "open" and b.next_trial_ms is not None and now_ms >= b.next_trial_ms
        ):
            return False, "assistant circuit breaker is open"
        why = self.budget.refusal(self.store, now_ms)
        if why is not None:
            return False, why
        return True, ""

    def _handle_chat(self, chat_id: int, text: str, history: list[dict[str, Any]]) -> str:
        now = self.clock_ms()
        ok, why = self.available(now)
        if not ok:
            self._send(chat_id, UNAVAILABLE_TEXT.format(reason=why))
            return "chat:unavailable"
        deep = is_deep_request(text)
        spec = self.specs[deep]
        turn = ChatTurn(chat_id=chat_id, text=text, now_ms=now, history=history)
        before = list(self.action_ctx.prepared)
        kwargs: dict[str, Any] = {}
        if self.sleep is not None:
            kwargs["sleep"] = self.sleep
        assert self.provider is not None
        run = run_agent(
            spec,
            turn,
            self.provider,
            self.store,
            now_ms=now,
            breaker=self.breaker,
            budget=self.budget,
            **kwargs,
        )
        tools_used = list(self.tracker.calls)
        new_actions = [a for a in self.action_ctx.prepared if a not in before]
        if run.status == "ok" and isinstance(run.output, ChatReply):
            reply, rejected = postprocess(run.output, tools_used, self.secrets)
            if rejected:
                log.info("chat reply rejected by post-processor: %s", rejected)
            self._send(chat_id, reply.text, tools=tools_used, run_id=run.run_id)
            tag = "chat:ok" if not rejected else "chat:rejected"
        else:
            reason = run.failure_reason or run.status
            log.warning("chat run %s: %s", run.status, reason)
            self._send(
                chat_id,
                UNAVAILABLE_TEXT.format(reason=f"run {run.status}"),
                tools=tools_used,
                run_id=run.run_id,
            )
            tag = f"chat:{run.status}"
        for action_id in new_actions:
            action = self.confirm.get(action_id)
            if action is None or action.status != "pending":
                continue
            self._record_action(action, chat_id, f"agent:{run.run_id}")
            self._send_buttons(chat_id, action)
        return tag

    # --- buttons --------------------------------------------------------------------------------------
    def _handle_callback(self, cb: dict[str, Any], update_id: int | None) -> str:
        cb_id = str(cb.get("id", ""))
        from_id = (cb.get("from") or {}).get("id")
        msg = cb.get("message") or {}
        chat_id = _chat_id(msg)
        if from_id is None or int(from_id) != self.owner_chat_id or chat_id != self.owner_chat_id:
            self._answer(cb_id, "")
            return "ignored:non_owner"
        data = str(cb.get("data", ""))
        message_id = str(msg.get("message_id", "")) or None
        try:
            kind, action_id, token = parse_callback(data)
        except ValueError:
            self._answer(cb_id, "Unknown button.")
            return "callback:unknown"
        now = self.clock_ms()
        self._record(chat_id, "owner", f"[button {kind} {action_id}]", update_id=update_id)
        if kind == "cancel":
            try:
                action = self.confirm.cancel(action_id, now)
            except ConfirmError as e:
                self._answer(cb_id, "Unknown action.")
                self._send(chat_id, f"Cannot cancel: {e.reason}.")
                return "callback:cancel:unknown"
            self._edit_away(chat_id, message_id or action.message_id)
            self._answer(cb_id, "Cancelled.")
            self._send(chat_id, f"Cancelled ({action.status}): {action.summary} Nothing was done.")
            return "callback:cancelled"

        assert token is not None
        try:
            res = self.confirm.confirm(action_id, token, now)
        except ConfirmError as e:
            self._edit_away(chat_id, message_id or (e.action.message_id if e.action else None))
            if e.reason == "expired":
                self._answer(cb_id, "Expired.")
                self._send(
                    chat_id,
                    "That confirmation expired; nothing was done. Send the command again.",
                )
                return "callback:expired"
            self._answer(cb_id, "Refused.")
            self._send(chat_id, f"Confirmation refused ({e.reason}); nothing was done.")
            return f"callback:{e.reason}"
        if res.already:
            self._answer(cb_id, "Already done.")
            self._send(chat_id, "Already confirmed earlier; not executing twice.")
            return "callback:already"
        self._edit_away(chat_id, message_id or res.action.message_id)
        self._answer(cb_id, "Confirmed.")
        try:
            line = execute_confirmed(res.action, self.action_ctx)
            outcome = "callback:confirmed"
        except Exception as e:  # noqa: BLE001 - report, never crash the loop
            line = f"{res.action.kind}: FAILED — {self.redactor.redact(str(e))[:300]}"
            outcome = "callback:failed"
        self.c.execute(
            "UPDATE chat_actions SET executed_ms=?, result=? WHERE action_id=?",
            (self.clock_ms(), self.redactor.redact(line), action_id),
        )
        self._send(chat_id, f"Confirmed → {line}")
        return outcome

    def _edit_away(self, chat_id: int, message_id: str | None) -> None:
        if not message_id:
            return
        try:
            self.sender.edit_buttons(chat_id, str(message_id), None)
        except Exception as e:  # noqa: BLE001
            log.warning("edit_buttons failed: %s", self.redactor.redact(repr(e)))

    def _answer(self, cb_id: str, text: str) -> None:
        if not cb_id:
            return
        try:
            self.updates.answer_callback(cb_id, text)
        except Exception as e:  # noqa: BLE001
            log.warning("answerCallbackQuery failed: %s", self.redactor.redact(repr(e)))


def _chat_id(msg: dict[str, Any]) -> int | None:
    chat = msg.get("chat") if isinstance(msg, dict) else None
    cid = chat.get("id") if isinstance(chat, dict) else None
    try:
        return int(cid) if cid is not None else None
    except (TypeError, ValueError):
        return None

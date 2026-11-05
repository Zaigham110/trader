"""Prepared actions of the Chat bot (AGENTS.md §3.5 "Action tools (prepare only)"; PRD R-043;
RISK_POLICY.md §3–4; D-021, D-039).

Two halves, deliberately separated:

* :func:`prepare_action` — validates the request, writes a :class:`PendingAction` through
  :class:`ConfirmStore` (the owner-facing summary never contains the token) and returns the summary
  plus ``action_id``. It changes **nothing** else. ``set_config_value`` refuses every key under
  ``owner.`` (RISK_POLICY §4) and validates the new value by applying it to a copy of the current
  config through :func:`parse_config`, so an invalid value is rejected before any button appears.
* :func:`execute_confirmed` — runs **only** after the Telegram layer has confirmed the action
  (``ConfirmStore.confirm`` returned ``already=False``). pause / resume / kill / close_position
  become an :class:`Intent` with ``source="owner_confirmed"`` sent to the gateway, which re-runs
  its own risk engine (R-043); ``set_config_value`` rewrites the YAML atomically and records a
  ``config_versions`` row. Nothing else in the chat package can change state.

The model never sees the confirmation token: ``prepare_action`` returns the summary and the id only.
"""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from goldtrader.bots.common import defang
from goldtrader.config.loader import ConfigError, config_hash, owner_hash, parse_config
from goldtrader.contracts import Intent
from goldtrader.llm.provider import ToolSpec
from goldtrader.llm.runtime import Tool
from goldtrader.notify.confirm import ConfirmStore, PendingAction

ACTION_TYPES: tuple[str, ...] = ("pause", "resume", "kill", "close_position", "set_config_value")
"""Everything the chat may *prepare*. ``kill_reset`` is deliberately absent: RISK_POLICY §3 makes it
a dedicated owner command after the incident is acknowledged, outside the chat assistant."""

OWNER_ONLY_REASON = (
    "owner-only: the `owner` section (risk limits, permissions, safety, profile, mode) cannot be "
    "changed through chat. The owner edits the config file by hand; see RISK_POLICY.md §4."
)
MAX_REASON_CHARS = 200


@dataclass
class ActionContext:
    """What preparing and executing an action may reach."""

    conn: sqlite3.Connection
    gateway: Any  # GatewayClient-like: state() / send_intent()
    cfg: Any
    clock_ms: Callable[[], int]
    confirm: ConfirmStore
    config_path: Path | None = None
    ttl_s: int = 120
    prepared: list[str] = field(default_factory=list)
    """action_ids prepared through this context (the Telegram layer renders their buttons)."""

    @property
    def now_ms(self) -> int:
        return self.clock_ms()


@dataclass(frozen=True)
class Prepared:
    action_id: str
    kind: str
    summary: str
    expires_ms: int

    def as_tool_result(self) -> dict[str, Any]:
        return {
            "status": "pending_confirmation",
            "action_id": self.action_id,
            "type": self.kind,
            "summary": self.summary,
            "expires_at_ms": self.expires_ms,
            "note": "Nothing has happened yet. The owner must press Confirm within the timeout.",
        }


class ActionError(ValueError):
    """The request cannot be prepared; ``str(e)`` is safe to show the owner and the model."""


# --- config helpers ------------------------------------------------------------------------------
def _current_raw(ctx: ActionContext) -> dict[str, Any]:
    """The YAML on disk when there is one (the owner's hand-edited source), else the in-memory dump."""
    if ctx.config_path is not None and Path(ctx.config_path).exists():
        with Path(ctx.config_path).open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            raise ActionError("config root must be a mapping")
        return raw
    return ctx.cfg.model_dump(mode="json")


def _split_key(key: Any) -> list[str]:
    if not isinstance(key, str) or not key.strip():
        raise ActionError("key must be a dotted path such as session.pre_break_buffer_min")
    parts = [p for p in key.strip().split(".") if p]
    if not parts or any(not p.replace("_", "").isalnum() for p in parts):
        raise ActionError(f"invalid key {key!r}")
    return parts


def _get_path(obj: Any, parts: list[str]) -> Any:
    cur = obj
    for p in parts:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        elif isinstance(cur, list) and p.isdigit() and int(p) < len(cur):
            cur = cur[int(p)]
        else:
            return None
    return cur


def _set_path(obj: dict[str, Any], parts: list[str], value: Any) -> None:
    cur: Any = obj
    for p in parts[:-1]:
        if isinstance(cur, list) and p.isdigit():
            cur = cur[int(p)]
            continue
        if not isinstance(cur, dict):
            raise ActionError(f"cannot descend into {'.'.join(parts)}")
        cur = cur.setdefault(p, {})
    last = parts[-1]
    if isinstance(cur, list) and last.isdigit():
        cur[int(last)] = value
    elif isinstance(cur, dict):
        cur[last] = value
    else:
        raise ActionError(f"cannot set {'.'.join(parts)}")


def apply_config_value(ctx: ActionContext, key: str, value: Any) -> tuple[dict[str, Any], Any, Any]:
    """Apply ``key = value`` to a copy of the current raw config and validate it.

    Returns ``(new_raw, old_value, canonical_new_value)``. Raises :class:`ActionError` for an
    owner-section key (RISK_POLICY §4), an unknown key or a value the schema / ceilings reject.
    """
    parts = _split_key(key)
    if parts[0] == "owner":
        raise ActionError(OWNER_ONLY_REASON)
    if parts[0] == "version":
        raise ActionError("the config version is managed by the loader, not by chat")
    raw = _current_raw(ctx)
    new_raw = copy.deepcopy(raw)
    _set_path(new_raw, parts, value)
    try:
        new_cfg = parse_config(new_raw)
    except ConfigError as e:
        raise ActionError(f"rejected by the config schema: {_short(str(e))}") from None
    if owner_hash(new_cfg) != owner_hash(ctx.cfg):
        raise ActionError(OWNER_ONLY_REASON)  # pragma: no cover - defended above as well
    dump = new_cfg.model_dump(mode="json")
    canonical = _get_path(dump, parts)
    old_value = _get_path(ctx.cfg.model_dump(mode="json"), parts)
    if canonical is None and value is not None:
        raise ActionError(f"unknown config key {key!r}")
    # Store the schema's canonical value (e.g. "10" → 10) so the YAML stays typed.
    _set_path(new_raw, parts, canonical)
    return new_raw, old_value, canonical


def _short(text: str, n: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def write_config_atomic(path: Path, raw: dict[str, Any]) -> None:
    """Write YAML to a temp file in the same directory and ``os.replace`` it into place."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(raw, sort_keys=False, default_flow_style=False, allow_unicode=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- prepare ---------------------------------------------------------------------------------------
def _reason(params: dict[str, Any]) -> str:
    return defang(params.get("reason", ""), MAX_REASON_CHARS)


def _position_ids(ctx: ActionContext) -> list[int] | None:
    """Tickets of the open positions per the gateway, or None when the gateway is unreachable."""
    try:
        st = ctx.gateway.state()
    except Exception:  # noqa: BLE001
        return None
    if st is None:
        return None
    out: list[int] = []
    for p in st.positions:
        t = p.get("ticket") if isinstance(p, dict) else None
        if t is not None:
            out.append(int(t))
    return out


def prepare_action(
    ctx: ActionContext,
    action_type: str,
    params: dict[str, Any] | None = None,
    *,
    chat_id: int | None = None,
) -> Prepared:
    """Validate and write a pending confirmation; execute nothing (R-043, AGENTS §3.5).

    Raises :class:`ActionError` with an owner-readable reason when the request is refused.
    """
    params = dict(params or {})
    now = ctx.now_ms
    if action_type not in ACTION_TYPES:
        raise ActionError(f"unknown action type {action_type!r}; one of {', '.join(ACTION_TYPES)}")
    reason = _reason(params)
    stored: dict[str, Any] = {"reason": reason} if reason else {}

    if action_type == "pause":
        summary = "Pause: block new entries (open positions and their stops stay managed)."
    elif action_type == "resume":
        summary = "Resume: allow new entries again (owner-only intent)."
    elif action_type == "kill":
        summary = (
            "KILL SWITCH: close every position at market, cancel pending orders and block all "
            "entries until the owner resets it (RISK_POLICY §3)."
        )
    elif action_type == "close_position":
        pid_raw = params.get("position_id")
        try:
            pid = int(pid_raw)
        except (TypeError, ValueError):
            raise ActionError("close_position needs an integer position_id") from None
        if pid <= 0:
            raise ActionError("position_id must be positive")
        known = _position_ids(ctx)
        if known is not None and pid not in known:
            raise ActionError(
                f"position {pid} is not open according to the gateway (open: {known or 'none'})"
            )
        stored["position_id"] = pid
        summary = f"Close position {pid} at market (reduce-only exit through the gateway)."
    else:  # set_config_value
        key = params.get("key")
        if "value" not in params:
            raise ActionError("set_config_value needs key and value")
        new_raw, old, new = apply_config_value(ctx, key, params["value"])
        key_s = ".".join(_split_key(key))
        stored.update({"key": key_s, "value": new})
        before = config_hash(ctx.cfg)
        after = config_hash(parse_config(new_raw))
        summary = (
            f"Set config {key_s}: {json.dumps(old)} → {json.dumps(new)} "
            f"(config hash {before} → {after}). Takes effect on the next config reload."
        )
    if reason:
        summary += f" Reason: {reason}"

    action = ctx.confirm.create(
        action_type, stored, summary, ttl_s=ctx.ttl_s, now_ms=now, chat_id=chat_id
    )
    ctx.prepared.append(action.action_id)
    return Prepared(action.action_id, action.kind, action.summary, action.expires_ms)


# --- execute --------------------------------------------------------------------------------------
_INTENT_KIND = {"pause": "pause", "resume": "resume", "kill": "kill", "close_position": "exit"}


def execute_confirmed(action: PendingAction, ctx: ActionContext) -> str:
    """Run a **confirmed** action; return the owner-facing result line.

    The caller guarantees ``action.status == "confirmed"`` and that it won the confirm race
    (``ConfirmResult.already is False``). Intents carry ``source="owner_confirmed"`` so the gateway
    accepts owner-only kinds (resume, kill) and journals who asked (R-043, D-039).
    """
    if action.status != "confirmed":
        raise ActionError(f"action {action.action_id} is {action.status}, not confirmed")
    params = action.params
    now = ctx.now_ms
    if action.kind in _INTENT_KIND:
        kind = _INTENT_KIND[action.kind]
        intent = Intent(
            intent_id=f"chat-{action.action_id}",
            kind=kind,
            created_ms=now,
            position_id=params.get("position_id") if kind == "exit" else None,
            reason=(params.get("reason") or f"owner confirmed {action.kind} via Telegram")[:300],
            source="owner_confirmed",
        )
        res = ctx.gateway.send_intent(intent)
        line = f"{action.kind}: {res.status}"
        if res.risk_code:
            line += f" ({res.risk_code})"
        if res.detail:
            line += f" — {defang(res.detail, 200)}"
        return line
    if action.kind == "set_config_value":
        if ctx.config_path is None:
            raise ActionError("no config path configured; cannot write the config file")
        new_raw, old, new = apply_config_value(ctx, params["key"], params["value"])
        new_cfg = parse_config(new_raw)
        write_config_atomic(Path(ctx.config_path), new_raw)
        h = config_hash(new_cfg)
        ctx.conn.execute(
            "INSERT OR IGNORE INTO config_versions(config_hash, owner_hash, source, body_json, "
            "created_ms) VALUES (?,?,?,?,?)",
            (
                h,
                owner_hash(new_cfg),
                "telegram",
                json.dumps(new_cfg.model_dump(mode="json"), sort_keys=True, default=str),
                now,
            ),
        )
        return (
            f"config updated: {params['key']} = {json.dumps(new)} (was {json.dumps(old)}); "
            f"new config hash {h}. It applies on the next config reload."
        )
    raise ActionError(f"unknown action kind {action.kind!r}")


# --- the model-facing tool --------------------------------------------------------------------------
PREPARE_ACTION_SPEC = ToolSpec(
    "prepare_action",
    "Prepare (never execute) a state change for the owner to confirm with a button: pause, "
    "resume, kill, close_position (params.position_id) or set_config_value (params.key dotted "
    "path, params.value). Keys under `owner.` are refused. Returns a summary and action_id; "
    "tell the owner a confirmation was prepared, never that the action happened.",
    {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": list(ACTION_TYPES)},
            "params": {
                "type": "object",
                "properties": {
                    "position_id": {"type": "integer", "minimum": 1},
                    "key": {"type": "string", "minLength": 1, "maxLength": 120},
                    "value": {"type": ["string", "number", "boolean", "null"]},
                    "reason": {"type": "string", "maxLength": MAX_REASON_CHARS},
                },
                "required": [],
                "additionalProperties": False,
            },
        },
        "required": ["type"],
        "additionalProperties": False,
    },
)


def prepare_action_tool(ctx: ActionContext, chat_id_fn: Callable[[], int | None]) -> Tool:
    """The ``prepare_action`` tool bound to ``ctx``; errors come back as data for the model."""

    def handler(inp: dict[str, Any]) -> dict[str, Any]:
        try:
            prepared = prepare_action(
                ctx, str(inp.get("type", "")), inp.get("params") or {}, chat_id=chat_id_fn()
            )
        except ActionError as e:
            return {"status": "refused", "error": str(e)}
        return prepared.as_tool_result()

    return Tool(PREPARE_ACTION_SPEC, handler)

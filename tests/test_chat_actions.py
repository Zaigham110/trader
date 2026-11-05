"""prepare_action / execute_confirmed (R-043, RISK_POLICY §3–4, D-039): pending confirmation with
buttons and no execution; owner keys refused; config values validated before the button appears;
confirmed actions become owner_confirmed intents or an atomic YAML rewrite + config_versions row."""

from __future__ import annotations

import yaml
import pytest

from goldtrader.chat.actions import (
    ACTION_TYPES,
    OWNER_ONLY_REASON,
    ActionError,
    execute_confirmed,
    prepare_action,
    prepare_action_tool,
)
from goldtrader.config.loader import config_hash, load_config
from goldtrader.notify.confirm import ConfirmStore
from tests.test_chat_helpers import NOW, OWNER, Clock, FakeGateway, action_ctx, db, write_cfg


def _ctx(tmp_path=None, **over):
    conn, _ = db()
    gw = FakeGateway()
    clock = Clock()
    config = None
    path = None
    if tmp_path is not None:
        path = tmp_path / "demo.yaml"
        config = write_cfg(path, **over)
    return conn, gw, clock, action_ctx(conn, gw, clock, config, path)


def _confirm(ctx, prepared):
    """Press Confirm the way the Telegram layer does (token from the DB row, never the summary)."""
    action = ctx.confirm.get(prepared.action_id)
    res = ctx.confirm.confirm(action.action_id, action.token, ctx.now_ms)
    assert res.already is False
    return res.action


def test_prepare_pause_creates_pending_confirmation_and_executes_nothing():
    conn, gw, clock, ctx = _ctx()
    p = prepare_action(ctx, "pause", {"reason": "lunch <b>break</b>"}, chat_id=OWNER)
    a = ctx.confirm.get(p.action_id)
    assert a.status == "pending" and a.kind == "pause" and a.chat_id == OWNER
    assert a.expires_ms == NOW + 120_000
    assert a.token not in p.summary and a.token not in a.params_json
    assert "Reason: lunch break" in p.summary and "<b>" not in p.summary  # defanged
    rows = a.buttons()
    assert [b.text for b in rows[0]] == ["Confirm", "Cancel"]
    assert gw.intents == []  # nothing executed
    assert ctx.prepared == [p.action_id]
    assert p.as_tool_result()["status"] == "pending_confirmation"
    assert "token" not in p.as_tool_result()


def test_execute_confirmed_sends_owner_confirmed_intents():
    conn, gw, clock, ctx = _ctx()
    for kind, intent_kind in (("pause", "pause"), ("resume", "resume"), ("kill", "kill")):
        p = prepare_action(ctx, kind, chat_id=OWNER)
        line = execute_confirmed(_confirm(ctx, p), ctx)
        it = gw.intents[-1]
        assert it.kind == intent_kind and it.source == "owner_confirmed"
        assert it.intent_id == f"chat-{p.action_id}" and it.created_ms == NOW
        assert line.startswith(f"{kind}: executed")
    p = prepare_action(ctx, "close_position", {"position_id": "4242"}, chat_id=OWNER)
    assert "Close position 4242" in p.summary
    execute_confirmed(_confirm(ctx, p), ctx)
    it = gw.intents[-1]
    assert it.kind == "exit" and it.position_id == 4242 and it.source == "owner_confirmed"


def test_kill_summary_is_explicit_and_unconfirmed_actions_cannot_execute():
    conn, gw, clock, ctx = _ctx()
    p = prepare_action(ctx, "kill", chat_id=OWNER)
    assert "KILL SWITCH" in p.summary and "RISK_POLICY" in p.summary
    pending = ctx.confirm.get(p.action_id)
    with pytest.raises(ActionError):
        execute_confirmed(pending, ctx)  # status pending, not confirmed
    assert gw.intents == []
    cancelled = ctx.confirm.cancel(p.action_id, NOW)
    with pytest.raises(ActionError):
        execute_confirmed(cancelled, ctx)
    assert gw.intents == []


def test_close_position_validation():
    conn, gw, clock, ctx = _ctx()
    with pytest.raises(ActionError, match="integer position_id"):
        prepare_action(ctx, "close_position", {}, chat_id=OWNER)
    with pytest.raises(ActionError, match="not open"):
        prepare_action(ctx, "close_position", {"position_id": 1}, chat_id=OWNER)
    gw.down = True  # gateway unreachable: cannot verify, still prepares (gateway re-checks)
    p = prepare_action(ctx, "close_position", {"position_id": 1}, chat_id=OWNER)
    assert ctx.confirm.get(p.action_id).params == {"position_id": 1}
    with pytest.raises(ActionError, match="unknown action type"):
        prepare_action(ctx, "kill_reset", chat_id=OWNER)
    assert "kill_reset" not in ACTION_TYPES


def test_set_config_value_refuses_owner_keys(tmp_path):
    conn, gw, clock, ctx = _ctx(tmp_path)
    with pytest.raises(ActionError) as e:
        prepare_action(
            ctx,
            "set_config_value",
            {"key": "owner.risk.daily_loss_pct", "value": 4.0},
            chat_id=OWNER,
        )
    assert str(e.value) == OWNER_ONLY_REASON
    with pytest.raises(ActionError):
        prepare_action(ctx, "set_config_value", {"key": "owner", "value": {}}, chat_id=OWNER)
    assert ctx.confirm.list_pending() == []
    assert yaml.safe_load((tmp_path / "demo.yaml").read_text())["owner"]["risk"] == {
        "daily_loss_pct": 2.0
    }
    assert conn.execute("SELECT COUNT(*) FROM config_versions").fetchone()[0] == 0


def test_set_config_value_invalid_rejected_before_button(tmp_path):
    conn, gw, clock, ctx = _ctx(tmp_path)
    # below the ceiling floor (min_pre_break_buffer_min = 5)
    with pytest.raises(ActionError, match="rejected by the config schema"):
        prepare_action(
            ctx,
            "set_config_value",
            {"key": "session.pre_break_buffer_min", "value": 1},
            chat_id=OWNER,
        )
    # wrong type
    with pytest.raises(ActionError, match="rejected by the config schema"):
        prepare_action(
            ctx,
            "set_config_value",
            {"key": "session.pre_break_buffer_min", "value": "soon"},
            chat_id=OWNER,
        )
    # unknown key (schema forbids extras)
    with pytest.raises(ActionError):
        prepare_action(ctx, "set_config_value", {"key": "session.nope", "value": 1}, chat_id=OWNER)
    with pytest.raises(ActionError, match="needs key and value"):
        prepare_action(
            ctx, "set_config_value", {"key": "session.stale_tick_seconds"}, chat_id=OWNER
        )
    assert ctx.confirm.list_pending() == []


def test_set_config_value_rewrites_yaml_atomically_and_records_version(tmp_path):
    conn, gw, clock, ctx = _ctx(tmp_path)
    path = tmp_path / "demo.yaml"
    before_hash = config_hash(ctx.cfg)
    p = prepare_action(
        ctx,
        "set_config_value",
        {"key": "session.pre_break_buffer_min", "value": "10"},
        chat_id=OWNER,
    )
    assert "session.pre_break_buffer_min: 15 → 10" in p.summary
    assert before_hash in p.summary
    stored = ctx.confirm.get(p.action_id).params
    assert stored == {"key": "session.pre_break_buffer_min", "value": 10}  # canonical int
    # nothing written yet
    assert yaml.safe_load(path.read_text())["session"]["pre_break_buffer_min"] == 15
    line = execute_confirmed(_confirm(ctx, p), ctx)
    raw = yaml.safe_load(path.read_text())
    assert raw["session"]["pre_break_buffer_min"] == 10
    assert raw["owner"]["risk"]["daily_loss_pct"] == 2.0  # untouched
    new_cfg = load_config(path)
    assert new_cfg.session.pre_break_buffer_min == 10
    assert config_hash(new_cfg) in line and config_hash(new_cfg) != before_hash
    rows = conn.execute("SELECT config_hash, source FROM config_versions ORDER BY id").fetchall()
    assert [(r["config_hash"], r["source"]) for r in rows] == [(config_hash(new_cfg), "telegram")]
    assert gw.intents == []
    assert not list(tmp_path.glob(".demo.yaml.*"))  # no temp file left behind


def test_set_config_value_without_config_path_fails_at_execution_only():
    conn, gw, clock, ctx = _ctx()  # no path: prepare validates against the in-memory dump
    p = prepare_action(
        ctx, "set_config_value", {"key": "chat.poll_seconds", "value": 5}, chat_id=OWNER
    )
    with pytest.raises(ActionError, match="no config path"):
        execute_confirmed(_confirm(ctx, p), ctx)


def test_prepare_action_tool_returns_errors_as_data():
    conn, gw, clock, ctx = _ctx()
    tool = prepare_action_tool(ctx, lambda: OWNER)
    out = tool.handler(
        {"type": "set_config_value", "params": {"key": "owner.mode", "value": "live"}}
    )
    assert out["status"] == "refused" and "owner-only" in out["error"]
    out = tool.handler({"type": "pause", "params": {}})
    assert out["status"] == "pending_confirmation" and out["action_id"] in ctx.prepared
    assert ConfirmStore(conn).get(out["action_id"]).token not in str(out)
    assert gw.intents == []

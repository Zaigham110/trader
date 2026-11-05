"""Chat read-only tools: provenance envelope (R-041), scorecard arithmetic, memory search, config
visibility, flat schemas, no writes."""

from __future__ import annotations

import json

import pytest

from goldtrader.chat.tools import (
    DATA_TOOLS,
    ChatTools,
    chat_tool_specs,
    chat_tools,
    period_start_ms,
    scorecard,
)
from goldtrader.llm.schema import validate
from tests.test_chat_helpers import DAY, HOUR, NOW, Clock, FakeGateway, db, insert_trade, tool_ctx


def _tools():
    conn, _ = db()
    gw = FakeGateway()
    clock = Clock()
    return conn, gw, clock, ChatTools(tool_ctx(conn, gw, clock))


def test_every_tool_returns_data_plus_provenance_with_iso_time():
    conn, gw, clock, t = _tools()
    names = [s.name for s in chat_tool_specs()]
    assert set(names) >= DATA_TOOLS | {"search_memory"}
    inputs = {
        "get_trade_detail": {"trade_id": "none"},
        "get_config": {"section": "session"},
        "get_scorecard": {"period": "all"},
        "get_llm_costs": {"period": "today"},
        "search_memory": {"query": "fed"},
    }
    for name in names:
        out = getattr(t, name)(inputs.get(name, {}))
        assert set(out) == {"data", "provenance"}, name
        assert " as of 20" in out["provenance"] and out["provenance"].endswith("Z"), name
        json.dumps(out, default=str)  # serialisable for the tool_result block


def test_schemas_are_flat_and_inputs_validate():
    for spec in chat_tool_specs():
        s = spec.input_schema
        assert s["type"] == "object" and s["additionalProperties"] is False
        assert not any(k in json.dumps(s) for k in ("$ref", "anyOf", "oneOf", "allOf"))
    by = {s.name: s for s in chat_tool_specs()}
    assert validate({"n": 51}, by["get_trades"].input_schema)  # above the cap
    assert not validate({"n": 50}, by["get_trades"].input_schema)
    assert validate({"period": "year"}, by["get_scorecard"].input_schema)


def test_positions_and_account_come_from_gateway_state():
    conn, gw, clock, t = _tools()
    pos = t.get_positions({})
    assert pos["data"]["positions"][0]["ticket"] == 4242
    assert "MT5 positions_get" in pos["provenance"]
    acct = t.get_account({})
    assert acct["data"]["equity"] == 5_120.5 and acct["data"]["day_pnl"] == 20.5
    assert "account_info" in acct["provenance"]
    gw.down = True
    assert t.get_account({})["data"] is None
    assert "unavailable" in t.get_account({})["provenance"]


def test_features_health_events_and_config():
    conn, gw, clock, t = _tools()
    f = t.get_features({})
    assert f["data"]["regime"] == "normal" and "features" in f["provenance"]
    conn.execute(
        "INSERT INTO heartbeats(process, last_ms, status) VALUES ('gateway', ?, 'ok')",
        (NOW - 5000,),
    )
    conn.execute(
        "INSERT INTO incidents(opened_ms, severity, kind, summary) VALUES (?, 'high', 'x', 'boom')",
        (NOW - HOUR,),
    )
    h = t.get_health({})["data"]
    assert h["gateway"]["started"] is True
    assert h["heartbeats"][0]["age_s"] == 5.0
    assert h["open_incidents"][0]["summary"] == "boom"
    # events table exists (007) but is empty → []
    assert t.get_events({"next_n": 3})["data"] == []
    conn.execute(
        "INSERT INTO events(event_id, time_ms, title, country, impact, source, fetched_ms) "
        "VALUES ('bls:1', ?, 'CPI', 'US', 'high', 'bls', ?)",
        (NOW + 30 * 60_000, NOW),
    )
    ev = t.get_events({"next_n": 3})["data"]
    assert ev[0]["title"] == "CPI" and ev[0]["minutes_from_now"] == 30.0
    c = t.get_config({"section": "owner"})["data"]
    assert c["values"]["risk"]["daily_loss_pct"] == 2.0 and "owner-only" in c["note"]
    assert "unknown section" in t.get_config({"section": "nope"})["data"]["error"]


def test_trades_detail_and_proposals():
    conn, gw, clock, t = _tools()
    conn.execute(
        "INSERT INTO proposals(proposal_id, created_ms, bot, action, side, thesis, invalidation, "
        "raw_json, gate_status) VALUES ('p1', ?, 'strategy', 'enter', 'long', 'trend up and spread "
        "ok', 'close below ema', '{}', 'sent')",
        (NOW - 2 * HOUR,),
    )
    conn.execute(
        "INSERT INTO intents(intent_id, created_ms, proposal_id, kind, payload_json, status) "
        "VALUES ('i1', ?, 'p1', 'enter', '{}', 'executed')",
        (NOW - 2 * HOUR,),
    )
    conn.execute(
        "INSERT INTO fills(deal_ticket, order_ticket, position_id, time_ms, type, entry, volume, "
        "price) VALUES (9, 8, 4242, ?, 0, 0, 0.01, 3990.0)",
        (NOW - 2 * HOUR,),
    )
    insert_trade(conn, "t1", 12.0, exit_ms=NOW - HOUR, position_id=4242, proposal_id="p1")
    insert_trade(conn, "t2", -5.0, exit_ms=NOW - 30 * 60_000)
    tr = t.get_trades({"n": 1})["data"]
    assert [x["trade_id"] for x in tr] == ["t2"] and tr[0]["risk_pct_of_equity"] == 0.5
    d = t.get_trade_detail({"trade_id": "t1"})["data"]
    assert d["proposal"]["thesis"].startswith("trend up")
    assert d["intents"][0]["intent_id"] == "i1" and d["fills"][0]["deal_ticket"] == 9
    assert "not found" in t.get_trade_detail({"trade_id": "zz"})["data"]["error"]
    assert t.get_proposals({"n": 5})["data"][0]["proposal_id"] == "p1"


def test_scorecard_matches_hand_built_trades():
    conn, gw, clock, t = _tools()
    # cumulative: 10, 40, -10, -20, 15 → net 15; peak 40 → trough -20 → max drawdown 60
    insert_trade(conn, "a", 10.0, exit_ms=NOW - 5 * HOUR, spread=0.2, slip=0.1, swap=-0.5, r=1.0)
    insert_trade(conn, "b", 30.0, exit_ms=NOW - 4 * HOUR, spread=0.2, slip=0.1, r=3.0)
    insert_trade(conn, "c", -50.0, exit_ms=NOW - 3 * HOUR, spread=0.2, slip=0.1, r=-1.0)
    insert_trade(conn, "d", -10.0, exit_ms=NOW - 2 * HOUR, spread=0.2, slip=0.1, commission=-0.7)
    insert_trade(conn, "e", 35.0, exit_ms=NOW - 1 * HOUR, spread=0.2, slip=0.1, swap=0.3)
    card = t.get_scorecard({"period": "all"})["data"]
    assert card["n_trades"] == 5
    assert card["net_pnl"] == 15.0
    assert card["hit_rate"] == 0.6
    assert card["profit_factor"] == pytest.approx(75.0 / 60.0, abs=1e-4)
    assert card["costs"] == pytest.approx(5 * 0.3 + 0.5 + 0.7, abs=1e-6)
    assert card["max_drawdown"] == 60.0
    assert card["avg_r_multiple"] == 1.0
    assert "computed in code" in t.get_scorecard({"period": "all"})["provenance"]
    # period filter: nothing exited "today" before 23:46 UTC except the last five hours → all 5;
    # move the clock a day ahead and today is empty.
    clock.advance(DAY)
    today = t.get_scorecard({"period": "today"})["data"]
    assert today["n_trades"] == 0 and today["hit_rate"] is None and today["profit_factor"] is None
    assert scorecard([]) == {
        "n_trades": 0,
        "net_pnl": 0.0,
        "hit_rate": None,
        "profit_factor": None,
        "gross_wins": 0.0,
        "gross_losses": 0.0,
        "costs": 0.0,
        "max_drawdown": 0.0,
        "avg_r_multiple": None,
    }


def test_period_starts_are_utc_boundaries():
    assert period_start_ms("all", NOW) == 0
    day = period_start_ms("today", NOW)
    assert (NOW - day) < DAY and day % DAY == 0
    week = period_start_ms("week", NOW)
    assert (day - week) % DAY == 0 and 0 <= (day - week) // DAY <= 6
    assert period_start_ms("month", NOW) <= week
    with pytest.raises(ValueError):
        period_start_ms("year", NOW)


def test_search_memory_and_llm_costs():
    conn, gw, clock, t = _tools()
    conn.execute(
        "INSERT INTO lessons(created_ms, trade_id, text, norm_text) VALUES (?, 't1', "
        "'Do not fade the Fed statement', 'do not fade the fed statement')",
        (NOW - HOUR,),
    )
    conn.execute(
        "INSERT INTO news_events(created_ms, run_id, item_id, relevance, event_type, "
        "direction_for_gold, magnitude, horizon, confidence, summary) VALUES (?, 'r', 'n1', 'high', "
        "'fed', 'bullish', 'high', 'hours', 0.8, 'Fed hints at a pause')",
        (NOW - 2 * HOUR,),
    )
    hits = t.search_memory({"query": "Fed"})["data"]
    assert {h["kind"] for h in hits} == {"lesson", "news_event"}
    assert hits[0]["kind"] == "lesson"  # newest first
    assert t.search_memory({"query": "<script>"})["data"] == []
    conn.execute(
        "INSERT INTO llm_calls(created_ms, agent, run_id, model_id, prompt_hash, schema_version, "
        "input_tokens, output_tokens, cost_usd, status) VALUES (?, 'chat', 'r1', 'm', 'h', 1, 100, "
        "50, 0.0123, 'ok')",
        (NOW - HOUR,),
    )
    costs = t.get_llm_costs({"period": "today"})["data"]
    assert costs["calls"] == 1 and costs["cost_usd"] == pytest.approx(0.0123)
    assert costs["by_agent"][0]["agent"] == "chat" and costs["daily_budget_usd"] == 3.0


def test_tools_are_bound_and_write_nothing():
    conn, gw, clock, _ = _tools()
    tools = chat_tools(tool_ctx(conn, gw, clock))
    before = conn.total_changes
    for tool in tools:
        inp = {
            "get_trade_detail": {"trade_id": "x"},
            "get_config": {"section": "llm"},
            "get_scorecard": {"period": "week"},
            "get_llm_costs": {"period": "month"},
            "search_memory": {"query": "x"},
        }.get(tool.spec.name, {})
        tool.handler(inp)
    assert conn.total_changes == before

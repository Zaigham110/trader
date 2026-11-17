"""Read-only dashboard (D-056): read model, loopback server, and import isolation."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from goldtrader.dashboard import data
from goldtrader.dashboard.server import (
    DashboardServer,
    _host_ok,
    connect_inbox,
    connect_readonly,
)
from goldtrader.db import connect, migrate
from tests.test_gateway_api import _state

NOW = 1_794_886_000_000
SRC = Path(__file__).resolve().parents[1] / "src"


class Gateway:
    def __init__(self, up: bool = True) -> None:
        self.up = up

    def state(self):
        if not self.up:
            raise OSError("refused")
        return _state()


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "d.sqlite"
    c = connect(path)
    migrate(c)
    c.execute("INSERT INTO heartbeats VALUES ('gateway', ?, 'ok', '{}')", (NOW - 2_000,))
    c.execute("INSERT INTO heartbeats VALUES ('brain', ?, 'ok', '{}')", (NOW - 120_000,))
    for i in range(12):  # 12 one-minute bars starting on a 5-minute boundary
        t = 1_794_885_000 + 60 * i
        c.execute(
            "INSERT INTO bars VALUES ('XAUUSDm', 60, ?, ?, ?, ?, ?, ?, ?, ?, ?, 5, 'live')",
            (t, 4000 + i, 4002 + i, 3999 + i, 4001 + i, 4000.2 + i, 4002.2 + i, 3999.2, 4001.2 + i),
        )
    out = {"action": "wait", "side": None, "confidence": 0.8, "thesis": "Signal is flat."}
    c.execute(
        "INSERT INTO agent_runs (run_id, agent, started_ms, finished_ms, status, output_json) "
        "VALUES ('r1', 'strategy', ?, ?, 'ok', ?)",
        (NOW - 9_000, NOW - 6_000, json.dumps(out)),
    )
    c.execute(
        "INSERT INTO agent_runs (run_id, agent, started_ms, status) "
        "VALUES ('r2', 'risk_advisor', ?, 'ok')",
        (NOW - 1_000,),
    )
    c.execute(
        "INSERT INTO llm_calls (created_ms, agent, run_id, model_id, prompt_hash, schema_version, "
        "cost_usd, status) VALUES (?, 'strategy', 'r1', 'claude-sonnet-5-5', 'h', 1, 0.01, 'ok')",
        (NOW - 7_000,),
    )
    c.execute(
        "INSERT INTO breaker_state VALUES ('strategy', 'open', 3, ?, ?, ?)", (NOW, NOW + 1, NOW)
    )
    steps = [{"step": "gate", "ok": False, "reason": "wait", "at_ms": NOW}]
    c.execute(
        "INSERT INTO brain_cycles VALUES ('c1', ?, ?, 'wait', NULL, 'wait', 70, ?)",
        (NOW - 9_000, NOW - 5_000, json.dumps(steps)),
    )
    c.execute(
        "INSERT INTO trades (trade_id, side, volume, entry_ms, exit_ms, entry_price, exit_price, "
        "gross_pnl, spread_cost, slippage_cost, commission, swap, net_pnl, r_multiple, exit_reason) "
        "VALUES ('t1', 'long', 0.01, ?, ?, 4000, 4010, 10, 0.2, 0, 0, 0, 9.8, 1.4, 'target')",
        (NOW - 600_000, NOW - 300_000),
    )
    c.execute(
        "INSERT INTO account_snapshots (read_ms, login, equity) VALUES (?, 1, 5000), (?, 1, 5009.8)",
        (NOW - 900_000, NOW - 100_000),
    )
    c.execute(
        "INSERT INTO chat_messages (created_ms, chat_id, role, text) VALUES (?, 1, 'owner', ?)",
        (NOW - 1_000, "a private question"),
    )
    c.commit()
    c.close()
    return path


def test_snapshot_shape_and_agent_cards(tmp_path):
    conn = connect_readonly(_db(tmp_path))
    snap = data.snapshot(conn, Gateway(), NOW)
    json.dumps(snap)  # everything is JSON-ready
    assert snap["gateway_reachable"] and snap["gateway"]["session"] == "OPEN"
    assert "owner_hash" not in snap["gateway"]
    procs = {p["process"]: p for p in snap["processes"]}
    assert not procs["gateway"]["stale"] and procs["brain"]["stale"]
    cards = {a["key"]: a for a in snap["agents"]}
    assert set(cards) == {k for k, _, _ in data.LLM_AGENTS + data.CODE_AGENTS}
    s = cards["strategy"]
    assert s["last"]["summary"].startswith("WAIT (confidence 0.8): Signal is flat")
    assert s["breaker"] == "open" and s["model"] == "claude-sonnet-5-5" and s["calls_today"] == 1
    assert cards["risk_advisor"]["running"] and not s["running"]
    assert cards["research"]["last"] is None
    assert cards["health"]["last"]["status"] == "down"  # the brain heartbeat is stale
    assert snap["cycles"][0]["steps"][0]["step"] == "gate"
    assert snap["trades"]["all"] == {"n": 1, "net_pnl": 9.8, "hit_rate": 1.0}
    assert snap["equity"] == [[NOW - 900_000, 5000], [NOW - 100_000, 5009.8]]
    assert snap["chat"]["messages_today"] == 1
    assert "a private question" not in json.dumps(snap)  # chat text never leaves the table
    assert {f["kind"] for f in snap["feed"]} >= {"agent", "cycle", "trade"}
    assert snap["feed"] == sorted(snap["feed"], key=lambda f: f["at_ms"], reverse=True)


def test_snapshot_without_gateway_falls_back_to_the_database(tmp_path):
    conn = connect_readonly(_db(tmp_path))
    for gw in (None, Gateway(up=False)):
        snap = data.snapshot(conn, gw, NOW)
        assert snap["gateway"] is None and not snap["gateway_reachable"]
        assert snap["positions"] == []


def test_bars_are_aggregated_from_one_minute_bars(tmp_path):
    conn = connect_readonly(_db(tmp_path))
    one = data.bars(conn, "XAUUSDm", 1, 240)["candles"]
    assert len(one) == 12 and one[0] == [1_794_885_000, 4000, 4002, 3999, 4001]
    five = data.bars(conn, "XAUUSDm", 5, 240)
    assert [c[0] for c in five["candles"]] == [1_794_885_000, 1_794_885_300, 1_794_885_600]
    assert five["candles"][0] == [1_794_885_000, 4000, 4006, 3999, 4005]  # o first, h max, c last
    assert data.bars(conn, "XAUUSDm", 7, 240)["minutes"] == 1  # unknown timeframe → 1 minute
    assert data.bars(conn, "OTHER", 1, 240)["candles"] == []


def _get(port: int, path: str, host: str | None = None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if host:
        req.add_header("Host", host)
    with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310
        return r.status, r.headers, r.read()


def test_server_serves_page_and_api_and_is_read_only(tmp_path):
    path = _db(tmp_path)
    srv = DashboardServer(path, Gateway(), port=0, clock_ms=lambda: NOW)
    srv.start()
    try:
        code, headers, body = _get(srv.port, "/")
        assert code == 200 and b"<title>Goldtrader</title>" in body
        assert "default-src 'none'" in headers["Content-Security-Policy"]
        assert b"http://" not in body and b"https://" not in body  # nothing loaded from outside
        snap = json.loads(_get(srv.port, "/api/snapshot")[2])
        assert snap["now_ms"] == NOW and snap["symbol"] == "XAUUSDm"
        bars = json.loads(_get(srv.port, "/api/bars?minutes=5&limit=abc")[2])
        assert bars["minutes"] == 5 and len(bars["candles"]) == 3
        for bad_path, want in (("/api/nope", 404), ("/../state/gateway.token", 404)):
            with pytest.raises(urllib.error.HTTPError) as e:
                _get(srv.port, bad_path)
            assert e.value.code == want
        with pytest.raises(urllib.error.HTTPError) as e:  # another site resolving to this PC
            _get(srv.port, "/api/snapshot", host="evil.example")
        assert e.value.code == 403
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            req = urllib.request.Request(
                f"http://127.0.0.1:{srv.port}/api/snapshot", data=b"{}", method=method
            )
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req, timeout=5)  # noqa: S310
            assert e.value.code == 405
    finally:
        srv.stop()
    # the connection the server uses cannot write
    conn = connect_readonly(path)
    with pytest.raises(Exception, match="readonly"):
        conn.execute("DELETE FROM trades")


def test_server_refuses_non_loopback_and_checks_host_header(tmp_path):
    with pytest.raises(ValueError):
        DashboardServer(tmp_path / "x.sqlite", host="0.0.0.0", port=0)
    assert _host_ok("127.0.0.1:8760") and _host_ok("localhost") and _host_ok("[::1]:8760")
    assert not _host_ok(None) and not _host_ok("192.168.1.5:8760") and not _host_ok("evil.example")


def test_dashboard_cannot_reach_mt5_or_execution():
    code = (
        "import json, sys\n"
        "import goldtrader.dashboard.server\n"
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('goldtrader'))))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=str(SRC)
    )
    loaded = json.loads(out.stdout.strip().splitlines()[-1])
    forbidden = ("goldtrader.mt5", "goldtrader.execution", "goldtrader.gateway", "goldtrader.risk")
    assert not [m for m in loaded if m.startswith(forbidden)]
    assert "goldtrader.secrets" not in loaded and "goldtrader.llm" not in loaded


# --- live view and chat (D-057) -------------------------------------------------------------------
def test_live_shows_running_agent_cycle_steps_and_countdown(tmp_path):
    path = _db(tmp_path)
    c = connect(path)
    c.execute(
        "INSERT INTO brain_cycles VALUES ('c2', ?, NULL, 'running', NULL, NULL, NULL, ?)",
        (NOW - 9_000 + 300_000, json.dumps([{"step": "gateway_state", "available": True}])),
    )
    c.close()
    live = data.live(connect_readonly(path), Gateway(), NOW + 300_000)
    json.dumps(live)
    assert live["tick"]["session"] == "OPEN" and live["cycle_every_s"] == 300
    assert live["cycle"]["cycle_id"] == "c2" and live["cycle"]["running"]
    assert live["cycle"]["steps"][0]["step"] == "gateway_state"
    earlier = data.live(connect_readonly(path), None, NOW)
    runs = {r["run_id"]: r for r in earlier["runs"]}
    assert earlier["tick"] is None
    assert runs["r2"]["running"] and runs["r2"]["summary"] == ""
    assert not runs["r1"]["running"] and runs["r1"]["summary"].startswith("WAIT")


def _post(port: int, body: bytes, **headers: str):
    h = {"Content-Type": "application/json", "X-Dashboard": "1"} | headers
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/chat", data=body, method="POST", headers=h
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_chat_question_is_queued_and_only_the_dashboard_conversation_is_shown(tmp_path):
    path = _db(tmp_path)
    srv = DashboardServer(path, Gateway(), port=0, clock_ms=lambda: NOW)
    srv.start()
    try:
        ok = json.dumps({"text": "  why no trade?  "}).encode()
        assert _post(srv.port, ok, **{"X-Dashboard": "0"})[0] == 403  # a plain form cannot set it
        assert _post(srv.port, ok, Origin="http://evil.example")[0] == 403
        assert _post(srv.port, ok, Host="evil.example")[0] == 403
        assert _post(srv.port, b"not json")[0] == 400
        assert _post(srv.port, json.dumps({"text": "   "}).encode())[0] == 400
        assert _post(srv.port, json.dumps({"text": "x" * 2001}).encode())[0] == 400
        code, out = _post(srv.port, ok, Origin=f"http://127.0.0.1:{srv.port}")
        assert code == 200 and out["id"] == 1
        chat = json.loads(_get(srv.port, "/api/chat")[2])
        assert chat["ready"] and chat["messages"] == [] and not chat["thinking"]
        assert [w["text"] for w in chat["waiting"]] == ["why no trade?"]
        assert "a private question" not in json.dumps(chat)  # Telegram chat stays out
        assert _post(srv.port, ok)[0] == 200 and _post(srv.port, ok)[0] == 200
        assert _post(srv.port, ok) == (429, {"error": "too_many_waiting"})
        # the brain picks the first one up, then answers it
        c = connect(path)
        c.execute(
            "INSERT INTO chat_messages (created_ms, chat_id, role, text, update_id) "
            "VALUES (?, 0, 'owner', 'why no trade?', 1)",
            (NOW,),
        )
        chat = json.loads(_get(srv.port, "/api/chat")[2])
        assert chat["thinking"] and len(chat["waiting"]) == 2 and len(chat["messages"]) == 1
        c.execute(
            "INSERT INTO chat_messages (created_ms, chat_id, role, text) "
            "VALUES (?, 0, 'assistant', 'Signal is flat.')",
            (NOW,),
        )
        c.execute("INSERT INTO notify_cursors VALUES ('dashboard_chat_offset', 2, ?)", (NOW,))
        c.close()
        last = chat["messages"][-1]["id"]
        chat = json.loads(_get(srv.port, f"/api/chat?after={last}")[2])
        assert [m["text"] for m in chat["messages"]] == ["Signal is flat."]
        assert not chat["thinking"] and len(chat["waiting"]) == 2
    finally:
        srv.stop()


def _post_to(port: int, path: str, body: dict, **headers: str):
    h = {"Content-Type": "application/json", "X-Dashboard": "1"} | headers
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(), method="POST", headers=h
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_time_machine_starts_one_whitelisted_process_and_serves_stored_runs(tmp_path):
    path = _db(tmp_path)
    c = connect(path)
    result = {"metrics": {"n_trades": 3, "net_pnl": -4.5, "net_return_pct": -0.09}, "trades": []}
    c.execute(
        "INSERT INTO timemachine_runs (created_ms, finished_ms, status, params_json, result_json) "
        "VALUES (?, ?, 'done', ?, ?)",
        (NOW - 5_000, NOW - 1_000, json.dumps({"days": 7}), json.dumps(result)),
    )
    c.execute(
        "INSERT INTO timemachine_runs (created_ms, status, params_json) VALUES (?, 'running', '{}')",
        (NOW - 3_600_000,),
    )
    c.close()

    class Proc:
        def __init__(self):
            self.done = False

        def poll(self):
            return 0 if self.done else None

    spawned: list[tuple[list[str], Proc]] = []

    def spawn(cmd):
        spawned.append((cmd, Proc()))
        return spawned[-1][1]

    srv = DashboardServer(
        path,
        Gateway(),
        port=0,
        clock_ms=lambda: NOW,
        config_path="config/demo.yaml",
        strategies=("trend_atr_v1",),
        spawn=spawn,
    )
    srv.start()
    try:
        listing = json.loads(_get(srv.port, "/api/timemachine")[2])
        assert listing["available"] and not listing["busy"]
        assert listing["strategies"] == ["trend_atr_v1"]
        runs = {r["id"]: r for r in listing["runs"]}
        assert runs[1]["n_trades"] == 3 and runs[1]["net_pnl"] == -4.5
        assert runs[2]["status"] == "error"  # "running" for an hour: its process is gone
        one = json.loads(_get(srv.port, "/api/timemachine?id=1")[2])
        assert one["result"]["metrics"]["n_trades"] == 3 and one["params"] == {"days": 7}
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(srv.port, "/api/timemachine?id=99")
        assert e.value.code == 404

        good = {"days": 30, "timing": "live_5min", "spread_mult": 1.5, "strategy_id": None}
        url = "/api/timemachine"
        for bad in (
            good | {"days": 31},
            good | {"days": True},
            good | {"timing": "every_bar; rm -rf"},
            good | {"spread_mult": 9},
            good | {"strategy_id": "other"},
            good | {"strategy_id": "--db"},
        ):
            assert _post_to(srv.port, url, bad) == (400, {"error": "bad_parameters"})
        assert _post_to(srv.port, url, good, Origin="http://evil.example")[0] == 403
        assert _post_to(srv.port, url, good, **{"X-Dashboard": "0"})[0] == 403
        assert spawned == []
        assert _post_to(srv.port, url, good | {"strategy_id": "trend_atr_v1"})[0] == 202
        cmd = spawned[0][0]
        assert cmd[3] == "timemachine" and cmd[cmd.index("--days") + 1] == "30"
        assert cmd[cmd.index("--timing") + 1] == "live_5min"
        assert cmd[cmd.index("--strategy-id") + 1] == "trend_atr_v1"
        assert _post_to(srv.port, url, good) == (429, {"error": "already_running"})
        assert json.loads(_get(srv.port, "/api/timemachine")[2])["busy"]
        spawned[0][1].done = True
        assert _post_to(srv.port, url, good)[0] == 202 and len(spawned) == 2
    finally:
        srv.stop()
    plain = DashboardServer(path, Gateway(), port=0)
    assert plain.timemachine_start({"days": 7})[0] == 503  # no config: nothing to run


def test_inbox_connection_can_do_nothing_but_queue_a_question(tmp_path):
    path = _db(tmp_path)
    conn = connect_inbox(path)
    conn.execute("INSERT INTO dashboard_chat_inbox(created_ms, text) VALUES (1, 'q')")
    for sql in (
        "DELETE FROM trades",
        "UPDATE account_snapshots SET equity = 1",
        "INSERT INTO chat_messages (created_ms, chat_id, role, text) VALUES (1, 0, 'owner', 'x')",
        "SELECT * FROM chat_messages",
        "DELETE FROM dashboard_chat_inbox",
        "DROP TABLE dashboard_chat_inbox",
        "PRAGMA journal_mode=DELETE",
    ):
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql)
    conn.close()
    assert connect(path).execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1

import secrets

from goldtrader.contracts import GatewayState, Intent, IntentResult
from goldtrader.gateway.api import GatewayAPI, GatewayClient


def _state():
    return GatewayState(
        time_ms=1,
        connected=True,
        trade_allowed=True,
        session="OPEN",
        entries_allowed=True,
        bid=4000.0,
        ask=4000.2,
        spread_pips=20,
        atr=40,
        tick_age_s=0.1,
        equity=50,
        balance=50,
        margin_free=50,
        margin_level=None,
        open_positions=0,
        kill_switch=False,
        paused=False,
        owner_hash="abc",
        spec_ok=True,
        incident_open=False,
        day_pnl=0.0,
    )


def test_api_round_trip_and_auth():
    token = secrets.token_hex(16)
    seen = []

    def handle(i: Intent) -> IntentResult:
        seen.append(i)
        return IntentResult(intent_id=i.intent_id, status="rejected", risk_code="kill_switch")

    api = GatewayAPI(token, _state, handle, lambda: {"ok": True}, port=0)
    api.start()
    try:
        good = GatewayClient(f"http://127.0.0.1:{api.port}", token)
        st = good.state()
        assert st is not None and st.session == "OPEN"
        assert good.health() == {"ok": True}
        res = good.send_intent(
            Intent(
                intent_id="i1",
                kind="enter",
                created_ms=1,
                side="long",
                stop_price=3980.0,
                target_price=4040.0,
            )
        )
        assert (
            res.status == "rejected" and res.risk_code == "kill_switch" and seen[0].kind == "enter"
        )
        bad = GatewayClient(f"http://127.0.0.1:{api.port}", "x" * 32)
        assert bad.state() is None and bad.send_intent(seen[0]).status == "failed"
        # invalid body is rejected before the handler runs
        r = good._call("POST", "/intents", {"intent_id": "x", "kind": "teleport", "created_ms": 1})
        assert r["error"] == "http_400" and len(seen) == 1
    finally:
        api.stop()


def test_api_refuses_non_loopback_and_short_token():
    import pytest

    with pytest.raises(ValueError):
        GatewayAPI("x" * 32, _state, lambda i: None, dict, host="0.0.0.0", port=0)
    with pytest.raises(ValueError):
        GatewayAPI("short", _state, lambda i: None, dict, port=0)

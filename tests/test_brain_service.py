"""Gateway + brain integration over the loopback API with FakeMT5 and FakeProvider."""

from datetime import UTC, datetime

from goldtrader.config.loader import parse_config
from goldtrader.brain.service import BrainService
from goldtrader.data.resample import frame_to_bars
from goldtrader.data.synthetic import generate_bars
from goldtrader.db import Repo, connect, migrate
from goldtrader.gateway.api import GatewayClient
from goldtrader.gateway.service import GatewayService
from goldtrader.llm.fake import FakeProvider
from goldtrader.mt5.fake import FakeMT5
from goldtrader.notify.telegram import FakeTelegramSender
from tests.test_gateway_service import Clock, _tick

START = datetime(2026, 1, 13, 9, 0, tzinfo=UTC)


def _stack(tmp_path):
    db = tmp_path / "demo.sqlite"
    conn = connect(db)
    migrate(conn)
    repo = Repo(conn)
    hist = generate_bars(START.replace(day=10), 3, seed=4, seconds=60)
    repo.upsert_bars("XAUUSDm", frame_to_bars(hist, 60), "synthetic")
    cfg = parse_config(
        {
            "session": {"dst_mode": "winter"},
            "owner": {"risk": {"max_volume_lots": 1.0}},
            "strategies": [
                {
                    "id": "t",
                    "kind": "trend_atr",
                    "enabled": True,
                    "mode": "demo",
                    "params": {"stop_atr": 1.0, "target_atr": 1.5},
                }
            ],
        }
    )
    clock = Clock(int(START.timestamp() * 1000))
    client = FakeMT5()
    client.set_price(4000.0, 4000.2, clock.ms)
    gw = GatewayService(client, cfg, repo, clock_ms=clock, token="t" * 32, port=0)
    gw.start()
    for _ in range(70):
        _tick(client, clock, 4000.0, 4000.2)
        gw.step()
    gclient = GatewayClient(f"http://127.0.0.1:{gw.api.port}", gw.token)
    # second connection for the brain (separate process in production)
    brain_repo = Repo(connect(db))
    return cfg, repo, brain_repo, gw, gclient, client, clock


def test_brain_cycle_places_trade_through_gateway(tmp_path):
    cfg, repo, brain_repo, gw, gclient, client, clock = _stack(tmp_path)
    try:
        provider = FakeProvider()
        provider.script("fast", [{"budget": 90, "reasons": ["calm"]}] * 5)
        side = "long"
        provider.script(
            "standard",
            [
                {
                    "action": "enter",
                    "side": side,
                    "stop_atr": 1.0,
                    "target_atr": 1.5,
                    "confidence": 0.9,
                    "thesis": "Trend state agrees with the deterministic signal; spread normal.",
                    "invalidation": "Close through the fast EMA.",
                    "horizon_minutes": 120,
                    "agrees_with_signal": True,
                }
            ]
            * 5,
        )
        sender = FakeTelegramSender()
        brain = BrainService(
            cfg,
            brain_repo,
            gclient,
            provider,
            sender=sender,
            owner_chat_id=1,
            clock_ms=clock,
            cycle_every_s=60,
        )
        # force the deterministic signal to agree with the scripted side
        brain.cycle.signal_fn = lambda: 1 if side == "long" else -1
        ran = brain.tick()
        assert "cycle" in ran and "heartbeat" in ran
        res = brain.last_cycle
        assert res.status in ("sent", "ok", "executed") or res.intent_result is not None, res
        assert res.intent_result is not None and res.intent_result.status == "executed", res
        assert len(client.positions_get()) == 1
        rows = brain_repo.c.execute("SELECT gate_status FROM proposals").fetchall()
        assert rows and rows[0][0] == "sent"
        hb = brain_repo.c.execute("SELECT status FROM heartbeats WHERE process='brain'").fetchone()
        assert hb[0] == "ok"
        # close at target → gateway reconciles → brain reviews the closed trade
        pos = client.positions_get()[0]
        _tick(client, clock, pos.tp + 0.5, pos.tp + 0.7)
        clock.advance(61)
        gw.step()
        provider.script(
            "standard",
            [
                {
                    "outcome_class": "correct_win",
                    "signal_pnl": 0.0,
                    "timing_pnl": 0.0,
                    "slippage": 0.0,
                    "costs": 0.0,
                    "lessons": [],
                    "confidence": 0.7,
                }
            ]
            * 3,
        )
        clock.advance(61)
        brain.tick()
        pm = brain_repo.c.execute("SELECT COUNT(*) FROM postmortems").fetchone()[0]
        assert pm == 1
    finally:
        gw.stop()


def test_brain_fails_flat_without_provider_and_when_gateway_down(tmp_path):
    cfg, repo, brain_repo, gw, gclient, client, clock = _stack(tmp_path)
    try:
        brain = BrainService(
            cfg, brain_repo, gclient, provider=None, clock_ms=clock, cycle_every_s=60
        )
        brain.tick()
        assert brain.last_cycle is not None and brain.last_cycle.intent_result is None
        assert client.positions_get() == []
        gw.stop()
        clock.advance(61)
        ran = brain.tick()  # gateway unreachable → only_when False → cycle skipped, no exception
        assert "cycle" not in ran
    finally:
        pass

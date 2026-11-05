from datetime import UTC, datetime

from goldtrader.config.loader import parse_config
from goldtrader.contracts import Intent
from goldtrader.data.resample import frame_to_bars
from goldtrader.data.synthetic import generate_bars
from goldtrader.db import Repo, connect, migrate
from goldtrader.gateway.api import GatewayClient
from goldtrader.gateway.service import GatewayService
from goldtrader.mt5.fake import FakeMT5
from goldtrader.types import SessionState

START = datetime(2026, 1, 13, 9, 0, tzinfo=UTC)  # Tuesday 09:00 UTC, winter → OPEN


class Clock:
    def __init__(self, ms):
        self.ms = ms

    def __call__(self):
        return self.ms

    def advance(self, seconds):
        self.ms += int(seconds * 1000)


def _service(cfg_over=None):
    conn = connect(None)
    migrate(conn)
    repo = Repo(conn)
    hist = generate_bars(START.replace(day=10), 3, seed=4, seconds=60)  # Sat 10th → covers Mon 12th
    repo.upsert_bars("XAUUSDm", frame_to_bars(hist, 60), "synthetic")
    client = FakeMT5()
    cfg = parse_config(
        {
            "session": {"dst_mode": "winter"},
            "owner": {"risk": {"max_volume_lots": 1.0, **(cfg_over or {})}},
        }
    )
    clock = Clock(int(START.timestamp() * 1000))
    client.set_price(4000.0, 4000.2, clock.ms)
    svc = GatewayService(client, cfg, repo, clock_ms=clock, token="t" * 32, port=0)
    svc.start()
    return svc, client, clock, repo


def _tick(client, clock, bid, ask):
    clock.advance(1)
    client.set_price(bid, ask, clock.ms)


def test_end_to_end_enter_target_reconcile_and_flatten():
    svc, client, clock, repo = _service()
    try:
        gw = GatewayClient(f"http://127.0.0.1:{svc.api.port}", svc.token)
        # warm the live market so spread regime is known
        for i in range(70):
            _tick(client, clock, 4000.0 + 0.01 * (i % 3), 4000.2 + 0.01 * (i % 3))
            svc.step()
        st = gw.state()
        assert st is not None and st.session == "OPEN" and st.entries_allowed and st.atr
        atr = st.atr
        res = gw.send_intent(
            Intent(
                intent_id="e1",
                kind="enter",
                created_ms=clock.ms,
                side="long",
                stop_price=round(4000.2 - 0.8 * atr, 2),
                target_price=round(4000.2 + 1.2 * atr, 2),
                bundle_id="b1",
                proposal_id="p1",
            )
        )
        assert res.status == "executed", res
        pos = client.positions_get()
        assert len(pos) == 1 and pos[0].sl > 0 and pos[0].tp > 0
        # idempotent
        assert (
            gw.send_intent(
                Intent(
                    intent_id="e1",
                    kind="enter",
                    created_ms=clock.ms,
                    side="long",
                    stop_price=3900.0,
                    target_price=4100.0,
                    bundle_id="b1",
                    proposal_id="p1",
                )
            ).status
            == "executed"
        )
        assert len(client.positions_get()) == 1
        # second entry blocked by max_positions
        res2 = gw.send_intent(
            Intent(
                intent_id="e2",
                kind="enter",
                created_ms=clock.ms,
                side="long",
                stop_price=round(4000.2 - 0.8 * atr, 2),
                target_price=round(4000.2 + 1.2 * atr, 2),
                bundle_id="b1",
                proposal_id="p2",
            )
        )
        assert res2.status == "rejected" and res2.risk_code == "max_positions"
        # price reaches the target → broker closes → reconciler books the trade
        tp = pos[0].tp
        _tick(client, clock, tp + 0.5, tp + 0.7)
        clock.advance(61)
        svc.step()
        assert client.positions_get() == []
        trades = repo.c.execute("SELECT exit_reason, net_pnl FROM trades").fetchall()
        assert len(trades) == 1 and trades[0][0] == "target" and trades[0][1] > 0
        # pre-break flatten: open a position then move the clock to 21:50 UTC
        _tick(client, clock, 4000.0, 4000.2)
        svc.step()
        res3 = gw.send_intent(
            Intent(
                intent_id="e3",
                kind="enter",
                created_ms=clock.ms,
                side="short",
                stop_price=round(4000.0 + 0.8 * atr, 2),
                target_price=round(4000.0 - 1.2 * atr, 2),
                bundle_id="b1",
                proposal_id="p3",
            )
        )
        assert res3.status == "executed", res3
        target_dt = datetime(2026, 1, 13, 21, 50, tzinfo=UTC)
        clock.ms = int(target_dt.timestamp() * 1000)
        client.set_price(4000.0, 4000.2, clock.ms)
        snap = svc.step()
        assert snap.session is SessionState.PRE_BREAK_BUFFER
        assert client.positions_get() == [] and svc.counters.flattens == 1
        assert svc.health()["kills"] == 0
    finally:
        svc.stop()


def test_kill_switch_paths():
    svc, client, clock, repo = _service({"max_drawdown_pct": 10.0})
    try:
        gw = GatewayClient(f"http://127.0.0.1:{svc.api.port}", svc.token)
        for _ in range(70):
            _tick(client, clock, 4000.0, 4000.2)
            svc.step()
        # strategy bot may not kill
        r = gw.send_intent(
            Intent(intent_id="k0", kind="kill", created_ms=clock.ms, source="strategy_bot")
        )
        assert r.status == "rejected" and "owner_only" in r.detail
        # drawdown kill: simulate an equity collapse via the fake account
        client.set_account(balance=4400.0)
        _tick(client, clock, 4000.0, 4000.2)
        svc.step()
        assert svc.flags.kill_switch and svc.health()["kills"] == 1
        st = gw.state()
        assert st.kill_switch
        res = gw.send_intent(
            Intent(
                intent_id="e9",
                kind="enter",
                created_ms=clock.ms,
                side="long",
                stop_price=3970.0,
                target_price=4040.0,
                bundle_id="b1",
                proposal_id="p9",
            )
        )
        assert res.status == "rejected" and res.risk_code == "kill_switch"
        # owner resume clears the kill incident
        r2 = gw.send_intent(
            Intent(intent_id="r1", kind="resume", created_ms=clock.ms, source="owner_confirmed")
        )
        assert r2.status == "executed" and not svc.flags.kill_switch
    finally:
        svc.stop()


def test_step_survives_client_errors_and_disconnect():
    svc, client, clock, repo = _service()
    try:
        client.connected = False
        _tick(client, clock, 4000.0, 4000.2)
        snap = svc.step()
        assert snap is not None and snap.session is SessionState.STALE and not snap.entries_allowed
        client.connected = True

        def boom(*a, **k):
            raise RuntimeError("ipc")

        client.symbol_info_tick = boom
        assert svc.step() is None and svc.counters.errors == 1 and "ipc" in svc.counters.last_error
    finally:
        svc.stop()

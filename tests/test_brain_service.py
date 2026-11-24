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
        # the scheduler swallows job errors, so call the shadow job and the status directly:
        # `ShadowLedger.bundles` is a property and both used to call it (TypeError on the demo PC)
        brain._shadow_job()
        assert brain.learning.status()["shadow_bundles"] == []
        gw.stop()
        clock.advance(61)
        ran = brain.tick()  # gateway unreachable → only_when False → cycle skipped, no exception
        assert "cycle" not in ran
    finally:
        pass


def test_brain_news_calendar_and_chat_jobs(tmp_path):
    """Feeds → Research bot → news_events; calendar → events; chat command answered by code."""
    from goldtrader.chat.telegram_loop import FakeTelegramUpdates
    from goldtrader.feeds.rss import FetchResult

    cfg, repo, brain_repo, gw, gclient, client, clock = _stack(tmp_path)
    try:
        rss = (
            '<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
            "<item><guid>g1</guid><title>Fed holds rates, signals patience</title>"
            "<link>https://example.org/1</link><pubDate>Tue, 13 Jan 2026 08:30:00 GMT</pubDate>"
            "<description>Statement text.</description></item></channel></rss>"
        )
        ics = (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:cpi1\nDTSTART:20260114T133000Z\n"
            "SUMMARY:Consumer Price Index\nEND:VEVENT\nEND:VCALENDAR\n"
        )

        def fake_fetch(url, **kw):
            if url.endswith(".ics"):
                return FetchResult(200, ics, None, None)
            return FetchResult(200, rss, None, None)

        provider = FakeProvider()
        # the cycle job's Risk-advisor shares the "fast" tier queue and runs first in the tick
        research_out = {
            "events": [
                {
                    "item_id": "fed_press:g1",
                    "relevance": "high",
                    "event_type": "fed",
                    "direction_for_gold": "neutral",
                    "magnitude": "medium",
                    "horizon": "hours",
                    "confidence": 0.8,
                    "summary": "Fed on hold.",
                }
            ],
            "dropped": ["bls_latest:g1"],
        }
        provider.script("fast", [{"budget": 50, "reasons": ["test"]}] + [research_out] * 3)
        provider.script(
            "standard",
            [
                {
                    "action": "wait",
                    "confidence": 0.5,
                    "thesis": "No setup: waiting for the trend to confirm.",
                    "invalidation": "n/a while flat",
                    "horizon_minutes": 60,
                    "agrees_with_signal": True,
                }
            ]
            * 3,
        )
        sender = FakeTelegramSender()
        updates = FakeTelegramUpdates()
        brain = BrainService(
            cfg,
            brain_repo,
            gclient,
            provider,
            sender=sender,
            owner_chat_id=7,
            clock_ms=clock,
            cycle_every_s=60,
            chat_updates=updates,
            feed_fetch=fake_fetch,
        )
        updates.push(
            {
                "update_id": 1,
                "message": {
                    "message_id": 1,
                    "chat": {"id": 7},
                    "from": {"id": 7},
                    "text": "/status",
                },
            }
        )
        ran = brain.tick()
        assert {"news", "calendar", "chat"} <= set(ran), ran
        assert brain_repo.c.execute("SELECT COUNT(*) FROM news_items").fetchone()[0] >= 1
        assert brain_repo.c.execute("SELECT COUNT(*) FROM news_events").fetchone()[0] >= 1
        assert (
            brain_repo.c.execute(
                "SELECT COUNT(*) FROM news_items WHERE processed_ms IS NOT NULL"
            ).fetchone()[0]
            >= 1
        )
        assert (
            brain_repo.c.execute(
                "SELECT COUNT(*) FROM events WHERE title LIKE 'Consumer%'"
            ).fetchone()[0]
            == 1
        )
        assert sender.sent and "OPEN" in sender.sent[-1].text
        # the gateway now sees the CPI event through the calendar store
        from goldtrader.feeds.calendar_store import CalendarStore

        evs = CalendarStore(repo.c).events_fn(clock)()
        assert any("Consumer" in e.title for e in evs)
    finally:
        gw.stop()


def test_dashboard_commands_become_checked_intents(tmp_path):
    import time

    import pytest

    from goldtrader.contracts import Intent

    cfg, repo, brain_repo, gw, gclient, client, clock = _stack(tmp_path)
    try:
        brain = BrainService(
            cfg, brain_repo, gclient, provider=None, clock_ms=clock, cycle_every_s=3600
        )
        atr = gclient.state().atr
        stop = round(4000.2 - 0.8 * atr, 2)
        res = gclient.send_intent(
            Intent(
                intent_id="e1",
                kind="enter",
                created_ms=clock.ms,
                side="long",
                stop_price=stop,
                target_price=round(4000.2 + 1.2 * atr, 2),
                bundle_id="t",
                proposal_id="p1",
            )
        )
        assert res.status == "executed", res
        pid = client.positions_get()[0].ticket

        def run(kind, position_id=None, age_ms=0):
            cur = brain_repo.c.execute(
                "INSERT INTO dashboard_commands(created_ms, kind, position_id) VALUES (?,?,?)",
                (int(time.time() * 1000) - age_ms, kind, position_id),
            )
            brain._dashboard_commands_job()
            gw.step()  # the gateway's published state refreshes on its next step
            return brain_repo.c.execute(
                "SELECT status, detail FROM dashboard_commands WHERE id=?", (cur.lastrowid,)
            ).fetchone()

        assert run("pause")[0] == "done" and gclient.state().paused
        assert run("resume")[0] == "done" and not gclient.state().paused
        assert run("pause", age_ms=61_000)[0] == "expired" and not gclient.state().paused
        assert run("enter")[0] == "refused" and run("close")[0] == "refused"  # unknown / no id
        # stop to entry while the price is below the entry: the risk engine refuses it
        status, detail = run("breakeven", pid)
        assert status == "refused" and "stop" in detail
        assert client.positions_get()[0].sl == pytest.approx(stop)
        # ... and allows it once the price is above the entry
        _tick(client, clock, 4003.0, 4003.2)
        gw.step()
        assert run("breakeven", pid)[0] == "done"
        pos = client.positions_get()[0]
        assert pos.sl == pytest.approx(pos.price_open)
        assert run("close", pid)[0] == "done" and client.positions_get() == []
        assert repo.c.execute("SELECT exit_reason FROM trades").fetchone()[0] == "manual"
        # kill from the dashboard works; undoing it from the dashboard does not
        assert run("kill")[0] == "done" and gclient.state().kill_switch
        status, detail = run("resume")
        assert status == "refused" and "Telegram" in detail and gclient.state().kill_switch
        # even a resume intent sent straight to the gateway with this source leaves it on
        direct = gclient.send_intent(
            Intent(intent_id="r9", kind="resume", created_ms=clock.ms, source="owner_dashboard")
        )
        assert direct.status == "executed" and gclient.state().kill_switch
        n = brain_repo.c.execute(
            "SELECT COUNT(*) FROM alerts WHERE kind='owner_command'"
        ).fetchone()[0]
        assert n == 10  # every command is alerted, whatever became of it
    finally:
        gw.stop()

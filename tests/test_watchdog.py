"""Loop watchdog and the stale-gateway alert (D-065)."""

from __future__ import annotations

import threading

import pytest

from goldtrader.db import connect, migrate
from goldtrader.process import watchdog as wd


def test_watchdog_ends_a_stuck_loop_once_and_leaves_the_stacks(tmp_path):
    now = {"t": 100.0}
    exits: list[int] = []
    why: list[str] = []
    dog = wd.Watchdog(
        120, name="gateway loop", dump_path=tmp_path / "s" / "stuck.log", on_exit=why.append,
        clock=lambda: now["t"], terminate=exits.append,
    )  # fmt: skip
    now["t"] += 119
    assert dog.check() is False and exits == []
    dog.beat()
    now["t"] += 119
    assert dog.check() is False  # a beat resets the timer
    now["t"] += 2
    assert dog.check() is True and exits == [wd.EXIT_CODE] and wd.EXIT_CODE != 0
    assert "no progress for 121s" in why[0]
    dump = (tmp_path / "s" / "stuck.log").read_text(encoding="utf-8")
    assert "watchdog: gateway loop" in dump and "test_watchdog" in dump  # where it was stuck
    assert dog.check() is False and len(exits) == 1  # fires once
    with pytest.raises(ValueError):
        wd.Watchdog(0)
    # a loop that ended on purpose is not stuck: after stop() the watchdog never fires
    quiet = wd.Watchdog(1, clock=lambda: now["t"], terminate=exits.append)
    quiet.stop()
    now["t"] += 500
    assert quiet.check() is False and len(exits) == 1

    def boom(_why):
        raise RuntimeError("exit log unwritable")

    dog2 = wd.Watchdog(1, on_exit=boom, clock=lambda: now["t"], terminate=exits.append)
    now["t"] += 5
    assert dog2.check() is True and len(exits) == 2  # nothing stops the exit


def test_watchdog_thread_fires_on_a_loop_that_stops_beating():
    fired = threading.Event()
    dog = wd.Watchdog(0.2, terminate=lambda code: fired.set(), check_s=0.05)
    dog.start()
    dog.start()  # idempotent
    assert fired.wait(3)


def test_gateway_run_loop_beats_only_after_a_step_that_worked():
    from goldtrader.gateway.service import GatewayService

    class Stub:
        on_step = None
        cfg = type(
            "C", (), {"data": type("D", (), {"tick_poll_closed_s": 0, "tick_poll_ms": 0})()}
        )()

        calls = 0

        def step(self):
            # every second step fails (returns None), as when the terminal link is gone
            self.calls += 1
            if self.calls % 2 == 0:
                return None
            return type("Snap", (), {"session": None})()

    stop = threading.Event()
    beats: list[int] = []

    def beat():
        beats.append(1)
        if len(beats) == 3:
            stop.set()

    s = Stub()
    s.on_step = beat
    GatewayService.run(s, stop)
    assert len(beats) == 3 and s.calls == 5  # the two failed steps did not count


def test_owner_is_told_when_the_gateway_goes_quiet_and_when_it_returns():
    conn = connect(None)
    migrate(conn)
    t0 = 1_800_000_000_000
    assert wd.watch_heartbeat(conn, "gateway", t0) is None  # never started: nothing to say
    conn.execute("INSERT INTO heartbeats VALUES ('gateway', ?, 'ok', '{}')", (t0,))
    assert wd.watch_heartbeat(conn, "gateway", t0 + 170_000) is None
    assert wd.watch_heartbeat(conn, "gateway", t0 + 240_000) == "stale"
    assert wd.watch_heartbeat(conn, "gateway", t0 + 300_000) is None  # not every minute
    assert wd.watch_heartbeat(conn, "gateway", t0 + 240_000 + wd.REPEAT_MS) == "stale"  # hourly
    conn.execute(
        "UPDATE heartbeats SET last_ms=? WHERE process='gateway'", (t0 + 2 * wd.REPEAT_MS,)
    )
    assert wd.watch_heartbeat(conn, "gateway", t0 + 2 * wd.REPEAT_MS + 5_000) == "recovered"
    assert wd.watch_heartbeat(conn, "gateway", t0 + 2 * wd.REPEAT_MS + 9_000) is None
    rows = conn.execute("SELECT severity, kind, message FROM alerts ORDER BY id").fetchall()
    assert [(r[0], r[1]) for r in rows] == [
        ("critical", "gateway_stale"), ("critical", "gateway_stale"), ("info", "gateway_recovered"),
    ]  # fmt: skip
    assert "not reported for 4 minutes" in rows[0][2] and "stop at the broker" in rows[0][2]


def test_brain_run_loop_beats_after_every_tick_even_one_that_raised():
    from goldtrader.brain.service import BrainService

    class Stub:
        calls = 0

        def tick(self):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("a job blew up")
            return []

    stop = threading.Event()
    beats: list[int] = []

    def beat():
        beats.append(1)
        if len(beats) == 3:
            stop.set()

    s = Stub()
    BrainService.run(s, stop, interval_s=0, on_tick=beat)
    assert len(beats) == 3 and s.calls == 3  # the loop is still turning after an error

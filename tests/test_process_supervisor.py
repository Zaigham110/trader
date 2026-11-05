"""Supervisor: restart with backoff, healthy reset, env strip, stop, exit recording."""

import os
import sys
import threading
import time

from goldtrader.db import connect, migrate
from goldtrader.process.supervisor import (
    ChildExit,
    ChildSpec,
    Supervisor,
    build_child_env,
    db_exit_recorder,
    exit_reason,
)

PY = sys.executable


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def _wait_exit(sup: Supervisor, name: str) -> None:
    assert sup.wait_child(name, 10) is not None


def test_restarts_crashing_child_with_doubling_backoff_and_records_exits():
    clock = FakeClock()
    exits: list[ChildExit] = []
    spec = ChildSpec("crasher", [PY, "-c", "import sys; sys.exit(3)"])
    sup = Supervisor(
        [spec],
        on_exit=exits.append,
        backoff_initial_s=1,
        backoff_max_s=4,
        healthy_reset_s=600,
        clock=clock,
    )
    sup.start()
    first_pid = sup.pid("crasher")
    assert first_pid
    _wait_exit(sup, "crasher")
    seen = sup.poll()
    assert len(seen) == 1 and seen[0].exit_code == 3 and seen[0].reason == "crashed"
    assert seen[0].pid == first_pid and seen[0].process == "crasher"
    assert exits == seen
    assert not sup.is_running("crasher")
    # not yet due: 1 s backoff
    clock.advance(0.5)
    assert sup.poll() == [] and not sup.is_running("crasher")
    clock.advance(0.6)
    sup.poll()
    assert sup.restarts("crasher") == 1
    _wait_exit(sup, "crasher")
    sup.poll()
    assert sup.backoff("crasher") == 4  # 1 → 2 → 4 pending for the next restart
    clock.advance(1.9)
    sup.poll()
    assert sup.restarts("crasher") == 1  # 2 s not elapsed yet
    clock.advance(0.2)
    sup.poll()
    assert sup.restarts("crasher") == 2
    _wait_exit(sup, "crasher")
    sup.poll()
    assert sup.backoff("crasher") == 4  # capped
    assert len(exits) == 3
    sup.stop()


def test_backoff_resets_after_healthy_run():
    clock = FakeClock()
    spec = ChildSpec("c", [PY, "-c", "import sys; sys.exit(1)"])
    sup = Supervisor(
        [spec], backoff_initial_s=1, backoff_max_s=60, healthy_reset_s=600, clock=clock
    )
    sup.start()
    _wait_exit(sup, "c")
    sup.poll()
    assert sup.backoff("c") == 2
    clock.advance(1)
    sup.poll()  # restart
    clock.advance(601)  # ran "healthy" for > healthy_reset_s before dying
    _wait_exit(sup, "c")
    sup.poll()
    assert sup.backoff("c") == 2  # reset to initial (1) then doubled once for the next time
    sup.stop()


def test_env_strip_removes_secret_from_child(tmp_path):
    out = tmp_path / "env.txt"
    code = (
        "import os,sys; open(sys.argv[1],'w').write("
        "os.environ.get('MT5_PASSWORD','ABSENT')+'|'+os.environ.get('KEEP_ME','none'))"
    )
    base = dict(os.environ, MT5_PASSWORD="hunter2", KEEP_ME="yes")
    spec = ChildSpec("brain", [PY, "-c", code, str(out)], env_strip=["MT5_PASSWORD"])
    sup = Supervisor([spec], base_env=base)
    sup.start()
    _wait_exit(sup, "brain")
    exits = sup.stop()
    assert out.read_text() == "ABSENT|yes"
    assert exits[0].reason == "exited" and exits[0].exit_code == 0
    assert build_child_env({"A": "1", "B": "2"}, ["A"], {"C": "3"}) == {"B": "2", "C": "3"}


def test_stop_terminates_long_running_child_and_records_stopped():
    conn = connect(None)
    migrate(conn)
    spec = ChildSpec("sleeper", [PY, "-c", "import time; time.sleep(60)"])
    sup = Supervisor([spec], on_exit=db_exit_recorder(conn), stop_grace_s=5)
    sup.start()
    assert sup.is_running("sleeper")
    t0 = time.monotonic()
    exits = sup.stop()
    assert time.monotonic() - t0 < 5
    assert len(exits) == 1 and exits[0].reason == "stopped"
    assert not sup.is_running("sleeper")
    row = conn.execute("SELECT process, pid, exit_code, reason FROM process_exits").fetchone()
    assert row["process"] == "sleeper" and row["pid"] == exits[0].pid and row["reason"] == "stopped"
    # a stopped supervisor does not restart
    assert sup.poll() == [] and not sup.is_running("sleeper")


def test_run_loop_stops_on_event():
    spec = ChildSpec("sleeper", [PY, "-c", "import time; time.sleep(60)"])
    sup = Supervisor([spec], stop_grace_s=5)
    stop = threading.Event()
    t = threading.Thread(target=sup.run, args=(stop, 0.05))
    t.start()
    deadline = time.monotonic() + 5
    while not sup.is_running("sleeper") and time.monotonic() < deadline:
        time.sleep(0.02)
    assert sup.is_running("sleeper")
    stop.set()
    t.join(10)
    assert not t.is_alive() and not sup.is_running("sleeper")


def test_exit_reason_mapping():
    assert exit_reason(0, False) == "exited"
    assert exit_reason(2, False) == "crashed"
    assert exit_reason(-9, False) == "signal:9"
    assert exit_reason(-15, True) == "stopped"
    assert exit_reason(None, False) == "unknown"

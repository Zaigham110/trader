"""PID locks: acquire, held by a live pid, stale takeover, conflict sets, context manager."""

import json
import os
import subprocess
import sys

import pytest

from goldtrader.process.lock import CONFLICTS, LockHeld, acquire, lock_path, pid_alive


def _dead_pid() -> int:
    pid = 2**22 - 7
    while pid_alive(pid):
        pid -= 1
    return pid


def test_acquire_release_and_context_manager(tmp_path):
    lock = acquire("gateway", tmp_path)
    path = lock_path("gateway", tmp_path)
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["pid"] == os.getpid() and data["name"] == "gateway"
    with pytest.raises(LockHeld) as ei:
        acquire("gateway", tmp_path)
    assert ei.value.pid == os.getpid()
    lock.release()
    lock.release()  # idempotent
    assert not path.exists()
    with acquire("gateway", tmp_path) as again:
        assert again.pid == os.getpid()
    assert not path.exists()


def test_held_by_another_live_process(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        path = lock_path("brain", tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"pid": child.pid, "name": "brain"}))
        with pytest.raises(LockHeld) as ei:
            acquire("brain", tmp_path)
        assert ei.value.pid == child.pid
        assert "brain" in str(ei.value)
    finally:
        child.kill()
        child.wait()


def test_stale_lock_is_taken_over(tmp_path):
    path = lock_path("brain", tmp_path)
    path.parent.mkdir(parents=True)
    dead = _dead_pid()
    path.write_text(json.dumps({"pid": dead, "name": "brain"}))
    lock = acquire("brain", tmp_path)
    assert json.loads(path.read_text())["pid"] == os.getpid()
    lock.release()
    # unreadable / torn file counts as stale too
    path.write_text("{not json")
    with acquire("brain", tmp_path):
        assert json.loads(path.read_text())["pid"] == os.getpid()


def test_conflict_sets(tmp_path):
    assert "gateway-replay" in CONFLICTS["gateway"] and "gateway" in CONFLICTS["gateway-replay"]
    with acquire("gateway", tmp_path):
        with pytest.raises(LockHeld) as ei:
            acquire("gateway-replay", tmp_path)
        assert ei.value.name == "gateway" and ei.value.requested == "gateway-replay"
        assert "conflicting" in str(ei.value)
        # unrelated lock is fine
        with acquire("brain", tmp_path):
            pass
    # the conflicting lock is stale → ignored and cleaned up
    stale = lock_path("gateway", tmp_path)
    stale.write_text(json.dumps({"pid": _dead_pid(), "name": "gateway"}))
    with acquire("gateway-replay", tmp_path):
        assert not stale.exists()
    # custom conflict map
    with acquire("a", tmp_path, conflicts={"a": frozenset({"b"}), "b": frozenset({"a"})}):
        with pytest.raises(LockHeld):
            acquire("b", tmp_path, conflicts={"a": frozenset({"b"}), "b": frozenset({"a"})})


def test_pid_alive():
    assert pid_alive(os.getpid())
    assert not pid_alive(0)
    assert not pid_alive(-1)
    assert not pid_alive(_dead_pid())

"""Exit log: start / exit lines for SIGTERM, exception and normal exit; unlogged-exit detection."""

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from goldtrader.process.exitlog import ExitLog

PY = sys.executable


def _child_script(log_path: Path, body: str) -> str:
    return textwrap.dedent(
        f"""
        import sys, time
        from goldtrader.process.exitlog import ExitLog
        log = ExitLog({str(log_path)!r})
        log.install("gateway")
        print("ready", flush=True)
        {body}
        """
    )


def _run(log_path: Path, body: str, *, kill: int | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    proc = subprocess.Popen(
        [PY, "-c", _child_script(log_path, body)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "ready"
    if kill is not None:
        time.sleep(0.2)
        proc.send_signal(kill)
    out, err = proc.communicate(timeout=15)
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_sigterm_path_is_logged(tmp_path):
    log_path = tmp_path / "exit.log"
    res = _run(log_path, "time.sleep(30)", kill=signal.SIGTERM)
    assert res.returncode == 128 + signal.SIGTERM
    lines = _lines(log_path)
    assert [d["event"] for d in lines] == ["start", "exit"]
    assert lines[1]["reason"] == "SIGTERM"
    assert lines[0]["run"] == lines[1]["run"] and lines[0]["process"] == "gateway"
    assert ExitLog(log_path).report_unlogged("gateway") is None


def test_sigint_normal_and_exception_paths(tmp_path):
    log_path = tmp_path / "exit.log"
    res = _run(log_path, "time.sleep(30)", kill=signal.SIGINT)
    assert res.returncode != 0
    assert _lines(log_path)[-1]["reason"] == "SIGINT"

    _run(log_path, "sys.exit(0)")
    assert _lines(log_path)[-1]["reason"] == "normal"

    res = _run(log_path, "raise RuntimeError('boom')")
    assert res.returncode == 1
    last = _lines(log_path)[-1]
    assert last["reason"] == "exception"
    assert "RuntimeError: boom" in last["detail"]
    assert "Traceback" in last["detail"]
    assert "RuntimeError: boom" in res.stderr  # the previous excepthook still ran
    # three starts, three exits: nothing unlogged
    assert len(_lines(log_path)) == 6
    assert ExitLog(log_path).report_unlogged("gateway") is None


def test_sigkill_leaves_no_exit_line_and_is_reported_at_next_start(tmp_path):
    log_path = tmp_path / "exit.log"
    _run(log_path, "time.sleep(30)", kill=signal.SIGKILL)
    lines = _lines(log_path)
    assert [d["event"] for d in lines] == ["start"]
    # next start of the same process
    nxt = ExitLog(log_path)
    msg = nxt.report_unlogged("gateway")
    assert msg is not None
    assert "gateway" in msg and f"pid {lines[0]['pid']}" in msg and "no exit record" in msg
    # another process name is not confused with it
    assert nxt.report_unlogged("brain") is None
    assert nxt.mark_reported("gateway") == 1
    assert nxt.report_unlogged("gateway") is None


def test_report_excludes_current_run_and_counts_multiple(tmp_path):
    log_path = tmp_path / "exit.log"
    clock = [1_000_000]

    def now_ms():
        clock[0] += 1000
        return clock[0]

    # two earlier runs that never wrote an exit line (hand-written, as a torn log would look)
    with open(log_path, "w") as f:
        for run in ("aaaa", "bbbb"):
            f.write(
                json.dumps(
                    {"ms": now_ms(), "process": "brain", "pid": 1, "run": run, "event": "start"}
                )
                + "\n"
            )
        f.write("{torn line\n")
    log = ExitLog(log_path, now_ms=now_ms)
    log.install("brain")  # hooks are installed in the test process; harmless (record once)
    msg = log.report_unlogged()
    assert msg is not None and "2 runs ended this way" in msg
    assert [d["run"] for d in log.unlogged_runs()] == ["aaaa", "bbbb"]
    assert log.record_exit("normal") is True
    assert log.record_exit("normal") is False  # only one exit line per run
    runs = {d["run"]: d for d in log.read() if d["event"] == "exit"}
    assert log.run_id in runs and runs[log.run_id]["reason"] == "normal"

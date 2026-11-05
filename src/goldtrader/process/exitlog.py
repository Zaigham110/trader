"""Exit log: one line per process start and per exit path, and detection of unlogged exits.

ARCHITECTURE.md §5 "PC reboot / power loss — detection: exit log missing". :meth:`ExitLog.install`
writes a ``start`` line and hooks ``atexit`` (normal exit), ``sys.excepthook`` (uncaught exception
with stack) and ``SIGTERM`` / ``SIGINT`` so every known exit path leaves an ``exit`` line with its
reason. At the next start :meth:`ExitLog.report_unlogged` finds a previous run that started but
never wrote an exit line (kill -9, power loss, reboot) and returns a message for the owner.

Each line is JSON: ``{"ms", "process", "pid", "run", "event": "start"|"exit", "reason", "detail"}``.
No secrets are ever written here (only process names, pids, reasons and exception text).
"""

from __future__ import annotations

import atexit
import json
import os
import secrets
import signal
import sys
import threading
import time
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType, TracebackType
from typing import Any

_SIGNAL_NAMES = {
    getattr(signal, "SIGTERM", None): "SIGTERM",
    getattr(signal, "SIGINT", None): "SIGINT",
}


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def boot_time_ms() -> int | None:
    """Host boot time (Linux ``/proc/stat btime``); ``None`` where unavailable."""
    try:
        with open("/proc/stat", encoding="utf-8") as f:
            for line in f:
                if line.startswith("btime "):
                    return int(line.split()[1]) * 1000
    except OSError:
        return None
    return None


class ExitLog:
    """Append-only exit log at ``path``. Safe to call from signal handlers (plain file append)."""

    def __init__(self, path: str | Path, *, now_ms: Callable[[], int] | None = None):
        self.path = Path(path)
        self.process: str | None = None
        self.run_id: str | None = None
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._logged_exit = False
        self._lock = threading.Lock()
        self._prev_excepthook: Callable[..., Any] | None = None
        self._prev_handlers: dict[int, Any] = {}

    # --- writing -----------------------------------------------------------------------------
    def _write(self, event: str, reason: str | None = None, detail: str | None = None) -> None:
        line = {
            "ms": self._now_ms(),
            "process": self.process,
            "pid": os.getpid(),
            "run": self.run_id,
            "event": event,
            "reason": reason,
            "detail": detail,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, default=str) + "\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass

    def record_exit(self, reason: str, detail: str | None = None) -> bool:
        """Write the exit line once; later calls (e.g. atexit after a signal) are no-ops."""
        with self._lock:
            if self._logged_exit:
                return False
            self._logged_exit = True
        self._write("exit", reason, detail)
        return True

    # --- hooks -------------------------------------------------------------------------------
    def install(self, process_name: str) -> str:
        """Write the start marker and hook atexit, excepthook, SIGTERM and SIGINT. Returns run id."""
        self.process = process_name
        self.run_id = secrets.token_hex(4)
        self._logged_exit = False
        self._write("start")
        atexit.register(self._on_atexit)
        self._prev_excepthook = sys.excepthook
        sys.excepthook = self._on_exception
        if threading.current_thread() is threading.main_thread():
            for sig in _SIGNAL_NAMES:
                if sig is None:
                    continue
                try:
                    self._prev_handlers[int(sig)] = signal.signal(sig, self._on_signal)
                except (ValueError, OSError):  # pragma: no cover - not supported on this platform
                    pass
        return self.run_id

    def _on_atexit(self) -> None:
        self.record_exit("normal")

    def _on_exception(
        self,
        exc_type: type[BaseException],
        exc: BaseException,
        tb: TracebackType | None,
    ) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            self.record_exit("SIGINT")
        else:
            stack = "".join(traceback.format_exception(exc_type, exc, tb))[-4000:]
            self.record_exit("exception", stack)
        if self._prev_excepthook is not None:
            self._prev_excepthook(exc_type, exc, tb)

    def _on_signal(self, signum: int, frame: FrameType | None) -> None:
        name = next(
            (n for s, n in _SIGNAL_NAMES.items() if s is not None and int(s) == signum), None
        )
        self.record_exit(name or f"signal:{signum}")
        prev = self._prev_handlers.get(signum)
        if callable(prev):
            prev(signum, frame)
            return
        if name == "SIGINT":
            raise KeyboardInterrupt
        raise SystemExit(128 + signum)

    # --- reading -----------------------------------------------------------------------------
    def read(self) -> list[dict[str, Any]]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        out: list[dict[str, Any]] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn line from a power loss mid-write
            if isinstance(d, dict):
                out.append(d)
        return out

    def unlogged_runs(self, process_name: str | None = None) -> list[dict[str, Any]]:
        """Start lines (other than the current run) that have no matching exit line."""
        name = process_name or self.process
        starts: dict[str, dict[str, Any]] = {}
        for d in self.read():
            if name is not None and d.get("process") != name:
                continue
            run = str(d.get("run"))
            if d.get("event") == "start":
                starts[run] = d
            elif d.get("event") == "exit":
                starts.pop(run, None)
        if self.run_id is not None:
            starts.pop(self.run_id, None)
        return sorted(starts.values(), key=lambda d: int(d.get("ms") or 0))

    def report_unlogged(self, process_name: str | None = None) -> str | None:
        """Owner-facing message when the previous run left no exit line; ``None`` when clean.

        Call it after :meth:`install` (the current run is excluded) or before it with an explicit
        ``process_name``. ARCHITECTURE.md §5: on start, reconcile and report the gap to the owner.
        """
        runs = self.unlogged_runs(process_name)
        if not runs:
            return None
        last = runs[-1]
        name = last.get("process") or process_name or "process"
        started = int(last.get("ms") or 0)
        msg = (
            f"{name} (pid {last.get('pid')}) started {_iso(started)} and left no exit record: "
            f"it was killed, lost power or the PC rebooted."
        )
        if len(runs) > 1:
            msg += f" {len(runs)} runs ended this way."
        boot = boot_time_ms()
        if boot is not None:
            rel = "after" if boot > started else "before"
            msg += f" Host booted {_iso(boot)} ({rel} that start)."
        return msg

    def mark_reported(self, process_name: str | None = None) -> int:
        """Write synthetic exit lines for the unlogged runs so they are reported only once."""
        runs = self.unlogged_runs(process_name)
        for d in runs:
            line = {
                "ms": self._now_ms(),
                "process": d.get("process"),
                "pid": d.get("pid"),
                "run": d.get("run"),
                "event": "exit",
                "reason": "unlogged",
                "detail": "reported at next start",
            }
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(line) + "\n")
        return len(runs)

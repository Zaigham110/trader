"""PID lock files with liveness check, stale takeover and conflict sets (ARCHITECTURE.md §4).

"PID lock files prevent duplicates": one gateway, one brain per state directory. A lock file holds
``{"pid", "name", "acquired_ms", "argv"}``. :func:`acquire` raises :class:`LockHeld` when a live
process owns the lock (or a conflicting lock, see :data:`CONFLICTS`), and silently takes over a
lock whose process is dead (crash / power loss, ARCHITECTURE.md §5).

Liveness is checked without signalling the process: ``os.kill(pid, 0)`` on POSIX; on Windows
``os.kill`` would *terminate* the target, so ``OpenProcess`` + ``GetExitCodeProcess`` via ctypes
is used instead (stdlib only).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from types import TracebackType

log = logging.getLogger(__name__)

#: Lock names that may not be held at the same time (symmetric). A replay / backtest of the gateway
#: must never run beside the live gateway; the same for the brain.
CONFLICTS: dict[str, frozenset[str]] = {
    "gateway": frozenset({"gateway-replay"}),
    "gateway-replay": frozenset({"gateway"}),
    "brain": frozenset({"brain-replay"}),
    "brain-replay": frozenset({"brain"}),
}


class LockHeld(RuntimeError):
    """Another live process holds the lock (or a conflicting one)."""

    def __init__(self, name: str, pid: int, *, requested: str | None = None):
        what = f"lock {name!r}" if requested in (None, name) else f"conflicting lock {name!r}"
        super().__init__(f"{what} is held by live pid {pid}")
        self.name = name
        self.pid = pid
        self.requested = requested or name


def _pid_alive_windows(pid: int) -> bool:  # pragma: no cover - not executed on Linux CI
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    ERROR_ACCESS_DENIED = 5
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return kernel32.GetLastError() == ERROR_ACCESS_DENIED  # exists but not ours
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True  # cannot tell: assume alive (fail closed)
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def pid_alive(pid: int) -> bool:
    """True when a process with ``pid`` exists. Never sends a real signal."""
    if pid <= 0:
        return False
    if sys.platform == "win32":  # pragma: no cover
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return True  # unknown: fail closed
    return True


def _read_lock(path: Path) -> Mapping[str, object] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}  # unreadable / half-written: treat as stale
    return data if isinstance(data, dict) else {}


def _holder_pid(path: Path) -> int | None:
    """PID recorded in the lock file, ``None`` when the file is absent, ``0`` when unreadable."""
    data = _read_lock(path)
    if data is None:
        return None
    pid = data.get("pid")
    return int(pid) if isinstance(pid, int) and pid > 0 else 0


def lock_path(name: str, state_dir: str | Path) -> Path:
    return Path(state_dir) / "locks" / f"{name}.pid"


class Lock:
    """An acquired lock. Use as a context manager or call :meth:`release`."""

    def __init__(self, name: str, path: Path, pid: int):
        self.name = name
        self.path = path
        self.pid = pid
        self._released = False

    def release(self) -> None:
        """Remove the lock file if it is still ours. Idempotent."""
        if self._released:
            return
        self._released = True
        if _holder_pid(self.path) == self.pid:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass

    def __enter__(self) -> Lock:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    def __repr__(self) -> str:
        return f"Lock(name={self.name!r}, pid={self.pid}, path={str(self.path)!r})"


def _check_conflicts(name: str, state_dir: Path, conflicts: Mapping[str, frozenset[str]]) -> None:
    for other in sorted(conflicts.get(name, ())):
        other_path = lock_path(other, state_dir)
        pid = _holder_pid(other_path)
        if pid is None:
            continue
        if pid and pid_alive(pid):
            raise LockHeld(other, pid, requested=name)
        log.warning("removing stale conflicting lock %s (pid %s dead)", other_path, pid)
        _unlink_quiet(other_path)


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def acquire(
    name: str,
    state_dir: str | Path,
    *,
    conflicts: Mapping[str, frozenset[str]] | None = None,
    retries: int = 3,
) -> Lock:
    """Take the lock ``name`` under ``state_dir/locks``.

    Raises :class:`LockHeld` when a live process holds it (or a conflicting lock). A lock file whose
    PID is dead is taken over. The file is created with ``O_EXCL`` so two starters racing for a free
    lock cannot both win.
    """
    state = Path(state_dir)
    path = lock_path(name, state)
    path.parent.mkdir(parents=True, exist_ok=True)
    conflicts = CONFLICTS if conflicts is None else conflicts
    me = os.getpid()
    for _ in range(max(1, retries)):
        _check_conflicts(name, state, conflicts)
        pid = _holder_pid(path)
        if pid is not None:
            if pid and pid_alive(pid):
                raise LockHeld(name, pid)
            log.warning("taking over stale lock %s (pid %s dead)", path, pid)
            _unlink_quiet(path)
        payload = json.dumps(
            {
                "pid": me,
                "name": name,
                "acquired_ms": int(time.time() * 1000),
                "argv": sys.argv[:3],
            }
        )
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue  # someone else won the race; re-check liveness
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        return Lock(name, path, me)
    holder = _holder_pid(path) or 0
    raise LockHeld(name, holder)

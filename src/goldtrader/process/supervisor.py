"""Child-process supervisor: restart with exponential backoff, record every exit.

ARCHITECTURE.md §4: ``gold-gateway`` and ``gold-brain`` "restart on failure with backoff". The
supervisor spawns each :class:`ChildSpec` with ``subprocess.Popen``, restarts it after it exits
(1 s doubling to 60 s; the delay resets once a child has run ``healthy_reset_s`` without dying),
stops children with SIGTERM then SIGKILL after 15 s, and reports every exit through an injected
callback (:func:`db_exit_recorder` writes ``process_exits``, migration 003). ``env_strip`` removes
named variables from the child's environment so e.g. ``MT5_PASSWORD`` never reaches the brain
(ARCHITECTURE.md §6, R-053).

Windows-compatible: no ``os.setsid``; ``CREATE_NEW_PROCESS_GROUP`` only on win32. Time and sleep
are injectable so tests can drive the backoff schedule without waiting.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

BACKOFF_INITIAL_S = 1.0
BACKOFF_MAX_S = 60.0
HEALTHY_RESET_S = 600.0
STOP_GRACE_S = 15.0


@dataclass(frozen=True)
class ChildSpec:
    """One supervised process."""

    name: str
    argv: Sequence[str]
    env_strip: Sequence[str] = ()
    env: Mapping[str, str] | None = None  # extra / overriding variables
    cwd: str | None = None


@dataclass(frozen=True)
class ChildExit:
    """What :class:`Supervisor` reports for every child exit (→ ``process_exits``)."""

    process: str
    pid: int
    exit_code: int | None
    started_ms: int
    exited_ms: int
    reason: str  # exited | crashed | signal:<n> | stopped


ExitCallback = Callable[[ChildExit], None]
PopenFactory = Callable[..., subprocess.Popen]


def db_exit_recorder(conn: sqlite3.Connection) -> ExitCallback:
    """Return a callback that inserts each :class:`ChildExit` into ``process_exits``."""

    def record(ex: ChildExit) -> None:
        with conn:
            conn.execute(
                "INSERT INTO process_exits(process, pid, exit_code, started_ms, exited_ms, reason) "
                "VALUES (?,?,?,?,?,?)",
                (ex.process, ex.pid, ex.exit_code, ex.started_ms, ex.exited_ms, ex.reason),
            )

    return record


def build_child_env(
    base: Mapping[str, str], strip: Iterable[str], extra: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Copy ``base`` without the ``strip`` names, then apply ``extra``."""
    out = {k: v for k, v in base.items() if k not in set(strip)}
    if extra:
        out.update(extra)
    return out


def exit_reason(code: int | None, stopped: bool) -> str:
    if stopped:
        return "stopped"
    if code is None:
        return "unknown"
    if code == 0:
        return "exited"
    if code < 0:
        return f"signal:{-code}"
    return "crashed"


@dataclass
class _Child:
    spec: ChildSpec
    proc: subprocess.Popen | None = None
    pid: int = 0
    started_mono: float = 0.0
    started_ms: int = 0
    backoff_s: float = BACKOFF_INITIAL_S
    restart_due_mono: float = 0.0
    restarts: int = 0
    stopping: bool = False
    exits: list[ChildExit] = field(default_factory=list)


class Supervisor:
    """Keep every :class:`ChildSpec` running. Drive it with :meth:`run` or :meth:`poll` + a loop."""

    def __init__(
        self,
        specs: Sequence[ChildSpec],
        *,
        on_exit: ExitCallback | None = None,
        backoff_initial_s: float = BACKOFF_INITIAL_S,
        backoff_max_s: float = BACKOFF_MAX_S,
        healthy_reset_s: float = HEALTHY_RESET_S,
        stop_grace_s: float = STOP_GRACE_S,
        clock: Callable[[], float] = time.monotonic,
        wall_ms: Callable[[], int] = lambda: int(time.time() * 1000),
        popen: PopenFactory = subprocess.Popen,
        base_env: Mapping[str, str] | None = None,
    ):
        names = [s.name for s in specs]
        if len(set(names)) != len(names):
            raise ValueError("child names must be unique")
        self._children: dict[str, _Child] = {
            s.name: _Child(spec=s, backoff_s=backoff_initial_s) for s in specs
        }
        self.on_exit = on_exit
        self.backoff_initial_s = backoff_initial_s
        self.backoff_max_s = backoff_max_s
        self.healthy_reset_s = healthy_reset_s
        self.stop_grace_s = stop_grace_s
        self._clock = clock
        self._wall_ms = wall_ms
        self._popen = popen
        self._base_env = base_env
        self._stopped = False

    # --- introspection -----------------------------------------------------------------------
    @property
    def names(self) -> list[str]:
        return list(self._children)

    def pid(self, name: str) -> int | None:
        c = self._children[name]
        return c.proc.pid if c.proc is not None and c.proc.poll() is None else None

    def is_running(self, name: str) -> bool:
        return self.pid(name) is not None

    def backoff(self, name: str) -> float:
        """Delay that will be applied before the *next* restart of ``name``."""
        return self._children[name].backoff_s

    def restarts(self, name: str) -> int:
        return self._children[name].restarts

    def exits(self, name: str) -> list[ChildExit]:
        return list(self._children[name].exits)

    def wait_child(self, name: str, timeout_s: float) -> int | None:
        """Block until the current process of ``name`` exits (tests / shutdown); return its code."""
        c = self._children[name]
        if c.proc is None:
            return None
        try:
            return c.proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return None

    # --- lifecycle ---------------------------------------------------------------------------
    def start(self) -> None:
        """Spawn every child now."""
        self._stopped = False
        for c in self._children.values():
            if c.proc is None:
                self._spawn(c)

    def _spawn(self, c: _Child) -> None:
        env = build_child_env(
            self._base_env if self._base_env is not None else os.environ,
            c.spec.env_strip,
            c.spec.env,
        )
        kwargs: dict[str, object] = {"env": env, "cwd": c.spec.cwd}
        if sys.platform == "win32":  # pragma: no cover
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        c.proc = self._popen(list(c.spec.argv), **kwargs)
        c.pid = c.proc.pid
        c.started_mono = self._clock()
        c.started_ms = self._wall_ms()
        c.stopping = False
        log.info("started %s pid %s", c.spec.name, c.pid)

    def _record_exit(self, c: _Child, code: int | None, stopped: bool) -> ChildExit:
        ex = ChildExit(
            process=c.spec.name,
            pid=c.pid,
            exit_code=code,
            started_ms=c.started_ms,
            exited_ms=self._wall_ms(),
            reason=exit_reason(code, stopped),
        )
        c.exits.append(ex)
        c.proc = None
        if self.on_exit is not None:
            try:
                self.on_exit(ex)
            except Exception:  # noqa: BLE001 - recording must never kill the supervisor
                log.exception("exit callback failed for %s", c.spec.name)
        return ex

    def poll(self) -> list[ChildExit]:
        """Reap exited children, schedule / perform restarts. Call this often (every ≤ 1 s)."""
        now = self._clock()
        seen: list[ChildExit] = []
        for c in self._children.values():
            if c.proc is not None:
                code = c.proc.poll()
                if code is None:
                    continue
                ran_s = now - c.started_mono
                ex = self._record_exit(c, code, stopped=c.stopping)
                seen.append(ex)
                if self._stopped:
                    continue
                if ran_s >= self.healthy_reset_s:
                    c.backoff_s = self.backoff_initial_s
                delay = c.backoff_s
                c.restart_due_mono = now + delay
                c.backoff_s = min(self.backoff_max_s, c.backoff_s * 2)
                log.warning(
                    "%s exited (%s, code %s) after %.1fs; restart in %.0fs",
                    c.spec.name,
                    ex.reason,
                    code,
                    ran_s,
                    delay,
                )
                continue
            if not self._stopped and now >= c.restart_due_mono:
                c.restarts += 1
                self._spawn(c)
        return seen

    def run(self, stop_event: threading.Event, tick_s: float = 0.5) -> None:
        """Start, poll until ``stop_event`` is set, then :meth:`stop`."""
        self.start()
        try:
            while not stop_event.is_set():
                self.poll()
                stop_event.wait(tick_s)
        finally:
            self.stop()

    def stop(self) -> list[ChildExit]:
        """SIGTERM every live child, SIGKILL whatever is still alive after ``stop_grace_s``."""
        self._stopped = True
        live = [c for c in self._children.values() if c.proc is not None and c.proc.poll() is None]
        for c in live:
            c.stopping = True
            assert c.proc is not None
            try:
                c.proc.terminate()
            except OSError:
                pass
        deadline = time.monotonic() + self.stop_grace_s
        for c in live:
            assert c.proc is not None
            remaining = max(0.0, deadline - time.monotonic())
            try:
                c.proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                log.warning("%s ignored SIGTERM for %.0fs; killing", c.spec.name, self.stop_grace_s)
                try:
                    c.proc.kill()
                except OSError:
                    pass
                c.proc.wait()
        out: list[ChildExit] = []
        for c in self._children.values():
            if c.proc is not None and c.proc.poll() is not None:
                out.append(self._record_exit(c, c.proc.returncode, stopped=c.stopping))
        return out

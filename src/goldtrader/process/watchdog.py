"""Loop watchdog: end a process whose main loop has stopped making progress (D-065).

The gateway's loop calls into the MT5 terminal, and those calls have no timeout. On 2026-11-17 one
of them never returned at the daily break: the process stayed alive, held its lock, answered
nothing, and nobody was told for five and a half hours.

`Watchdog.beat()` is called after every loop step. A daemon thread checks the time since the last
beat; past `limit_s` it writes every thread's stack to `dump_path` (so the stuck call is known next
time), records the reason in the exit log and ends the process with a non-zero code. The scheduled
task (or the supervisor) then starts a fresh one, which reconciles with the broker as on any start.

It cannot make the stuck call return; it only refuses to stay stuck silently.
"""

from __future__ import annotations

import faulthandler
import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

EXIT_CODE = 75  # "temporary failure": the task scheduler treats non-zero as failed and restarts


class Watchdog:
    def __init__(
        self,
        limit_s: float,
        *,
        name: str = "loop",
        dump_path: str | Path | None = None,
        on_exit: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        terminate: Callable[[int], None] = os._exit,
        check_s: float = 5.0,
    ) -> None:
        if limit_s <= 0:
            raise ValueError("limit_s must be positive")
        self.limit_s = float(limit_s)
        self.name = name
        self.dump_path = Path(dump_path) if dump_path is not None else None
        self.on_exit = on_exit
        self.clock = clock
        self.terminate = terminate
        self.check_s = check_s
        self._last = clock()
        self._fired = False
        self._thread: threading.Thread | None = None

    def beat(self) -> None:
        self._last = self.clock()

    def stuck_for(self) -> float:
        return self.clock() - self._last

    def check(self) -> bool:
        """End the process if the loop is stuck. Returns True when it fired (tests inject
        `terminate`; in production this call does not return)."""
        age = self.stuck_for()
        if self._fired or age < self.limit_s:
            return False
        self._fired = True
        reason = f"{self.name} made no progress for {age:.0f}s (limit {self.limit_s:.0f}s)"
        log.critical("watchdog: %s; ending the process so it is restarted", reason)
        self._dump(reason)
        if self.on_exit is not None:
            try:
                self.on_exit(reason)
            except Exception:  # noqa: BLE001 - nothing may stop the exit
                pass
        self.terminate(EXIT_CODE)
        return True

    def _dump(self, reason: str) -> None:
        if self.dump_path is None:
            return
        try:
            self.dump_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.dump_path, "a", encoding="utf-8") as f:
                f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} watchdog: {reason}\n")
                f.flush()
                faulthandler.dump_traceback(file=f, all_threads=True)
        except Exception:  # noqa: BLE001
            pass

    def stop(self) -> None:
        """The loop ended on purpose (shutdown): stand down so a clean exit is not 'stuck'."""
        self._fired = True

    def start(self) -> None:
        if self._thread is not None:
            return
        self.beat()

        def loop() -> None:
            while not self._fired:
                before = self.clock()
                time.sleep(self.check_s)
                if self.clock() - before > self.check_s + 30:
                    # this thread was frozen too: the PC slept or was suspended, the loop is not
                    # stuck. Give it a full limit to get going again.
                    self.beat()
                    continue
                self.check()

        self._thread = threading.Thread(target=loop, name=f"watchdog-{self.name}", daemon=True)
        self._thread.start()


STALE_AFTER_MS = 180_000
REPEAT_MS = 3_600_000


def watch_heartbeat(conn, process: str, now_ms: int) -> str | None:
    """Alert the owner when `process` has stopped reporting, and once more when it is back.
    Returns 'stale', 'recovered' or None. Called by a different process than the one watched."""
    row = conn.execute("SELECT last_ms FROM heartbeats WHERE process=?", (process,)).fetchone()
    if row is None:
        return None
    age = now_ms - int(row[0])
    kind_down, kind_up = f"{process}_stale", f"{process}_recovered"
    last = conn.execute(
        "SELECT kind, created_ms FROM alerts WHERE kind IN (?,?) ORDER BY id DESC LIMIT 1",
        (kind_down, kind_up),
    ).fetchone()

    def alert(severity: str, kind: str, message: str) -> None:
        conn.execute(
            "INSERT INTO alerts(created_ms, severity, kind, message, details_json) "
            "VALUES (?,?,?,?,'{}')",
            (now_ms, severity, kind, message),
        )

    if age > STALE_AFTER_MS:
        if last is not None and last[0] == kind_down and now_ms - int(last[1]) < REPEAT_MS:
            return None
        alert(
            "critical",
            kind_down,
            f"The {process} has not reported for {age // 60_000} minutes. No trading decisions "
            "can be made until it is back. Open positions keep their stop at the broker.",
        )
        return "stale"
    if last is not None and last[0] == kind_down:
        alert("info", kind_up, f"The {process} is reporting again.")
        return "recovered"
    return None

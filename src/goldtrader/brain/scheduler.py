"""Serial job scheduler on wall-clock boundaries (ARCHITECTURE §2.2 `scheduler`).

Jobs never overlap (one worker), run at `every_seconds` boundaries or `daily_at` HH:MM UTC, and can
be triggered out of band. The clock is injectable so tests run instantly.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

log = logging.getLogger(__name__)


@dataclass
class Job:
    name: str
    fn: Callable[[], None]
    every_seconds: int | None = None
    daily_at_utc: str | None = None  # "HH:MM"
    only_when: Callable[[], bool] | None = None
    last_run_s: int | None = None
    runs: int = 0
    failures: int = 0
    last_error: str | None = None
    pending_trigger: bool = False

    def due(self, now_s: int) -> bool:
        if self.pending_trigger:
            return True
        if self.every_seconds:
            boundary = now_s - (now_s % self.every_seconds)
            return self.last_run_s is None or self.last_run_s < boundary
        if self.daily_at_utc:
            hh, mm = (int(x) for x in self.daily_at_utc.split(":"))
            dt = datetime.fromtimestamp(now_s, UTC)
            target = int(dt.replace(hour=hh, minute=mm, second=0, microsecond=0).timestamp())
            return now_s >= target and (self.last_run_s is None or self.last_run_s < target)
        return False


@dataclass
class Scheduler:
    clock: Callable[[], float]
    jobs: list[Job] = field(default_factory=list)

    def add(self, job: Job) -> Job:
        self.jobs.append(job)
        return job

    def trigger(self, name: str) -> None:
        for j in self.jobs:
            if j.name == name:
                j.pending_trigger = True

    def tick(self) -> list[str]:
        """Run every due job once, in registration order. Returns the names that ran."""
        now_s = int(self.clock())
        ran: list[str] = []
        for j in self.jobs:
            if not j.due(now_s):
                continue
            if j.only_when is not None and not j.only_when():
                j.last_run_s = now_s
                j.pending_trigger = False
                continue
            try:
                j.fn()
                j.runs += 1
                j.last_error = None
            except Exception as e:  # a failing job must never stop the others
                j.failures += 1
                j.last_error = f"{type(e).__name__}: {e}"
                log.exception("job %s failed", j.name)
            finally:
                j.last_run_s = now_s
                j.pending_trigger = False
            ran.append(j.name)
        return ran

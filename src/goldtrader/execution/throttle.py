"""`Throttle`: request pacing for the execution engine (docs/MT5_EXNESS_INTEGRATION.md §4 "Throttle").

* token bucket: ≤ `rate_per_s` trade requests per second sustained, `burst` tokens of headroom;
* exponential backoff on 10024 TOO_MANY_REQUESTS: 1, 2, 4, 8 … s capped at `max_backoff_s`,
  reset after a successful send; a short fixed pause for other transient retcodes;
* `coalesce`: a repeated SL/TP modification for the same position with the same levels inside
  `coalesce_ms` is sent once.

`clock` returns epoch milliseconds and `sleep` takes seconds; both are injectable so tests run with
a fixed clock and no real waiting. After a wait the bucket assumes the time has passed, so a no-op
sleep in tests never loops.
"""

from __future__ import annotations

import time
from collections.abc import Callable


class Throttle:
    def __init__(
        self,
        *,
        clock: Callable[[], int] | None = None,
        sleep: Callable[[float], None] | None = None,
        rate_per_s: float = 1.0,
        burst: int = 2,
        transient_pause_s: float = 0.5,
        max_backoff_s: float = 60.0,
        coalesce_ms: int = 1000,
    ) -> None:
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.sleep = sleep or time.sleep
        self.rate = rate_per_s
        self.burst = burst
        self.transient_pause_s = transient_pause_s
        self.max_backoff_s = max_backoff_s
        self.coalesce_ms = coalesce_ms
        self._tokens = float(burst)
        self._last_ms = self.clock()
        self._rate_limit_hits = 0
        self._recent: dict[int, tuple[int, float, float]] = {}  # position → (ms, sl, tp)
        self.waits: list[float] = []  # seconds slept, for tests and diagnostics

    # ------------------------------------------------------------------ token bucket
    def acquire(self) -> float:
        """Take one token, sleeping until one is available; returns the seconds waited."""
        now = self.clock()
        elapsed = max(0, now - self._last_ms) / 1000.0
        self._last_ms = now
        self._tokens = min(float(self.burst), self._tokens + elapsed * self.rate)
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return 0.0
        wait = (1.0 - self._tokens) / self.rate
        self._sleep(wait)
        self._tokens = 0.0
        self._last_ms = now + int(wait * 1000)
        return wait

    # ------------------------------------------------------------------ backoff
    def backoff(self, retcode: int | None = None) -> float:
        """Pause before a retry: exponential for 10024, a short fixed pause otherwise."""
        if retcode == 10024:
            wait = min(self.max_backoff_s, float(2**self._rate_limit_hits))
            self._rate_limit_hits += 1
        else:
            wait = self.transient_pause_s
        self._sleep(wait)
        return wait

    def reset_backoff(self) -> None:
        self._rate_limit_hits = 0

    @property
    def rate_limit_hits(self) -> int:
        return self._rate_limit_hits

    # ------------------------------------------------------------------ coalescing
    def coalesce(self, position_id: int, sl: float, tp: float) -> bool:
        """True when an identical SL/TP for `position_id` was sent inside the coalesce window."""
        prev = self._recent.get(position_id)
        return (
            prev is not None and self.clock() - prev[0] < self.coalesce_ms and prev[1:] == (sl, tp)
        )

    def note_sent(self, position_id: int, sl: float, tp: float) -> None:
        """Record a sent SL/TP so an identical repeat inside the window is coalesced."""
        self._recent[position_id] = (self.clock(), sl, tp)

    def forget(self, position_id: int) -> None:
        self._recent.pop(position_id, None)

    # ------------------------------------------------------------------ internals
    def _sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        if seconds > 0:
            self.sleep(seconds)


__all__ = ["Throttle"]

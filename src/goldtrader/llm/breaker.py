"""Per-agent circuit breaker (R-023; docs/LOCAL_LLM.md §5.5).

closed --3 consecutive failures--> open --wait--> half_open (one trial)
half_open --success--> closed | --failure--> open with the wait doubled (30 min → … → 6 h max)

State is persisted through `LLMStore.get_state` / `set_state` so a restart keeps an open breaker
open. Every transition is appended to `events` for the alerting layer (health → Telegram).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


class BreakerStore(Protocol):
    def get_state(self, agent: str) -> dict[str, Any] | None: ...

    def set_state(self, agent: str, state: dict[str, Any]) -> None: ...


@dataclass(frozen=True)
class BreakerEvent:
    agent: str
    at_ms: int
    from_state: str
    to_state: str
    reason: str


@dataclass
class CircuitBreaker:
    agent: str
    store: BreakerStore | None = None
    failures_to_open: int = 3
    base_wait_s: int = 1800
    max_wait_s: int = 21600
    state: str = CLOSED
    failures: int = 0
    opened_ms: int | None = None
    next_trial_ms: int | None = None
    events: list[BreakerEvent] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.store is not None:
            saved = self.store.get_state(self.agent)
            if saved is not None:
                self.state = saved["state"]
                self.failures = int(saved.get("failures", 0))
                self.opened_ms = saved.get("opened_ms")
                self.next_trial_ms = saved.get("next_trial_ms")

    # --- queries ----------------------------------------------------------------------------
    @property
    def wait_s(self) -> int:
        """The current open wait, derived from the persisted timestamps (no extra column)."""
        if self.opened_ms is None or self.next_trial_ms is None:
            return self.base_wait_s
        return max(int((self.next_trial_ms - self.opened_ms) / 1000), self.base_wait_s)

    def allows(self, now_ms: int) -> bool:
        """May the agent run now? Moves open → half_open when the wait has elapsed and lets exactly
        one trial through; further calls while half-open are refused until the trial reports."""
        if self.state == CLOSED:
            return True
        if self.state == OPEN:
            if self.next_trial_ms is not None and now_ms >= self.next_trial_ms:
                self._transition(HALF_OPEN, now_ms, "wait elapsed; one trial run allowed")
                return True
            return False
        return False  # half-open trial in flight

    # --- outcomes ---------------------------------------------------------------------------
    def record_success(self, now_ms: int) -> None:
        self.failures = 0
        if self.state != CLOSED:
            self.opened_ms = None
            self.next_trial_ms = None
            self._transition(CLOSED, now_ms, "trial succeeded")
        else:
            self._persist(now_ms)

    def record_failure(self, now_ms: int, reason: str = "failure") -> None:
        if self.state == HALF_OPEN:
            wait = min(self.wait_s * 2, self.max_wait_s)
            self.failures += 1
            self.opened_ms = now_ms
            self.next_trial_ms = now_ms + wait * 1000
            self._transition(OPEN, now_ms, f"trial failed ({reason}); next trial in {wait}s")
            return
        self.failures += 1
        if self.state == CLOSED and self.failures >= self.failures_to_open:
            self.opened_ms = now_ms
            self.next_trial_ms = now_ms + self.base_wait_s * 1000
            self._transition(
                OPEN,
                now_ms,
                f"{self.failures} consecutive failures ({reason}); next trial in {self.base_wait_s}s",
            )
        else:
            self._persist(now_ms)

    # --- internals --------------------------------------------------------------------------
    def _transition(self, to_state: str, now_ms: int, reason: str) -> None:
        self.events.append(BreakerEvent(self.agent, now_ms, self.state, to_state, reason))
        self.state = to_state
        self._persist(now_ms)

    def _persist(self, now_ms: int) -> None:
        if self.store is not None:
            self.store.set_state(self.agent, self.to_dict(now_ms))

    def to_dict(self, now_ms: int) -> dict[str, Any]:
        return {
            "state": self.state,
            "failures": self.failures,
            "opened_ms": self.opened_ms,
            "next_trial_ms": self.next_trial_ms,
            "updated_ms": now_ms,
        }

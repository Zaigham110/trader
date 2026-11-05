"""Daily / monthly USD budget for bot calls (R-023; docs/LOCAL_LLM.md §4).

Budget reached → every bot call stops; execution safety, alerts and the kill command do not go
through this module and keep working (S-7). Spend is read from the `llm_calls` ledger so a restart
cannot reset it. Days and months are UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol


class SpendStore(Protocol):
    def spent_since(self, since_ms: int) -> float: ...


def day_start_ms(now_ms: int) -> int:
    d = datetime.fromtimestamp(now_ms / 1000, tz=UTC)
    return int(d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


def month_start_ms(now_ms: int) -> int:
    d = datetime.fromtimestamp(now_ms / 1000, tz=UTC)
    return int(d.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


@dataclass(frozen=True)
class Budget:
    daily_usd: float
    monthly_usd: float

    def spent_today(self, store: SpendStore, now_ms: int) -> float:
        return store.spent_since(day_start_ms(now_ms))

    def spent_month(self, store: SpendStore, now_ms: int) -> float:
        return store.spent_since(month_start_ms(now_ms))

    def refusal(self, store: SpendStore, now_ms: int, estimated_cost: float = 0.0) -> str | None:
        """None when a call costing `estimated_cost` fits; else the reason it does not."""
        today = self.spent_today(store, now_ms)
        if today + estimated_cost > self.daily_usd:
            return f"daily budget {self.daily_usd:.2f} USD reached (spent {today:.4f})"
        month = self.spent_month(store, now_ms)
        if month + estimated_cost > self.monthly_usd:
            return f"monthly budget {self.monthly_usd:.2f} USD reached (spent {month:.4f})"
        return None

    def allows(self, store: SpendStore, now_ms: int, estimated_cost: float = 0.0) -> bool:
        return self.refusal(store, now_ms, estimated_cost) is None

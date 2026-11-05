"""Shared learning-system types (D-046)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Stage(StrEnum):
    PROPOSED = "proposed"
    OFFLINE = "offline"
    DEMO = "demo"
    SHADOW = "shadow"
    LIMITED_LIVE = "limited_live"
    PROMOTED = "promoted"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"


ACTIVE_STAGES = (Stage.OFFLINE, Stage.DEMO, Stage.SHADOW, Stage.LIMITED_LIVE)
NEXT_STAGE = {
    Stage.PROPOSED: Stage.OFFLINE,
    Stage.OFFLINE: Stage.DEMO,
    Stage.DEMO: Stage.SHADOW,
    Stage.SHADOW: Stage.LIMITED_LIVE,
    Stage.LIMITED_LIVE: Stage.PROMOTED,
}


@dataclass(frozen=True)
class Change:
    """Exactly one change relative to the champion (D-046)."""

    change_type: str  # param | rule | prompt
    key: str  # param name, 'kind', or prompt name
    value: Any  # new value / new kind / new prompt version
    old_value: Any = None


@dataclass
class Experiment:
    experiment_id: str
    strategy_id: str
    candidate_bundle_id: str
    champion_bundle_id: str
    change: Change
    pre_registered_metric: str
    stage: Stage
    stage_entered_ms: int
    created_ms: int
    hypothesis_id: str | None = None
    regime_scope: str | None = None
    closed_ms: int | None = None
    close_reason: str | None = None
    stats: dict[str, Any] = field(default_factory=dict)


@dataclass
class GateCheck:
    name: str
    ok: bool
    detail: str


@dataclass
class StageDecision:
    experiment_id: str
    from_stage: Stage
    to_stage: Stage
    checks: list[GateCheck]
    actor: str = "pipeline"

    @property
    def advanced(self) -> bool:
        return self.to_stage != self.from_stage and self.to_stage not in (
            Stage.REJECTED,
            Stage.ROLLED_BACK,
        )

    def as_dict(self) -> dict:
        return {"checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks]}

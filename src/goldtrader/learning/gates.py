"""Learning gates: the numeric thresholds of LEARNING_SYSTEM §3 as a frozen, owner-only model.

D-045: the gates live in `owner.learning`, are hash-checked with the rest of the owner section and
are never learnable (R-034). This module only *reads* them: `gates_for(cfg)` returns
`cfg.owner.learning` when the schema carries it and the §3 defaults otherwise. There is no writer.

Every default is an Assumption (research/04 §7 "Default gates") until demo data exists.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from goldtrader.backtest.gate import GateThresholds

DAY_MS = 86_400_000


class LearningGates(BaseModel):
    """LEARNING_SYSTEM §3.1–§3.5 thresholds. Frozen; extra keys are refused."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # §3.1 intake
    max_proposals_per_week: int = 10
    # §3.2 offline gate
    spread_multiplier: float = 1.5
    offline_min_trades: int = 100
    offline_min_days: int = 120
    offline_min_sharpe: float = 0.8
    offline_max_pbo: float = 0.05
    offline_min_dsr: float = 0.95
    offline_min_haircut_sr: float = 0.5
    offline_max_perm_p: float = 0.05
    offline_fdr: float = 0.10
    min_sharpe_gain: float = 0.1  # candidate OOS Sharpe ≥ champion + gain on the same data
    # §3.3 demo and shadow
    demo_min_days: int = 60
    demo_min_trades: int = 100
    shadow_min_days: int = 30
    shadow_min_trades: int = 50
    shadow_max_slippage_ratio: float = 1.25
    # §3.4 limited live and promotion
    live_min_days: int = 90
    live_min_trades: int = 100
    evalue_threshold: float = 20.0  # α = 0.05 by Ville's inequality
    evalue_bet_cap: float = 0.5
    challenger_share: float = 0.25  # D-047: 1 in 4 qualifying signals
    max_experiment_days: int = 180  # "stop after 6 months"
    # §3.5 rollback
    rollback_window_days: int = 30

    def offline_thresholds(self) -> GateThresholds:
        """The backtest gate's threshold object built from these gates (LEARNING_SYSTEM §3.2)."""
        return GateThresholds(
            min_trades=self.offline_min_trades,
            min_days=self.offline_min_days,
            min_sharpe=self.offline_min_sharpe,
            max_pbo=self.offline_max_pbo,
            min_dsr=self.offline_min_dsr,
            min_haircut_sr=self.offline_min_haircut_sr,
            max_perm_p=self.offline_max_perm_p,
            spread_multiplier=self.spread_multiplier,
        )


def gates_for(cfg: Any, override: LearningGates | None = None) -> LearningGates:
    """Resolve the gates for a config: an explicit `override` (tests), else `cfg.owner.learning`
    (D-045; accepted as a LearningGates, any pydantic model or a mapping with the same keys), else
    the §3 defaults. Read-only by construction."""
    if override is not None:
        return override
    owner = getattr(cfg, "owner", None)
    section = getattr(owner, "learning", None)
    if section is None:
        return LearningGates()
    if isinstance(section, LearningGates):
        return section
    if isinstance(section, BaseModel):
        return LearningGates.model_validate(section.model_dump())
    return LearningGates.model_validate(dict(section))

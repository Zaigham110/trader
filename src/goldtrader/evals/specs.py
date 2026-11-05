"""Eval-only `AgentSpec`s (D-040; LOCAL_LLM.md §7 "indicator table → regime label").

The headline and review sets run the real bot specs from `goldtrader.bots`; the regime set needs a
small spec of its own because no production bot emits a bare regime label. The prompt gives the
same thresholds `goldtrader.data.features` uses, so the set measures rubric-following on a flat
enum output, which is what the local-model migration needs to know (LOCAL_LLM §2, §8).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from goldtrader.data.features import ATR_PCT_CALM_DAILY, ATR_PCT_STRESS_DAILY
from goldtrader.llm.prompts import register
from goldtrader.llm.runtime import AgentSpec

REGIME_FEATURES: tuple[str, ...] = (
    "atr_pct_daily",
    "rv_pctile",
    "trend_state",
    "spread_regime",
    "hour_utc",
)

REGIME_PROMPT = register(
    "evals.regime.system",
    1,
    "You label the volatility regime of the gold (XAUUSD) market from a small table of "
    "code-computed features. Rule: the daily-equivalent ATR fraction `atr_pct_daily` decides the "
    f"label. Below {ATR_PCT_CALM_DAILY:.3f} (1.2% of price per day) the regime is `calm`; above "
    f"{ATR_PCT_STRESS_DAILY:.3f} (2.5%) it is `stress`; exactly on or between the thresholds it is "
    "`normal`. The other features (realised-vol percentile, trend state, spread regime, hour) are "
    "context only and never change the label. Output the RegimeLabel JSON only: `regime` and a "
    "`reason` of at most 120 characters naming the atr_pct_daily value and the threshold it met.",
)


class RegimeLabel(BaseModel):
    """Flat eval output (LOCAL_LLM §2): one enum plus a short reason."""

    model_config = ConfigDict(extra="forbid")

    regime: Literal["calm", "normal", "stress"]
    reason: str = Field(max_length=120)


def regime_table(features: dict[str, Any]) -> str:
    """The feature dict as the same `key  value` text table the bots use."""
    rows = [(k, _fmt(features.get(k))) for k in REGIME_FEATURES]
    width = max(len(k) for k, _ in rows)
    return "\n".join(f"{k.ljust(width)}  {v}" for k, v in rows)


def _fmt(v: Any) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


def build_regime_messages(ctx: Any) -> list[dict[str, Any]]:
    """`ctx` is a dict with `features` (the eval case inputs); no BotContext is needed."""
    features = ctx.get("features", {}) if isinstance(ctx, dict) else {}
    text = f"Features:\n{regime_table(features)}\n\nReturn the RegimeLabel JSON."
    return [{"role": "user", "content": text}]


def regime_spec(cfg: Any) -> AgentSpec:
    """Regime-labelling `AgentSpec` (tier `fast`, no tools, no fallback: a failed run is scored as
    schema-invalid rather than hidden behind a rule-based default)."""
    tier = cfg.llm.tiers["fast"]
    return AgentSpec(
        name="eval_regime",
        tier="fast",
        system_prompt=REGIME_PROMPT,
        build_messages=build_regime_messages,
        output_model=RegimeLabel,
        tools=[],
        timeout_s=float(tier.timeout_s),
        max_turns=3,
        max_cost_usd=tier.max_cost_usd,
        effort=tier.effort,
        schema_version=1,
    )

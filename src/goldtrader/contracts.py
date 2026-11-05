"""Brain ↔ gateway contracts: proposals (LLM output after gates) and intents (what the gateway may
execute). Pydantic models so both processes validate the same shapes (ARCHITECTURE §2.1 `api`,
§3.1; AGENTS §3.2). Everything the LLM produces is a *proposal*; only code turns it into an intent.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StrategyProposal(Strict):
    """Strategy bot output schema (AGENTS §3.2). Flat, enum-heavy, no nesting (LOCAL_LLM §2)."""

    action: Literal["wait", "enter", "exit", "move_stop"]
    side: Literal["long", "short"] | None = None
    stop_atr: float | None = Field(default=None, ge=0.05, le=5.0)
    target_atr: float | None = Field(default=None, ge=0.05, le=10.0)
    new_stop_price: float | None = Field(default=None, gt=0)
    confidence: float = Field(ge=0.0, le=1.0)
    thesis: str = Field(min_length=20, max_length=600)
    invalidation: str = Field(min_length=10, max_length=300)
    horizon_minutes: int = Field(ge=15, le=480)
    agrees_with_signal: bool


class RiskAdvice(Strict):
    budget: int = Field(ge=0, le=100)
    reasons: list[str] = Field(default_factory=list, max_length=3)


class NewsEvent(Strict):
    item_id: str
    relevance: Literal["none", "low", "medium", "high"]
    event_type: Literal[
        "fed", "inflation", "employment", "growth", "usd", "geopolitical", "flows", "other"
    ]
    direction_for_gold: Literal["bullish", "bearish", "neutral", "unclear"]
    magnitude: Literal["low", "medium", "high"]
    horizon: Literal["minutes", "hours", "days"]
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str = Field(max_length=200)


class NewsExtraction(Strict):
    events: list[NewsEvent]
    dropped: list[str] = Field(default_factory=list)


class PostMortem(Strict):
    outcome_class: Literal[
        "signal_wrong",
        "timing",
        "stopped_by_noise",
        "news_shock",
        "execution",
        "correct_loss",
        "correct_win",
        "lucky_win",
        "unreviewed",
    ]
    signal_pnl: float
    timing_pnl: float
    slippage: float
    costs: float
    lessons: list[str] = Field(default_factory=list, max_length=3)
    confidence: float = Field(ge=0.0, le=1.0)


IntentKind = Literal["enter", "exit", "move_stop", "close_all", "pause", "resume", "kill"]


class Intent(Strict):
    """What the brain may ask the gateway to do. The gateway re-runs the risk engine on every one."""

    intent_id: str
    kind: IntentKind
    created_ms: int
    proposal_id: str | None = None
    bundle_id: str | None = None
    side: Literal["long", "short"] | None = None
    stop_price: float | None = Field(default=None, gt=0)
    target_price: float | None = Field(default=None, gt=0)
    position_id: int | None = None
    new_stop_price: float | None = Field(default=None, gt=0)
    reason: str = Field(default="", max_length=300)
    source: Literal["strategy_bot", "owner_confirmed", "system"] = "strategy_bot"


class IntentResult(Strict):
    intent_id: str
    status: Literal["executed", "rejected", "failed", "ignored"]
    risk_code: str | None = None
    detail: str = ""
    order_ticket: int | None = None
    position_id: int | None = None
    filled_price: float | None = None
    volume: float | None = None


class GatewayState(Strict):
    """Read model served on GET /state for the brain, chat and health."""

    time_ms: int
    connected: bool
    trade_allowed: bool
    session: str
    entries_allowed: bool
    bid: float | None
    ask: float | None
    spread_pips: float | None
    atr: float | None
    tick_age_s: float | None
    equity: float | None
    balance: float | None
    margin_free: float | None
    margin_level: float | None
    open_positions: int
    kill_switch: bool
    paused: bool
    owner_hash: str
    spec_ok: bool
    incident_open: bool
    day_pnl: float
    positions: list[dict] = Field(default_factory=list)

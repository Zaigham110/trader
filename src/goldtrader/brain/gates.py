"""Code gates between the Strategy bot's proposal and an intent (AGENTS §3.2, ARCHITECTURE §3.1).

The LLM proposes; these deterministic checks decide whether a proposal may even become an intent.
The gateway's risk engine then checks it again with live account data. Nothing here can raise a
limit; everything reads the owner config and the deterministic signal.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from goldtrader.config.schema import Config, StrategyConfig
from goldtrader.contracts import Intent, StrategyProposal
from goldtrader.gateway.market import LiveFeatures
from goldtrader.types import SessionState


@dataclass(frozen=True)
class GateInput:
    proposal: StrategyProposal
    proposal_id: str
    proposal_age_s: float
    bundle_id: str
    signal_side: int  # deterministic strategy signal: +1, -1, 0
    features: LiveFeatures | None
    session: SessionState
    entries_allowed: bool
    open_positions: int
    risk_budget: int  # 0–100 after code hard rules
    bid: float
    ask: float
    now_ms: int


@dataclass(frozen=True)
class GateOutcome:
    ok: bool
    reason: str
    intent: Intent | None = None


def _side_sign(side: str | None) -> int:
    return 1 if side == "long" else (-1 if side == "short" else 0)


def gate_entry(
    g: GateInput,
    cfg: Config,
    strat: StrategyConfig,
    *,
    min_confidence: float = 0.6,
    max_age_s: float = 60.0,
) -> GateOutcome:
    p = g.proposal
    r = cfg.owner.risk
    if p.action == "wait":
        return GateOutcome(False, "wait")
    if p.action == "exit":
        return GateOutcome(True, "exit", _intent(g, "exit"))
    if p.action == "move_stop":
        if p.new_stop_price is None:
            return GateOutcome(False, "move_stop_without_price")
        return GateOutcome(True, "move_stop", _intent(g, "move_stop", new_stop=p.new_stop_price))
    # --- enter -------------------------------------------------------------------------
    if g.proposal_age_s > max_age_s:
        return GateOutcome(False, "proposal_stale")
    if g.session is not SessionState.OPEN or not g.entries_allowed:
        return GateOutcome(False, f"session_{g.session.value.lower()}")
    if g.open_positions >= r.max_positions:
        return GateOutcome(False, "max_positions")
    if g.risk_budget <= 0:
        return GateOutcome(False, "risk_budget_zero")
    if p.side is None or p.stop_atr is None:
        return GateOutcome(False, "enter_missing_fields")
    if p.confidence < min_confidence:
        return GateOutcome(False, f"confidence_{p.confidence:.2f}")
    sig = _side_sign(p.side)
    if sig != g.signal_side and not strat.allow_discretionary:
        return GateOutcome(False, "disagrees_with_signal")
    if g.features is None or g.features.atr is None or g.features.atr <= 0:
        return GateOutcome(False, "no_atr")
    stop_atr = p.stop_atr
    lo, hi = max(strat.stop_atr_min, r.stop_atr_min), min(strat.stop_atr_max, r.stop_atr_max)
    if not (lo <= stop_atr <= hi):
        return GateOutcome(False, f"stop_atr_{stop_atr:.2f}_outside_[{lo:.2f},{hi:.2f}]")
    min_rr = max(strat.min_reward_risk, r.min_reward_risk)
    target_atr = p.target_atr if p.target_atr is not None else stop_atr * min_rr
    if target_atr < stop_atr * min_rr:
        return GateOutcome(False, f"reward_risk_{target_atr / stop_atr:.2f}")
    if g.features.spread_regime != "normal" or g.features.spread_pips > r.max_spread_pips:
        return GateOutcome(False, "spread")
    atr = g.features.atr
    entry = g.ask if p.side == "long" else g.bid
    stop = entry - stop_atr * atr if p.side == "long" else entry + stop_atr * atr
    target = entry + target_atr * atr if p.side == "long" else entry - target_atr * atr
    return GateOutcome(
        True,
        "enter",
        _intent(g, "enter", side=p.side, stop=round(stop, 2), target=round(target, 2)),
    )


def _intent(
    g: GateInput,
    kind: str,
    *,
    side: str | None = None,
    stop: float | None = None,
    target: float | None = None,
    new_stop: float | None = None,
) -> Intent:
    iid = hashlib.sha256(f"{g.proposal_id}:{kind}:{g.now_ms}".encode()).hexdigest()[:16]
    return Intent(
        intent_id=iid,
        kind=kind,
        created_ms=g.now_ms,
        proposal_id=g.proposal_id,
        bundle_id=g.bundle_id,
        side=side,
        stop_price=stop,
        target_price=target,
        new_stop_price=new_stop,
        reason=g.proposal.thesis[:300],
        source="strategy_bot",
    )

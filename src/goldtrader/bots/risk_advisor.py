"""Risk-advisor bot (AGENTS.md §3.3; ARCHITECTURE §3.1 step 5; R-020, R-021).

The model returns a 0–100 budget with ≤ 3 reasons; `hard_rules()` in code forces 0 on kill switch,
pause, open incident, session not OPEN / entries not allowed, abnormal spread, stale ticks, daily
loss limit, drawdown limit, margin level below the floor, spec change, broker disconnected. The
final budget is `min(advised, code)`: the model can only lower the result, never raise it. The
spec's `finalize` applies the hard rules so the cycle (and the fallback path) always reads a
code-decided number. Fallback on provider trouble: advised 100 flagged `degraded`; the hard rules
still apply.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from goldtrader.bots.common import (
    BotContext,
    ContextCell,
    bullet_list,
    features_table,
    fmt_ms,
    gateway_table,
)
from goldtrader.contracts import RiskAdvice
from goldtrader.llm.prompts import register
from goldtrader.llm.runtime import AgentSpec
from goldtrader.types import SessionState

RISK_PROMPT = register(
    "risk_advisor.system",
    1,
    "You are the Risk-advisor bot of a gold (XAUUSD) trading system. From the fast context you are "
    "given (realised vol vs regime, spread vs its hourly norm, minutes to the next high-impact "
    "event, today's P&L vs the daily limit, consecutive losses, margin level, open incidents) you "
    "return a risk budget from 0 to 100 and at most 3 short reasons (≤ 120 chars each).\n"
    "100 = normal conditions, nothing against a full-size entry; 0 = no new entries. Lower the "
    "budget for elevated vol, wide spread, an imminent event, losses near the limit, a losing "
    "streak or thin margin. Your number can only reduce what code allows; code applies its own "
    "hard rules and never lets you raise them. Output the RiskAdvice JSON only.",
)


@dataclass(frozen=True)
class RiskBudget:
    """What the cycle reads: the model's number, the code-decided budget and both reason lists."""

    advised: int
    budget: int
    reasons: list[str] = field(default_factory=list)
    hard_rules: list[str] = field(default_factory=list)
    degraded: bool = False


def consecutive_losses(trades: list[dict[str, Any]]) -> int:
    """Losing streak at the head of the (newest-first) trade list."""
    n = 0
    for t in trades:
        pnl = t.get("net_pnl")
        if pnl is None or float(pnl) >= 0:
            break
        n += 1
    return n


def hard_rules(ctx: BotContext, advised: int) -> tuple[int, list[str]]:
    """Deterministic rules (AGENTS §3.3 "Code hard_rules()"): returns `(budget, reasons)`. Any
    reason forces 0; otherwise the budget is `min(advised, 100)` (and ≥ 0)."""
    reasons: list[str] = []
    s = ctx.gateway_state
    cfg = ctx.cfg
    r = cfg.owner.risk
    f = ctx.features

    if s is None:
        reasons.append("no_gateway_state")
    else:
        if s.kill_switch:
            reasons.append("kill_switch")
        if s.paused:
            reasons.append("paused")
        if s.incident_open:
            reasons.append("incident_open")
        if not s.connected:
            reasons.append("disconnected")
        if not s.trade_allowed:
            reasons.append("trade_not_allowed")
        if not s.spec_ok:
            reasons.append("spec_changed")
        if s.session != SessionState.OPEN.value:
            reasons.append(f"session_{s.session.lower()}")
        if not s.entries_allowed:
            reasons.append("entries_not_allowed")
        stale = cfg.session.stale_tick_seconds
        if s.tick_age_s is None or s.tick_age_s > stale:
            reasons.append("stale_ticks")
        if s.margin_level is not None and s.margin_level < r.min_margin_level_pct:
            reasons.append("margin_level_low")
        ref = s.balance if s.balance else s.equity
        if ref and ref > 0:
            if s.day_pnl <= -(r.daily_loss_pct / 100.0) * ref:
                reasons.append("daily_loss_limit")
            if s.equity is not None and s.balance and s.balance > 0:
                drawdown_pct = (s.balance - s.equity) / s.balance * 100.0
                if drawdown_pct >= r.max_drawdown_pct:
                    reasons.append("drawdown_limit")
        if s.equity is not None and s.equity < r.min_equity_to_enter_usd:
            reasons.append("equity_below_minimum")
    if f is None or f.spread_regime != "normal":
        reasons.append("spread_regime_" + (f.spread_regime if f else "unknown"))
    elif f.spread_pips > r.max_spread_pips:
        reasons.append("spread_above_cap")
    if (
        ctx.minutes_to_event is not None
        and ctx.minutes_to_event <= cfg.session.news_high.before_min
    ):
        reasons.append("news_window")

    if reasons:
        return 0, reasons
    return max(0, min(int(advised), 100)), []


def build_risk_messages(ctx: BotContext) -> list[dict[str, Any]]:
    r = ctx.cfg.owner.risk
    s = ctx.gateway_state
    ref = (s.balance or s.equity or 0.0) if s else 0.0
    limit = -(r.daily_loss_pct / 100.0) * ref if ref else None
    event = (
        f"{ctx.next_event} in {ctx.minutes_to_event:.0f} min"
        if ctx.next_event and ctx.minutes_to_event is not None
        else "none scheduled"
    )
    text = (
        f"Time: {fmt_ms(ctx.now_ms)}\n"
        f"Daily loss limit: {r.daily_loss_pct:.2f}% of balance"
        f"{f' = {limit:.2f} USD' if limit is not None else ''}; "
        f"max drawdown {r.max_drawdown_pct:.1f}%; min margin level {r.min_margin_level_pct:.0f}%; "
        f"spread cap {r.max_spread_pips:.0f} pips.\n"
        f"Consecutive losses: {consecutive_losses(ctx.recent_trades)} "
        f"(pause after {r.consecutive_loss_pause}).\n"
        f"Next high-impact event: {event}\n\n"
        f"Features:\n{features_table(ctx.features)}\n\n"
        f"Account / gateway:\n{gateway_table(s)}\n\n"
        f"Recent trades:\n{bullet_list(ctx.recent_trades[:5], max_items=5)}\n\n"
        "Return the RiskAdvice JSON."
    )
    return [{"role": "user", "content": text}]


def check_advice(a: RiskAdvice) -> list[str]:
    return [f"reason {i} longer than 120 chars" for i, s in enumerate(a.reasons) if len(s) > 120]


def finalize_advice(a: RiskAdvice, ctx: BotContext) -> RiskBudget:
    budget, rules = hard_rules(ctx, a.budget)
    degraded = any(s.startswith("degraded") for s in a.reasons)
    return RiskBudget(
        advised=a.budget,
        budget=budget,
        reasons=list(a.reasons),
        hard_rules=rules,
        degraded=degraded,
    )


def degraded_advice(ctx: BotContext, reason: str) -> RiskAdvice:
    """Fallback (R-021): the model's opinion is unavailable, so it does not reduce anything; the
    code hard rules still decide through `finalize_advice`."""
    return RiskAdvice(budget=100, reasons=[f"degraded: {reason}"[:120]])


def risk_advisor_spec(cfg: Any) -> AgentSpec:
    """Risk-advisor `AgentSpec` (tier `fast`). No tools."""
    tier = cfg.llm.tiers["fast"]
    cell = ContextCell()

    def build(ctx: BotContext) -> list[dict[str, Any]]:
        cell.set(ctx)
        return build_risk_messages(ctx)

    return AgentSpec(
        name="risk_advisor",
        tier="fast",
        system_prompt=RISK_PROMPT,
        build_messages=build,
        output_model=RiskAdvice,
        tools=[],
        check=check_advice,
        finalize=finalize_advice,
        timeout_s=float(tier.timeout_s),
        max_turns=3,
        max_cost_usd=tier.max_cost_usd,
        fallback=degraded_advice,
        effort=tier.effort,
        schema_version=1,
    )

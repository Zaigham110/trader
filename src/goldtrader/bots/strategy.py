"""Strategy bot (AGENTS.md §3.2; ARCHITECTURE §3.1 step 3; R-020, R-021).

The deterministic signal is the primary input; the model may wait, veto, size down or explain, and
its output is a `StrategyProposal` that the code gates (`brain.gates.gate_entry`) and the gateway's
risk engine decide on. This module has **no tools**: nothing here can change state.

Code checks: `enter` needs `side` and `stop_atr`, `move_stop` needs `new_stop_price` (errors are fed
back for one repair, R-021). Code overrides `agrees_with_signal` (truthfulness is computed, not
trusted) and clips `stop_atr` / `target_atr` into the configured bounds. Fallback on provider
trouble: `wait`, thesis `degraded: provider unavailable`.
"""

from __future__ import annotations

from typing import Any

from goldtrader.bots.common import (
    BotContext,
    ContextCell,
    bullet_list,
    clip,
    features_table,
    fmt_ms,
    gateway_table,
    side_sign,
    signal_text,
)
from goldtrader.config.schema import StrategyConfig
from goldtrader.contracts import StrategyProposal
from goldtrader.llm.prompts import register
from goldtrader.llm.runtime import AgentSpec

NEWS_LOOKBACK_MS = 6 * 3600 * 1000
RECENT_TRADES = 10
LATEST_LESSONS = 5

STRATEGY_PROMPT = register(
    "strategy.system",
    1,
    "You are the Strategy bot of a gold (XAUUSD) trading system on Exness MT5. You receive "
    "code-computed features, the active strategy's deterministic signal, structured news events, "
    "the open position, recent trades, lessons, a calibration line and the risk budget.\n\n"
    "Decide one action: wait | enter | exit | move_stop.\n"
    "- The deterministic signal is primary. You may wait (veto), size down or explain; you may not "
    "enter against the signal unless the strategy config allows discretionary entries. Code "
    "verifies `agrees_with_signal` itself.\n"
    "- enter requires side, stop_atr and target_atr in ATR multiples within the configured bounds, "
    "and target_atr ≥ stop_atr × min reward/risk. exit and move_stop apply only to the open "
    "position; move_stop needs new_stop_price and may only move toward profit.\n"
    "- confidence 0–1 must be calibrated against the calibration line you are given; a thesis "
    "(≥ 20 chars) states the evidence, an invalidation (≥ 10 chars) states what would prove it "
    "wrong; horizon_minutes 15–480.\n"
    "- With risk budget 0 you may still propose exit or move_stop, never enter.\n"
    "You never place orders or change settings: every output is a proposal that code gates, "
    "then the risk engine checks again. Output the StrategyProposal JSON only.",
)


def atr_bounds(cfg: Any, strat: StrategyConfig) -> tuple[float, float, float]:
    """(stop_atr_min, stop_atr_max, min_reward_risk): the stricter of strategy and owner risk."""
    r = cfg.owner.risk
    lo = max(strat.stop_atr_min, r.stop_atr_min)
    hi = min(strat.stop_atr_max, r.stop_atr_max)
    if lo > hi:  # misconfiguration: fall back to the owner's bounds
        lo, hi = r.stop_atr_min, r.stop_atr_max
    return lo, hi, max(strat.min_reward_risk, r.min_reward_risk)


def recent_news(events: list[dict[str, Any]], now_ms: int) -> list[dict[str, Any]]:
    """News events from the last 6 h (AGENTS §3.2); events without a timestamp are kept."""
    out = []
    for e in events:
        ts = e.get("created_ms", e.get("time_ms"))
        if ts is None or now_ms - int(ts) <= NEWS_LOOKBACK_MS:
            out.append(e)
    return out


def build_strategy_messages(ctx: BotContext, strat: StrategyConfig) -> list[dict[str, Any]]:
    lo, hi, rr = atr_bounds(ctx.cfg, strat)
    budget = "unknown" if ctx.risk_budget is None else str(ctx.risk_budget)
    pos = (
        bullet_list([ctx.open_position], empty="none", max_items=1) if ctx.open_position else "none"
    )
    event = (
        f"{ctx.next_event} in {ctx.minutes_to_event:.0f} min"
        if ctx.next_event and ctx.minutes_to_event is not None
        else "none scheduled within the horizon"
    )
    text = (
        f"Time: {fmt_ms(ctx.now_ms)}\n"
        f"Strategy: {strat.id} ({strat.kind}); discretionary entries "
        f"{'allowed' if strat.allow_discretionary else 'NOT allowed'}; stop_atr bounds "
        f"[{lo:.2f}, {hi:.2f}]; min reward/risk {rr:.2f}.\n"
        f"Deterministic signal: {signal_text(ctx.signal_side)}\n"
        f"Risk budget (code-decided, 0–100): {budget}\n"
        f"Next high-impact event: {event}\n\n"
        f"Features:\n{features_table(ctx.features)}\n\n"
        f"Gateway state:\n{gateway_table(ctx.gateway_state)}\n\n"
        f"Open position:\n{pos}\n\n"
        f"News events (last 6 h, structured by code):\n"
        f"{bullet_list(recent_news(ctx.news_events, ctx.now_ms), max_items=20)}\n\n"
        f"Last {RECENT_TRADES} trades:\n"
        f"{bullet_list(ctx.recent_trades[:RECENT_TRADES], max_items=RECENT_TRADES)}\n\n"
        f"Latest {LATEST_LESSONS} lessons:\n"
        f"{bullet_list(ctx.lessons[:LATEST_LESSONS], max_items=LATEST_LESSONS)}\n\n"
        f"Calibration: {ctx.calibration_line or 'no calibration data yet'}\n\n"
        "Return the StrategyProposal JSON."
    )
    return [{"role": "user", "content": text}]


def check_proposal(p: StrategyProposal) -> list[str]:
    """Structural problems the model must fix (fed back once, R-021)."""
    problems: list[str] = []
    if p.action == "enter":
        if p.side is None:
            problems.append("action enter requires side (long | short)")
        if p.stop_atr is None:
            problems.append("action enter requires stop_atr")
    if p.action == "move_stop" and p.new_stop_price is None:
        problems.append("action move_stop requires new_stop_price")
    return problems


def normalise_proposal(
    p: StrategyProposal, ctx: BotContext, strat: StrategyConfig
) -> StrategyProposal:
    """Code overrides (AGENTS §3.2): `agrees_with_signal` is computed from side vs the deterministic
    signal; `stop_atr` / `target_atr` are clipped into the configured bounds."""
    lo, hi, rr = atr_bounds(ctx.cfg, strat)
    update: dict[str, Any] = {}
    if p.side is not None:
        update["agrees_with_signal"] = side_sign(p.side) == ctx.signal_side
    elif p.action == "wait":
        update["agrees_with_signal"] = ctx.signal_side == 0
    if p.stop_atr is not None:
        stop = clip(p.stop_atr, lo, hi)
        update["stop_atr"] = stop
        if p.target_atr is not None:
            update["target_atr"] = clip(p.target_atr, stop * rr, 10.0)
    return p.model_copy(update=update)


def degraded_proposal(ctx: BotContext, reason: str) -> StrategyProposal:
    """Rule-based default when the provider fails (R-021): wait, flagged degraded."""
    return StrategyProposal(
        action="wait",
        confidence=0.0,
        thesis=f"degraded: provider unavailable ({reason})"[:600],
        invalidation="n/a: degraded run, no position change proposed",
        horizon_minutes=15,
        agrees_with_signal=ctx.signal_side == 0,
    )


def strategy_spec(cfg: Any, strat: StrategyConfig) -> AgentSpec:
    """Strategy `AgentSpec` (tier `standard`, effort `medium`). No tools."""
    tier = cfg.llm.tiers["standard"]
    cell = ContextCell()

    def build(ctx: BotContext) -> list[dict[str, Any]]:
        cell.set(ctx)
        return build_strategy_messages(ctx, strat)

    def finalize(p: StrategyProposal, ctx: BotContext) -> StrategyProposal:
        return normalise_proposal(p, ctx, strat)

    return AgentSpec(
        name="strategy",
        tier="standard",
        system_prompt=STRATEGY_PROMPT,
        build_messages=build,
        output_model=StrategyProposal,
        tools=[],
        check=check_proposal,
        finalize=finalize,
        timeout_s=float(tier.timeout_s),
        max_turns=4,
        max_cost_usd=tier.max_cost_usd,
        fallback=degraded_proposal,
        effort="medium",
        schema_version=1,
    )

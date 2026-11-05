"""Review bot (AGENTS.md §3.4; ARCHITECTURE §3.3; LEARNING_SYSTEM §6; R-020, R-021).

Post-mortem per closed trade: code computes the attribution (`attribution()`), the model classifies
the outcome, copies the numbers and writes ≤ 3 lessons. `check()` verifies the copy (|Δ| ≤ 1e-6,
else the error is fed back); `finalize` deduplicates lessons against existing ones by normalised
text. Noise guard: lessons feed the Strategy prompt (latest 5) and never change parameters.

Daily brief: narrative over code-computed numbers only (`ctx.brief_numbers`); the model adds no
numbers of its own. `Brief.sections` is a list of `(key, text)` pairs because the flat schema
subset cannot express a free-form `dict[str, str]` (LOCAL_LLM §2); `sections_dict()` gives the map.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from goldtrader.bots.common import BotContext, ContextCell, bullet_list, defang, fmt_ms
from goldtrader.contracts import PostMortem
from goldtrader.llm.prompts import register
from goldtrader.llm.runtime import AgentSpec

ATTRIBUTION_TOL = 1e-6
ATTRIBUTION_KEYS = ("signal_pnl", "timing_pnl", "slippage", "costs")
CONTRACT_SIZE_OZ = 100.0
"""XAUUSD: one lot is 100 troy ounces (symbol spec `trade_contract_size`; overridable)."""

REVIEW_PROMPT = register(
    "review.postmortem.system",
    1,
    "You are the Review bot of a gold (XAUUSD) trading system. You write the post-mortem of one "
    "closed trade from its journal: proposal, gates, risk verdict, fills, costs, the 1-minute path "
    "and the news events cited. Code has already computed the attribution numbers (signal_pnl, "
    "timing_pnl, slippage, costs): copy them EXACTLY as given; never compute or round them.\n"
    "Classify outcome_class: signal_wrong | timing | stopped_by_noise | news_shock | execution | "
    "correct_loss | correct_win | lucky_win. Write at most 3 lessons (≤ 200 chars each) that are "
    "specific, testable and not already in the prior lessons list; a lesson is an observation, "
    "never a parameter change. confidence 0–1. Output the PostMortem JSON only.",
)

BRIEF_PROMPT = register(
    "review.brief.system",
    1,
    "You are the Review bot writing the owner's daily brief for a gold (XAUUSD) trading system. "
    "Use ONLY the code-computed numbers you are given; do not invent, derive or round numbers. "
    "Write at most 6 sections (key ≤ 40 chars, text ≤ 500 chars) in plain language: what happened, "
    "P&L vs limits, what the AI got right or wrong, health, costs. Add at most 3 questions for the "
    "owner when a decision is needed. Output the Brief JSON only.",
)


# --- attribution (code) -----------------------------------------------------------------------
def _mid_at(bars: list[Any], at_ms: int) -> float | None:
    """Mid close of the last 1 m bar at or before `at_ms`; bars are `Bar`s or dicts."""
    best: float | None = None
    for b in bars:
        t = b["time"] if isinstance(b, dict) else b.time
        t_ms = int(t) * 1000 if int(t) < 10**12 else int(t)
        if t_ms <= at_ms:
            if isinstance(b, dict):
                best = (float(b["bid_close"]) + float(b["ask_close"])) / 2.0
            else:
                best = b.mid_close
        else:
            break
    return best


def attribution(
    trade: dict[str, Any], bars_1m: list[Any], contract_size: float = CONTRACT_SIZE_OZ
) -> dict[str, float]:
    """Code-computed attribution (LEARNING_SYSTEM §6) in account currency.

    * `signal_pnl`: mid-to-mid P&L from the signal time (`signal_ms`, else `entry_ms`) to the exit
      — what the idea was worth before execution.
    * `timing_pnl`: mid at signal vs the actual entry fill (`entry_price`); adverse fills are negative.
    * `slippage`: the trade row's `slippage_cost`; `costs`: spread + commission + swap (positive =
      cost).
    Values are rounded to 6 dp so the model's copy can be compared with a tolerance of 1e-6.
    """
    sign = 1.0 if str(trade.get("side", "long")).lower() == "long" else -1.0
    vol = float(trade.get("volume", 0.0))
    mult = sign * vol * contract_size
    signal_ms = int(trade.get("signal_ms") or trade.get("entry_ms") or 0)
    exit_ms = int(trade.get("exit_ms") or 0)
    entry_price = float(trade.get("entry_price", 0.0))
    exit_price = float(trade.get("exit_price", 0.0))
    mid_signal = _mid_at(bars_1m, signal_ms) if bars_1m else None
    if mid_signal is None:
        mid_signal = entry_price
    mid_exit = _mid_at(bars_1m, exit_ms) if bars_1m else None
    if mid_exit is None:
        mid_exit = exit_price
    signal_pnl = (mid_exit - mid_signal) * mult
    timing_pnl = (mid_signal - entry_price) * mult
    slippage = float(trade.get("slippage_cost", 0.0))
    costs = (
        float(trade.get("spread_cost", 0.0))
        + float(trade.get("commission", 0.0))
        + float(trade.get("swap", 0.0))
    )
    return {
        "signal_pnl": round(signal_pnl, 6),
        "timing_pnl": round(timing_pnl, 6),
        "slippage": round(slippage, 6),
        "costs": round(costs, 6),
    }


# --- lessons ------------------------------------------------------------------------------------
_PUNCT = re.compile(r"[^a-z0-9 ]+")
_WS = re.compile(r"\s+")


def normalise_lesson(text: str) -> str:
    """Dedup key: lower-case, punctuation stripped, whitespace collapsed."""
    return _WS.sub(" ", _PUNCT.sub(" ", defang(text, 400).lower())).strip()


def dedupe_lessons(new: list[str], existing: list[str]) -> list[str]:
    seen = {normalise_lesson(e) for e in existing}
    out: list[str] = []
    for lesson in new:
        key = normalise_lesson(lesson)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(defang(lesson, 200))
    return out[:3]


# --- post-mortem spec ---------------------------------------------------------------------------
def attribution_problems(pm: PostMortem, expected: dict[str, float] | None) -> list[str]:
    if not expected:
        return []
    problems = []
    for k in ATTRIBUTION_KEYS:
        want = float(expected.get(k, 0.0))
        got = float(getattr(pm, k))
        if abs(got - want) > ATTRIBUTION_TOL:
            problems.append(f"{k} must be copied exactly from the attribution: expected {want!r}")
    return problems


def build_review_messages(ctx: BotContext) -> list[dict[str, Any]]:
    trade = ctx.trade or {}
    attr = ctx.attribution or {}
    text = (
        f"Time: {fmt_ms(ctx.now_ms)}\n\n"
        f"Trade journal:\n{bullet_list([trade], max_items=1)}\n\n"
        f"Attribution (code-computed; copy exactly):\n"
        + "\n".join(f"- {k} = {attr.get(k, 0.0)!r}" for k in ATTRIBUTION_KEYS)
        + "\n\n"
        f"Cited news events:\n{bullet_list(ctx.news_events, max_items=10)}\n\n"
        f"Prior lessons (do not repeat):\n{bullet_list(ctx.lessons, max_items=20)}\n\n"
        "Return the PostMortem JSON."
    )
    return [{"role": "user", "content": text}]


def review_spec(cfg: Any) -> AgentSpec:
    """Post-mortem `AgentSpec` (tier `standard`). No tools."""
    tier = cfg.llm.tiers["standard"]
    cell = ContextCell()

    def build(ctx: BotContext) -> list[dict[str, Any]]:
        cell.set(ctx)
        return build_review_messages(ctx)

    def check(pm: PostMortem) -> list[str]:
        ctx = cell.ctx
        return attribution_problems(pm, ctx.attribution if ctx else None)

    def finalize(pm: PostMortem, ctx: BotContext) -> PostMortem:
        return pm.model_copy(update={"lessons": dedupe_lessons(pm.lessons, ctx.lessons)})

    def fallback(ctx: BotContext, reason: str) -> PostMortem:
        attr = ctx.attribution or {}
        return PostMortem(
            outcome_class="unreviewed",
            signal_pnl=float(attr.get("signal_pnl", 0.0)),
            timing_pnl=float(attr.get("timing_pnl", 0.0)),
            slippage=float(attr.get("slippage", 0.0)),
            costs=float(attr.get("costs", 0.0)),
            lessons=[],
            confidence=0.0,
        )

    return AgentSpec(
        name="review",
        tier="standard",
        system_prompt=REVIEW_PROMPT,
        build_messages=build,
        output_model=PostMortem,
        tools=[],
        check=check,
        finalize=finalize,
        timeout_s=float(tier.timeout_s),
        max_turns=4,
        max_cost_usd=tier.max_cost_usd,
        fallback=fallback,
        effort="medium",
        schema_version=1,
    )


# --- daily brief --------------------------------------------------------------------------------
class BriefSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=40)
    text: str = Field(min_length=1, max_length=500)


class Brief(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sections: list[BriefSection] = Field(default_factory=list, max_length=6)
    questions: list[str] = Field(default_factory=list, max_length=3)

    def sections_dict(self) -> dict[str, str]:
        return {s.key: s.text for s in self.sections}


def check_brief(b: Brief) -> list[str]:
    keys = [s.key.strip().lower() for s in b.sections]
    problems = []
    if len(set(keys)) != len(keys):
        problems.append("section keys must be unique")
    problems += [
        f"question {i} longer than 300 chars" for i, q in enumerate(b.questions) if len(q) > 300
    ]
    return problems


def build_brief_messages(ctx: BotContext) -> list[dict[str, Any]]:
    text = (
        f"Date: {fmt_ms(ctx.now_ms)}\n\n"
        f"Code-computed numbers (the only numbers you may use):\n"
        f"{bullet_list([ctx.brief_numbers], max_items=1)}\n\n"
        f"Latest lessons:\n{bullet_list(ctx.lessons[:5], max_items=5)}\n\n"
        "Return the Brief JSON."
    )
    return [{"role": "user", "content": text}]


def daily_brief_spec(cfg: Any) -> AgentSpec:
    """Daily brief `AgentSpec` (tier `standard`). No tools; numbers come from code only."""
    tier = cfg.llm.tiers["standard"]
    cell = ContextCell()

    def build(ctx: BotContext) -> list[dict[str, Any]]:
        cell.set(ctx)
        return build_brief_messages(ctx)

    def finalize(b: Brief, ctx: BotContext) -> Brief:
        return Brief(
            sections=[
                BriefSection(key=defang(s.key, 40) or "section", text=defang(s.text, 500) or "-")
                for s in b.sections
            ],
            questions=[defang(q, 300) for q in b.questions],
        )

    def fallback(ctx: BotContext, reason: str) -> Brief:
        return Brief(
            sections=[BriefSection(key="status", text=f"degraded: {reason}"[:500])],
            questions=[],
        )

    return AgentSpec(
        name="daily_brief",
        tier="standard",
        system_prompt=BRIEF_PROMPT,
        build_messages=build,
        output_model=Brief,
        tools=[],
        check=check_brief,
        finalize=finalize,
        timeout_s=float(tier.timeout_s),
        max_turns=3,
        max_cost_usd=tier.max_cost_usd,
        fallback=fallback,
        effort="medium",
        schema_version=1,
    )

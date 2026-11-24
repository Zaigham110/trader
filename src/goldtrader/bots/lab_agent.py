"""The research agent of the lab (D-085): proposes rules to test and says what it learned.

It has no tools. It is shown numbers that code measured (a profile of how the market moves, and
the notebook: every rule tried so far with how far it got through the gates and why it stopped)
and returns data: rules written in the lab's rule language, each with the reason it should make
money, plus one line of what it concluded from each newly tested rule. Code validates every rule,
runs every test and applies every threshold. The agent never sees dated prices, cannot run
anything, and cannot change a gate.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from goldtrader.llm.prompts import register
from goldtrader.llm.runtime import AgentSpec

MAX_IDEAS = 8
MAX_LESSONS = 30

PROMPT = register(
    "lab_research.system",
    1,
    "You are the research agent of a gold (XAUUSD) trading system. Your one task is to find a "
    "trading rule that has a real edge: one that keeps making money on price history it was not "
    "fitted to. You do not trade and you change nothing. You propose rules; code tests them.\n\n"
    "HOW A RULE IS JUDGED (fixed, you cannot change it). Code runs each rule on stored 1-minute "
    "bid/ask history with real spreads, one position at a time, positions closed before the "
    "daily market break. R is the amount risked on a trade (entry to stop). A rule must pass, in "
    "order: (1) trades: at least 100 trades in all and 25 in each of three separate periods; "
    "(2) periods: a positive average R and a positive net result in EACH of the three periods; "
    "(3) robust: still positive with the spread 1.5 times wider and with the stop and the target "
    "each moved 20% either way; (4) luck: better than random entries with the same exits, and "
    "still credible after counting every rule ever tried, so each extra rule you propose raises "
    "the bar for all of them; (5) blind: positive on data nobody has tested on. Almost every "
    "rule fails gate 2: positive in one or two periods and negative in another.\n\n"
    "THE RULE LANGUAGE. A rule has one entry, up to three filters and one exit. The allowed "
    "blocks and ranges are given as JSON below the evidence. Entries are decided on completed "
    "bars and filled on the next 1-minute bar. Stops and targets are multiples of the 1-minute "
    "ATR; the stop may not exceed the owner's limit (given). Filters only remove signals.\n\n"
    "HOW TO WORK.\n"
    "- First write lessons: for every notebook entry marked needs_lesson, one sentence on what "
    "its result says about the market or about that kind of rule, citing its numbers. A lesson "
    "is about why, never just 'it failed'.\n"
    "- Then propose up to 8 new rules. Each needs a rationale that names a mechanism: who is on "
    "the other side of the trade and why they keep losing, or what behaviour of the market "
    "(given in the profile) the rule exploits. No mechanism, no proposal.\n"
    "- State an expectation you could be wrong about: which periods or conditions should work "
    "and what result would disprove the idea.\n"
    "- Learn from the notebook. Do not repeat a rule or make a cosmetic variation of a failed "
    "one. When something passed gate 1 with two good periods, ask what was different about the "
    "bad period and test that, changing one thing at a time. Prefer a different kind of idea "
    "over a tenth variant of the same one.\n"
    "- A filter that helped once is usually noise. Use a filter only when the mechanism calls "
    "for it. Fewer filters are better.\n"
    "- Costs matter: the spread is roughly a quarter of an ATR, so tiny stops lose to costs.\n"
    "- Proposing fewer than 8 rules, or none, is fine and often right. Finding nothing is a "
    "valid result; inventing a pass is not possible, so do not try to game the gates.\n"
    "Output the LabOut JSON only.",
)


class EntryBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["trend_flip", "trend_bars", "breakout", "pullback", "fade", "session_range"]
    minutes: int | None = None
    ema_fast: int | None = None
    ema_slow: int | None = None
    on_cross: int | None = None
    lookback: int | None = None
    trend_minutes: int | None = None
    z_entry: float | None = None
    range_start_hour: int | None = None
    range_end_hour: int | None = None
    trade_start_hour: int | None = None
    trade_end_hour: int | None = None


class FilterBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["htf_trend", "hours", "vol", "side", "range_pos", "weekday"]
    minutes: int | None = None
    fast: int | None = None
    slow: int | None = None
    mode: Literal["with", "against"] | None = None
    from_hour: int | None = None
    to_hour: int | None = None
    min_pctile: float | None = None
    max_pctile: float | None = None
    only: Literal["long", "short"] | None = None
    lookback_minutes: int | None = None
    max: float | None = None
    days: list[int] | None = None


class ExitBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stop_atr: float = Field(gt=0)
    target_atr: float = Field(gt=0)
    trail_atr: float | None = None
    trail_after_r: float | None = None
    breakeven_at_r: float | None = None
    use_target: bool | None = None
    on_opposite: Literal["hold", "close", "reverse"] | None = None


class LabIdea(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry: EntryBlock
    filters: list[FilterBlock] = Field(default_factory=list, max_length=3)
    exit: ExitBlock
    rationale: str = Field(min_length=30, max_length=600)
    expectation: str = Field(min_length=10, max_length=300)


class LabLesson(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    lesson: str = Field(min_length=15, max_length=300)


class LabOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lessons: list[LabLesson] = Field(default_factory=list, max_length=MAX_LESSONS)
    ideas: list[LabIdea] = Field(default_factory=list, max_length=MAX_IDEAS)


def to_rule(idea: LabIdea) -> dict[str, Any]:
    """The idea as a plain rule document for `research.rules.validate` (unset fields dropped;
    the model's `from_hour` / `to_hour` are the language's `from` / `to`)."""
    entry = {k: v for k, v in idea.entry.model_dump().items() if v is not None}
    filters = []
    for f in idea.filters:
        d = {k: v for k, v in f.model_dump().items() if v is not None}
        if "from_hour" in d:
            d["from"] = d.pop("from_hour")
        if "to_hour" in d:
            d["to"] = d.pop("to_hour")
        filters.append(d)
    ex = {k: v for k, v in idea.exit.model_dump().items() if v is not None}
    if ex.get("use_target") is True:
        ex.pop("use_target")
    return {"entry": entry, "filters": filters, "exit": ex}


def messages(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    def j(v: Any) -> str:
        return json.dumps(v, indent=1, default=str)

    text = (
        f"How the market has moved (code-measured, no dates):\n{j(ctx['profile'])}\n\n"
        f"The funnel so far:\n{j(ctx['funnel'])}\n\n"
        f"The notebook, newest first (results in R; periods A, B, C are three separate stretches "
        f"of history):\n{j(ctx['notebook'])}\n\n"
        f"The rule language (blocks and [low, high] ranges). In a filter of type hours use "
        f"from_hour and to_hour:\n{j(ctx['grammar'])}\n\n"
        f"Owner's stop limit: stop_atr at most {ctx['stop_atr_max']}.\n\n"
        f"Write the lessons, then propose at most {ctx.get('max_ideas', MAX_IDEAS)} new rules, as "
        "LabOut JSON."
    )
    return [{"role": "user", "content": text}]


def spec(cfg: Any, tier: str = "standard") -> AgentSpec:
    t = cfg.llm.tiers[tier]
    return AgentSpec(
        name="lab_research",
        tier=tier,
        system_prompt=PROMPT,
        build_messages=messages,
        output_model=LabOut,
        tools=[],
        timeout_s=float(max(t.timeout_s, 180)),
        max_turns=3,
        max_cost_usd=max(t.max_cost_usd, 0.60),
        effort="high",
        max_tokens=8192,
        schema_version=1,
    )


def make_fn(cfg: Any, provider: Any, store: Any, clock_ms: Any, *, daily_usd: float, tier: str):
    """`propose(ctx) -> (ideas, lessons, cost_usd)`: one model run under the lab's own budget.
    `store` is the lab's own ledger, so this spending never counts against the trading agents.
    A failed or refused run returns nothing and the session carries on with code-made rules."""
    from goldtrader.llm.breaker import CircuitBreaker
    from goldtrader.llm.budget import Budget
    from goldtrader.llm.runtime import run_agent

    sp = spec(cfg, tier)
    store.record_prompt(sp.system_prompt)
    budget = Budget(daily_usd, daily_usd * 31)

    def propose(ctx: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], float]:
        before = store.spent_since(0)
        res = run_agent(
            sp,
            ctx,
            provider,
            store,
            now_ms=clock_ms(),
            breaker=CircuitBreaker(sp.name, store=store),
            budget=budget,
        )
        cost = store.spent_since(0) - before
        if res.status != "ok" or res.degraded or res.output is None:
            return [], [], cost
        out: LabOut = res.output
        ideas = [
            {"rule": to_rule(i), "rationale": i.rationale, "expectation": i.expectation}
            for i in out.ideas
        ]
        return ideas, [x.model_dump() for x in out.lessons], cost

    return propose

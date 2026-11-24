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
    2,
    "You are the research agent of a gold (XAUUSD) trading system. Your one task is to find a "
    "trading rule that has a real edge: one that keeps making money on price history it was not "
    "fitted to. You do not trade and you change nothing. You propose rules; code tests them.\n\n"
    "HOW A RULE IS JUDGED (fixed, you cannot change it). Code runs each rule on stored 1-minute "
    "bid/ask history with real spreads, one position at a time, positions closed before the "
    "daily market break. R is the amount risked on a trade (entry to stop). A rule must pass, in "
    "order: (1) trades: at least 100 trades in all and 25 in each of three separate periods; "
    "(2) periods: a positive average R and a positive net result in EACH of the three periods; "
    "(3) robust: still positive with the spread 1.5 times wider and with the stop and the target "
    "each moved 20% either way; (4) luck, four checks: better than random entries in the same "
    "hours with the same exits in all 20 runs; the entry alone (no stop or target) moves "
    "further in its direction over 15, 60 or 120 minutes than random bars from the same hours "
    "and volatility; the average per trade stays above zero when whole trading days are "
    "resampled, because trades of one day are not independent; and the result is still "
    "credible after counting every rule ever tried, so each extra rule you propose raises the "
    "bar for all of them; (5) blind: positive on data nobody has tested on. Almost every "
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
    "- Read entry_only in the notebook: mean_atr is how far the price moved in the signal's "
    "direction after the spread, random_mean_atr is the same for matched random bars, p is "
    "how often random did as well. An entry with p near 0.5 predicts nothing, whatever its "
    "exits earned; look for entries whose p is small and build on those.\n"
    "- Costs matter: the spread is roughly an eighth of an ATR, so tiny stops lose to costs.\n"
    "- Proposing fewer than 8 rules, or none, is fine and often right. Finding nothing is a "
    "valid result; inventing a pass is not possible, so do not try to game the gates.\n"
    "Output the LabOut JSON only.",
)


class Setting(BaseModel):
    """One numeric setting of a block, by its name in the rule language."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=24)
    value: float


class EntryBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["trend_flip", "trend_bars", "breakout", "pullback", "fade", "session_range"]
    settings: list[Setting] = Field(max_length=6)


class FilterBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["htf_trend", "hours", "vol", "side", "range_pos", "weekday"]
    settings: list[Setting] = Field(max_length=4)
    # htf_trend: "with" or "against"; side: "long" or "short"; every other filter: ""
    choice: Literal["", "with", "against", "long", "short"]
    days: list[int] = Field(max_length=6)  # weekday only, else empty


class ExitBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stop_atr: float = Field(gt=0)
    target_atr: float = Field(gt=0)
    # optional extras by name: trail_atr, trail_after_r, breakeven_at_r
    settings: list[Setting] = Field(max_length=3)
    use_target: bool
    on_opposite: Literal["hold", "close", "reverse"]


class LabIdea(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry: EntryBlock
    filters: list[FilterBlock] = Field(max_length=3)
    exit: ExitBlock
    rationale: str = Field(min_length=30, max_length=600)
    expectation: str = Field(min_length=10, max_length=300)


class LabLesson(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    lesson: str = Field(min_length=15, max_length=300)


class LabOut(BaseModel):
    """Every field is required (an empty list where there is nothing): the provider limits how
    many optional fields an output format may have."""

    model_config = ConfigDict(extra="forbid")

    lessons: list[LabLesson] = Field(max_length=MAX_LESSONS)
    ideas: list[LabIdea] = Field(max_length=MAX_IDEAS)


def to_rule(idea: LabIdea) -> dict[str, Any]:
    """The idea as a plain rule document for `research.rules.validate`. A setting the language
    does not know is passed through, so validation names it in its refusal."""
    entry: dict[str, Any] = {"type": idea.entry.type}
    entry.update({x.name: x.value for x in idea.entry.settings})
    filters = []
    for f in idea.filters:
        d: dict[str, Any] = {"type": f.type}
        d.update({x.name: x.value for x in f.settings})
        if f.type == "htf_trend" and f.choice:
            d["mode"] = f.choice
        elif f.type == "side":
            d["only"] = f.choice
        elif f.type == "weekday":
            d["days"] = list(f.days)
        filters.append(d)
    ex: dict[str, Any] = {"stop_atr": idea.exit.stop_atr, "target_atr": idea.exit.target_atr}
    ex.update({x.name: x.value for x in idea.exit.settings})
    if not idea.exit.use_target:
        ex["use_target"] = False
    if idea.exit.on_opposite != "hold":
        ex["on_opposite"] = idea.exit.on_opposite
    return {"entry": entry, "filters": filters, "exit": ex}


def messages(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    def j(v: Any) -> str:
        return json.dumps(v, indent=1, default=str)

    text = (
        f"How the market has moved (code-measured, no dates):\n{j(ctx['profile'])}\n\n"
        f"The funnel so far:\n{j(ctx['funnel'])}\n\n"
        f"The notebook, newest first (results in R; periods A, B, C are three separate stretches "
        f"of history):\n{j(ctx['notebook'])}\n\n"
        f"The rule language (blocks and [low, high] ranges):\n{j(ctx['grammar'])}\n\n"
        "How to write a block: numeric settings go in its `settings` list as {name, value} "
        "using the names above (for example {name: minutes, value: 15}); leave a setting out "
        "to take its default. In a filter, `choice` is with/against for htf_trend, long/short "
        "for side and an empty string otherwise; `days` is used by weekday only. In the exit, "
        "trail_atr, trail_after_r and breakeven_at_r go in `settings`; use_target is true "
        "unless only a trailing stop should close the trade; on_opposite is normally hold.\n\n"
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

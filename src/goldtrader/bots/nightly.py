"""The two nightly learning agents (D-064). Neither has tools; both see only numbers computed by
code (`learning.loop.evidence`) and their outputs are data that code validates:

* `nightly_review` rewrites the playbook the Strategy agent reads. Code drops any lesson that
  cites fewer cases than the smallest group it was shown.
* `strategy_ideas` proposes up to three rule variants from a fixed library. Code checks every
  parameter, tests each idea on history with a held-out period and only then paper-trades it.

Neither can change a parameter, a risk limit or the account.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from goldtrader.llm.prompts import register
from goldtrader.llm.runtime import AgentSpec

MAX_LESSONS = 8
MAX_IDEAS = 3

REVIEW_PROMPT = register(
    "nightly_review.system",
    1,
    "You are the nightly Review agent of a gold (XAUUSD) trading system. You are given numbers "
    "that code measured from real prices and the system's own records: how the live rule's "
    "signals played out as code-only trades (in R, multiples of the amount risked), how the "
    "Strategy agent handled the signals it saw, the paper-trading candidates and the Telegram "
    "channels. You are also given the current playbook.\n\n"
    "Write the playbook the Strategy agent will read before every decision tomorrow: at most 8 "
    "short lessons. Rules:\n"
    "- Every lesson must rest on a group in the numbers and state its size and result, for "
    "example 'London-session signals: 41 played out, mean -0.21 R; wait unless ...'. Put that "
    "group's size in evidence_n. Never invent a number and never use a group you were not shown.\n"
    "- Compare a win rate with break_even_win_rate before calling a group good or bad.\n"
    "- Keep a lesson from the current playbook only if tonight's numbers still support it; "
    "revise or drop the rest. Fewer, well-supported lessons are better than eight weak ones. "
    "With too little evidence, return an empty list.\n"
    "- Lessons guide when to wait, when to trust a signal and how confident to be. They never "
    "tell the agent to break a rule, change a limit or size up.\n"
    "- Small samples are noise: say 'weak evidence' for groups under 30.\n"
    "Output the PlaybookOut JSON only.",
)

IDEAS_PROMPT = register(
    "strategy_ideas.system",
    3,
    "You are the nightly Research agent of a gold (XAUUSD) trading system. You propose at most 3 "
    "strategy variants for code to test on stored price history. You do not trade and you change "
    "nothing: your ideas are tried first in a nightly search that also runs its own optimiser. "
    "Code scores every variant on four separate stretches of history, blind-tests only the "
    "single best one on later days at real and widened spreads, and paper-trades it only if it "
    "passes. fold_score in the list below is that score (mean return across the stretches minus "
    "half its spread).\n\n"
    "The rule library (decided on completed N-minute bars, executed on 1-minute bars):\n"
    "- trend_htf: EMA trend. minutes; on_cross true = fast/slow EMA cross, false = trend state.\n"
    "- channel_break: close beyond the highest or lowest close of the last `lookback` bars.\n"
    "- range_fade: fade a close more than `z_entry` standard deviations from the mean of the "
    "last `lookback` bars.\n"
    "- breakout_session: first close beyond the earlier session's range; session asia or london.\n"
    "- pullback_htf: with the trend of `trend_minutes` bars (60 or 120), wait for a close back "
    "through the 20-bar average of `minutes` bars (5, 15 or 30) and enter when price turns "
    "again; it never enters on the move itself.\n"
    "minutes must be one of 5, 15, 30, 60, 120, 240; lookback 5-100; z_entry 1.0-4.0. stop_atr "
    "and target_atr are multiples of the 1-minute ATR; the stop is capped by the owner's limit "
    "(shown), target_atr may be up to 40.\n\n"
    "You are given the measured evidence and every idea already tested with its result. Rules:\n"
    "- Never repeat a tested idea. Move away from what failed and vary what held in one "
    "direction at a time.\n"
    "- Each test on the same days makes a lucky pass more likely, so propose only ideas you can "
    "justify from the evidence. Proposing fewer than 3, or none, is fine.\n"
    "- rationale: one or two sentences on why the evidence suggests it; no dated outcomes.\n"
    "Output the IdeasOut JSON only.",
)


class Lesson(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tag: str = Field(min_length=2, max_length=24)
    text: str = Field(min_length=20, max_length=240)
    evidence_n: int = Field(ge=0)


class PlaybookOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lessons: list[Lesson] = Field(default_factory=list, max_length=MAX_LESSONS)


class Idea(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["trend_htf", "range_fade", "channel_break", "breakout_session", "pullback_htf"]
    minutes: int | None = None
    trend_minutes: int | None = None
    lookback: int | None = None
    z_entry: float | None = None
    on_cross: bool | None = None
    session: Literal["asia", "london"] | None = None
    stop_atr: float = Field(gt=0)
    target_atr: float = Field(gt=0)
    rationale: str = Field(min_length=10, max_length=300)


class IdeasOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ideas: list[Idea] = Field(default_factory=list, max_length=MAX_IDEAS)


def _json(v: Any) -> str:
    return json.dumps(v, indent=1, default=str)


def review_messages(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    text = (
        f"Measured numbers (code-computed):\n{_json(ctx['evidence'])}\n\n"
        f"Current playbook:\n{_json(ctx.get('playbook') or [])}\n\n"
        "Write tomorrow's playbook as PlaybookOut JSON."
    )
    return [{"role": "user", "content": text}]


def ideas_messages(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    text = (
        f"Measured numbers (code-computed):\n{_json(ctx['evidence'])}\n\n"
        f"Ideas already tested (newest first):\n{_json(ctx.get('tried') or [])}\n\n"
        f"Owner's stop limit: stop_atr at most {ctx.get('stop_atr_max')}.\n\n"
        f"Propose at most {MAX_IDEAS} new ideas as IdeasOut JSON."
    )
    return [{"role": "user", "content": text}]


def nightly_review_spec(cfg: Any) -> AgentSpec:
    tier = cfg.llm.tiers["standard"]
    return AgentSpec(
        name="nightly_review",
        tier="standard",
        system_prompt=REVIEW_PROMPT,
        build_messages=review_messages,
        output_model=PlaybookOut,
        tools=[],
        timeout_s=float(max(tier.timeout_s, 90)),
        max_turns=3,
        max_cost_usd=max(tier.max_cost_usd, 0.25),
        effort="medium",
        max_tokens=4096,
        schema_version=1,
    )


def strategy_ideas_spec(cfg: Any) -> AgentSpec:
    tier = cfg.llm.tiers["standard"]
    return AgentSpec(
        name="strategy_ideas",
        tier="standard",
        system_prompt=IDEAS_PROMPT,
        build_messages=ideas_messages,
        output_model=IdeasOut,
        tools=[],
        timeout_s=float(max(tier.timeout_s, 90)),
        max_turns=3,
        max_cost_usd=max(tier.max_cost_usd, 0.25),
        effort="medium",
        max_tokens=4096,
        schema_version=1,
    )


def make_fns(cfg: Any, provider: Any, store: Any, clock_ms: Any) -> tuple[Any, Any]:
    """(review_fn, ideas_fn) for `learning.loop.nightly`, each one model run under the shared
    daily budget. A run that fails returns None / [] so the loop keeps what it had."""
    from goldtrader.llm.breaker import CircuitBreaker
    from goldtrader.llm.budget import Budget
    from goldtrader.llm.runtime import run_agent

    budget = Budget(cfg.llm.daily_budget_usd, cfg.llm.monthly_budget_usd)
    specs = {"review": nightly_review_spec(cfg), "ideas": strategy_ideas_spec(cfg)}
    for s in specs.values():
        store.record_prompt(s.system_prompt)

    def run(which: str, ctx: dict[str, Any]) -> Any:
        spec = specs[which]
        res = run_agent(
            spec,
            ctx,
            provider,
            store,
            now_ms=clock_ms(),
            breaker=CircuitBreaker(spec.name, store=store),
            budget=budget,
        )
        return res.output if res.status == "ok" and not res.degraded else None

    def review_fn(evidence: dict[str, Any], playbook: list[dict[str, Any]]) -> Any:
        out = run("review", {"evidence": evidence, "playbook": playbook})
        return None if out is None else [x.model_dump() for x in out.lessons]

    def ideas_fn(evidence: dict[str, Any], tried: list[dict[str, Any]]) -> Any:
        ctx = {
            "evidence": evidence,
            "tried": tried,
            "stop_atr_max": cfg.owner.risk.stop_atr_max,
        }
        out = run("ideas", ctx)
        return [] if out is None else [x.model_dump() for x in out.ideas]

    return review_fn, ideas_fn

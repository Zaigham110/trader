"""`BotRegistry`: builds the bot specs, owns one circuit breaker per bot and the shared budget,
runs a bot and records its rows (AGENTS.md §2 "Writes", §4 permission matrix, §5; R-023, R-025).

* Permissions: every tool name on a bot must be allowed for that bot. With an empty
  `OwnerConfig.bot_permissions` the defaults from AGENTS §4 / §3 apply (only Research has tools,
  both read-only); a non-empty mapping replaces the defaults and is enforced as written. The check
  runs at construction so a mis-permissioned bot never starts (`PermissionError`).
* `run(name, ctx)` → `AgentRun`. The bot's finalized (code-normalised) output replaces the raw one
  for every reader; the registry writes `proposals` (gate_status `proposed`, proposal_id = run_id),
  `news_events` / `news_dropped`, `hypotheses`, `risk_budgets`, `postmortems` + `lessons` or
  `briefs`. Without a provider, every run is the spec's fallback flagged `degraded`, recorded in
  `agent_runs`, and the breaker is not touched.
* Nothing in this module imports MT5, the execution engine or the gateway state builder
  (`tests/test_bots_isolation.py`).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable
from typing import Any

from goldtrader.bots.common import BotContext, context_hash
from goldtrader.bots.research import (
    HypothesesOut,
    SearchMemory,
    dropped_id,
    research_spec,
    weekly_research_spec,
)
from goldtrader.bots.review import Brief, daily_brief_spec, normalise_lesson, review_spec
from goldtrader.bots.risk_advisor import RiskBudget, risk_advisor_spec
from goldtrader.bots.strategy import strategy_spec
from goldtrader.config.schema import StrategyConfig
from goldtrader.contracts import NewsExtraction, PostMortem, StrategyProposal
from goldtrader.llm.breaker import CircuitBreaker
from goldtrader.llm.budget import Budget
from goldtrader.llm.provider import ModelProvider
from goldtrader.llm.runtime import AgentRun, AgentSpec, run_agent
from goldtrader.llm.store import LLMStore

DEFAULT_PERMISSIONS: dict[str, tuple[str, ...]] = {
    # AGENTS §3.1: Research's tools are read-only; §3.2–3.4: the others have none.
    "research": ("search_memory", "get_features"),
    "weekly_research": ("search_memory", "get_features"),
    "strategy": (),
    "risk_advisor": (),
    "review": (),
    "daily_brief": (),
}

BOT_NAMES = tuple(DEFAULT_PERMISSIONS)


def _stable(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def active_strategy(cfg: Any) -> StrategyConfig:
    """The first enabled strategy, else the first configured, else a disabled `flat` placeholder."""
    for s in cfg.strategies:
        if s.enabled:
            return s
    if cfg.strategies:
        return cfg.strategies[0]
    return StrategyConfig(id="none", kind="flat", enabled=False)


def check_permissions(specs: dict[str, AgentSpec], bot_permissions: dict[str, Any]) -> None:
    """Raise `PermissionError` when a bot carries a tool it is not allowed (AGENTS §1 "least
    privilege", §4). Empty mapping → defaults; otherwise the mapping is the whole truth."""
    allowed_map: dict[str, tuple[str, ...]] = (
        {k: tuple(v) for k, v in bot_permissions.items()}
        if bot_permissions
        else DEFAULT_PERMISSIONS
    )
    violations: list[str] = []
    for name, spec in specs.items():
        allowed = set(allowed_map.get(name, ()))
        for tool in spec.tools:
            if tool.spec.name not in allowed:
                violations.append(f"{name}:{tool.spec.name}")
    if violations:
        raise PermissionError(
            "bot tools not permitted by owner.bot_permissions: " + ", ".join(sorted(violations))
        )


class BotRegistry:
    def __init__(
        self,
        cfg: Any,
        provider: ModelProvider | None,
        store: LLMStore,
        repo_conn: sqlite3.Connection,
        *,
        strat: StrategyConfig | None = None,
        search_memory: SearchMemory | None = None,
        clock_ms: Callable[[], int] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.provider = provider
        self.store = store
        self.c = repo_conn
        self.strat = strat if strat is not None else active_strategy(cfg)
        self.clock_ms = clock_ms
        self.sleep = sleep
        self.specs: dict[str, AgentSpec] = {
            "research": research_spec(cfg, search_memory=search_memory),
            "weekly_research": weekly_research_spec(cfg, search_memory=search_memory),
            "strategy": strategy_spec(cfg, self.strat),
            "risk_advisor": risk_advisor_spec(cfg),
            "review": review_spec(cfg),
            "daily_brief": daily_brief_spec(cfg),
        }
        check_permissions(self.specs, dict(cfg.owner.bot_permissions))
        self.breakers: dict[str, CircuitBreaker] = {
            name: CircuitBreaker(name, store=store) for name in self.specs
        }
        self.budget = Budget(cfg.llm.daily_budget_usd, cfg.llm.monthly_budget_usd)
        for spec in self.specs.values():
            store.record_prompt(spec.system_prompt)

    # --- queries --------------------------------------------------------------------------------
    def spec(self, name: str) -> AgentSpec:
        return self.specs[name]

    def breaker_open(self, name: str, now_ms: int) -> bool:
        """True when the breaker would refuse a run now (without consuming a half-open trial)."""
        b = self.breakers[name]
        if b.state == "closed":
            return False
        if b.state == "open" and b.next_trial_ms is not None and now_ms >= b.next_trial_ms:
            return False
        return True

    def available(self, name: str, now_ms: int) -> bool:
        return self.provider is not None and not self.breaker_open(name, now_ms)

    # --- running --------------------------------------------------------------------------------
    def run(self, name: str, ctx: BotContext) -> AgentRun:
        """Run bot `name` on `ctx` and record its rows. Never raises for model trouble."""
        spec = self.specs[name]
        now = ctx.now_ms
        if self.provider is None:
            run = self._no_provider_run(spec, ctx, now)
        else:
            kwargs: dict[str, Any] = {}
            if self.sleep is not None:
                kwargs["sleep"] = self.sleep
            run = run_agent(
                spec,
                ctx,
                self.provider,
                self.store,
                now_ms=now,
                breaker=self.breakers[name],
                budget=self.budget,
                **kwargs,
            )
        try:
            self._record(name, run, ctx)
        except Exception as e:  # recording must never hide the run result
            run.failure_reason = (run.failure_reason or "") + f"; record failed: {e!r}"
        return run

    def output_of(self, run: AgentRun) -> Any:
        """The code-normalised output when the spec has one, else the raw validated output."""
        return run.finalized if run.finalized is not None else run.output

    def _no_provider_run(self, spec: AgentSpec, ctx: BotContext, now: int) -> AgentRun:
        reason = "no provider configured"
        output = spec.fallback(ctx, reason) if spec.fallback is not None else None
        finalized = spec.finalize(output, ctx) if (spec.finalize and output is not None) else None
        run_id = uuid.uuid4().hex[:16]
        self.store.record_run(
            run_id=run_id,
            agent=spec.name,
            started_ms=now,
            finished_ms=now,
            status="degraded",
            turns=0,
            cost_usd=0.0,
            degraded=True,
            failure_reason=reason,
            output=output.model_dump(mode="json") if output is not None else None,
        )
        return AgentRun(
            run_id=run_id,
            status="degraded",
            output=output,
            turns=0,
            cost_usd=0.0,
            latency_ms=0,
            degraded=True,
            failure_reason=reason,
            model_id="none",
            finalized=finalized,
        )

    # --- recording ------------------------------------------------------------------------------
    def _record(self, name: str, run: AgentRun, ctx: BotContext) -> None:
        out = self.output_of(run)
        if out is None:
            return
        now = ctx.now_ms
        if name == "strategy" and isinstance(out, StrategyProposal):
            self._record_proposal(run, out, ctx)
        elif name == "research" and isinstance(out, NewsExtraction):
            for e in out.events:
                self.c.execute(
                    "INSERT INTO news_events(created_ms, run_id, item_id, relevance, event_type, "
                    "direction_for_gold, magnitude, horizon, confidence, summary, degraded) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        now,
                        run.run_id,
                        e.item_id,
                        e.relevance,
                        e.event_type,
                        e.direction_for_gold,
                        e.magnitude,
                        e.horizon,
                        e.confidence,
                        e.summary,
                        int(run.degraded),
                    ),
                )
            for d in out.dropped:
                reason = d.split(": ", 1)[1] if ": " in d else ""
                self.c.execute(
                    "INSERT INTO news_dropped(created_ms, run_id, item_id, reason) VALUES (?,?,?,?)",
                    (now, run.run_id, dropped_id(d), reason),
                )
        elif name == "weekly_research" and isinstance(out, HypothesesOut):
            for h in out.hypotheses:
                self.c.execute(
                    "INSERT INTO hypotheses(created_ms, run_id, title, change_type, change, "
                    "rationale, pre_registered_metric, regime_scope, flagged_lookahead) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        now,
                        run.run_id,
                        h.title,
                        h.change_type,
                        h.change,
                        h.rationale,
                        h.pre_registered_metric,
                        h.regime_scope,
                        int(h.flagged_lookahead),
                    ),
                )
        elif name == "risk_advisor" and isinstance(out, RiskBudget):
            self.c.execute(
                "INSERT INTO risk_budgets(created_ms, run_id, advised, budget, reasons_json, "
                "hard_rules_json, degraded) VALUES (?,?,?,?,?,?,?)",
                (
                    now,
                    run.run_id,
                    out.advised,
                    out.budget,
                    _stable(out.reasons),
                    _stable(out.hard_rules),
                    int(run.degraded or out.degraded),
                ),
            )
        elif name == "review" and isinstance(out, PostMortem):
            trade_id = str((ctx.trade or {}).get("trade_id", ""))
            cur = self.c.execute(
                "INSERT INTO postmortems(created_ms, run_id, trade_id, outcome_class, signal_pnl, "
                "timing_pnl, slippage, costs, confidence, degraded) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    now,
                    run.run_id,
                    trade_id,
                    out.outcome_class,
                    out.signal_pnl,
                    out.timing_pnl,
                    out.slippage,
                    out.costs,
                    out.confidence,
                    int(run.degraded),
                ),
            )
            pm_id = int(cur.lastrowid)
            for lesson in out.lessons:
                self.c.execute(
                    "INSERT OR IGNORE INTO lessons(created_ms, postmortem_id, trade_id, text, "
                    "norm_text) VALUES (?,?,?,?,?)",
                    (now, pm_id, trade_id, lesson, normalise_lesson(lesson)),
                )
        elif name == "daily_brief" and isinstance(out, Brief):
            self.c.execute(
                "INSERT INTO briefs(created_ms, run_id, kind, sections_json, questions_json, "
                "degraded) VALUES (?,?,?,?,?,?)",
                (
                    now,
                    run.run_id,
                    "daily",
                    _stable(out.sections_dict()),
                    _stable(out.questions),
                    int(run.degraded),
                ),
            )

    def _record_proposal(self, run: AgentRun, p: StrategyProposal, ctx: BotContext) -> None:
        """`proposals` row with gate_status `proposed`; the cycle updates it after `gate_entry`."""
        self.c.execute(
            "INSERT OR REPLACE INTO proposals(proposal_id, created_ms, bot, bundle_id, action, side, "
            "stop_atr, target_atr, confidence, thesis, invalidation, agrees_with_signal, "
            "inputs_hash, raw_json, gate_status, gate_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run.run_id,
                ctx.now_ms,
                "strategy" if not run.degraded else "strategy:degraded",
                self.strat.id,
                p.action,
                p.side,
                p.stop_atr,
                p.target_atr,
                p.confidence,
                p.thesis,
                p.invalidation,
                int(p.agrees_with_signal),
                context_hash(ctx),
                _stable(
                    {
                        "raw": run.output.model_dump(mode="json") if run.output else None,
                        "final": p.model_dump(mode="json"),
                        "status": run.status,
                        "failure_reason": run.failure_reason,
                    }
                ),
                "proposed",
                None,
            ),
        )

    def lessons(self, limit: int = 5) -> list[str]:
        rows = self.c.execute(
            "SELECT text FROM lessons ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()
        return [r[0] for r in rows]

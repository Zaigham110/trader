"""`DecisionCycle`: ARCHITECTURE.md §3.1 steps 1–6 in code (AGENTS §1 "LLMs propose; code decides";
R-020, R-021, R-030).

    gateway.state() ──None──▶ gateway_unavailable (no bot call)
        │ kill_switch / paused ──▶ blocked
        ▼
    BotContext ─▶ Risk-advisor run ─▶ hard_rules ─▶ budget
        ▼
    Strategy run ─▶ StrategyProposal ─▶ proposals row ─▶ gate_entry (brain/gates.py)
        │ ok ─▶ Intent ─▶ gateway.send_intent ─▶ intents row ─▶ proposals.gate_status = sent
        └ not ok ─▶ proposals.gate_status = gated | wait

Every step is journaled in `brain_cycles.steps_json`; `run_once()` never raises (status `error`).
The brain's only path to the broker is the gateway client: this module imports nothing from
`goldtrader.mt5`, `goldtrader.execution` or `goldtrader.gateway.state`.

Signal-only mode: when the provider is missing or the Strategy breaker is open, the cycle fails
flat (`strategy.fallback_mode`) unless the owner passed `signal_only_fallback=True`, in which case
a deterministic-signal proposal (bot `signal_only`) goes through the same gates.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from goldtrader.bots.common import BotContext, context_hash
from goldtrader.bots.registry import BotRegistry
from goldtrader.bots.review import attribution
from goldtrader.bots.risk_advisor import RiskBudget, hard_rules
from goldtrader.bots.strategy import atr_bounds
from goldtrader.brain.gates import GateInput, gate_entry
from goldtrader.config.schema import StrategyConfig
from goldtrader.contracts import GatewayState, Intent, IntentResult, StrategyProposal
from goldtrader.gateway.market import LiveFeatures
from goldtrader.llm.runtime import AgentRun
from goldtrader.types import SessionState

log = logging.getLogger(__name__)


class GatewayLike(Protocol):
    """What the cycle needs from `goldtrader.gateway.api.GatewayClient`."""

    def state(self) -> GatewayState | None: ...

    def send_intent(self, intent: Intent) -> IntentResult: ...


@dataclass
class CycleResult:
    status: str  # sent | gated | wait | blocked | flat | gateway_unavailable | error
    proposal_id: str | None = None
    gate_reason: str | None = None
    intent_result: IntentResult | None = None
    risk_budget: int | None = None
    cycle_id: str = ""
    bundle_id: str | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)


def _stable(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _session(state: GatewayState) -> SessionState:
    try:
        return SessionState(state.session)
    except ValueError:
        return SessionState.CLOSED


class DecisionCycle:
    def __init__(
        self,
        cfg: Any,
        strat: StrategyConfig,
        registry: BotRegistry,
        gateway: GatewayLike,
        repo_conn: sqlite3.Connection,
        features_fn: Callable[[], LiveFeatures | None],
        signal_fn: Callable[[], int],
        news_fn: Callable[[], list[dict[str, Any]]],
        trades_fn: Callable[[], list[dict[str, Any]]],
        lessons_fn: Callable[[], list[str]],
        clock_ms: Callable[[], int],
        *,
        signal_only_fallback: bool = False,
        calibration_fn: Callable[[], str] | None = None,
        event_fn: Callable[[], tuple[str, float] | None] | None = None,
        router: Callable[[str], tuple[str, StrategyConfig]] | None = None,
    ) -> None:
        self.cfg = cfg
        self.strat = strat
        # D-047: optional limited-live router: proposal_id -> (bundle_id, strategy variant).
        # Without it every proposal is stamped with the strategy id and gated with its config.
        self.router = router
        self.registry = registry
        self.gateway = gateway
        self.c = repo_conn
        self.features_fn = features_fn
        self.signal_fn = signal_fn
        self.news_fn = news_fn
        self.trades_fn = trades_fn
        self.lessons_fn = lessons_fn
        self.clock_ms = clock_ms
        self.signal_only_fallback = signal_only_fallback
        self.calibration_fn = calibration_fn
        self.event_fn = event_fn

    # --- the cycle ---------------------------------------------------------------------------
    def run_once(self) -> CycleResult:
        """One decision cycle. Never raises: failures are journaled and returned as `error`."""
        started = self.clock_ms()
        res = CycleResult(status="error", cycle_id=uuid.uuid4().hex[:16])
        try:
            self._run(res)
        except Exception as e:  # noqa: BLE001 - the cycle must never take the scheduler down
            log.exception("decision cycle failed")
            res.status = "error"
            res.gate_reason = f"{type(e).__name__}: {e}"[:300]
            self._step(res, "error", error=res.gate_reason)
        try:
            self._journal(res, started)
        except Exception:  # noqa: BLE001
            log.exception("decision cycle journal write failed")
        return res

    def _run(self, res: CycleResult) -> None:
        state = self.gateway.state()
        if state is None:
            res.status = "gateway_unavailable"
            self._step(res, "gateway_state", available=False)
            return
        self._step(
            res,
            "gateway_state",
            available=True,
            session=state.session,
            kill_switch=state.kill_switch,
            paused=state.paused,
        )
        if state.kill_switch or state.paused:
            res.status = "blocked"
            res.gate_reason = "kill_switch" if state.kill_switch else "paused"
            return

        ctx = self._context(state)
        self._step(res, "context", signal_side=ctx.signal_side, news_events=len(ctx.news_events))

        # Risk-advisor (LLM, can only lower) → hard rules (code) → budget
        budget = self._risk_budget(ctx, res)
        ctx.risk_budget = budget
        res.risk_budget = budget

        # Strategy proposal
        proposal, proposal_id = self._proposal(ctx, res)
        if proposal is None:
            res.status = "flat"
            res.gate_reason = res.gate_reason or "strategy_unavailable"
            return
        res.proposal_id = proposal_id

        # Bundle routing (D-047): champion by default; a limited-live challenger by allocation
        bundle_id, strat = self.strat.id, self.strat
        if self.router is not None:
            try:
                bundle_id, strat = self.router(proposal_id)
            except Exception as e:  # noqa: BLE001 - routing failure must not block the champion
                self._step(res, "route", error=f"{type(e).__name__}: {e}")
        res.bundle_id = bundle_id
        self._step(res, "route", bundle_id=bundle_id, strategy=strat.id)
        self.c.execute(
            "UPDATE proposals SET bundle_id=? WHERE proposal_id=?", (bundle_id, proposal_id)
        )

        # Code gates
        now = self.clock_ms()
        gi = GateInput(
            proposal=proposal,
            proposal_id=proposal_id,
            proposal_age_s=max(0.0, (now - ctx.now_ms) / 1000.0),
            bundle_id=bundle_id,
            signal_side=ctx.signal_side,
            features=ctx.features,
            session=_session(state),
            entries_allowed=state.entries_allowed,
            open_positions=state.open_positions,
            risk_budget=budget,
            bid=state.bid if state.bid is not None else 0.0,
            ask=state.ask if state.ask is not None else 0.0,
            now_ms=now,
        )
        outcome = gate_entry(gi, self.cfg, strat)
        res.gate_reason = outcome.reason
        self._step(res, "gate", ok=outcome.ok, reason=outcome.reason)
        if not outcome.ok or outcome.intent is None:
            status = "wait" if outcome.reason == "wait" else "gated"
            self._update_proposal(proposal_id, status, outcome.reason)
            res.status = status
            return

        # Intent → gateway (the gateway re-runs the risk engine on every intent)
        intent = outcome.intent
        self._record_intent(intent)
        result = self.gateway.send_intent(intent)
        self._finish_intent(result)
        res.intent_result = result
        self._step(
            res,
            "intent",
            intent_id=intent.intent_id,
            kind=intent.kind,
            status=result.status,
            risk_code=result.risk_code,
        )
        self._update_proposal(proposal_id, "sent", result.status)
        res.status = "sent"

    # --- pieces --------------------------------------------------------------------------------
    def _context(self, state: GatewayState) -> BotContext:
        now = self.clock_ms()
        event = self.event_fn() if self.event_fn else None
        positions = state.positions or []
        return BotContext(
            now_ms=now,
            cfg=self.cfg,
            features=self.features_fn(),
            gateway_state=state,
            signal_side=int(self.signal_fn()),
            recent_trades=list(self.trades_fn())[:10],
            lessons=list(self.lessons_fn())[:5],
            news_events=list(self.news_fn()),
            open_position=dict(positions[0]) if positions else None,
            calibration_line=self.calibration_fn() if self.calibration_fn else "",
            next_event=event[0] if event else None,
            minutes_to_event=event[1] if event else None,
        )

    def _risk_budget(self, ctx: BotContext, res: CycleResult) -> int:
        run = self.registry.run("risk_advisor", ctx)
        out = self.registry.output_of(run)
        if isinstance(out, RiskBudget):
            budget, rules, advised = out.budget, out.hard_rules, out.advised
        else:  # breaker open / budget refused: code result only (AGENTS §3.3 escalation)
            budget, rules = hard_rules(ctx, 100)
            advised = None
        self._step(
            res,
            "risk_advisor",
            run_id=run.run_id,
            status=run.status,
            advised=advised,
            budget=budget,
            hard_rules=rules,
        )
        return budget

    def _proposal(self, ctx: BotContext, res: CycleResult) -> tuple[StrategyProposal | None, str]:
        if not self.registry.available("strategy", ctx.now_ms):
            why = "no_provider" if self.registry.provider is None else "breaker_open"
            if self.signal_only_fallback and self.strat.enabled and ctx.signal_side != 0:
                proposal = self._signal_only_proposal(ctx)
                proposal_id = uuid.uuid4().hex[:16]
                self._record_signal_only(proposal_id, proposal, ctx, why)
                self._step(res, "strategy", mode="signal_only", reason=why)
                return proposal, proposal_id
            res.gate_reason = why
            self._step(res, "strategy", mode="unavailable", reason=why, fallback="flat")
            return None, ""
        run = self.registry.run("strategy", ctx)
        out = self.registry.output_of(run)
        self._step(
            res,
            "strategy",
            run_id=run.run_id,
            status=run.status,
            degraded=run.degraded,
            action=getattr(out, "action", None),
        )
        if not isinstance(out, StrategyProposal):
            res.gate_reason = run.failure_reason or run.status
            return None, run.run_id
        return out, run.run_id

    def _signal_only_proposal(self, ctx: BotContext) -> StrategyProposal:
        lo, hi, rr = atr_bounds(self.cfg, self.strat)
        stop = (lo + hi) / 2.0
        side = "long" if ctx.signal_side > 0 else "short"
        return StrategyProposal(
            action="enter",
            side=side,
            stop_atr=stop,
            target_atr=stop * max(rr, 1.0) * 1.5,
            confidence=1.0,
            thesis=f"deterministic signal only ({self.strat.id}): {side}, LLM unavailable",
            invalidation="signal flips or the stop is hit",
            horizon_minutes=120,
            agrees_with_signal=True,
        )

    # --- journal -------------------------------------------------------------------------------
    def _step(self, res: CycleResult, name: str, **data: Any) -> None:
        res.steps.append({"step": name, "at_ms": self.clock_ms(), **data})

    def _journal(self, res: CycleResult, started: int) -> None:
        self.c.execute(
            "INSERT OR REPLACE INTO brain_cycles(cycle_id, started_ms, finished_ms, status, "
            "proposal_id, gate_reason, risk_budget, steps_json) VALUES (?,?,?,?,?,?,?,?)",
            (
                res.cycle_id,
                started,
                self.clock_ms(),
                res.status,
                res.proposal_id,
                res.gate_reason,
                res.risk_budget,
                _stable(res.steps),
            ),
        )

    def _update_proposal(self, proposal_id: str, gate_status: str, reason: str | None) -> None:
        self.c.execute(
            "UPDATE proposals SET gate_status=?, gate_reason=? WHERE proposal_id=?",
            (gate_status, reason, proposal_id),
        )

    def _record_signal_only(
        self, proposal_id: str, p: StrategyProposal, ctx: BotContext, why: str
    ) -> None:
        self.c.execute(
            "INSERT OR REPLACE INTO proposals(proposal_id, created_ms, bot, bundle_id, action, side, "
            "stop_atr, target_atr, confidence, thesis, invalidation, agrees_with_signal, "
            "inputs_hash, raw_json, gate_status, gate_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                proposal_id,
                ctx.now_ms,
                "signal_only",
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
                _stable({"final": p.model_dump(mode="json"), "why": why}),
                "proposed",
                None,
            ),
        )

    def _record_intent(self, intent: Intent) -> None:
        self.c.execute(
            "INSERT OR IGNORE INTO intents(intent_id, created_ms, proposal_id, kind, payload_json, "
            "status, source) VALUES (?,?,?,?,?,'sending',?)",
            (
                intent.intent_id,
                intent.created_ms,
                intent.proposal_id,
                intent.kind,
                intent.model_dump_json(),
                intent.source,
            ),
        )

    def _finish_intent(self, result: IntentResult) -> None:
        self.c.execute(
            "UPDATE intents SET status=?, risk_code=?, handled_ms=?, result_json=? WHERE intent_id=?",
            (
                result.status,
                result.risk_code,
                self.clock_ms(),
                result.model_dump_json(),
                result.intent_id,
            ),
        )

    # --- post-trade ----------------------------------------------------------------------------
    def review_closed_trade(
        self, trade: dict[str, Any], bars_1m: list[Any] | None = None
    ) -> AgentRun:
        """Review bot on one closed trade (ARCHITECTURE §3.3). Attribution is computed by code; the
        run records `postmortems` + deduplicated `lessons`. Callers may run this off the cycle."""
        attr = attribution(trade, bars_1m or [])
        ctx = BotContext(
            now_ms=self.clock_ms(),
            cfg=self.cfg,
            features=self.features_fn(),
            lessons=list(self.lessons_fn()),
            news_events=list(self.news_fn()),
            trade=dict(trade),
            attribution=attr,
        )
        return self.registry.run("review", ctx)

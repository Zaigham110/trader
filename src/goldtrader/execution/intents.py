"""`IntentHandler`: the gateway's single entry point for brain / chat intents (ARCHITECTURE.md §2.1
`api`, §3.1; contracts.Intent).

Maps `enter` / `exit` / `move_stop` / `close_all` / `pause` / `resume` / `kill` onto the
`ExecutionEngine` (R-003: the engine re-runs the risk engine on every one). `pause` / `resume`
toggle `paused`, which is folded into the `RiskContext` the engine sees. `kill` and `resume` are
owner-only: a `strategy_bot` source is rejected with detail `owner_only` (RISK_POLICY.md §3).
Idempotent on `intent_id`: a repeated id returns the stored result (in-memory map backed by the
`intents` table).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

from pydantic import ValidationError

from goldtrader.contracts import Intent, IntentResult
from goldtrader.execution.engine import ExecutionEngine
from goldtrader.risk.types import AccountState, EntryRequest, MarketState, RiskContext

StateProvider = Callable[[], tuple[AccountState, MarketState, RiskContext]]

OWNER_ONLY_KINDS = frozenset({"kill", "resume"})
OWNER_SOURCES = frozenset({"owner_confirmed", "system"})


class IntentHandler:
    def __init__(self, engine: ExecutionEngine, state_provider: StateProvider) -> None:
        self.engine = engine
        self.state_provider = state_provider
        self.paused: bool = False
        self._results: dict[str, IntentResult] = {}

    # ------------------------------------------------------------------ public
    def handle(self, intent: Intent | dict[str, Any]) -> IntentResult:
        try:
            model = intent if isinstance(intent, Intent) else Intent.model_validate(intent)
        except ValidationError as e:
            iid = str(intent.get("intent_id", "?")) if isinstance(intent, dict) else "?"
            return IntentResult(intent_id=iid, status="rejected", detail=f"invalid_intent: {e}")

        stored = self._results.get(model.intent_id) or self.engine.journal.intent_result(
            model.intent_id
        )
        if stored is not None:
            self._results[model.intent_id] = stored
            return stored

        self.engine.journal.record_intent(model)
        if model.kind in OWNER_ONLY_KINDS and model.source not in OWNER_SOURCES:
            result = IntentResult(intent_id=model.intent_id, status="rejected", detail="owner_only")
        else:
            result = self._dispatch(model)
        self._results[model.intent_id] = result
        self.engine.journal.finish_intent(result)
        return result

    # ------------------------------------------------------------------ dispatch
    def _dispatch(self, it: Intent) -> IntentResult:
        iid = it.intent_id
        if it.kind == "pause":
            self.paused = True
            return IntentResult(intent_id=iid, status="executed", detail="paused")
        if it.kind == "resume":
            self.paused = False
            return IntentResult(intent_id=iid, status="executed", detail="resumed")
        if it.kind == "kill":
            results = self.engine.kill(it.reason or f"kill intent {iid}")
            return IntentResult(
                intent_id=iid, status="executed", detail=f"killed; {len(results)} actions"
            )

        acct, market, ctx = self.state_provider()
        ctx = replace(ctx, paused=ctx.paused or self.paused)

        if it.kind == "enter":
            if it.side is None or it.stop_price is None or not it.bundle_id or not it.proposal_id:
                return IntentResult(
                    intent_id=iid,
                    status="rejected",
                    detail="invalid_intent: enter needs side, stop_price, bundle_id, proposal_id",
                )
            req = EntryRequest(
                side=it.side,
                stop_price=it.stop_price,
                target_price=it.target_price,
                bundle_id=it.bundle_id,
                proposal_id=it.proposal_id,
                reason=it.reason,
            )
            return self.engine.enter(req, acct, market, ctx, intent_id=iid)
        if it.kind == "exit":
            if it.position_id is None:
                return IntentResult(
                    intent_id=iid,
                    status="rejected",
                    detail="invalid_intent: exit needs position_id",
                )
            return self.engine.close(
                it.position_id, it.reason or "exit", market, ctx, intent_id=iid
            )
        if it.kind == "move_stop":
            if it.position_id is None or it.new_stop_price is None:
                return IntentResult(
                    intent_id=iid,
                    status="rejected",
                    detail="invalid_intent: move_stop needs position_id and new_stop_price",
                )
            return self.engine.move_stop(
                it.position_id, it.new_stop_price, market, ctx, intent_id=iid
            )
        if it.kind == "close_all":
            results = self.engine.close_all(it.reason or "close_all", market, ctx, intent_id=iid)
            failed = [r for r in results if r.status != "executed"]
            return IntentResult(
                intent_id=iid,
                status="executed" if not failed else "failed",
                detail=f"closed {len(results) - len(failed)} of {len(results)}",
            )
        return IntentResult(intent_id=iid, status="rejected", detail=f"unknown kind {it.kind}")


__all__ = ["OWNER_ONLY_KINDS", "OWNER_SOURCES", "IntentHandler", "StateProvider"]

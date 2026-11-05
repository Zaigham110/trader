"""Agent runtime: the loop every LLM bot runs through (R-021, R-023, R-025; D-031).

    AgentSpec  -> run_agent(spec, ctx, provider, store, now_ms=...) -> AgentRun

Order of operations (docs/research/05_existing_platform_audit.md §2, re-implemented):

1. Circuit breaker and daily / monthly budget are checked **before** any call (`breaker_open`,
   `budget`: no provider call, not counted as a failure).
2. The request carries the output JSON schema built from the pydantic output model; the provider
   enforces it (structured outputs / grammar) and the runtime re-validates with pydantic plus the
   spec's `check()`.
3. Tool calls: input validated against the tool's schema; the handler runs under the run deadline;
   results (or the validation error) go back as a `tool_result` block; turns are capped.
4. Invalid output: the errors are fed back as a user message and the model resubmits, up to
   `max_invalid_resubmits` extra attempts.
5. `ProviderTransientError`: retried 3× with 1 / 2 / 4 s backoff (sleep injectable) inside the one
   wall-clock deadline that also covers tool handlers.
6. Timeout, refusal, `max_tokens`, invalid exhaustion, provider error, run cost cap: the spec's
   `fallback(ctx, reason)` (rule-based default) returns with status `degraded`; without a fallback
   the failure status is returned. Failures feed the breaker; successes close it.
7. Every call and every run is written to the `LLMStore` ledger.

Never: an LLM failure changing a risk limit, delaying a stop or blocking the kill switch. This
module imports nothing from execution, risk or the gateway.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from goldtrader.llm.breaker import CircuitBreaker
from goldtrader.llm.budget import Budget
from goldtrader.llm.prompts import PromptRef
from goldtrader.llm.provider import (
    Effort,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ProviderError,
    ProviderRefusal,
    ProviderTransientError,
    ToolCall,
    ToolSpec,
    Usage,
)
from goldtrader.llm.schema import pydantic_to_schema, validate
from goldtrader.llm.store import LLMStore

TRANSIENT_RETRIES = 3
TRANSIENT_BACKOFF_S = (1.0, 2.0, 4.0)
TOOL_RESULT_MAX_CHARS = 20_000

RunStatus = str  # ok | invalid | timeout | budget | breaker_open | error | degraded


class _Deadline(Exception):
    """Raised inside the loop when the run's wall-clock deadline passes."""


@dataclass(frozen=True)
class Tool:
    """A tool the agent may call: its spec plus a read-only handler (`handler(input) -> result`)."""

    spec: ToolSpec
    handler: Callable[[dict[str, Any]], Any]


@dataclass(frozen=True)
class AgentSpec:
    """What a bot supplies (docs/AGENTS.md §3). Bots never see the provider or the store."""

    name: str
    tier: str
    system_prompt: PromptRef
    build_messages: Callable[[Any], list[dict[str, Any]]]
    output_model: type[BaseModel]
    tools: list[Tool] = field(default_factory=list)
    check: Callable[[Any], list[str]] | None = None
    finalize: Callable[[Any, Any], Any] | None = None
    timeout_s: float = 45.0
    max_turns: int = 6
    max_cost_usd: float = 0.10
    max_invalid_resubmits: int = 2
    fallback: Callable[[Any, str], BaseModel] | None = None
    effort: Effort = "low"
    max_tokens: int = 4096
    schema_version: int = 1


@dataclass
class AgentRun:
    run_id: str
    status: RunStatus
    output: BaseModel | None
    turns: int
    cost_usd: float
    latency_ms: int
    degraded: bool
    failure_reason: str | None
    model_id: str
    finalized: Any = None


def run_agent(
    spec: AgentSpec,
    ctx: Any,
    provider: ModelProvider,
    store: LLMStore,
    *,
    now_ms: int,
    breaker: CircuitBreaker | None = None,
    budget: Budget | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> AgentRun:
    """Run one agent to completion under the spec's deadline and cost cap. Never raises for
    provider / model trouble; the `AgentRun.status` says what happened."""
    return _Runner(spec, ctx, provider, store, now_ms, breaker, budget, sleep).run()


class _Runner:
    def __init__(
        self,
        spec: AgentSpec,
        ctx: Any,
        provider: ModelProvider,
        store: LLMStore,
        now_ms: int,
        breaker: CircuitBreaker | None,
        budget: Budget | None,
        sleep: Callable[[float], None],
    ) -> None:
        self.spec = spec
        self.ctx = ctx
        self.provider = provider
        self.store = store
        self.now_ms = now_ms
        self.breaker = breaker
        self.budget = budget
        self.sleep = sleep
        self.run_id = uuid.uuid4().hex[:16]
        self.started_mono = time.monotonic()
        self.deadline = self.started_mono + spec.timeout_s
        self.turns = 0
        self.cost = 0.0
        self.model_id = provider.model_for_tier(spec.tier)
        self.schema = pydantic_to_schema(spec.output_model)
        self.tools = {t.spec.name: t for t in spec.tools}

    # --- helpers ----------------------------------------------------------------------------
    def _elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started_mono) * 1000)

    def _clock_ms(self) -> int:
        return self.now_ms + self._elapsed_ms()

    def _remaining(self) -> float:
        return self.deadline - time.monotonic()

    def _finish(
        self,
        status: RunStatus,
        output: BaseModel | None,
        failure_reason: str | None,
        degraded: bool = False,
        finalized: Any = None,
    ) -> AgentRun:
        self.store.record_run(
            run_id=self.run_id,
            agent=self.spec.name,
            started_ms=self.now_ms,
            finished_ms=self._clock_ms(),
            status=status,
            turns=self.turns,
            cost_usd=self.cost,
            degraded=degraded,
            failure_reason=failure_reason,
            output=output.model_dump(mode="json") if output is not None else None,
        )
        return AgentRun(
            run_id=self.run_id,
            status=status,
            output=output,
            turns=self.turns,
            cost_usd=self.cost,
            latency_ms=self._elapsed_ms(),
            degraded=degraded,
            failure_reason=failure_reason,
            model_id=self.model_id,
            finalized=finalized,
        )

    def _fail(self, status: RunStatus, reason: str) -> AgentRun:
        """A real failure: feeds the breaker, then fallback (degraded) or the failure status."""
        if self.breaker is not None:
            self.breaker.record_failure(self._clock_ms(), reason)
        if self.spec.fallback is not None:
            output = self.spec.fallback(self.ctx, reason)
            finalized = self.spec.finalize(output, self.ctx) if self.spec.finalize else None
            return self._finish("degraded", output, reason, degraded=True, finalized=finalized)
        return self._finish(status, None, reason)

    def _record_call(
        self, response: ModelResponse | None, status: str, latency_ms: int, payload_hash: str
    ) -> None:
        self.store.record_call(
            agent=self.spec.name,
            run_id=self.run_id,
            model_id=response.model_id if response else self.model_id,
            prompt_hash=self.spec.system_prompt.hash,
            schema_version=self.spec.schema_version,
            usage=response.usage if response else Usage(),
            cost_usd=response.cost_usd if response else 0.0,
            latency_ms=latency_ms,
            status=status,
            stop_reason=response.stop_reason if response else None,
            payload_hash=payload_hash,
            created_ms=self._clock_ms(),
        )

    # --- main loop --------------------------------------------------------------------------
    def run(self) -> AgentRun:
        now = self.now_ms
        if self.breaker is not None and not self.breaker.allows(now):
            return self._finish("breaker_open", None, f"breaker open for {self.spec.name}")
        if self.budget is not None:
            why = self.budget.refusal(self.store, now)
            if why is not None:
                return self._finish("budget", None, why)

        try:
            messages = list(self.spec.build_messages(self.ctx))
        except Exception as e:  # a bot's own bug must not escape as an unhandled error
            return self._fail("error", f"build_messages failed: {e!r}")

        invalid_resubmits = 0
        while True:
            if self.turns >= self.spec.max_turns:
                return self._fail("error", f"max_turns {self.spec.max_turns} reached")
            try:
                response = self._call_model(messages)
            except _Deadline:
                return self._fail("timeout", f"deadline {self.spec.timeout_s}s exceeded")
            except ProviderRefusal as e:
                return self._fail("error", f"refusal: {e}")
            except ProviderTransientError as e:
                return self._fail("error", f"transient errors exhausted: {e}")
            except ProviderError as e:
                return self._fail("error", f"provider error: {e}")
            except Exception as e:  # pragma: no cover - defensive
                return self._fail("error", f"unexpected provider failure: {e!r}")

            self.turns += 1
            self.cost += response.cost_usd
            if self.cost > self.spec.max_cost_usd:
                return self._fail(
                    "budget", f"run cost {self.cost:.4f} USD over cap {self.spec.max_cost_usd}"
                )
            if response.stop_reason == "refusal":
                return self._fail("error", "refusal")
            if response.stop_reason == "max_tokens":
                return self._fail("error", "max_tokens: output truncated")

            if response.tool_calls:
                messages.append(_assistant_tool_use(response))
                try:
                    results = [self._run_tool(tc) for tc in response.tool_calls]
                except _Deadline:
                    return self._fail(
                        "timeout", f"deadline {self.spec.timeout_s}s exceeded in tool"
                    )
                messages.append({"role": "user", "content": results})
                continue

            output, problems = self._parse_output(response)
            if output is not None and not problems:
                if self.breaker is not None:
                    self.breaker.record_success(self._clock_ms())
                finalized = self.spec.finalize(output, self.ctx) if self.spec.finalize else None
                return self._finish("ok", output, None, finalized=finalized)

            invalid_resubmits += 1
            if invalid_resubmits > self.spec.max_invalid_resubmits:
                return self._fail("invalid", "invalid output: " + "; ".join(problems)[:500])
            messages.append({"role": "assistant", "content": response.raw_text or "(no output)"})
            messages.append(
                {
                    "role": "user",
                    "content": "Your output was invalid. Fix every problem and resubmit the complete "
                    "output matching the schema exactly:\n- " + "\n- ".join(problems),
                }
            )

    # --- one model call with transient retry under the deadline ----------------------------
    def _call_model(self, messages: list[dict[str, Any]]) -> ModelResponse:
        payload_hash = _payload_hash(self.spec.system_prompt.text, messages)
        last: ProviderTransientError | None = None
        for attempt in range(TRANSIENT_RETRIES + 1):
            remaining = self._remaining()
            if remaining <= 0:
                self._record_call(None, "timeout", self._elapsed_ms(), payload_hash)
                raise _Deadline()
            request = ModelRequest(
                tier=self.spec.tier,
                system=self.spec.system_prompt.text,
                messages=list(messages),
                output_schema=self.schema,
                tools=[t.spec for t in self.spec.tools],
                max_tokens=self.spec.max_tokens,
                timeout_s=remaining,
                effort=self.spec.effort,
                metadata={
                    "bot": self.spec.name,
                    "run_id": self.run_id,
                    "prompt_hash": self.spec.system_prompt.hash,
                    "attempt": attempt,
                },
            )
            t0 = time.monotonic()
            try:
                response = _race(lambda req=request: self.provider.complete(req), remaining)
            except _Deadline:
                self._record_call(
                    None, "timeout", int((time.monotonic() - t0) * 1000), payload_hash
                )
                raise
            except ProviderTransientError as e:
                last = e
                self._record_call(
                    None, "transient_error", int((time.monotonic() - t0) * 1000), payload_hash
                )
                if attempt >= TRANSIENT_RETRIES:
                    break
                backoff = TRANSIENT_BACKOFF_S[min(attempt, len(TRANSIENT_BACKOFF_S) - 1)]
                if backoff >= self._remaining():
                    raise _Deadline() from e
                self.sleep(backoff)
                continue
            except ProviderRefusal:
                self._record_call(
                    None, "refusal", int((time.monotonic() - t0) * 1000), payload_hash
                )
                raise
            except ProviderError:
                self._record_call(None, "error", int((time.monotonic() - t0) * 1000), payload_hash)
                raise
            status = (
                "tool_use"
                if response.tool_calls
                else "refusal"
                if response.stop_reason == "refusal"
                else "ok"
                if response.parsed_output is not None
                else "invalid"
            )
            self._record_call(response, status, response.latency_ms, payload_hash)
            return response
        assert last is not None
        raise last

    # --- tools --------------------------------------------------------------------------------
    def _run_tool(self, call: ToolCall) -> dict[str, Any]:
        tool = self.tools.get(call.name)
        if tool is None:
            return _tool_result(call.id, f"unknown tool {call.name!r}", is_error=True)
        problems = validate(call.input, tool.spec.input_schema)
        if problems:
            return _tool_result(
                call.id, "invalid tool input: " + "; ".join(problems), is_error=True
            )
        remaining = self._remaining()
        if remaining <= 0:
            raise _Deadline()
        try:
            result = _race(lambda: tool.handler(call.input), remaining)
        except _Deadline:
            raise
        except Exception as e:  # tool bugs are reported to the model, not raised
            return _tool_result(call.id, f"tool error: {e!r}", is_error=True)
        text = result if isinstance(result, str) else json.dumps(result, default=str)
        return _tool_result(call.id, text[:TOOL_RESULT_MAX_CHARS])

    # --- output -------------------------------------------------------------------------------
    def _parse_output(self, response: ModelResponse) -> tuple[BaseModel | None, list[str]]:
        data = response.parsed_output
        if data is None:
            try:
                data = json.loads(response.raw_text) if response.raw_text else None
            except json.JSONDecodeError:
                return None, ["output is not valid JSON"]
            if not isinstance(data, dict):
                return None, ["output must be a JSON object matching the schema"]
            problems = validate(data, self.schema)
            if problems:
                return None, problems
        try:
            output = self.spec.output_model.model_validate(data)
        except ValidationError as e:
            return None, [
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()
            ]
        problems = list(self.spec.check(output)) if self.spec.check is not None else []
        return (output, problems) if not problems else (None, problems)


# --- module helpers ------------------------------------------------------------------------------
def _race(fn: Callable[[], Any], timeout_s: float) -> Any:
    """Run `fn` in a daemon thread and wait at most `timeout_s`; `_Deadline` if it does not finish.
    An abandoned worker keeps running until it returns, like an aborted HTTP call draining."""
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as e:  # propagate into the caller's thread
            box["error"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(max(timeout_s, 0.0))
    if t.is_alive():
        raise _Deadline()
    if "error" in box:
        raise box["error"]
    return box["value"]


def _assistant_tool_use(response: ModelResponse) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input}
            for tc in response.tool_calls
        ],
    }


def _tool_result(tool_use_id: str, content: str, is_error: bool = False) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
        "is_error": is_error,
    }


def _payload_hash(system: str, messages: list[dict[str, Any]]) -> str:
    """sha256 of the outbound payload (LOCAL_LLM.md §6): proves what was sent, stores none of it."""
    body = json.dumps({"system": system, "messages": messages}, sort_keys=True, default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()

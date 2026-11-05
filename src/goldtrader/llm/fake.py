"""`FakeProvider`: scripted model turns for tests (D-031). No network, no SDK.

    fake = FakeProvider()
    fake.script("standard", [{"action": "wait", ...}])            # structured output
    fake.script("fast", [ToolCall("t1", "get_features", {}), {...}])  # tool call, then output
    fake.script("standard", ["not json at all"])                 # raw invalid text
    fake.script("standard", [ProviderTransientError("503"), {...}])  # raise, then succeed
    fake.script("standard", [HANG])                               # sleeps past request.timeout_s
    fake.script("standard", [REFUSE])                             # stop_reason "refusal"

A scripted dict is validated against the request's `output_schema` exactly like a real provider's
structured output would be; a dict that violates it comes back as `raw_text` with
`parsed_output=None`, which exercises the runtime's repair path (R-021).
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from goldtrader.llm.provider import (
    PRICES,
    ModelRequest,
    ModelResponse,
    PriceTable,
    ProviderError,
    ProviderTransientError,
    ToolCall,
    Usage,
    estimate_cost,
)
from goldtrader.llm.schema import validate


class _Sentinel:
    def __init__(self, name: str):
        self.name = name

    def __repr__(self) -> str:
        return f"<{self.name}>"


HANG = _Sentinel("HANG")
"""Sleep until just past `request.timeout_s`, then raise `ProviderTransientError` (SDK timeout)."""
REFUSE = _Sentinel("REFUSE")
"""Return a response with stop_reason 'refusal' and no output."""
MAX_TOKENS = _Sentinel("MAX_TOKENS")
"""Return a truncated response with stop_reason 'max_tokens'."""

DEFAULT_TIERS: dict[str, str] = {
    "fast": "claude-haiku-5-5",
    "standard": "claude-sonnet-5-5",
    "deep": "claude-opus-5-5",
}

Turn = Any  # dict | ToolCall | list[ToolCall] | str | BaseException | _Sentinel | ModelResponse


@dataclass
class FakeProvider:
    tiers: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_TIERS))
    prices: PriceTable = field(default_factory=lambda: dict(PRICES))
    chars_per_token: int = 4
    requests: list[ModelRequest] = field(default_factory=list)
    _scripts: dict[str, deque[Turn]] = field(default_factory=dict)

    # --- scripting -------------------------------------------------------------------------
    def script(self, tier_or_model: str, turns: list[Turn]) -> None:
        """Queue turns for a tier (looked up first) or a model id."""
        self._scripts.setdefault(tier_or_model, deque()).extend(turns)

    def pending(self, tier_or_model: str) -> int:
        return len(self._scripts.get(tier_or_model, ()))

    # --- ModelProvider ----------------------------------------------------------------------
    def model_for_tier(self, tier: str) -> str:
        try:
            return self.tiers[tier]
        except KeyError as e:
            raise ProviderError(f"unknown tier {tier!r}") from e

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        model_id = self.model_for_tier(request.tier)
        turn = self._next_turn(request.tier, model_id)
        started = time.monotonic()

        if isinstance(turn, BaseException):
            raise turn
        if turn is HANG:
            time.sleep(request.timeout_s + 0.1)
            raise ProviderTransientError(f"timed out after {request.timeout_s}s")

        in_tokens = self._count(request.system) + self._count(json.dumps(request.messages))
        if isinstance(turn, ModelResponse):
            return turn

        parsed: dict[str, Any] | None = None
        raw_text = ""
        tool_calls: list[ToolCall] = []
        stop_reason = "end_turn"
        if turn is REFUSE:
            stop_reason = "refusal"
        elif turn is MAX_TOKENS:
            stop_reason = "max_tokens"
            raw_text = "{"
        elif isinstance(turn, ToolCall):
            tool_calls = [turn]
            stop_reason = "tool_use"
        elif isinstance(turn, list) and all(isinstance(t, ToolCall) for t in turn):
            tool_calls = list(turn)
            stop_reason = "tool_use"
        elif isinstance(turn, dict):
            raw_text = json.dumps(turn, sort_keys=True)
            if not validate(turn, request.output_schema):
                parsed = turn
        elif isinstance(turn, str):
            raw_text = turn
        else:
            raise ProviderError(f"unsupported scripted turn {turn!r}")

        out_tokens = self._count(raw_text) + sum(
            self._count(json.dumps(tc.input)) + 4 for tc in tool_calls
        )
        usage = Usage(input_tokens=in_tokens, output_tokens=out_tokens)
        return ModelResponse(
            parsed_output=parsed,
            raw_text=raw_text,
            tool_calls=tool_calls,
            usage=usage,
            cost_usd=estimate_cost(model_id, usage, self.prices),
            latency_ms=int((time.monotonic() - started) * 1000),
            model_id=model_id,
            stop_reason=stop_reason,
        )

    # --- internals --------------------------------------------------------------------------
    def _next_turn(self, tier: str, model_id: str) -> Turn:
        for key in (tier, model_id):
            q = self._scripts.get(key)
            if q:
                return q.popleft()
        raise ProviderError(f"FakeProvider: no scripted turn for tier {tier!r} / {model_id!r}")

    def _count(self, text: str) -> int:
        return max(1, len(text) // self.chars_per_token) if text else 0

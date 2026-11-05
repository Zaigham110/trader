"""Provider-agnostic model interface (R-022) and the price table used for cost accounting (R-025).

Bots never import a concrete provider; they receive a `ModelProvider` by injection (D-019, D-031).
The Anthropic adapter (Phase 5) and a local OpenAI-compatible adapter implement the same
`complete()` / `model_for_tier()` pair; `FakeProvider` (`goldtrader.llm.fake`) is the test double.

Message format (provider-neutral, Anthropic-shaped so the adapter is a thin mapping):

* `{"role": "user" | "assistant", "content": str | list[block]}`
* assistant tool-use block: `{"type": "tool_use", "id", "name", "input"}`
* user tool-result block: `{"type": "tool_result", "tool_use_id", "content": str, "is_error": bool}`
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

Effort = Literal["low", "medium", "high"]


# --- exceptions -----------------------------------------------------------------------------
class ProviderError(Exception):
    """Base class: the provider could not produce a response (non-retryable unless subclassed)."""


class ProviderTransientError(ProviderError):
    """Timeouts, connection errors, 408/409/429 and 5xx: retried by the runtime with backoff."""


class ProviderRefusal(ProviderError):
    """The model refused (stop_reason `refusal`); counted as a failure, never retried (R-021)."""


class ProviderInvalidOutput(ProviderError):
    """The provider returned something the adapter could not read as output or tool calls."""


# --- request / response ---------------------------------------------------------------------
@dataclass(frozen=True)
class ToolSpec:
    """A tool offered to the model. `input_schema` is a flat JSON schema (see `llm.schema`)."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    """A tool invocation requested by the model."""

    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class Usage:
    """Token counts for one call. Cache fields are zero on providers without prompt caching."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
        )


@dataclass(frozen=True)
class ModelRequest:
    """One model call (LOCAL_LLM.md §1). `output_schema` is the JSON schema the output must obey."""

    tier: str
    system: str
    messages: list[dict[str, Any]]
    output_schema: dict[str, Any]
    tools: list[ToolSpec] = field(default_factory=list)
    max_tokens: int = 4096
    timeout_s: float = 30.0
    effort: Effort = "low"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelResponse:
    """The provider's answer. Exactly one of `parsed_output` / `tool_calls` / raw text is meaningful.

    `parsed_output` is None when the output did not validate against `output_schema`; the raw text
    is kept so the runtime can feed the validation errors back (R-021).
    """

    parsed_output: dict[str, Any] | None
    raw_text: str
    tool_calls: list[ToolCall]
    usage: Usage
    cost_usd: float
    latency_ms: int
    model_id: str
    stop_reason: str
    degraded: bool = False


@runtime_checkable
class ModelProvider(Protocol):
    """What every adapter implements (R-022)."""

    def complete(self, request: ModelRequest) -> ModelResponse:
        """Run one call. Raises `ProviderTransientError` for retryable failures."""
        ...

    def model_for_tier(self, tier: str) -> str:
        """Resolve a config tier (fast | standard | deep) to a concrete model id."""
        ...


# --- prices --------------------------------------------------------------------------------
PriceTable = dict[str, tuple[float, float]]
"""model id → (USD per 1M input tokens, USD per 1M output tokens)."""

PRICES: PriceTable = {
    # Verified list prices 2026-11-04 (docs/LOCAL_LLM.md §2).
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-opus-5-5": (4.0, 20.0),
}

CACHE_READ_MULT = 0.10
"""Cache reads are billed at 10% of the input price (Anthropic prompt caching)."""
CACHE_WRITE_MULT = 1.25
"""Cache writes are billed at 125% of the input price (5-minute cache)."""


def estimate_cost(model_id: str, usage: Usage, prices: PriceTable | None = None) -> float:
    """USD cost of one call from the price table (R-025).

    Unknown models are billed at the most expensive known row so a mis-configured model id can
    only over-count, never hide spend from the budget.
    """
    table = prices if prices is not None else PRICES
    if model_id in table:
        in_price, out_price = table[model_id]
    else:
        in_price, out_price = max(table.values(), key=lambda p: p[0] + p[1])
    per_m = 1_000_000
    return (
        usage.input_tokens * in_price
        + usage.cache_read_tokens * in_price * CACHE_READ_MULT
        + usage.cache_write_tokens * in_price * CACHE_WRITE_MULT
        + usage.output_tokens * out_price
    ) / per_m

"""Anthropic adapter for the `ModelProvider` protocol (D-037; docs/LOCAL_LLM.md §2, §5).

Thin mapping from the provider-neutral `ModelRequest` to one `messages.create` /
`messages.stream` call on the official `anthropic` SDK and back to a `ModelResponse`.

What is sent (Claude API reference, 2026-11):

* `model` from `cfg.tiers[tier].model` (`claude-haiku-5-5` / `claude-sonnet-5-5` / `claude-opus-5-5`,
  no date suffix).
* `system` as a list with one text block carrying `cache_control: ephemeral`; the tool list carries a
  second breakpoint on its last tool. Tools render first, then system, so the one system breakpoint
  caches both; the tool breakpoint keeps a read point when the system prompt changes.
* `messages` passed through: they are already Anthropic-shaped (`provider.py` module docstring).
  Anything that is not a user/assistant message with text / `tool_use` / `tool_result` content is a
  `ProviderError` before any network call.
* `tools` with `strict: true`; every object schema gets `additionalProperties: false` and a
  `required` list naming every property (strict mode's contract).
* `output_config.format = {"type": "json_schema", "schema": request.output_schema}` whenever the
  request has an output schema (the runtime relies on `parsed_output`), together with the tools:
  the reference documents the combination ("Using Both Together"), so no fallback is needed.
* `output_config.effort`: the higher of the tier's configured effort and the request's effort.
  Thinking is adaptive by default on these models, so no `thinking`, `budget_tokens`, `temperature`,
  `top_p` or `tool_choice` is sent (forced `tool_choice` is a 400 on Opus 5.5 / Sonnet 5.5; the
  prompt names the tool instead).
* Streaming (`messages.stream` + `get_final_message()`) for the deep tier and for
  `max_tokens > NONSTREAMING_MAX_TOKENS`; plain `create` otherwise.
* The request timeout through `client.with_options(timeout=..., max_retries=...)`; SDK retries stay
  off (`max_retries=0`) because the runtime owns retries and the budget (LOCAL_LLM §5.1).

What comes back: `stop_reason` is checked first. `refusal` raises `ProviderRefusal` (with the
`stop_details` category / explanation on `.detail`); `max_tokens` returns a response the runtime
treats as a failure; text blocks become `raw_text` and are parsed + validated against the output
schema into `parsed_output` (None when invalid: the runtime repairs); `tool_use` blocks become
`ToolCall`s. Usage includes cache read / write tokens; cost comes from `estimate_cost`.

Errors: `APITimeoutError`, `APIConnectionError`, 408 / 409 / 429 / 5xx (incl. 529 overloaded) →
`ProviderTransientError`; 400 / 401 / 403 / 404 and every other status → `ProviderError`. Messages
are redacted of anything that looks like an API key before they leave this module.

Optional server-side refusal fallback (`enable_fallbacks=True`, off by default): the call goes
through `client.beta.messages` with `betas=["server-side-fallback-2026-07-01"]` and
`fallbacks="default"`; a response served by a fallback model is marked `degraded`. The owner
enables this deliberately (LOCAL_LLM §5.4); the beta header and the scalar form must match.

Secrets: the key comes from the `api_key` argument, else `goldtrader.secrets` (environment /
keyring), else the SDK's own resolution (`ANTHROPIC_API_KEY`). It is never stored on the provider,
never logged, and `repr(provider)` shows only tier → model ids.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import anthropic

from goldtrader.config.schema import LLMConfig, LLMTier
from goldtrader.llm.provider import (
    Effort,
    ModelRequest,
    ModelResponse,
    ProviderError,
    ProviderRefusal,
    ProviderTransientError,
    ToolCall,
    ToolSpec,
    Usage,
    estimate_cost,
)
from goldtrader.llm.schema import validate

NONSTREAMING_MAX_TOKENS = 16_000
"""Above this `max_tokens` the call streams: the SDK refuses long non-streaming requests."""
STREAMING_TIERS: frozenset[str] = frozenset({"deep"})
"""Tiers that always stream (weekly research on Opus; LOCAL_LLM §2 "streaming for long outputs")."""
FALLBACK_BETA = "server-side-fallback-2026-07-01"
"""Beta header for the scalar `fallbacks="default"` form (README → Refusal Fallbacks)."""
TRANSIENT_STATUSES: frozenset[int] = frozenset({408, 409, 429, 529})
"""Statuses retried by the runtime besides every 5xx (shared/error-codes.md)."""

_CACHE = {"type": "ephemeral"}
_EFFORT_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2}
_KEY_SHAPE = re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}")
_MASK = "***"
_BLOCK_KEYS: dict[str, tuple[str, ...]] = {
    "text": ("text",),
    "tool_use": ("id", "name", "input"),
    "tool_result": ("tool_use_id",),
}


# --- pure helpers (testable without a client) --------------------------------------------------
def redact(text: str) -> str:
    """Mask anything shaped like an Anthropic API key (R-053)."""
    return _KEY_SHAPE.sub(_MASK, text)


def validate_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Check the provider-neutral message shape; return the list unchanged or raise ProviderError."""
    if not messages:
        raise ProviderError("messages must not be empty")
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict) or msg.get("role") not in ("user", "assistant"):
            raise ProviderError(f"messages[{i}]: role must be 'user' or 'assistant'")
        content = msg.get("content")
        if isinstance(content, str):
            continue
        if not isinstance(content, list) or not content:
            raise ProviderError(f"messages[{i}]: content must be a string or a list of blocks")
        for j, block in enumerate(content):
            if not isinstance(block, dict):
                raise ProviderError(f"messages[{i}].content[{j}]: block must be an object")
            btype = block.get("type")
            keys = _BLOCK_KEYS.get(btype)
            if keys is None:
                raise ProviderError(
                    f"messages[{i}].content[{j}]: unsupported block type {btype!r} "
                    "(text, tool_use, tool_result only)"
                )
            missing = [k for k in keys if k not in block]
            if missing:
                raise ProviderError(f"messages[{i}].content[{j}]: {btype} block lacks {missing}")
            if btype == "tool_use" and not isinstance(block["input"], dict):
                raise ProviderError(f"messages[{i}].content[{j}]: tool_use input must be an object")
    return messages


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Copy of a tool input schema that satisfies `strict: true`.

    Every object level gets `additionalProperties: false` and a `required` list naming every
    property (optional fields express optionality through a nullable type, see `llm.schema`).
    """
    out = dict(schema)
    schema_type = out.get("type", "object")
    is_object = schema_type == "object" or (
        isinstance(schema_type, list) and "object" in schema_type
    )
    if is_object or "properties" in out:
        props = {
            k: strict_schema(v) if isinstance(v, dict) else v
            for k, v in out.get("properties", {}).items()
        }
        out.setdefault("type", "object")
        out["properties"] = props
        out["required"] = list(props.keys())
        out["additionalProperties"] = False
    items = out.get("items")
    if isinstance(items, dict):
        out["items"] = strict_schema(items)
    return out


def tool_param(spec: ToolSpec) -> dict[str, Any]:
    """One `ToolSpec` as a strict Anthropic tool definition."""
    return {
        "name": spec.name,
        "description": spec.description,
        "strict": True,
        "input_schema": strict_schema(spec.input_schema),
    }


def resolve_effort(request_effort: Effort, tier_effort: str) -> str:
    """The deeper of the tier's configured effort and the request's effort."""
    candidates = [e for e in (request_effort, tier_effort) if e in _EFFORT_RANK]
    if not candidates:
        return tier_effort
    return max(candidates, key=lambda e: _EFFORT_RANK[e])


def should_stream(request: ModelRequest) -> bool:
    return request.tier in STREAMING_TIERS or request.max_tokens > NONSTREAMING_MAX_TOKENS


def build_create_kwargs(request: ModelRequest, tier_cfg: LLMTier) -> dict[str, Any]:
    """The exact keyword payload for `messages.create` / `messages.stream` (pure, no network).

    Never contains `thinking`, `temperature`, `top_p`, `top_k` or `tool_choice`.
    """
    kwargs: dict[str, Any] = {
        "model": tier_cfg.model,
        "max_tokens": request.max_tokens,
        "system": [{"type": "text", "text": request.system, "cache_control": dict(_CACHE)}],
        "messages": validate_messages(list(request.messages)),
    }
    if request.tools:
        tools = [tool_param(t) for t in request.tools]
        tools[-1]["cache_control"] = dict(_CACHE)
        kwargs["tools"] = tools
    output_config: dict[str, Any] = {"effort": resolve_effort(request.effort, tier_cfg.effort)}
    if request.output_schema:
        output_config["format"] = {"type": "json_schema", "schema": request.output_schema}
    kwargs["output_config"] = output_config
    return kwargs


# --- response mapping ---------------------------------------------------------------------------
def _block_attr(block: Any, name: str, default: Any = None) -> Any:
    if isinstance(block, dict):
        return block.get(name, default)
    return getattr(block, name, default)


def _map_response(
    message: Any, request: ModelRequest, latency_ms: int, requested_model: str
) -> ModelResponse:
    stop_reason = str(_block_attr(message, "stop_reason") or "end_turn")
    model_id = str(_block_attr(message, "model") or requested_model)
    if stop_reason == "refusal":
        details = _block_attr(message, "stop_details")
        detail = {
            "category": _block_attr(details, "category"),
            "explanation": _block_attr(details, "explanation"),
            "model": model_id,
        }
        err = ProviderRefusal(
            f"model refused (category={detail['category']!r}): {detail['explanation'] or ''}".strip()
        )
        err.detail = detail  # type: ignore[attr-defined]
        raise err

    texts: list[str] = []
    tool_calls: list[ToolCall] = []
    degraded = False
    for block in _block_attr(message, "content") or ():
        btype = _block_attr(block, "type")
        if btype == "text":
            texts.append(str(_block_attr(block, "text") or ""))
        elif btype == "tool_use":
            raw_input = _block_attr(block, "input")
            tool_calls.append(
                ToolCall(
                    id=str(_block_attr(block, "id")),
                    name=str(_block_attr(block, "name")),
                    input=dict(raw_input) if isinstance(raw_input, dict) else {},
                )
            )
        elif btype == "fallback":
            degraded = True  # served by the server-side fallback model
    raw_text = "".join(texts)

    parsed: dict[str, Any] | None = None
    if stop_reason != "max_tokens" and request.output_schema and raw_text and not tool_calls:
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and not validate(data, request.output_schema):
            parsed = data

    u = _block_attr(message, "usage")
    usage = Usage(
        input_tokens=int(_block_attr(u, "input_tokens", 0) or 0),
        output_tokens=int(_block_attr(u, "output_tokens", 0) or 0),
        cache_read_tokens=int(_block_attr(u, "cache_read_input_tokens", 0) or 0),
        cache_write_tokens=int(_block_attr(u, "cache_creation_input_tokens", 0) or 0),
    )
    return ModelResponse(
        parsed_output=parsed,
        raw_text=raw_text,
        tool_calls=tool_calls,
        usage=usage,
        cost_usd=estimate_cost(model_id, usage),
        latency_ms=latency_ms,
        model_id=model_id,
        stop_reason=stop_reason,
        degraded=degraded,
    )


def map_exception(e: BaseException) -> ProviderError:
    """Translate an SDK exception into the provider exception the runtime understands.

    Most-specific first (shared/error-codes.md → "Catch most-specific first, in a chain").
    """
    msg = redact(str(getattr(e, "message", None) or e))
    if isinstance(e, anthropic.APITimeoutError):
        return ProviderTransientError(f"timeout: {msg}")
    if isinstance(e, anthropic.APIConnectionError):
        return ProviderTransientError(f"connection error: {msg}")
    if isinstance(e, anthropic.RateLimitError):
        return ProviderTransientError(f"429 rate limited: {msg}")
    if isinstance(e, anthropic.InternalServerError):  # 5xx incl. 529 overloaded
        return ProviderTransientError(f"{e.status_code} server error: {msg}")
    if isinstance(e, anthropic.AuthenticationError):
        return ProviderError("401 authentication failed: check ANTHROPIC_API_KEY")
    if isinstance(e, anthropic.PermissionDeniedError):
        return ProviderError(f"403 permission denied: {msg}")
    if isinstance(e, anthropic.NotFoundError):
        return ProviderError(f"404 not found (model id?): {msg}")
    if isinstance(e, anthropic.BadRequestError):
        return ProviderError(f"400 bad request: {msg}")
    if isinstance(e, anthropic.APIStatusError):
        status = e.status_code
        if status in TRANSIENT_STATUSES or status >= 500:
            return ProviderTransientError(f"{status}: {msg}")
        return ProviderError(f"{status}: {msg}")
    if isinstance(e, anthropic.APIError):
        return ProviderError(f"api error: {msg}")
    return ProviderError(f"unexpected {type(e).__name__}: {msg}")


# --- the provider -------------------------------------------------------------------------------
class AnthropicProvider:
    """`ModelProvider` over the official Anthropic SDK (D-037). See the module docstring."""

    def __init__(
        self,
        cfg: LLMConfig,
        *,
        api_key: str | None = None,
        client: anthropic.Anthropic | None = None,
        enable_fallbacks: bool = False,
        max_retries: int = 0,
    ) -> None:
        self._tiers: dict[str, LLMTier] = dict(cfg.tiers)
        self._enable_fallbacks = enable_fallbacks
        self._max_retries = max_retries
        if client is None:
            if api_key is None:
                from goldtrader.secrets import get_secret

                api_key = get_secret("ANTHROPIC_API_KEY", required=False)
            # None → the SDK resolves ANTHROPIC_API_KEY / its own credential sources itself.
            client = anthropic.Anthropic(api_key=api_key, max_retries=max_retries)
        self._client: Any = client
        del api_key  # never kept on the instance

    def __repr__(self) -> str:
        tiers = {k: v.model for k, v in self._tiers.items()}
        return f"AnthropicProvider(tiers={tiers!r}, fallbacks={self._enable_fallbacks})"

    # --- ModelProvider -----------------------------------------------------------------------
    def model_for_tier(self, tier: str) -> str:
        return self._tier(tier).model

    def complete(self, request: ModelRequest) -> ModelResponse:
        tier_cfg = self._tier(request.tier)
        kwargs = build_create_kwargs(request, tier_cfg)
        client = self._client.with_options(timeout=request.timeout_s, max_retries=self._max_retries)
        messages_api = client.messages
        if self._enable_fallbacks:
            messages_api = client.beta.messages
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        started = time.monotonic()
        try:
            if should_stream(request):
                with messages_api.stream(**kwargs) as stream:
                    message = stream.get_final_message()
            else:
                message = messages_api.create(**kwargs)
        except ProviderError:
            raise
        except Exception as e:  # SDK exceptions → provider exceptions, most-specific first
            raise map_exception(e) from e
        latency_ms = int((time.monotonic() - started) * 1000)
        return _map_response(message, request, latency_ms, tier_cfg.model)

    # --- operations --------------------------------------------------------------------------
    def ping(self, tier: str = "fast") -> str:
        """Connectivity + credential check: `GET /v1/models/{id}` for the tier's model.

        Returns the model id the API reports. Used by `goldctl llm-check`.
        """
        model_id = self.model_for_tier(tier)
        try:
            info = self._client.models.retrieve(model_id)
        except Exception as e:
            raise map_exception(e) from e
        return str(_block_attr(info, "id") or model_id)

    # --- internals ---------------------------------------------------------------------------
    def _tier(self, tier: str) -> LLMTier:
        try:
            return self._tiers[tier]
        except KeyError as e:
            raise ProviderError(f"unknown tier {tier!r} (configured: {sorted(self._tiers)})") from e

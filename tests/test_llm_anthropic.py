"""Anthropic adapter (D-037, LOCAL_LLM.md §2/§5): payload shape, response mapping, error mapping.

No network: a fake client stands in for `anthropic.Anthropic`; SDK exceptions are the real classes
built on stub HTTP responses (the SDK's HTTP library is `httpx2`).
"""

from __future__ import annotations

import os
from typing import Any

import anthropic
import pytest
from anthropic.types import Message, RefusalStopDetails, TextBlock, ToolUseBlock
from anthropic.types import Usage as SdkUsage

from goldtrader.config.schema import LLMConfig, LLMTier
from goldtrader.llm import AnthropicProvider
from goldtrader.llm.anthropic_provider import (
    FALLBACK_BETA,
    build_create_kwargs,
    map_exception,
    strict_schema,
)
from goldtrader.llm.provider import (
    ModelRequest,
    ProviderError,
    ProviderRefusal,
    ProviderTransientError,
    ToolCall,
    ToolSpec,
    Usage,
    estimate_cost,
)

try:  # anthropic >= 1.x ships on httpx2; older SDKs used httpx
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx

KEY = "sk-ant-api03-SECRETSECRETSECRETSECRETSECRET0123456789"
SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["wait", "enter"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["action", "confidence"],
    "additionalProperties": False,
}
TOOL = ToolSpec(
    name="get_features",
    description="Latest features for a symbol",
    input_schema={
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "window": {"type": "object", "properties": {"bars": {"type": "integer"}}},
        },
    },
)
CFG = LLMConfig()


# --- fakes ------------------------------------------------------------------------------------
def _message(
    content: list[Any],
    *,
    stop_reason: str = "end_turn",
    model: str = "claude-sonnet-5-5",
    usage: SdkUsage | None = None,
    stop_details: RefusalStopDetails | None = None,
) -> Message:
    return Message(
        id="msg_01",
        type="message",
        role="assistant",
        model=model,
        content=content,
        stop_reason=stop_reason,  # type: ignore[arg-type]
        stop_details=stop_details,
        usage=usage or SdkUsage(input_tokens=100, output_tokens=20),
    )


def _text(s: str) -> TextBlock:
    return TextBlock(type="text", text=s)


class _Stream:
    def __init__(self, result: Any):
        self._result = result

    def __enter__(self) -> _Stream:
        if isinstance(self._result, BaseException):
            raise self._result
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def get_final_message(self) -> Any:
        return self._result


class _Messages:
    def __init__(self, owner: FakeClient):
        self.owner = owner
        self.create_calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.create_calls.append(kwargs)
        result = self.owner.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def stream(self, **kwargs: Any) -> _Stream:
        self.stream_calls.append(kwargs)
        return _Stream(self.owner.results.pop(0))


class _Models:
    def __init__(self, owner: FakeClient):
        self.owner = owner
        self.retrieved: list[str] = []

    def retrieve(self, model_id: str) -> Any:
        self.retrieved.append(model_id)
        result = self.owner.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class _Beta:
    def __init__(self, owner: FakeClient):
        self.messages = _Messages(owner)


class FakeClient:
    """The slice of `anthropic.Anthropic` the adapter touches."""

    def __init__(self, *results: Any):
        self.results: list[Any] = list(results)
        self.options: list[dict[str, Any]] = []
        self.messages = _Messages(self)
        self.beta = _Beta(self)
        self.models = _Models(self)

    def with_options(self, **kwargs: Any) -> FakeClient:
        self.options.append(kwargs)
        return self


def _request(**over: Any) -> ModelRequest:
    base: dict[str, Any] = dict(
        tier="standard",
        system="You are the strategy bot.",
        messages=[{"role": "user", "content": "features: trend"}],
        output_schema=SCHEMA,
        tools=[TOOL],
        max_tokens=2048,
        timeout_s=12.5,
        effort="low",
    )
    base.update(over)
    return ModelRequest(**base)


def _http_error(cls: type, status: int, message: str = "boom", body: Any = None) -> Exception:
    response = httpx.Response(status, request=httpx.Request("POST", "https://api.anthropic.com/x"))
    return cls(message, response=response, body=body)


def _provider(client: FakeClient, **kw: Any) -> AnthropicProvider:
    return AnthropicProvider(CFG, client=client, **kw)


# --- payload ----------------------------------------------------------------------------------
def test_payload_shape_per_tier():
    for tier, model in (
        ("fast", "claude-haiku-5-5"),
        ("standard", "claude-sonnet-5-5"),
        ("deep", "claude-opus-5-5"),
    ):
        kw = build_create_kwargs(_request(tier=tier), CFG.tiers[tier])
        assert kw["model"] == model
    kw = build_create_kwargs(_request(), CFG.tiers["standard"])
    assert kw["max_tokens"] == 2048
    assert kw["system"] == [
        {
            "type": "text",
            "text": "You are the strategy bot.",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    assert kw["messages"] == [{"role": "user", "content": "features: trend"}]
    for forbidden in ("thinking", "temperature", "top_p", "top_k", "tool_choice", "metadata"):
        assert forbidden not in kw
    assert set(kw) == {"model", "max_tokens", "system", "messages", "tools", "output_config"}


def test_payload_tools_are_strict_with_cache_control_on_last():
    second = ToolSpec(
        "get_news", "news", {"type": "object", "properties": {"q": {"type": "string"}}}
    )
    kw = build_create_kwargs(_request(tools=[TOOL, second]), CFG.tiers["standard"])
    tools = kw["tools"]
    assert [t["name"] for t in tools] == ["get_features", "get_news"]
    assert all(t["strict"] is True for t in tools)
    first = tools[0]["input_schema"]
    assert first["additionalProperties"] is False
    assert first["required"] == ["symbol", "window"]
    assert first["properties"]["window"]["additionalProperties"] is False
    assert first["properties"]["window"]["required"] == ["bars"]
    assert "cache_control" not in tools[0]
    assert tools[1]["cache_control"] == {"type": "ephemeral"}
    # the ToolSpec itself is untouched
    assert "required" not in TOOL.input_schema


def test_strict_schema_handles_arrays_of_objects():
    s = strict_schema(
        {"type": "array", "items": {"type": "object", "properties": {"a": {"type": "string"}}}}
    )
    assert s["items"]["additionalProperties"] is False and s["items"]["required"] == ["a"]


def test_payload_output_config_effort_and_format():
    kw = build_create_kwargs(_request(effort="low"), CFG.tiers["standard"])
    assert kw["output_config"] == {
        "effort": "medium",  # tier default wins over a lower request effort
        "format": {"type": "json_schema", "schema": SCHEMA},
    }
    kw = build_create_kwargs(_request(tier="fast", effort="high"), CFG.tiers["fast"])
    assert kw["output_config"]["effort"] == "high"  # a request may ask for more depth
    kw = build_create_kwargs(_request(tier="deep", effort="low"), CFG.tiers["deep"])
    assert kw["output_config"]["effort"] == "high"
    kw = build_create_kwargs(_request(output_schema={}, tools=[]), CFG.tiers["standard"])
    assert kw["output_config"] == {"effort": "medium"} and "tools" not in kw


def test_payload_passes_tool_use_and_tool_result_blocks_through():
    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "get_features", "input": {}}],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "{}", "is_error": False}
            ],
        },
    ]
    kw = build_create_kwargs(_request(messages=messages), CFG.tiers["standard"])
    assert kw["messages"] == messages


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [{"role": "system", "content": "x"}],
        [{"role": "user", "content": 42}],
        [{"role": "user", "content": []}],
        [{"role": "user", "content": [{"type": "image", "source": {}}]}],
        [{"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "f"}]}],
        [
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t", "name": "f", "input": 1}],
            }
        ],
        [{"role": "user", "content": [{"type": "tool_result", "content": "x"}]}],
        ["not a dict"],
    ],
)
def test_bad_message_shape_is_rejected_before_any_call(messages):
    client = FakeClient(_message([_text("{}")]))
    with pytest.raises(ProviderError):
        _provider(client).complete(_request(messages=messages))
    assert client.messages.create_calls == [] and client.messages.stream_calls == []


# --- complete(): happy paths -----------------------------------------------------------------
def test_complete_parses_json_text_and_passes_timeout_through_with_options():
    usage = SdkUsage(
        input_tokens=120,
        output_tokens=30,
        cache_read_input_tokens=900,
        cache_creation_input_tokens=50,
    )
    client = FakeClient(_message([_text('{"action": "wait", "confidence": 0.4}')], usage=usage))
    provider = _provider(client)
    resp = provider.complete(_request())

    assert client.options == [{"timeout": 12.5, "max_retries": 0}]
    assert len(client.messages.create_calls) == 1 and client.messages.stream_calls == []
    sent = client.messages.create_calls[0]
    assert sent["model"] == "claude-sonnet-5-5"
    assert "betas" not in sent and "fallbacks" not in sent
    assert resp.parsed_output == {"action": "wait", "confidence": 0.4}
    assert resp.raw_text == '{"action": "wait", "confidence": 0.4}'
    assert resp.tool_calls == [] and resp.stop_reason == "end_turn"
    assert resp.model_id == "claude-sonnet-5-5" and not resp.degraded
    assert resp.usage == Usage(120, 30, 900, 50)
    assert resp.cost_usd == pytest.approx(
        estimate_cost("claude-sonnet-5-5", Usage(120, 30, 900, 50))
    )
    assert resp.cost_usd == pytest.approx((120 * 2 + 900 * 0.2 + 50 * 2.5 + 30 * 10) / 1e6)
    assert resp.latency_ms >= 0


def test_complete_invalid_json_or_schema_violation_leaves_parsed_output_none():
    client = FakeClient(
        _message([_text("not json")]),
        _message([_text('{"action": "fly", "confidence": 0.4}')]),
        _message([_text('["a", "b"]')]),
    )
    provider = _provider(client)
    for expected in ("not json", '{"action": "fly", "confidence": 0.4}', '["a", "b"]'):
        resp = provider.complete(_request())
        assert resp.parsed_output is None and resp.raw_text == expected
        assert resp.stop_reason == "end_turn"


def test_complete_maps_tool_use_blocks():
    client = FakeClient(
        _message(
            [
                _text("Let me check."),
                ToolUseBlock(
                    type="tool_use", id="tu_1", name="get_features", input={"symbol": "XAU"}
                ),
                ToolUseBlock(
                    type="tool_use", id="tu_2", name="get_features", input={"symbol": "X"}
                ),
            ],
            stop_reason="tool_use",
        )
    )
    resp = _provider(client).complete(_request())
    assert resp.tool_calls == [
        ToolCall("tu_1", "get_features", {"symbol": "XAU"}),
        ToolCall("tu_2", "get_features", {"symbol": "X"}),
    ]
    assert resp.stop_reason == "tool_use" and resp.parsed_output is None
    assert resp.raw_text == "Let me check."


def test_complete_refusal_raises_provider_refusal_with_stop_details():
    client = FakeClient(
        _message(
            [],
            stop_reason="refusal",
            stop_details=RefusalStopDetails(type="refusal", category="cyber", explanation="nope"),
        )
    )
    with pytest.raises(ProviderRefusal) as info:
        _provider(client).complete(_request())
    assert info.value.detail["category"] == "cyber"
    assert info.value.detail["explanation"] == "nope"
    assert "cyber" in str(info.value) and "nope" in str(info.value)


def test_complete_max_tokens_returns_truncated_response():
    client = FakeClient(_message([_text('{"action": "wa')], stop_reason="max_tokens"))
    resp = _provider(client).complete(_request())
    assert resp.stop_reason == "max_tokens"
    assert resp.parsed_output is None and resp.raw_text == '{"action": "wa'


def test_complete_uses_streaming_for_deep_tier_and_long_outputs():
    client = FakeClient(
        _message([_text('{"action": "wait", "confidence": 0.9}')], model="claude-opus-5-5"),
        _message([_text('{"action": "wait", "confidence": 0.9}')], model="claude-sonnet-5-5"),
    )
    provider = _provider(client)
    resp = provider.complete(_request(tier="deep", timeout_s=900.0))
    assert resp.parsed_output == {"action": "wait", "confidence": 0.9}
    assert resp.model_id == "claude-opus-5-5"
    assert len(client.messages.stream_calls) == 1 and client.messages.create_calls == []
    assert client.messages.stream_calls[0]["model"] == "claude-opus-5-5"
    assert client.messages.stream_calls[0]["output_config"]["effort"] == "high"
    assert client.options[-1]["timeout"] == 900.0

    provider.complete(_request(tier="standard", max_tokens=32_000))
    assert len(client.messages.stream_calls) == 2
    assert client.messages.stream_calls[1]["max_tokens"] == 32_000


def test_streaming_errors_are_mapped_too():
    client = FakeClient(_http_error(anthropic.OverloadedError, 529, "overloaded"))
    with pytest.raises(ProviderTransientError, match="529"):
        _provider(client).complete(_request(tier="deep"))


def test_fallbacks_use_beta_namespace_and_mark_fallback_served_degraded():
    fallback_block = {
        "type": "fallback",
        "from": {"model": "claude-sonnet-5-5"},
        "to": {"model": "x"},
    }
    msg = _message([_text('{"action": "wait", "confidence": 0.1}')], model="claude-opus-4-8")
    msg = msg.model_copy(update={"content": [fallback_block, *msg.content]})
    client = FakeClient(msg)
    resp = _provider(client, enable_fallbacks=True).complete(_request())
    assert client.messages.create_calls == []
    sent = client.beta.messages.create_calls[0]
    assert sent["betas"] == [FALLBACK_BETA] and sent["fallbacks"] == "default"
    assert resp.degraded and resp.model_id == "claude-opus-4-8"
    assert resp.parsed_output == {"action": "wait", "confidence": 0.1}
    # unknown model → billed at the dearest known row, never under-counted
    assert resp.cost_usd == pytest.approx(estimate_cost("claude-opus-4-8", resp.usage))


# --- errors -------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "exc",
    [
        _http_error(anthropic.RateLimitError, 429, "rate limited"),
        _http_error(anthropic.InternalServerError, 500, "server"),
        _http_error(anthropic.InternalServerError, 503, "unavailable"),
        _http_error(anthropic.OverloadedError, 529, "overloaded"),
        _http_error(anthropic.APIStatusError, 408, "request timeout"),
        _http_error(anthropic.APIStatusError, 409, "conflict"),
        _http_error(anthropic.APIStatusError, 529, "overloaded"),
        anthropic.APITimeoutError(request=httpx.Request("POST", "https://api.anthropic.com/x")),
        anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/x")),
    ],
)
def test_transient_sdk_errors_map_to_provider_transient_error(exc):
    client = FakeClient(exc)
    with pytest.raises(ProviderTransientError):
        _provider(client).complete(_request())


@pytest.mark.parametrize(
    "exc",
    [
        _http_error(anthropic.BadRequestError, 400, "tool_choice not supported"),
        _http_error(anthropic.AuthenticationError, 401, f"invalid x-api-key {KEY}"),
        _http_error(anthropic.PermissionDeniedError, 403, "forbidden"),
        _http_error(anthropic.NotFoundError, 404, "model: claude-nope"),
        _http_error(anthropic.APIStatusError, 413, "too large"),
        _http_error(anthropic.UnprocessableEntityError, 422, "unprocessable"),
    ],
)
def test_client_errors_map_to_non_retryable_provider_error(exc):
    client = FakeClient(exc)
    with pytest.raises(ProviderError) as info:
        _provider(client).complete(_request())
    assert not isinstance(info.value, ProviderTransientError)
    assert KEY not in str(info.value)


def test_error_messages_are_redacted_of_api_keys():
    err = map_exception(_http_error(anthropic.BadRequestError, 400, f"key {KEY} rejected"))
    assert KEY not in str(err) and "***" in str(err)
    err = map_exception(RuntimeError(f"weird {KEY}"))
    assert isinstance(err, ProviderError) and KEY not in str(err)


def test_unknown_tier_is_a_provider_error():
    provider = _provider(FakeClient())
    with pytest.raises(ProviderError, match="unknown tier"):
        provider.model_for_tier("turbo")
    with pytest.raises(ProviderError, match="unknown tier"):
        provider.complete(_request(tier="turbo"))
    assert provider.model_for_tier("fast") == "claude-haiku-5-5"


def test_custom_tier_config_is_honoured():
    cfg = LLMConfig(
        tiers={"fast": LLMTier(model="claude-haiku-5-5", timeout_s=5, max_cost_usd=0.01)}
    )
    provider = AnthropicProvider(cfg, client=FakeClient())
    assert provider.model_for_tier("fast") == "claude-haiku-5-5"
    with pytest.raises(ProviderError):
        provider.model_for_tier("deep")


# --- secrets, repr, ping ------------------------------------------------------------------------
def test_repr_hides_the_key_and_the_provider_keeps_no_copy():
    provider = AnthropicProvider(CFG, api_key=KEY, client=FakeClient())
    assert KEY not in repr(provider)
    assert KEY not in str(vars(provider))
    assert "claude-sonnet-5-5" in repr(provider)


def test_real_client_is_built_with_sdk_retries_off(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    provider = AnthropicProvider(CFG)
    client = provider._client
    assert isinstance(client, anthropic.Anthropic)
    assert client.max_retries == 0
    assert KEY not in repr(provider)
    # sanity: the SDK accepts the payload types we send
    kw = build_create_kwargs(_request(), CFG.tiers["standard"])
    assert kw["output_config"]["format"]["type"] == "json_schema"


def test_ping_retrieves_the_tier_model():
    client = FakeClient({"id": "claude-haiku-5-5", "display_name": "Claude Haiku 5.5"})
    provider = _provider(client)
    assert provider.ping() == "claude-haiku-5-5"
    assert client.models.retrieved == ["claude-haiku-5-5"]
    client.results.append(_http_error(anthropic.AuthenticationError, 401, "bad key"))
    with pytest.raises(ProviderError, match="401"):
        provider.ping("standard")


def test_provider_satisfies_the_protocol():
    from goldtrader.llm.provider import ModelProvider

    assert isinstance(_provider(FakeClient()), ModelProvider)


# --- live (skipped without a key) --------------------------------------------------------------
@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="needs ANTHROPIC_API_KEY")
def test_live_haiku_tiny_schema():
    provider = AnthropicProvider(CFG)
    assert provider.ping("fast") == "claude-haiku-5-5"
    resp = provider.complete(
        ModelRequest(
            tier="fast",
            system="Answer with the JSON object only.",
            messages=[{"role": "user", "content": "Should we trade right now? Say wait."}],
            output_schema=SCHEMA,
            max_tokens=256,
            timeout_s=30.0,
        )
    )
    assert resp.stop_reason == "end_turn"
    assert resp.parsed_output is not None and resp.parsed_output["action"] in ("wait", "enter")
    assert resp.usage.input_tokens > 0 and resp.cost_usd > 0


# --- through the real SDK, no network (httpx2 MockTransport) ----------------------------------
def _sse(message: dict[str, Any], text: str) -> bytes:
    def ev(kind: str, data: dict[str, Any]) -> str:
        import json

        return f"event: {kind}\ndata: {json.dumps(data)}\n\n"

    start = {**message, "content": [], "stop_reason": None}
    return (
        ev("message_start", {"type": "message_start", "message": start})
        + ev(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        )
        + ev(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        )
        + ev("content_block_stop", {"type": "content_block_stop", "index": 0})
        + ev(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 5},
            },
        )
        + ev("message_stop", {"type": "message_stop"})
    ).encode()


def test_payload_is_accepted_and_serialized_by_the_real_sdk():
    import json

    wire: list[tuple[dict[str, str], dict[str, Any]]] = []
    text = '{"action": "wait", "confidence": 0.2}'
    body = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 7,
            "cache_creation_input_tokens": 3,
        },
    }

    def handler(req: httpx.Request) -> httpx.Response:
        sent = json.loads(req.content)
        wire.append((dict(req.headers), sent))
        if sent.get("stream"):
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=_sse(body, text)
            )
        return httpx.Response(200, json=body)

    client = anthropic.Anthropic(
        api_key=KEY,
        max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx.MockTransport(handler)),
    )
    provider = AnthropicProvider(CFG, client=client)
    resp = provider.complete(_request())
    assert resp.parsed_output == {"action": "wait", "confidence": 0.2}
    assert resp.usage == Usage(10, 5, 7, 3)
    deep = provider.complete(_request(tier="deep"))
    assert (
        deep.parsed_output == {"action": "wait", "confidence": 0.2}
        and deep.stop_reason == "end_turn"
    )
    fb = AnthropicProvider(CFG, client=client, enable_fallbacks=True).complete(_request())
    assert fb.parsed_output == {"action": "wait", "confidence": 0.2}

    plain, streamed, beta = (w[1] for w in wire)
    assert plain["model"] == "claude-sonnet-5-5" and "stream" not in plain
    assert plain["tools"][0]["strict"] is True
    assert plain["tools"][0]["input_schema"]["additionalProperties"] is False
    assert plain["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert plain["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert plain["output_config"] == {
        "effort": "medium",
        "format": {"type": "json_schema", "schema": SCHEMA},
    }
    for key in ("thinking", "temperature", "top_p", "top_k", "tool_choice"):
        assert key not in plain
    assert streamed["stream"] is True and streamed["model"] == "claude-opus-5-5"
    assert wire[0][0].get("anthropic-beta") is None
    assert wire[2][0]["anthropic-beta"] == FALLBACK_BETA and beta["fallbacks"] == "default"
    assert all(KEY not in json.dumps(w[1]) for w in wire)  # the key travels in a header only

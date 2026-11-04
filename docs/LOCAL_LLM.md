# LOCAL_LLM — model provider layer: Anthropic-first, local optional

Status: Phase 2 draft. Last updated: 2026-11-04. File name kept from the brief; scope changed by owner
decision D-019: the Anthropic API is the main brain, local models are an optional later provider.
Implements R-021 to R-025.

## 1. Provider interface (R-022)

```
ModelProvider.complete(request) -> ModelResponse
  request:  model_tier (fast|standard|deep), system, messages, tools?, output_schema (JSON schema),
            max_tokens, timeout_s, effort (low|medium|high), metadata {bot, run_id, prompt_hash}
  response: parsed_output (validated), raw_text, tool_calls, usage {in, out, cache_read, cache_write},
            cost_usd, latency_ms, model_id, stop_reason, degraded: bool
```

Providers: `AnthropicProvider` (default), `LocalOpenAICompatibleProvider` (optional; llama.cpp,
Ollama or vLLM endpoint with JSON-schema enforcement), `FakeProvider` (tests, scripted turns).
Bots never import a provider; they receive one by injection. Tier → model mapping lives in config.

## 2. Model selection (D-025, Proposed)

| Tier | Model ID | Used by | List price per 1M tokens (in / out) | Verified 2026-11-04 |
| --- | --- | --- | --- | --- |
| fast | `claude-haiku-5-5` | Research extraction, Risk-advisor, log summaries | 0.10 / 0.50 USD (≤ 100k-token prompts) | Yes |
| standard | `claude-sonnet-5-5` | Strategy, Review, Chat | 2 / 10 USD | Yes |
| deep | `claude-opus-5-5` | Weekly strategy research, hard chat questions on request | 4 / 20 USD | Yes |

Anthropic's own guidance defaults everything to Opus 5.5; the tiering above is a cost decision and
needs owner confirmation. Estimated monthly cost at the workload in RESEARCH §4.4 (**Assumption**):
tiered ≈ 20–60 USD; all-Sonnet ≈ 163 USD; all-Opus ≈ 327 USD. Prompt caching of the stable system
prompt and tool list is expected to cut input cost substantially; measured in Phase 5 (KU-021).

API behaviour the implementation must respect (from the Claude API reference, 2026-11):

- Structured output through `output_config.format` with a JSON schema (not the deprecated
  `output_format`); tools use `strict: true` with `additionalProperties: false` and `required`.
- Thinking is adaptive by default on these models; control depth with `output_config.effort`
  (`low` for extraction and chat, `medium` for Strategy and Review, `high` for weekly research).
  `budget_tokens` and `temperature` are not sent.
- Forced `tool_choice` (`any`/`tool`) is not available on Opus 5.5 and Sonnet 5.5; use `auto` plus an
  instruction, or structured outputs.
- Check `stop_reason` before reading content; handle `refusal` (with `stop_details`) and `max_tokens`
  as failures; enable the server-side fallback option where the owner agrees.
- Prompt caching: static system prompt and tool definitions first, volatile data last; verify
  `cache_read_input_tokens` > 0 in Phase 5.
- Streaming for long outputs (weekly research); non-streaming for short structured calls.
- Message Batches (50% cost) for the nightly review of the day's trades where latency does not matter.

## 3. Hardware sizing (D-018)

No GPU is required for the first release. The PC must run MT5, two Python processes and SQLite:
**Assumption** 8 GB RAM, 4 cores and an SSD are sufficient; verified by the Phase 3 soak (KU-020).
If a local provider is added later, the research sizing applies (`research/03_local_llm.md` §3):
Build A (one 24 GB GPU) covers a 9B extraction model and a 27B analysis model.

## 4. Performance targets

| Call | Timeout | Target p95 latency | Max cost per run |
| --- | --- | --- | --- |
| Research extraction (fast) | 20 s | 6 s | 0.02 USD |
| Risk-advisor (fast) | 15 s | 4 s | 0.01 USD |
| Strategy (standard) | 45 s | 15 s | 0.10 USD |
| Review post-mortem (standard) | 60 s | 20 s | 0.15 USD |
| Chat turn (standard) | 60 s | 10 s to first token | 0.20 USD |
| Weekly research (deep, streaming) | 15 min | n/a | 5 USD |

Daily budget default 3 USD; monthly 60 USD; both configurable in the owner section. Budget reached →
all bot calls stop; execution safety, alerts and the owner's kill command continue (S-7).

## 5. Fallback behaviour (R-021, R-023)

1. Timeout or 5xx/429 → one retry with backoff (SDK retries disabled; our loop controls the budget).
2. Still failing → next-lower tier with the same schema (standard → fast). Opus research jobs queue
   instead of downgrading.
3. Schema-invalid output → one repair call with the validator error → if still invalid, rule-based
   default: Research marks items `unclear`; Risk-advisor budget = code hard-rules result; Strategy
   returns `wait`; Review writes `outcome_class: unreviewed`. The record is flagged `degraded`.
4. `refusal` stop reason → counted as failure; server-side fallback if enabled; otherwise step 3.
5. Circuit breaker: 3 consecutive failures per bot → disabled; half-open trial after 30 min, doubling
   to 6 h; every transition alerts.
6. Provider fully unavailable → `strategy.fallback_mode` applies (`flat` by default: no new entries,
   existing positions managed by broker-side stops and the pre-break flatten).
7. Never: an LLM failure changing a risk limit, delaying a stop, or blocking the kill switch.

## 6. Privacy and data handling (D-019)

Account data may be sent to Anthropic by owner decision. Anthropic states API inputs are not used for
training by default (Verified, RESEARCH §4.5); retention terms are KU-017. Still never sent: MT5
password, Telegram token, API keys, Personal Area identifiers. A scrubber removes those patterns from
every prompt as defence in depth, and every outbound payload is hashed into `llm_calls`.

## 7. Evaluation (KU-007)

Three versioned eval sets, built in Phase 5 and run with promptfoo against the Anthropic API:
headline → JSON (≈ 300 labelled items), indicator table → regime label (≈ 200), trade-review rubric
(≈ 50 with a human-graded subset). Metrics: schema validity, enum agreement and macro-F1, run-to-run
agreement, p50/p95 latency, cost per call. Any prompt or model change must not regress agreement by
more than 2 points or latency by more than 25% to be promoted (gate in `LEARNING_SYSTEM.md`).

## 8. Adding a local provider later

Config `providers.local.endpoint` pointing at an OpenAI-compatible server with JSON-schema
enforcement (llama.cpp, Ollama with raised context, vLLM). Recommended models and caveats are in
`research/03_local_llm.md` (Qwen3.5-9B for fast, Qwen3.8-27B for standard; flat schemas; thinking off
for extraction; Ollama context default must be raised). The same eval sets gate the switch.

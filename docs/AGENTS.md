# AGENTS — bot roster: roles, skills, permissions, inputs, outputs, escalation

Status: Phase 2 draft. Last updated: 2026-11-04. Implements R-020 to R-025, R-040 to R-043.

## 1. Principles

1. **LLMs propose; code decides.** Every bot output is a schema-validated proposal that passes
   deterministic gates before it can influence an order. The risk engine and execution engine
   (`gateway`) have no LLM in them. (D-006)
2. **Least privilege.** Each bot has an explicit tool list. No bot has MT5 credentials, an import path to
   execution, or write access to the risk section of config. A static test enforces import isolation and
   tool allow-lists. (R-010, R-034)
3. **Untrusted input is quarantined.** Only the Research bot reads raw news text, inside delimiters, and
   it has no state-changing tools. (R-024)
4. **Every run is accounted.** Model, prompt hash, tokens, cost, latency, schema version, outcome.
   (R-025)
5. **Escalate to the owner, never around the rules.** When a bot cannot complete safely it returns a
   `needs_owner` result; nothing proceeds by default.

## 2. Roster

| Bot | Model (D-025) | Cadence | Writes | Can reach broker? |
| --- | --- | --- | --- | --- |
| Research | Haiku 5.5 (extraction), Opus 5.5 (weekly research) | News: on arrival, ≤ every 5 min; Weekly: Sunday | `news_events`, `hypotheses` | No |
| Strategy | Sonnet 5.5 | Every 5 min in session, or on trigger (new high-impact event, position state change) | `proposals` | No (via gateway intents only after code gates) |
| Risk-advisor | Haiku 5.5 | With each Strategy cycle | `risk_budgets` | No (code hard rules override) |
| Review | Sonnet 5.5 | After each closed trade; daily brief; weekly review (deterministic numbers + narrative) | `postmortems`, `lessons`, `briefs` | No |
| Chat | Sonnet 5.5 | On owner message | `chat_messages`, `chat_actions` (pending confirmation) | No (confirmed actions become intents or config changes through the same gates) |
| Execution | none (deterministic) | Continuous | `orders`, `fills`, `positions`, `trades` | Yes (only component that can) |
| Risk engine | none (deterministic) | Per request | verdicts | n/a |
| Health | none; Haiku for log summaries on request | Continuous | `alerts`, `incidents` | No |

## 3. Bot specifications

Each LLM bot is an `AgentSpec`: `name, version, model, system_prompt (registered, hashed),
tools[], output_schema, checks(), finalize(), timeout_s, max_turns, max_cost_usd`.

### 3.1 Research bot

- **Role:** turn news and calendar data into structured, gold-relevant events; weekly, generate
  strategy hypotheses for the learning pipeline.
- **Skills:** news interpretation (hybrid), macro-event classification, strategy research (sandboxed).
- **Inputs:** `news_items` (title, source, published_at, body excerpt ≤ 1,500 chars, defanged),
  `events` (release time, actual, consensus, previous), current features summary (text table).
- **Output schema (extraction):** per item `{item_id, relevance: none|low|medium|high, event_type:
  enum[fed, inflation, employment, growth, usd, geopolitical, flows, other], direction_for_gold:
  bullish|bearish|neutral|unclear, magnitude: low|medium|high, horizon: minutes|hours|days, confidence:
  0–1, summary ≤ 200 chars}`; every input id must be accounted for (`dropped` list with reasons).
- **Output schema (weekly research):** `{hypotheses: [{title, single_change: {param|rule|prompt},
  rationale ≤ 400 chars, pre_registered_metric, regime_scope}], max 10}`. Hypotheses that reference
  dated outcomes inside the evaluation window are flagged for look-ahead review by code.
- **Tools:** `search_memory` (read-only FTS over past events and lessons), `get_features` (read-only).
- **Checks in code:** magnitude and confidence clipped; `high` relevance requires an event_type other
  than `other`; items older than 24 h cannot be `high`.
- **Permissions:** read `news_items`, `events`, `features`, `lessons`; write `news_events`,
  `hypotheses`. No proposals, no intents, no config.
- **Escalation:** feed failures → health alert; > 20% items `unclear` for 3 cycles → Review bot notified
  as a prompt-quality signal.

### 3.2 Strategy bot

- **Role:** combine the active strategy bundle's deterministic signal with context and decide
  `wait | enter | exit | move_stop`.
- **Skills:** market analysis (reads code-computed features), discretion within bounds, explanation.
- **Inputs:** `features` (regime, ATR, spread regime, session state, next event and minutes to it),
  deterministic signal from the active bundle (e.g. `trend_state`, `breakout_levels`, `signal:
  long|short|flat`), `news_events` (last 6 h), open position and its P&L, last 10 trades, latest 5
  lessons, calibration feedback line, risk budget.
- **Output schema:** `{action: wait|enter|exit|move_stop, side?: long|short, stop_atr?: number,
  target_atr?: number, confidence: 0–1, thesis ≥ 20 chars, invalidation ≥ 10 chars, horizon_minutes:
  15–480, agrees_with_signal: bool}`.
- **Code gates (`gate_entry`):** action `enter` requires the deterministic signal to agree unless
  `strategy.allow_discretionary = true` (default false); `stop_atr` within configured bounds (default
  0.3–1.5); `target_atr ≥ stop_atr × min_rr` (default 1.0); confidence ≥ threshold (default 0.6);
  session OPEN and outside all buffers; spread ≤ cap; no open position; proposal age < 60 s; risk
  budget > 0. `move_stop` only toward profit (`check_stop_move`).
- **Permissions:** read as above; write `proposals`. Cannot send intents directly: the `proposals`
  module does so after gates.
- **Escalation:** 3 consecutive invalid outputs → bot disabled (breaker), alert, trading falls back to
  `strategy.fallback_mode` (default `flat`: no new entries).

### 3.3 Risk-advisor bot

- **Role:** a 0–100 risk budget from fast context; a second opinion that can only reduce.
- **Inputs:** realised vol vs regime, spread vs hourly percentile, minutes to next high-impact event,
  today's P&L vs daily limit, consecutive losses, account margin level, open incidents.
- **Output schema:** `{budget: 0–100, reasons: [≤ 3 strings ≤ 120 chars]}`.
- **Code `hard_rules()`:** force 0 on kill switch, daily/weekly loss limit hit, session not OPEN, news
  or fix window, stale data, spec change, margin level below threshold, incident open. The bot's
  number can never raise the code result.
- **Permissions:** read only; write `risk_budgets`.
- **Escalation:** breaker as above; when disabled, budget = code result only.

### 3.4 Review bot

- **Role:** post-mortem per closed trade, daily brief, weekly review narrative, hypothesis seeds.
- **Inputs:** the trade's journal (proposal, gates, risk verdict, order, fills, costs, 1 m path from
  entry − 60 min to exit + 60 min), the market snapshot, cited news events, prior lessons; for briefs:
  code-computed scorecard numbers.
- **Output schema (post-mortem):** `{outcome_class: enum[signal_wrong, timing, stopped_by_noise,
  news_shock, execution, correct_loss, correct_win, lucky_win], attribution: {signal_pnl, timing_pnl,
  slippage, costs} (numbers copied from code, not computed by the model), lessons: [{tag, text ≤ 200}]
  ≤ 3, confidence: 0–1}`.
- **Code checks:** attribution numbers must equal the code-computed values (the model copies, code
  verifies); lessons deduplicated against existing by FTS similarity.
- **Permissions:** read journal and reports; write `postmortems`, `lessons`, `briefs`.
- **Noise guard:** per-trade narratives never change parameters; only aggregated, pre-registered
  hypotheses enter the learning pipeline (RESEARCH §5.5).

### 3.5 Chat bot

- **Role:** answer the owner's free-text questions; prepare, but never execute, state changes.
- **Inputs:** owner message, conversation window, retrieval results.
- **Tools (all read-only):** `get_positions` (MT5 via gateway `/state`), `get_account`, `get_trades(n)`,
  `get_trade_detail(id)`, `get_proposals(n)`, `get_features`, `get_events(next_n)`, `get_health`,
  `get_config(section)` (risk section visible, not editable), `get_scorecard(period)`, `search_memory`,
  `get_llm_costs`. Each returns data plus a provenance string (`source table/MT5 call, as-of time`).
- **Action tools (prepare only):** `prepare_action({type: pause|resume|close_position|set_config_value|
  kill, params})` → writes `chat_actions` with status `pending_confirmation` and returns a summary. The
  Telegram layer renders Confirm / Cancel buttons; confirm within 2 minutes → the action is routed
  through the config schema (owner section excluded unless the owner uses the dedicated owner command)
  or through the gateway risk engine. The model never sees the confirmation token.
- **Output format:** text with mandatory sections when relevant: `Facts (source: …)`, `Opinion`,
  `Forecast (uncertainty)`. A code post-processor rejects answers containing account numbers or
  secrets and answers that assert facts without a provenance tag when a data tool was available.
- **Permissions:** read everything except secrets; write `chat_messages`, `chat_actions`.
- **Escalation:** questions about changing risk ceilings → the bot explains they are owner-only and
  points to the owner command; it never prepares that action.

### 3.6 Execution engine and risk engine (deterministic)

Documented in `ARCHITECTURE.md` §2.1 and `RISK_POLICY.md`. Permissions: the only component with MT5
trade rights. Escalation: any rejected order with code in {10019, 10016, 10030, 10017, 10027} alerts
the owner; three rejects in 10 minutes pause new entries.

### 3.7 Health

Deterministic checks (heartbeats, tick freshness, breaker states, budget, spec drift, disk space,
clock skew vs server time). On request from the owner, Haiku summarises the last N log lines. Writes
`alerts` and `incidents`; an open incident forces risk budget 0.

## 4. Permission matrix

| Resource | Research | Strategy | Risk-advisor | Review | Chat | Execution | Learning |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Raw news text | R | — | — | — | — | — | — |
| features, events | R | R | R | R | R | R | R |
| proposals | — | W | — | R | R | R | R |
| intents → gateway | — | via gates | — | — | via confirm | consumes | — |
| orders/fills/positions | — | R | R | R | R | W | R |
| config: strategies, sessions, bots | — | — | — | — | prepare (confirm) | R | propose (pipeline) |
| config: risk, permissions, safety | — | — | — | — | R | R | — |
| prompts | — | — | — | — | — | — | propose (pipeline) |
| MT5 credentials | — | — | — | — | — | yes | — |
| Anthropic key | provider layer only | | | | | — | provider layer |

## 5. Escalation rules (summary)

| Situation | Action |
| --- | --- |
| Bot invalid output ×3 | Breaker opens; alert; `fallback_mode` |
| Bot cost > per-run cap | Run aborted; counted as failure |
| Daily AI budget reached | All bots pause; Execution and Health continue; owner alerted |
| Proposal contradicts deterministic signal | Gated (unless discretionary allowed); logged for review |
| Chat asks for a forbidden change | Explain, do not prepare |
| Repeated order rejects | Pause entries; alert; owner resume |
| Lessons contradict each other | Review bot flags; no automatic parameter change |

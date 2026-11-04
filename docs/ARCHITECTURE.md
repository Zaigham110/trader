# ARCHITECTURE — components, data flow, deployment, failure handling

Status: Phase 2 draft. Last updated: 2026-11-04. Implements PRD requirements R-001 to R-054.

## 1. Overview

One Windows PC (D-018) runs four things:

```
+-------------------------------------------------------------------------------+
| Windows PC                                                                    |
|                                                                               |
|  MetaTrader 5 terminal (Exness, Standard account)                             |
|        ^  IPC (MetaTrader5 Python package, polling)                           |
|        |                                                                      |
|  [gateway]  Python process                                                    |
|    market data poller -> session state -> risk engine -> execution engine     |
|    reconciler, journal writer, localhost HTTP API (token)                     |
|        ^  SQLite (shared file, WAL)        ^ HTTP intents (localhost)         |
|        |                                   |                                  |
|  [brain]    Python process                                                    |
|    scheduler -> bots (Research, Strategy, Risk-advisor, Review) -> proposals  |
|    learning pipeline, Telegram chat + alerts, LLM provider layer              |
|        |                                                                      |
|        v  HTTPS                                                               |
|  Anthropic API (Haiku / Sonnet / Opus)                 Telegram Bot API       |
+-------------------------------------------------------------------------------+
```

Design rules carried from the research and the old platform:

1. **Two processes, one database.** `gateway` owns everything that touches money or the terminal.
   `brain` owns everything that thinks. The brain never imports the MT5 package or the execution
   module; a static import-chain test enforces it. (R-003, R-010)
2. **Proposals, not orders.** Bots write `proposals` rows; the gateway turns an approved proposal into
   an order only after the deterministic risk engine passes it. (D-006)
3. **Polling, not callbacks.** The MT5 Python API has no push events, so the gateway polls
   `symbol_info_tick()` at a configurable interval (default 250 ms during open sessions, 5 s when
   closed) and `positions_get()` every second. (RESEARCH §3)
4. **Fail flat.** Any ambiguity about connection, data freshness or account state blocks new entries;
   existing positions keep their broker-side stop. (G5)
5. **Everything versioned.** Config hash, prompt hash, code commit and strategy bundle id are stamped on
   every proposal, order and fill. (R-033)

## 2. Components

### 2.1 gateway (Python, Windows)

| Module | Responsibility | Deterministic |
| --- | --- | --- |
| `mt5_client` | Thin wrapper over the `MetaTrader5` package: initialize, login, symbol select, tick/rates/positions/deals reads, `order_check`/`order_send`. Retries transient retcodes, maps retcodes to typed errors, never retries 10019 (no money) or 10016 (invalid stops) blindly. | yes |
| `spec` | Reads `symbol_info()` and `account_info()` at startup and daily; stores a `symbol_specs` row; compares to the previous row; fails closed on change of contract size, volume step, digits, trade mode or filling mode. | yes |
| `session` | Computes session state from `trade_mode`, the configured Exness schedule and the DST flag; publishes `session_state` (OPEN, CLOSE_ONLY, CLOSED, PRE_BREAK_BUFFER, NEWS_WINDOW, FIX_WINDOW). | yes |
| `market` | Tick sampler (1 s bars of bid/ask/spread into `ticks_1s`, rolled to `bars_1m`), ATR and realised-vol features, spread percentiles by hour, stale-data detector. | yes |
| `risk_engine` | Pure function `check_entry(request, account, market, config) -> Approved | Rejected(code)`; `check_stop_move`; `check_close`. Ordered checks: kill switch → loss limits → session → news/fix windows → spread cap → spec sanity → position/lot caps → stop sanity → margin (via `order_calc_margin`) → size. Property-tested. | yes |
| `execution` | The only order placer. `enter()` runs `check_entry`, then `order_check`, then `order_send` with SL/TP attached; `move_stop()`, `close()`, `close_all()`, `ensure_stop()`. Deterministic `magic` number and comment carry the bundle id. Throttle: ≤ 1 request/s sustained, back off on 10024. | yes |
| `reconciler` | Every 60 s and at start: positions vs journal, stops present, pending orders, deals since last cursor; repairs or closes; raises alerts. | yes |
| `journal` | Writes `orders`, `fills`, `positions`, `trades` (closed round-trips with costs), `market_snapshots`. | yes |
| `api` | Localhost HTTP (FastAPI) with a shared token: `POST /intents` (from brain: enter/exit/move_stop/pause/kill), `GET /state`. Rejects anything not matching the schema. | yes |
| `health` | Heartbeat row every 10 s; checks terminal connected, trade allowed, tick freshness, ping. | yes |

### 2.2 brain (Python, Windows)

| Module | Responsibility | LLM involved |
| --- | --- | --- |
| `scheduler` | Serial job queue on wall-clock boundaries; jobs: features every minute, Research bot every 15 min in session, Strategy bot every 5 min, Review bot after each closed trade, daily brief, weekly research. | no |
| `features` | Deterministic market analysis: regime labels (ATR percentile, realised-vol percentile, trend state), session, spread regime, upcoming events from the calendar store. | no |
| `calendar` | BLS iCal, Fed FOMC page, optional ForexFactory JSON (KU-013) → `events` with release time in UTC; surprise = actual − consensus when available. | no |
| `news` | RSS/official feeds → `news_items`; the Research bot extracts structured events via the LLM (schema-enforced). | yes (extract) |
| `bots/*` | Research, Strategy, Risk-advisor, Review, Chat; each is an `AgentSpec` (system prompt, tools, output schema, checks, finalize). See `AGENTS.md`. | yes |
| `llm` | `ModelProvider` interface; `AnthropicProvider` default; budget, timeout, retries, circuit breaker, cost ledger, prompt registry with hashes. | — |
| `proposals` | Writes `proposals`; the Strategy bot's entry proposals go through code gates (`gate_entry`) before being sent to the gateway as intents. | no |
| `learning` | Trial ledger, backtester, offline gate (CPCV, PBO, DSR, haircut, permutation), promotion state machine, champion/challenger e-test, rollback. | partly (hypotheses) |
| `telegram` | Alerts outbox with cursors; chat handler; confirm-button state machine; owner-only middleware; redaction. | yes (chat) |
| `health` | Watches gateway heartbeat, LLM breaker states, budget; raises alerts. | no |

### 2.3 Storage (SQLite, WAL; D-024)

One database file per environment (`demo.sqlite`, `live.sqlite`) plus `research.sqlite` for backtests.
Key tables (full schema in Phase 3 migrations):

`symbol_specs`, `account_snapshots`, `ticks_1s`, `bars_1m`, `events`, `news_items`, `news_events`,
`features`, `proposals`, `intents`, `orders`, `fills`, `positions`, `trades`, `market_snapshots`,
`agent_runs`, `llm_calls`, `prompts`, `bundles`, `trial_ledger`, `experiments`, `promotions`,
`monitors`, `alerts`, `chat_messages`, `chat_actions`, `config_versions`, `heartbeats`, `incidents`.

Embeddings for chat memory in `memories` with sqlite-vec (Anthropic has no embeddings endpoint; a small
local embedding model or deterministic full-text search is used; default: SQLite FTS5, embeddings optional).

### 2.4 External

- **Exness MT5 server** via the terminal (UTC+0, schedule shifts with US DST).
- **Anthropic API** (`claude-haiku-5-5`, `claude-sonnet-5-5`, `claude-opus-5-5`; D-025).
- **Telegram Bot API**.
- **Calendars**: bls.gov iCal, federalreserve.gov, optional ForexFactory JSON.

## 3. Data flow

### 3.1 Decision cycle (every 5 minutes in session, or on trigger)

1. `gateway.market` has fresh 1 s ticks and 1 m bars; `brain.features` computes regime, ATR, spread
   regime, next event and session flags → `features` row (deterministic).
2. Research bot (LLM, Haiku) has already turned new news into `news_events` with a flat schema
   (event type, direction, magnitude, confidence, affected horizon). Code clips values to allowed ranges.
3. Strategy bot (LLM, Sonnet) receives: features, active strategy bundle's deterministic signal (e.g.
   trend state and breakout levels computed in code), news events, open position, recent trades, lessons.
   It returns a `proposal`: `wait | enter(side, stop_distance_atr, target_distance_atr, confidence,
   thesis, invalidation) | exit | move_stop`. The deterministic strategy signal is the primary input;
   the LLM may veto or size down, but cannot enter against the code signal unless the strategy config
   allows discretionary entries (default: not allowed).
4. `gate_entry` (code): confidence ≥ threshold, stop within [min, max] ATR multiples, session OPEN,
   not inside buffers, spread below cap, no open position, proposal fresh (< 60 s). Fails → `gated`.
5. Risk-advisor bot (LLM, Haiku, optional) returns a 0–100 risk budget; `hard_rules()` in code force
   0 on kill switch, loss limits, news window, stale data. Budget scales size (which at 0.01-lot minimum
   means allow or block).
6. Passing proposal → `intent` → `POST /intents` to the gateway.
7. Gateway `execution.enter()`: `risk_engine.check_entry` → `order_calc_margin` → `order_check` →
   `order_send` with SL and TP → journal. Rejections are written back to the proposal with the code.
8. Fill → Telegram alert with risk % of equity, stop, target, bundle id, and a provenance line.

### 3.2 Exit paths

Broker-side SL/TP; Strategy bot `exit`/`move_stop` proposals (through `check_stop_move`: stops move
only toward profit); pre-break flattening by the gateway at the configured buffer; kill switch; owner
confirmed command from chat.

### 3.3 Post-trade

Closed deal → `trades` row with costs (spread paid, slippage vs mid, swap = 0, commission = 0) →
Review bot (Sonnet) writes a structured post-mortem and ≤ 3 tagged lessons → lessons feed the Strategy
prompt (latest 5) and the learning pipeline's hypothesis queue.

### 3.4 Learning (see `LEARNING_SYSTEM.md`)

Weekly: Research bot (Opus) proposes ≤ 10 single-change hypotheses → trial ledger → offline gate →
`experiments` with status → demo/shadow/limited-live stages → promotion or rollback. Nothing in this
loop can write to the risk section of config.

## 4. Deployment

- **Install**: Python 3.12 virtual environment; `MetaTrader5` package; MT5 terminal installed and
  logged in once by the owner with "Allow algorithmic trading" on.
- **Run**: two Windows Scheduled Tasks (or NSSM services) `gold-gateway` and `gold-brain`, restart on
  failure with backoff; PID lock files prevent duplicates. A `goldctl` CLI wraps start/stop/status/
  health/scorecard/backup.
- **Environments**: `demo` and `live` are separate config profiles, database files and MT5 logins;
  live needs `mode: live` in the owner section plus an `ALLOW_LIVE` phrase in the secrets file.
- **Secrets**: Windows Credential Manager via `keyring`; fallback encrypted `.secrets` outside the repo.
  MT5 password, Anthropic key, Telegram token, gateway token. Redaction filter on every logger.
- **Backups**: nightly SQLite backup copy with 14-day retention; config versions in the database.
- **Updates**: `goldctl deploy <ref>`: pause → wait flat or confirm → checkout → tests → restart →
  health → resume; unhealthy → auto-rollback to the previous commit (pattern from the old platform).

## 5. Failure handling

| Failure | Detection | Response |
| --- | --- | --- |
| Terminal disconnected | `terminal_info().connected == False`, retcode 10031 | Block entries; keep positions (broker holds SL); alert after 60 s; reconnect loop; kill switch if a position has no stop and connection is down > 5 min (configurable) |
| AutoTrading disabled | `terminal_info().trade_allowed == False`, retcode 10027/10026 | Block entries; alert; owner must re-enable |
| Stale ticks | No tick for > N s while session OPEN | Block entries; alert; treat as CLOSED for risk |
| Spec change | `symbol_specs` diff on contract size, step, digits, trade mode | Fail closed; alert; owner confirms new spec |
| Order rejected | retcodes 10004/10006/10013–10021/10030 | Map to typed error; no blind retry; proposal marked rejected with code; alert on repeated rejects |
| Partial fill (10010) | result.volume < requested | Journal actual volume; ensure stop on actual volume |
| Stop missing | reconciler | Re-create SL; if impossible, close position; alert |
| LLM timeout/invalid | provider layer | Retry once → smaller model → rule-based default flagged `degraded`; never blocks execution safety |
| LLM refusal / API outage | stop_reason refusal, 5xx, 429 | Server-side fallback where available; circuit breaker; trading continues on deterministic strategy signal only if config allows, else no new entries |
| Budget exceeded | cost ledger | AI calls stop; alerts continue; existing positions managed by code |
| Gateway down | brain sees no heartbeat 30 s | Brain alerts; no intents sent; on restart gateway reconciles |
| Brain down | gateway sees no brain heartbeat | Gateway keeps managing stops and pre-break flatten; alerts via its own minimal Telegram sender |
| PC reboot / power loss | exit log missing | On start: reconcile, report the gap to the owner, resume only if session OPEN and health green |
| DST schedule shift | config `dst_mode` auto from date | Session windows recomputed; alert the owner the week before |

## 6. Security

- Credentials never in config or logs (R-053); a test greps logs from a full test run for secrets.
- Brain has no MT5 credentials and no import path to execution (static test).
- Gateway HTTP API binds 127.0.0.1 only, requires a token, validates every intent against a schema.
- Telegram: owner chat ID only; every state-changing command confirms with a button; confirmation
  tokens expire after 2 minutes.
- Untrusted text (news) is wrapped in delimiters and stripped of markup before any prompt (R-024).

## 7. Reuse map from the old platform

Patterns ported with tests: agent runtime loop, risk-engine ordering and property test, config
schema + ceilings + watcher + hash, version tagging, reconcile loop, approval state machine,
supervisor/lock/exit-log, deploy with health and auto-rollback, scorecard structure. Code is rewritten
in Python (D-004). See `research/05_existing_platform_audit.md` for the per-component table.

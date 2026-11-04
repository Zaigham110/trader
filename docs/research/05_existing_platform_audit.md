# Reuse audit: `binance-ai-trader` ("TradeBrain") → new XAUUSD / Exness MT5 / local-LLM product

Source: `/home/user/binance-ai-trader` (read-only). 193 TypeScript files under `src/`, 25,480 lines total
(16,229 production, 8,890 tests, ~360 test doubles). Single squashed commit dated 2026-11-05. Node 22 (Dockerfile pins
`22.21.1`), TypeScript 7, ESM, `tsx` runtime (no build step). Everything below was read from the code unless marked
**Inferred**.

---

## 1. Architecture

**Processes** (each a `tsx` entrypoint, each takes a PID lock in `state/locks/`, `src/process/lock.ts` 58 lines):

| Process | Entry | What it does | Exchange keys? |
|---|---|---|---|
| collector | `src/market/collector.ts` (237) | ccxt.pro WebSocket candles 1m/5m/15m + mark/funding + 1d/1h sync + positioning (OI, taker ratio, L/S) → Postgres | no (public) |
| brain | `src/brain/main.ts` (124) + `supervisor.ts` (70) | Supervisor: spawns agents, paper, telegram, api as children with IPC; restart with backoff 1 s→60 s; every exit path logged (`exitLog.ts`) | strips `BINANCE_*` from children except paper |
| agents | `src/agents/main.ts` (179) | Scheduler (serial queue, wall-clock intervals, `dailyAt` in a tz) running the LLM agents; writes `agent_reports`, `decisions` | no (deleted from env at startup, line 28) |
| paper | `src/paper/main.ts` (138) + `ledger.ts` (224) | Plain code: executes `pending` decisions on the Binance futures **demo** account through `Trader.enterExternal` → `OrderExecutor` → `checkEntry`; books closes, baselines, snapshots | yes (demo only; `guard.ts` refuses live / non-demo host) |
| trader | `src/trader/main.ts` (184) + `engine.ts` (729) | Rule-based strategy loop (all strategies disabled; refuses to run alongside brain/paper) | yes |
| telegram | `src/notify/main.ts` (93) | Outbox poller every 10 s + grammy bot (owner chat only) | no |
| api | `src/api/main.ts` (51) + `server.ts` (633) | Fastify on 127.0.0.1, bcrypt + signed cookie auth, 21 GET / 6 POST / 1 PUT routes, `/ws` 2 s DB-poll push (`live.ts` 111) | no (controls go through `control_requests` table) |
| ops | `src/ops/*` (deploy 189, awsDeploy 142, health 87, soak 148, ctl 93) | deploy/rollback state machine, health checks, 7-day soak verdict | read-only exchange |

**Communication**: entirely through Postgres tables (no message bus). Agents write `agent_reports`/`decisions`; paper
ledger polls `decisions` every 5 s; telegram polls `notifications` outbox with per-source cursors in `notifier_state`;
API polls tables every 2 s for `/ws`. Dashboard controls are queued as `control_requests` rows and executed by the paper
ledger (`src/paper/controls.ts` 67). Brain ↔ parts: IPC channel only used as a liveness signal (`src/process/supervised.ts` 17:
a part SIGTERMs itself when the channel closes).

**DB**: `docker-compose.yml` → `timescale/timescaledb-ha:pg16` (TimescaleDB + pgvector). 13 forward-only migrations in
`db/migrations/` (`src/db/migrate.ts` 42). Hypertables: `candles`, `equity_snapshots`. **pgvector is declared but
unused**: `agent_notes.embedding vector(1024)` exists in `001_init.sql:102` and no TypeScript references `agent_notes`
or any embedding. Memory search is Postgres full-text (`tsvector` generated columns on `agent_reports`, `decisions`,
`postmortems`, `lessons`; `websearch_to_tsquery`, `src/agents/store.ts:129-150`). DB client is `porsager/postgres`
tagged templates (`src/db/client.ts` 25), no ORM.

**Config system** (`src/config/`, 749 lines incl. 238 test):
- `config/trading.yaml` → `loader.ts` (43): YAML → zod `configSchema` (`schema.ts` 286, `.strict()` everywhere, `superRefine` for cross-field rules) → `deepFreeze`.
- `ceilings.ts` (11 lines): `maxLeverage 20, riskPerTradePct 2, daily/weekly/monthly loss 10/20/30, maxDrawdownPct 30`; the schema uses `.max(CEILINGS.x)` so over-ceiling values fail to load.
- `watcher.ts` (50): `fs.watch` on the directory, 200 ms debounce, `diffConfig` (`diff.ts` 32, leaf-level diff) → `configEvents.emit('change')`; invalid file → old config stays, error logged (`index.ts:31-35`, with a note about a 2026-10-31 outage caused by an unhandled `error` emit).
- `getConfig()` is called at use time, never cached (CLAUDE.md rule).
- Dashboard `PUT /api/config` runs the same schema + ceilings, cannot change `mode`, `approval.live`, `approval.liveChiefOfStaff`.
- Live mode needs both `mode: live` and `ALLOW_LIVE=I_UNDERSTAND_THE_RISK` (`index.ts:66`); autonomous AI approval in live needs `ALLOW_AUTONOMOUS_LIVE=I_ACCEPT_AI_APPROVAL` (`schema.ts:256`).

**Versioning** (`src/version/`): `configHash.ts` (16) = sha256 of the parsed config via a key-sorted stable JSON; `version.ts` (79) = one `run_versions` row per (config version, `promptsHash`, git commit, dirty flag). `promptsHash` (`src/agents/prompts.ts` 34) hashes every agent system prompt as built for the current config. `decisions`, `approvals`, `paper_trades` carry `version_id`; tagging never blocks a write (`versionIdOrNull`). Scorecard groups by version.

---

## 2. LLM layer

Files: `model.ts` (49), `prompts.ts` (34), `tools.ts` (44), `runtime.ts` (270), test double `src/test/fakeModel.ts` (69).

**Provider support: Anthropic only.** `model.ts` imports `@anthropic-ai/sdk` and the types leak through the whole layer:

```ts
// src/agents/model.ts:7-13
export type MessageParams = Anthropic.Beta.MessageCreateParamsNonStreaming;
export type Message = Anthropic.Beta.BetaMessage;
export type Usage = Anthropic.Beta.BetaUsage;

export interface ModelClient {
  create(params: MessageParams, opts: { signal: AbortSignal }): Promise<Message>;
}
```

The interface is a seam for tests, **not provider-agnostic**: `runtime.ts` builds `Anthropic.Beta.BetaTool[]`, reads
`res.content` as `BetaToolUseBlock[]`, checks `stop_reason` values `refusal` / `max_tokens` / `pause_turn`, passes
`output_config: { effort }` and `cache_control: { type: 'ephemeral' }` on the system block, and `anthropicModel()`
sets a beta header for server-side fallback. `tools.ts:27-34` converts zod → JSON Schema via `z.toJSONSchema` into
`BetaTool.input_schema`. `PRICES` (`model.ts:32-37`) is a hard-coded Anthropic price table; unknown models are billed at
the most expensive row. No OpenAI, Ollama, llama.cpp, vLLM or generic OpenAI-compatible path exists anywhere in `src/`.

**Agent spec** (what each agent supplies; `runtime.ts:49-64`):

```ts
export interface AgentSpec<O, D = O> {
  name: string; kind: string; schemaVersion: number;
  system: string; prompt: string;
  tools: AgentTool[];
  output: z.ZodType<O>;
  check?: (o: O) => string[];                 // semantic checks the schema can't express
  finalize: (o: O) => { data: D; summary: string; symbols: string[] };
  timeoutMs: number; maxTurns: number; effort: 'low' | 'medium' | 'high';
}
```

**Loop semantics** (`runAgent`, `runtime.ts:90-229`), all of which are worth keeping conceptually:
- Structured output = a forced final tool call `submit_report` whose `input_schema` is the agent's zod output schema; a plain-text answer gets one nudge ("Call the submit_report tool now").
- Invalid output: zod issues (and `check()` problems) are returned as an `is_error` tool result; after `MAX_INVALID_SUBMITS = 2` extra attempts the run fails with status `invalid`.
- Tool inputs zod-validated before running; results JSON-stringified and truncated at 20,000 chars; unknown tool → error result (model keeps going).
- Timeout: `AbortController` + `setTimeout(spec.timeoutMs)`; every model call and tool call is raced against the deadline (a slow tool counts, tested).
- Transient errors (connection, 408/409/429/5xx, `ECONNRESET`…) retried 3× with 2/4/8 s backoff capped by `retry-after`, inside the timeout; SDK retries disabled.
- Budget: `store.spentTodayUsd()` checked before every call; a `budget` stop is not a failure.
- Circuit breaker: 3 consecutive failures → disabled; half-open trial after 30 min, doubling to 6 h max; every transition an event (→ Telegram).
- Cost accounting per run (`agent_runs`: tokens, cache tokens, cost, turns, duration).
- `refusal` and `max_tokens` (8,000) stop reasons are hard failures.

Test double `FakeModel` scripts turns; `FakeAgentStore` in-memory. 148 agent tests run with no network.

---

## 3. Agent roster (`src/agents/`)

| File (lines) | Role | Inputs | Output / sink | LLM? |
|---|---|---|---|---|
| `marketAnalyst.ts` (155) | Regime + technical picture per coin (10 coins) | code-computed digest (`marketData.ts` 129: 15m/1h/4h/1d indicators, funding, positioning), regime score; tools `getCandles`, `getIndicators`, `getFunding`, `searchMemory` | `market_report` (zod: per-coin regime/trend/vol/keyLevels/funding/confidence/note ≤200 chars); code re-sides and sorts key levels | **LLM**, maxTurns 8 |
| `newsReader.ts` (204) + `news/feeds.ts` (177) | Turn RSS (CoinDesk, Cointelegraph, The Block, Decrypt) + Binance announcements into structured events | `news_items` wrapped in `<untrusted_news>` with `<>` defanged; explicit injection instructions; importance rubric per event type | `news_report`: events (coins, eventType, affectsVenue, sentiment, importance 1-5, summary ≤200) + `dropped` with reasons; every input id must be accounted for; importance clamped to the rubric in code | **LLM**, maxTurns 4 |
| `fastRisk.ts` (145) | Fast-risk inputs | BTC/ETH moves, liquidation spike vs 7-day avg, high-importance events, latest account snapshot, 30-day correlations | plain object for the Risk Officer | deterministic |
| `riskOfficer.ts` (153) | Risk budget 0-100 % + blocked coins | fastRisk inputs + market-wide crowding (`positioning.ts` `marketCrowding`) + `searchMemory` | `risk_report`; **`hardRules()` in code overrides to 0** on venue incident (live), kill switch, loss limit (`finalBudget`, line 87) | **LLM** proposes, code caps; maxTurns 5 |
| `positioning.ts` (148) | Crowding features | `positioning` table (OI, taker ratio, global/top L/S) vs the coin's own 30 days | `crowded_long / crowded_short / flush / neutral / unknown` | deterministic |
| `regime/*` (components 142, score 79, history 68) | TS port of `crypto-regime-analyzer`: 6 weighted components → 0-100 → RISK_ON/NEUTRAL/RISK_OFF, fail-closed to UNKNOWN; `regimeHistory` point-in-time for backtests | daily closes, dominance, funding | composite | deterministic |
| `timeMachine.ts` (227) | Odds for one proposed trade | 2,160 h of 1h candles; 10,000 seeded block-bootstrap replays, volatility-matched (0.67-1.5× today's 24 h vol), plus historical analogs (EMA50 side, RSI ±7.5, ATR% band) | `pTarget/pStop/pNeither/expectedR` after fees; verdict | deterministic numbers; a small LLM "explainer" spec (`TM_SYSTEM`, maxTurns 3) only narrates |
| `portfolioManager.ts` (318) | Decide enter(long/short)/wait/exit/move_stop | Risk report, account, prices, Market report, positioning, calibration feedback, structured news events, recent decisions, Auditor lessons; tools `timeMachine`, `searchMemory` | `pm_decision` (≤4 actions; `enter` needs thesis ≥20 chars, invalidation text, confidence 0-1, horizon 4-72 h, sizePct 10-100); then **`gateEntry()` in code** (budget>0, not blocked, ≤3 positions, stop side/distance ≤8 %, target side, TM E[R] > 0.2, in universe, inputs fresh) and `counterRegimeReasons` size cut | **LLM** + code gates, maxTurns 10 |
| `consensus.ts` (64) | N PM samples → majority vote | `samples` (config 3) PM outputs | entry needs majority of *requested* samples on same coin+side (median stop, confidence × agreement); exits/stop moves need majority of *completed* | deterministic |
| `challenger.ts` (87) | Shadow-only PM variant (different samples/mult/model) on the same inputs | same `PmContext` + its own shadow book | `challenger_decisions` → `shadow_trades source challenger:<name>`; can never be executed | **LLM** (reuses `pmSpec`) |
| `cycle.ts` (138) | One decision cycle: Risk Officer → PM×N → consensus → gates → TM explainer → `decisions` rows → approval → challenger | — | `decisions` statuses `wait / gated / awaiting_approval / pending / shadow` | orchestration |
| `approvalReview.ts` (109) | "Approver" agent reviewing one gated entry for the Chief of Staff | decision, gate, cited report summaries, past trades in the coin, lessons; tool `searchMemory` only | `approve / reject + reason + concerns`; escalation rules in code first (`src/approval/policy.ts` 58); approve → veto window with Telegram Cancel | **LLM**, maxTurns 5 |
| `chiefOfStaff.ts` (83) | Daily brief 08:30 Asia/Karachi | everything computed in code (`briefInputs`): account, decisions, risk, news, scorecard, AI cost, open breakers | `daily_brief` (6 sections ≤500 chars, ≤3 questions for the owner) | **LLM**, maxTurns 5 |
| `weeklyReview.ts` (156) | Monday review | DB only | `weekly_review` report text | **deterministic** (explicitly no model so the forward test's prompts don't change) |
| `auditor.ts` (97) | Post-mortem per closed paper trade | trade, decision, cited reports, 1h price path to +24 h, baselines | `postmortems` + 1-3 tagged `lessons` (fed back to the PM prompt and `searchMemory`) | **LLM**, maxTurns 5 |
| `scheduler.ts` (91) | Serial job queue; `everyMinutes` on wall-clock boundaries, `dailyAt` in an IANA tz; `trigger()` on importance ≥4 news | — | — | deterministic |
| `store.ts` (150) | `PgAgentStore`: runs, breaker state, reports, events, full-text `searchMemory` (300-char snippets; weekly reviews excluded) | — | — | — |

Config also declares an `agents.researcher` block (`schema.ts:180`), but **no researcher agent exists in code** (only the schema references it).

---

## 4. Risk engine (`src/risk/`, 808 lines + 627 test)

`riskEngine.ts` (240) `checkEntry(req, acct, ctx, config): Approved | Rejected` is pure (no I/O, config passed in). Order of checks (lines 113-232): kill switch → loss limits (daily/weekly/monthly, drawdown fires the kill switch) → paused → market enabled / shorts allowed → prices valid, stop on the losing side, stop distance ≤ `min(req.maxStopDistancePct ?? risk.maxStopDistancePct, 50)`, TP side → stale data → regime multiplier / `blockLongsInRiskOff` → trades/day, cooldowns (`guards.ts` 104: after loss, after N stops in a row 60 min), max open positions, per-symbol → funding filter, leverage clamp to config and exchange max (`effectiveLeverage`), liquidation buffer (`sizing.ts` `estimateLiquidationPrice`, `stopPctOfWayToLiquidation`) → fee-aware min edge → `sizePosition` (risk-based qty, clamped by margin %, total exposure × regime mult, free balance, exchange `qtyStep/minQty/maxQty/minNotional`). 29 `RejectCode`s. `checkStopMove` (233): stops only move toward profit and must stay on the losing side of the mark. `sizing.ts` (183) also has `realizedVolPct` / `volTargetNotional` (vol-target sizing, ≤1.5× leverage).

**"LLM cannot bypass" enforcement (all tested):**
1. Agents never import exchange/execution/trader/paper/ccxt through any import chain, never read keys, and every tool is on `ALLOWED_TOOLS` (`isolation.test.ts` 137, 6 tests; `safety.test.ts` 40 greps `src/` for `createOrder`… outside `orders.ts` and bans the words withdraw/transfer().
2. LLM output passes zod, then `gateEntry()` in code, then approval, then `release()` is the only path to `pending` (`approval/store.ts`), then `OrderExecutor.enter()` calls `checkEntry()` itself (`orders.test.ts`: "places no order of any kind when the risk engine rejects").
3. Risk Officer `hardRules()` zero the budget regardless of the model's number.
4. Config ceilings in zod; dashboard can't change `mode` / live approval settings; `.env` phrases for live.
5. Every filled entry gets an exchange reduce-only `STOP_MARKET`; failed stop → immediate close + alert; reconcile every 60 s re-creates missing stops (`trader/engine.ts`, `ops/health.ts` `positionsWithoutStop`).

**Property test** `riskEngine.property.test.ts` (155): 20,000 seeded random configs/accounts/orders — no accepted order can lose more than `riskPerTradePct × equity (× regime mult) + fees` at its stop, all limits hold; second test: once the drawdown kill switch fires every order is rejected.

---

## 5. Backtest engine (`src/backtest/`, 2,957 lines + 1,044 test)

- `engine.ts` (480) `runBacktest`: multi-symbol portfolio, spot + futures, long + short, **same `checkEntry`**, closed candles only (look-ahead tested), trailing stops, max hold, liquidation, kill switch, funding every 8 h, optional post-only entry model (47 % no-fill, measured on demo).
- `fills.ts` (104): taker fee + `slippagePct` on every fill incl. stops; stop fills at the worse of stop and open on a gap; stop before TP in one candle; `fundingCashflow`, `gapsThroughLiquidation`.
- `bookSlippage.ts` (26): walk an order-book snapshot to price a market order of a notional (built for gold D3).
- `carry.ts` (270): delta-neutral spot+short-perp funding carry, backtest-only.
- `walkForward.ts` (56): k folds, pick the best param multiplier in-sample, score out-of-sample.
- `regimes.ts` (112): 30-day windows bull/bear/sideways; `volScaledThreshold` (k × sd of window returns — built for gold D2).
- `metrics.ts` (127): trade stats, `periodStats` (CAGR, Sharpe, worst month, yearly), `holdCurve` benchmarks.
- `evaluate.ts` (181): TS port of `backtest-expert` 5×20-point scorer, red flags, DEPLOY/REFINE/ABANDON.
- `validate.ts` (241): gates `min_trades (300), min_years (2), bull_and_bear, net_profit, slippage_stress (×2), plateau (±25/±50 % params), walk_forward (OOS ≥ 0.5 × IS), backtest_expert (≥70)` → `canEnable` only when every gate passes.
- `dailyData.ts` (94): spot history spliced before a perp listed, assumed funding before that; `data.ts` (103) CSV cache of public Binance REST.
- `lotEngine.ts` (292): daily lot strategies (trend ensemble, TSMOM) with vol-target sizing.

**Gold specifically** (`docs/17_GOLD_RESEARCH.md` 139 lines, `src/backtest/researchGold.ts` 152, `config/research-gold.yaml`, `src/exchange/goldMarkets.ts` 49, `checkGold.ts` 49):
- Gold was attempted **on Binance via PAXG** (tokenized gold), pre-registered 2026-11-01, run once by the owner the same day. Setup: PAXG/USDT:USDT daily perp candles with PAXG spot spliced before 2025-03-27, real funding from listing, assumed +0.01 %/8 h before, $20k equity, slippage = max(0.02 %, measured book cost of a $5k order over 30 snapshots).
- `checkGold.ts` listed what Binance offers: PAXG perp (2025-03-27), **XAU/USDT TradFi perpetual** (2025-12-11, $161M/day mainnet, also on the demo venue), XAUT perp (2026-03-26). `goldMarkets.ts` classifies `TRADIFI_PERPETUAL`; `universe.isEligible()` refuses TradFi perps and PAXG/XAUT are in `universe.dynamic.exclude`.
- Results (docs/17, table): buy & hold PAXG +115 % / Sharpe 0.93 / maxDD 29.7 %; **G1 trend ensemble +4.2 % / Sharpe 0.38 / maxDD 2.9 %, 105 trades, scorer 56 < 70 → `canEnable: false`**; G2 TSMOM 5 trades, informational. Conclusion: "gold is not traded by a rule-based strategy"; profit came only from 8 bull windows (mostly 2025), 48 sideways windows lost; assumed funding ate 40 % of the pre-funding profit; walk-forward picked 4 different param sets (unstable). The XAU perp step was **never started** (conditional on a PAXG pass). Docs flag gold-market-hours issues for XAU: stops can't fill while closed, reopen gaps, needs a market-hours design.
- The JSON reports (`docs/research/research-gold/`) are **not in the repo** (written on the owner's Mac); only the summary in docs/17 survives. No `reports/` directory exists at the repo root.
- No code models gold sessions, weekend gaps, points/pips/lots, contract size, or swap (overnight) charges; everything is 24/7 crypto with 8-hourly funding.

---

## 6. Learning / approval / promotion loop

**How a "strategy change" is proposed, tested, promoted, rolled back:**
- There is **no automated self-learning that changes behaviour**. The only feedback loops into the LLM are: (a) Auditor `lessons` (last 5 in the PM prompt + `searchMemory`), (b) `calibrationFeedback` text (Brier score, overconfidence; `src/report/calibration.ts` 110) injected into the PM prompt once ≥5 closed trades. Neither changes prompts, config, gates or sizes.
- Prompts are string constants in each agent file; "versioning" = `promptsHash` in `run_versions` so results can be grouped by prompt version (`docs/16`). No prompt registry, no A/B of prompts beyond the `challenger` (one alternative PM config run in shadow), no prompt history table.
- Promotion is manual and documented in `docs/16_CHANGE_PROCEDURE.md`: run in `mode: shadow` first → compare scorecard "Shadow vs paper vs baselines" and "By version" → owner edits config / deploys → `scripts/deploy.sh` (pause → wait in-flight → checkout/migrate/test → restart → health → resume, auto-rollback on failed health, `src/ops/deploy.ts` 189, 202-line test).
- `src/paper/approvalE2e.ts` (163): a scripted end-to-end check of the approval path on demo with a labelled test entry.
- `src/report/scorecard.ts` (389): paper vs baselines (do-nothing, `btc_hold`, `random` same side/stop/size — `src/paper/baselines.ts` 20), hit rates, AI cost, "what rejected entries would have made" via `report/shadow.ts` `simulateLong`, calibration, `pairedEdge` bootstrap vs random (`calibration.ts:81`), go-live gate (`goLiveGate`: ≥8 weeks, ≥30 trades, net > 0 and > random, maxDD ≤10 %, no safety violations, AI cost <20 % gross, shorts net ≥0).
- `src/shadow/ledger.ts` (207) `tickShadow`: simulates shadow PM / strategy / challenger entries on 15m candles with the backtester's fill model + recorded funding (`shadow_trades`). `shadow/mode.ts` (15) `effectiveMode`: paper config in live mode degrades to shadow.
- Rollback of *decisions*: none needed (shadow never executes); rollback of *code/config*: `scripts/rollback.sh` → `deploy()` to the previous commit; config history in `config_versions` with diffs.

---

## 7. Chat / notify (`src/notify/`, 547 lines + 157 test)

- `telegram.ts` (72): grammy bot; middleware drops every update not from `TELEGRAM_OWNER_CHAT_ID`; token redaction in logs; 4,000-char chunking; inline-keyboard buttons.
- `commands.ts` (93): **fixed slash commands only** — `/status /brief /week /scorecard /pause /resume /help`. No free-text chat, no LLM behind commands. `/pause` sets `bot_state.paused` which `checkEntry` honours.
- Buttons: `apr:<id>:approve|reject|cancel` (`approval/policy.ts` `parseButton`) → `ownerAction()` with conditional UPDATEs so timers and presses can't race (`approval/store.ts` 174).
- Confirmation flow for setting changes: **none via Telegram** (no command changes config). Settings change only through the dashboard `PUT /api/config` (password re-entry, same zod + ceilings, atomic write) or the YAML file. Kill-switch reset / manual close / pause also need the password in the API (`api/auth.ts` 63: 5 attempts / 15 min).
- `alerts.ts` (289): DB tables → outbox with cursors, quiet hours (critical kinds bypass), 8 send attempts; texts built from structured fields only, never raw news.

---

## 8. Ops

- `src/ops/deploy.ts` (189): dependency-injected deploy state machine (git / run / ctl / service seams), tested with fakes; `awsDeploy.ts` (142) maps the same steps onto SSM Run Command + Docker image staging; `deployCli.ts`, `deployAwsCli.ts`, `scripts/deploy*.sh`, `rollback.sh`, `./tb` wrapper (5,348 bytes) for the owner.
- `health.ts` (87): pure checks — every part holds its lock since the restart, brain reports the expected commit, paper ledger reconciled since restart, every position has an exchange stop; `inFlightProblems` (pending decisions, queued controls, open entry orders).
- `soak.ts` (148): 7-day pass/fail from the DB (snapshot gap ≤15 min, no unprotected position in 2+ consecutive snapshots, no safety alerts, ≤3 restarts, ≤0.1 % missing candles, ≥80 % cycles ran, no open breaker).
- `process/lock.ts` (58): PID lock files with liveness check and stale takeover, conflict sets (trader ⟂ brain/paper); `process/supervised.ts` (17).
- `brain/supervisor.ts` (70): child restart with exponential backoff, healthy-reset after 10 min, SIGTERM then SIGKILL after 15 s; `exitLog.ts` (51) records every exit path and detects unlogged exits (kill -9 / power loss) at next start.
- `scripts/watchdog.sh`: host-level bash/docker watchdog (containers, stale 1m candles > 5 min, RAM/disk > 85 %, backup age, open breakers) with once-per-hour re-alerts and recovery messages. `scripts/backup-db.sh`, `restore-db.sh`, launchd plists for macOS.

---

## 9. Tests

- Framework: **vitest 5** (`vitest.config.ts` includes `src/**/*.test.ts`). 50 test files, **650 `it()` cases**, 8 `describe.skipIf(!TEST_DATABASE_URL)` integration suites (`src/db/db.test.ts` 441 lines: migrations, agent store, paper ledger, outbox, approvals, versions, shadow, venue tag).
- By area: agents 148, backtest 81, risk 61 (incl. 20k-case property test), market 45, api 40, trader 36, execution 35, ops 34, strategy 32, config 26, paper 25, db 22, approval 16, brain 14, notify 11, exchange 8, shadow 8, version 5.
- Doubles: `src/test/fakeExchange.ts` (271, a fake Binance futures account + feed), `fakeModel.ts` (scripted LLM turns, hang for timeouts), `fakeSql.ts`.
- Repo-wide static tests: `safety.test.ts` (order calls only in `orders.ts`, no withdraw/transfer), `isolation.test.ts` (import-chain reachability), `api.test.ts` checks the API imports no exchange/execution code and binds 127.0.0.1.
- Not covered: the Angular dashboard (48 files, 3,536 lines TS; karma configured, no spec content checked), real exchange behaviour beyond `demoTrade.ts`.

---

## 10. Docs (`docs/`, 18 files, 2,425 lines)

`00_START_HERE` (owner guide) · `01_PRD` · `02_TRD` · `03_APP_FLOW` · `04_UI_UX_BRIEF` · `05_BACKEND_SCHEMA` · `06_IMPLEMENTATION_PLAN` · `07_CONFIG` · `08_TRADING_SKILLS` · `09_STRATEGY_RESEARCH` · `10_STRATEGY_CANDIDATES` · `11_RESEARCH_3` · `12_AGENT_TEAM` · `13_DEPLOY_AWS` · `14_DASHBOARD` · `16_CHANGE_PROCEDURE` · `17_GOLD_RESEARCH` · `18_AI_RESEARCH` (no 15). Plus `docs/research/research-{1,2,3}/*.json` backtest reports and two MIT skills in `.claude/skills/` (`backtest-expert`, `crypto-regime-analyzer`, Python; ported to TS).

**00_START_HERE** (226 lines): plain-language owner manual. The product today = an AI team paper-trading a Binance futures demo account, about to start an 8-12 week forward test; agents table; one decision step by step (Risk Officer → 3 PM samples with 2-of-3 majority → code gates → half-size against the tide → Approver with 2-minute Cancel → risk engine → exchange stop); the four always-on guards; `./tb` commands; Telegram commands and messages; dashboard pages; week-by-week forward-test plan and go-live checklist; troubleshooting table; the few settings that matter; costs (~$2-4/day Claude, ~$22/month AWS).

**06_IMPLEMENTATION_PLAN** (85 lines): Phases 0-5 done (config, DB + collector with a passed 24 h gap check, risk engine v2 with property test, strategy engine + backtester with the `validation` gate, execution engine proven on demo). Phase 5's planned 7-day soak was replaced by the forward test's first week (`soak:check`). Phases 6-8 (Telegram/API, dashboard, agents) were built via docs/12 and 14. Phase 9 deploy went to AWS (docs/13). Phase 10 = the forward test. Go-live gate is the owner's manual decision.

**Documented lessons / limitations** (read from docs/09, 11, 12, 17, 18 and code comments):
- 25 pre-registered rule-based variants over 3 rounds + the gold round: **0 passed the gate**; "stop strategy research"; with 0.5 % risk and ≤5 positions the strategies hold a few % of equity and can't be an income engine at the owner's capital.
- LLM agents cannot be backtested on history (memorisation); only forward paper trading counts (docs/12 "Honest constraints", docs/18 §2).
- Evidence review (docs/18): no AI architecture shows durable after-cost alpha; LLM timing is too timid in bull / too bold in bear markets; LLM confidence is poorly calibrated → consensus sampling, calibration feedback, counter-regime size cut, skill-vs-luck test; foundation-model price forecasts and RL were explicitly not adopted.
- Operational lessons baked into code: unhandled config-reload `error` event took the brain down (2026-10-31); Binance conditional orders are algo orders needing `{trigger: true}`; market orders may be acked before filling; maker-only entries never fill 47 % of the time; ccxt dropped the old futures testnet sandbox (demo only); Binance order responses carry no commission.
- Known gaps the code admits: no hedged-pair position type; trader refuses lot / vol-target / post-only / regime-filter strategies; carry needs transfers (forbidden); pgvector unused; `researcher` agent in config but not built; the regime engine is "not wired into the live loop".

---

## 11. Tech stack (`package.json`)

Runtime deps: `@anthropic-ai/sdk ^1.3.0`, `ccxt ^4.5.90` (incl. ccxt.pro WS), `fastify ^5.12.7` + `@fastify/{cookie,static,websocket}`, `grammy ^1.46.0`, `postgres ^4.0.0`, `zod ^4.6.7`, `yaml ^2.9.1`, `fast-xml-parser ^5.11.2`, `bcryptjs ^3.0.3`, `dotenv`, `tsx ^4.23.15`. Dev: `typescript ^7.0.2`, `vitest ^5.0.5`, `@types/node ^26`. Dashboard: Angular 20 (separate `dashboard/package.json`). Docker image `node:22.21.1-bookworm-slim`, multi-arch, non-root. DB `timescale/timescaledb-ha:pg16`. No ORM, no queue, no Python at runtime.

---

## 12. Reuse verdicts

| Component | Verdict | Reason | What changes for MT5 / gold / local LLM |
|---|---|---|---|
| `src/agents/runtime.ts` loop (submit-tool structured output, invalid-resubmit, timeout race, retry, budget, circuit breaker, cost ledger) | **adapt** | Best-engineered part of the repo; semantics are provider-neutral, types are not | Replace Anthropic types with an internal `ChatMessage/ToolCall/ToolResult` model; drop `effort`, `cache_control`, beta fallback; cost by tokens or wall-time instead of USD; local models need a JSON-schema / grammar-constrained output path and a fallback when a model lacks native tool calling (prompted JSON + repair) |
| `src/agents/model.ts`, `tools.ts` | **rewrite** | 100 % Anthropic-specific (SDK types, price table, beta headers) | New provider adapters (Ollama / OpenAI-compatible / llama.cpp) behind one interface; keep `jsonSchema(zod)` idea |
| Agent *pattern* (`AgentSpec`: system + prompt + zod output + `check` + `finalize`; code-computed inputs; LLM never sees raw untrusted text) | **reuse as-is** (pattern) | Clean, tested, isolation-tested | Specs themselves are crypto-specific (coins, funding, perps) and must be rewritten for XAUUSD |
| `marketAnalyst.ts`, `riskOfficer.ts`, `portfolioManager.ts`, `consensus.ts`, `challenger.ts`, `cycle.ts` | **adapt** | Structure (code digest → LLM → code gates → consensus → shadow challenger) maps directly onto research/strategy/risk bots | Single instrument; replace universe/positioning/funding with gold drivers (DXY, real yields, sessions, COT, macro calendar); `gateEntry` in points/lots; hard rules for session close, spread blowouts, news windows |
| `newsReader.ts` + `news/feeds.ts` | **adapt** | Injection-hardened structured-event extraction is reusable | Different feeds (macro, Fed, gold miners, geopolitics), event types, no "venue" notion (or Exness status) |
| `timeMachine.ts` | **adapt** | Seeded block bootstrap + vol matching + analogs is instrument-agnostic | Candle source is MT5 history; model session gaps (skip weekend blocks), costs in spread + commission + swap |
| `regime/*` | **drop / rewrite** | Components are crypto-only (BTC dominance, alt breadth, funding) | A gold regime needs DXY/yields/vol/trend components |
| `positioning.ts`, `fastRisk.ts` | **drop** (reuse percentile/crowding helpers) | Binance OI / taker / L/S data | COT, retail sentiment if available; `percentile`, `crowdingOf` shape reusable |
| `auditor.ts`, `chiefOfStaff.ts`, `weeklyReview.ts`, `approvalReview.ts` | **adapt** | Post-mortem → lessons → prompt feedback, daily brief, deterministic weekly review are good patterns | Queries are table-specific; brief sections and schedule tz change |
| `scheduler.ts`, `store.ts` (full-text memory) | **reuse as-is** / adapt | Small, tested, generic | Memory: consider pgvector + local embeddings (unused so far); keep FTS as baseline |
| `src/risk/riskEngine.ts`, `guards.ts`, `sizing.ts`, property test | **adapt** | Pure, ordered, exhaustively tested, reject codes | Units: lots, contract size (100 oz), tick value, points; margin/leverage per Exness account type; spread + commission + swap instead of taker fee/funding; session-aware limits (no new entries into the close, weekend flat option); drop regime zones or redefine |
| `src/config/*` (zod + ceilings + watcher + diff + hash) | **reuse as-is** (mechanism), rewrite schema | Mechanism is generic and battle-tested | New schema for XAUUSD / MT5 / local-LLM settings |
| `src/version/*` | **reuse as-is** | Config hash + prompts hash + git commit | Extend `promptsHash` to a prompt registry if prompts become data |
| `src/backtest/engine.ts`, `fills.ts`, `walkForward.ts`, `metrics.ts`, `evaluate.ts`, `validate.ts`, `regimes.ts` | **adapt** | Pessimistic fill model, gates, walk-forward and scorer are instrument-agnostic | Session/weekend gaps, variable spread by session, swap at 00:00 server time (triple Wednesday), lot rounding, no funding/liquidation (use margin call/stop-out level); daily data loader is Binance-specific |
| `bookSlippage.ts`, `carry.ts`, `lotEngine.ts`, `dailyData.ts`, `data.ts`, `researchGold.ts` | **drop** | Binance order books / perps / PAXG | Replace with MT5 tick/candle history and spread-at-time modelling |
| `src/exchange/*`, `src/execution/orders.ts`, `src/trader/engine.ts`, `market/feeds.ts`, `market/collector.ts`, `market/universe.ts` | **rewrite** | Entirely ccxt/Binance (algo orders, GTX, reduce-only, user-data stream, funding) | MT5 bridge (Python `MetaTrader5` package or a Windows/Wine gateway, ZeroMQ/REST); keep the *invariants*: one order placer, `checkEntry` inside it, SL attached to every position (MT5 supports SL/TP on the order itself), reconcile loop, deterministic client ids (`magic` + comment) |
| `src/paper/*`, `src/shadow/*`, `src/report/shadow.ts` | **adapt** | Shadow ledger + baselines + "what not-taken would have made" is exactly the self-learning evidence layer | Baselines: do-nothing, hold-gold, random same stop/size; fills in points with spread; Exness demo account replaces Binance demo |
| `src/approval/*` + Telegram buttons | **adapt** | Conditional-UPDATE state machine, escalation rules, veto window | Same flow; add confirm-flow for settings changes |
| `src/report/scorecard.ts`, `calibration.ts` | **adapt** | Brier calibration, paired bootstrap vs random, go-live gate | Metrics in money/R unchanged; swap cost lines |
| `src/notify/*` | **adapt** | Owner-only bot, outbox with cursors, redaction | Needs a real chat assistant (LLM-backed, read-only by default, confirm-before-change), not fixed commands |
| `src/api/*` + `dashboard/` | **adapt / rewrite UI** | Fastify auth, localhost bind, DB-poll `/ws` are fine | Routes are table-specific; Angular dashboard is crypto-specific |
| `src/brain/*`, `src/process/*`, `src/ops/*`, `scripts/watchdog.sh`, deploy/rollback | **reuse as-is** (patterns) | Generic supervision, locks, deploy with health + auto-rollback | MT5 bridge becomes a supervised part; health must include "terminal connected / symbol quotes fresh"; AWS/launchd specifics change |
| `db/migrations`, `docker-compose.yml`, `src/db/*` | **adapt** | Timescale + pgvector + forward-only migrations | New schema; actually use pgvector for memory |
| Tests + doubles (`fakeExchange`, `fakeModel`, `fakeSql`), safety/isolation greps | **adapt** | The *style* (pure functions, injected deps, static safety tests) is the main thing to carry over | New `fakeMt5`, provider-agnostic `fakeModel` |
| Docs 12, 16, 18 | **reuse as reference** | Honest constraints, change procedure, evidence review | — |

---

## 13. Weaknesses the new product must beat

1. **Hard-wired to one cloud LLM provider** (Anthropic SDK types through runtime/tools/prompts; USD price table; `effort`/cache/beta features). No local model path, no model routing, no structured-output fallback for models without tool calling.
2. **No real self-learning loop.** Feedback is prompt text only (5 latest lessons, calibration line). Nothing proposes, tests, promotes or rolls back a strategy/prompt/parameter change automatically; promotion is a human reading a scorecard and editing YAML. Prompts are constants, not versioned data; the only experiment mechanism is one shadow challenger.
3. **Chat assistant is seven fixed Telegram commands**, no natural-language Q&A, no LLM access to memory/state for the owner, and no confirm flow for changing settings through chat (settings only via dashboard password or file).
4. **Research is manual and offline** (`npm run research:*` scripts, pre-registered by hand); no research bot that generates hypotheses, runs the backtester, and files results; backtest reports for gold weren't even committed.
5. **Crypto assumptions everywhere**: 24/7 markets, 8-hourly funding, USDT notional sizing, taker-fee cost model, OI/funding crowding, BTC-relative regime, universe of coins. No session calendar, swap, lots/points, spread-by-session, weekend gap or margin-stop-out modelling.
6. **Memory is full-text search over summaries** (300-char snippets); pgvector is installed but unused; no embeddings, no retrieval over richer artefacts.
7. **Performance review is thin**: Auditor judges one trade at a time; weekly review is arithmetic; no cross-trade pattern mining, no per-agent attribution beyond helped/misled labels, no automatic parameter or prompt adjustment from it.
8. **Decision cadence is slow and coarse** (PM every 4 h, Market Analyst hourly, 10 coins, ≤3 positions, 4-72 h horizons); fine for crypto swing but not for gold session-based intraday behaviour.
9. **Execution is single-venue, single-path** (ccxt/Binance quirks encoded in orders.ts/engine.ts); nothing abstracts a broker.
10. **Cost/latency accounting is USD-only**; a local-LLM product needs token/latency budgets, GPU queueing, model warm-up and per-model timeouts.
11. **Dashboard is an afterthought built in a separate session**, untested, and reflects the Binance schema one-to-one.
12. **Evidence to date**: 0 of 26 pre-registered strategies (incl. gold on PAXG) passed; the AI team's edge is unproven (forward test only just starting). The new product inherits no validated gold edge — only infrastructure and discipline.

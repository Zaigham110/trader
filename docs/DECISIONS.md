# Decisions Log

Append-only. Each entry: ID, date, status (Proposed / Accepted / Superseded), decision,
reasons, alternatives considered, consequences. "Accepted" requires owner approval or is a
recorded default the owner has not overridden (marked "Default").

| ID | Date | Status | Decision |
| --- | --- | --- | --- |
| D-001 | 2026-11-04 | Accepted | New product is standalone; existing `binance-ai-trader` is a reference, not a base. |
| D-002 | 2026-11-04 | Accepted | Phase gates: no code until Phase 2 docs approved; each phase ends with owner sign-off recorded here. |
| D-003 | 2026-11-04 | Accepted | Claim-labelling convention (Verified / Assumption / Unknown) applies to all docs. |
| D-004 | 2026-11-04 | Accepted (owner, 2026-11-04) | Core language: Python (see brief Q7). Port patterns from the TypeScript platform, not code. |
| D-005 | 2026-11-04 | Superseded by D-018 | Hosting topology: Windows host for MT5 + execution gateway; Linux GPU host for LLMs/analytics/chat (see brief Q3). |
| D-006 | 2026-11-04 | Accepted (Phase 1 approval, 2026-11-04) | LLM role: proposals only. Risk engine and execution engine are deterministic and own all orders. Permissions enforced in code. |
| D-007 | 2026-11-04 | Accepted (Phase 1 approval, 2026-11-04) | Risk limits, permissions and safety rules live in an owner-only config layer; bots have no write path to it. |
| D-008 | 2026-11-04 | Accepted (Phase 1 approval, 2026-11-04) | Martingale, grid averaging and any averaging-down logic are excluded by policy unless the owner explicitly approves after a risk review. |
| D-009 | 2026-11-04 | Accepted (Phase 1 approval, 2026-11-04) | All performance is reported net of spread, commission, swap and modelled slippage; gross figures are never shown alone. |
| D-010 | 2026-11-04 | Superseded by D-019 | LLM stack is local-first (Qwen3.5-9B / Qwen3.8-27B / 122B-class MoE) behind one OpenAI-compatible provider interface with decode-time JSON-schema enforcement; API fallback off by default and limited to redacted, non-account research. |
| D-011 | 2026-11-04 | Superseded by D-016 | Account type for automation: Raw Spread or Zero, chosen after two weeks of live spread sampling; Pro avoided unless the account currency enables market execution. |
| D-012 | 2026-11-04 | Accepted (Phase 1 approval, 2026-11-04) | Learning pipeline: append-only trial ledger; offline gate (CPCV, PBO ≤ 0.05, DSR ≥ 0.95, BH at FDR 10%) → demo → shadow → limited live → full, with anytime-valid champion/challenger tests and automatic rollback. Numeric gates are defaults pending Phase 2 review. |
| D-013 | 2026-11-04 | Accepted (Phase 1 approval, 2026-11-04) | Strategy library starts with trend / time-series-momentum variants plus a volatility-targeting overlay; session and news hypotheses enter only as pre-registered experiments. |
| D-014 | 2026-11-04 | Accepted (Phase 1 approval, 2026-11-04) | Cost model is clock-aware (session-dependent spread multipliers); new entries blocked around 08:30 ET releases, FOMC, the LBMA fix starts and the daily break. |
| D-015 | 2026-11-04 | Accepted (Phase 1 approval, 2026-11-04) | LLM news-interpretation is evaluated only on data after the model's training cutoff; trading gates on deterministic surprise features, the LLM is a validated extractor. |
| D-016 | 2026-11-04 | Accepted (owner) | Account type: Exness **Standard** (MT5 symbol expected `XAUUSDm`, suffix `-m`). No commission; cost is the spread. |
| D-017 | 2026-11-04 | Accepted (owner, risk exception) | Live capital under 50 USD on a Standard account. The owner was told that the 0.01-lot minimum (1 oz) makes per-trade risk 10–50% of equity and chose to accept it. Risk limits are set as an explicit exception (see RISK_POLICY.md §2) and every trade displays the real risk percentage. Standard Cent remains the documented alternative. |
| D-018 | 2026-11-04 | Accepted (owner) | Hosting: a single Windows PC owned by the owner runs the MT5 terminal, the Python services and the database. No dedicated GPU is assumed. |
| D-019 | 2026-11-04 | Accepted (owner) | LLM provider: Anthropic API is the main brain; account data (positions, balance, P&L) may be sent. Local models are not deployed in Phase 5 but the provider interface stays pluggable. Model tiering is D-025. |
| D-020 | 2026-11-04 | Accepted (owner) | Holding horizon: intraday only. Every position is closed before the daily break; no swap, no weekend exposure. |
| D-021 | 2026-11-04 | Accepted (owner) | Chat and alerts: Telegram only. Settings and trade commands require an explicit confirm button. |
| D-022 | 2026-11-04 | Accepted (owner) | Historical data: Exness Personal Area tick history and MT5 terminal history only. |
| D-023 | 2026-11-04 | Accepted (owner) | Phase 1 approved; Phase 2 (documentation and architecture) started 2026-11-04. |
| D-024 | 2026-11-04 | Default | Database: SQLite (WAL mode) with sqlite-vec for embeddings, one file per environment (demo/live). Chosen over Postgres because the system runs on one PC with one writer per table family and no Docker dependency. |
| D-025 | 2026-11-04 | Accepted (owner, 2026-11-04) | Model tiering on Anthropic: Haiku 5.5 for high-frequency extraction and classification, Sonnet 5.5 for analysis, post-trade review and chat, Opus 5.5 for the weekly strategy-research job. Anthropic's own guidance defaults to Opus 5.5 everywhere; the tiering is a cost choice that needs owner confirmation (estimated 20–60 USD/month vs ~330 USD/month all-Opus). |
| D-026 | 2026-11-04 | Default | Process topology on the PC: two supervised Python processes. `gateway` (MT5 I/O, risk engine, execution, reconciliation) and `brain` (bots, LLM calls, learning, Telegram). They share the SQLite database; order intents go to the gateway over a localhost HTTP API with a shared token. |
| D-027 | 2026-11-04 | Accepted (owner) | Phase 2 documents approved; Phase 3 (data pipeline, MT5 integration, backtesting) started 2026-11-04. |
| D-028 | 2026-11-04 | Default | Phase 3 is developed on Linux against a `FakeMT5` double that reproduces the documented MT5 return codes and data shapes; the real `MetaTrader5` adapter is a thin module that imports the package only on Windows. The 16 on-account tests ship as a runnable command (`goldctl mt5-tests`) for the owner's PC and their results are pasted back into `MT5_EXNESS_INTEGRATION.md`. |
| D-030 | 2026-11-05 | Accepted (owner) | Phase 3 approved; Phase 4 (trading bots and risk engine) started 2026-11-05. The on-account verification (MT5-T00..T16 on the owner's PC) was not yet run; it is carried as a Phase 4/7 prerequisite (KU-001, KU-003, KU-012, KU-019, KU-022). |
| D-031 | 2026-11-05 | Default | Phase 4 builds the agent runtime and bots against a `FakeProvider`; the Anthropic adapter and the chat bot arrive in Phase 5 as planned. Bots therefore run end-to-end in tests with scripted model output, and in demo with the deterministic strategy signal only until Phase 5. |
| D-032 | 2026-11-05 | Default | Risk engine is a pure function module `goldtrader.risk` with an ordered check list and a hypothesis property test (≥ 20,000 cases) asserting no approved order can lose more than the configured risk per trade plus modelled costs at its stop, and that every limit holds. |
| D-033 | 2026-11-05 | Default | Strategy-bot proposals with `stop_atr`/`target_atr` outside the configured bounds are clipped into bounds by code before gating (not rejected). The clipped values are what the gate and the risk engine see; the raw proposal is journaled. Owner may prefer rejection; change in `bots/strategy.py::normalise_proposal`. |
| D-034 | 2026-11-05 | Default | Brain-side features are recomputed from the shared bars table with the same `add_features` code as the backtester; the deterministic strategy signal comes from the same strategy classes. One feature implementation for backtest, gateway and brain. |
| D-035 | 2026-11-05 | Accepted (owner) | Phase 4 code complete on Linux (gateway, risk engine, execution, bots, brain, ops; 496 tests). Exit items that need the owner's PC are listed in TEST_PLAN.md §4 and carried into Phase 7. Awaiting owner approval to start Phase 5 (Anthropic provider and Telegram chat bot). |
| D-036 | 2026-11-05 | Accepted (owner) | Phase 4 approved; Phase 5 (Anthropic provider, news and calendar feeds, Telegram chat assistant, evaluation sets) started 2026-11-05. |
| D-037 | 2026-11-05 | Default | Anthropic adapter uses the official `anthropic` Python SDK: structured outputs via `output_config.format` with the bot's JSON schema, `strict` tools, `output_config.effort` per tier, adaptive thinking left at the model default, prompt caching on the static system prompt and tool list, streaming for the deep tier. Transient errors (408/409/429/5xx/timeouts) map to `ProviderTransientError`; `stop_reason == "refusal"` to `ProviderRefusal`. |
| D-038 | 2026-11-05 | Default | News sources are configurable RSS/Atom feeds; defaults are official publishers only (Federal Reserve press releases, BLS latest releases). Calendar events come from the BLS iCal feed, the committed FOMC dates and, if enabled by the owner after reading its terms, the ForexFactory weekly JSON (KU-013). |
| D-039 | 2026-11-05 | Default | Chat assistant runs on Telegram long polling inside the brain process; every factual answer about the account, trades or system is produced by read-only tools that return data plus a provenance string; state-changing requests become pending confirmations with Confirm/Cancel buttons and execute only through the gateway intents API (`source=owner_confirmed`) or the config schema. |
| D-040 | 2026-11-05 | Default | Evaluation harness is in-repo Python (no Node dependency): versioned JSONL sets for headline extraction, regime labelling and trade review, scored for schema validity, agreement and latency against any `ModelProvider`. Seed sets are small and labelled as seeds to be extended before model changes are promoted. |
| D-041 | 2026-11-05 | Default | Effort sent to Anthropic is the higher of the tier's configured effort and the bot's requested effort; the bot cannot lower the owner-configured depth. |
| D-042 | 2026-11-05 | Default | Chat actions exclude kill-switch reset: a reset is an owner command on the gateway (`resume` intent with source owner_confirmed) and is never prepared by the model. |
| D-043 | 2026-11-05 | Accepted (owner) | Phase 5 code complete on Linux (Anthropic adapter, feeds and calendar, Telegram chat assistant, evaluation harness, brain wiring, `goldctl llm-check`/`eval`; 630+ tests). Live checks that need the owner's API key, bot token and PC are listed in TEST_PLAN.md §4. Awaiting owner approval to start Phase 6 (learning system). |
| D-044 | 2026-11-05 | Accepted (owner) | Phase 5 approved; Phase 6 (learning system) started 2026-11-05. |
| D-045 | 2026-11-05 | Default | Learning gates (sample minimums, Sharpe, PBO, DSR, haircut, e-value thresholds, allocation share) live in `owner.learning`, hash-checked with the rest of the owner section; the learning module has no write path to them (R-034). |
| D-046 | 2026-11-05 | Default | A candidate is an immutable bundle derived from the champion by exactly one change: strategy parameters, strategy rule (kind from the library), or a prompt version. Experiments track one candidate against the current champion through the stages proposed → offline → demo → shadow → limited_live → promoted, or rejected / rolled_back. |
| D-047 | 2026-11-05 | Default | Limited-live allocation at the 0.01-lot minimum is by deterministic hash of the proposal id: a configurable share of qualifying signals (default 1 in 4) is routed to the challenger bundle; both are journaled against the same quotes. Shadow-stage candidates are simulated on live bars with the backtester's fill model and never sent to the gateway. |
| D-048 | 2026-11-05 | Default | Sequential champion/challenger test is a betting e-process on paired per-day return differences (winsorised at 3 MAD, bet size capped at 0.5): promote at e ≥ 20, reject when the mirrored process reaches 20, hard stop at 180 days. Valid at any stopping time because the wealth process is a nonnegative supermartingale under the null (research/04 §4.2). |
| D-049 | 2026-11-05 | Default | Production monitors apply the highest level reached (warn < pause < retire) to the bundle; a bundle never returns to active automatically; a second pause within 90 days retires it. Un-pause is an owner command (`goldctl learn unpause`). |
| D-050 | 2026-11-05 | Accepted (owner) | Phase 6 code complete on Linux (intake, offline gate on the trial ledger, e-process, stage pipeline with rollback, shadow ledger, monitors, nightly job, limited-live routing, `goldctl learn`). Numeric gates remain Assumptions until demo data exists. Awaiting owner approval to start Phase 7 (demo trading and performance report), which requires the owner's PC. |
| D-051 | 2026-11-05 | Accepted (owner) | Phase 6 approved; Phase 7 (demo trading and performance report) started 2026-11-05. Runs on the owner's Windows PC per `docs/PHASE7_RUNBOOK.md`; the developer side produces the report from the journals the owner sends back. |
| D-052 | 2026-11-05 | Default | Phase 7 uses two demo profiles: `config/demo.yaml` (5,000 USD demo balance, normal limits) is the primary run; a second demo at 50 USD with `config/demo_micro.yaml` (micro_live limits, demo environment) rehearses the live exception. Both run against the same bars; only one gateway/brain pair at a time per database. |
| D-053 | 2026-11-05 | Accepted (owner) | The owner's daily computer is a Mac; the owner will provide and manage a Windows machine (PC or VPS) that runs the MT5 terminal, the gateway and the brain. The Mac is used for Telegram and for remote administration (Microsoft Remote Desktop). D-018 is amended accordingly: one Windows host, owner-managed. |
| D-054 | 2026-11-16 | Accepted (owner) | Model spend is funded by the owner's Claude Max plan through its included monthly API credit (100 USD per billing cycle, no rollover), claimed by linking a Claude Console organization to the plan. The system keeps using a Console API key through the Anthropic API; a Claude.ai login, Claude Code OAuth token or `claude setup-token` is not used, because Anthropic's terms reserve subscription OAuth for its own applications and the Agent SDK docs direct custom applications to API keys. Verified on platform.claude.com/docs/en/about-claude/api-credits-for-subscribers (accessed 2026-11-16). Budget defaults stay under the credit: 3 USD/day, 60 USD/month. |
| D-056 | 2026-11-17 | Accepted (owner) | A local, read-only web dashboard is added (`goldctl dashboard`, http://127.0.0.1:8760). It amends D-021 and PRD N7: Telegram and `goldctl` remain the only way to command the system; the dashboard only shows the market, the account, the decision cycle and each bot's last output. It binds to loopback, answers GET only, opens the database read-only, refuses foreign `Host` headers, and imports no MT5, execution, gateway or risk code. Reason: Telegram proved unreliable from the owner's network (connection resets to api.telegram.org), leaving no dependable way to watch the system. |
| D-057 | 2026-11-17 | Accepted (owner) | The dashboard shows the agents working live and has a chat box (owner request). Amends D-056 in two ways. (1) Live view: `run_agent` writes an `agent_runs` row with status `running` at the start of a run and the decision cycle journals itself as `running` after every step; both rows are replaced by the normal result. (2) Chat: `POST /api/chat` on the dashboard inserts the owner's question into `dashboard_chat_inbox` (migration 009) through a SQLite connection whose authorizer denies every other statement; the brain's `DashboardChatService` answers it with the same Chat bot, tools, breaker, budget and post-processor as Telegram and stores the conversation under chat id 0. The dashboard still cannot command anything: `/pause`, `/kill`, `/close` and any action the model prepares are cancelled at once with an explanation, and the dashboard process still imports no MT5, execution, gateway, risk, secrets or LLM code. The POST requires a loopback `Host`, a loopback `Origin` when a browser sends one, and a custom header, so no other website can post to it. |
| D-058 | 2026-11-17 | Accepted (owner) | Time machine: `goldctl timemachine` replays the 1-minute bid/ask bars stored in the database through the configured strategy, session rules, calendar blackouts, position sizing, loss limits and costs (`backtest/timemachine.py`, reusing `run_backtest`, `compute_metrics` and `offline_gate`), and stores the result in `timemachine_runs` (migration 010). The dashboard shows runs and can start one: `POST /api/timemachine` accepts only listed values for period, timing, spread stress and strategy id and starts the command as a separate process, one at a time, so the dashboard still imports no backtest, risk or execution code. The LLM agents are not replayed: a model asked about a past date remembers what followed, so the result would be dishonest (look-ahead by memorisation); their effect is to be measured on live decisions. Two timings exist: `every_bar` and `live_5min`, the latter keeping only the signals a 5-minute decision cycle reads. First result on 60 days of stored history (2026-09-18 to 2026-11-17, gold −12%): `trend_atr_v1` with `every_bar` made 1,512 trades, 33% wins, −10.0% (stopped by the drawdown limit), costs 83% of gross moves; with `live_5min` 661 trades, −2.9%. Consequence: the planned change to make the live brain act on every 1-minute trend flip is NOT made, because it would switch on a strategy this replay shows losing after costs. Also in this change: the Strategy and Risk-advisor context now receives the next high-impact event (`BrainService._next_event`), and duplicated job methods in `brain/service.py` are removed. Open: the live brain reads the flip-only signal once per 5 minutes (it acts on about 1 flip in 5), and `calibration_fn` and the D-047 router are still not passed to `DecisionCycle`. |
| D-059 | 2026-11-17 | Accepted (owner) | Strategy search and paper candidates. (1) `goldctl strategy-search` tests a fixed grid of the trend idea decided on 5/15/30/60-minute bars (`TrendHTF` in `backtest/strategies.py`, signals only on the 1-minute bar that completes a group, executed on the 1-minute frame). It ranks on the earlier two thirds of the stored history, touches the held-out last third only for the best five, requires profit there at recorded spread and at 1.5x spread, and writes every trial to the trial ledger (`state/research.sqlite`). (2) Result on 103 days (2026-08-06 to 2026-11-17, `state/research_history.sqlite`, the MT5 terminal's history cap): the live 1-minute `trend_atr_v1` loses in both periods (−9.9% and −5.1%); the only family positive in-sample and on the held-out days is 60-minute trend state (+0.7..+1.7% over 69 days, +1.8..+2.4% over 34 days, 33-35 held-out trades, 6-18% winners). The engine, the shadow ledger and the live gate all clamp the stop to `owner.risk.stop_atr_max` (1.5 x 1-minute ATR), so every grid entry with a wider stop ran with that stop; `search.effective()` now reports and de-duplicates candidates by what is actually run. This is a candidate, not evidence of an edge: about 110 trades, a few large winners carry it, and the held-out days overlap those of an earlier search. (3) Paper candidates: table `paper_candidates` (migration 011) and `goldctl paper-add / paper-list / paper-remove`; the brain registers active rows with the shadow ledger (`BrainService._sync_paper_candidates`, experiment id `paper`), which simulates on live bars at 0.01 lot and has no path to an order. `paper_htf60_state` (minutes 60, trend state, stop 1.5, target 29.05) is registered. It is outside the learning pipeline on purpose: it cannot be promoted or routed. Live trading is unchanged. |
| D-060 | 2026-11-17 | Accepted (owner) | Three additions. (1) Decision record: every cycle stores `BotContext.snapshot()` (all bot inputs except the config) zlib-compressed in `decision_snapshots`, and `run_agent` stores the exact messages sent to the model in `agent_inputs` (not for the chat bot, whose conversation is already in `chat_messages`). `report/veto.py` and `goldctl veto-report` take every cycle where the deterministic signal wanted a trade, classify what happened (taken, vetoed by AI = wait or confidence below threshold, blocked by code, AI unavailable) and play the signal forward on stored bars as a code-only trade, in R. The brain refreshes it every 15 minutes into `stored_reports` for the dashboard. Under 30 resolved vetoes the report says it is too few to judge. (2) Positioning context: `feeds/cot.py` polls the CFTC public reporting API (dataset 72hh-3qpy, contract 088691, US government open data, no key) every 12 hours into `cot_reports`; Strategy and Risk-advisor prompts get one line (managed-money net position, percentile of the last 156 reports, change). It is context only: no code rule sizes or gates on it, because the published evidence for positioning extremes is mixed. No weekday is assumed (the CFTC announced more frequent releases). (3) Telegram transport: `notify/telegram.py` no longer uses `urllib`; it resolves every address of api.telegram.org, tries the family that worked last first (IPv6 by default), walks the list twice inside the caller's time limit, and does the TLS handshake inside that loop. Measured on the owner's line: 8 of 8 calls in about 0.5 s (before: IPv4 0 of 8, IPv6 5 of 8). Migration 012. |
| D-029 | 2026-11-04 | Default | Toolchain: Python 3.12+ (3.13 used in development), `uv` for environments, `pytest` + `hypothesis` for tests, `pydantic` v2 for schemas, `numpy`/`pandas` for data, SQLite via the standard library. No ORM. |


## D-001 — Standalone product
- **Reasons:** different venue (MT5/CFD vs Binance), different instrument economics
  (swap, commission, 100-oz contract, leverage tiers), different runtime (Windows terminal
  dependency), local-LLM-first. The owner asked for a new product.
- **Alternatives:** fork and adapt. Rejected: exchange abstraction in the old code is
  Binance-shaped; the fee/funding model does not map to CFD swaps.
- **Consequences:** reuse is by pattern and selective port (see audit in `RESEARCH.md`).

## D-004 — Python core (Proposed)
- **Reasons:** the official `MetaTrader5` package is Python; the validation stack (purged
  CV, PBO, deflated Sharpe, statsmodels/arch) and local-LLM tooling are Python-native.
- **Alternatives:** TypeScript (direct reuse of existing risk/config/backtest code).
  Would require a Python or MQL5 sidecar for MT5 anyway.
- **Consequences:** proven TypeScript modules (risk engine with property tests, config
  ceilings, fills/slippage model) are ported with their tests, not imported.
- **Needs:** owner confirmation (brief Q7).

## D-005 — Split hosting (Proposed)
- **Reasons:** MetaTrader5 Python package requires Windows and a running terminal (to be
  Verified in RESEARCH.md §3); GPU LLM serving is best supported on Linux.
- **Alternatives:** single Windows GPU workstation (simplest, viable for one GPU); MT5
  under Wine on Linux (unsupported by MetaQuotes for the Python API).
- **Consequences:** a small authenticated gateway service on the Windows host exposes
  quotes, account state and order endpoints; all strategy/LLM logic stays on Linux.
- **Needs:** owner confirmation (brief Q3).

## D-010 — Local-first LLM stack (Proposed)
- **Reasons:** owner requirement; 2026 open-weight models are Apache/MIT and strong (RESEARCH.md §4);
  every mainstream local server enforces JSON schemas at decode time, which the proposal-only agent
  design depends on; privacy of positions and strategy.
- **Alternatives:** API-first. Cheaper at our volume (10–25 USD/month vs 80–360 USD/month) but
  exposes account data, carries repricing and deprecation risk, and contradicts the owner requirement.
- **Consequences:** hardware purchase (brief Q3); eval harness in CI; fallback ladder in `LOCAL_LLM.md`.

## D-011 — Raw Spread or Zero account (Proposed)
- **Reasons:** market execution without requotes; explicit, verified commission (3.5 or 5.5 USD per lot
  per side on XAUUSD); the lowest modelled all-in round-trip cost; MT5 Real allows unlimited open
  positions and 1,000 pending orders on both.
- **Alternatives:** Standard (no commission, wider spread; acceptable for low-turnover trend
  strategies), Pro (instant execution with requotes unless account currency is USD/JPY/THB/CNY/IDR/VND),
  Standard Cent (100/200 order cap, server-dependent).
- **Consequences:** account choice is finalised only after live spread sampling (KU-004).

## D-012 to D-015
- Derived directly from RESEARCH.md §1, §5 and §7; rationale and sources are there. Numeric defaults
  are Assumptions and will be reviewed when `LEARNING_SYSTEM.md`, `RISK_POLICY.md` and `TEST_PLAN.md`
  are written in Phase 2.

## D-017 — Oversized per-trade risk accepted (owner exception)
- **Context:** Owner capital is under 50 USD on a Standard account. The smallest XAUUSD trade is 0.01 lot
  = 1 troy ounce, so a 1 USD move in gold is 1 USD of P&L. Gold's 14-day ATR on 2026-11-04 was about
  81 USD (RESEARCH.md §1.4). Any stop wide enough to survive normal noise is a large share of equity.
- **Owner decision (2026-11-04):** stay on Standard and accept oversized risk. Offered alternatives were
  Standard Cent (0.01 oz minimum), depositing more, or demo-only.
- **Consequences:** `RISK_POLICY.md` carries an "owner exception" block with ceilings far above normal
  practice (risk per trade up to 30% of equity, daily loss 40%, drawdown kill switch 60%). The bot
  never opens more than one 0.01-lot position. Every Telegram trade message shows "risk X% of equity".
  Go-live criteria in `TEST_PLAN.md` treat live Phase 8 as a functional proof, not an income test.
- **Reversal path:** open a Standard Cent account; change `account.symbol` and the exception block;
  no code change needed.

## D-019 — Anthropic API as main brain (owner)
- **Reasons:** owner choice (brief Q5 follow-up). No GPU on the owner's PC; research showed cheap
  API tiers cost 10–25 USD/month at our volume versus 80–360 USD/month for local hardware.
- **Alternatives:** local-first (original brief), scrubbed-API. Owner accepted sending account data;
  Anthropic states API inputs are not used for training by default (RESEARCH.md §4.5, Verified).
- **Consequences:** `LOCAL_LLM.md` is retitled in content to cover the provider layer with Anthropic
  as the default provider and a local provider as an optional later addition. Budget caps, structured
  outputs and a rule-based fallback remain mandatory because API outages and refusals happen.

## D-024 — SQLite instead of Postgres (Default)
- **Reasons:** one PC, two processes, low write rate (ticks are sampled, not stored raw), no Docker.
  SQLite with WAL handles concurrent readers and a single writer per process cleanly; sqlite-vec covers
  embeddings for chat memory.
- **Alternatives:** Postgres + TimescaleDB + pgvector (the old platform). Better for multi-host and
  high-frequency tick storage; unnecessary here.
- **Consequences:** a thin repository layer so the engine can move to Postgres later; nightly backups
  are a file copy.

## D-055 — Spec read from the account is authoritative; "pip" stays 0.01 USD (Default)
- **Date:** 2026-11-17. **Reasons:** `spec-dump --real` on the owner's demo showed digits 3 /
  point 0.001 / tick value 0.1 USD, not the 2-decimal quoting the published Exness pages imply. All
  code paths size and place stops in points from the stored `symbol_specs` row; the docs keep
  "pip = 0.01 USD" only for human money math. The fake terminal and `goldctl` defaults were changed to
  the verified values so tests and the real account agree.
- **Also recorded:** Exness returns 10011/10006 "bad stops" and 10014 where the MQL5 docs say 10016 and
  10038; the on-account probes accept both, and the execution engine treats any rejection whose comment
  mentions stops or volume as fatal (no retry), because retrying the same request cannot succeed.
- **Alternatives:** redefine pip as 0.001 everywhere (confusing against Exness's own pages); keep the
  2-decimal defaults (tests would pass against a terminal that does not exist).
- **Consequences:** `MT5_EXNESS_INTEGRATION.md` §2 and §8 hold the verified table; KU-001, KU-012,
  KU-019 closed, KU-003 partly closed.


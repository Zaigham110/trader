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
| D-025 | 2026-11-04 | Proposed | Model tiering on Anthropic: Haiku 5.5 for high-frequency extraction and classification, Sonnet 5.5 for analysis, post-trade review and chat, Opus 5.5 for the weekly strategy-research job. Anthropic's own guidance defaults to Opus 5.5 everywhere; the tiering is a cost choice that needs owner confirmation (estimated 20–60 USD/month vs ~330 USD/month all-Opus). |
| D-026 | 2026-11-04 | Default | Process topology on the PC: two supervised Python processes. `gateway` (MT5 I/O, risk engine, execution, reconciliation) and `brain` (bots, LLM calls, learning, Telegram). They share the SQLite database; order intents go to the gateway over a localhost HTTP API with a shared token. |


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

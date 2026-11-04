# PRD — AI Gold Trader (XAUUSD on Exness via MT5)

Status: Phase 2 draft for owner review. Last updated: 2026-11-04.
Traceability: requirements are `R-nnn`; decisions `D-nnn` live in `DECISIONS.md`; open items `KU-nnn`
in `KNOWN_UNKNOWNS.md`. Code and tests reference these IDs.

## 1. Goals

G1. Trade XAUUSD on the owner's Exness Standard account through MT5, intraday only, with every order
    passing a deterministic risk engine that no AI component can bypass.
G2. Make every decision traceable: signal → reasoning → risk check → order → fill → outcome, with the
    market conditions at the time attached.
G3. Learn from results under statistical gates: proposals are tested out-of-sample, on demo, in shadow
    and in limited live before promotion, and roll back automatically.
G4. Give the owner a Telegram chat assistant that answers about the market, positions, performance,
    reasoning, system health and settings from live data and logs, with sources shown, and that acts
    only after explicit confirmation.
G5. Run unattended for weeks on one Windows PC and degrade safely (flat, not frozen) when MT5, data or
    the AI provider fails.
G6. Report performance net of spread, swap (zero when intraday), commission (zero on Standard) and
    realistic slippage, reconciled to the broker's deal history.

## 2. Non-goals

N1. Not an income engine at the owner's current capital. With under 50 USD and a 1-oz minimum trade,
    the live phase is a functional proof under an owner-approved risk exception (D-017). The product is
    built so that larger capital or a Cent account changes configuration, not code.
N2. No martingale, grid averaging, averaging down or any "guaranteed profit" logic (D-008).
N3. No multi-day or weekend positions in the first release (D-020).
N4. No local LLM deployment in the first release (D-019); the provider interface keeps it possible.
N5. No reinforcement learning for signal generation (RESEARCH.md §5.4).
N6. No instruments other than XAUUSD in the first release; the config supports more symbols later.
N7. No web dashboard in the first release (D-021); Telegram is the only interface.
N8. The AI never holds broker credentials, never places orders, never edits risk limits, permissions
    or its own safety rules.

## 3. Users

- **Owner** (single user, Karachi, Telegram). Approves phases, sets risk limits, confirms commands,
  reviews the weekly report. Not watching in real time.
- **Operators of the system** are the bots themselves (see `AGENTS.md`); they have scoped permissions.

## 4. Requirements

### 4.1 Trading core

| ID | Requirement | Source |
| --- | --- | --- |
| R-001 | Connect to the owner's MT5 terminal with the official `MetaTrader5` Python package on Windows; read and log `account_info()`, `terminal_info()` and the full `symbol_info()` for the gold symbol at startup and daily; fail closed if contract size, volume step or trade mode change. | RESEARCH §3, D-018 |
| R-002 | Derive session state (open, close-only, closed, daily break, weekend) from `symbol_info().trade_mode` and the Exness schedule, never from a hard-coded clock; handle the US-DST schedule shift. | RESEARCH §3.4 |
| R-003 | Exactly one code path places, modifies or closes orders (the execution engine in `gateway`). It re-runs the risk engine before every send. | D-006 |
| R-004 | Every position carries a stop-loss on the broker side at open. If the SL cannot be set, close the position immediately and alert. | old platform rule, adopted |
| R-005 | Close all positions before the daily break (configurable buffer, default 15 minutes) and refuse new entries inside the buffer. | D-020 |
| R-006 | Block new entries during configurable windows around high-impact releases, FOMC, LBMA fix starts and the daily break; defaults in `RISK_POLICY.md`. | RESEARCH §1.5, §3.5 |
| R-007 | Model costs per trade from the live spread at decision time plus a configurable slippage allowance; record requested vs filled price for every fill. | D-009, D-014 |
| R-008 | Reconcile open positions, pending orders and deal history against the broker every 60 s and at startup; repair missing stops; alert on discrepancies. | old platform pattern |
| R-009 | All configuration (symbol, sessions, strategies, limits, bot permissions, model tiering, budgets) lives in one versioned YAML with a schema; invalid config never loads; the previous valid config stays active. | brief |
| R-010 | Risk limits, permissions and safety rules live in a separate owner-only section with hard ceilings compiled into code; bots have no write path. | D-007 |

### 4.2 Bots and AI

| ID | Requirement | Source |
| --- | --- | --- |
| R-020 | Specialised bots: Research, Strategy, Risk, Execution, Review, plus Chat. Roles, permissions, inputs, outputs and escalation are in `AGENTS.md`. | brief |
| R-021 | LLM outputs are structured and schema-validated (Anthropic structured outputs plus Pydantic). Invalid output after one repair attempt falls back to a rule-based default flagged `degraded`. | RESEARCH §4.2 |
| R-022 | Provider-agnostic `ModelProvider` interface; Anthropic is the default; a local OpenAI-compatible provider can be added without touching bot code. | D-019 |
| R-023 | Per-call timeouts, per-day USD budget, circuit breaker (3 consecutive failures → bot disabled, half-open retry), cost ledger per run. | old platform pattern |
| R-024 | Untrusted text (news) is wrapped and defanged; bots that read it have no tools that change state. | old platform pattern |
| R-025 | Every LLM call records model ID, prompt version hash, input/output tokens, cost, latency and the schema version. | G2 |

### 4.3 Learning

| ID | Requirement | Source |
| --- | --- | --- |
| R-030 | Record every signal, decision, reason, order, fill, outcome and the market snapshot (spread, ATR, session, regime, upcoming events) in the trade journal. | brief |
| R-031 | Append-only trial ledger: every backtest run, parameter set, prompt version and proposal with its Sharpe, sample size, skew, kurtosis, cost model and data snapshot id. | RESEARCH §5 |
| R-032 | Promotion pipeline: proposal → offline gate → demo → shadow → limited live → full, with numeric gates in `LEARNING_SYSTEM.md`; automatic demotion and rollback. | D-012 |
| R-033 | Every change is a versioned immutable bundle (code hash, params, prompts, model ID, config) stamped on every order. | RESEARCH §5.4 |
| R-034 | Bots cannot change risk limits, permissions, safety rules or the promotion gates. | D-007 |
| R-035 | LLM news interpretation is evaluated only on post-training-cutoff data. | D-015 |

### 4.4 Chat

| ID | Requirement | Source |
| --- | --- | --- |
| R-040 | Free-text Telegram chat about market, positions, performance, trade reasoning, system health and settings; owner chat ID only. | D-021 |
| R-041 | Factual answers about account, trades and system come from the database or MT5 state via read-only tools and carry a provenance line (table or MT5 call and timestamp). | brief |
| R-042 | Answers label content as Fact (from data), Opinion (model judgement) or Forecast (expectation with uncertainty). | brief |
| R-043 | Any command that changes settings, closes or opens a trade, pauses or resumes presents a summary and requires a confirm button press within a timeout; the confirmed action goes through the same config schema or risk engine as any other. | brief |

### 4.5 Risk and security

| ID | Requirement | Source |
| --- | --- | --- |
| R-050 | Configurable limits: risk per trade, daily loss, weekly loss, max drawdown, max positions and lots, max spread, news windows, session windows, max orders per minute. Hard ceilings in code. | brief, D-017 |
| R-051 | Kill switch: owner command, drawdown trigger, or safety-violation trigger; closes positions, cancels orders, blocks entries until owner reset. | brief |
| R-052 | Safe responses to lost MT5 connection, stale ticks, failed orders, AutoTrading disabled, API outage: defined in `RISK_POLICY.md` §5. | brief |
| R-053 | Credentials in Windows Credential Manager or an encrypted secrets file outside the repo; never in config, logs or Telegram. Log redaction tested. | brief |
| R-054 | Performance reported net of all costs and reconciled to `history_deals_get()`. | D-009 |

## 5. Measurable success criteria

| ID | Criterion | Measured in |
| --- | --- | --- |
| S-1 | 100% of orders in demo and live pass through the execution engine with a logged risk-engine verdict; static tests prove no other order path exists. | Phase 4 tests, Phase 7 audit |
| S-2 | Every closed trade has a complete journal record (signal, reason, risk verdict, order, fill, outcome, snapshot); zero orphans in a 4-week demo. | Phase 7 |
| S-3 | Demo run of at least 4 weeks with uptime ≥ 99% of trading hours, no position without a broker-side stop for more than one reconcile cycle, no unhandled crash. | Phase 7 soak |
| S-4 | Reported net P&L reconciles to broker deal history within 0.01 USD per trade. | Phase 7 |
| S-5 | Chat: 20 scripted questions answered with correct provenance; 5 scripted change commands blocked without confirmation. | Phase 5 acceptance |
| S-6 | Learning: at least one proposal has travelled the full pipeline on demo with ledger, gates and rollback demonstrated (promotion itself may legitimately fail). | Phase 6 |
| S-7 | AI spend stays under the configured monthly budget; budget breach stops AI calls, not trading safety. | Phase 5 onward |
| S-8 | Live Phase 8 (under D-017): zero risk-rule violations, every live fill within modelled slippage × 2, owner can halt within one Telegram command. Profitability is not a criterion at this capital. | Phase 8 |

## 6. Constraints and assumptions

- Capital under 50 USD; Standard account; one PC; Telegram; Anthropic API; intraday only (D-016 to D-022).
- **Assumption:** the PC stays on during gold trading hours; a VPS is a later option (KU-020).
- **Assumption:** monthly AI cost 20–60 USD with tiered models (D-025), which exceeds the trading
  capital. The owner has accepted that this phase is about building and proving the system.

# Project Brief — AI Gold Trader (XAUUSD on Exness via MetaTrader 5)

Status: Phase 1 approved 2026-11-04 (D-023). Phase 2 documents drafted the same day and awaiting owner review. Owner answers to Q1–Q8 are recorded as D-016 to D-023; the defaults below are superseded where those decisions differ.
Last updated: 2026-11-04

This file records the shared understanding of the project, the questions that block
design decisions, the research plan, and the documentation structure. It is the entry
point for every other document in `docs/`.

---

## 1. Understanding of the project

**What we are building.** A new, standalone product (not a fork) that trades spot gold
CFDs (`XAUUSD`) on an Exness account through the MetaTrader 5 (MT5) terminal. It is
operated by a roster of specialised bots, uses locally hosted large language models (LLMs)
by default, records and learns from its own results under strict safety gates, and exposes
a chat assistant the owner can talk to about the market, positions, performance, trade
reasoning, system health and settings.

**Hard constraints the owner set.**

| Constraint | Consequence for the design |
| --- | --- |
| Deterministic code for execution and risk; an LLM must never bypass them | LLMs produce *proposals* (structured JSON). A deterministic risk engine and execution engine own every order. Permissions are enforced in code, not prompts. |
| Local LLMs are the default, behind a provider-agnostic interface | One `ModelProvider` interface; local serving (e.g. OpenAI-compatible local server) is the default; API providers are optional fallbacks with an explicit policy. |
| Full configurability (symbols, sessions, strategies, limits, permissions) | Single versioned config with a schema; immutable "ceilings" that bots cannot raise. |
| Self-learning with versioning, out-of-sample tests, demo validation and rollback | A promotion pipeline: propose → backtest OOS → demo → shadow → limited live → full, with champion/challenger comparison and automatic rollback triggers. |
| Bots must not change risk limits, permissions or their own safety rules | Those live in a separate, read-only-to-bots config layer, signed/hashed, changed only by the owner. |
| Chat answers about account/trades/system come from live data or logs, with sources | Chat assistant is a retrieval-first agent over the trade journal, config and MT5 state; every factual answer carries a provenance tag; facts, opinions and forecasts are labelled. Any state-changing command needs explicit confirmation. |
| No martingale, grid averaging or "guaranteed profit" logic without explicit approval | Strategy library excludes these by policy; risk engine rejects position-adding patterns that match them. |
| Performance reported after spread, commission, swap and realistic slippage | Backtester and live P&L attribution both model all four; "gross" numbers are never shown alone. |
| Credentials stored securely, never hardcoded or logged | OS keychain / secrets file outside the repo, redaction in logs, no secrets in config. |
| Phased delivery with approval gates after each phase | Nothing in a later phase starts without a recorded approval in `DECISIONS.md`. |

**Reuse from `binance-ai-trader`.** The existing platform (TypeScript, Node 20, Postgres/
TimescaleDB, Binance spot/futures, multi-agent roster, risk engine, backtester, Telegram
notifications) is being audited for parts that fit. The preliminary view is that patterns
(config schema + ceilings, risk-engine property tests, backtest fills/slippage model,
approval-review flow, scorecard reporting) transfer well, while exchange code, candle
collection and the Binance-specific fee/funding model do not. The audit result will be
recorded in `RESEARCH.md` (section "Existing platform audit") and `DECISIONS.md`.

**What "significantly more capable" means here (proposed success criteria, to be
finalised in `PRD.md`).**

1. Every decision is traceable: signal → reasoning → risk check → order → fill → outcome,
   with market conditions attached.
2. The system can run unattended for weeks on demo with measured uptime, and degrades
   safely (flat, not frozen) when MT5, data or the LLM fails.
3. Strategy changes are evidence-gated; the owner can see for any live parameter *why*
   it is what it is and *what it replaced*.
4. The chat assistant answers from data with provenance, and never acts without
   confirmation.
5. Reported performance is net of all costs and reconciles to the broker statement.

---

## 2. Questions that block progress

These change the design materially. Defaults are recorded so that work continues if the
owner does not answer; the default is used until overridden and is logged in
`DECISIONS.md`.

| # | Question | Why it matters | Default if unanswered |
| --- | --- | --- | --- |
| Q1 | Which Exness **entity/region** is the account under, and which **account type** (Standard, Pro, Raw Spread, Zero, Standard Cent)? | Leverage caps, spreads, commissions, swap-free eligibility and XAUUSD contract specs differ by entity and account type. | Design for **Raw Spread or Zero** (lowest all-in cost for an automated system), verify specs on the live account via `symbol_info()` before Phase 3 sign-off. |
| Q2 | Approximate **starting capital** for demo and later live, and the risk appetite (max daily loss %, max drawdown % before stop)? | Determines minimum lot feasibility (XAUUSD contract size is typically 100 oz), sizing granularity, and whether Standard Cent is needed. | Design assuming **USD 5,000–20,000**; default limits: 0.5% risk per trade, 2% daily loss, 10% max drawdown kill switch. |
| Q3 | **Hosting topology.** Do you have (or will you buy) a GPU workstation? Is the MT5 terminal to run on the same Windows machine, on a Windows VPS, or on Linux under Wine? | The official MetaTrader5 Python package is Windows-only and needs a running terminal; LLM serving prefers Linux. This decides the bridge design. | **Windows VPS (or Windows PC) runs MT5 + a thin execution gateway; a Linux GPU box runs LLMs, analytics and the chat bot; they talk over an authenticated local network/VPN.** |
| Q4 | **Holding horizon** preference: intraday-only (flat before rollover/weekend) or multi-day swing allowed? | Multi-day holds pay swap and carry weekend gap risk; intraday avoids both but limits strategies. | **Intraday-first; swing allowed only when explicitly enabled per strategy with swap cost modelled.** |
| Q5 | May the system use a **paid API model as a fallback** (e.g. for weekly strategy research or when the local model is down), with trading data redacted? | Affects privacy, cost and the fallback ladder. | **Allowed for non-time-critical research only, off by default, with redaction; never for execution-path decisions.** |
| Q6 | Chat interface: **Telegram** (like the existing platform), a local **web UI**, both, or CLI? | Affects the confirmation flow and the security model for commands. | **Local web UI as primary (richer provenance display) + Telegram for alerts and simple commands.** |
| Q7 | **Programming language** for the new product: Python (dominant for MT5 package, ML, backtesting) or TypeScript (reuse existing code directly)? | Decides how much existing code is reused verbatim vs ported. | **Python for core (MT5, data, backtest, ML, LLM, risk); port the proven TypeScript risk/config patterns rather than the code.** |
| Q8 | Do you have an existing **historical data** source/licence (tick data) or should we rely on Exness MT5 history plus a free provider? | Backtest realism for spread/slippage depends on tick data. | **Exness MT5 history as primary (depth to be measured), Dukascopy tick data as secondary where licence permits.** |

Everything else (strategy set, model choice, metrics) will be decided with recorded
defaults and can be changed later without re-architecture.

---

## 3. Research plan (Phase 1)

Each area is researched from primary sources, with every important claim labelled
**Verified** (link + access date), **Assumption**, or **Unknown** (with a proposed test).

| Area | Questions to answer | Primary sources | Output |
| --- | --- | --- | --- |
| 1. Gold trading | Drivers, sessions, volatility regimes, macro events, spread/slippage behaviour, strategies suited to automation, what does not work | LBMA, CME Group, World Gold Council, Fed/FRED, Cboe (GVZ), academic literature | `RESEARCH.md` §1 |
| 2. Agent skills | Skills per bot; LLM vs deterministic vs hybrid; evidence from multi-agent trading literature | Papers (TradingAgents, FinMem, etc.), practitioner sources | `RESEARCH.md` §2, feeds `AGENTS.md` |
| 3. Exness + MT5 | Integration options, account types, order rules, costs, limits, demo, hosting | exness.com help centre & contract specs, mql5.com docs, MetaQuotes | `RESEARCH.md` §3, feeds `MT5_EXNESS_INTEGRATION.md` |
| 4. Local LLMs | Models (late-2026), serving tools, structured output support, hardware, latency, cost vs API | Hugging Face model cards, vendor blogs, serving-tool docs, pricing pages | `RESEARCH.md` §4, feeds `LOCAL_LLM.md` |
| 5. Self-learning | Overfitting (PBO, DSR, CPCV), leakage (incl. LLM lookahead), decay monitoring, safe promotion, champion/challenger | López de Prado, Bailey et al., Harvey–Liu–Zhu, Lo, McLean–Pontiff, library docs | `RESEARCH.md` §5, feeds `LEARNING_SYSTEM.md`, `TEST_PLAN.md` |
| 6. Existing platform audit | What to reuse, adapt, rewrite or drop | `binance-ai-trader` source | `RESEARCH.md` §6, `DECISIONS.md` |

Verification items that cannot be completed without the live account (e.g. exact XAUUSD
specs, swap values, history depth) are listed in `KNOWN_UNKNOWNS.md` with a concrete test
for each, to be executed in Phase 3.

---

## 4. Documentation structure

All of these live in `docs/` and are the source of truth. Code requirements must trace to
them; conflicts stop implementation until resolved.

| File | Purpose | Phase created | Owner approval needed |
| --- | --- | --- | --- |
| `00_PROJECT_BRIEF.md` | This file: understanding, blocking questions, plan, doc map | 1 | — |
| `RESEARCH.md` | Findings, sources, confidence levels (Phase 1 deliverable) | 1 | Phase 1 gate |
| `KNOWN_UNKNOWNS.md` | Open questions and how each will be resolved | 1 (seeded), updated always | — |
| `PRD.md` | Goals, non-goals, users, measurable success criteria | 2 | Phase 2 gate |
| `ARCHITECTURE.md` | Components, data flow, deployment, failure handling | 2 | Phase 2 gate |
| `AGENTS.md` | Each bot's role, skills, permissions, inputs, outputs, escalation | 2 | Phase 2 gate |
| `MT5_EXNESS_INTEGRATION.md` | Verified integration details and instrument specs | 2 (draft), 3 (verified on account) | Phase 3 gate |
| `LOCAL_LLM.md` | Model selection, hardware sizing, performance targets, fallback behaviour | 2 | Phase 2 gate |
| `LEARNING_SYSTEM.md` | How bots learn, how changes are tested, promoted, rolled back | 2 | Phase 2 gate |
| `RISK_POLICY.md` | Configurable limits, kill switches, incident response | 2 | Phase 2 gate |
| `TEST_PLAN.md` | Backtesting, walk-forward, demo trading, live-readiness criteria | 2 | Phase 2 gate |
| `DECISIONS.md` | Key technical decisions and reasons (ADR style, append-only) | 1 | — |
| `GLOSSARY.md` | Trading and system terms | 2 | — |

Conventions used in all docs:

- Claim labels: **Verified** (link, accessed YYYY-MM-DD), **Assumption** (with rationale),
  **Unknown** (with proposed test).
- Decision IDs `D-nnn` in `DECISIONS.md`; open items `KU-nnn` in `KNOWN_UNKNOWNS.md`;
  requirements `R-nnn` in `PRD.md`; risks `RK-nnn` in `RISK_POLICY.md`. Code and tests
  reference these IDs in comments and commit messages.

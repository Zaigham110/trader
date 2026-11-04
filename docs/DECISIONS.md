# Decisions Log

Append-only. Each entry: ID, date, status (Proposed / Accepted / Superseded), decision,
reasons, alternatives considered, consequences. "Accepted" requires owner approval or is a
recorded default the owner has not overridden (marked "Default").

| ID | Date | Status | Decision |
| --- | --- | --- | --- |
| D-001 | 2026-11-04 | Default | New product is standalone; existing `binance-ai-trader` is a reference, not a base. |
| D-002 | 2026-11-04 | Default | Phase gates: no code until Phase 2 docs approved; each phase ends with owner sign-off recorded here. |
| D-003 | 2026-11-04 | Default | Claim-labelling convention (Verified / Assumption / Unknown) applies to all docs. |
| D-004 | 2026-11-04 | Proposed | Core language: Python (see brief Q7). Port patterns from the TypeScript platform, not code. |
| D-005 | 2026-11-04 | Proposed | Hosting topology: Windows host for MT5 + execution gateway; Linux GPU host for LLMs/analytics/chat (see brief Q3). |
| D-006 | 2026-11-04 | Proposed | LLM role: proposals only. Risk engine and execution engine are deterministic and own all orders. Permissions enforced in code. |
| D-007 | 2026-11-04 | Proposed | Risk limits, permissions and safety rules live in an owner-only config layer; bots have no write path to it. |
| D-008 | 2026-11-04 | Proposed | Martingale, grid averaging and any averaging-down logic are excluded by policy unless the owner explicitly approves after a risk review. |
| D-009 | 2026-11-04 | Proposed | All performance is reported net of spread, commission, swap and modelled slippage; gross figures are never shown alone. |
| D-010 | 2026-11-04 | Proposed | LLM stack is local-first (Qwen3.5-9B / Qwen3.8-27B / 122B-class MoE) behind one OpenAI-compatible provider interface with decode-time JSON-schema enforcement; API fallback off by default and limited to redacted, non-account research. |
| D-011 | 2026-11-04 | Proposed | Account type for automation: Raw Spread or Zero, chosen after two weeks of live spread sampling; Pro avoided unless the account currency enables market execution. |
| D-012 | 2026-11-04 | Proposed | Learning pipeline: append-only trial ledger; offline gate (CPCV, PBO ≤ 0.05, DSR ≥ 0.95, BH at FDR 10%) → demo → shadow → limited live → full, with anytime-valid champion/challenger tests and automatic rollback. Numeric gates are defaults pending Phase 2 review. |
| D-013 | 2026-11-04 | Proposed | Strategy library starts with trend / time-series-momentum variants plus a volatility-targeting overlay; session and news hypotheses enter only as pre-registered experiments. |
| D-014 | 2026-11-04 | Proposed | Cost model is clock-aware (session-dependent spread multipliers); new entries blocked around 08:30 ET releases, FOMC, the LBMA fix starts and the daily break. |
| D-015 | 2026-11-04 | Proposed | LLM news-interpretation is evaluated only on data after the model's training cutoff; trading gates on deterministic surprise features, the LLM is a validated extractor. |

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

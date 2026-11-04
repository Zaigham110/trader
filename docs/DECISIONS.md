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

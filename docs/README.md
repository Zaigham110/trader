# docs/

Source of truth for the AI Gold Trader project. Start with `00_PROJECT_BRIEF.md`.

| File | Status (2026-11-04) |
| --- | --- |
| `00_PROJECT_BRIEF.md` | Phase 1: understanding, blocking questions (answered, see DECISIONS), research plan, doc map |
| `RESEARCH.md` | Phase 1 deliverable, approved: findings with Verified / Assumption / Unknown labels |
| `research/01..05_*.md` | Full research appendices with sources and access dates |
| `PRD.md` | Phase 2 draft: goals, non-goals, users, requirements R-nnn, success criteria S-n |
| `ARCHITECTURE.md` | Phase 2 draft: gateway + brain on one Windows PC, SQLite, data flow, deployment, failure handling |
| `AGENTS.md` | Phase 2 draft: bot roster, schemas, permissions matrix, escalation |
| `MT5_EXNESS_INTEGRATION.md` | Phase 2 draft: verified Exness/MT5 facts, Standard-account specifics, 16 on-account tests for Phase 3 |
| `LOCAL_LLM.md` | Phase 2 draft: provider layer, Anthropic-first (D-019), model tiering (D-025), fallback ladder |
| `LEARNING_SYSTEM.md` | Phase 2 draft: trial ledger, offline gate, demo/shadow/limited-live promotion, monitors, rollback |
| `RISK_POLICY.md` | Phase 2 draft: normal limits, owner exception profile (D-017), kill switch, safe responses, risk register |
| `TEST_PLAN.md` | Phase 2 draft: test levels, backtest standard, phase exit gates, CI |
| `GLOSSARY.md` | Phase 2 draft |
| `PHASE7_RUNBOOK.md` | Owner's guide for demo trading (daily/weekly cadence, exit criteria) |
| `WINDOWS_SETUP.md` | Complete Windows installation steps, in order, with expected results |
| `DECISIONS.md` | D-001 to D-026; owner decisions of 2026-11-04 are D-016 to D-023 |
| `KNOWN_UNKNOWNS.md` | KU-001 to KU-022 with resolution method and deadline |

Phase gate: Phase 3 (data pipeline, MT5 integration, backtesting) starts only after the owner approves
the Phase 2 documents. Code requirements must trace to R-nnn / D-nnn / KU-nnn IDs.

# TEST_PLAN — backtesting, walk-forward, demo trading, live-readiness

Status: Phase 2 draft. Last updated: 2026-11-04. Covers PRD success criteria S-1 to S-8.

## 1. Test levels

| Level | Tooling | What it proves |
| --- | --- | --- |
| Unit | pytest, hypothesis (property tests) | Risk engine invariants, sizing math in lots/points, session state machine incl. DST, cost model, schema validation, redaction |
| Static safety | pytest over the repo | Only `execution` calls `order_send`; brain has no import path to `MetaTrader5` or `execution`; no secret patterns in logs from a full test run; bots' tools ⊆ allow-list |
| Integration (fake MT5) | `FakeMT5` double reproducing retcodes, partial fills, disconnects, close-only windows | Execution engine paths, reconciler repairs, kill switch, pre-break flatten |
| Integration (fake LLM) | `FakeProvider` scripted turns, invalid JSON, timeouts, refusals | Bot loop, repair, fallback ladder, breaker, budget |
| Backtest | in-house engine on Exness tick history | Strategy evidence under the offline gate |
| Eval | promptfoo against Anthropic | Prompt/model quality and latency gates |
| Demo | Exness demo accounts (50 USD and 5,000 USD profiles) | Real terminal behaviour, hours, slippage lower bound, uptime |
| Shadow | live quotes, orders logged only | Fill-quality assumptions vs reality |
| Limited live | 50 USD under D-017 | Functional proof under the exception |

## 2. Backtesting standard (Phase 3)

- Data: Exness Personal Area tick CSV resampled to 1 s and 1 m with bid and ask kept separately;
  MT5 M1 as cross-check; first available timestamp recorded (KU-003).
- Fills: entries at ask/bid of the decision second plus slippage allowance; stops filled at the worse
  of level and next quote; session and break rules identical to production; no trading in CLOSED or
  buffers; spread model by hour-of-day percentile, stress at 1.5× and 2×.
- Costs: spread paid, slippage, swap = 0 (intraday), commission = 0 (Standard); a 0.01-lot minimum and
  the chosen account profile applied.
- Look-ahead guards: features use only bars with close time ≤ decision time; a shift test (lag every
  feature by one bar) must not change the P&L sign dramatically; event timestamps are release times.
- Outputs per run: trade list, equity curve, metrics (net return, Sharpe, Sortino, Calmar, max DD, worst
  day, CVaR 99%, profit factor, expectancy in USD/oz vs cost per oz, turnover), regime-bucketed table,
  trial-ledger row.

## 3. Walk-forward and validation (Phase 3, then every proposal)

CPCV (N = 6, k = 2, purge and embargo), rolling walk-forward as a sanity plot, PBO, DSR, Harvey–Liu
haircut, permutation test, Monte Carlo drawdown distribution, parameter plateau test (±25% and ±50%
of each parameter), DST-shift sensitivity (±1 h), bull/bear/range window breakdown. Thresholds in
`LEARNING_SYSTEM.md` §3.2.

## 4. Phase gates

### Phase 3 exit (data, MT5, backtesting)
- Code delivered on Linux against `FakeMT5` (D-028): config, types, MT5 layer, session clock,
  data pipeline, backtest engine, validation statistics, offline gate, trial ledger, CLI, with the
  full pytest suite green. Items below that need the terminal run on the owner's PC.
- The 16 on-account tests `MT5-T01..T16` pass on demo and are recorded in `MT5_EXNESS_INTEGRATION.md`.
- 7-day data soak: ≥ 99.5% of expected 1 s samples in OPEN sessions; 1 m bars match `copy_rates` M1
  within tolerance; session state transitions match the published schedule.
- Backtester reproduces a hand-computed 10-trade sample exactly; look-ahead shift test passes.
- At least one strategy bundle has completed the offline gate (pass or fail is acceptable; the
  machinery is what is tested).

### Phase 4 exit (bots and risk engine)
- Risk engine property test (≥ 20,000 cases) passes for both profiles; static safety tests pass.
- Fake-MT5 integration: every failure path in `ARCHITECTURE.md` §5 has a test.
- End-to-end on demo: 10 labelled test trades through proposal → gates → intent → order → fill →
  journal → Telegram, with correct costs and risk % shown.

### Phase 5 exit (LLM and chat)
- Eval sets built; baseline agreement and latency recorded for Haiku/Sonnet/Opus; model tiering (D-025)
  confirmed or changed by the owner with the numbers.
- Chat acceptance: 20 scripted questions with correct provenance; 5 change commands blocked without
  confirmation; 3 confirmed commands executed through the correct gate; 0 secrets in transcripts.
- Budget breach drill: AI calls stop, safety continues.

### Phase 6 exit (learning)
- One hypothesis travels the whole pipeline on demo with ledger rows, gate results, stage transitions
  and a forced rollback demonstrated (S-6).
- Attempted write to the owner section by the learning module fails in a test.

### Phase 7 exit (demo trading, 4–8 weeks)
- Uptime ≥ 99% of OPEN hours; zero positions without a broker-side stop beyond one reconcile cycle;
  zero unhandled crashes; all incidents acknowledged.
- Journal completeness 100%; net P&L reconciles to deal history within 0.01 USD per trade.
- Performance report net of costs, regime-bucketed, with the honest statement that 4–8 weeks cannot
  establish an edge (Lo 2002 arithmetic in RESEARCH §5.5).
- Owner review and explicit approval for Phase 8.

### Phase 8 entry (limited live under D-017)
- Owner confirms the exception profile and the `ALLOW_LIVE` phrase.
- First week: one position at a time, 0.01 lot, entries only in London/NY overlap hours, no trading on
  NFP/CPI/FOMC days; daily owner summary.
- Live fills within 2 × modelled slippage; any risk-rule violation → kill and stop the phase.
- Success is functional (S-8); profitability is explicitly not a criterion at this capital.

## 5. Regression and CI

Every commit: unit + static + fake integration suites (< 5 min). Nightly: backtest smoke on a fixed
data snapshot (results hash compared), eval suite on the fixed sets (agreement and latency gates).
Deploy script refuses to restart services if the suites fail.

# LEARNING_SYSTEM — how bots learn, how changes are tested, promoted and rolled back

Status: Phase 2 draft. Last updated: 2026-11-04. Implements R-030 to R-035; decisions D-012, D-015.
All numeric gates are **Assumptions** with rationale from `research/04_learning_and_validation.md`;
they live in the owner-only config section and are not learnable.

## 1. What is recorded (R-030, R-031)

- **Trade journal:** proposal (full LLM output and inputs hash), gate results, risk verdict with code,
  intent, order request and result, fills (requested vs filled price, slippage), position lifecycle,
  closed trade with costs (spread paid at entry and exit, slippage, swap = 0, commission = 0), and a
  market snapshot (bid/ask/spread, ATR14, realised vol percentile, regime label, session state, minutes
  to next event, GVZ if available).
- **Trial ledger (append-only):** one row per backtest run, parameter set, prompt version, model change
  or hypothesis evaluation: `bundle_id, parent_bundle_id, change_type (param|rule|prompt|model),
  description, data_snapshot_id, cost_model_id, n_trades, sharpe, t_days, skew, kurtosis, pbo, dsr,
  haircut_sr, created_by (bot|owner), status`. Rows are never deleted; the quarterly "reset" only
  changes which rows count toward N for the Deflated Sharpe Ratio.
- **Bundle:** immutable set `{code_commit, strategy_params, prompt_hashes, model_ids, feature_pipeline
  version, config_hash}` with an id stamped on every proposal, order and fill (R-033).

## 2. What may be learned, and what may not (R-034)

| Learnable through the pipeline | Never learnable |
| --- | --- |
| Strategy parameters (ATR multiples, lookbacks, thresholds) within configured ranges | Risk limits, ceilings, kill-switch triggers |
| Strategy rules (entry/exit logic variants from the library) | Instrument list, session calendar, news windows |
| Prompt versions (evaluated on extraction accuracy, not P&L) | Bot permissions, tool allow-lists |
| Model tier assignment per bot (within budget) | Promotion gates and sample-size minimums |
| Session filters as pre-registered hypotheses | Credentials, logging, audit |

Enforcement: the owner section of config is hash-checked at startup and on every order; the learning
module has no code path that writes to it; a test attempts the write and must fail.

## 3. Pipeline (R-032)

```
hypothesis (Research bot, weekly, ≤10) -> trial ledger
  -> OFFLINE GATE (backtester, costs, CPCV, PBO, DSR, haircut, permutation, BH)
  -> DEMO stage (demo account, real-time)           >= 60 trading days and >= 100 trades
  -> SHADOW stage (live quotes, orders logged only)   >= 30 trading days and >= 50 trades
  -> LIMITED LIVE (25% size; at 0.01-lot minimum this means alternate trades or coin-flip allocation,
     see §3.4)                                       >= 90 days and >= 100 trades
  -> FULL (becomes champion)
Each stage: entry gate, exit gate, demotion triggers, automatic rollback to the previous champion.
```

### 3.1 Hypothesis intake

Single change only; pre-registered metric and regime scope; rationale must not cite dated outcomes
inside the evaluation window (code flags; Review bot checks). Max 10 per week keeps the trial count
compatible with the minimum backtest length (with ~5 years of data, about 45 independent trials at
E[max SR] = 1 is the paper's bound; we cap at 100–150 trials a year and accept lower targets).

### 3.2 Offline gate (all must pass)

- Costs at 1.5 × measured hourly spread percentiles plus slippage allowance; sessions and buffers as in
  production; no fixed spread.
- Combinatorial purged cross-validation, N = 6 groups, k = 2 test groups, purge = label horizon,
  embargo = 1% of sample; report the distribution of out-of-sample Sharpe across paths.
- Median CPCV OOS Sharpe ≥ 0.8 after costs; Probability of Backtest Overfitting ≤ 0.05; Deflated
  Sharpe Ratio ≥ 0.95 with N = all ledger rows since the last quarterly reset; Harvey–Liu haircut
  Sharpe ≥ 0.5; minimum backtest length satisfied; signal-permutation test p < 0.05 against a
  random-timing null with equal trade count and holding time.
- Benjamini–Hochberg at FDR 10% across the week's batch; Bonferroni for the single candidate that
  advances.
- Anything involving LLM news interpretation is evaluated only on data after the model's training
  cutoff (D-015); otherwise the result is informational and cannot pass the gate.

### 3.3 Demo and shadow gates

Demo: ≥ 60 trading days and ≥ 100 trades; bootstrap 95% CI of expectancy after costs has lower bound
> 0; zero risk-rule violations; latency within budget. Shadow: ≥ 30 trading days and ≥ 50 trades;
measured slippage ≤ 1.25 × cost model; signal-time to order-time drift < 1 bar; a sequential e-value
test versus the champion starts here (paired per-day returns, winsorised).

### 3.4 Limited live and promotion

With a 0.01-lot minimum, "25% size" is implemented as allocation: the challenger takes every fourth
qualifying signal (deterministic hash of proposal id), the champion the rest; both are logged against
the same quotes. ≥ 90 days and ≥ 100 trades; promote when e-value > 20 (α = 0.05) or the CI criterion
holds, and no pause event occurred; reject when the reverse e-process exceeds 20; stop after 6 months.

### 3.5 Rollback and demotion

Automatic: any production monitor pause (§4) within 30 days of a promotion reverts to the previous
champion bundle; schema or guard failure reverts immediately; a provider model-string change re-runs
shadow. Rollback is a bundle pointer change plus alert; nothing is deleted.

## 4. Production monitors (strategy decay)

| Monitor | Default | Action |
| --- | --- | --- |
| CUSUM on per-trade R | k = 0.5σ, h = 4σ from the offline distribution | Pause |
| Rolling Sharpe with Lo bands | window 100 trades; live below backtest lower band for 2 windows | Pause |
| Drawdown vs Monte Carlo | live DD > 95th percentile of permuted trade order | Pause; > 99th → Retire |
| Hit-rate p-chart | lower 3σ limit on n ≥ 50 | Warn |
| Fill-quality drift | mean slippage > 1.5 × model over 50 trades | Pause (execution review) |
| Bayesian change-point | hazard 1/200 trades; P(run < 20) > 0.5 | Warn, review |

Pause = no new entries for that bundle; Retire = removed from the roster, needs a fresh cycle.
Un-pausing is never automatic; the owner confirms via Telegram.

## 5. Prompt and model changes

Prompts are registered data with hashes. A prompt edit is a new bundle version evaluated on the eval
sets (`LOCAL_LLM.md` §7), never on P&L. LLM-as-judge is used for triage only, never as a gate.
A model-tier change for a bot follows the same path.

## 6. Post-trade review feedback

Code computes attribution (signal P&L at mid, timing, slippage, costs); the Review bot writes the
narrative and lessons. Lessons influence the Strategy prompt (latest 5) but never parameters.
Counterfactuals (no-stop, different sizing) are reported only when n ≥ 200.

## 7. Cadence

Weekly hypotheses; monthly summary of the ledger to the owner; quarterly batch re-estimation of
parameters through the pipeline; drift monitors decide when to trigger, never what to change.

## 8. Implementation status (Phase 6, 2026-11-05)

Owner-only gates live in `owner.learning` (D-045). Tables: `champions`, `experiments`,
`experiment_events` (append-only), `shadow_trades`, `monitor_events`, `bundle_state`,
`allocation_log` (migration 008). The decision cycle accepts a router that stamps each proposal with
the champion or, during limited live, the challenger bundle by deterministic allocation (D-047). The
weekly research job runs on Sundays; the learning job (intake → offline gate → stage advancement →
monitors → rollback) is wired in the brain once the pipeline module lands. Numeric defaults are the
§3 values and remain Assumptions until demo data exists.

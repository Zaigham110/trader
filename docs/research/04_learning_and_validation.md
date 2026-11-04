# Safe self-improvement for a trading system: research notes

Prepared 2026-11-04. Labels: **Verified** = checked today against the linked primary source; **Assumption** = reasoned default or recollection not re-verified today; **Unknown** = could not establish. Sources are listed at the end with access dates. Nothing here is investment advice.

---

## 1. Overfitting and backtest overfitting

### 1.1 Summary table

| Method | What it detects | Inputs | Practical threshold | Main limitation |
|---|---|---|---|---|
| Probability of Backtest Overfitting (PBO, CSCV) | Whether the IS-optimal configuration is likely to rank below median OOS | Matrix of T periods x N trial returns | Reject if PBO > 0.05 (paper's "customary" Neyman-Pearson suggestion) | Needs all trials recorded; ranks, not magnitudes; symmetric splits break path dependence (drawdown) |
| Deflated Sharpe Ratio (DSR) | Whether the best-of-N Sharpe clears the expected maximum under the null, after non-normality | N, variance of trial SRs, T, skew, kurtosis | DSR (a probability) >= 0.95 | Needs N and SR variance across trials; trials assumed roughly exchangeable |
| Minimum Backtest Length (MinBTL) | Whether sample length is long enough for N trials | N, target E[max SR] | MinBTL < 2 ln N / E[max]^2 years; necessary, not sufficient | Assumes independent trials; a bound, not a test |
| Harvey-Liu haircut / HLZ hurdle | Multiple-testing inflation of t-stat / SR | SR, T, number of prior tests, method (Bonferroni/Holm/BHY) | t > 3.0 for new factors (HLZ); haircut is non-linear | Number of prior tests is a judgement call |
| CPCV + purge + embargo | Leakage between train/test from overlapping labels; path dependence of a single split | Label horizons, N groups, k test groups | Report distribution of OOS SR across paths, not one number | Compute cost; still vulnerable to feature-engineering leakage |
| Walk-forward (anchored/rolling) | Temporal generalisation along one historical path | Window sizes | Compare WF SR to IS SR; expect degradation | One path only; easy to overfit the window choice; low power |
| White RC / Hansen SPA | Data-snooping across many rules vs a benchmark | Loss differentials, bootstrap | p < 0.05 after bootstrap | RC loses power with many poor rules; SPA fixes partly |
| Trade-order permutation / bootstrap CI | Whether equity-curve features (drawdown, streaks) are consistent with the trade distribution; CI on SR/expectancy | Per-trade P&L | 5th percentile of permuted max DD as risk budget; CI excludes 0 | Permutation destroys autocorrelation; tests luck-in-sequence, not alpha existence |

### 1.2 PBO and CSCV

**Verified** (Bailey, Borwein, López de Prado, Zhu, "The Probability of Backtest Overfitting", SSRN 2326253, J. Computational Finance 20(4), 2017; eScholarship copy, accessed 2026-11-04). Definition 2.2: the PBO is the probability that the strategy with optimal in-sample (IS) performance ranks below the median out-of-sample (OOS). Combinatorially Symmetric Cross-Validation (CSCV) procedure, from the paper:

1. Build matrix M of T rows (time) by N columns (trial configurations) of returns, aligned to a common index.
2. Partition rows into an even number S of disjoint sub-matrices of equal size.
3. Form all C(S, S/2) combinations of S/2 sub-matrices as the training set J; the complement is the test set. The paper gives the S=16 example (C(16,8) = 12,870 combinations; the paper's text prints "12,780").
4. For each combination, rank the N trials IS and OOS; find the IS-best n*, compute its OOS relative rank w = rank/(N+1) and the logit lambda = ln(w/(1-w)).
5. PBO = fraction of combinations with logit <= 0.

The paper states that "a customary approach would be to reject models for which PBO is estimated to be greater than 0.05" and lists three further outputs: performance degradation (regression of OOS on IS SR, typically negative slope), OOS probability of loss, and stochastic dominance of the selected strategy's OOS distribution over the pool. **Assumption**: S between 10 and 16 is the practical range; larger S explodes combinations while small S gives few logits. **Limitation (Verified from paper's own remark)**: ordering of sub-matrices does not matter for SR but does matter for path-dependent metrics such as drawdown.

### 1.3 Deflated Sharpe Ratio

**Verified** (Bailey & López de Prado, "The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting, and Non-Normality", J. Portfolio Management 40(5): 94-107, 2014, SSRN 2460551, accessed 2026-11-04): the DSR corrects the SR for the number of trials, the variance of trial SRs, sample length, skewness and kurtosis. The expected maximum SR of N trials under a zero-true-SR null is **Verified** from the companion paper (Bailey et al., "Pseudo-Mathematics and Financial Charlatanism", Notices AMS 2014, eq. 2.4, accessed 2026-11-04):

E[max_N] ~= sqrt(V[SR]) * [ (1-gamma) Z^-1(1 - 1/N) + gamma Z^-1(1 - 1/(N e)) ],  gamma ~= 0.5772, with upper bound sqrt(2 ln N).

**Assumption** (formula recalled, not re-verified today against the JPM text): DSR = Z[ (SR_hat - SR*) sqrt(T-1) / sqrt(1 - g3 SR_hat + (g4-1)/4 SR_hat^2) ], where SR* = E[max_N] above, g3 = skew, g4 = kurtosis, Z the normal CDF. Procedure: log every trial's SR (including abandoned ones), compute N and V[SR] across trials, compute SR*, then DSR for the selected strategy. Accept only if DSR >= 0.95. The practical difficulty is honest trial accounting: an LLM that "tries ideas" in a loop must write every attempt to an append-only trial ledger or N is understated.

### 1.4 Minimum backtest length

**Verified** (Pseudo-Mathematics paper, Theorem 3.1, accessed 2026-11-04): MinBTL (years) ~= [E[max_N] expression]^2 / E[max_N]^2 < 2 ln N / E[max_N]^2. Worked example in the paper: "if only 5 years of data are available, no more than 45 independent model configurations should be tried, or we are almost guaranteed to produce strategies with an annualized Sharpe ratio IS of 1, but an expected Sharpe ratio OOS of zero." The paper calls MinBTL "a necessary, non-sufficient condition". Dependent trials should be reduced to an effective N (the paper suggests PCA).

### 1.5 Multiple testing: HLZ hurdle and Harvey-Liu haircut

**Verified** (Harvey, Liu, Zhu, "... and the Cross-Section of Expected Returns", Review of Financial Studies 29(1): 5-68, 2016, accessed 2026-11-04): because hundreds of factors have been tested, a newly proposed factor should clear t > 3.0 rather than 2.0, and "most claimed research findings in financial economics are likely false".

**Verified** (Harvey & Liu, "Backtesting", J. Portfolio Management 41(1): 13-28, Fall 2015, Duke PDF accessed 2026-11-04): three adjustments, Bonferroni (p_adj = M p), Holm (step-down), and BHY (Benjamini-Hochberg-Yekutieli, FDR control). Worked example from the paper: with T = 240 months, an annual SR of 0.75 has single-test p = 0.0008; with N = 200 tests the adjusted p is 0.15, giving a haircut SR of 0.32, a roughly 60% haircut. "We argue that it is a serious mistake to use the usual 50% haircut"; the haircut is non-linear: marginal SRs are hit hardest, very high SRs only moderately. **Assumption for our use**: treat every LLM-proposed variant evaluated in a week as one test; apply BH (Benjamini & Hochberg 1995, JRSS-B 57(1): 289-300, **Verified** citation) at FDR 5-10% across the week's batch, and Bonferroni for anything promoted to live.

### 1.6 Purging, embargo, CPCV, walk-forward

**Verified** (temporalcv docs cite López de Prado, *Advances in Financial Machine Learning*, Wiley 2018, ch. 7 for purged/embargoed K-fold; skfolio implements `CombinatorialPurgedCV`; accessed 2026-11-04). Purging removes training observations whose label window overlaps any test observation's label window; embargo drops a further buffer after each test block to kill serial-correlation leakage. **Assumption** (from the book, not re-verified today): CPCV with N groups and k test groups yields phi = (k/N) C(N,k) backtest paths (N=6, k=2 gives 5 paths), so you obtain a distribution of OOS Sharpe ratios rather than one number; the book argues walk-forward tests a single path, is easily overfit through window choices, and has low power. Walk-forward remains useful as a sanity check and for parameter stability plots. Anchored WF (growing window) suits slowly drifting regimes; rolling WF suits regime-switching, but each adds a free parameter (window length) that is itself a trial and must be counted in N. Embargo size: **Assumption** ~1% of T or at least the maximum feature lookback plus label horizon.

### 1.7 Reality Check and SPA

**Verified** citations: White, "A Reality Check for Data Snooping", Econometrica 68: 1097-1126 (2000); Hansen, "A Test for Superior Predictive Ability", J. Business & Economic Statistics 23: 365-380 (2005), accessed 2026-11-04. Both test the null that the best of many rules does no better than a benchmark, using the stationary bootstrap of the loss-differential series. Hansen's abstract states SPA "is more powerful and less sensitive to poor and irrelevant alternatives" than the RC, because it studentises and recentres to exclude clearly inferior rules. **Procedure (Assumption on details)**: compute d_k,t = loss_benchmark,t - loss_rule_k,t; statistic = max_k sqrt(T) mean(d_k); bootstrap (block length ~ sqrt(T)) the centred statistics; p-value = share of bootstrap maxima exceeding observed. Use when the LLM has generated a family of rules against a passive benchmark. **Limitation**: both need the full universe of rules tried, and both lose power quickly when the family is large and mostly junk (RC more so).

### 1.8 Monte Carlo permutation and bootstrap CIs

**Verified** that Masters' *Permutation and Randomization Tests for Trading System Development* (2020) covers overfitting detection, skill-vs-luck, and drawdown bounds (publisher listings, accessed 2026-11-04); details below are **Assumption** (standard practice). (a) Shuffle trade order 10,000 times; use the 95th percentile of simulated max drawdown as the drawdown budget. (b) Bootstrap trades (or stationary-block bootstrap returns) for CIs on expectancy and SR; require the 5% lower bound of expectancy after costs > 0. (c) Permute the *signal* relative to prices (random entry offsets, block-shuffled signal series) to test against a random-timing null with the same trade count and holding time. Only (c) tests whether alpha exists; (a) tests whether the sequence was unlucky.

---

## 2. Data leakage in trading ML

| Leak | Mechanism | Detection | Mitigation |
|---|---|---|---|
| Look-ahead in features | Using the current bar's close, high, low, or volume for a decision executed at that bar's open | Shift test: lag every feature by one bar; a big P&L collapse proves leakage | Build features only from bars with close_time <= decision_time; assert in code |
| Label leakage | Labels (e.g., triple-barrier outcome) computed with data overlapping the next sample's features | Purged CV gap test; correlation of label with "future" features | Purge + embargo; non-overlapping labels or sample weights by uniqueness (**Assumption**, AFML ch. 4) |
| Indicator warm-up / centred filters | Zero-phase or centred smoothing, repainting indicators | Recompute on truncated history and compare | Causal filters only; recompute indicator from scratch on each as-of snapshot in tests |
| Calendar/news timestamps | Using the *publication* time of a revised figure, or the event date without release time/timezone | Check that P&L concentrates in the minute after releases | Store release_time_utc separately from event_date; use first-print values (ALFRED "initial release only", **Verified** FRED/ALFRED docs, accessed 2026-11-04) |
| Broker history revisions | Broker backfills/corrects bars; tick history differs from what was streamed live | Keep a hash of each bar at first receipt; diff against later downloads | Record live bars to an immutable store and backtest on *that*, not re-downloaded history |
| Survivorship | Less relevant for a single symbol; still relevant for strategy selection (dead strategies vanish from the ledger) | Count of abandoned trials vs. recorded | Append-only trial ledger |
| LLM parametric look-ahead | The model has read what happened to gold/XAUUSD in 2023 and "interprets" 2023 news accordingly | Sarkar-Vafa style tests (see below); regime-split alpha decay | Post-cutoff evaluation windows; chronologically consistent models; masking (partial) |

### 2.1 LLM-specific look-ahead bias

**Verified** (Sarkar & Vafa, "Lookahead Bias in Pretrained Language Models", NBER conference draft dated 28 June 2024; first version 11 March 2024; ICML 2025 workshop; accessed 2026-11-04). Definition: "This bias arises when a language model's pre-training data contains information about the future, which then leaks into analysis that should only use information from the past." They propose tests based on events that are unpredictable given a prespecified information set, and find lookahead bias in predicting risk factors from earnings calls and election winners from biographies. Specific results: with Llama 2 (base cutoff September 2022), pandemic-related language appears in generated risk lists for pre-COVID (2019) calls at 2.2% and rises for 2020 calls, i.e., the model injects known future events; a no-lookahead instruction in the prompt did not remove the most obvious symptom; and masking is weak: the model re-identified the firm "with 61% accuracy from a call segment with the name and products censored", with accuracy increasing with prompt length. Their conclusion: "As of now, there is no statistical framework that suggests that prompt design will eliminate lookahead bias"; the fix is "language models whose pretraining cutoff dates lie before, but only shortly before, the analysis period".

**Verified** (Glasserman & Lin, "Assessing Look-Ahead Bias in Stock Return Predictions Generated By GPT Sentiment Analysis", arXiv 2309.17322, 2023, accessed 2026-11-04): 129,431 headlines; they anonymise company identifiers and find in-sample that anonymised headlines actually perform better, attributing it to a "distraction effect" that outweighs look-ahead; out-of-sample look-ahead is not a concern but distraction remains. **Verified** (Lopez-Lira & Tang, "Can ChatGPT Forecast Stock Price Movements?", arXiv 2304.07619, 2023): ChatGPT headline scores predict next-day returns, sample Oct 2021-Dec 2022, i.e., partly post-cutoff.

Newer, directly relevant work (all **Verified** abstracts, accessed 2026-11-04):
- He, Lv, Manela, Wu, "Chronologically Consistent Large Language Models", arXiv 2502.21206 (2025): ChronoBERT/ChronoGPT trained only on text available at each point in time; in a news-to-returns test the bias is modest.
- Yan, Tang, Gao, Jiang, Lu, "DatedGPT: Preventing Lookahead Bias in LLMs with Time-Aware Pretraining", arXiv 2603.11838 (2026): twelve 1.3B models with year-end cutoffs 2013-2024; models trained through the outcome period show a "lookahead premium of 26.4 basis points per standard deviation, significant at the 1% level".
- Benhenda, "Look-Ahead-Bench", arXiv 2601.13770 (2026): standard LLMs (Llama 3.1, DeepSeek 3.2) show significant look-ahead bias measured as alpha decay across regimes; point-in-time models do not to the same degree.
- Li, Wang, Ma, "Summoning the Oracle to Slay It", arXiv 2605.24564 (2026): FinCAD, an inference-time decoding adjustment; largest mean in-sample return correction was -67.1% across five 7-14B models and five mega-caps, with 2025 OOS results roughly unchanged, i.e., most of the in-sample "edge" was memorised.

**Implications for a gold/XAUUSD news-interpretation module (Assumption, derived):** (1) backtests of LLM news interpretation on dates before the model's training cutoff are inadmissible as evidence; evaluate post-cutoff only and record the cutoff in experiment metadata; (2) masking reduces but does not eliminate leakage, and "gold", "Fed", "CPI" are unmaskable for a single-asset system, so masked backtests are a sanity check, not a gate; (3) use perturbed tests: flip the sign of the surprise in real headlines and check the interpretation flips, and feed invented but plausible releases; (4) gate trading on deterministic surprise features (actual minus consensus, standardised) and use the LLM only for extraction that is validated against them; (5) benchmark against a chronologically consistent model (ChronoGPT/DatedGPT) for pre-cutoff periods.

---

## 3. Strategy decay and live monitoring

**Verified** (McLean & Pontiff, "Does Academic Research Destroy Stock Return Predictability?", J. Finance 71(1), 2016, accessed 2026-11-04): across 97 published predictors, returns are 26% lower out-of-sample (upper bound on data-mining bias) and 58% lower post-publication; decay is larger for predictors with higher in-sample returns. **Verified** (Novy-Marx & Velikov, "A Taxonomy of Anomalies and Their Trading Costs", RFS 29(1): 104-147, 2016): costs always reduce profitability; few strategies above 50% monthly turnover survive costs. Lesson: a discovered edge should be assumed to lose a quarter to a half of its backtest magnitude even if real.

### 3.1 Monitors (what to run, defaults are Assumptions)

| Monitor | Statistic | Default trigger | Comment |
|---|---|---|---|
| CUSUM on per-trade returns | S_t = max(0, S_{t-1} + (r_t - mu_0 + k)); Page 1954 (**Verified** citation, Biometrika 41:100-115) | k = 0.5 sigma, h = 4-5 sigma (Assumption) | Detects a persistent downward shift in mean; drift parameter from backtest expectancy |
| Rolling Sharpe with CI | SR over last n trades, band +/- 1.96 sqrt((1+0.5 SR^2)/n) (Lo 2002, **Verified**) | Live SR below the backtest SR's lower band for 2 consecutive windows | n >= 60-100 trades per window or the band is useless |
| Drawdown vs Monte Carlo | Live max DD vs permuted-trade DD distribution | Live DD > 95th percentile of simulated DD -> pause; > 99th -> retire pending review | Simulated distribution from backtest trade list after cost haircut |
| Hit rate / profit factor p-chart | Binomial control limits around backtest hit rate p0: p0 +/- 3 sqrt(p0(1-p0)/n) | Breach of lower 3-sigma limit on n >= 50 trades | Low power; useful as a cheap early warning |
| Bayesian online change-point (Adams & MacKay 2007, arXiv 0710.3742, **Verified**) | Run-length posterior on return mean/variance | P(run length < 20 trades) > 0.5 | Catches regime change; needs a hazard rate (Assumption: 1/200 trades) |
| Fill-quality drift | Live slippage vs backtest assumption; live spread vs assumed | Mean slippage > 1.5x assumption over 50 trades | Often the real reason live diverges |

### 3.2 Pause/retire rules (Assumption, derived)

- **Pause** (no new entries, keep risk controls): any of CUSUM breach, DD > MC 95th percentile, or fill-quality breach.
- **Retire** (remove from roster, require a fresh promotion cycle): DD > MC 99th percentile; or live expectancy's 95% CI upper bound < 0 after n >= 100 trades; or two pauses within 90 days.
- Never "un-pause" automatically on a return to profitability; a human review or an explicit rule reset is required, because a pause that resolves by luck re-introduces the same decayed strategy.

---

## 4. Safe self-improvement loops with LLM involvement

### 4.1 Promotion pipeline

Propose -> offline tests (CPCV, DSR, PBO, costs) -> demo/paper (real-time, simulated fills) -> shadow (real-time, real quotes, orders logged not sent) -> limited live (small size, hard caps) -> full. Each stage has entry gates (statistical), exit gates (time and sample), and automatic demotion triggers. The shadow stage is where fill assumptions and latency are measured against reality (vendor MLOps material describes shadow/champion-challenger/canary patterns; these are practitioner sources, **Verified** as existing but not peer-reviewed, accessed 2026-11-04).

### 4.2 Champion/challenger with sequential stopping

Fixed-horizon tests with repeated peeking inflate false positives. Options: Wald's SPRT (**Verified** citation: Annals of Math. Stat. 16(2): 117-186, 1945); always-valid p-values via mixture SPRT (Johari, Koomen, Pekelis, Walsh, "Always Valid Inference: Continuous Monitoring of A/B Tests", Operations Research 70(3), 2022; arXiv 1512.04922, **Verified**); and e-values / test martingales (Ramdas, Grünwald, Vovk, Shafer, "Game-Theoretic Statistics and Safe Anytime-Valid Inference", Statistical Science 38(4), 2023; arXiv 2210.01948, **Verified**), which "remain valid at all stopping times". **Procedure (Assumption)**: paired comparison of challenger minus champion per-trade (or per-day) return in shadow; run an e-process with a mixture over effect sizes; promote when e-value > 20 (alpha = 0.05), reject when the e-process for the reverse hypothesis exceeds 20, or stop at a maximum of 6 months. Because trading returns are heavy-tailed and dependent, use per-day aggregates and a bounded or winsorised statistic (Assumption).

### 4.3 Versioning and rollback

Version everything that affects a decision as one immutable bundle (code hash, parameters, prompt text, model name plus provider version and training cutoff, feature-pipeline version, data snapshot id, config) and stamp the bundle id on every order and trade. Rollback triggers (Assumption): any Section 3.2 pause within 30 days of a promotion reverts to the previous champion; any schema or guard failure reverts immediately; a provider model-string change forces a re-run of the shadow stage.

### 4.4 What must never be learnable

Risk limits (max position, max daily loss, max leverage, max orders per minute), instrument whitelist, trading-hours calendar, kill switch, permission scopes, data-retention and audit logging, and the promotion gates themselves. Enforce with (a) an immutable config file whose hash is checked at startup and on every order; (b) code-level guards in the execution layer that reject orders violating limits regardless of what the strategy or LLM requested; (c) separation of credentials so the proposing agent has no broker write permission; (d) tests that attempt violations and must fail. The learning loop may propose changes to these only through a human-approved pull request, never through its own config writes. (**Assumption**, design principle.)

### 4.5 Prompt optimisation

DSPy (Khattab et al., ICLR 2024; PyPI `dspy` 3.4.1 released 2026-10-12, **Verified**) separates program structure from prompts and optimises prompts/demos against a metric (MIPROv2 does Bayesian search over instructions and demonstrations; the "GEPA" successor is reported by third-party sources, **Unknown** whether current flagship). Risks: the metric is the whole game; if the metric is "backtest P&L on a window", the optimiser will find leakage or regime-specific prompts. Safer metrics are extraction accuracy against deterministic ground truth (e.g., did the model parse the CPI surprise sign and magnitude correctly) and calibration of stated confidence. LLM-as-judge is biased: position bias, verbosity bias, self-enhancement bias are documented (Zheng et al., "Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena", NeurIPS 2023, arXiv 2306.05685, **Verified** abstract). Do not use an LLM judge as a promotion gate; use it only for triage. Keep manual prompt versioning with a fixed eval set that is frozen before optimisation and never shown to the optimiser.

### 4.6 Guarding against fitting noise

Per week the LLM may propose many variants. Rules (Assumption, derived from Sections 1 and 7): (1) minimum sample per proposal: n >= 100 trades OOS or >= 6 months of daily returns, whichever is longer; (2) effect-size floor: OOS SR improvement >= 0.3 annualised *and* expectancy after costs > 0 with 95% CI excluding 0; (3) all proposals in the batch enter the trial ledger; apply BH at FDR 10% for the shortlist and Bonferroni for the one promoted; (4) DSR >= 0.95 using N = total trials since the last reset of the ledger (never reset more than quarterly); (5) proposals that change more than one parameter group at once are rejected (attribution impossible); (6) any proposal whose rationale references outcomes of specific dated events in the test window is flagged for look-ahead review.

### 4.7 Online vs batch retraining

Online/incremental learning adapts quickly but makes versioning, rollback and multiple-testing accounting almost impossible, and concept-drift literature (Gama et al., "A Survey on Concept Drift Adaptation", ACM Computing Surveys 46(4), 2014, **Verified** citation) treats drift detection and adaptation as separate problems. Recommendation (Assumption): periodic batch retraining (monthly or quarterly) through the same promotion pipeline, with drift monitors (Section 3) deciding *when* to trigger a retrain, not *what* to change.

### 4.8 Reinforcement learning

Surveys (Pricope, "Deep Reinforcement Learning in Quantitative Algorithmic Trading: A Review", arXiv 2106.00123, 2021; Millea, "Deep Reinforcement Learning for Trading: A Critical Survey", Data 6(11), 2021; **Verified** abstracts) report proof-of-concept settings, unrealistic assumptions, no live deployment, and lack of standard evaluation. FinRL-Meta (NeurIPS 2022, arXiv 2211.03107) itself cites low signal-to-noise, survivorship bias and overfitting as obstacles, and a FinRL-group paper on crypto (arXiv 2209.05559) explicitly adds a backtest-overfitting hypothesis test because earlier works "optimistically reported increased profits". **Unknown**: any peer-reviewed evidence of sustained real-money RL trading performance. Label: RL for position sizing or execution scheduling is plausible research; RL for signal generation on a single retail-traded symbol is not an evidence-backed path and should not be in the self-improvement loop.

---

## 5. Post-trade review and attribution

| Component | Computation | Signal-to-noise | Actionable? |
|---|---|---|---|
| Signal P&L | Theoretical P&L at mid at signal time to mid at exit signal | Medium | Yes: tells whether the idea works independent of execution |
| Timing/latency | Mid at signal time vs mid at order time | High (mechanical) | Yes: infrastructure fix |
| Execution slippage | Fill price vs mid at order time, by side | High | Yes: order type, venue, time-of-day |
| Costs | Spread paid, commission, swap/rollover | High | Yes: holding-period and session rules |
| Stop/target design counterfactuals | Replay with no stop, wider stop, different sizing | Low to medium | Only with n >= 200 trades; otherwise noise |
| Regime-conditional performance | Expectancy by volatility tercile, session, news-day flag | Medium | Yes if the regime variable is pre-registered; no if discovered after the fact |
| Trade clustering | Autocorrelation of trade outcomes; losses within the same hour/day | Medium | Yes: cap concurrent exposure, cool-down after k losses |
| "Luck" | Bootstrap CI of expectancy; permuted DD | n/a | Framing only: prevents over-reaction |

Metrics: expectancy = p_win * avg_win - (1 - p_win) * avg_loss (in R multiples); profit factor = gross profit / gross loss; SQN = sqrt(n) * mean(R) / sd(R), with Tharp's convention of capping sqrt(n) at 10 for n > 100 (**Verified** via secondary sources only; primary text not accessed). SQN is a t-statistic of mean R; its bands ("good" ~2.5-3) are folklore and should not be gates. Low-noise feedback: execution, costs, latency, risk-rule compliance. High-noise feedback that should **not** feed an automated loop: per-trade "what-ifs", post-hoc regime splits, win-rate over fewer than 50 trades, and narrative explanations of individual losses by an LLM.

---

## 6. Multi-agent LLM trading systems and the skills roster

### 6.1 Literature

| System | Claim | Evaluation as reported (**Verified** from arXiv full text, accessed 2026-11-04) | Weaknesses |
|---|---|---|---|
| TradingAgents (Xiao, Sun, Luo, Wang, arXiv 2412.20138, v6 Apr 2025) | Analyst/researcher/trader/risk roles with debate; better CR, SR, MDD than rule baselines | AAPL, GOOGL, AMZN; 1 Jan-29 Mar 2024 (3 months); gpt-4o-mini, gpt-4o, o1-preview; reported SR 8.21/6.39/5.60; authors note the SR "exceeds our expected empirical range"; live deployment is future work | 3-month window; test window likely inside model training data (look-ahead); no transaction costs mentioned; ~60 decisions per ticker |
| FinMem (Yu et al., arXiv 2311.13743; AAAI Spring Symp. 2024) | Layered memory + character; beats DRL and LLM baselines | TSLA, NFLX, AMZN, MSFT, COIN; test 6 Oct 2022-10 Apr 2023; GPT-4-Turbo; baselines B&H, PPO, DQN, A2C, GA, FinGPT; TSLA CR 61.8% vs B&H -18.6% | ~6-month window, five tickers; GPT-4-Turbo's training data overlaps the test window; costs not mentioned |
| FinAgent (Zhang et al., KDD 2024, arXiv 2402.18485) | Multimodal, tool-augmented; beats 9-12 baselines on 6 datasets, one with 92.27% return | Stocks and crypto datasets | Baseline counts differ across versions; same overlap concern |
| FinRobot (Yang et al., arXiv 2405.14767) | Open platform, four layers, smart scheduler | Two demo applications | Platform paper; not a performance study |
| ai-hedge-fund (virattt, GitHub) | Investor-persona agents | README: "educational and research purposes only", not for real trading | Look-Ahead-Bench (arXiv 2601.13770) uses it as a test bed and argues such systems' performance "may be artificially inflated" |

Lessons: (1) every published result uses windows inside or overlapping the LLM's training data, so headline Sharpe ratios are not evidence; (2) windows of 3-6 months and 3-5 tickers cannot distinguish skill from luck (Section 7); (3) costs are routinely absent; (4) the useful contribution is architectural: role separation, structured outputs, explicit risk agent, and debate to surface disagreement, which maps well onto a deterministic-core design.

### 6.2 Skills roster (Assumption, design reasoning)

| Skill | Mode | Reasoning |
|---|---|---|
| Market analysis (features, regimes, stats) | Deterministic code | Reproducible, testable, leakage-controllable; LLM may *describe* results |
| News interpretation | Hybrid | LLM extracts event, direction, magnitude, confidence into a schema; code validates against consensus data and clips to allowed ranges; LLM output is a feature, never an order |
| Strategy research (hypothesis generation) | LLM-driven, sandboxed | Cheap idea generation; every idea logged to the trial ledger; cannot touch config or execution |
| Backtesting | Deterministic code | Engine, costs, CPCV, DSR, PBO all in code; LLM may call it but not alter it |
| Risk management | Deterministic code, immutable config | Must not be learnable (Section 4.4) |
| Execution | Deterministic code | Latency, idempotency, exact limits; LLM has no path to the broker |
| Post-trade review | Hybrid | Code computes attribution; LLM writes the narrative and proposes hypotheses that re-enter the pipeline as proposals |
| System health | Deterministic code with LLM triage | Alerts are rules; LLM summarises logs and suggests causes |
| Chat/explanation | LLM-driven, read-only | Explains state and decisions; cannot trade or change config |

---

## 7. Minimum evidence standards

**Verified** (Lo, "The Statistics of Sharpe Ratios", Financial Analysts Journal 58(4): 36-52, 2002, equation (9), accessed 2026-11-04): under IID returns, SE(SR_hat) ~= sqrt((1 + 0.5 SR^2) / T), where SR and T are in the same frequency; the 95% CI is SR_hat +/- 1.96 SE. Lo also shows that multiplying by sqrt(12) to annualise is valid only under IID returns and that serial correlation can overstate annual SRs by up to 65%; for non-IID returns use the autocorrelation-adjusted estimator in his Non-IID section.

Derivation of a rule of thumb (per-trade units, Assumption built on the verified formula): to show the per-trade SR_t is positive at one-sided 95% you need SR_t / SE > 1.645, i.e., n > (1.645/SR_t)^2 (1 + 0.5 SR_t^2). A strategy with annual SR 1.0 and 250 trades per year has SR_t ~= 1/sqrt(250) = 0.063, giving n > ~680 trades (about 2.7 years). With annual SR 1.5 and 250 trades/year, SR_t = 0.095, n > ~300 trades (1.2 years). With annual SR 2.0, n > ~170. On daily returns, to distinguish annual SR 1.0 from 0 at 95% requires about 2.7 years of data (same arithmetic with 252 days). Consequences:

- A 3-month demo of a plausible retail strategy (annual SR 1-1.5) cannot be statistically decisive; it can only reject gross failures (bugs, execution mismatch).
- **Default gates (Assumption)**: demo/paper >= 60 trading days *and* >= 100 trades; shadow >= 30 trading days *and* >= 50 trades with fill-quality checks; limited live >= 90 days *and* >= 100 trades before full size. Promotion to full requires live expectancy's 95% CI lower bound > 0 *or* a sequential e-test (Section 4.2) that concludes in favour, plus no pause events.
- Trials count: all demo variants count toward N in the DSR; a demo period is not a free lunch because the LLM may be running several.

---

## 8. Tooling (status checked via PyPI JSON on 2026-11-04; GitHub API was not reachable from this session, so star counts and archive flags are **Unknown**)

| Library | Latest PyPI release | Status | Use |
|---|---|---|---|
| vectorbt | 1.2.0, 2026-10-19 (**Verified**) | Active; 1.x adds optional Rust engine per release notes mirror (**Unknown** exact 1.0 date); free version alongside paid VectorBT PRO | Fast vectorised parameter sweeps; large N trial matrices for PBO |
| backtesting.py | 0.6.6, 2026-07-22 (**Verified**) | Active | Simple event-driven single-asset backtests; good for rule strategies |
| mlfinlab | Not on PyPI; "all rights reserved", commercial (QuantConnect docs cite GBP 100/month per user) (**Verified**) | Closed | Open alternatives: `mlfinpy` (MIT rewrite, baobach) and `famlafl` (fork, mixed licence) (**Verified** PyPI pages); treat as low-maintenance |
| skfolio | 1.12.5, 2026-10-29 (**Verified**) | Active | `CombinatorialPurgedCV`, portfolio optimisation, scikit-learn API |
| arch | 8.0.0, 2025-10-21 (**Verified**) | Active, slower cadence | GARCH, stationary/circular block bootstrap (useful for RC/SPA and CIs) |
| statsmodels | 0.15.0, 2026-08-27 (**Verified**) | Active | Tests, HAC standard errors, multiple-testing helpers (`multipletests` incl. BH) |
| quantstats | 0.0.86, 2026-09-27 (**Verified**) | Active | Tear sheets; metrics only |
| pyfolio-reloaded | 0.10.2, 2026-11-02 (**Verified**) | Active (Stefan Jansen fork); original pyfolio last release 0.9.2, 2019-04-15 (**Verified**, dead) | Tear sheets, round-trip analysis |
| alphalens-reloaded | 0.5.1, 2026-10-26 (**Verified**) | Active; original alphalens 0.4.0, 2020-04-27 (**Verified**, dead) | Factor IC analysis; less relevant for single symbol |
| dspy | 3.4.1, 2026-10-12 (**Verified**) | Active | Prompt optimisation under a fixed metric |
| R package `pbo` (CRAN) | exists (**Verified** README) | n/a | Reference implementation of CSCV/PBO if cross-checking a Python port |

**Assumption**: implement PBO/DSR/MinBTL in-house (a few hundred lines on NumPy) rather than depend on mlfinlab forks; cross-check against the CRAN `pbo` package.

---

## Recommended learning-system design

All numeric defaults are **Assumptions** with rationale.

1. **Trial ledger (append-only).** Every backtest run, parameter set, prompt version, and LLM proposal gets a row with SR, T, skew, kurtosis, costs model, data snapshot id, model cutoff. N for DSR is the row count since the last quarterly reset. Rationale: DSR, MinBTL, HL haircut all fail without honest N.
2. **Proposal intake (LLM).** Proposals must be single-change, pre-register the hypothesis and the regime variables they will be evaluated on, and cite no dated outcomes inside the evaluation window. Max 10 proposals/week (keeps BH power reasonable and MinBTL satisfiable with ~5 years of data: 2 ln 500 / 1^2 = 12.4 years would already be needed for 500 trials at E[max]=1, so cap annual trials at roughly 100-150 and accept lower E[max] targets).
3. **Offline gate.** Costs at 1.5x measured live spread + commission + swap. CPCV (N=6, k=2, purge = label horizon, embargo = 1% of T). Pass if: median CPCV OOS SR >= 0.8 after costs; PBO <= 0.05; DSR >= 0.95; HL haircut SR >= 0.5; MinBTL satisfied; permutation test p < 0.05 against random-timing null; news-driven components evaluated only post-model-cutoff. BH at FDR 10% across the week's batch.
4. **Demo/paper gate.** >= 60 trading days and >= 100 trades; expectancy 95% bootstrap CI lower bound > 0 after costs; no risk-rule violations; latency < budget.
5. **Shadow gate.** >= 30 trading days and >= 50 trades; measured slippage <= 1.25x the cost model; signal-time vs order-time drift < 1 bar; sequential e-test vs champion started here.
6. **Limited live.** 25% of normal size, hard daily-loss cap at 50% of normal; >= 90 days and >= 100 trades; promote on e-value > 20 or CI criterion; demote on any Section 3.2 pause.
7. **Monitoring in production.** CUSUM (k = 0.5 sigma, h = 4 sigma), rolling 100-trade SR with Lo bands, DD vs permuted 95th/99th, fill-quality drift, BOCPD with hazard 1/200. Pause and retire rules as in Section 3.2; un-pause requires explicit reset.
8. **Immutable layer.** Risk limits, instrument list, hours, kill switch, promotion thresholds in a hash-checked config; execution-layer guards; the proposing LLM has read-only credentials.
9. **Retraining cadence.** Batch, quarterly or on drift trigger; never online updates to anything that gates orders.
10. **Post-trade review loop.** Code computes attribution (signal/timing/slippage/costs); the LLM writes hypotheses that become proposals in step 2. Counterfactuals only reported when n >= 200.
11. **Prompt changes are versions.** Any prompt edit is a new bundle version and re-enters at step 3 with a frozen eval set measured on extraction accuracy, not P&L.

## Key takeaways

- Overfitting is a function of trials, not intent: log every trial, compute DSR and PBO, and respect MinBTL (5 years of data supports roughly 45 independent trials at E[max SR] = 1).
- The "50% haircut" is wrong in both directions: use Harvey-Liu multiple-testing adjustments; a marginal SR of 0.75 can become 0.32 after 200 tests.
- Single-path walk-forward is weak evidence; CPCV with purging and embargo gives a distribution of OOS outcomes.
- Lo's SE(SR) = sqrt((1 + 0.5 SR^2)/T) means a retail-scale edge (annual SR ~1) needs on the order of 2.5-3 years or several hundred trades to be distinguished from zero; short demos only catch bugs.
- LLM look-ahead bias is real, measurable, and not removed by prompting or masking (Sarkar & Vafa; DatedGPT's 26.4 bp/SD lookahead premium; FinCAD's -67% in-sample correction). Evaluate LLM news modules only post-cutoff or with chronologically consistent models.
- Published anomalies decay 26% OOS and 58% post-publication (McLean & Pontiff); plan for it.
- Costs kill high-turnover strategies (Novy-Marx & Velikov); model costs above measured.
- Monitor live vs backtest with CUSUM, Lo-band rolling SR, Monte Carlo drawdown percentiles, and fill-quality drift; pause and retire by rule, un-pause by review.
- Champion/challenger comparisons must use sequential methods (SPRT, always-valid p-values, e-values) or peeking inflates false promotions.
- Risk limits, permissions and promotion gates are not learnable; enforce with immutable config plus execution-layer guards and credential separation.
- LLM-as-judge is biased (position, verbosity, self-enhancement); never a promotion gate.
- Multi-agent LLM trading papers (TradingAgents, FinMem, FinAgent) report 3-6 month windows on a handful of mega-caps inside their models' training data with no costs; borrow the architecture, not the numbers.
- RL for signal generation has no public real-money evidence; keep it out of the loop.
- Batch retraining through the pipeline beats online learning for auditability; drift monitors decide when, not what.
- Most tooling needed (statsmodels, arch, skfolio, vectorbt, backtesting.py, pyfolio-/alphalens-reloaded, quantstats, dspy) is actively released as of Oct-Nov 2026; mlfinlab is closed-source, so implement PBO/DSR in-house.

## Sources (all accessed 2026-11-04)

- Bailey, Borwein, López de Prado, Zhu, "The Probability of Backtest Overfitting", SSRN: https://papers.ssrn.com/abstract=2326253 ; eScholarship PDF: https://escholarship.org/content/qt4w1110bb/qt4w1110bb.pdf
- Bailey & López de Prado, "The Deflated Sharpe Ratio", JPM 40(5) 2014: https://papers.ssrn.com/abstract=2460551
- Bailey, Borwein, López de Prado, Zhu, "Pseudo-Mathematics and Financial Charlatanism", Notices AMS 2014, working paper PDF: https://carmamaths.org/jon/backtest.pdf
- Harvey, Liu, Zhu, "... and the Cross-Section of Expected Returns", RFS 29(1) 2016: https://papers.ssrn.com/abstract=2513152 ; NBER w20592: https://www.nber.org/papers/w20592
- Harvey & Liu, "Backtesting", JPM 41(1) 2015: https://people.duke.edu/~charvey/Research/Published_Papers/P120_Backtesting.PDF
- Lo, "The Statistics of Sharpe Ratios", FAJ 58(4) 2002: https://traders.studentorg.berkeley.edu/papers/The-Statistics-of-Sharpe-Ratios.pdf ; CFA Institute record: https://rpc.cfainstitute.org/research/financial-analysts-journal/2002/the-statistics-of-sharpe-ratios
- White, "A Reality Check for Data Snooping", Econometrica 68 (2000): https://users.ssc.wisc.edu/~bhansen/718/White2000.pdf
- Hansen, "A Test for Superior Predictive Ability", JBES 23 (2005): https://typeset.io/pdf/a-test-for-superior-predictive-ability-1fbgz05jht.pdf
- López de Prado, Advances in Financial Machine Learning, Wiley 2018 (ch. 7 cited by temporalcv docs): https://temporalcv.readthedocs.io/en/latest/api/cv_financial.html ; skfolio CombinatorialPurgedCV: https://skfolio.org/generated/skfolio.model_selection.CombinatorialPurgedCV.html
- Masters, Permutation and Randomization Tests for Trading System Development (2020): https://www.goodreads.com/book/show/51518758-permutation-and-randomization-tests-for-trading-system-development
- Benjamini & Hochberg 1995, JRSS-B 57(1): https://www.dcscience.net/Benjamini-Hochberg-1995-FDR.pdf
- Sarkar & Vafa, "Lookahead Bias in Pretrained Language Models" (2024): https://conference.nber.org/conf_papers/f205530/f205530.pdf ; ICML 2025 workshop listing: https://icml.cc/virtual/2025/48685
- Glasserman & Lin, arXiv 2309.17322: https://arxiv.org/abs/2309.17322
- Lopez-Lira & Tang, arXiv 2304.07619: https://arxiv.org/abs/2304.07619
- He, Lv, Manela, Wu, "Chronologically Consistent Large Language Models", arXiv 2502.21206: https://arxiv.org/pdf/2502.21206
- Yan et al., "DatedGPT", arXiv 2603.11838: https://arxiv.org/abs/2603.11838
- Benhenda, "Look-Ahead-Bench", arXiv 2601.13770: https://arxiv.org/abs/2601.13770
- Li, Wang, Ma, "Summoning the Oracle to Slay It" (FinCAD), arXiv 2605.24564: https://arxiv.org/abs/2605.24564
- ALFRED/FRED vintage data help: https://alfred.stlouisfed.org/help/downloaddata
- McLean & Pontiff, "Does Academic Research Destroy Stock Return Predictability?", JF 71(1) 2016: https://Www.Gwern.net/doc/economics/2016-mclean.pdf
- Novy-Marx & Velikov, "A Taxonomy of Anomalies and Their Trading Costs", RFS 29(1) 2016: https://www.nber.org/papers/w20721
- Page, "Continuous Inspection Schemes", Biometrika 41 (1954) (citation via Crossref): https://api.crossref.org/works/10.1145%2F3421537.3421548
- Adams & MacKay, "Bayesian Online Changepoint Detection", arXiv 0710.3742: https://arxiv.org/abs/0710.3742
- Gama et al., "A Survey on Concept Drift Adaptation", ACM CSUR 46(4) 2014: https://research.tue.nl/en/publications/a-survey-on-concept-drift-adaptation/
- Wald, "Sequential Tests of Statistical Hypotheses", Ann. Math. Stat. 16(2) 1945: https://projecteuclid.org/euclid.aoms/1177731118
- Johari, Koomen, Pekelis, Walsh, "Always Valid Inference", arXiv 1512.04922 / Operations Research 70(3) 2022: https://arxiv.org/abs/1512.04922
- Ramdas, Grünwald, Vovk, Shafer, "Game-Theoretic Statistics and Safe Anytime-Valid Inference", Statistical Science 38(4) 2023: https://arxiv.org/abs/2210.01948v2 ; https://projecteuclid.org/journals/statistical-science/volume-38/issue-4/Game-Theoretic-Statistics-and-Safe-Anytime-Valid-Inference/10.1214/23-STS894.full
- Zheng et al., "Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena", NeurIPS 2023: https://arxiv.org/abs/2306.05685
- DSPy project page (ICLR 2024): https://sky.cs.berkeley.edu/project/dspy/ ; PyPI: https://pypi.org/project/dspy/
- Xiao, Sun, Luo, Wang, "TradingAgents", arXiv 2412.20138: https://arxiv.org/abs/2412.20138 ; full text v7: https://arxiv.org/html/2412.20138v7
- Yu et al., "FinMem", arXiv 2311.13743: https://arxiv.org/abs/2311.13743 ; full text: https://arxiv.org/html/2311.13743v2
- Zhang et al., "FinAgent", arXiv 2402.18485 / KDD 2024: https://arxiv.org/abs/2402.18485
- Yang et al., "FinRobot", arXiv 2405.14767: https://arxiv.org/abs/2405.14767
- virattt/ai-hedge-fund README: https://github.com/virattt/ai-hedge-fund
- Pricope, "Deep Reinforcement Learning in Quantitative Algorithmic Trading: A Review", arXiv 2106.00123: https://arxiv.org/abs/2106.00123v1
- Millea, "Deep Reinforcement Learning for Trading: A Critical Survey", Data 6(11) 2021: https://mdpi.com/2306-5729/6/11/119
- FinRL-Meta, NeurIPS 2022, arXiv 2211.03107: https://arxiv.org/abs/2211.03107v1 ; DRL crypto backtest-overfitting test, arXiv 2209.05559: https://arxiv.org/pdf/2209.05559
- Van Tharp SQN (secondary): https://www.luxalgo.com/library/concept/sqn/
- mlfinlab licensing (QuantConnect docs): https://www.quantconnect.com/docs/v2/drafts/mlfinlab.md ; mlfinpy: https://pypi.org/project/mlfinpy/ ; famlafl: https://pypi.org/project/famlafl/
- CRAN pbo package README: https://packages.oit.ncsu.edu/cran/web/packages/pbo/readme/README.html
- PyPI JSON endpoints queried for vectorbt, backtesting, skfolio, arch, statsmodels, quantstats, pyfolio-reloaded, alphalens-reloaded, pyfolio, alphalens, dspy: https://pypi.org/pypi/<name>/json
- Shadow / champion-challenger practitioner material: https://atlan.com/know/shadow-deployment-for-ml-models/ ; https://www.datarobot.com/blog/introducing-mlops-champion-challenger-models/

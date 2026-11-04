# RESEARCH.md — Phase 1 findings, sources and confidence levels

Status: Phase 1 deliverable, awaiting owner review. Last updated: 2026-11-04.

How to read this file. Each section summarises one research area and ends with the design
implications we propose to carry into Phase 2. The full evidence, tables and source lists are in
the appendices under `docs/research/`; this file links to them rather than repeating them.
Every important claim carries one of three labels:

- **Verified** — read on a primary or near-primary source on 2026-11-04; link given in the appendix.
- **Assumption** — a reasonable working inference or default, with the rationale stated.
- **Unknown** — could not be verified; a concrete test is listed in `KNOWN_UNKNOWNS.md`.

"Computed" means calculated in this session from downloaded primary data (Yahoo GC=F daily OHLC,
FRED GVZCLS and DFII10). Secondary sources (broker blogs, news, review sites) are flagged as lower
confidence wherever they are used.

Method note on Exness sources. exness.com and the HTML help-centre pages return HTTP 403 to
automated fetches from this environment. The Exness help centre is a Zendesk instance whose public
JSON API serves the same article bodies; about 55 articles were read that way. Every Exness claim
marked Verified cites the human-readable help-centre URL. The Exness client agreement PDF could not
be fetched and is an Unknown (KU-011).

Appendices:

| File | Content | Words |
| --- | --- | --- |
| `research/01_gold_trading.md` | Gold drivers, sessions, volatility, events, costs, strategies, data | ~6,100 |
| `research/02_exness_mt5.md` | MT5 integration options, Exness accounts, XAUUSD spec, order rules, costs, demo, hosting, failure modes, 16 on-account tests | ~8,000 |
| `research/03_local_llm.md` | Open-weight models (late 2026), serving tools, hardware, latency, cost vs API, eval, embeddings | ~6,000 |
| `research/04_learning_and_validation.md` | Overfitting tests, leakage (incl. LLM look-ahead), decay monitoring, safe promotion, tooling | ~6,700 |
| `research/05_existing_platform_audit.md` | Reuse audit of `binance-ai-trader` with per-component verdicts | ~5,200 |

---

## 0. Executive summary

1. **Gold is in a high-volatility regime and the macro relationships are unstable.** Realised vol was
   21% in 2025 and 30% year-to-date 2026 against a 20-year average of 17%; a single day fell 11.4%
   (30 Jan 2026) and the Jan–Oct 2026 drawdown was 27% (Computed; WGC corroborates). The textbook
   gold/real-yield link inverted at trend scale 2022–2026 while staying negative day-to-day (Computed
   from FRED). Design consequence: size off current volatility, treat macro as conditioning context,
   and budget for crash days.
2. **Only two strategy families have strong independent evidence for gold:** time-series momentum
   (Moskowitz–Ooi–Pedersen 2012; Hurst–Ooi–Pedersen 2017) and volatility targeting as an overlay
   (Harvey et al.), and the latter cuts tails rather than raising Sharpe. Session breakouts, intraday
   mean reversion and news-reaction scalps have only vendor evidence and are extremely cost-sensitive.
   Martingale and grid logic has a near-certain ruin path at 2026 range statistics.
3. **The MT5 integration path is clear and constrained.** The official `MetaTrader5` Python package
   ships Windows-only wheels and requires a running terminal (Verified, PyPI and mql5.com). It is a
   polling API with no push events. Exness allows EAs, uses market execution with slippage (no requotes)
   on Standard/Cent/Raw/Zero, caps leverage dynamically around news and breaks (HMR), and runs servers
   on fixed UTC+0 with a schedule that shifts with US daylight saving.
4. **XAUUSD at Exness:** 100 oz contract, pip 0.01, 0.01–200 lots, triple swap Wednesday, commission
   3.5 USD/lot/side (Raw Spread) or 5.5 USD/lot/side (Zero), none on Standard/Pro (Verified, help
   centre article updated 2026-11-03). Lot step, stops/freeze level, swap values, fill modes and
   typical spreads are not published and must be read from `symbol_info()` on the actual account.
5. **Local LLMs are a privacy and control decision, not a cost saving at our volume.** At roughly
   57M tokens/month the cheapest API options cost about 10–25 USD/month while local builds cost
   80–360 USD/month fully loaded (Assumption with cited inputs). The 2026 open-weight field is strong
   and permissively licensed (Qwen3.5/3.6/3.8, Gemma 4, gpt-oss, Mistral Small 4, GLM-5.x, DeepSeek
   V4.x). Every mainstream local server enforces JSON schemas at decode time, which is the capability
   this system depends on.
6. **Self-learning must be gated by statistics, not enthusiasm.** Lo (2002) implies a strategy with
   annual Sharpe near 1 needs on the order of 2.5–3 years or several hundred trades to be distinguished
   from zero; short demos only catch bugs. PBO, deflated Sharpe and minimum backtest length need an
   honest count of every trial, so the system must keep an append-only trial ledger. LLM look-ahead
   bias is real and not removed by prompting or masking (Sarkar & Vafa 2024; DatedGPT 2026), so
   news-interpretation backtests before the model's training cutoff are inadmissible as evidence.
7. **The existing platform contributes discipline and patterns, not a gold edge.** Its agent runtime
   loop, risk-engine design (pure, ordered, property-tested), config ceilings, versioning, shadow
   ledger, deploy/health/soak tooling and test style transfer well. Its LLM layer is Anthropic-only,
   its chat is seven fixed commands, it has no automated learning loop, and 0 of 26 pre-registered
   strategies (including gold via PAXG) passed its own gate.

---

## 1. Gold trading (appendix `research/01_gold_trading.md`)

### 1.1 Regime as of 2026-11-04

| Item | Value | Label |
| --- | --- | --- |
| Spot gold | ~3,930–3,960 USD/oz on 3 Nov 2026 | Verified (news outlets, lower confidence) |
| All-time high | LBMA PM 5,405 USD on 29 Jan 2026 | Verified (WGC) |
| 2026 peak-to-trough (close) | −26.7% (29 Jan → 30 Oct) | Computed (GC=F) |
| Largest daily move | −11.4% on 30 Jan 2026 | Computed |
| Fed target range | 3.75–4.00%, held 28 Oct 2026; Sept 2026 hike was the first since 2023 | Verified (FOMC calendar); decision details secondary |
| 10y TIPS real yield | 2.65% | Verified (FRED DFII10) |
| GVZ | 25.8 = 88th percentile since 2008; no 2026 print below 21 | Computed (FRED GVZCLS) |
| Q3 2026 demand | Central banks 321 t (+42% y/y); ETFs +211 t; LBMA avg 4,262 USD | Verified (WGC GDT Q3 2026) |

### 1.2 Drivers

- Real yields: long-run relationship inverted 2022–2026 (DFII10 −1.06% → +2.65% while gold tripled);
  same-day beta still negative. **Verified/Computed.** Use as conditioning feature, not a model.
- USD: negative day-to-day correlation, regime dependent. **Assumption** (no primary stability study).
- Central-bank buying and ETF flows: quarterly/monthly, revised, lagging; regime context only. **Verified** (WGC).
- Inflation: weak short-horizon hedge (Erb & Harvey 2013). **Verified.**
- Fed: gold reacts more to looser-than-expected FOMC shocks and the adjustment persists beyond 5 minutes
  (Awartani, Hussain & Virk 2024). **Verified.**

### 1.3 Market structure and sessions

- Price discovery leans to COMEX New York futures despite London's larger OTC volume (Hauptfleisch,
  Putniņš & Lucey 2016). **Verified.** CME GC tick data is a legitimate reference series.
- LBMA Gold Price auctions at 10:30 and 15:00 London (IBA). **Verified** (lbma.org.uk).
- COMEX GC: Sunday 17:00 to Friday 16:00 CT with a 60-minute daily break from 16:00 CT. **Verified via
  snippet, lower confidence** (CME pages returned 503). CME 1-oz gold futures went 24/7 on 24 Jul 2026
  (secondary). CFD broker weekend quoting policy: **Unknown**.
- Spreads lowest during European hours; volatility and volume cluster at major-market opens and the
  08:30 ET release block (Batten et al. 2017; Elder, Miao & Ramchander 2012). **Verified.**
- London PM fix start raises first-minute volatility by 33–52% and edges accrue to auction
  participants, not outsiders (Caminschi & Heaney 2014, full text read). **Verified.**
- Asian-hours rebounds and US-hours pullbacks were documented by the WGC for H1 2026. **Verified**, but
  persistence is **Unknown**; treat as a hypothesis to test.

### 1.4 Volatility (Computed, Yahoo GC=F daily)

| Year | Mean true range (USD) | Realised vol |
| --- | --- | --- |
| 2022 | 28 | 15.7% |
| 2023 | 26 | 13.4% |
| 2024 | 36 | 15.3% |
| 2025 | 67 | 20.9% |
| 2026 YTD | 128 | 29.5% (Jan 2026 month: 48%) |

Latest ATR(14) ≈ 81 USD ≈ 2.0% of price. GVZ median since 2008 is 17.5; 2026 has sat entirely above
the long-run 75th percentile. Regime detection: no gold-specific peer-reviewed HMM paper found
(**Unknown**); robust practice is rolling realised-vol and ATR percentiles plus GVZ percentile
(**Assumption**). Volatility targeting reduces tail risk for commodities without raising Sharpe
(Harvey et al., SSRN 3175538). **Verified.**

### 1.5 Macro events

NFP, CPI, FOMC, GDP, PPI and durable goods are the verified movers; PCE has no gold-specific study
(**Unknown**). No peer-reviewed basis-points-per-surprise table exists for 2020s gold (**Unknown**;
estimate from own tick data). Spread widening around releases is documented qualitatively (Dukascopy
2020 notice, broker pages) but retail-broker magnitudes are **Unknown**. Free, verified calendar
sources: BLS iCal feed, Fed FOMC page, FRED API (attribution required), ForexFactory weekly JSON (no
licence notice found; commercial use **Unknown**).

### 1.6 Costs on retail XAUUSD CFDs

- Published spreads: six-broker average 14.8 "pips" in Oct 2026 (secondary; unit ambiguity flagged).
- Slippage: Saxo Bank Aug 2026 statistics show 43.8% of XAUUSD market orders negatively slipped, the
  worst of 12 instruments. **Verified** (home.saxo).
- Swaps: tom-next plus admin fee; three days' funding on Wednesday; long gold typically pays. Order of
  magnitude at 2026 USD rates: 0.45–0.65 USD/oz/day at 4,000 USD, comparable to 2–4 raw spreads per
  day. **Assumption** (arithmetic on verified formula). Exness-specific values: **Unknown** (KU-001).
- Working backtest assumption: 0.15–0.30 USD/oz all-in in London/NY hours on a raw account; 2–4× in
  Asia; 10×+ for 1–3 minutes around 08:30 ET, FOMC, the fixes and the 17:00 ET break. **Assumption.**

### 1.7 Strategy families

| Family | Evidence | Cost sensitivity | Verdict for Phase 2 |
| --- | --- | --- | --- |
| Time-series momentum / trend | High (peer-reviewed, multi-asset, century-long); gold-only Sharpe **Unknown** | Low to spread, high to swap | Core candidate, faster variants, swap modelled |
| Volatility-targeting overlay | High; cuts tails, no Sharpe gain | Adds turnover | Mandatory overlay |
| Session/time-of-day rules | Low–medium (WGC pattern, theses) | High | Test as regime-dependent hypothesis |
| London/Asian-range breakout | Low (vendor backtests only) | Very high | Only with session-aware cost model |
| Intraday mean reversion | None gold-specific | Extreme | Deprioritise |
| News-reaction drift | Medium (futures, no retail spread) | Extreme | Research only; gate on deterministic surprise features |
| Martingale / grid | Vendor disclaimers describe ruin path | n/a | Excluded by policy (D-008) |

### 1.8 Data sources

Exness MT5 history (depth **Unknown**, KU-003), Exness Personal Area tick-history CSV (**Verified**),
Dukascopy (free export, licence unstated), HistData (2009–2026, terms unstated), Databento CME GC
(paid; best reference series), Yahoo (ToS forbids automation; used here for regime stats only), FRED,
LBMA (licensed), WGC Goldhub.

### 1.9 Design implications

Size off ATR/realised vol; model costs by clock; block entries around 08:30 ET, FOMC, fix starts and
the daily break; prefer limit entries; model stop fills at widened spread; judge after costs with
regime-bucketed metrics, worst day, CVaR and Calmar; never ship averaging-down logic.

---

## 2. Agent skills (appendix `research/04_learning_and_validation.md` §6)

Multi-agent LLM trading papers (TradingAgents, FinMem, FinAgent, FinRobot) report 3–6 month windows on
a handful of mega-cap stocks, inside their models' training data, without transaction costs
(**Verified** from the papers' own text). Their contribution is architectural: role separation,
structured outputs, an explicit risk role, and debate to surface disagreement. We borrow the
architecture and none of the numbers.

Proposed skill-to-mode mapping (**Assumption**, design reasoning; becomes `AGENTS.md`):

| Skill | Mode | Reason |
| --- | --- | --- |
| Market analysis (features, regimes, statistics) | Deterministic code | Reproducible, leakage-controllable; LLM may describe results |
| News interpretation | Hybrid | LLM extracts event/direction/magnitude/confidence into a flat schema; code validates against consensus data; output is a feature, never an order |
| Strategy research (hypotheses) | LLM, sandboxed | Cheap idea generation; every idea goes to the trial ledger; no config or execution access |
| Backtesting | Deterministic code | Engine, costs, CPCV, DSR, PBO in code |
| Risk management | Deterministic code, immutable config | Not learnable |
| Execution | Deterministic code | Latency, idempotency, exact limits; LLM has no broker path |
| Post-trade review | Hybrid | Code computes attribution; LLM writes narrative and hypotheses |
| System health | Code with LLM triage | Alerts are rules; LLM summarises logs |
| Chat/explanation | LLM, read-only, confirm-to-act | Explains state with provenance; any change needs explicit confirmation |

The existing platform's pattern (code-computed inputs → LLM → schema validation → code gates → approval
→ single order placer that re-runs the risk check) matches this mapping and is retained (appendix 05 §3–4).

---

## 3. Exness and MT5 (appendix `research/02_exness_mt5.md`)

### 3.1 Integration options

| Option | Key facts | Label | Verdict |
| --- | --- | --- | --- |
| `MetaTrader5` Python package | v5.0.6231 (2026-09-27); Windows x86-64 wheels only; `initialize()` attaches to or launches a local terminal over IPC; 32 functions; polling only, no callbacks | Verified (PyPI, mql5.com) | **Primary execution path** |
| MQL5 Expert Advisor + bridge | Each EA runs in its own thread; ticks arriving during `OnTick` are dropped; `WebRequest` blocks and needs the allowed-URL list; sockets gated the same way; DLLs need "Allow DLL imports" | Verified (mql5.com) | Optional tick-streamer later; not for LLM calls |
| MT5 under Wine on Linux | Official MetaQuotes install script exists; Python support not mentioned; `mt5linux` (PyPI v1.1.1) is community tooling | Verified / Assumption | Research and ingestion only |
| MetaApi cloud bridge | REST + WebSocket; billed while deployed with 6-hour minimum; claims 6 ms median added latency; pricing seen only in a snippet | Verified / Unknown | Not needed; keep as contingency |
| Exness API (REST/WebSocket) | Works only for Exness-terminal accounts, "Standard MetaTrader accounts cannot connect"; excludes stocks/indices; prices throttled 100–500 ms; possible regional gating | Verified / Unknown | Not usable with an MT5 account |
| FIX | No Exness page found | Unknown | Assume none for retail |

### 3.2 Account types (Verified, help centre, 2026-11-04)

| | Standard | Standard Cent | Pro | Raw Spread | Zero |
| --- | --- | --- | --- | --- | --- |
| Execution | Market | Market | Instant (requotes) unless account currency is USD/JPY/THB/CNY/IDR/VND | Market | Market |
| XAUUSD commission per lot per side | none | none | none | 3.5 USD | 5.5 USD |
| Margin call / stop out | 60% / 0% | 60% / 0% | 30% / 0% | 30% / 0% | 30% / 0% |
| MT5 Real open / pending | Unlimited / 1,000 | 100 or 200 combined (server-dependent) | Unlimited / 1,000 | Unlimited / 1,000 | Unlimited / 1,000 |
| Symbol suffix | `-m` | `-c` | none | none or `-r` | none or `-z` |

Stop out is 20% for the Kenya and Jordan entities. Hedged margin is 0% on all types. Swap-free status
is account-wide, algorithmic and revocable; XAUUSD is on the swap-free instrument list (**Verified**).
Entity list: Seychelles (SC) verified; Curaçao, BVI, South Africa, CySEC and FCA entities seen only
in snippets or secondary sources (**Unknown**). The owner's entity and account type are KU-002.

### 3.3 XAUUSD contract (Verified, Commodities article updated 2026-11-03)

100 troy oz; pip 0.01; 0.01–200 lots; leverage "1:Unlimited if eligible"; higher margin requirement
(HMR) 1:200–1:1000 (HMR article says 1:100–1:1000, the two pages disagree); hedged margin 0%; triple
swap Wednesday; stop level "refer to trading platform". **Unknown** and to be read from `symbol_info()`
(KU-001): lot step, point/digits, freeze level, swap long/short and mode, volume limit, filling modes,
margin currency, typical spread.

### 3.4 Hours and time (Verified)

Servers are UTC+0 and do not shift; the schedule shifts with US daylight saving. Current (winter)
XAUUSD hours: Mon–Thu open 00:00–21:58, closed 21:58–23:00:30, close-only to 23:02, reopen 23:02;
Sunday open 23:05; Friday close 21:58. Swap applied at 22:00 UTC winter, 21:00 summer. Weekend
maintenance Sat/Sun 05:00–09:00 UTC; Cent/Trial (demo) servers also pause daily at 23:20 UTC.

### 3.5 Order rules (Verified)

Market execution with slippage; pending orders, SL and TP fill at the requested price only if the gap
is within the instrument's "slippage-free range" (XAUUSD: 0–3× spread, dynamic); pending orders are
cancelled rather than filled when a gap would open them beyond their own SL/TP; SL/TP are queued after
trigger and filled at the next market price; hedging and Close-By are supported; negative balance
protection resets only after a full stop-out. HMR applies 15 minutes before and 90 seconds after
high-impact news, 30 minutes before and about 10 minutes after the gold daily break, and about 3 hours
before to 1 hour after the weekend. Leverage falls by equity tier (unlimited below 5,000 USD if
eligible, down to 1:500 at 100,000 USD and above).

### 3.6 Costs: worked 0.1-lot round trip, intraday (Assumption inputs, Verified commission)

| | Standard | Pro | Raw Spread | Zero |
| --- | --- | --- | --- | --- |
| Spread assumed (pips) | 20 | 12 | 6 | 0 |
| Spread cost | 2.00 | 1.20 | 0.60 | 0.00 |
| Commission | 0 | 0 | 0.70 | 1.10 |
| Slippage (1 pip × 2 fills) | 0.20 | 0.20 | 0.20 | 0.20 |
| **Total USD** | **2.20** | **1.40** | **1.50** | **1.30** |

Spread sampling on the live symbol decides the account choice; Zero's spread is floating around
news and rollover so its figure is best-case.

### 3.7 Policy, demo, hosting, data

- EAs are explicitly allowed; Exness may disable an EA that floods the server; no numeric rate limit
  is published (MT5 returns retcode 10024 on too-frequent requests). Client-agreement clauses on
  latency arbitrage were seen only in a snippet (**Unknown**, KU-011).
- Demo conditions are stated as identical to real; balance can be set in the Personal Area; demo
  accounts are deleted after unstated inactivity; whether commission and swap are booked on demo is
  **Unknown** (KU-009). Demo cannot exercise the per-client risk models behind slippage-free ranges
  and HMR, so demo slippage is a lower bound (**Assumption**).
- Exness free VPS has balance/volume keep-alive rules, is MT-only with RDP, and wipes data on
  termination; VPS locations are Amsterdam, Dubai, Seoul, Singapore, Miami, Johannesburg
  (**Verified**). Which data centre serves a given account is **Unknown** (measure ping).
- Official tick history is downloadable from the Personal Area as CSV (Exness, Symbol, Timestamp,
  Bid, Ask), sourced from MT4 Real servers (**Verified**). Terminal history depth is **Unknown** (KU-003).

### 3.8 Design implications

Polling gateway on Windows with `symbol_info_tick`, `positions_get`, `order_check` immediately before
`order_send`; derive session state from `symbol_info().trade_mode`, never from a hard-coded clock;
read and log the full `symbol_info()` and `account_info()` at startup and daily, failing closed on
change; treat stops as best-effort at or beyond the level; throttle requests and back off on 10024;
keep free-margin headroom for HMR; prefer Raw Spread or Zero for automation; avoid Pro unless the
account currency enables market execution. The 16 on-account verification tests in appendix 02 §(a)
become the Phase 3 acceptance checklist.

---

## 4. Local LLMs (appendix `research/03_local_llm.md`)

### 4.1 Models (Verified on Hugging Face, 2026-11-04)

| Tier | Recommended | Alternatives | Licence | Q4 footprint |
| --- | --- | --- | --- | --- |
| (a) extraction, sentiment, JSON | Qwen3.5-9B | gpt-oss-20b, Gemma 4 12B | Apache-2.0 | 5.7 GB |
| (b) analysis, review, chat | Qwen3.8-27B (262k context, hybrid linear attention) | Gemma 4 31B (multimodal) | Apache-2.0 | 16.5 GB |
| (c) strategy research | Qwen3.5-122B-A10B or gpt-oss-120b (MoE, ≤10B active) | Mistral Small 4 119B-A6B | Apache-2.0 | 63–70 GB |

Llama 4 (Apr 2025) is the oldest family in the field and superseded. Qwen3.8-Flash-Next is not
Apache (internal use exempt; read the licence). Finance-tuned models (Fin-R1, Agentar-Fin-R1, FinGPT)
sit on 2024–25 bases and exam-style benchmarks; **Assumption**: not worth it, build our own evals.

### 4.2 Serving

Every mainstream server enforces a JSON schema at decode time: llama.cpp (GBNF; silently drops
unsupported keywords, breaks on nested `$ref` and `anyOf` with `properties`), Ollama (`format`;
default context 4,096 tokens, must be raised), vLLM (xgrammar/guidance; Linux only), SGLang
(structural tags for tool calls), LM Studio, TGI, TabbyAPI. Raw `mlx_lm.server` does not
(**Verified**). Grammar constrains syntax, not semantics: schema and example still go in the prompt;
keep schemas flat; disable thinking for extraction calls. Single GPU → llama.cpp router mode or
Ollama; 2+ GPUs on Linux → vLLM, one process per GPU.

### 4.3 Hardware (specs Verified; prices partly Community; tokens/s Community)

| Build | Cost | Runs | Latency, 2k prompt → 500 tokens, thinking off |
| --- | --- | --- | --- |
| A, ~2.5k USD, one 24 GB GPU | tiers (a)+(b) swapped | 27B at 40–60 t/s | ~6–14 s |
| B, ~7k USD, RTX 5090 32 GB + 128 GB RAM | (a)+(b) resident; 120B via CPU-MoE offload at 8–20 t/s for nightly batch | | ~6–10 s; 9B ~3–4 s |
| C, ~12k USD, RTX PRO 6000 Blackwell 96 GB | (a)+(c) resident; 122B-A10B at ~98 t/s, gpt-oss-120b at ~210 t/s | | ~3–6 s |

RTX 5090 street price was 4,200–5,100 USD in Aug–Sep 2026 against a 1,999 USD MSRP (Community).
Unified-memory boxes (DGX Spark 4,699 USD, Framework Strix Halo 3,449 USD, Mac Studio M5 Ultra from
5,499 USD) run 120B-class MoE at 50–60 t/s but dense 27–32B at only 11–30 t/s. Dense 70B is a poor
deal everywhere. All tokens/s figures are community measurements; run `llama-bench` on the actual box.

### 4.4 Cost comparison (Assumptions with cited list prices)

Workload: 6,240 analysis calls, 1,100 chat turns and 22 nightly 200k-token reviews per month ≈ 50.6M
input + 6.2M output tokens. Monthly: cheapest APIs 10–25 USD (gpt-oss-120b hosts, DeepSeek Flash,
Claude Haiku 5.5); mid-tier frontier 160–440 USD; local 81–93 USD (A), 206–218 USD (B), 347–359 USD
(C) including 36-month amortisation and about 80 kWh electricity. Conclusion: local is justified by
privacy of positions and strategy, absence of rate limits and repricing, and deterministic latency,
not by cost, unless volume grows about tenfold.

### 4.5 Hybrid policy and fallback ladder (Assumption, recommended)

Local-first; API permitted only for weekly strategy research synthesis, chat the owner explicitly tags
for cloud, or degraded mode on non-account tasks; account data (positions, balances, orders, broker
IDs, credentials) never leaves; deterministic scrubber and payload-hash log on every outbound call.
OpenAI and Anthropic do not train on API data by default (**Verified**); Anthropic retention terms and
China-hosted providers' terms are **Unknown**. Fallback: retry once → next-smaller local model → re-ask
with validator error → rule-based default flagged `degraded` → never let an LLM failure block or alter
execution.

### 4.6 Evaluation and memory

Three versioned eval sets (headline → JSON; indicator table → regime label; trade-review rubric) run
through promptfoo in CI with JSON-schema, agreement and latency assertions, and Inspect AI for agentic
reviews (**Verified** tools). Embeddings: Qwen3-Embedding-0.6B or bge-m3 into pgvector (or sqlite-vec),
always time-filtered to prevent look-ahead leakage.

---

## 5. Self-learning and validation (appendix `research/04_learning_and_validation.md`)

### 5.1 Overfitting tests (Verified from the papers)

| Method | Detects | Threshold | Limitation |
| --- | --- | --- | --- |
| Probability of Backtest Overfitting (Bailey, Borwein, López de Prado, Zhu) | IS-best trial ranking below median OOS | Reject if PBO > 0.05 | Needs every trial recorded |
| Deflated Sharpe Ratio (Bailey & López de Prado 2014) | Best-of-N Sharpe vs expected max under null | DSR ≥ 0.95 | Needs N and variance of trial SRs |
| Minimum Backtest Length | Sample too short for N trials | 5 years supports ~45 independent trials at E[max SR] = 1 | Necessary, not sufficient |
| Harvey–Liu haircut / HLZ hurdle | Multiple-testing inflation | t > 3.0; an SR of 0.75 after 200 tests becomes 0.32 | Count of prior tests is a judgement |
| CPCV + purge + embargo | Label-overlap leakage; single-path fragility | Report OOS SR distribution across paths | Compute cost |
| Walk-forward | Temporal generalisation along one path | Expect degradation | Low power; window choice is itself a trial |
| White Reality Check / Hansen SPA | Data snooping across many rules | Bootstrap p < 0.05 | Needs full rule universe |
| Trade permutation / bootstrap | Luck in sequence; CI on expectancy | 95th percentile permuted drawdown as budget | Tests luck, not alpha |

### 5.2 Leakage

Standard leaks (current-bar close at open, overlapping labels, centred filters, release vs publication
time, broker history revisions) and their mitigations are tabulated in the appendix. LLM-specific:
Sarkar & Vafa (2024) show look-ahead bias that prompting does not remove and masking only weakens
(61% firm re-identification from censored text); DatedGPT (2026) measures a 26.4 bp per standard
deviation look-ahead premium; FinCAD (2026) finds up to 67% of in-sample LLM "edge" is memorised
(**Verified** abstracts). Consequence: evaluate news-interpretation only on post-cutoff data, record the
cutoff in experiment metadata, and gate trading on deterministic surprise features (actual minus
consensus) with the LLM as validated extractor.

### 5.3 Decay and live monitoring

Published anomalies lose 26% out-of-sample and 58% post-publication (McLean & Pontiff 2016,
**Verified**). Monitors (defaults are **Assumptions**): CUSUM on per-trade returns (k = 0.5σ, h = 4σ),
rolling Sharpe with Lo bands (n ≥ 60–100 trades), live drawdown vs Monte Carlo 95th/99th percentile,
hit-rate p-chart, Bayesian online change-point, fill-quality drift (mean slippage > 1.5× assumption).
Pause on any breach; retire on 99th-percentile drawdown or negative upper-CI expectancy after 100
trades or two pauses in 90 days; never auto-unpause.

### 5.4 Safe improvement loop

Pipeline: propose → offline gate (CPCV, PBO ≤ 0.05, DSR ≥ 0.95, haircut SR ≥ 0.5, costs at 1.5×
measured, BH at FDR 10% across the week's batch) → demo (≥ 60 trading days and ≥ 100 trades) → shadow
(≥ 30 days and ≥ 50 trades, slippage ≤ 1.25× model) → limited live (25% size, ≥ 90 days and ≥ 100
trades, sequential e-value test vs champion) → full. Champion/challenger comparisons must use
anytime-valid methods (SPRT, always-valid p-values, e-values; **Verified** citations) because fixed
tests with peeking inflate false promotions. Everything that affects a decision is one immutable
versioned bundle stamped on every order. Risk limits, instrument list, hours, kill switch, permission
scopes and the promotion gates themselves are not learnable: hash-checked config, execution-layer
guards, credential separation, and tests that attempt violations. Prompt changes are versions evaluated
on extraction accuracy, not P&L; LLM-as-judge is biased (Zheng et al. 2023) and never a gate. Batch
retraining through the pipeline, not online learning. Reinforcement learning for signal generation has
no public real-money evidence (**Unknown**) and stays out of the loop.

### 5.5 Minimum evidence (Verified formula, Assumption derivation)

Lo (2002): SE(SR) ≈ sqrt((1 + 0.5·SR²)/T). A strategy with annual Sharpe 1.0 and 250 trades/year
needs about 680 trades (2.7 years) to be distinguished from zero at one-sided 95%; Sharpe 1.5 needs
about 300 trades; Sharpe 2.0 about 170. A 3-month demo can reject gross failures only.

### 5.6 Tooling (Verified on PyPI, 2026-11-04)

vectorbt 1.2.0, backtesting.py 0.6.6, skfolio 1.12.5 (CombinatorialPurgedCV), arch 8.0.0,
statsmodels 0.15.0, quantstats 0.0.86, pyfolio-reloaded 0.10.2, alphalens-reloaded 0.5.1, dspy 3.4.1
are all actively released. mlfinlab is closed-source; PBO/DSR/MinBTL will be implemented in-house and
cross-checked against the CRAN `pbo` package (**Assumption**).

---

## 6. Existing platform audit (appendix `research/05_existing_platform_audit.md`)

`binance-ai-trader`: 25,480 lines TypeScript (Node 22, zod, Fastify, grammy, Postgres/TimescaleDB,
ccxt), 650 vitest cases, eight processes communicating only through Postgres. All read from code.

| Component | Verdict | Note |
| --- | --- | --- |
| Agent runtime loop (schema-forced output, invalid-resubmit retries, timeout racing, transient retry, budget, circuit breaker, cost ledger) | Adapt | Semantics excellent; types are Anthropic-specific |
| `model.ts`, `tools.ts` | Rewrite | 100% Anthropic SDK; no local or OpenAI-compatible path |
| Agent spec pattern (code inputs → LLM → zod → code gates → approval → single order placer) | Reuse as pattern | Isolation-tested |
| Risk engine (pure `checkEntry`, 29 reject codes, 20,000-case property test), guards, sizing | Adapt | Units → lots/points/swap/sessions; margin model → Exness stop-out |
| Config loader + ceilings + watcher + diff + hash; version tagging | Reuse mechanism, rewrite schema | |
| Backtest engine, fills, walk-forward, metrics, scorer, validation gates | Adapt | Add sessions, weekend gaps, spread-by-time, swap, lot rounding |
| Shadow ledger, baselines, calibration, scorecard, go-live gate | Adapt | Exactly the evidence layer the learning system needs |
| Approval state machine + Telegram buttons | Adapt | Add confirm flow for settings |
| Supervisor, locks, exit log, deploy with health and auto-rollback, soak checks, watchdog | Reuse as patterns | |
| Exchange, execution, trader, collector, feeds, regime components, positioning, carry, lot engine, gold-on-Binance research | Rewrite or drop | Crypto-specific |
| Notify/Telegram | Adapt | Seven fixed commands; no free-text assistant |

Gold findings in the old repo: a PAXG-on-Binance trend ensemble returned +4.2% with Sharpe 0.38
against buy-and-hold Sharpe 0.93, failed the scorer (56 < 70), and the XAU perpetual step was never
started; no code models sessions, swaps, lots or spread by session. The twelve weaknesses the new
product must beat are listed in appendix 05 §13; the headline ones are provider lock-in, no automated
learning loop, fixed-command chat, manual research, crypto assumptions throughout, unused vector
memory, and no validated gold edge to inherit.

---

## 7. Consolidated design defaults proposed for Phase 2

Each becomes a `D-nnn` entry in `DECISIONS.md` once the owner reviews this file.

1. Python core; port the TypeScript risk/config/backtest patterns with their tests (D-004).
2. Windows host (VPS in Amsterdam or owner PC) runs MT5 and a thin polling execution gateway; Linux
   GPU host runs LLMs, analytics, learning and chat; authenticated link between them (D-005).
3. Raw Spread or Zero account for automation, decided by two weeks of live spread sampling; avoid
   Pro unless the account currency enables market execution.
4. Intraday-first; multi-day holds only per strategy with swap modelled from `symbol_info()`.
5. Local-first LLM stack: Qwen3.5-9B / Qwen3.8-27B / (122B-A10B or gpt-oss-120b) behind one
   OpenAI-compatible provider interface with decode-time JSON-schema enforcement and Pydantic
   validation; API fallback only for redacted, non-account research, off by default (D-010 proposed).
6. Hardware recommendation pending owner answer on budget (brief Q3, KU-006): Build B if an RTX 5090 is
   obtainable near MSRP, otherwise Build C; Build A suffices for tiers (a)+(b) with API for research.
7. Learning pipeline exactly as §5.4 with an append-only trial ledger and immutable safety layer.
8. Cost model by clock with session-dependent spread multipliers; news and break windows blocked.
9. Strategy library starts with trend/TSMOM variants plus a volatility-targeting overlay; session
   hypotheses enter the pipeline as pre-registered experiments; martingale/grid excluded (D-008).
10. The 16 on-account tests (appendix 02 §(a)) are the Phase 3 acceptance checklist.

## 8. Open unknowns

All items are tracked in `KNOWN_UNKNOWNS.md` (KU-001 to KU-018) with a concrete resolution method
and a phase deadline. The ones that block design rather than implementation are KU-002 (entity and
account type), KU-006 (owner hardware), and the owner answers to brief questions Q3, Q4, Q5 and Q7.

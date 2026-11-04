# Evidence base for automated XAUUSD (spot gold CFD) trading

Compiled 2026-11-04. Labels: **Verified** (primary or near-primary source, link + access date), **Assumption** (reasonable inference, not independently confirmed), **Unknown** (no reliable source found). "Computed" = calculated in this session from downloaded primary data (Yahoo GC=F daily OHLC 2021-11-04 to 2026-11-04; FRED GVZCLS and DFII10 full history). Secondary sources (broker blogs, news aggregators, comparison sites) are flagged as lower confidence.

---

## 0. Current regime snapshot (as of 2026-11-04)

| Item | Value | Status |
|---|---|---|
| Spot gold, 3 Nov 2026 | ~US$3,927–3,960/oz intraday (different outlets, different timestamps) | **Verified** (CNBC, Fortune, Nation Thailand, accessed 2026-11-04) – news, not exchange data |
| GC=F front-month close 3 Nov / 4 Nov 2026 | $3,940.4 / $4,072.2 (+3.3% on 4 Nov) | **Computed** from Yahoo GC=F |
| All-time high | LBMA PM high $5,405.00 on 29 Jan 2026; spot intraday high $5,595.47 same day; GC=F intraday high $5,586.2 | **Verified** WGC Mid-Year Outlook 2026 (gold.org, accessed 2026-11-04); GC=F computed |
| Peak-to-trough 2026 (close basis) | $5,318 (29 Jan) → $3,899 (30 Oct) = **−26.7%** | **Computed** (GC=F) |
| Largest daily move | 30 Jan 2026: −11.4% close-to-close (TR $780) after Warsh Fed-chair nomination; silver −26% | **Computed**; cause per Reuters-syndicated reports (gvwire, ThinkAdvisor, accessed 2026-11-04) – secondary |
| Fed policy | Target range 3.75–4.00%, held 28 Oct 2026 (10–2, two dissents for a hike); +25bp hike in Sept 2026 was the first since 2023; Dec 9 meeting carries SEP | **Verified** FOMC calendar (federalreserve.gov, accessed 2026-11-04); decision details from Admiral Markets / Trading Economics / investinglive (secondary, accessed 2026-11-04) |
| 10y TIPS real yield (DFII10) | 2.65% on 3 Nov 2026 (2026 range 1.72–2.95%) | **Verified/Computed** FRED DFII10 (accessed 2026-11-04) |
| GVZ (Cboe Gold ETF Vol Index) | 25.82 on 3 Nov 2026 = 88th percentile of full history since 2008 | **Verified/Computed** FRED GVZCLS (accessed 2026-11-04) |
| WGC Q3 2026 demand (published 3 Nov 2026) | Total 1,369t (−1% y/y); central banks 321.2t (+42% y/y); ETFs +211.2t (after −47.3t in Q2); bar & coin 267.8t; jewellery 313t; LBMA PM avg **$4,262/oz** (+23% y/y) | **Verified** gold.org GDT Q3 2026 (accessed 2026-11-04) |
| WGC full-year 2025 (published 29 Jan 2026) | Total demand 5,002t (first >5,000t); central banks 863.3t (−21% vs 1,092t in 2024); ETFs +801.2t; LBMA PM avg $3,431.5 (+44%) | **Verified** gold.org GDT FY2025 (accessed 2026-11-04) |

Regime summary: 2025 was a parabolic year (GC=F $2,669 → $4,341, +63%; first close >$4,000 on 8 Oct 2025); the rally extended to ~$5,300–5,600 in late Jan 2026, then a hawkish Fed under new chair Warsh, a stronger dollar, rising real yields and Q2 ETF outflows drove a ~27% drawdown; H2 2026 has oscillated around $4,000–4,300 with central-bank buying at a record quarterly pace and ETF inflows returning in Q3. The WGC's H2 baseline was "±5% around US$4,100/oz" (**Verified**, Mid-Year Outlook, 1 Jul 2026).

---

## 1. Price drivers and the stability of the relationships

### 1.1 Real yields (TIPS)
- Textbook relationship: gold is a zero-coupon real asset, so its price should fall when real yields rise. **Verified** that the relationship broke down at the level/trend scale: DFII10 went from −1.06% (end-2020) to +1.58% (end-2022) to +2.65% (Nov 2026), while gold went from ~$1,800 to >$5,000 (**Computed** from FRED and GC=F). Over 2022–2026 the sign of the long-run relationship is positive, not negative.
- Short-window correlations remain negative much of the time but are unstable: a 20-day rolling gold–10y TIPS correlation fell from −0.84 (May 2023) to −0.60 (Jul 2023) (MarketPulse, secondary, accessed 2026-11-04). Blog claims of "R² 0.82 pre-2022 vs 0.15 after" are **unsourced** – treat as Unknown.
- Explanations offered by sell-side/asset managers (T. Rowe Price, Schroders, RBC Wealth Management; secondary, accessed 2026-11-04): price-insensitive official-sector buying (>1,000t/yr 2022–2024, roughly double the 2010–2021 average), geopolitical/fiscal hedging, de-dollarisation. The WGC's own attribution model (GRAM) attributed 14% of H1-2026 price variability to FX (**Verified**, Mid-Year Outlook 2026).
- Implication: a real-yield signal is useful as a *conditioning* variable for intraday/daily reactions (rates up on the day → gold down on the day is still the typical same-day response, e.g. 3 Nov 2026) but is not a reliable standalone directional model. **Assumption**: daily-frequency beta to real yields is still negative; the drift term is dominated by flows.

### 1.2 USD (DXY)
- Gold is priced in USD; dollar strength was repeatedly cited as a driver of the 2026 drawdown ("stronger US dollar and an increase in Treasury yields", Nation Thailand 3 Nov 2026; WGC lists "US dollar strength and rates rising beyond current expectations" as the main headwind; **Verified** WGC Mid-Year Outlook). Day-to-day gold/DXY correlation is negative but regime-dependent (**Assumption**; no primary stability study retrieved).

### 1.3 Central-bank buying (WGC data)
- **Verified** (gold.org GDT): 2024 1,092t; 2025 863t; Q3 2026 321t (+42% y/y, largest quarterly figure in the current series as reported). The WGC estimated in Q3 2025 that ~66% of official-sector buying was unreported (secondary summaries of GDT Q3 2025) – reported figures are revised and partly estimated. Reclassification cut Q1-2026 reported buying from 244t to 57t (Khaleej Times citing WGC, secondary).
- Limitation: quarterly, lagged, revised data – useful for regime context, not tradable signals.

### 1.4 ETF flows
- **Verified** (gold.org): 2025 +801t; Q2 2026 −47t; Q3 2026 +211t. The WGC publishes monthly flows (xlsx, "usually within one week of month-end"; latest file September 2026, page dated 26 Oct 2026). Daily GLD/IAU holdings changes are available from the issuers (**Assumption**: freely downloadable from fund sites). Flows are coincident/lagging with price; evidence they *lead* price at daily horizon is **Unknown**.

### 1.5 Inflation expectations / inflation hedge
- Erb & Harvey (2013, FAJ, "The Golden Dilemma"): gold is an unreliable inflation hedge at short horizons; the real gold price mean-reverts only over very long horizons (**Verified** NBER w18706 abstract via search; accessed 2026-11-04). Consequence: CPI matters for gold mainly through the expected-Fed-path channel, not as a mechanical hedge.

### 1.6 Risk-off / geopolitics / safe haven
- Baur & Lucey (2010, Financial Review 45(2)) define "hedge vs safe haven" and find gold is a safe haven for stocks in extreme stress, but only briefly (**Verified** bibliographic record; findings from secondary summaries). The 2026 episode shows the haven premium can unwind violently (Jan 30 2026 flash crash attributed to removal of Fed-independence risk premium + margin hikes; secondary).

### 1.7 Fed policy
- Awartani, Hussain & Virk (2024, Int. Rev. Fin. Analysis 95): with 5-minute data, gold returns and volatility react **more to looser-than-expected** FOMC shocks than to tighter ones, and the adjustment **persists beyond 5 minutes** (potential short-term inefficiency) (**Verified** KFUPM repository page, accessed 2026-11-04).
- A Yale student study (1993–2023) found ~1.09% gold-futures rise per 1pp of rate surprise over 2 days, p = 0.083 (weak, **low confidence**).

---

## 2. Market structure and sessions

### 2.1 How "XAUUSD" is priced
- The underlying is the London OTC loco-London spot market (LBMA), with COMEX GC futures the main exchange-traded venue. **Verified** academic finding (Hauptfleisch, Putniņš & Lucey 2016, J. Futures Markets 36(6)): using 17 years of intraday data, both venues contribute to price discovery but **New York futures lead on average** despite London's far larger volume; an earlier paper (Lucey, Larkin & O'Connor 2013) found leadership switches over time. For an automated system this means COMEX GC tick data is a legitimate (often superior) reference for spot CFD price behaviour.
- Retail broker XAUUSD CFD quotes are aggregated from liquidity-provider OTC spot streams (banks/non-banks) with a markup; some brokers derive from futures. **Assumption** – no broker publishes its exact construction; search found only review-site descriptions. Futures-vs-spot basis is passed to the trader through the overnight swap, not the quote.

### 2.2 LBMA Gold Price (the "fix")
- **Verified** (lbma.org.uk, accessed 2026-11-04): set twice daily at **10:30 and 15:00 London time** in USD, administered by ICE Benchmark Administration (IBA); electronic auction in rounds, chairperson sets starting price, auction concludes when net imbalance is within **10,000 oz**; published on Reuters/Bloomberg live and on LBMA site delayed. Silver at 12:00; platinum/palladium 09:45 and 14:00.

### 2.3 COMEX GC hours and daily break
- CME's own spec page returned HTTP 503 in this session (three attempts). From CME fact-card text surfaced in a cmegroup.com-restricted search and a broker (MetroTrade) spec page: **CME Globex Sunday 5:00 p.m. – Friday 4:00 p.m. CT with a 60-minute break each day beginning 4:00 p.m. CT** (= 17:00–18:00 ET / 22:00–23:00 UTC in winter) (**Verified via snippet, lower confidence**; cmegroup.com fact card, accessed 2026-11-04; help.metrotrade.com, accessed 2026-11-04). Contract 100 oz, tick $0.10 = $10.
- **New in 2026**: CME extended the cash-settled **1-ounce gold futures** to weekend/24-7 trading (notice 26-220; effective 24 Jul 2026; ~15,000 contracts/~$60m notional first weekend; TradeInformer and CME press release 27 Jul 2026, the latter 503 – **Verified via secondary**, accessed 2026-11-04). The 100-oz GC schedule was not changed by that notice (**Assumption**). CFD brokers' weekend quoting policies are **Unknown** – check your broker.

### 2.4 Sessions, volatility and spread by time of day
- **Verified** (Batten, Lucey, McGroarty, Peat & Urquhart 2017, PLOS ONE 12(4), 5-min data May 2000–Apr 2015): strong intraday periodicity in returns, volatility, volume and bid-ask spread; "returns and volume ... linked to the opening and closing of major markets", **bid-ask spread lowest when European markets are open**; spreads narrowed and trade counts rose over the sample (EconPapers/DOAJ records, accessed 2026-11-04).
- **Verified** (Elder, Miao & Ramchander 2012, J. Banking & Finance 36(1), intraday 2002–2008): the **08:30 ET** US release block (NFP, durable goods) has the largest impact on gold futures returns, volatility and volume.
- **Verified** (Caminschi & Heaney 2014, J. Futures Markets 34(11), full text read): around the **London PM fix start (15:00 London)**, average relative volatility in the first minute rises **+32.9% for GC** (+52.2% in the 2011–12 sub-sample), with elevated volume; statistically significant return advantages of a few bps (e.g. "4bps in the two minutes before the end of the fixing") for traders who see the auction; trades in the opening minutes predict fixing direction 80–95% for large fixings; **no significant returns after publication**. Interpretation for automation: the fix window is a volatility/volume event, not a free directional drift for outsiders; the post-2015 electronic auction may have changed this (**Unknown**).
- Session-of-day *returns* asymmetry: WGC's Mid-Year Outlook 2026 states gold's "rebounds generally occurred during Asian hours" while many pullbacks occurred in US hours (**Verified**, gold.org). Student theses (CBS; Zurich) report a "hat-shaped" pattern (rise in eastern hours, fall into/after London fixes) and that short London-open→PM-fix / long PM-fix→next London open was the best-performing rule in-sample (**low confidence** – unreviewed theses). A "long overnight/short intraday" gold strategy has circulated since ~2010 with very large claimed returns (commentary, **unverified**). Treat as a hypothesis to test with your own broker's bid/ask data, not as established fact.
- Practitioner consensus (broker education pages, MQL5 blogs; **low confidence**): Asian session (00:00–07:00 UTC) is the quietest with wider spreads; London open (07:00–08:00 UTC) and the London/NY overlap (12:30–16:00 UTC) carry most of the range; the 17:00 ET rollover is the worst liquidity hole.

---

## 3. Volatility regimes

### 3.1 Realised volatility and ATR (Computed from Yahoo GC=F daily OHLC; close-to-close log returns, annualised ×√252)

| Year | Mean true range ($) | Median TR ($) | Mean (H−L)/C | Realised vol | Close range |
|---|---|---|---|---|---|
| 2022 | 28.0 | 25.3 | 1.50% | 15.7% | 1,631–2,043 |
| 2023 | 25.6 | 22.3 | 1.27% | 13.4% | 1,817–2,093 |
| 2024 | 35.5 | 31.9 | 1.43% | 15.3% | 2,004–2,801 |
| 2025 | 66.5 | 55.8 | 1.82% | 20.9% | 2,647–4,553 |
| 2026 YTD (to 4 Nov) | 127.6 | 105.9 | 2.70% | 29.5% | 3,899–5,318 |

Monthly, 2025–2026 (mean TR $, mean range %, RV%): Jan-25 36/1.3%/13%; Apr-25 92/2.7%/31%; Oct-25 117/2.8%/33%; Jan-26 171/3.4%/**48%**; Feb-26 191/3.7%/33%; Mar-26 193/3.9%/38%; Jul-26 93/2.2%/21%; Oct-26 74/1.8%/18%; Nov-26 (3 days) 107/2.7%/26%. Latest ATR(14) ≈ **$81 ≈ 2.0% of price**.

- **Verified** corroboration: WGC says realised vol "pushed above 50%" in the Jan–Feb 2026 sell-off, has "come down below 30%", versus a **20-year average of 17%** (gold.org Mid-Year Outlook 2026, accessed 2026-11-04). Cboe (20 Oct 2025): GLD 1-month implied vol hit a 5-year high of 30%, implied–realised spread 12.5 pts, gold vol "4 standard deviations above its long-term average" (**Verified**, cboe.com insights, accessed 2026-11-04).
- Caveat: Yahoo GC=F is a continuous front-month series (roll gaps affect a handful of days); vol is close-to-close, so range-based estimators would be ~10–20% different. Figures are ±1–2 pts, adequate for regime sizing.

### 3.2 GVZ behaviour (FRED GVZCLS, daily since 3 Jun 2008; Computed)
- Full-history percentiles: p5 11.1, p25 14.9, **median 17.5**, p75 21.5, p95 31.9.
- Yearly min/median/max: 2022 14.1/18.1/31.7; 2023 10.2/14.6/22.8; 2024 10.9/16.2/22.3; 2025 14.5/18.5/**32.8 (16 Oct)**; 2026 21.4/**26.6**/**46.0 (29 Jan)**. 2026 has not printed below 21 – i.e. the entire year sits above the long-run 75th percentile.
- GVZ is VIX methodology applied to GLD options (30-day implied) (**Verified** Cboe via FRED notes). It typically exceeds subsequent realised vol (variance risk premium); 2025–26 shows the usual pattern of implied spiking *with* realised rather than leading it.

### 3.3 Regime-detection methods (evidence quality)
- Hidden Markov / Markov-switching models on returns or realised vol (Hamilton 1989 lineage). Recent applied example: arXiv 2510.03236 fits an HMM to smoothed realised vol and a separate HAR model per regime (S&P 500, not gold) (**Verified** arXiv listing, accessed 2026-11-04). No gold-specific peer-reviewed HMM paper was found (**Unknown**). A Substack summary of a comparison study reports GARCH-based regime inputs were most stable and 3 regimes beat 2 (**low confidence**).
- Simpler, robust practice: (a) rolling realised-vol percentile vs 1–3 year history; (b) ATR(14)/price bands (e.g. <1.2% calm, 1.2–2% normal, >2.5% stress – thresholds are **Assumption** derived from the table above); (c) GVZ vs its own percentile; (d) implied–realised spread sign. Regime labels should gate position size and strategy selection, not predict direction.
- Volatility targeting: Harvey, Hoyle, Korgaonkar, Rattray, Sargaison & van Hemert (SSRN 3175538): for commodities, vol scaling has a **negligible effect on Sharpe** but **consistently reduces tail risk** (**Verified** via search of SSRN/Quantpedia, accessed 2026-11-04). Moreira & Muir (2017, JF 72(4)) show gains in equities/carry, but DeMiguel, Martín-Utrera & Uppal (JF 2024) note later work finds these fail out-of-sample and after costs (**Verified** EconPapers/NBER records).

---

## 4. Macro events

### 4.1 Which scheduled releases move gold
| Release | Evidence | Status |
|---|---|---|
| NFP / Employment Situation (08:30 ET, first Friday) | Largest impact on gold futures among US releases (Elder et al. 2012); unemployment rate significant (Christie-David, Chaudhry & Koch 2000, J. Econ. & Business 52(5)) | **Verified** abstracts |
| CPI (08:30 ET) | "Gold also responds strongly to the release of the CPI" (Christie-David et al. 2000) | **Verified** |
| FOMC statement (14:00 ET) + presser (14:30) | Asymmetric: looser-than-expected shocks move gold more; effect persists >5 min (Awartani et al. 2024) | **Verified** |
| GDP, PPI, capacity utilisation | Significant (Christie-David et al. 2000) | **Verified** |
| Durable goods | Among the largest 08:30 movers (Elder et al. 2012) | **Verified** |
| PCE | Widely watched; no gold-specific study retrieved | **Unknown** |
| Direction | "Unexpected improvement in the economy" → negative for gold and silver (Elder et al.) | **Verified** |
Magnitudes: no peer-reviewed study gives a bp-per-surprise table for modern (2020s) gold; the Yale student estimate (~1.1% per 1pp rate surprise, p≈0.08) is weak. **Unknown** – estimate from your own tick data per event type.

### 4.2 Spread behaviour around events
- Dukascopy client notice (24 Mar 2020): "liquidity in XAU/USD and XAG/USD has evaporated ... causing wide spreads. Our provider price quotations strongly vary or may temporarily cease ... This can cause rejections and lead to slippage" (**Verified**, dukascopy.com, accessed 2026-11-04). BlackBull support pages list announcements, central-bank decisions and off-hours as widening causes (**Verified** broker page).
- Measured magnitudes of news widening for XAUUSD at retail brokers: only vendor/blog claims ("10–30×") – **Unknown/low confidence**. FastBull's monthly spread survey reported an IC Markets XAUUSD worst-case print of 255 points vs an 8.4-point average (Sept 2025) – secondary, methodology unstated.

### 4.3 Free / API-accessible calendars (verified in session)
| Source | What | Terms / notes | Status |
|---|---|---|---|
| BLS release calendar | CPI, Employment Situation etc.; iCal feed `https://www.bls.gov/schedule/news_release/bls.ics`; month/list views; all times ET. Nov 2026: NFP Fri 6 Nov 08:30, CPI Tue 10 Nov 08:30 | Public domain | **Verified** bls.gov (accessed 2026-11-04) |
| Fed FOMC calendar | 2026: Jan 27-28, Mar 17-18*, Apr 28-29, Jun 16-17*, Jul 28-29, Sep 15-16*, Oct 27-28, Dec 8-9* (*=SEP); 2027 dates listed | Public | **Verified** federalreserve.gov (accessed 2026-11-04) |
| BEA schedule (GDP, PCE) | bea.gov/news/schedule | Not fetched this session | **Assumption** |
| ForexFactory weekly JSON | `https://nfs.faireconomy.media/ff_calendar_thisweek.json` returns an array with fields `title, country, date (ISO, −05:00 offset), impact, forecast, previous`; no event IDs, no "actual" field in the sample | **No terms/attribution notice in the feed**; ForexFactory's ToS not retrieved – commercial/redistribution rights **Unknown** | **Verified** feed structure (accessed 2026-11-04) |
| FRED API | Free API key required; must display "This product uses the FRED® API but is not endorsed or certified by the Federal Reserve Bank of St. Louis"; bandwidth limits at Fed's discretion; copyrighted third-party series (e.g. Cboe GVZ) need owner permission beyond personal use | **Verified** fred.stlouisfed.org terms (accessed 2026-11-04) |
| Trading Economics API | Trial: "limited to 100000 data points and 100 requests"; paid plans not priced publicly (third-party claim $199–399/mo, unverified) | **Verified** trial limits (tradingeconomics.com, accessed 2026-11-04); pricing **Unknown** |

---

## 5. Spreads, slippage and financing for retail XAUUSD CFDs

### 5.1 Spreads
- Unit warning: brokers quote gold in "points"/"pips" that mean $0.01 or $0.10 depending on the broker; always convert to $/oz.
- Published measurements (all secondary, time-stamped averages, session unspecified):
  - BestBrokers live cTrader tracker, reported by FX News Group 5 Oct 2026: average XAU/USD spreads Fusion 9, FP Markets 9, BlackBull 12, FxPro 17.03, IC Markets 19, Pepperstone 23 "pips"; six-broker mean 14.84; "spreads can expand sharply during periods of lower liquidity or elevated market risk" (**Verified** article, accessed 2026-11-04; underlying methodology partly stated).
  - FastBull BrokersView monthly review: IC Markets XAUUSD ~7.3 points (Nov 2025), 8.4 points avg / 255 worst (Sept 2025) (**low confidence**).
  - Broker raw/ECN accounts typically advertise "from $0.05–0.15/oz" plus commission (~$6–7 per lot round turn) (**Assumption** from broker marketing).
- Working assumption for backtests: **$0.15–0.30/oz all-in (spread + commission) in London/NY hours on a raw account; 2–4× that in Asian hours; 10×+ for 1–3 minutes around 08:30 ET releases and at the 17:00 ET rollover** (**Assumption**, consistent with the above and Batten et al.'s finding that spreads are lowest in European hours).

### 5.2 Rollover / server-midnight widening
- Mechanism: the OTC "trading day" rolls at 17:00 New York; swaps are applied and LPs pull/widen quotes; many MT4/5 brokers run server time = GMT+2/+3 so this appears at 00:00 server time; some brokers suspend XAUUSD quoting for several minutes (BlackBull education page; MQL5 blog posts – **Verified** broker page / **low confidence** blogs, accessed 2026-11-04). This coincides with the COMEX 60-minute maintenance break (17:00–18:00 ET), when the futures reference disappears (**Assumption** linking the two).

### 5.3 Slippage – a rare published measurement
- Saxo Bank publishes monthly market-order execution statistics by instrument. **August 2026, XAUUSD market orders: 42.9% price improvement, 13.3% zero slippage, 43.8% negative slippage; average net improvement −0.7 pips** – the worst negative-slippage share of all 12 instruments listed (FX majors ~14–32%) (**Verified**, home.saxo, accessed 2026-11-04). Historic all-instrument figures: FXCM Jul 2018 63.9% no slippage / 25.9% positive / 10.2% negative; FxPro Q1 2016 46.9% positive / 31.8% at quote / 21.3% negative (Finance Magnates, secondary).
- Takeaway: on gold, expect roughly symmetric but *frequent* slippage on market orders at a bank-grade broker; at MT4/5 retail brokers with last-look LPs slippage during news is asymmetric against the trader (**Assumption**). Pending (limit) orders avoid negative slippage but add fill uncertainty; stop orders are filled as market orders after the trigger and take the full widened spread (**Assumption** – standard CFD execution policy).

### 5.4 Swaps / overnight financing
- **Verified** (IG help page, accessed 2026-11-04): "For forex and spot metals deals, we charge the tom-next rate plus an admin fee" (1.5% p.a. stated in the text; a worked CFD example uses 0.3%/0.8% – internally inconsistent), charged at 22:00 UK; positions held through **Wednesday 22:00 UK incur three days'** funding (T+2 settlement). OANDA: FX, gold and silver rates are "a blend of underlying liquidity providers' tom-next swap rates, adjusted by the instrument specific admin fee"; daily charge = position × rate/365 (**Verified** oanda.com, accessed 2026-11-04). Some brokers apply triple swap on Friday for metals – check the symbol spec (**Assumption**).
- Why long gold pays: the tom-next rate embeds the USD funding rate (~3.9% IORB today) minus the gold lease rate (near zero), plus markup, so **long swaps are negative and typically larger than short swaps are positive** (**Assumption**; no broker explains the gold leg). Order of magnitude at 2026 USD rates: 4–6% p.a. of notional ≈ **$0.45–0.65/oz/day at $4,000** – comparable to 2–4 raw spreads *per day*, so multi-day longs need a daily edge well above that; trend-followers holding weeks pay several % per year of notional (**Assumption**, arithmetic on the verified formula).

### 5.5 Regulatory frame
- ESMA (1 Jun 2018, applied from 1 Aug 2018): retail CFD leverage **20:1 for gold**, 50% margin close-out, negative-balance protection, mandatory loss-percentage warning (**Verified** esma.europa.eu, accessed 2026-11-04). ESMA's analysis of NCA data found 74–89% of retail CFD accounts lose money (ESMA 2018 statements, reproduced by NBS/FinanceFeeds – **Verified via secondary**). NCAs (FCA, BaFin, etc.) made equivalent rules permanent in 2019 (**Verified via secondary**, Finance Magnates).

---

## 6. Strategy families: evidence, holding period, cost sensitivity, failure modes

| Strategy | Best evidence | Quality | Holding | Cost sensitivity | Known failure modes |
|---|---|---|---|---|---|
| Time-series momentum / trend | Moskowitz, Ooi & Pedersen (2012, JFE 104(2)): "persistence in returns for one to 12 months that partially reverses over longer horizons" across 58 futures incl. gold; "performs best during extreme markets" (**Verified** abstract). Hurst, Ooi & Pedersen (2017, JPM): profitable back to 1880 (**Verified** AQR page). Baltas & Kosowski (2012 WP): TSMOM Sharpe >1.2 across 71 contracts 1974–2012 (**Verified** via search) | High (peer-reviewed, century-long, multi-asset) but **gold-only** single-asset Sharpe is far lower and not separately reported (**Unknown**) | Weeks–months | Low to spreads; **high to swap** on CFDs (see 5.4) | Whipsaw in range regimes (e.g. H2 2026 ±5% band); crash reversals (30 Jan 2026 −11% erased weeks of trend P&L in one day); needs vol scaling and crisis-aware sizing |
| Volatility targeting overlay | Harvey et al. (SSRN 3175538): negligible Sharpe change for commodities, **reduces tail risk** (**Verified**) | High | n/a (overlay) | Adds turnover | Over-leverages into calm-then-crash (GVZ 21 → 46 within days in Jan 2026) |
| London-open / Asian-range breakout | Only vendor backtests and blogs; one practitioner reports a fixed-18-point-spread backtest that lost 11% in 3 weeks live because real spreads widen at the open (MQL5 blog, **low confidence**). No peer-reviewed study (**Unknown**) | Low | Hours | **Very high** – entries coincide with spread/vol spikes; stops inside the widened spread get hit | False breakouts in range regimes; dependence on exact session clock/DST; data-snooping over session windows |
| Intraday mean reversion | No gold-specific peer-reviewed evidence found (**Unknown**). Structural facts: spreads lowest in European hours (Batten et al.); vol/volume spikes at fix start and 08:30 ET | Low | Minutes–hours | **Extreme** – edge per trade is a few bps vs 4–8 bps round-trip cost | Trend days (2.7% average daily range in 2026) blow through reversion bands; news |
| Session / time-of-day rules (e.g. long Asia, short London→PM fix) | WGC: rebounds in Asian hours, pullbacks in US hours (H1 2026, **Verified**); student theses (**low**); Caminschi & Heaney: volatility +33–52% at fix start, few-bp edges only for informed participants (**Verified**) | Low–medium (pattern exists in-sample; persistence unknown) | Hours | High (two crossings/day) and swap if held through 17:00 ET | Pattern reversal; regime-specific (bull-market Asian buying) |
| News-reaction (post-release drift) | Elder et al.: reaction "swift and significant"; Awartani et al.: FOMC adjustment persists >5 min (**Verified**) | Medium (academic, but on futures without retail spread) | Minutes | **Extreme** (spreads 10×, slippage, requotes; Dukascopy warning) | Broker-side: widened spreads, delayed quotes, "off-quotes"; direction of surprise must be parsed correctly from feed |
| Carry / swap harvesting | Verified financing mechanics (IG/OANDA) | n/a | Days+ | Positive swap on shorts is small; short gold in a secular bull market is directionally expensive | Not a standalone edge on gold CFDs |

### What does not work / is oversold
- **Martingale / grid EAs on gold**: vendor disclaimers themselves say "a sustained one-directional move can exceed the grid depth and lead to significant drawdown or loss of the account"; forum consensus: "most EAs that show amazing backtests on gold rely on some form of martingale or grid logic"; a grid EA that survived EURUSD "blew up sooner" on XAUUSD (MQL5 forum/market pages, **Verified** that these statements exist; **low confidence** as evidence). With 2026's 2.7% average daily range and a −11% day, any averaging-down scheme has a near-certain ruin path.
- "Guaranteed" bots / myfxbook curves: smooth equity curves with high win rate and no reported floating drawdown are the martingale signature; ESMA loss statistics (74–89% of retail accounts lose) are the base rate.
- Fixed-spread backtests on gold are structurally over-optimistic for any strategy that trades at session opens, 08:30 ET, or rollover.

---

## 7. Data sources for gold history

| Source | Coverage / granularity | Cost / licence | Quality caveats | Status |
|---|---|---|---|---|
| MT5 broker history | Whatever the broker's server holds; M1 typically years, real ticks often only months; "Max bars in chart" only affects display | Free with account | Bid-only bars at many brokers; history can be synthetic/filled; Strategy Tester bar counts can differ from live chart (MQL5 forum reports) | **Verified** forum/MetaQuotes docs (accessed 2026-11-04), **low** on depth per broker |
| Dukascopy Historical Data Export | Tick to monthly CSV; XAUUSD from 2003-05 (Tickstory, secondary) | "free through Dukascopy's Historical Data Export tool"; licence/redistribution terms not stated on the page (**Unknown**) | One Swiss bank's bid/ask stream; forum reports of spikes in MT5 backtests – validate | **Verified** page (accessed 2026-11-04) |
| HistData.com | XAUUSD tick data (generic CSV) **2009–2026**, M1 for MT4/5/Excel; files updated 2026-11-01 | Free download; $7/mo Google-Drive updates; FTP paid; terms not stated | Source undisclosed; bid/ask convention unstated | **Verified** page (accessed 2026-11-04) |
| Polygon.io (now Massive.com) | C:XAUUSD history from 2009-09-27 but "mostly missing from July 2010 to July 2013" and gold/silver "sparse from January 2021 to August 2025" (their FAQ) | Currencies Basic $0 (2y EOD, 5 calls/min); Starter $49/mo (10+y, real-time, unlimited calls) | Gaps make it unsuitable as the primary gold history | **Verified** massive.com (accessed 2026-11-04) |
| Databento GLBX.MDP3 (CME GC/MGC) | COMEX futures; MBO from May 2017, MBP-10/trades/OHLCV back to 2010 ("16+ years") | Pay-as-you-go by uncompressed size; $125 free credits; Standard $199/mo; redistribution per CME licence (most datasets after 24h) | Futures, not spot: roll/basis handling needed; best quality reference series | **Verified** databento.com (accessed 2026-11-04) |
| Yahoo Finance GC=F | Daily/intraday front-month via unofficial chart endpoint | ToS: no automated collection "without our express, prior permission"; no commercial reuse | Continuous-contract splices; fine for regime stats (used here), not for production | **Verified** ToS (accessed 2026-11-04) |
| FRED | GVZCLS (daily since 2008), DFII10, T5YIE/T10YIE breakevens, DTWEXBGS dollar | Free API key, attribution required; Cboe series copyrighted for commercial reuse | No gold price series | **Verified** |
| LBMA | AM/PM prices (delayed on site; licensed via IBA for commercial use) | Licence needed for redistribution | Two prints/day | **Verified** lbma.org.uk |
| WGC Goldhub | Quarterly GDT, monthly ETF flows (xlsx), central-bank stats | Free, registration for some downloads | Revised/estimated official-sector data | **Verified** gold.org |

---

## 8. Metrics for judging an automated gold system (after all costs)

1. Net return after spread, commission, swap and *measured* slippage, with costs modelled by session (not a constant).
2. Sharpe and Sortino on daily P&L; **Calmar** (CAGR / max drawdown) given gold's fat tails.
3. Max drawdown and **max daily loss** vs the 30 Jan 2026 (−11%) and 21 Oct 2025 (−5.9%) scenarios; drawdown duration.
4. Tail metrics: 99% daily VaR/CVaR of P&L; worst 5 days as share of total P&L (dependence on a few days = fragility).
5. Profit factor and expectancy **per trade in $/oz vs round-trip cost in $/oz**; share of gross edge consumed by costs (should be <40–50%).
6. Turnover (lots/day) and cost ratio; swap paid as % of equity per year for multi-day books.
7. Hit rate × payoff ratio with trade-count-based confidence intervals; exclude martingale-style lot escalation (report max lot / base lot).
8. Regime breakdown: performance in calm/normal/stress buckets (ATR% or GVZ percentile) and in trend vs range months; performance with/without event windows.
9. Out-of-sample / walk-forward results, deflated Sharpe or parameter-stability tests; sensitivity to ±1 h session-clock shifts and DST.
10. Execution quality: realised vs modelled slippage, rejected/requoted orders, fill latency; fraction of trades entered within 2 min of a Tier-1 release or within the 16:55–18:05 ET window.
11. Leverage and margin headroom under a 50% close-out rule at 20:1; worst-case intraday equity trough.
12. Capacity: P&L scaling vs size at your broker's typical top-of-book depth (gold books are thin at retail LPs).

---

## Key takeaways for system design

1. The macro drivers are real but unstable: the gold–real-yield relationship inverted at the trend scale 2022–2026 while staying negative at daily scale. Use rates/dollar as same-day conditioning features and regime context, never as the standalone directional model.
2. 2025–2026 is a high-vol regime: realised vol 21% (2025) → 30% (2026 YTD) vs a 20-year average of 17%; ATR(14) ≈ 2% of price; GVZ has spent all of 2026 above its long-run 75th percentile. Size everything off current ATR/RV, not fixed dollar stops.
3. Budget for crashes: a −11% day (30 Jan 2026) and a −27% drawdown (Jan–Oct 2026) happened inside a secular bull market. Position sizing and any averaging logic must survive that path.
4. Volatility targeting is the one overlay with strong evidence for commodities: it mainly cuts tails rather than raising Sharpe. Implement it; don't expect it to create edge.
5. Trend/TSMOM is the best-evidenced directional family, but on CFDs the swap (~4–6% p.a. of notional long at current USD rates ≈ $0.5/oz/day) is a first-order cost; favour faster trend signals, or hold exposure in futures/options if available.
6. Price discovery leans to COMEX (NY) despite London volume; use CME GC tick data (Databento) as the reference for microstructure research and to detect broker-feed anomalies.
7. Model costs by clock: cheapest liquidity in European hours (Batten et al.); widen spreads 2–4× in Asia and 10×+ for the 08:30 ET block, 14:00 ET FOMC, 10:30/15:00 London fixes and the 17:00–18:00 ET COMEX break/rollover. Fixed-spread backtests are invalid for session-open strategies.
8. Treat the London PM fix start (15:00 London) and 08:30 ET as volatility events: block new entries 2–3 min before and after, move/avoid tight stops. Edges around the fix accrue to auction participants, not to outsiders.
9. Session-of-day return asymmetry (Asian-hours rebounds, US-hours pullbacks in H1 2026) is documented by the WGC but not shown to persist; test it as a regime-dependent hypothesis with bid/ask data, with explicit DST handling.
10. Expect frequent slippage on gold market orders even at bank-grade brokers (Saxo Aug 2026: 43.8% of XAUUSD market orders negatively slipped). Prefer limit entries; model stop fills at the widened spread.
11. Use the official calendars (BLS .ics, Fed FOMC page) as the authoritative schedule; use the ForexFactory JSON feed only as a convenience layer and check its licence before any commercial use; FRED needs attribution and an API key.
12. Never ship martingale/grid logic on gold; the vendors' own disclaimers describe the ruin path and 2026's range statistics make it near-certain.
13. Judge the system after costs with session-aware cost modelling, regime-bucketed performance, walk-forward validation and explicit tail metrics (worst day, CVaR, Calmar) – not on win rate or equity-curve smoothness.
14. Keep an eye on structural changes: 24/7 CME 1-oz gold futures (from July 2026) may change weekend gap behaviour and broker weekend quoting; the ESMA/NCA 20:1 retail leverage cap bounds position size in EU/UK.

---

## Sources (all accessed 2026-11-04)

Primary / official
- World Gold Council, Gold Demand Trends Q3 2026 (3 Nov 2026): https://www.gold.org/goldhub/research/gold-demand-trends/gold-demand-trends-q3-2026
- World Gold Council, Gold Demand Trends Full Year 2025 (29 Jan 2026): https://www.gold.org/goldhub/research/gold-demand-trends/gold-demand-trends-full-year-2025
- World Gold Council, Gold Mid-Year Outlook 2026 (1 Jul 2026): https://www.gold.org/goldhub/research/gold-mid-year-outlook-2026
- World Gold Council, Gold ETF holdings and flows (data page, Sept 2026 file): https://www.gold.org/goldhub/data/gold-etfs-holdings-and-flows
- LBMA Gold Price methodology page: https://www.lbma.org.uk/prices-and-data/lbma-gold-price/lbma-gold-price
- CME Group gold futures fact card (hours; via cmegroup.com search snippet – page itself returned 503): https://www.cmegroup.com/trading/metals/files/fact-card-gold-futures-options.pdf ; GC spec page (503 in session): https://www.cmegroup.com/markets/metals/precious/gold.contractSpecs.html ; 1-oz gold 24/7 notice 26-220 (503 in session): https://www.cmegroup.com/notices/clearing/2026/06/26-220.html
- MetroTrade GC spec (broker page used for hours): https://help.metrotrade.com/kb/gold-futures-gc-contract-specifications
- Cboe Insights, "Gold Volatility Spikes to 5-Year High on Haven Demand" (20 Oct 2025): https://www.cboe.com/insights/posts/gold-volatility-spikes-to-5-year-high-on-haven-demand
- FRED GVZCLS: https://fred.stlouisfed.org/series/GVZCLS ; FRED DFII10: https://fred.stlouisfed.org/series/DFII10 ; FRED API terms: https://fred.stlouisfed.org/docs/api/terms_of_use.html
- Federal Reserve FOMC calendars: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
- BLS release calendar / iCal: https://www.bls.gov/schedule/news_release/ (feed: https://www.bls.gov/schedule/news_release/bls.ics)
- ForexFactory weekly JSON feed: https://nfs.faireconomy.media/ff_calendar_thisweek.json
- Trading Economics API page: https://tradingeconomics.com/api/pricing.aspx
- ESMA product intervention measures (1 Jun 2018): https://www.esma.europa.eu/press-news/esma-news/esma-adopts-final-product-intervention-measures-cfds-and-binary-options
- Saxo Bank order execution statistics (XAUUSD slippage, Aug 2026): https://www.home.saxo/en-gb/rates-and-conditions/forex/orders-and-execution
- IG overnight funding (tom-next + admin fee): https://www.ig.com/uk/help-and-support/spread-betting-and-cfds/fees-and-charges/why-is-overnight-funding-charged-and-how-is-it-calculated-
- OANDA financing fees: https://www.oanda.com/au-en/trading/financing-fees
- Dukascopy notice "Beware, large spreads on Gold and Silver" (24 Mar 2020): https://www.dukascopy.com/swiss/it/about/ournews/beware-large-spreads-on-gold-and-silver ; Dukascopy historical data export: https://www.dukascopy.com/swiss/english/marketwatch/historical/
- HistData XAUUSD tick data: https://www.histdata.com/download-free-forex-historical-data/?/ascii/tick-data-quotes/xauusd
- Massive (Polygon) currencies plans/FAQ: https://massive.com/currencies
- Databento pricing: https://databento.com/pricing ; GC dataset: https://databento.com/datasets/GLBX.MDP3/futures/GC
- Yahoo Terms of Service: https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html ; GC=F chart endpoint used for computed stats: https://query1.finance.yahoo.com/v8/finance/chart/GC=F?range=5y&interval=1d
- BlackBull Markets, why spreads widen / rollover: https://blackbull.com/en/support/why-spreads-may-widen/ ; https://blackbull.com/en/education-hub/understanding-roll-over-and-widening-spreads-2

Academic
- Moskowitz, Ooi & Pedersen (2012) "Time series momentum", JFE 104(2): https://econpapers.repec.org/RePEc:eee:jfinec:v:104:y:2012:i:2:p:228-250 (SSRN 2089463)
- Hurst, Ooi & Pedersen (2017) "A Century of Evidence on Trend-Following Investing": https://www.aqr.com/Insights/Research/Journal-Article/A-Century-of-Evidence-on-Trend-Following-Investing
- Baltas & Kosowski (2012) "Momentum Strategies in Futures Markets and Trend-following Funds": https://efmaefm.org/0EFMAMEETINGS/EFMA%20ANNUAL%20MEETINGS/2012-Barcelona/papers/BK_MOMF_Full.pdf
- Harvey, Hoyle, Korgaonkar, Rattray, Sargaison & van Hemert, "The Impact of Volatility Targeting": https://papers.ssrn.com/abstract=3175538
- Moreira & Muir (2017) "Volatility-Managed Portfolios": https://www.nber.org/papers/w22208 ; DeMiguel, Martín-Utrera & Uppal (2024) critique: https://econpapers.repec.org/RePEc:bla:jfinan:v:79:y:2024:i:6:p:3859-3891
- Hauptfleisch, Putniņš & Lucey (2016) "Who Sets the Price of Gold? London or New York", JFM 36(6): https://econpapers.repec.org/RePEc:wly:jfutmk:v:36:y:2016:i:6:p:564-586 ; Lucey, Larkin & O'Connor (2013): https://econpapers.repec.org/RePEc:taf:apeclt:v:20:y:2013:i:8:p:813-817
- Batten, Lucey, McGroarty, Peat & Urquhart (2017) "Stylized facts of intraday precious metals", PLOS ONE 12(4): https://econpapers.repec.org/RePEc:plo:pone00:0174232
- Caminschi & Heaney (2014) "Fixing a Leaky Fixing", JFM 34(11) (full text): https://api.research-repository.uwa.edu.au/ws/files/4725264/A0205_use_this.pdf
- Elder, Miao & Ramchander (2012) "Impact of macroeconomic news on metal futures", JBF 36(1): https://econpapers.repec.org/RePEc:eee:jbfina:v:36:y:2012:i:1:p:51-65
- Christie-David, Chaudhry & Koch (2000) "Do macroeconomics news releases affect gold and silver prices?", J. Econ. & Business 52(5): https://scholarscommons.fgcu.edu/esploro/outputs/journalArticle/Do-macroeconomics-news-releases-affect-gold/99384088373606570
- Awartani, Hussain & Virk (2024) "How do the gold intra-day returns and volatility react to monetary policy shocks?", IRFA 95: https://pure.kfupm.edu.sa/en/publications/how-do-the-gold-intra-day-returns-and-volatility-react-to-monetar
- Erb & Harvey (2013) "The Golden Dilemma", FAJ 69(4) / NBER w18706: https://www.nber.org/papers/w18706
- Baur & Lucey (2010) "Is Gold a Hedge or a Safe Haven? An Analysis of Stocks, Bonds and Gold", The Financial Review 45(2), 217–229 – bibliographic record only (via RePEc search result; paper not opened; no direct URL verified)
- Erb & Harvey (2013) FAJ record on IDEAS: https://ideas.repec.org/a/taf/ufajxx/v69y2013i4p10-42.html
- arXiv 2510.03236, HMM regime-switching HAR volatility forecasting (S&P 500): https://arxiv.org/pdf/2510.03236
- Yale student study on FOMC surprises and gold futures (weak evidence): https://csec.yale.edu/node/81
- CBS student thesis "Gold Price Dynamics Around the Clock" (low confidence): https://research.cbs.dk/en/studentProjects/gold-price-dynamics-around-the-clock/

Secondary / news / broker-review (lower confidence)
- CNBC gold price 3 Nov 2026: https://www.cnbc.com/select/the-price-of-gold-today-nov-3-2026/ ; Fortune: https://fortune.com/article/current-price-of-gold-11-03-2026/ ; Nation Thailand (3 Nov 2026, Fed/dollar): https://www.nationthailand.com/business/trading-investment/40073078
- FOMC Oct 2026 decision coverage: https://admiralmarkets.com/analytics/traders-blog/fomc-october-2026-fed-holds-rates ; https://tradingeconomics.com/united-states/interest-rate/news/592678 ; https://investinglive.com/central-banks/the-full-statement-from-the-october-2026-fomc-rate-decision/
- 30 Jan 2026 crash coverage: https://gvwire.com/2026/01/30/gold-set-for-steepest-daily-drop-since-1983-silver-eyes-worst-day-ever/ ; https://www.thinkadvisor.com/amp/2026/01/30/gold-and-silver-plunge-as-wild-swings-rock-metals-markets/
- 2026 drawdown drivers: https://www.kucoin.com/news/flash/gold-s-43-year-worst-drop-27-peak-to-valley-in-2026 ; https://goldsilver.com/industry-news/goldsilver-news/gold-etf-outflows-central-bank-buying-divergence-2026/
- TradeInformer on CME 24/7 gold futures: https://www.tradeinformer.com/institutional-trading/cme-24-7-gold-futures-weekend-trading
- Real-yield decoupling commentary: T. Rowe Price https://troweprice.com/en/us/insights/what-is-driving-gold-prices-to-all-time-record-highs ; RBC WM https://www.rbcwealthmanagement.com/en-us/insights/golds-regime-change ; MarketPulse rolling correlation: https://www.marketpulse.com/news/resilient-gold-is-supported-by-geopolitical-risk/
- Spread surveys: FX News Group / BestBrokers (5 Oct 2026): https://fxnewsgroup.com/forex-news/institutional/gold-cfd-spreads-vary-by-more-than-150-between-major-brokers/ ; FastBull BrokersView Nov 2025: https://www.fastbull.com/brokersview/news/brokersview-november-spread-review-the-winners-revealed-for-forex-xauusd-trading-316709
- Historic slippage reports: FXCM Jul 2018 https://www.financemagnates.com/forex/brokers/fxcm-reports-spreads-execution-quality-metrics-for-july-2018/ ; FxPro Q1 2016 https://www.fxpro.com/press-releases/fxpro-announces-execution-statistics-for-q1-2016
- ESMA retail-loss statistics and NCA permanence: https://nbs.sk/en/news/esma-agrees-to-prohibit-binary-options-and-restrict-cfds-to-protect-retail-investors/ ; https://www.financemagnates.com/forex/brokers/esma-urges-national-regulators-for-permanent-cfds-measures/
- Martingale/grid on gold (forum & vendor disclaimers): https://www.mql5.com/en/forum/502291/page2 ; https://www.mql5.com/en/market/product/185970
- London breakout vendor backtests / live-vs-backtest anecdote: https://www.mql5.com/en/blogs/post/777540 ; https://www.mql5.com/en/blogs/post/770488
- MT5 history-depth forum threads: https://www.mql5.com/en/forum/493089 ; https://www.mql5.com/en/forum/446581
- Tickstory Dukascopy date ranges (XAUUSD from 2003-05-04): https://tickstory.com/dukascopy-historical-data-available-date-ranges

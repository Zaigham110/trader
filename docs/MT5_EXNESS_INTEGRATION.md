# MT5_EXNESS_INTEGRATION — verified integration details and instrument specification

Status: Phase 2 draft; account-specific values are filled in Phase 3 from the live terminal.
Last updated: 2026-11-04. Labels: **Verified** (source and access date in `research/02_exness_mt5.md`),
**Assumption**, **Unknown** (KU-id). Implements R-001 to R-008.

## 1. Account (owner answers, D-016, D-017)

| Item | Value | Label |
| --- | --- | --- |
| Account type | Standard | Owner |
| Platform | MT5 | Owner |
| Expected gold symbol | `XAUUSDm` (Standard suffix `-m`) | Verified suffix table; exact name KU-019 |
| Execution | Market execution, no requotes, slippage possible | Verified |
| Commission | None | Verified |
| Margin call / stop out | 60% / 0% (20% if the entity is Kenya or Jordan) | Verified |
| Entity | Unknown; read `account_info().company` and PA footer | KU-002 |
| Capital | Under 50 USD | Owner |
| Leverage setting | Recommend 1:500 in the Personal Area (margin ≈ 8 USD per 0.01 lot at 4,000 USD); default is 1:2000 below 5,000 USD equity | Verified tiers; recommendation Assumption |
| Swap-free | Likely by default ("By default, all accounts are swap-free"); irrelevant for intraday | Verified statement |

## 2. XAUUSD contract specification

Published by Exness (Verified, Commodities article updated 2026-11-03):

| Spec | Value |
| --- | --- |
| Contract size | 100 troy oz per 1.00 lot |
| Pip size | 0.01 |
| Volume | 0.01 to 200 lots |
| Hedged margin | 0% |
| Swap | Triple on Wednesday (not applicable intraday) |
| Max leverage | 1:Unlimited if eligible; HMR 1:200–1:1000 (one page says 1:100) |
| Stop level | "Refer to trading platform" |

To be read from `symbol_info("XAUUSDm")` at startup and stored in `symbol_specs` (KU-001):

`digits, point, trade_tick_size, trade_tick_value, trade_contract_size, volume_min, volume_max,
volume_step, volume_limit, trade_stops_level, trade_freeze_level, trade_mode, trade_exemode,
filling_mode, swap_mode, swap_long, swap_short, swap_rollover3days, margin_initial, margin_hedged,
currency_base, currency_profit, currency_margin, session_deals, spread, spread_float`.

Derived money math (Verified formulas):

- Pip value per 1.00 lot = 1 × 100 × 0.01 = 1.00 USD per pip; per 0.01 lot = 0.01 USD per pip.
- 1 USD move in gold on 0.01 lot = 1.00 USD P&L.
- Margin (dynamic leverage) = lots × 100 × price / leverage. At 4,000 USD: 1:2000 → 2.00 USD;
  1:500 → 8.00 USD; HMR 1:200 → 20.00 USD; HMR 1:100 → 40.00 USD per 0.01 lot.

## 3. Trading hours and time

- Server time is UTC+0, fixed; the schedule shifts with US daylight saving (Verified).
- Winter (in force since 2026-11-01): Mon–Thu open 00:00–21:58, closed 21:58–23:00:30, close-only
  23:00:30–23:02, reopen 23:02; Sunday open 23:05; Friday close 21:58; Saturday closed.
- Summer: one hour earlier (daily break 20:58–22:02; Sunday open 22:05).
- Swap time 22:00 UTC winter / 21:00 summer (not applicable intraday).
- Weekend server maintenance Sat/Sun 05:00–09:00 UTC; demo ("Trial") servers also pause daily at
  23:20 UTC.
- **Rule:** the gateway derives session state from `symbol_info().trade_mode` first and the schedule
  second; a disagreement is logged and treated as CLOSED.

## 4. Order rules used by the execution engine

| Rule | Implementation |
| --- | --- |
| Market order | `TRADE_ACTION_DEAL`, `type` BUY/SELL, `volume` 0.01, `sl`/`tp` attached, `deviation` in points (ignored on market execution but set), `type_filling` from the probed `filling_mode` bitmask (KU-012), `type_time` GTC, `magic` = bundle id hash, `comment` = proposal id |
| Pre-check | `order_calc_margin` then `order_check`; proceed only on retcode 0/`TRADE_RETCODE_DONE` equivalents in the check result |
| Stops | `trade_stops_level` is reported as ~0 at Exness, but SL/stop orders cannot sit inside the spread; the engine places SL at least `max(stops_level, 2) × point` beyond the relevant side and never inside the spread |
| Slippage-free rule | Pending/SL/TP fill at requested price if the gap < 3 × spread (dynamic); otherwise at market. Stops are "best effort at or beyond level" in the cost model |
| Modify stop | `TRADE_ACTION_SLTP` with `position` ticket; only toward profit |
| Close | Opposite `TRADE_ACTION_DEAL` with `position` ticket and the position's volume; partial close by smaller volume (retcode 10038 if too large) |
| Hedging | Account is expected to be `ACCOUNT_MARGIN_MODE_RETAIL_HEDGING`; the engine still allows at most one open position (config) |
| Throttle | ≤ 1 trade request/s sustained; exponential backoff on 10024; coalesce SL/TP modifications |
| HMR | 15 min before to 90 s after high-impact news, 30 min before to ~10 min after the daily break, ~3 h before to 1 h after the weekend: margin can jump to 1:200 or 1:100. Entries are blocked in these windows anyway (RISK_POLICY) |

Return codes handled explicitly: 10004 requote, 10006 reject, 10008 placed, 10009 done, 10010 partial,
10013 invalid, 10014 volume, 10015 price, 10016 stops, 10017 trade disabled, 10018 market closed,
10019 no money, 10021 price off, 10024 too many requests, 10026/10027 autotrading disabled, 10030
invalid fill, 10031 no connection, 10033/10034/10040 limits, 10038 close volume, 10046 hedge
prohibited (Verified list in `research/02_exness_mt5.md` §1a).

## 5. Data

- **Live:** `symbol_info_tick()` polled at 250 ms during OPEN (configurable), sampled to 1 s rows
  (bid, ask, spread, time_msc); 1 m bars built in code and cross-checked against `copy_rates_from`
  M1 daily.
- **History:** Exness Personal Area tick-history CSV (`Exness, Symbol, Timestamp, Bid, Ask`; MT4 Real
  server sourced, Verified) for backtests; MT5 `copy_rates_*` and `copy_ticks_range` for recent data;
  depth measured in Phase 3 (KU-003). All timestamps UTC.
- **Calendar:** BLS iCal, Fed FOMC page (Verified, public); ForexFactory JSON optional (KU-013).

## 6. Demo environment

- Demo Standard MT5 account; balance settable in the Personal Area; conditions stated as identical to
  real (Verified) but per-client dynamic rules (slippage-free range, HMR duration) are not exercised,
  so demo slippage is a lower bound (Assumption).
- Demo accounts are deleted after unstated inactivity; a heartbeat trade or balance operation every
  14 days is scheduled (KU-015).
- Demo balance for Phase 7: set to the live amount (50 USD) for a faithful rehearsal, and a second
  demo at 5,000 USD to exercise sizing logic (both configs committed).

## 7. Hosting (D-018)

Owner's Windows PC: MT5 terminal, Python 3.12, SQLite. Requirements to measure in Phase 3 (KU-020):
RAM ≥ 8 GB, SSD, stable internet, PC awake during gold hours, Windows Update deferred to weekends.
Ping to the assigned server from `terminal_info().ping_last`. A paid Windows VPS near Amsterdam is
the documented upgrade path if uptime is a problem.

## 8. Phase 3 acceptance checklist (the 16 on-account tests)

Copied from `research/02_exness_mt5.md` §(a) and tracked as `MT5-T01` to `MT5-T16`: connectivity and
identity; symbol discovery; contract spec dump; filling-mode probe; margin pre-check incl. HMR; stops
rule probe; partial close; hedge + close-by; pending limit and `[SL violated]`; slippage logging (20
demo + 20 live 0.01-lot orders); swap booking (informational, intraday system); commission booking (expect
0 on Standard); session/hours polling across the break; history depth; failure drills (AutoTrading
off, network off, investor password, request burst); latency (100 round trips, p50/p95).

## 9. Failure modes specific to MT5 (from research §10)

Covered in `ARCHITECTURE.md` §5: terminal disconnect, AutoTrading disabled, investor password,
symbol close-only, maintenance windows, DST schedule shift, stale data, rate limiting, position/volume
limits, HMR margin spikes.

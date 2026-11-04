# Known Unknowns

Open questions, each with its impact, how it will be resolved, and the phase in which it
must be closed. Items are closed by moving the answer into the relevant doc and marking
the row Resolved with a link.

| ID | Unknown | Impact | Resolution method | Close by | Status |
| --- | --- | --- | --- | --- | --- |
| KU-001 | Exact XAUUSD specs on the owner's Exness account (contract size, volume min/max/step, stops/freeze level, swap long/short, swap mode, 3-day swap day, trading sessions, filling modes, margin tiers) | Sizing, SL placement, cost model, backtest realism | Read `symbol_info()` and `account_info()` on demo and live; record output in `MT5_EXNESS_INTEGRATION.md` | Phase 3 | Open |
| KU-002 | Exness entity and account type of the owner's account | Leverage caps, commissions, swap-free eligibility | Owner answer (brief Q1) + `account_info()` | Phase 2 | Open |
| KU-003 | Depth of M1 and tick history available from the Exness MT5 server | Backtest length, walk-forward feasibility | Call `copy_rates_from` / `copy_ticks_range` with progressively older dates; record first available timestamp | Phase 3 | Open |
| KU-004 | Real spread profile of XAUUSD on the account by hour-of-day and around news | Cost model, session filters | Record `symbol_info_tick` every second for ≥2 weeks on demo and live; build hourly spread percentiles | Phase 3 | Open |
| KU-005 | Live slippage vs demo slippage | Backtest-to-live gap | Compare requested vs filled price on limited-live trades against demo; report in Phase 7/8 | Phase 8 | Open |
| KU-006 | Local hardware available to the owner (GPU, VRAM, OS) | Model tier, latency targets | Owner answer (brief Q3); benchmark on the actual box | Phase 2 | Open |
| KU-007 | Measured latency and JSON-validity rate of candidate local models on our tasks | Model selection, fallback thresholds | Eval harness in Phase 5 | Phase 5 | Open |
| KU-008 | Exness server time zone and DST behaviour | Session windows, rollover timing | `symbol_info` session data + compare server time to UTC across a DST change; check Exness help centre | Phase 3 | Open |
| KU-009 | Whether Exness demo applies swap and commission identically to live | Demo results validity | Compare demo deal history fields (`commission`, `swap`) to spec; Exness help centre | Phase 3 | Open |
| KU-010 | Exness policy limits on EAs (order rate, max positions, prohibited strategies) | Execution engine throttles | Exness client agreement + help centre; test on demo | Phase 2/3 | Open |

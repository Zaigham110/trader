# RISK_POLICY — configurable limits, kill switches, incident response

Status: Phase 2 draft. Last updated: 2026-11-04. Implements R-050 to R-054. Risks are `RK-nnn`.
Two layers: **owner section** (hash-checked, bots cannot write) and **hard ceilings** compiled into
code that config cannot exceed.

## 1. Normal-practice defaults (for the 5,000 USD demo profile and future capital)

| Limit | Default | Hard ceiling |
| --- | --- | --- |
| Risk per trade (stop distance × volume) | 0.5% of equity | 2% |
| Daily loss (realised + floating) | 2% | 5% |
| Weekly loss | 4% | 10% |
| Max drawdown from equity high → kill switch | 10% | 20% |
| Max open positions | 1 | 3 |
| Max volume per position | computed from risk | 1.00 lot |
| Max spread to enter | 2 × the hour's median spread, and ≤ 60 pips | 100 pips |
| Max orders per minute | 5 | 20 |
| Pre-break flatten buffer | 15 min before the daily break | ≥ 5 min |
| News window (high impact) | no entries 30 min before to 15 min after; flatten 5 min before if configured | ≥ 15/5 |
| Fix windows (10:30 and 15:00 London) | no entries 3 min before to 3 min after | ≥ 1 min |
| Stop distance | 0.3–1.5 × ATR14 | ≤ 3 × ATR14 |
| Minimum reward:risk | 1.0 | ≥ 0.5 |
| Consecutive-loss cool-down | 3 losses → 60 min pause | ≥ 15 min |
| Margin level floor for new entries | 300% after the order | ≥ 150% |

## 2. Owner exception profile for live capital under 50 USD (D-017)

The owner was told that a 1-oz minimum trade on a 50 USD account makes normal limits impossible and
chose to proceed. This profile is a documented exception, not a recommendation.

| Limit | Exception value | Note |
| --- | --- | --- |
| Risk per trade | up to 30% of equity (stop ≤ 15 USD at 50 USD equity) | Every trade message shows the actual % |
| Daily loss | 40% | Then no entries until the next session day |
| Max drawdown kill switch | 60% from the starting live equity | Owner reset required |
| Max positions / volume | 1 position, 0.01 lot, hard | |
| Minimum equity to enter | 30 USD | Below it the bot is flat and alerts |
| Leverage setting | 1:500 recommended (8 USD margin per 0.01 lot) | HMR windows blocked anyway (KU-022) |
| Reward:risk | ≥ 1.0 | |

The hard ceilings for this profile are compiled separately (`ceilings.micro`) and apply only when
`account.profile = micro_live` is set in the owner section. Standard Cent (`XAUUSDc`) is the documented
way back to the normal profile without code changes.

## 3. Kill switch (R-051)

Triggers: owner Telegram `/kill` (confirm button), drawdown breach, safety violation (position without
stop that cannot be repaired, spec change, repeated order rejects), connection lost > N min with an
open unprotected position. Actions: close all positions at market, cancel pending orders, set
`bot_state.killed`, block all entries, alert. Reset: owner only, via a dedicated Telegram command with
confirmation, after the incident record is acknowledged.

## 4. Permissions and ceilings enforcement

- Owner section hash checked at startup and before every order; mismatch → kill switch.
- Config loader rejects values above ceilings; the previous valid config stays active.
- Bots and the learning module have no write path to the owner section (static test).
- Telegram `set_config_value` prepared by the Chat bot refuses owner-section keys.
- Risk engine is a pure function with a property test: across randomised accounts, markets and
  requests, no approved order can lose more than the configured risk per trade plus modelled costs at
  its stop, and every limit holds.

## 5. Safe responses (R-052)

| Event | Response |
| --- | --- |
| Lost connection to terminal or server | Block entries; rely on broker-side SL; reconnect loop; alert at 60 s; kill switch if unprotected position and > 5 min |
| Stale data (no tick > 10 s in OPEN) | Block entries; treat as CLOSED for risk; alert at 60 s |
| Failed order send | Typed error; no blind retry on 10016/10019/10030; alert; 3 rejects in 10 min → pause entries |
| Failed stop placement after fill | Immediate market close; alert; incident |
| AutoTrading disabled | Block entries; alert; owner re-enables |
| Anthropic API down or budget hit | Bots pause; deterministic code keeps stops, flattening and kill switch working; entries only if `fallback_mode` allows (default `flat`) |
| PC restart | Reconcile first; resume entries only if health green and session OPEN |
| Spec or schedule change | Fail closed; owner confirms |

## 6. Prohibited logic (D-008)

Martingale, grid, averaging down, position adding after a loss, and any "recovery" sizing are not
implemented and are rejected by the risk engine (`max_positions = 1`, no size increase on an existing
losing position). Enabling any of it requires an owner-approved decision entry after a written risk
review.

## 7. Incident response

1. Detection → `incidents` row with severity, Telegram alert (critical bypasses quiet hours).
2. Automatic containment per §5.
3. Owner acknowledgement via Telegram button; the incident stays open until acknowledged; an open
   incident forces risk budget 0.
4. Post-incident: Health bot summary (Haiku) of logs; owner decides on resume; entry in `DECISIONS.md`
   if a rule changes.

## 8. Risk register

| ID | Risk | Mitigation |
| --- | --- | --- |
| RK-001 | Oversized per-trade risk at 50 USD (D-017) | Exception profile, hard 0.01-lot cap, kill switch at 60%, Cent path documented |
| RK-002 | Gold crash day (−11% on 30 Jan 2026) | Broker-side stop on every position; intraday only; news windows; vol-scaled stops |
| RK-003 | Spread blowout at open/news/break | Spread cap; windows; slippage logged; stops modelled as best effort |
| RK-004 | HMR margin spike | Entries blocked in HMR windows; `order_check` before send; margin floor |
| RK-005 | LLM proposes a bad trade | Deterministic signal must agree; gates; risk engine; one position |
| RK-006 | Prompt injection via news | Quarantine, defang, no state tools for the reader |
| RK-007 | Overfitting in the learning loop | Trial ledger, PBO, DSR, haircut, sample minimums, BH |
| RK-008 | API cost exceeds budget | Daily/monthly caps; tiering; caching; batch for nightly review |
| RK-009 | PC uptime | Soak test; VPS upgrade path |
| RK-010 | Credential leak | Keyring, redaction tests, no secrets in config or Telegram |
| RK-011 | Demo-to-live gap in slippage | Shadow stage, fill-quality monitor, live limited stage |
| RK-012 | Exness disables the EA for request flooding | Throttle, backoff, coalesced modifications |

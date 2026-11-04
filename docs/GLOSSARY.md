# GLOSSARY

| Term | Meaning in this project |
| --- | --- |
| ATR14 | Average true range over 14 periods; the volatility unit for stops and sizing. ~81 USD on gold on 2026-11-04. |
| Bundle | Immutable version set (code commit, parameters, prompt hashes, model IDs, config hash) stamped on every decision and order. |
| Champion / challenger | The live strategy bundle versus a candidate compared on the same quotes under a sequential test. |
| Close-only | Exness session state in which positions can be closed but not opened (e.g. 23:00:30–23:02 UTC winter). |
| CPCV | Combinatorial purged cross-validation; produces a distribution of out-of-sample results instead of one path. |
| Daily break | Gold's nightly closure on Exness (21:58–23:02 UTC winter, 20:58–22:02 summer). |
| Deflated Sharpe Ratio (DSR) | Sharpe corrected for the number of trials, non-normality and sample length; gate ≥ 0.95. |
| Degraded | Flag on a record produced by a rule-based default after the LLM failed or returned invalid output. |
| Demo | Exness demo account; conditions stated identical to real, per-client dynamics not exercised. |
| DST | US daylight saving time; shifts the Exness gold schedule by one hour while server time stays UTC+0. |
| e-value | Anytime-valid evidence measure for sequential champion/challenger tests; promote at > 20. |
| Execution engine | The only code that places, modifies or closes orders; runs the risk engine before every send. |
| Fail flat | Default response to uncertainty: no new entries, existing positions keep their broker-side stop. |
| Fix (LBMA Gold Price) | Twice-daily auction at 10:30 and 15:00 London; a volatility event, blocked for entries. |
| Gate | Deterministic check a proposal must pass (code gates, risk engine, promotion gates). |
| Gateway | The Python process that owns MT5 I/O, risk, execution and reconciliation. |
| Brain | The Python process that owns bots, LLM calls, learning and Telegram. |
| HMR | Higher margin requirements: Exness raises gold margin to 1:200–1:100 around news, breaks and weekends. |
| Intent | A gated request from the brain to the gateway to enter, exit, move a stop, pause or kill. |
| Kill switch | Closes everything, cancels orders, blocks entries until owner reset. |
| Lot | 100 troy ounces of gold; minimum 0.01 lot = 1 oz. |
| Magic number | MT5 order field used to tag orders with the bundle id. |
| Micro-live profile | Owner-exception risk profile for live capital under 50 USD (D-017). |
| Owner section | Config part holding risk limits, permissions and safety rules; hash-checked; bots cannot write. |
| PBO | Probability of backtest overfitting; gate ≤ 0.05. |
| Pip | 0.01 USD in gold price; 1 pip on 1.00 lot = 1 USD; on 0.01 lot = 0.01 USD. |
| Point | MT5's smallest price step for the symbol; may equal 0.01 or 0.001 depending on `digits` (KU-001). |
| Proposal | Structured LLM output (`wait|enter|exit|move_stop`) that becomes an intent only after gates. |
| Provenance | The source tag on a chat fact: table or MT5 call plus as-of time. |
| Regime | Deterministic label from ATR and realised-vol percentiles and trend state; gates size and strategy. |
| Retcode | MT5 trade server return code (10009 = done). |
| Shadow | Stage where orders are computed and logged against live quotes but not sent. |
| Slippage-free range | Exness rule: pending/SL/TP fill at the requested price if the gap is under 3× spread. |
| Spread | Ask minus bid; the only trading cost on a Standard account. |
| Standard account | Exness account type with spread-only pricing; symbol suffix `-m`. |
| Standard Cent | Exness account in cents; minimum trade 0.01 oz; the documented alternative to D-017. |
| Stop out | Exness closes positions when margin level reaches 0% (20% on some entities). |
| Swap | Overnight financing; not paid because the system is intraday only. |
| Trial ledger | Append-only record of every backtest, parameter set, prompt and hypothesis evaluation. |
| TSMOM | Time-series momentum; the best-evidenced directional strategy family for gold. |
| Volatility targeting | Sizing overlay scaling exposure inversely to recent volatility; reduces tails. |

# Exness × MetaTrader 5 integration research for an automated gold (XAUUSD) system

Compiled 2026-11-04. Labels: **Verified** = read on a primary page (URL + access date given); **Assumption** = reasonable inference, not read on a primary page; **Unknown** = could not verify; a concrete verification step is proposed.

Method note: `www.exness.com` and the HTML help-centre pages return HTTP 403 (Cloudflare) to automated fetches, but the help centre is a Zendesk instance whose JSON API (`https://get.exness.help/api/v2/help_center/en-us/articles/<id>.json`) serves the same article bodies. Every Exness claim below marked Verified was read from that API on 2026-11-04 and cites the human-readable `hc/en-us/articles/...` URL. mql5.com / metatrader5.com / pypi.org pages were fetched directly.

---

## 1. Integration options for MT5 automation

### 1a. `MetaTrader5` Python package

| Item | Finding | Label |
|---|---|---|
| Install | `pip install MetaTrader5` (upgrade: `pip install --upgrade MetaTrader5`) | **Verified** – https://www.mql5.com/en/docs/python_metatrader5 (accessed 2026-11-04) |
| Latest release | 5.0.6231, released 2026-09-27; requires Python `<4, >=3.6`; classifiers list Python 3.6–3.14 | **Verified** – https://pypi.org/project/MetaTrader5/ (accessed 2026-11-04) |
| Windows-only | PyPI release files are **only** `Windows x86-64` wheels; no Linux/macOS wheels and no source distribution. The mql5.com doc's setup steps link only to the Windows Python download. | **Verified** (by absence of other wheels) – PyPI page above; mql5.com page above |
| Requires a running terminal | `initialize()` "Establish a connection with the MetaTrader 5 terminal"; "If required, the MetaTrader 5 terminal is launched to establish connection when executing the initialize() call." `initialize(path, login=, password=, server=, timeout=60000 ms default, portable=False)`; returns `True`/`False`. | **Verified** – https://www.mql5.com/en/docs/python_metatrader5/mt5initialize_py (accessed 2026-11-04) |
| Transport | IPC between Python and the terminal process (error codes are named `RES_E_INTERNAL_FAIL_SEND/RECEIVE/INIT/CONNECT/TIMEOUT` "Internal IPC …"). | **Verified** – https://www.mql5.com/en/docs/python_metatrader5/mt5lasterror_py (accessed 2026-11-04) |

**Full documented function list (32)** – **Verified** (mql5.com overview page, accessed 2026-11-04): `initialize, login, shutdown, version, last_error, account_info, terminal_info, symbols_total, symbols_get, symbol_info, symbol_info_tick, symbol_select, market_book_add, market_book_get, market_book_release, copy_rates_from, copy_rates_from_pos, copy_rates_range, copy_ticks_from, copy_ticks_range, orders_total, orders_get, order_calc_margin, order_calc_profit, order_check, order_send, positions_total, positions_get, history_orders_total, history_orders_get, history_deals_total, history_deals_get`.

**`last_error()` codes** – **Verified** (page above): `RES_S_OK=1, RES_E_FAIL=-1, RES_E_INVALID_PARAMS=-2, RES_E_NO_MEMORY=-3, RES_E_NOT_FOUND=-4 (no history), RES_E_INVALID_VERSION=-5, RES_E_AUTH_FAILED=-6, RES_E_UNSUPPORTED=-7, RES_E_AUTO_TRADING_DISABLED=-8, RES_E_INTERNAL_FAIL=-10000, RES_E_INTERNAL_FAIL_SEND=-10001, RES_E_INTERNAL_FAIL_RECEIVE=-10002, RES_E_INTERNAL_FAIL_INIT=-10003, RES_E_INTERNAL_FAIL_CONNECT=-10003 (doc lists same value), RES_E_INTERNAL_FAIL_TIMEOUT=-10005`.

**TradeRequest (MqlTradeRequest) fields passed to `order_send()` / `order_check()`** – **Verified** – https://www.mql5.com/en/docs/python_metatrader5/mt5ordersend_py (accessed 2026-11-04): `action, magic, order, symbol, volume, price, stoplimit, sl, tp, deviation, type, type_filling, type_time, expiration, comment, position, position_by`. Notes from the page: `price` is "not set for market orders on Market Execution instruments with TRADE_ACTION_DEAL" (but the example still sets it); `deviation` is "in points"; `position` is the ticket "used to change or close a specific position"; `position_by` is the "Opposite position ticket. It is used when closing a position by an opposite one."

**OrderSendResult fields** (sample output on same page; descriptions not given): `retcode, deal, order, volume, price, bid, ask, comment, request_id, retcode_external, request`. The example tests `result.retcode != mt5.TRADE_RETCODE_DONE`.

**OrderCheckResult fields** – **Verified** – https://www.mql5.com/en/docs/python_metatrader5/mt5ordercheck_py (accessed 2026-11-04): `retcode, balance, equity, profit, margin, margin_free, margin_level, comment, request`.

**Enumerations** – **Verified** (order_check page and https://www.mql5.com/en/docs/constants/tradingconstants/orderproperties, accessed 2026-11-04):

| Enum | Members |
|---|---|
| `TRADE_REQUEST_ACTIONS` | `TRADE_ACTION_DEAL` (market order), `TRADE_ACTION_PENDING`, `TRADE_ACTION_SLTP` (change SL/TP of open position), `TRADE_ACTION_MODIFY` (modify pending order), `TRADE_ACTION_REMOVE` (delete pending), `TRADE_ACTION_CLOSE_BY` |
| `ORDER_TYPE` | `ORDER_TYPE_BUY, ORDER_TYPE_SELL, ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_SELL_LIMIT, ORDER_TYPE_BUY_STOP, ORDER_TYPE_SELL_STOP, ORDER_TYPE_BUY_STOP_LIMIT, ORDER_TYPE_SELL_STOP_LIMIT, ORDER_TYPE_CLOSE_BY` |
| `ORDER_TYPE_FILLING` | `ORDER_FILLING_FOK` ("executed in the specified volume only"), `ORDER_FILLING_IOC` (max available volume, remainder cancelled), `ORDER_FILLING_BOC` (passive/Book-or-Cancel, limit & stop-limit only), `ORDER_FILLING_RETURN` (remainder stays active; the orderproperties page says it "is not allowed in Market Execution mode"; the Python order_check page says it is used only for market/limit/stop-limit orders on symbols with Market or Exchange execution – the two pages word this differently, so **test on the account**) |
| `ORDER_TYPE_TIME` | `ORDER_TIME_GTC, ORDER_TIME_DAY, ORDER_TIME_SPECIFIED, ORDER_TIME_SPECIFIED_DAY` |

The symbol advertises which fill modes it accepts as flags in `symbol_info().filling_mode`: `SYMBOL_FILLING_FOK=1, SYMBOL_FILLING_IOC=2, SYMBOL_FILLING_BOC=4` ("It is possible to set several modes for each instrument via a combination of flags") – **Verified** – https://www.mql5.com/en/docs/constants/environment_state/marketinfoconstants (accessed 2026-11-04). Which flags Exness sets on XAUUSD is **Unknown** → read `symbol_info("XAUUSD").filling_mode` and bitmask-test.

**Trade server return codes to handle** – **Verified** – https://www.mql5.com/en/docs/constants/errorswarnings/enum_trade_return_codes (accessed 2026-11-04):

| Code | Constant | Meaning |
|---|---|---|
| 10004 | `TRADE_RETCODE_REQUOTE` | Requote |
| 10006 | `TRADE_RETCODE_REJECT` | Request rejected |
| 10007 | `TRADE_RETCODE_CANCEL` | Request canceled by trader |
| 10008 | `TRADE_RETCODE_PLACED` | Order placed (pending) |
| 10009 | `TRADE_RETCODE_DONE` | Request completed |
| 10010 | `TRADE_RETCODE_DONE_PARTIAL` | Only part completed |
| 10011 | `TRADE_RETCODE_ERROR` | Request processing error |
| 10012 | `TRADE_RETCODE_TIMEOUT` | Canceled by timeout |
| 10013 | `TRADE_RETCODE_INVALID` | Invalid request |
| 10014 | `TRADE_RETCODE_INVALID_VOLUME` | Invalid volume |
| 10015 | `TRADE_RETCODE_INVALID_PRICE` | Invalid price |
| 10016 | `TRADE_RETCODE_INVALID_STOPS` | Invalid stops |
| 10017 | `TRADE_RETCODE_TRADE_DISABLED` | Trade is disabled |
| 10018 | `TRADE_RETCODE_MARKET_CLOSED` | Market is closed |
| 10019 | `TRADE_RETCODE_NO_MONEY` | Not enough money |
| 10020 | `TRADE_RETCODE_PRICE_CHANGED` | Prices changed |
| 10021 | `TRADE_RETCODE_PRICE_OFF` | No quotes to process request |
| 10022 | `TRADE_RETCODE_INVALID_EXPIRATION` | Invalid expiration |
| 10023 | `TRADE_RETCODE_ORDER_CHANGED` | Order state changed |
| 10024 | `TRADE_RETCODE_TOO_MANY_REQUESTS` | Too frequent requests |
| 10025 | `TRADE_RETCODE_NO_CHANGES` | No changes in request |
| 10026 | `TRADE_RETCODE_SERVER_DISABLES_AT` | Autotrading disabled by server |
| 10027 | `TRADE_RETCODE_CLIENT_DISABLES_AT` | Autotrading disabled by client terminal |
| 10028 | `TRADE_RETCODE_LOCKED` | Request locked for processing |
| 10029 | `TRADE_RETCODE_FROZEN` | Order or position frozen |
| 10030 | `TRADE_RETCODE_INVALID_FILL` | Invalid order filling type |
| 10031 | `TRADE_RETCODE_CONNECTION` | No connection with trade server |
| 10032 | `TRADE_RETCODE_ONLY_REAL` | Only for live accounts |
| 10033 | `TRADE_RETCODE_LIMIT_ORDERS` | Pending-order count limit reached |
| 10034 | `TRADE_RETCODE_LIMIT_VOLUME` | Volume limit for symbol reached |
| 10035 | `TRADE_RETCODE_INVALID_ORDER` | Incorrect/prohibited order type |
| 10036 | `TRADE_RETCODE_POSITION_CLOSED` | Position already closed |
| 10038 | `TRADE_RETCODE_INVALID_CLOSE_VOLUME` | Close volume exceeds position volume |
| 10039 | `TRADE_RETCODE_CLOSE_ORDER_EXIST` | Close order already exists |
| 10040 | `TRADE_RETCODE_LIMIT_POSITIONS` | Open-position count limit (netting counts positions; hedging also counts pending orders) |
| 10041 | `TRADE_RETCODE_REJECT_CANCEL` | Pending activation rejected, order cancelled |
| 10042/10043/10044 | `TRADE_RETCODE_LONG_ONLY / SHORT_ONLY / CLOSE_ONLY` | Symbol direction restriction (maps to `SYMBOL_TRADE_MODE_*`) |
| 10045 | `TRADE_RETCODE_FIFO_CLOSE` | FIFO rule on account |
| 10046 | `TRADE_RETCODE_HEDGE_PROHIBITED` | Opposite positions disabled (retail hedging accounts) |

**Data functions** – **Verified** (https://www.mql5.com/en/docs/python_metatrader5/mt5copyratesfrom_py and …/mt5copyticksrange_py, accessed 2026-11-04): `copy_rates_from(symbol, timeframe, date_from, count)` returns a numpy array with columns `time, open, high, low, close, tick_volume, spread, real_volume`; "the terminal supplies bars only from the history it holds for charts. The amount is set by 'Max. bars in chart'"; bar/tick times are stored "in UTC time zone (without the shift)" — build `datetime` objects in UTC. `copy_ticks_range(symbol, date_from, date_to, flags)` with `COPY_TICKS_ALL / COPY_TICKS_INFO / COPY_TICKS_TRADE`; tick columns `time, bid, ask, last, volume, time_msc, flags, volume_real` (same fields for `symbol_info_tick()` – **Verified** …/mt5symbolinfotick_py). `positions_get(symbol=|group=|ticket=)` returns `ticket, time, time_msc, time_update, time_update_msc, type, magic, identifier, reason, volume, price_open, sl, tp, price_current, swap, profit, symbol, comment, external_id` (**Verified** …/mt5positionsget_py). `history_deals_get(date_from, date_to, group=) | (ticket=) | (position=)` returns `ticket, order, time, time_msc, type, entry, magic, position_id, reason, volume, price, commission, swap, profit, fee, symbol, comment, external_id` (**Verified** …/mt5historydealsget_py). `account_info()` fields: `login, trade_mode, leverage, limit_orders, margin_so_mode, trade_allowed, trade_expert, margin_mode, currency_digits, fifo_close, balance, credit, profit, equity, margin, margin_free, margin_level, margin_so_call, margin_so_so, margin_initial, margin_maintenance, assets, liabilities, commission_blocked, server, currency, company, name` (**Verified** …/mt5accountinfo_py). `terminal_info()` fields: `community_account, community_connection, connected, dlls_allowed, trade_allowed, tradeapi_disabled, email_enabled, ftp_enabled, notifications_enabled, mqid, build, maxbars, codepage, ping_last, community_balance, retransmission, company, name, language, path, data_path, commondata_path` (**Verified** …/mt5terminalinfo_py).

Pros/cons/latency: **Assumption** — the Python↔terminal hop is local IPC (sub-millisecond scale), so round-trip latency is dominated by terminal↔Exness server; `terminal_info().ping_last` exposes the terminal's measured ping (**Verified** field exists; units not stated on the Python page → **Unknown**, likely microseconds as in MQL5 `TERMINAL_PING_LAST`, verify). Cons: Windows-only, single terminal per Python process (`initialize()` has one `path`), synchronous calls, and the package exposes no event/callback — you must poll `symbol_info_tick()`/`copy_ticks_from()`.

### 1b. Native MQL5 Expert Advisor + bridge

| Fact | Label / source |
|---|---|
| "Each script, each service and each Expert Advisor runs in its own separate thread"; while a NewTick is being processed "no other events of this type are received" and a new NewTick "is not placed in the queue" (ticks arriving mid-handler are dropped, not queued). "All indicators calculated on one symbol, even if they are attached to different charts, work in the same thread." | **Verified** – https://www.mql5.com/en/docs/runtime/running (accessed 2026-11-04) |
| `WebRequest` (two overloads; returns HTTP code or -1) is **synchronous/blocking**; "Each server address must be added to the allowed list on the 'Expert Advisors' tab of the Options window"; cannot be called from indicators (error 4014) or in the Strategy Tester; timeout in ms; port inferred from scheme (80/443). | **Verified** – https://www.mql5.com/en/docs/network/webrequest (accessed 2026-11-04) |
| Raw TCP sockets exist in MQL5: `SocketCreate, SocketClose, SocketConnect, SocketIsConnected, SocketIsReadable, SocketIsWritable, SocketTimeouts, SocketRead, SocketSend, SocketTlsHandshake, SocketTlsCertificate, SocketTlsRead, SocketTlsReadAvailable, SocketTlsSend`. The allowed-address list also governs `Socket*` calls; a program cannot add entries programmatically. | **Verified** – https://www.mql5.com/en/docs/network (accessed 2026-11-04) |
| ZeroMQ / named-pipe bridges require a DLL and "Allow DLL imports" in the terminal; Exness's own EA guide tells users to tick "Allow algorithmic trading", "Allow DLL imports" and/or "Allow WebRequest for listed URL" as the EA requires. | **Verified** – https://get.exness.help/hc/en-us/articles/360019530859-Using-Expert-Advisors-EA (accessed 2026-11-04) |
| Exness explicitly supports EAs on desktop MT4/MT5 only ("they do not operate on mobile or web versions") and warns an EA may be disabled server-side if settings are "too aggressive… overload of the terminal with excessive submissions and irrelevant requests". | **Verified** – same article |

Design implication (**Assumption**): because `OnTick` drops ticks while busy and `WebRequest` blocks, an EA bridge should (a) never call the LLM/analytics service inside `OnTick`; (b) push ticks over a non-blocking socket or write to a file/pipe, and (c) poll a local decision queue with `OnTimer`. Latency of EA→Python via localhost socket is **Unknown** (expect ~ms; measure).

### 1c. Linux options

| Option | Finding | Label |
|---|---|---|
| Official Wine install | MetaQuotes script `mt5linux.sh` (`wget … ; chmod +x mt5linux.sh ; ./mt5linux.sh`), detects Ubuntu/Debian/Linux Mint/Fedora, installs matching Wine, needs Mono+Gecko, then reboot; files under `~/.mt5/drive_c/Program Files/MetaTrader 5`. The page does not mention Python. | **Verified** – https://www.metatrader5.com/en/terminal/help/start_advanced/install_linux (accessed 2026-11-04) |
| `mt5linux` (PyPI) | v1.1.1 released 2026-08-04 (gap from 2022 to Feb 2026, then frequent releases); uses Wine + RPyC + a standalone `mt5server.exe` (Windows binary run under Wine next to the terminal), default port 18812, with Docker-compose or library-managed container options; `py3-none-any` wheel; no provenance/Trusted Publishing metadata. | **Verified** – https://pypi.org/project/mt5linux/ (accessed 2026-11-04). Maturity: **Assumption** – community-maintained, no vendor support; treat as a lab tool, not a production path. |
| Docker images | Various community images wrap the Wine install; none are MetaQuotes-official. | **Unknown** – not verified; evaluate by building from the official script in a Dockerfile. |
| Stability under Wine | MetaQuotes page recommends keeping OS and Wine updated for stability; says nothing about Python support. | **Verified** (page above). Whether the `MetaTrader5` Python wheel works via Windows-Python-under-Wine is **Unknown** here (mt5linux's design implies it does; test). |
| MetaApi (metaapi.cloud) | Supports MT4 and MT5 ("Supports any MetaTrader broker"); REST API (primary use: monitoring) and WebSocket/socket.io streaming API (recommended for trading). Billed only while the account's API server is deployed; "you are billed for 6 hours each time you start your server"; median added execution latency "6ms", median streaming latency "<1ms"; demo accounts supported; investor-password read-only mode. Rate limits: CPU-credit system (e.g. 1,000 credits/s per application, trading calls cost 10 credits, ticks 0.1 + 0.0002/tick); streaming data 31,500 credits/10 s per account; HTTP 429 with `recommendedRetryTime`. Regions are "several"; exact list not read. | **Verified** – https://metaapi.cloud/docs/client/ , https://metaapi.cloud/docs/client/faq/ , https://metaapi.cloud/docs/client/rateLimiting/ (accessed 2026-11-04) |
| MetaApi pricing | Search-engine snippet of metaapi.cloud homepage: "Regular" $30/month and "Extended" $100/month subscriptions, API access billed pay-as-you-go on top, free tier/7-day trial with one free MT account. The pricing block could not be read in the fetched page body. | **Unknown** (snippet only) → open https://metaapi.cloud/#pricing manually before budgeting. |

### 1d. Exness's own APIs

| Finding | Label |
|---|---|
| **"Exness API"** exists: REST (orders, histories) + WebSocket (streaming). "Only Exness trading accounts are eligible to use the Exness API. Standard MetaTrader accounts cannot connect to this API." Needs full KYC (POI+POA); API key + secret from Personal Area; "The current API version does not support stock and indices instruments"; real-time price feed "may typically be limited to between 100ms and 500ms"; rate limits per method, queryable via a `getRateLimits` endpoint; Exness states trading conditions are identical across MT, Terminal, app and API. | **Verified** – https://get.exness.help/hc/en-us/articles/27866287512476-Exness-API (accessed 2026-11-04) |
| Regional restriction ("only available in Vietnam and Thailand regions at the moment") appeared in a search snippet of the same article but is **not** in the article body fetched today. | **Unknown** → check the Personal Area "API" page for your entity. |
| "Exness Partnership API" is a **separate** IB/affiliate API (rebates, campaigns, partner reports) – not a trading API. | **Verified** via search snippet of https://help.exnessaffiliates.com/hc/en-us/articles/360022982091-The-Exness-Partnership-API (page itself returned a proxy 502; label as snippet-only) |
| FIX API | No Exness help-centre article or exness.com page mentioning FIX was found. | **Unknown** → ask Exness support / institutional desk; assume none for retail. |

Note: the Exness API is for "Exness trading accounts" (the Exness Terminal/Trade-app account family), not MT5 accounts. If the system must hold an MT5 account (e.g. for the free VPS or for MT5 history), the Exness API is not an alternative for that same account.

---

## 2. Exness account types (as of 2026-11-04)

All rows **Verified** from the account articles (accessed 2026-11-04): Standard https://get.exness.help/hc/en-us/articles/17537782738460-Standard-account ; Standard Cent …/17537782786588-Standard-Cent-account ; Pro …/17537794560540-Pro-account ; Raw Spread …/17537767270556-Raw-Spread-account ; Zero …/17537782878236-Zero-account ; margin call/stop-out …/360014693340-Margin-call-and-stop-out-levels-by-account-type ; minimum deposit …/360011554840-What-is-the-minimum-amount-needed-to-start-trading.

| | Standard | Standard Cent | Pro | Raw Spread | Zero |
|---|---|---|---|---|---|
| Min first deposit | "may vary based on payment method or geographic location" | same | "Starts from 200 USD, depending on geographic region" | from 200 USD | from 200 USD |
| Execution | Market (no requotes) | Market (no requotes) | Instant (requotes apply); Market only if account currency is USD, JPY, THB, CNY, IDR, VND | Market | Market |
| Spread "from" | 0.2 pips | 0.3 pips | 0.1 pips | 0.0 pips | 0.0 pips* |
| Commission | none | none | none | "Up to 3.50 USD/lot per side"; XAUUSD = 3.5 USD/lot/side | "From 0.05 USD/lot per side"; XAUUSD = **5.5 USD/lot/side** |
| Max leverage (non-KE) | 1:Unlimited | 1:Unlimited | 1:Unlimited | 1:Unlimited | 1:Unlimited |
| Margin call | 60% | 60% | 30% | 30% | 30% |
| Stop out | 0% (100% during stocks daily break) | 0% | 0% | 0% | 0% |
| Hedged margin | 0% | 0% | 0% | 0% | 0% |
| Min volume | 0.01 lot | 0.01 lot (= 100,000 cents) | 0.01 | 0.01 | 0.01 |
| Max volume | Day 200 (07:00–20:59 GMT+0) / Night 60 (21:00–06:59), "may depend on the instrument" | 200 day & night | as Standard | as Standard | as Standard |
| MT5 Real max orders | Unlimited open (1,000 pending) | Real23/40/42/46 servers: 100; other MT5 Real: 200 (open+pending combined) | Unlimited (1,000 pending) | Unlimited (1,000 pending) | Unlimited (1,000 pending) |
| MT5 Demo max orders | 1,024 (1,024 pending) | no demo | 1,024 | 1,024 | 1,024 |
| Symbol suffix (MT5) | `-m` (XAUUSDm) | `-c` (XAUUSDc) | none (XAUUSD) | none or `-r` | none or `-z` |
| XAUUSD available | yes | yes (XAUUSDc) | yes | yes | yes |

\*Zero: "zero spread on the top 30 traded instruments during 95% of the day… floating spread during key periods such as economic news and rollovers". XAUUSD is listed by Exness among instruments with "stable spreads nearly 90% of the time" (**Verified** – https://get.exness.help/hc/en-us/articles/360014690880-About-spread, accessed 2026-11-04). Numeric typical XAUUSD spreads per account: **Unknown** (website shows previous-day averages; blocked) → sample `symbol_info().spread` over a week.

**Suffix table** – **Verified** – https://get.exness.help/hc/en-us/articles/360014560220-Account-type-suffixes (accessed 2026-11-04). "Metals are available for trading as currency pairs. You can find them in the Forex symbol group."

**Swap-free** – **Verified** – https://get.exness.help/hc/en-us/articles/4402341895570-Swap-free-status and …/18055079599132-Swap-free-status-Islamic-countries (accessed 2026-11-04): status applies to the whole Personal Area; "By default, all accounts are swap-free"; it is kept or lost by an algorithm ("If you do not trade mostly during trading hours and regularly hold large amounts of open orders overnight, you may be unqualified"); swap-free instrument list includes **XAUUSD, XAUAUD, XAUEUR, XAGUSD, XPTUSD, USOIL, UKOIL**, majors, listed minors, USDZAR, USDTHB, all crypto, all indices; "Swaps are charged for all other metals, energies, minor pairs, exotic pairs and stocks". Clients registered in the listed Islamic countries get automatic swap-free status on **all** instruments. The term "Extended swap-free" does **not** appear in either current article → **Unknown**/possibly retired naming; check "Trading conditions → Swap-Free" in the PA.

**Entities** (specs vary by entity):

| Entity | Regulator | Label |
|---|---|---|
| Exness (SC) Ltd, Seychelles – FSA licence SD025 | Named in help-centre search snippets; client categorization article confirms "Seychelles entity (Exness (SC) Limited)" | **Verified** (categorization) – https://get.exness.help/hc/en-us/articles/27004655321116-Client-categorization (accessed 2026-11-04); licence no. snippet-only |
| Exness B.V., Curaçao – CBCS 0003LSI | snippet of help centre | **Unknown** (snippet) |
| Exness (VG) Ltd – BVI FSC | snippet | **Unknown** (snippet) |
| Exness (KE) Limited – Kenya CMA: max leverage 1:400, stop out 20%, no Stop Out Protection | **Verified** – Leverage, stop-out, account articles above |
| Exness (JO) Limited / Exness Limited Jordan: stop out 20%, dynamic leverage up to 1:100 on migrated instruments | **Verified** – stop-out article; …/30743728061724-Leverage-increases-for-selected-instruments |
| Exness MENA Financial Brokers L.L.C.: no unlimited leverage | **Verified** – …/18055266964892-Unlimited-leverage |
| Exness ZA (PTY) Ltd – FSCA | snippet | **Unknown** (snippet) |
| Exness (Cy) Ltd – CySEC 178/12; Exness (UK) Ltd – FCA 730729 | Only secondary sources (Finance Magnates, review sites) say these are B2B / professional-only and that Exness does not onboard EEA/UK retail | **Unknown** → confirm on exness.com/regulation (blocked here) and on the CySEC/FCA registers |

Exness tells clients to read their entity from the Personal Area footer (**Verified** – …/360014539320-Is-Exness-regulated). Your counterparty is that entity acting as market maker: "liquidity and execution are provided internally" (**Verified** – https://get.exness.help/hc/en-us/articles/26544238207260-Execution, accessed 2026-11-04).

---

## 3. XAUUSD contract specification at Exness

**Verified** – https://get.exness.help/hc/en-us/articles/17854173039388-Commodities (updated 2026-11-03, accessed 2026-11-04):

| Spec | XAUUSD | XAUUSD247 (7-day gold) |
|---|---|---|
| Account types | Standard, Standard Cent, Pro, Raw Spread, Zero | Standard, Pro, Raw Spread, Zero |
| Platforms | Exness Terminal, MT5, MT4 | same |
| Execution | Market; Instant for Pro only | same |
| Symbol group | "Forex and Forex_Group" | same |
| Contract size | **100 troy ounces** | 100 troy ounces |
| Pip size | 0.01 | 0.01 |
| Min / max volume | 0.01 / 200 lots | 0.01 / 200 lots |
| Max leverage | "1:Unlimited if eligible" | "Fixed margin requirements: 1:200 (0.5%)" |
| HMR | "1:200 – 1:1000 (0.1%-0.5%)" | n/a (fixed) |
| Hedged margin | 0% | 0% |
| Swap | "Triple swap on Wednesday (swap schedule may change due to public/bank holidays)" | same |
| Commission | Raw Spread 3.5 USD, Zero 5.5 USD (per lot, per side) | same |
| Stop level | "Refer to trading platform" | "100% on daily breaks and weekends" |

Not stated on the page (→ **Unknown**, read from the terminal): lot step (0.01 implied by min volume but not stated), tick size vs pip (MT5 `point` will likely be 0.01 with `digits`=2 or 0.001 with `digits`=3 — **Unknown**), freeze level, swap long/short values, max aggregate volume per direction, margin currency.

Exness general stop-level policy – **Verified** – https://get.exness.help/hc/en-us/articles/360011676519-About-stop-levels (accessed 2026-11-04): table shows Buy limit "below current ask at -1 point", Sell limit "above current bid at +1 point", Buy stop "+1 point above ask", Sell stop "-1 point below bid"; "We do not have stop levels for most of our clients… Stop levels may not be available for traders using specific high-frequency trading strategies or Expert Advisors"; buy/sell stop and SL "cannot be set within the spread"; limit orders and TP "can be set within the spread"; pending orders at exactly the current price are rejected. Zero Stop Levels is listed as a per-account "trading condition" in the PA (**Verified** – …/17537777171996-PA-Exness-benefits).

Trading hours (server = UTC+0) – **Verified** – https://get.exness.help/hc/en-us/articles/31342732737180-Trading-Hours-Update-End-of-US-Daylight-Saving-Time-01-Nov-2026 (accessed 2026-11-04), "New Trading Hours (After DST)" column, applicable now (winter):

| Day | XAUUSD (UTC) |
|---|---|
| Sat | Closed all day |
| Sun | Closed 00:00–23:01:30; Close-only 23:01:30–23:05; Open 23:05–23:59:59 |
| Mon–Thu | Open 00:00–21:58; Closed 21:58–23:00:30; Close-only 23:00:30–23:02; Open 23:02–23:59:59 |
| Fri | Open 00:00–21:58; Closed 21:58–23:59:59 |

Summer (US DST) hours shift one hour earlier (open 22:05 Sun, daily break 20:58–22:02). XAUUSD247 trades Saturday and most of Sunday with a short daily break. "Gold (XAUUSD) is usually closed during forex rollover" (**Verified** – …/4405235684498-Instrument-trading-hours). Swap time: 21:00 GMT+0 summer / 22:00 winter, Mon–Fri, metals triple on Wednesday (**Verified** – …/360014709151-About-swap). HMR around the gold daily break: 30 min before, ~10 min after (**Verified** – HMR article).

**Authoritative spec = `symbol_info()` on the live account.** Fields that exist on the returned structure (**Verified** – https://www.mql5.com/en/docs/python_metatrader5/mt5symbolinfo_py, accessed 2026-11-04) and should be read and logged at startup: `name, path, description, currency_base, currency_profit, currency_margin, digits, point, spread, spread_float, trade_calc_mode, trade_mode, trade_exemode, trade_stops_level, trade_freeze_level, trade_contract_size, trade_tick_size, trade_tick_value, trade_tick_value_profit, trade_tick_value_loss, volume_min, volume_max, volume_step, volume_limit, swap_mode, swap_long, swap_short, swap_rollover3days, margin_initial, margin_maintenance, margin_hedged, margin_hedged_use_leg, filling_mode, order_mode, order_gtc_mode, expiration_mode, start_time, expiration_time, session_deals, session_buy_orders, session_sell_orders, session_open, session_close, bid, ask, last, time, visible, select`. Decode with the MQL5 enums (**Verified** – marketinfoconstants page): `trade_mode` → `SYMBOL_TRADE_MODE_DISABLED/LONGONLY/SHORTONLY/CLOSEONLY/FULL`; `trade_exemode` → `SYMBOL_TRADE_EXECUTION_REQUEST/INSTANT/MARKET/EXCHANGE`; `swap_mode` → `SYMBOL_SWAP_MODE_DISABLED/POINTS/CURRENCY_SYMBOL/CURRENCY_MARGIN/CURRENCY_DEPOSIT/INTEREST_CURRENT/INTEREST_OPEN/REOPEN_CURRENT/REOPEN_BID`; `swap_rollover3days` = "day of week to charge 3-day swap rollover" (expect Wednesday for gold); `trade_stops_level` = "Minimal indention in points from the current close price to place Stop orders"; `trade_freeze_level` = "Distance to freeze trade operations in points"; `volume_limit` = "Maximum allowed aggregate volume of an open position and pending orders in one direction"; `margin_hedged` = margin per lot of hedged positions (Exness says 0%).

Max positions: Exness caps **pending** orders (MT5 Real: 1,000 for all non-Cent types) and says open positions are "Unlimited" on MT5 Real for Standard/Pro/Raw/Zero (**Verified** – account articles and …/360012824139--Too-many-orders-error). Cross-check with `account_info().limit_orders` (`ACCOUNT_LIMIT_ORDERS` = "Maximum allowed number of active pending orders" – **Verified** – https://www.mql5.com/en/docs/constants/environment_state/accountinformation).

---

## 4. Order rules

| Rule | Finding | Label |
|---|---|---|
| Market execution | "Orders are executed at the current price at the moment of order processing… This price change is called slippage and is common with market execution"; Standard/Cent/Raw/Zero are market execution "(no requotes)". Instant execution (Pro) returns a 3-second requote notification; `deviation` only matters for instant execution. | **Verified** – …/360017885880-Available-order-and-execution-types ; …/360011802860-Requotes ; …/4402644779538-Setting-deviation (accessed 2026-11-04) |
| Slippage rule for pending orders/SL/TP | If the gap between requested and market price at execution is **less** than the instrument's "slippage-free range" the order fills at the requested price; otherwise at market. For **XAUUSD the range is "0 – (3x spread)"** at execution time; on Raw/Zero the commission per lot/side is folded into the range; ranges are "dynamic and can vary… based on market conditions and your trading activity". | **Verified** – …/18054855363484-Slippage-rule (accessed 2026-11-04) |
| Pending orders cancelled "[SL Violated]" / "[canceled/gap]" | When a gap would execute the entry beyond the attached TP (stop orders) or SL (limit orders), Exness cancels the pending order instead of opening a losing/illogical trade. Pending orders are also deleted if funds are insufficient at activation, and are cancelled (not executed) during close-only periods. | **Verified** – …/7766359507484-Why-was-my-pending-order-not-executed ; …/4402645127186-What-can-close-my-orders-automatically |
| SL/TP triggering | "the stop is triggered only after the equity has moved beyond the level you set, and the positions are closed at the next available market price. After the SL/TP trigger, the order enters an execution queue." | **Verified** – …/360017263680-Setting-stop-loss-SL-and-take-profit-TP |
| Hedging | Opposite positions on the same symbol are allowed ("hedged orders"); fully hedged = 0% margin; partially hedged = margin on the unmatched part; closing one leg during HMR can be refused if free margin cannot cover the newly unhedged leg. Stop-out can hit hedged books when equity < 0. MT5 "Close By" and bulk-close operations are supported ("server will execute orders one by one"). | **Verified** – …/360014696320-Hedged-orders ; …/11541096471068-Closing-orders |
| MT5 account margin mode | Since opposite positions coexist, the MT5 account must be `ACCOUNT_MARGIN_MODE_RETAIL_HEDGING` ("multiple positions can exist per symbol"). | **Assumption** → verify `account_info().margin_mode` (enum **Verified** on accountinformation page). |
| Partial close | Supported in all Exness front-ends; in MT5/Python a partial close is a `TRADE_ACTION_DEAL` of opposite type with `position=<ticket>` and smaller `volume` (retcode 10038 if volume exceeds position). | Exness UI support **Verified** (Closing orders); MT5 mechanics **Assumption** from the TradeRequest field semantics. |
| Margin accounting | Margin is position-level (per instrument leverage/HMR) and summed to account margin; margin level = Equity/Margin×100; stop out at 0% ("closing orders automatically, starting with the least profitable, until the margin level is returned above 0%"). | **Verified** – …/4402645127186 ; …/17685808714140-0-stop-out-Stop-Out-Protection |
| Negative balance protection | Balance reset to 0 via automatic "null operation" (shown as D-null in history) only after **all** orders are stopped out; negative equity on an open hedged book is not covered and will be deducted from a deposit. | **Verified** – …/18055126006044-Negative-Balance-Protection |
| Leverage by equity | 0–4,999.99 USD: 1:Unlimited (if eligible) / 1:2000 default; 5,000–29,999.9: 1:2000; 30,000–99,999.99: 1:1000; ≥100,000: 1:500; min setting 1:2; KE entity max 1:400. | **Verified** – …/360014529380-Leverage |
| Unlimited leverage eligibility | Real accounts: equity < 5,000 USD **and** ≥10 closed orders totalling ≥5 lots (or 500 cent lots) across all real accounts; eligible instruments include "All commodities, except XAUUSD247"; Stop Out Protection "may not be available" on unlimited-leverage accounts; not for MENA entity. | **Verified** – …/18055266964892-Unlimited-leverage |
| HMR (news) | "orders opened up to 15 minutes before and 90 seconds after a high-impact news release are subject to higher margin requirements"; metals XAU leverage during HMR "1:100 - 1:1000 (0.1% - 1%)" (HMR article) vs "1:200 – 1:1000 (0.1%-0.5%)" (Commodities article) — the two pages disagree on the lower bound. "If the account leverage is lower than the HMR leverage, margins will be calculated using the account leverage." "you may have no HMR or brief periods with higher margin requirements" depending on your activity. | **Verified** – …/20618327917596-Higher-Margin-Requirements-HMR (updated 2026-11-02) ; Commodities article |
| HMR (breaks) | Weekend: most instruments ~3 h before close to 1 h after reopen (gold falls under "all other trading instruments"); gold daily break: 30 min before, ~10 min after. | **Verified** – HMR article ; …/8633865683612-Why-is-the-margin-required-so-high-for-my-order |
| Margin formula | Dynamic: `Margin = Lots × Contract Size / Leverage`; fixed-margin instruments (incl. XAUUSD247): `Margin = Lots × Contract Size × Required Margin`. | **Verified** – …/360014504992-Margin |

---

## 5. Costs

**Formulas** (**Verified** – About swap article): `Pip value = lots × contract size × pip size`; `Swap = swap rate (pips) × number of days × pip value`; Exness quotes swap in pips, "while MetaTrader platforms calculate them in points". For XAUUSD: pip value per 1.0 lot = 1 × 100 × 0.01 = **1 USD/pip**; per 0.1 lot = 0.10 USD/pip.

**Swap conversion for MT5** (**Assumption**, to be verified against `history_deals_get()[…].swap` after one rollover): if `symbol_info().swap_mode == SYMBOL_SWAP_MODE_POINTS`, daily swap in USD ≈ `volume × trade_contract_size × point × swap_long` (or `swap_short`); triple on the weekday given by `swap_rollover3days`. If `swap_mode` is a currency mode, `swap_long/short` are already money per lot. Actual XAUUSD swap values: **Unknown** (not published in the help centre) → read `swap_long`, `swap_short` live; also check whether the account is swap-free (PA → Trading conditions), in which case XAUUSD swap is 0.

**Worked round-trip cost, 0.1 lot XAUUSD, intraday (no swap)**

| Input | Standard (-m) | Pro | Raw Spread | Zero |
|---|---|---|---|---|
| Spread assumed at entry+exit (**Assumption**; verify with `symbol_info().spread`) | 20 pips (0.20 USD/oz) | 12 pips | 6 pips | 0 pips (95%-of-day claim) |
| Spread cost = spread × 0.10 USD/pip | 2.00 USD | 1.20 USD | 0.60 USD | 0.00 USD |
| Commission (**Verified** rates: Raw 3.5, Zero 5.5 USD/lot/side) = rate × 0.1 lot × 2 sides | 0 | 0 | 0.70 USD | 1.10 USD |
| Expected slippage (**Assumption**: 1 pip per market fill, 2 fills) | 0.20 USD | 0.20 USD (instant: requote risk instead) | 0.20 USD | 0.20 USD |
| **Total** | **2.20 USD** | **1.40 USD** | **1.50 USD** | **1.30 USD** |
| Per 1.0 lot equivalent | 22 USD | 14 USD | 15 USD | 13 USD |

Overnight add-on (**Assumption** inputs): if `swap_long` were −30 pips/day → −3.00 USD/day per 0.1 lot, ×3 on Wednesday. Replace with live values. On Zero, spreads "may rise above 0 during high volatility" and are floating "during key periods such as economic news and rollovers" (**Verified**), so the Zero figure is best-case.

Slippage expectation: Exness states slippage "is possible when executing market and pending orders on all account types" and both directions occur (**Verified** – …/4418146279954-How-slippage-happens). Quantitative distribution: **Unknown** → log `result.price − request.price` for every fill.

---

## 6. Execution limits and bot policy

| Topic | Finding | Label |
|---|---|---|
| EAs/algos allowed | Yes: dedicated EA guide; Zero account "Suitable for the use of Expert Advisors"; Pro "flexible for any trading style, from day trading to automated trading strategies"; VPS offered specifically for EAs. | **Verified** – EA article; Zero/Pro articles; VPS articles |
| Order-frequency limits | No numeric orders/sec limit is published. Exness warns an EA can be disabled server-side when settings are "too aggressive… excessive submissions and irrelevant requests"; MT5 itself returns 10024 `TRADE_RETCODE_TOO_MANY_REQUESTS`. | **Verified** (qualitative). Numeric threshold **Unknown** → throttle (e.g. ≤1 request/s sustained) and back off on 10024. |
| Scalping / news trading | Not prohibited in any help-centre article found; Exness publishes its own scalping guides (insights.exness.com). HMR and slippage-free ranges are the practical constraints. | **Verified** (absence in help centre) ; secondary |
| Hedging | Allowed (see §4). | **Verified** |
| Abusive-strategy clauses | Search-engine snippet of the Exness (SC) Ltd Client Agreement PDF (my.exness.com/cdn/media/nymstarLimited/nymstar_limited_client_agreement.pdf) reads: "The Client agrees that trading operations using additional functions of the Client Trading Terminal such as Trailing Stop and/or Expert Advisor and/or any other automated processes are executed completely under the Client's responsibility" and that strategies exploiting "price errors, off-market quotes, or internet delays" (price latency/arbitrage) are "not permissible". Exness VPS terms reference "client agreement (section 2.9)" for unacceptable use. The PDF returned 403/404 to every fetch attempt. | **Unknown** (snippet-level only) → download the Client Agreement for **your** entity from the PA (Profile → Legal documents) and read §2.9 and the EA clause before go-live. |
| Stop Out Protection / swap-free revocation | Both are algorithmic benefits that "may be disabled if your trading strategy heavily relies on it or is misused" / lost if you "regularly hold large amounts of open orders overnight". | **Verified** – stop-out protection and swap-free articles |
| Max positions | See §3: MT5 Real open positions "Unlimited", 1,000 pending; Standard Cent 100/200 combined by server. | **Verified** |

---

## 7. Demo environment

| Topic | Finding | Label |
|---|---|---|
| Availability | A demo Standard MT5 is auto-created at registration; demo exists for every type except Standard Cent. | **Verified** – …/360013191919-Real-and-demo-accounts ; …/360010683800 |
| Conditions vs live | "Trading conditions for demo trading accounts are identical to real trading accounts" / "market conditions… are similar". | **Verified** – …/360013671440-Do-demo-trading-accounts-use-real-money ; Real-and-demo article. Whether commission and swap are actually booked on demo: **Unknown** → check `history_deals_get()` `commission`/`swap` on demo after a trade and a rollover. |
| Balance reset / top-up | PA → My Accounts → Demo → **Set Balance** (any value, higher or lower, in the demo currency). | **Verified** – …/360007200571-How-to-top-up-a-demo-account |
| Expiry | Demo accounts "are subject to deletion if there is no activity… cannot be reactivated after deletion"; inactivity = no trading or balance operations. Exact period not stated in the current article (older search snippets said MT5 demo deleted after 21 days). | **Verified** (deletion policy) ; period **Unknown** → keep a heartbeat trade or balance op at least every 2 weeks. |
| Unlimited leverage on demo | Option always selectable; only applies while equity ≤ 4,999.99 USD. | **Verified** – Unlimited leverage article |
| Server names | Accounts are "randomly assigned to a server. It is not possible to change the assigned server"; find server + login in PA account card. Third-party guides cite `Exness-MT5Real<N>` / `Exness-MT5Trial<N>` naming. | **Verified** (random assignment, PA lookup) – …/12232298502684-Available-trading-servers ; …/360012578619 ; naming pattern **Unknown** (secondary only) |
| Demo order cap | 1,024 open (1,024 pending) on MT5 Demo vs Unlimited/1,000 on MT5 Real — slightly different limits. | **Verified** – account articles |
| Daily maintenance | "Cent and Trial servers may also have brief daily maintenance… at 23:20 UTC+0 on weekdays and… Saturdays" — demo ("Trial") servers have a daily blip that live servers do not. | **Verified** – Instrument trading hours article |

Limitations of demo for slippage testing (**Assumption**, consistent with the Exness statements that slippage-free ranges and HMR "vary… based on… your trading activity"): demo fills do not exercise the market-maker's per-client risk models (slippage-free range, HMR duration, Stop Out Protection availability), so demo slippage is at best a lower bound. Validate on a small live account.

---

## 8. Hosting

| Option | Finding | Label |
|---|---|---|
| Exness free VPS | Criteria "on overall account balance and/or total trading volume… found in your Personal Area"; worked example mentions "an overall account balance between 500 and 1,999 USD" plus a 30-day round-turn volume (example target 1.5 M USD traded volume ≈ 7.5 USDJPY round-turn lots); keep-alive rules: trade from the VPS within 5 days of grant and at least every 30 days; lose it if volume criteria unmet over 30/90/180-day windows for 30 consecutive days — then all data is wiped; pre-installed MT4+MT5 only; RDP access; Exness disclaims liability for EA/connectivity failures. VPS locations: "near Exness trading servers located in **Amsterdam, Dubai, Seoul, Singapore, Miami, and Johannesburg**". | **Verified** – …/360004040011-About-Exness-VPS ; …/17667896877212-Exness-VPS-Terms-of-Use (accessed 2026-11-04). Exact current thresholds **Unknown** (PA-specific). |
| Which data centre serves *your* account | Not published; server assignment is random. | **Unknown** → measure `terminal_info().ping_last` from candidate VPS regions (Amsterdam first). |
| Third-party Windows VPS (ForexVPS, Beeks, Contabo, AWS Windows) | Not researched from primary sources in this pass. | **Unknown** → choose by measured ping to the assigned server; Exness permits own VPS ("You may use your own VPS to trade on our servers"). |
| Exness VPS specs (RAM/CPU) | Not stated in the articles read. | **Unknown** |

Recommended topologies (**Assumption**):

1. **Windows VPS (Amsterdam) running MT5 + the Python `MetaTrader5` adapter as a thin "execution gateway"; Linux GPU box runs the LLM/analytics and talks to the gateway over a WireGuard/VPN or mTLS gRPC/WebSocket link.** Best latency for order placement; keeps the heavy, GPU-bound stack off the VPS; the gateway can enforce risk checks locally even if the link drops. The Exness free VPS is MT-only with RDP access and no stated admin/driver guarantees, so a paid Windows VPS is safer for a custom Python service (**Assumption**).
2. **Single Windows GPU workstation**: simplest, but Exness notes EAs/terminals are the user's responsibility and home connectivity is the usual failure point; acceptable for low-frequency (minutes-to-hours) decision cadence.
3. **Everything on Linux via Wine** (`mt5linux`/Docker): lowest cost, but relies on unofficial tooling (§1c) — use for research/backtest ingestion, not live execution, until proven stable for weeks.

In all cases the decision loop should be asynchronous: the gateway polls ticks/positions, the LLM returns intents, and the gateway validates against `symbol_info()`/`account_info()` before `order_check()` → `order_send()`.

---

## 9. Data

| Topic | Finding | Label |
|---|---|---|
| History depth via `copy_rates_*` / `copy_ticks_*` | Limited by the terminal's "Max. bars in chart" and by what the broker server provides; no depth figure is published by Exness or MetaQuotes. | **Verified** (mechanism) ; depth **Unknown** → test: set Max bars in chart to "Unlimited", call `copy_rates_from("XAUUSD", TIMEFRAME_M1, datetime(2015,1,1,tzinfo=UTC), 10_000_000)` and `copy_ticks_range` month by month backwards until `None`/`RES_E_NOT_FOUND`; record first available timestamp per timeframe. |
| Official tick history download | PA → "?" menu → Tools & Services → Tick History; CSV columns `"Exness","Symbol","Timestamp","Bid","Ask"`; "tick-by-tick data for all trading instruments on all existing accounts until the present time"; daily files for the current month, monthly for prior months, yearly for earlier years; ticks are drawn from **MT4** Real 1 (Standard Cent/Standard/Pro), Real 3 (Zero), Real 9 (Raw Spread) — ticks "may differ slightly" across servers. | **Verified** – https://get.exness.help/hc/en-us/articles/360021547851-Tick-history (accessed 2026-11-04) |
| `ticks.ex2archive.com` | Returns an empty HTML shell to non-JS clients; third-party packages (`exfinance`, `exness-data-preprocess`) describe it as monthly ZIP/CSV archives. Relationship to Exness not confirmed. | **Unknown** → prefer the PA download; treat the archive as unofficial unless Exness confirms. |
| Timezone of data | Bar/tick timestamps from the terminal are UTC; Exness servers are UTC+0, so server time == UTC year-round. | **Verified** – mql5.com copy_rates page ; …/360014390760 ("MetaTrader trading platforms are set to Greenwich Mean Time (GMT+0)… cannot be changed") |

---

## 10. Failure modes

| Failure | Detection / handling | Label |
|---|---|---|
| Terminal not connected | `terminal_info().connected == False`; order_send → 10031 `TRADE_RETCODE_CONNECTION`. | **Verified** fields/codes |
| AutoTrading disabled (client) | `terminal_info().trade_allowed == False` and/or `last_error() == RES_E_AUTO_TRADING_DISABLED (-8)`; order_send → 10027. Server-side: 10026. Account-level: `account_info().trade_allowed`, `account_info().trade_expert` (`ACCOUNT_TRADE_EXPERT` "whether trading is allowed for an Expert Advisor"). Exness may disable an EA server-side for "too aggressive" behaviour; reactivation via support ticket. | **Verified** – lasterror page; retcodes; accountinformation; Exness EA article |
| Logged in with read-only (investor) password | "Trade is disabled" error / 10017; `account_info().trade_allowed` false. | **Verified** – …/360013352479--Trade-is-disabled-error |
| Symbol close-only / disabled | `symbol_info().trade_mode` ≠ `SYMBOL_TRADE_MODE_FULL`; pending orders are cancelled, not executed, during close-only (Exness). Gold has a close-only window 23:00:30–23:02 UTC (winter). | **Verified** |
| Server maintenance | Weekend: Sat 05:00–09:00 UTC winter (04:00–08:00 summer); Sun 05:00–09:00 UTC; Cent/Trial daily 23:20 UTC. "Exness will not be held responsible for any potential losses that may occur during scheduled maintenance" (Client Agreement p.1.5). | **Verified** – Instrument trading hours article |
| DST | Server clock is fixed at UTC+0 (no shift). What shifts is the **schedule**: gold open/close, swap time (21:00↔22:00 UTC) and HMR windows move by one hour on the US DST dates (second Sunday in March / first Sunday in November). Hard-code nothing; derive from `symbol_info()` session fields or the Exness schedule page. | **Verified** – timezone article; DST update article; About swap |
| Stale data | Compare `symbol_info_tick().time_msc` (ms) to wall clock (UTC); if no tick for >N s while `trade_mode == FULL` and inside session, treat as stale; also watch `terminal_info().retransmission` and `ping_last`. | Fields **Verified**; thresholds **Assumption** |
| Rate limiting | 10024 `TRADE_RETCODE_TOO_MANY_REQUESTS`; Exness's own EA-disable rule. | **Verified** |
| Position limit / volume limit | 10033, 10034, 10040; `symbol_info().volume_limit`; Exness max 200 lots XAUUSD (day) — night limits for XAUUSD not listed (other golds drop to 20 lots at night). | **Verified** |
| Negative equity on hedged book | NBP does not reset until all positions are stopped out; deposits are consumed. | **Verified** – NBP article |
| HMR margin spike | Closing one hedge leg may be refused during HMR; margin for new orders jumps to 1:200–1:1000 for gold 15 min before high-impact news. Pre-compute margin with `order_check()` immediately before `order_send()`. | **Verified** (rules) ; mitigation **Assumption** |

---

## (a) Verification tests to run on the real account

1. **Connectivity & identity** – `mt5.initialize(path, login, password, server)`; expect `True`; then `mt5.version()`, `mt5.terminal_info()` (check `connected=True`, `trade_allowed=True`, `tradeapi_disabled=False`, note `build`, `maxbars`, `ping_last`) and `mt5.account_info()` (record `company`, `server`, `currency`, `leverage`, `limit_orders`, `margin_so_mode`, `margin_so_call` (expect 60 or 30), `margin_so_so` (expect 0 or 20), `margin_mode` (expect 2 = `ACCOUNT_MARGIN_MODE_RETAIL_HEDGING` — confirm), `trade_mode` (demo=0/real=2 per enum order), `trade_expert`).
2. **Symbol discovery** – `mt5.symbols_get(group="*XAU*")`; confirm the exact gold symbol for the account suffix (`XAUUSD`, `XAUUSDm`, `XAUUSDc`, `XAUUSDr`, `XAUUSDz`) and that `XAUUSD247` is a separate symbol; `mt5.symbol_select(sym, True)`.
3. **Contract spec dump** – `mt5.symbol_info(sym)._asdict()`; assert `trade_contract_size == 100`, `volume_min == 0.01`, `volume_max` (expect 200), record `volume_step`, `digits`, `point`, `trade_tick_size`, `trade_tick_value`, `trade_stops_level`, `trade_freeze_level`, `trade_exemode` (expect `SYMBOL_TRADE_EXECUTION_MARKET` = 2 on non-Pro), `filling_mode` bitmask, `swap_mode`, `swap_long`, `swap_short`, `swap_rollover3days` (expect Wednesday), `margin_initial`, `margin_hedged` (expect 0), `volume_limit`, `session_deals`, `start_time/expiration_time`, `currency_margin/profit`.
4. **Filling-mode probe** – on demo, send a 0.01-lot `TRADE_ACTION_DEAL` with each of `ORDER_FILLING_FOK`, `ORDER_FILLING_IOC`, `ORDER_FILLING_RETURN`; expect 10009 for modes present in `filling_mode` and 10030 `TRADE_RETCODE_INVALID_FILL` otherwise. Fix one mode in config.
5. **Margin pre-check** – `mt5.order_calc_margin(ORDER_TYPE_BUY, sym, 0.1, ask)` and `mt5.order_check(request)`; compare `margin` against `0.1 × 100 × ask / leverage`; repeat 10 minutes before a scheduled high-impact release to observe the HMR jump (expect margin consistent with 1:200–1:1000).
6. **Stops rule probe** – place a buy with `sl = ask − trade_stops_level×point − 1×point` and `tp` similarly; expect 10009; then try `sl` inside the spread → expect 10016 `TRADE_RETCODE_INVALID_STOPS`; confirms live stops level (Exness claims ~0).
7. **Partial close** – open 0.02, close 0.01 with `TRADE_ACTION_DEAL`, opposite `type`, `position=<ticket>`; expect 10009 and `positions_get(ticket=…)` volume 0.01; then over-close 0.05 → expect 10038.
8. **Hedge + close-by** – open 0.01 buy and 0.01 sell; confirm both appear in `positions_get(symbol=sym)` (proves hedging mode); then `TRADE_ACTION_CLOSE_BY` with `position`/`position_by`; expect 10009 (or 10046 if hedging is prohibited — would contradict the Exness hedging article).
9. **Pending-order limit & SL-violated** – place a buy stop 1 pip above ask with a TP 1 pip above that; expect either acceptance or `[SL violated]`-style rejection in `result.comment` under gap conditions; also verify `account_info().limit_orders` (expect 1000 on MT5 Real).
10. **Slippage logging** – for 20 demo and 20 small live market orders, record `result.price − request.price` and `result.bid/ask` vs `symbol_info_tick()` just before; derive a slippage distribution and compare demo vs live.
11. **Swap booking** – hold 0.01 long and 0.01 short across the 22:00 UTC (winter) rollover on a Tuesday and a Wednesday; read `history_deals_get(position=…)`/`positions_get()` `.swap`; back out the effective USD/lot/day and check Wednesday = 3×; verify whether the account is swap-free (0).
12. **Commission booking** – on Raw/Zero, after a 0.1-lot round trip, sum `commission` over the two deals from `history_deals_get(position=ticket)`; expect −0.70 USD (Raw) or −1.10 USD (Zero) per `deal.entry` leg pair.
13. **Session/hours** – poll `symbol_info(sym).trade_mode` every 10 s across 21:55–23:05 UTC; expect `FULL → DISABLED/CLOSEONLY → FULL` matching the published winter break (21:58–23:02); note the exact close-only window.
14. **History depth** – `copy_rates_from(sym, TIMEFRAME_M1, datetime(2010,1,1,tzinfo=timezone.utc), 5_000_000)` and `copy_ticks_range(sym, start_of_month, end_of_month, COPY_TICKS_ALL)` stepping back monthly until `None`/`last_error()==(-4, …)`; record the earliest timestamp per timeframe; compare M1 counts against a PA tick-history download for one day.
15. **Failure drills** – (i) toggle AutoTrading off in the terminal → expect `terminal_info().trade_allowed=False`, `order_send` 10027, `last_error()` −8; (ii) disconnect network → expect `connected=False`, 10031; (iii) log in with investor password → expect `account_info().trade_allowed=False`, 10017; (iv) send 20 `order_check` calls in 1 s → observe whether 10024 appears.
16. **Latency** – time 100 `order_send` round-trips (0.01 lot open/close) at quiet hours from the chosen VPS; record p50/p95; compare with `terminal_info().ping_last`.

## (b) Key takeaways for architecture

- The only MetaQuotes-supported programmatic path on MT5 is Python (`MetaTrader5`, Windows-only wheel, requires a live terminal, IPC-bound) or an MQL5 EA; neither offers push events to Python — design a polling gateway with `symbol_info_tick()`/`copy_ticks_from()` and `positions_get()`.
- Exness has a native REST/WebSocket "Exness API" but it works **only** with Exness-terminal accounts, not MT5 accounts, excludes stocks/indices, throttles prices to 100–500 ms, and may be region-gated; it is a viable alternative only if you forgo MT5 (and the MT-only free VPS).
- Exness is the counterparty/market maker; execution is market execution (no requotes) on Standard/Cent/Raw/Zero; Pro is instant execution with requotes — avoid Pro for automation unless the account currency is one that enables market execution (USD, JPY, THB, CNY, IDR, VND).
- Gold contract: 100 oz, pip 0.01, 0.01–200 lots, triple swap Wednesday, hedged margin 0%; everything else (stops/freeze levels, point/digits, swap values, fill modes, volume step/limit) must be read from `symbol_info()` at startup and re-read daily; fail closed if the spec changes.
- Commission is explicit and verified: Raw Spread 3.5 USD/lot/side, Zero 5.5 USD/lot/side on XAUUSD; Standard/Pro are spread-only. For a 0.1-lot round trip the all-in cost is on the order of 1.3–2.2 USD depending on spread assumptions — spread sampling on the live symbol decides the account choice.
- Leverage is dynamic (equity tiers 1:Unlimited→1:500) and HMR caps gold leverage (down to 1:100–1:200) 15 min before / 90 s after high-impact news, 30 min before / ~10 min after the daily break and ~3 h before / 1 h after the weekend — always `order_check()` immediately before `order_send()`, and keep free margin headroom so hedged legs can be closed during HMR.
- Stop out is 0% (20% for KE/JO) with margin call at 60% (Standard/Cent) or 30% (Pro/Raw/Zero); Stop Out Protection and swap-free are revocable algorithmic benefits — do not depend on them in risk math.
- Pending orders/SL/TP are subject to Exness's slippage-free rule (XAUUSD: up to 3× current spread, dynamic) and to `[SL violated]` auto-cancellation on gaps — model stops as "best effort at or beyond the level", not guaranteed.
- Hedging mode is in effect (opposite positions allowed, close-by supported); treat `account_info().margin_mode` as a startup assertion and account for the fact that NBP never resets while a hedged book is open.
- Server time is UTC+0 and does not move; the **schedule** moves with US DST (gold winter break 21:58–23:02 UTC; summer 20:58–22:02; swap at 22:00/21:00). Derive session state from `symbol_info().trade_mode`, never from a hard-coded clock.
- Weekend maintenance (Sat/Sun 05:00–09:00 UTC) and the Cent/Trial daily 23:20 UTC blip mean demo servers have extra disconnects; design reconnect logic and treat demo as a functional test, not a slippage test.
- Rate limits: no published number, but Exness disables EAs that flood the server and MT5 returns 10024 — throttle requests, coalesce SL/TP modifications, and back off exponentially.
- Order caps are generous on MT5 Real (unlimited positions, 1,000 pending) except Standard Cent (100/200 combined, server-dependent) — do not prototype on Cent if the strategy uses many pending orders.
- Prefer a Windows VPS in Amsterdam (an Exness VPS location) for the MT5 gateway and keep the LLM/GPU stack on Linux behind a VPN; Linux/Wine is for research only until proven; the Exness free VPS has balance/volume keep-alive rules and wipes data on termination.
- Keep the authoritative legal text for **your** entity (Client Agreement §2.9 and the EA clause) on file — it could not be fetched here, and latency/arbitrage-style strategies are the one bot behaviour Exness explicitly polices.
- Historical data: use the PA Tick History download (CSV bid/ask, MT4-server sourced) for backtests and verify terminal history depth empirically; both are UTC.

## (c) Sources (all accessed 2026-11-04)

MetaQuotes / MQL5
- https://www.mql5.com/en/docs/python_metatrader5 — package overview, function list
- https://www.mql5.com/en/docs/python_metatrader5/mt5initialize_py — initialize()
- https://www.mql5.com/en/docs/python_metatrader5/mt5lasterror_py — RES_* codes
- https://www.mql5.com/en/docs/python_metatrader5/mt5ordersend_py — TradeRequest / OrderSendResult
- https://www.mql5.com/en/docs/python_metatrader5/mt5ordercheck_py — TRADE_ACTION_*, filling, time enums, OrderCheckResult
- https://www.mql5.com/en/docs/python_metatrader5/mt5symbolinfo_py — symbol_info() fields
- https://www.mql5.com/en/docs/python_metatrader5/mt5symbolinfotick_py — tick fields
- https://www.mql5.com/en/docs/python_metatrader5/mt5accountinfo_py — account_info() fields
- https://www.mql5.com/en/docs/python_metatrader5/mt5terminalinfo_py — terminal_info() fields
- https://www.mql5.com/en/docs/python_metatrader5/mt5copyratesfrom_py — rates, Max bars, UTC
- https://www.mql5.com/en/docs/python_metatrader5/mt5copyticksrange_py — ticks, COPY_TICKS_*
- https://www.mql5.com/en/docs/python_metatrader5/mt5positionsget_py — TradePosition fields
- https://www.mql5.com/en/docs/python_metatrader5/mt5historydealsget_py — TradeDeal fields
- https://www.mql5.com/en/docs/constants/errorswarnings/enum_trade_return_codes — retcodes 10004–10046
- https://www.mql5.com/en/docs/constants/tradingconstants/orderproperties — ORDER_TYPE_*, ORDER_FILLING_*, ORDER_TIME_*
- https://www.mql5.com/en/docs/constants/environment_state/marketinfoconstants — SYMBOL_* enums/properties
- https://www.mql5.com/en/docs/constants/environment_state/accountinformation — ACCOUNT_MARGIN_MODE_*, ACCOUNT_* properties
- https://www.mql5.com/en/docs/network/webrequest — WebRequest restrictions
- https://www.mql5.com/en/docs/network — Socket*/WebRequest list, allowed-address rule
- https://www.mql5.com/en/docs/runtime/running — EA threading, NewTick dropping
- https://www.metatrader5.com/en/terminal/help/start_advanced/install_linux — official Linux/Wine install
- https://pypi.org/project/MetaTrader5/ — version 5.0.6231, Windows-only wheels
- https://pypi.org/project/mt5linux/ — v1.1.1, Wine+RPyC bridge

MetaApi
- https://metaapi.cloud/docs/client/ — REST vs WebSocket, MT4/MT5
- https://metaapi.cloud/docs/client/faq/ — billing (6-hour minimum), latency figures, demo support
- https://metaapi.cloud/docs/client/rateLimiting/ — CPU-credit rate limits
- https://metaapi.cloud/ — pricing (snippet only; body not readable)

Exness help centre (read via the Zendesk JSON API; HTML URLs listed)
- https://get.exness.help/hc/en-us/articles/17854173039388-Commodities — XAUUSD/XAUUSD247 specs (updated 2026-11-03)
- https://get.exness.help/hc/en-us/articles/27866287512476-Exness-API — Exness API
- https://get.exness.help/hc/en-us/articles/17537782738460-Standard-account
- https://get.exness.help/hc/en-us/articles/17537782786588-Standard-Cent-account
- https://get.exness.help/hc/en-us/articles/17537794560540-Pro-account
- https://get.exness.help/hc/en-us/articles/17537767270556-Raw-Spread-account
- https://get.exness.help/hc/en-us/articles/17537782878236-Zero-account
- https://get.exness.help/hc/en-us/articles/360011554840-What-is-the-minimum-amount-needed-to-start-trading
- https://get.exness.help/hc/en-us/articles/360014560220-Account-type-suffixes
- https://get.exness.help/hc/en-us/articles/20618327917596-Higher-Margin-Requirements-HMR (updated 2026-11-02)
- https://get.exness.help/hc/en-us/articles/8633865683612-Why-is-the-margin-required-so-high-for-my-order
- https://get.exness.help/hc/en-us/articles/360014529380-Leverage
- https://get.exness.help/hc/en-us/articles/18055266964892-Unlimited-leverage
- https://get.exness.help/hc/en-us/articles/360014504992-Margin
- https://get.exness.help/hc/en-us/articles/30743728061724-Leverage-increases-for-selected-instruments
- https://get.exness.help/hc/en-us/articles/28761383982108-Which-trading-instruments-have-fixed-leverage
- https://get.exness.help/hc/en-us/articles/360014693340-Margin-call-and-stop-out-levels-by-account-type
- https://get.exness.help/hc/en-us/articles/17685808714140-0-stop-out-Stop-Out-Protection
- https://get.exness.help/hc/en-us/articles/4402645127186-What-can-close-my-orders-automatically
- https://get.exness.help/hc/en-us/articles/18055126006044-Negative-Balance-Protection
- https://get.exness.help/hc/en-us/articles/360014696320-Hedged-orders (updated 2026-11-04)
- https://get.exness.help/hc/en-us/articles/11541096471068-Closing-orders
- https://get.exness.help/hc/en-us/articles/26544238207260-Execution
- https://get.exness.help/hc/en-us/articles/360017885880-Available-order-and-execution-types
- https://get.exness.help/hc/en-us/articles/360011802860-Requotes
- https://get.exness.help/hc/en-us/articles/4402644779538-Setting-deviation
- https://get.exness.help/hc/en-us/articles/18054855363484-Slippage-rule
- https://get.exness.help/hc/en-us/articles/4418146279954-How-slippage-happens
- https://get.exness.help/hc/en-us/articles/360011676519-About-stop-levels
- https://get.exness.help/hc/en-us/articles/360017263680-Setting-stop-loss-SL-and-take-profit-TP
- https://get.exness.help/hc/en-us/articles/7766359507484-Why-was-my-pending-order-not-executed
- https://get.exness.help/hc/en-us/articles/360011906619--Invalid-SL-TP-error
- https://get.exness.help/hc/en-us/articles/360012824139--Too-many-orders-error
- https://get.exness.help/hc/en-us/articles/360014690880-About-spread
- https://get.exness.help/hc/en-us/articles/6214438375442-Why-is-spread-different-between-the-website-and-MetaTrader
- https://get.exness.help/hc/en-us/articles/360014709151-About-swap
- https://get.exness.help/hc/en-us/articles/360011745839-Does-Exness-charge-fees-on-orders-held-overnight
- https://get.exness.help/hc/en-us/articles/4402341895570-Swap-free-status
- https://get.exness.help/hc/en-us/articles/18055079599132-Swap-free-status-Islamic-countries
- https://get.exness.help/hc/en-us/articles/4405235684498-Instrument-trading-hours — UTC+0, rollover, maintenance, DST
- https://get.exness.help/hc/en-us/articles/31342732737180-Trading-Hours-Update-End-of-US-Daylight-Saving-Time-01-Nov-2026
- https://get.exness.help/hc/en-us/articles/360014390760-What-is-the-default-timezone-set-for-MetaTrader
- https://get.exness.help/hc/en-us/articles/360013191919-Real-and-demo-accounts
- https://get.exness.help/hc/en-us/articles/360013671440-Do-demo-trading-accounts-use-real-money
- https://get.exness.help/hc/en-us/articles/360010683800-Are-demo-accounts-available-for-all-account-types
- https://get.exness.help/hc/en-us/articles/360007200571-How-to-top-up-a-demo-account
- https://get.exness.help/hc/en-us/articles/12232298502684-Available-trading-servers
- https://get.exness.help/hc/en-us/articles/360012578619-Trading-account-login-and-server-details
- https://get.exness.help/hc/en-us/articles/360019530859-Using-Expert-Advisors-EA
- https://get.exness.help/hc/en-us/articles/360013352479--Trade-is-disabled-error
- https://get.exness.help/hc/en-us/articles/360004040011-About-Exness-VPS
- https://get.exness.help/hc/en-us/articles/17667896877212-Exness-VPS-Terms-of-Use
- https://get.exness.help/hc/en-us/articles/9662707664156-Trading-volume-calculation
- https://get.exness.help/hc/en-us/articles/360021547851-Tick-history
- https://get.exness.help/hc/en-us/articles/360014539320-Is-Exness-regulated
- https://get.exness.help/hc/en-us/articles/360016475859-About-Exness
- https://get.exness.help/hc/en-us/articles/27004655321116-Client-categorization
- https://get.exness.help/hc/en-us/articles/17537777171996-PA-Exness-benefits
- https://get.exness.help/hc/en-us/articles/360006922872-Trading-account-settings
- https://get.exness.help/hc/en-us/articles/360010646760-Available-trading-instruments

Not retrievable today (marked Unknown where used): https://www.exness.com/accounts/ , /regulation/ , /vps/ , /swap-free/ , /unlimited-leverage/ , /tick-history/ , /legal-documents/ (all HTTP 403); https://my.exness.com/cdn/media/nymstarLimited/nymstar_limited_client_agreement.pdf (403); https://fsaseychelles.sc/images/complaints-files/cf_655dfe3187077_exness_bv_client_agreement_1.pdf (404); https://help.exnessaffiliates.com/hc/en-us/articles/360022982091-The-Exness-Partnership-API (proxy 502); https://get.exness.help/hc/en-us/articles/9620993112604-MetaTrader-server-optimization-schedule and …/360013051339-Winter-trading-hours (401 via API; superseded by the Instrument trading hours and DST-update articles); https://ticks.ex2archive.com/ (empty JS shell).

-- goldtrader schema v1 (D-024). SQLite, WAL. All times are epoch milliseconds UTC unless suffixed _s.
-- Tables listed in docs/ARCHITECTURE.md §2.3. Phase 3 populates: symbol_specs, account_snapshots,
-- bars, trial_ledger, backtest_runs, config_versions, heartbeats, data_snapshots.

CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL, applied_ms INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS config_versions (
  id INTEGER PRIMARY KEY,
  config_hash TEXT NOT NULL,
  owner_hash TEXT NOT NULL,
  source TEXT NOT NULL,            -- file path or 'telegram' / 'pipeline'
  body_json TEXT NOT NULL,
  created_ms INTEGER NOT NULL,
  UNIQUE(config_hash)
);

CREATE TABLE IF NOT EXISTS symbol_specs (           -- R-001, KU-001
  id INTEGER PRIMARY KEY,
  symbol TEXT NOT NULL,
  read_ms INTEGER NOT NULL,
  spec_json TEXT NOT NULL,
  spec_hash TEXT NOT NULL,
  critical_change INTEGER NOT NULL DEFAULT 0,
  changed_fields TEXT
);
CREATE INDEX IF NOT EXISTS ix_symbol_specs_symbol ON symbol_specs(symbol, read_ms);

CREATE TABLE IF NOT EXISTS account_snapshots (
  id INTEGER PRIMARY KEY,
  read_ms INTEGER NOT NULL,
  login INTEGER NOT NULL,
  server TEXT, company TEXT, currency TEXT,
  trade_mode INTEGER, leverage INTEGER, margin_mode INTEGER,
  balance REAL, equity REAL, margin REAL, margin_free REAL, margin_level REAL, profit REAL,
  trade_allowed INTEGER, trade_expert INTEGER
);

CREATE TABLE IF NOT EXISTS bars (                    -- bid/ask OHLC, one row per bar; seconds = 1 or 60
  symbol TEXT NOT NULL,
  seconds INTEGER NOT NULL,
  time_s INTEGER NOT NULL,
  bid_open REAL NOT NULL, bid_high REAL NOT NULL, bid_low REAL NOT NULL, bid_close REAL NOT NULL,
  ask_open REAL NOT NULL, ask_high REAL NOT NULL, ask_low REAL NOT NULL, ask_close REAL NOT NULL,
  ticks INTEGER NOT NULL,
  source TEXT NOT NULL,            -- 'live' | 'exness_csv' | 'mt5_rates' | 'synthetic'
  PRIMARY KEY (symbol, seconds, time_s)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS data_snapshots (          -- R-031: which data a backtest used
  snapshot_id TEXT PRIMARY KEY,
  description TEXT NOT NULL,
  symbol TEXT NOT NULL,
  seconds INTEGER NOT NULL,
  start_s INTEGER NOT NULL, end_s INTEGER NOT NULL,
  n_bars INTEGER NOT NULL,
  created_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS bundles (                 -- R-033 immutable version bundle
  bundle_id TEXT PRIMARY KEY,
  parent_bundle_id TEXT,
  code_commit TEXT,
  strategy_id TEXT NOT NULL,
  strategy_kind TEXT NOT NULL,
  params_json TEXT NOT NULL,
  prompt_hashes_json TEXT NOT NULL DEFAULT '{}',
  model_ids_json TEXT NOT NULL DEFAULT '{}',
  config_hash TEXT NOT NULL,
  created_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS trial_ledger (            -- R-031 append-only; no UPDATE/DELETE path in code
  id INTEGER PRIMARY KEY,
  created_ms INTEGER NOT NULL,
  bundle_id TEXT NOT NULL,
  change_type TEXT NOT NULL,       -- param | rule | prompt | model | baseline
  description TEXT NOT NULL,
  data_snapshot_id TEXT NOT NULL,
  cost_model_json TEXT NOT NULL,
  n_trades INTEGER NOT NULL,
  n_days INTEGER NOT NULL,
  sharpe REAL, sortino REAL, calmar REAL,
  net_return_pct REAL, max_drawdown_pct REAL, worst_day_pct REAL,
  skew REAL, kurt REAL,
  pbo REAL, dsr REAL, haircut_sr REAL, perm_p REAL,
  created_by TEXT NOT NULL,        -- owner | research_bot | pipeline
  status TEXT NOT NULL,            -- recorded | passed_offline | failed_offline | promoted_demo | ...
  report_path TEXT
);

CREATE TABLE IF NOT EXISTS backtest_runs (
  run_id TEXT PRIMARY KEY,
  bundle_id TEXT NOT NULL,
  data_snapshot_id TEXT NOT NULL,
  profile TEXT NOT NULL,
  started_ms INTEGER NOT NULL, finished_ms INTEGER,
  params_json TEXT NOT NULL,
  metrics_json TEXT,
  trades_path TEXT,
  equity_path TEXT
);

-- Journal (populated from Phase 4 on; created now so the schema is complete) R-030
CREATE TABLE IF NOT EXISTS proposals (
  proposal_id TEXT PRIMARY KEY, created_ms INTEGER NOT NULL, bot TEXT NOT NULL, bundle_id TEXT,
  action TEXT NOT NULL, side TEXT, stop_atr REAL, target_atr REAL, confidence REAL,
  thesis TEXT, invalidation TEXT, agrees_with_signal INTEGER,
  inputs_hash TEXT, raw_json TEXT NOT NULL, gate_status TEXT NOT NULL, gate_reason TEXT
);
CREATE TABLE IF NOT EXISTS intents (
  intent_id TEXT PRIMARY KEY, created_ms INTEGER NOT NULL, proposal_id TEXT, kind TEXT NOT NULL,
  payload_json TEXT NOT NULL, status TEXT NOT NULL, risk_code TEXT, handled_ms INTEGER
);
CREATE TABLE IF NOT EXISTS orders (
  order_ticket INTEGER PRIMARY KEY, intent_id TEXT, bundle_id TEXT, sent_ms INTEGER NOT NULL,
  request_json TEXT NOT NULL, result_json TEXT NOT NULL, retcode INTEGER NOT NULL,
  requested_price REAL, filled_price REAL, slippage_points REAL, volume REAL
);
CREATE TABLE IF NOT EXISTS fills (
  deal_ticket INTEGER PRIMARY KEY, order_ticket INTEGER, position_id INTEGER, time_ms INTEGER NOT NULL,
  type INTEGER NOT NULL, entry INTEGER NOT NULL, volume REAL NOT NULL, price REAL NOT NULL,
  commission REAL NOT NULL DEFAULT 0, swap REAL NOT NULL DEFAULT 0, profit REAL NOT NULL DEFAULT 0,
  bid_at_fill REAL, ask_at_fill REAL
);
CREATE TABLE IF NOT EXISTS positions (
  position_id INTEGER PRIMARY KEY, bundle_id TEXT, symbol TEXT NOT NULL, side TEXT NOT NULL,
  volume REAL NOT NULL, price_open REAL NOT NULL, sl REAL, tp REAL, opened_ms INTEGER NOT NULL,
  closed_ms INTEGER, status TEXT NOT NULL, last_seen_ms INTEGER
);
CREATE TABLE IF NOT EXISTS trades (                  -- closed round trips, net of costs (R-054)
  trade_id TEXT PRIMARY KEY, position_id INTEGER, bundle_id TEXT, proposal_id TEXT,
  side TEXT NOT NULL, volume REAL NOT NULL,
  entry_ms INTEGER NOT NULL, exit_ms INTEGER NOT NULL, entry_price REAL NOT NULL, exit_price REAL NOT NULL,
  gross_pnl REAL NOT NULL, spread_cost REAL NOT NULL, slippage_cost REAL NOT NULL,
  commission REAL NOT NULL, swap REAL NOT NULL, net_pnl REAL NOT NULL,
  r_multiple REAL, exit_reason TEXT NOT NULL, risk_pct_of_equity REAL
);
CREATE TABLE IF NOT EXISTS market_snapshots (
  id INTEGER PRIMARY KEY, time_ms INTEGER NOT NULL, ref_type TEXT NOT NULL, ref_id TEXT NOT NULL,
  bid REAL, ask REAL, spread_pips REAL, atr REAL, atr_pct REAL, rv REAL, rv_pctile REAL,
  regime TEXT, session_state TEXT, minutes_to_event REAL, next_event TEXT
);

CREATE TABLE IF NOT EXISTS heartbeats (
  process TEXT PRIMARY KEY, last_ms INTEGER NOT NULL, status TEXT NOT NULL, details_json TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY, created_ms INTEGER NOT NULL, severity TEXT NOT NULL, kind TEXT NOT NULL,
  message TEXT NOT NULL, details_json TEXT, delivered_ms INTEGER
);
CREATE TABLE IF NOT EXISTS incidents (
  id INTEGER PRIMARY KEY, opened_ms INTEGER NOT NULL, severity TEXT NOT NULL, kind TEXT NOT NULL,
  summary TEXT NOT NULL, acknowledged_ms INTEGER, closed_ms INTEGER
);

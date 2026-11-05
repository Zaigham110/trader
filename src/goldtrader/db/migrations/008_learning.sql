-- Phase 6 learning system (LEARNING_SYSTEM.md, D-045..D-047). Append-only where noted.

CREATE TABLE IF NOT EXISTS champions (                -- current live bundle per strategy id
  strategy_id TEXT PRIMARY KEY,
  bundle_id TEXT NOT NULL,
  since_ms INTEGER NOT NULL,
  previous_bundle_id TEXT,
  reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiments (
  experiment_id TEXT PRIMARY KEY,
  strategy_id TEXT NOT NULL,
  candidate_bundle_id TEXT NOT NULL,
  champion_bundle_id TEXT NOT NULL,
  hypothesis_id TEXT,
  change_type TEXT NOT NULL,          -- param | rule | prompt
  change_json TEXT NOT NULL,          -- the single change applied to the champion
  pre_registered_metric TEXT NOT NULL,
  regime_scope TEXT,
  stage TEXT NOT NULL,                -- proposed|offline|demo|shadow|limited_live|promoted|rejected|rolled_back
  stage_entered_ms INTEGER NOT NULL,
  created_ms INTEGER NOT NULL,
  closed_ms INTEGER,
  close_reason TEXT,
  stats_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_experiments_stage ON experiments(strategy_id, stage);

CREATE TABLE IF NOT EXISTS experiment_events (        -- append-only audit of every stage decision
  id INTEGER PRIMARY KEY,
  experiment_id TEXT NOT NULL,
  at_ms INTEGER NOT NULL,
  from_stage TEXT NOT NULL,
  to_stage TEXT NOT NULL,
  gate_json TEXT NOT NULL,            -- every check with ok/detail
  actor TEXT NOT NULL                 -- pipeline | owner | monitor
);

CREATE TABLE IF NOT EXISTS shadow_trades (            -- simulated challenger trades on live bars
  shadow_id TEXT PRIMARY KEY,
  experiment_id TEXT NOT NULL,
  bundle_id TEXT NOT NULL,
  side TEXT NOT NULL,
  volume REAL NOT NULL,
  signal_ms INTEGER NOT NULL,
  entry_ms INTEGER NOT NULL, exit_ms INTEGER,
  entry_price REAL NOT NULL, exit_price REAL,
  sl REAL NOT NULL, tp REAL,
  net_pnl REAL, r_multiple REAL, exit_reason TEXT,
  spread_cost REAL NOT NULL DEFAULT 0, slippage_cost REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL                -- open | closed
);
CREATE INDEX IF NOT EXISTS ix_shadow_bundle ON shadow_trades(bundle_id, exit_ms);

CREATE TABLE IF NOT EXISTS monitor_events (           -- production decay monitors (LEARNING_SYSTEM §4)
  id INTEGER PRIMARY KEY,
  at_ms INTEGER NOT NULL,
  bundle_id TEXT NOT NULL,
  monitor TEXT NOT NULL,              -- cusum | rolling_sharpe | mc_drawdown | pchart | fill_drift
  level TEXT NOT NULL,                -- info | warn | pause | retire
  statistic REAL, threshold REAL,
  detail TEXT
);

CREATE TABLE IF NOT EXISTS bundle_state (             -- pause/retire flags per bundle; owner un-pauses
  bundle_id TEXT PRIMARY KEY,
  status TEXT NOT NULL,               -- active | paused | retired
  since_ms INTEGER NOT NULL,
  reason TEXT
);

CREATE TABLE IF NOT EXISTS allocation_log (           -- limited-live routing decisions (D-047)
  proposal_id TEXT PRIMARY KEY,
  at_ms INTEGER NOT NULL,
  experiment_id TEXT NOT NULL,
  routed_to TEXT NOT NULL             -- champion | challenger
);

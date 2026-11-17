-- goldtrader schema v12 (D-060): slow positioning context, the inputs of every decision, and
-- stored reports for the dashboard. Times are epoch milliseconds UTC.

-- CFTC Commitments of Traders, COMEX gold (contract 088691), disaggregated futures-only.
CREATE TABLE IF NOT EXISTS cot_reports (
  report_date TEXT PRIMARY KEY,        -- YYYY-MM-DD (positions as of that Tuesday)
  mm_long INTEGER NOT NULL,            -- managed money long contracts
  mm_short INTEGER NOT NULL,
  open_interest INTEGER,
  fetched_ms INTEGER NOT NULL
);

-- What the decision cycle knew: BotContext.snapshot() of that cycle, zlib-compressed JSON.
CREATE TABLE IF NOT EXISTS decision_snapshots (
  cycle_id TEXT PRIMARY KEY,
  at_ms INTEGER NOT NULL,
  signal_side INTEGER NOT NULL,
  bid REAL, ask REAL, atr REAL,
  inputs_z BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_decision_snapshots_at ON decision_snapshots(at_ms);

-- The exact messages sent to a model for one run (not kept for the chat bot), zlib JSON.
CREATE TABLE IF NOT EXISTS agent_inputs (
  run_id TEXT PRIMARY KEY,
  agent TEXT NOT NULL,
  created_ms INTEGER NOT NULL,
  messages_z BLOB NOT NULL
);

-- Reports computed by the brain for the dashboard to read (one row per report name).
CREATE TABLE IF NOT EXISTS stored_reports (
  name TEXT PRIMARY KEY,
  at_ms INTEGER NOT NULL,
  body_json TEXT NOT NULL
);

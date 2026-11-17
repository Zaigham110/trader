-- goldtrader schema v10: time machine runs (D-058). One row per replay of stored history through
-- the deterministic strategy, risk rules and costs. Written by `goldctl timemachine`; the
-- dashboard only reads it. Times are epoch milliseconds UTC.

CREATE TABLE IF NOT EXISTS timemachine_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_ms INTEGER NOT NULL,
  finished_ms INTEGER,
  status TEXT NOT NULL,             -- running | done | error
  params_json TEXT NOT NULL,
  result_json TEXT,
  error TEXT
);

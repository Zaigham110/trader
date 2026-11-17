-- goldtrader schema v11: paper candidates (D-059). A strategy the owner wants simulated on live
-- bars next to the live system, outside the learning pipeline: the brain registers every active
-- row with the shadow ledger, which logs simulated trades to shadow_trades (experiment_id
-- 'paper') and never builds an order. Times are epoch milliseconds UTC.

CREATE TABLE IF NOT EXISTS paper_candidates (
  bundle_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  params_json TEXT NOT NULL,
  label TEXT NOT NULL,
  created_ms INTEGER NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);

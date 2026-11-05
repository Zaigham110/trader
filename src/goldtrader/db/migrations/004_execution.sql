-- goldtrader schema v4: execution engine, journal and reconciler (ARCHITECTURE.md §2.1 `execution`,
-- `reconciler`, `journal`; R-003, R-004, R-007, R-008, R-054). migrate() records the schema_version
-- row itself. All times are epoch milliseconds UTC.

-- Every outbound trade request (order_check / order_send, every attempt) and every risk-engine
-- rejection the engine made before sending. `orders` (001) keeps one row per broker order ticket;
-- rejected sends have no ticket, so they live only here.
CREATE TABLE IF NOT EXISTS order_attempts (
  id INTEGER PRIMARY KEY,
  sent_ms INTEGER NOT NULL,
  kind TEXT NOT NULL,              -- entry | close | sltp | remove
  stage TEXT NOT NULL,             -- risk | check | send
  attempt INTEGER NOT NULL DEFAULT 1,
  intent_id TEXT,
  bundle_id TEXT,
  position_id INTEGER,
  order_ticket INTEGER,
  request_json TEXT,
  result_json TEXT,
  retcode INTEGER,                 -- NULL when the request never left the process
  risk_code TEXT,                  -- risk-engine reject code when stage = 'risk'
  requested_price REAL,
  filled_price REAL,
  slippage_points REAL,            -- signed, adverse positive, in symbol points (R-007)
  volume REAL
);
CREATE INDEX IF NOT EXISTS ix_order_attempts_sent ON order_attempts(sent_ms);
CREATE INDEX IF NOT EXISTS ix_order_attempts_position ON order_attempts(position_id);

-- Columns the trade attribution needs on the position (R-054): the stop the risk engine approved
-- (R multiple base), the risk taken, the deterministic identity and where the position came from.
ALTER TABLE positions ADD COLUMN proposal_id TEXT;
ALTER TABLE positions ADD COLUMN intent_id TEXT;
ALTER TABLE positions ADD COLUMN magic INTEGER;
ALTER TABLE positions ADD COLUMN initial_stop REAL;
ALTER TABLE positions ADD COLUMN risk_pct REAL;
ALTER TABLE positions ADD COLUMN risk_usd REAL;
ALTER TABLE positions ADD COLUMN source TEXT NOT NULL DEFAULT 'engine';  -- engine | adopted
ALTER TABLE positions ADD COLUMN exit_reason TEXT;

-- Which action an order row belongs to and the position it touched.
ALTER TABLE orders ADD COLUMN kind TEXT;
ALTER TABLE orders ADD COLUMN action INTEGER;
ALTER TABLE orders ADD COLUMN position_id INTEGER;

-- The full IntentResult so a repeated intent_id returns the stored answer (idempotency).
ALTER TABLE intents ADD COLUMN result_json TEXT;
ALTER TABLE intents ADD COLUMN source TEXT;

-- Reconciler cursors (R-008): name → integer value, e.g. deals_cursor_ms.
CREATE TABLE IF NOT EXISTS reconcile_state (
  name TEXT PRIMARY KEY,
  value INTEGER NOT NULL,
  updated_ms INTEGER NOT NULL
);

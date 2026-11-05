-- goldtrader schema v3: notify outbox state, confirmation state machine, process exits.
-- Supports ARCHITECTURE.md §2.2 (telegram / health rows), §4 (supervisor, PID locks), §5 (exit log),
-- §6 (confirmation tokens expire after 2 minutes) and RISK_POLICY.md §3 / §7 (R-051, R-052).
-- Times are epoch milliseconds UTC.

-- Outbox cursors (pattern ported from the old platform's alerts.ts): name → integer value.
CREATE TABLE IF NOT EXISTS notify_cursors (
  name TEXT PRIMARY KEY,
  value INTEGER NOT NULL,
  updated_ms INTEGER NOT NULL
);

-- Per-alert delivery attempts. `alerts.delivered_ms` stays the single source of truth for "sent";
-- this table only tracks retries (max 8) and the backoff window between them.
CREATE TABLE IF NOT EXISTS alert_deliveries (
  alert_id INTEGER PRIMARY KEY,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_ms INTEGER,
  last_error TEXT,
  gave_up_ms INTEGER
);

-- Confirm-button state machine (AGENTS.md §3.5, ARCHITECTURE.md §6). The token never appears in the
-- summary shown to the owner or to any model; it only travels inside the button callback data.
CREATE TABLE IF NOT EXISTS pending_confirmations (
  action_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,                 -- pause | resume | close_position | set_config_value | kill | kill_reset | ...
  params_json TEXT NOT NULL,
  summary TEXT NOT NULL,
  created_ms INTEGER NOT NULL,
  expires_ms INTEGER NOT NULL,
  status TEXT NOT NULL,               -- pending | confirmed | cancelled | expired
  token TEXT NOT NULL,
  resolved_ms INTEGER,
  chat_id INTEGER,
  message_id TEXT
);
CREATE INDEX IF NOT EXISTS ix_pending_confirmations_status ON pending_confirmations(status, expires_ms);

-- Every child exit the supervisor observes (ARCHITECTURE.md §4 "restart on failure with backoff").
CREATE TABLE IF NOT EXISTS process_exits (
  id INTEGER PRIMARY KEY,
  process TEXT NOT NULL,
  pid INTEGER,
  exit_code INTEGER,
  started_ms INTEGER NOT NULL,
  exited_ms INTEGER NOT NULL,
  reason TEXT                         -- exited | crashed | signal:<n> | stopped
);
CREATE INDEX IF NOT EXISTS ix_process_exits_process ON process_exits(process, exited_ms);

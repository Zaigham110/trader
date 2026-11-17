-- goldtrader schema v13 (D-061): signals posted in Telegram channels the owner follows, recorded
-- the moment they are seen and scored on our own prices. Read-only context: nothing here reaches
-- the decision cycle or the gateway. Times are epoch milliseconds UTC.

-- Channels and groups the reader watches. `id` is Telegram's chat id (-100... for channels).
CREATE TABLE IF NOT EXISTS signal_channels (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  username TEXT,
  gold_only INTEGER NOT NULL DEFAULT 0,  -- 1: every signal there is gold even when it does not say so
  active INTEGER NOT NULL DEFAULT 1,
  added_ms INTEGER NOT NULL
);

-- Every message seen in a watched channel. `text` is what it said when first seen and never changes.
CREATE TABLE IF NOT EXISTS signal_messages (
  channel_id INTEGER NOT NULL,
  msg_id INTEGER NOT NULL,
  posted_ms INTEGER NOT NULL,
  received_ms INTEGER NOT NULL,
  text TEXT NOT NULL,
  seen_edited INTEGER NOT NULL DEFAULT 0,  -- 1: already edited when first seen (not scored)
  edit_count INTEGER NOT NULL DEFAULT 0,
  last_edit_ms INTEGER,
  last_text TEXT,                          -- the latest edited wording, for comparison
  deleted_ms INTEGER,
  parsed TEXT NOT NULL DEFAULT 'pending',  -- pending | signal | not_signal | unclear
  ai_cost_usd REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (channel_id, msg_id)
);
CREATE INDEX IF NOT EXISTS ix_signal_messages_received ON signal_messages(received_ms);

-- One row per message that was a gold signal, frozen as first posted.
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY,
  channel_id INTEGER NOT NULL,
  msg_id INTEGER NOT NULL,
  posted_ms INTEGER NOT NULL,
  side INTEGER NOT NULL,                   -- 1 buy, -1 sell
  entry_lo REAL, entry_hi REAL,            -- NULL: enter at the market when posted
  stop REAL,
  targets_json TEXT NOT NULL,
  parser TEXT NOT NULL,                    -- rules | ai
  status TEXT NOT NULL DEFAULT 'open',     -- open | filled | closed | expired | no_stop | off_market | late
  entry_px REAL, entry_ms INTEGER,
  exit_px REAL, exit_ms INTEGER, exit_why TEXT,
  r REAL,
  UNIQUE (channel_id, msg_id)
);
CREATE INDEX IF NOT EXISTS ix_signals_status ON signals(status);

-- goldtrader schema v6: Telegram chat assistant (AGENTS.md §2 Chat "Writes": chat_messages,
-- chat_actions; §3.5; PRD R-040..R-043; D-039). The confirm-button state machine itself lives in
-- pending_confirmations (003); chat_actions links a prepared action to the chat run that prepared it
-- and records the execution result. migrate() records the schema_version row itself. Times are
-- epoch milliseconds UTC. Message text is stored redacted (R-053).

CREATE TABLE IF NOT EXISTS chat_messages (
  id INTEGER PRIMARY KEY,
  created_ms INTEGER NOT NULL,
  chat_id INTEGER NOT NULL,
  role TEXT NOT NULL,               -- owner | assistant | system
  text TEXT NOT NULL,               -- redacted
  tool_names_json TEXT NOT NULL DEFAULT '[]',
  run_id TEXT,
  update_id INTEGER
);
CREATE INDEX IF NOT EXISTS ix_chat_messages_chat ON chat_messages(chat_id, id);

CREATE TABLE IF NOT EXISTS chat_actions (
  action_id TEXT PRIMARY KEY,       -- = pending_confirmations.action_id
  created_ms INTEGER NOT NULL,
  chat_id INTEGER,
  kind TEXT NOT NULL,
  prepared_by TEXT NOT NULL,        -- command | agent:<run_id>
  summary TEXT NOT NULL,            -- never the token
  executed_ms INTEGER,
  result TEXT
);

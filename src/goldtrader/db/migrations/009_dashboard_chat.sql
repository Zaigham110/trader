-- goldtrader schema v9: questions typed into the local dashboard (D-057). The dashboard process
-- can INSERT into this one table and nothing else; the brain reads it, answers with the Chat bot
-- and stores both sides in chat_messages under chat_id 0. Times are epoch milliseconds UTC.

CREATE TABLE IF NOT EXISTS dashboard_chat_inbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_ms INTEGER NOT NULL,
  text TEXT NOT NULL
);

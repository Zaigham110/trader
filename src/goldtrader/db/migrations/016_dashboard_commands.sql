-- goldtrader schema v16: owner commands confirmed on the local dashboard (D-083). The dashboard
-- process can INSERT into this one table and nothing else (like dashboard_chat_inbox, D-057); the
-- brain turns each row into an intent for the gateway, whose risk engine checks it like any other,
-- and writes the outcome back here. Times are epoch milliseconds UTC.

CREATE TABLE IF NOT EXISTS dashboard_commands (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_ms INTEGER NOT NULL,
  kind TEXT NOT NULL,                    -- pause | resume | kill | close | breakeven
  position_id INTEGER,                   -- for close and breakeven
  status TEXT NOT NULL DEFAULT 'queued', -- queued | done | refused | failed | expired
  detail TEXT,
  handled_ms INTEGER
);

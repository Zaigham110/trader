-- goldtrader schema v7: calendar events and feed fetch state (D-038; ARCHITECTURE.md §2.2
-- `calendar`, `news`; AGENTS.md §3.1 Research inputs `events`). migrate() records the
-- schema_version row itself. Times are epoch milliseconds UTC.

-- Scheduled economic events from the BLS iCal feed, the committed FOMC dates, the LBMA fix times
-- and (owner-enabled, KU-013) the ForexFactory weekly JSON. `event_id` is the parser's stable id
-- (`bls:<uid>`, `fomc:<date>`, `lbma:<date>:<n>:<time>`, `ff:<hash>`); a re-fetch updates the row
-- in place so `actual` fills in after the release. Read by the gateway's `events_fn` (R-006
-- windows) and by brain features ("next event").
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  time_ms INTEGER NOT NULL,
  title TEXT NOT NULL,
  country TEXT NOT NULL,
  impact TEXT NOT NULL,            -- high | medium | low | holiday | fix
  source TEXT NOT NULL,            -- bls | fomc | lbma | forexfactory
  actual REAL,
  forecast REAL,
  previous REAL,
  fetched_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_time ON events(time_ms);

-- One row per named feed (news feeds by FeedConfig.name, calendars by source name): conditional
-- request validators and the outcome of the last attempt, so a failing feed is visible to health
-- (AGENTS §3.1 "feed failures → health alert") without raising inside the fetch loop.
CREATE TABLE IF NOT EXISTS feed_state (
  name TEXT PRIMARY KEY,
  etag TEXT,
  last_modified TEXT,
  last_fetch_ms INTEGER,
  last_error TEXT
);

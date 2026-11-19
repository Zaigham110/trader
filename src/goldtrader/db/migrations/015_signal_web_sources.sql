-- Web sources for the signal reader (D-071): an analysis page that publishes its trade plans as
-- JSON. Each analyst on the page becomes a channel in signal_channels, scored like any other.
CREATE TABLE IF NOT EXISTS signal_web_sources (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  url TEXT NOT NULL UNIQUE,
  active INTEGER NOT NULL DEFAULT 1,
  added_ms INTEGER NOT NULL,
  last_ok_ms INTEGER,
  last_error TEXT
);

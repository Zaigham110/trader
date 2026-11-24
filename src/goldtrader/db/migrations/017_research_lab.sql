-- goldtrader schema v17: the research lab's notebook (D-085). One row per rule ever proposed,
-- written before it is tested, with why it was proposed, how far it got through the gates and what
-- was learned. Dead ends stay, so the research agent reads its own history. Times are epoch
-- milliseconds UTC.

CREATE TABLE IF NOT EXISTS research_ideas (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL UNIQUE,            -- hash of the canonical rule: a rule is tested once
  created_ms INTEGER NOT NULL,
  source TEXT NOT NULL,                -- agent | optimiser | owner | seed
  spec_json TEXT NOT NULL,             -- the rule (research/rules.py)
  what TEXT NOT NULL,                  -- the rule in plain words
  rationale TEXT NOT NULL DEFAULT '',  -- why it should make money, stated before the test
  expectation TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'proposed', -- proposed | failed | awaiting_blind | passed | error
  gate INTEGER NOT NULL DEFAULT 0,     -- the last gate passed (0-5)
  failed_at TEXT,                      -- name of the gate that stopped it
  results_json TEXT,
  lesson TEXT,                         -- what the agent concluded afterwards
  tested_ms INTEGER,
  paper_bundle TEXT                    -- set when it was sent to paper trading
);
CREATE INDEX IF NOT EXISTS ix_research_ideas_status ON research_ideas(status, gate);

CREATE TABLE IF NOT EXISTS research_runs (   -- one row per lab session
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_ms INTEGER NOT NULL,
  finished_ms INTEGER,
  status TEXT NOT NULL,                -- running | done | failed
  proposed INTEGER NOT NULL DEFAULT 0,
  tested INTEGER NOT NULL DEFAULT 0,
  passed_gate2 INTEGER NOT NULL DEFAULT 0,
  passed_gate4 INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0,
  summary_json TEXT
);

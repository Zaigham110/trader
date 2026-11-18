-- Learning loop (D-064): graded decisions, the agents' playbook, tested strategy ideas, nightly runs.

-- One row per decision cycle in which the deterministic signal wanted a trade: what happened to
-- it and how the signal played out as a code-only trade (R = multiples of the amount risked).
CREATE TABLE IF NOT EXISTS decision_scores (
  cycle_id TEXT PRIMARY KEY,
  at_ms INTEGER NOT NULL,
  side INTEGER NOT NULL,
  what TEXT NOT NULL,               -- taken | ai_veto | code_block | ai_down
  reason TEXT,
  action TEXT,                      -- what the Strategy agent proposed: enter | wait | ...
  confidence REAL,
  hour_utc INTEGER,
  regime TEXT,
  r REAL,                           -- NULL until the signal has played out
  exit TEXT,
  scored_ms INTEGER
);
CREATE INDEX IF NOT EXISTS ix_decision_scores_at ON decision_scores(at_ms);

-- The playbook the Strategy agent reads before every decision. Rewritten each night by the
-- nightly review from measured numbers; old rows are kept with active=0.
CREATE TABLE IF NOT EXISTS playbook (
  id INTEGER PRIMARY KEY,
  night TEXT NOT NULL,
  created_ms INTEGER NOT NULL,
  tag TEXT NOT NULL,
  text TEXT NOT NULL,
  evidence_n INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_playbook_active ON playbook(active, id);

-- Every strategy idea the research agent proposed, tested or not. `key` is kind + effective
-- parameters, so the same idea is never tested twice.
CREATE TABLE IF NOT EXISTS strategy_ideas (
  id INTEGER PRIMARY KEY,
  night TEXT NOT NULL,
  created_ms INTEGER NOT NULL,
  kind TEXT NOT NULL,
  params_json TEXT NOT NULL,
  key TEXT NOT NULL UNIQUE,
  what TEXT NOT NULL,
  rationale TEXT NOT NULL,
  status TEXT NOT NULL,             -- holds | fails | too_few_trades | invalid:<why>
  in_sample_json TEXT,
  held_out_json TEXT,
  paper_bundle_id TEXT
);

CREATE TABLE IF NOT EXISTS learning_runs (
  night TEXT PRIMARY KEY,           -- UTC date of the trading day reviewed
  started_ms INTEGER NOT NULL,
  finished_ms INTEGER,
  status TEXT NOT NULL,             -- running | done | failed
  summary_json TEXT
);

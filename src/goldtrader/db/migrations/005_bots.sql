-- goldtrader schema v5: bot outputs and the brain's decision-cycle journal (AGENTS.md §2 "Writes",
-- §3.1–3.4; ARCHITECTURE.md §3.1; R-020, R-024, R-025, R-030). `proposals` and `intents` already
-- exist (001 / 004). migrate() records the schema_version row itself. Times are epoch ms UTC.

-- Raw news as fetched (ARCHITECTURE §2.2 `news`). Only the Research bot ever reads `body`, and only
-- defanged inside <untrusted_news> delimiters (R-024).
CREATE TABLE IF NOT EXISTS news_items (
  item_id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  title TEXT NOT NULL,
  url TEXT,
  published_ms INTEGER NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  fetched_ms INTEGER NOT NULL,
  processed_ms INTEGER
);
CREATE INDEX IF NOT EXISTS ix_news_items_published ON news_items(published_ms);

-- Structured, code-clipped extraction per item (AGENTS §3.1 output schema).
CREATE TABLE IF NOT EXISTS news_events (
  id INTEGER PRIMARY KEY,
  created_ms INTEGER NOT NULL,
  run_id TEXT NOT NULL,
  item_id TEXT NOT NULL,
  relevance TEXT NOT NULL,          -- none | low | medium | high
  event_type TEXT NOT NULL,         -- fed | inflation | employment | growth | usd | geopolitical | flows | other
  direction_for_gold TEXT NOT NULL, -- bullish | bearish | neutral | unclear
  magnitude TEXT NOT NULL,          -- low | medium | high
  horizon TEXT NOT NULL,            -- minutes | hours | days
  confidence REAL NOT NULL,
  summary TEXT NOT NULL,
  degraded INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_news_events_created ON news_events(created_ms);

-- Items the extraction dropped, with the reason ("degraded" when the provider was unavailable).
CREATE TABLE IF NOT EXISTS news_dropped (
  id INTEGER PRIMARY KEY,
  created_ms INTEGER NOT NULL,
  run_id TEXT NOT NULL,
  item_id TEXT NOT NULL,
  reason TEXT NOT NULL
);

-- Weekly research hypotheses (AGENTS §3.1 weekly schema). `flagged_lookahead` is set by code.
CREATE TABLE IF NOT EXISTS hypotheses (
  id INTEGER PRIMARY KEY,
  created_ms INTEGER NOT NULL,
  run_id TEXT NOT NULL,
  title TEXT NOT NULL,
  change_type TEXT NOT NULL,        -- param | rule | prompt
  change TEXT NOT NULL,
  rationale TEXT NOT NULL,
  pre_registered_metric TEXT NOT NULL,
  regime_scope TEXT NOT NULL,
  flagged_lookahead INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'proposed'
);

-- Risk-advisor runs: what the model advised and what code's hard rules decided (AGENTS §3.3).
CREATE TABLE IF NOT EXISTS risk_budgets (
  id INTEGER PRIMARY KEY,
  created_ms INTEGER NOT NULL,
  run_id TEXT NOT NULL,
  advised INTEGER NOT NULL,         -- the model's number (0–100)
  budget INTEGER NOT NULL,          -- min(advised, hard rules) — the only number anyone else reads
  reasons_json TEXT NOT NULL,       -- model reasons
  hard_rules_json TEXT NOT NULL,    -- code reasons that forced 0 (empty list when none)
  degraded INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_risk_budgets_created ON risk_budgets(created_ms);

-- Post-mortems (AGENTS §3.4). Attribution numbers are code-computed; the model copies them.
CREATE TABLE IF NOT EXISTS postmortems (
  id INTEGER PRIMARY KEY,
  created_ms INTEGER NOT NULL,
  run_id TEXT NOT NULL,
  trade_id TEXT NOT NULL,
  outcome_class TEXT NOT NULL,
  signal_pnl REAL NOT NULL,
  timing_pnl REAL NOT NULL,
  slippage REAL NOT NULL,
  costs REAL NOT NULL,
  confidence REAL NOT NULL,
  degraded INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_postmortems_trade ON postmortems(trade_id);

-- Lessons feed the Strategy prompt (latest 5) and never parameters (LEARNING_SYSTEM §6).
-- `norm_text` is the deduplication key (lower-case, punctuation stripped, whitespace collapsed).
CREATE TABLE IF NOT EXISTS lessons (
  id INTEGER PRIMARY KEY,
  created_ms INTEGER NOT NULL,
  postmortem_id INTEGER,
  trade_id TEXT,
  text TEXT NOT NULL,
  norm_text TEXT NOT NULL UNIQUE
);

-- Daily / weekly briefs (AGENTS §3.4): narrative over code-computed numbers.
CREATE TABLE IF NOT EXISTS briefs (
  id INTEGER PRIMARY KEY,
  created_ms INTEGER NOT NULL,
  run_id TEXT NOT NULL,
  kind TEXT NOT NULL,               -- daily | weekly
  sections_json TEXT NOT NULL,
  questions_json TEXT NOT NULL,
  degraded INTEGER NOT NULL DEFAULT 0
);

-- One row per DecisionCycle.run_once(): every step journaled (R-030; ARCHITECTURE §3.1).
CREATE TABLE IF NOT EXISTS brain_cycles (
  cycle_id TEXT PRIMARY KEY,
  started_ms INTEGER NOT NULL,
  finished_ms INTEGER,
  status TEXT NOT NULL,             -- sent | gated | wait | blocked | flat | gateway_unavailable | error
  proposal_id TEXT,
  gate_reason TEXT,
  risk_budget INTEGER,
  steps_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_brain_cycles_started ON brain_cycles(started_ms);

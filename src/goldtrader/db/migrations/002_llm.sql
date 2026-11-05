-- goldtrader schema v2: LLM layer (R-023, R-025). migrate() records the schema_version row itself.
-- All times are epoch milliseconds UTC.

CREATE TABLE IF NOT EXISTS llm_calls (                 -- one row per model call, failed calls included
  id INTEGER PRIMARY KEY,
  created_ms INTEGER NOT NULL,
  agent TEXT NOT NULL,
  run_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  prompt_hash TEXT NOT NULL,
  schema_version INTEGER NOT NULL,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0,
  latency_ms INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,            -- ok | invalid | tool_use | transient_error | timeout | refusal | error
  stop_reason TEXT,
  payload_hash TEXT                -- sha256 of the outbound payload (LOCAL_LLM.md §6); never the payload
);
CREATE INDEX IF NOT EXISTS ix_llm_calls_created ON llm_calls(created_ms);
CREATE INDEX IF NOT EXISTS ix_llm_calls_run ON llm_calls(run_id);

CREATE TABLE IF NOT EXISTS agent_runs (                -- one row per run_agent() invocation
  run_id TEXT PRIMARY KEY,
  agent TEXT NOT NULL,
  started_ms INTEGER NOT NULL,
  finished_ms INTEGER,
  status TEXT NOT NULL,            -- ok | invalid | timeout | budget | breaker_open | error | degraded
  turns INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0,
  degraded INTEGER NOT NULL DEFAULT 0,
  failure_reason TEXT,
  output_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_agent_runs_agent ON agent_runs(agent, started_ms);

CREATE TABLE IF NOT EXISTS breaker_state (             -- circuit breaker per agent (LOCAL_LLM.md §5.5)
  agent TEXT PRIMARY KEY,
  state TEXT NOT NULL,             -- closed | open | half_open
  failures INTEGER NOT NULL DEFAULT 0,
  opened_ms INTEGER,
  next_trial_ms INTEGER,
  updated_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS prompts (                   -- registered prompt texts by version (R-033)
  name TEXT NOT NULL,
  version INTEGER NOT NULL,
  hash TEXT NOT NULL,
  text TEXT NOT NULL,
  created_ms INTEGER NOT NULL,
  PRIMARY KEY (name, version)
);

"""SQLite persistence for the LLM layer (R-023, R-025): call ledger, run ledger, breaker state, prompts.

Lives next to `goldtrader.db.repo.Repo` but in its own class so the agent runtime depends on a
small surface. Tables come from migration `002_llm.sql`.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from goldtrader.db.connect import now_ms
from goldtrader.llm.prompts import PromptRef
from goldtrader.llm.provider import Usage


def _stable(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


class LLMStore:
    def __init__(self, conn: sqlite3.Connection):
        self.c = conn

    # --- calls ----------------------------------------------------------------------------
    def record_call(
        self,
        *,
        agent: str,
        run_id: str,
        model_id: str,
        prompt_hash: str,
        schema_version: int,
        usage: Usage,
        cost_usd: float,
        latency_ms: int,
        status: str,
        stop_reason: str | None = None,
        payload_hash: str | None = None,
        created_ms: int | None = None,
    ) -> int:
        """Append one model call (R-025). Failed calls are recorded too, with zero usage."""
        cur = self.c.execute(
            "INSERT INTO llm_calls(created_ms, agent, run_id, model_id, prompt_hash, schema_version, "
            "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd, "
            "latency_ms, status, stop_reason, payload_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                created_ms if created_ms is not None else now_ms(),
                agent,
                run_id,
                model_id,
                prompt_hash,
                schema_version,
                usage.input_tokens,
                usage.output_tokens,
                usage.cache_read_tokens,
                usage.cache_write_tokens,
                cost_usd,
                latency_ms,
                status,
                stop_reason,
                payload_hash,
            ),
        )
        return int(cur.lastrowid)

    def calls_for_run(self, run_id: str) -> list[sqlite3.Row]:
        return list(self.c.execute("SELECT * FROM llm_calls WHERE run_id=? ORDER BY id", (run_id,)))

    def spent_since(self, since_ms: int) -> float:
        """Total USD recorded in `llm_calls` since `since_ms` (budget input, R-023)."""
        v = self.c.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE created_ms>=?", (since_ms,)
        ).fetchone()[0]
        return float(v or 0.0)

    # --- runs -----------------------------------------------------------------------------
    def record_run(
        self,
        *,
        run_id: str,
        agent: str,
        started_ms: int,
        finished_ms: int | None,
        status: str,
        turns: int,
        cost_usd: float,
        degraded: bool,
        failure_reason: str | None,
        output: Any = None,
    ) -> None:
        self.c.execute(
            "INSERT OR REPLACE INTO agent_runs(run_id, agent, started_ms, finished_ms, status, turns, "
            "cost_usd, degraded, failure_reason, output_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                agent,
                started_ms,
                finished_ms,
                status,
                turns,
                cost_usd,
                int(degraded),
                failure_reason,
                _stable(output) if output is not None else None,
            ),
        )

    def get_run(self, run_id: str) -> sqlite3.Row | None:
        return self.c.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()

    # --- breaker --------------------------------------------------------------------------
    def get_state(self, agent: str) -> dict[str, Any] | None:
        row = self.c.execute("SELECT * FROM breaker_state WHERE agent=?", (agent,)).fetchone()
        if row is None:
            return None
        return {
            "state": row["state"],
            "failures": int(row["failures"]),
            "opened_ms": row["opened_ms"],
            "next_trial_ms": row["next_trial_ms"],
            "updated_ms": int(row["updated_ms"]),
        }

    def set_state(self, agent: str, state: dict[str, Any]) -> None:
        self.c.execute(
            "INSERT INTO breaker_state(agent, state, failures, opened_ms, next_trial_ms, updated_ms) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(agent) DO UPDATE SET state=excluded.state, "
            "failures=excluded.failures, opened_ms=excluded.opened_ms, "
            "next_trial_ms=excluded.next_trial_ms, updated_ms=excluded.updated_ms",
            (
                agent,
                state["state"],
                int(state.get("failures", 0)),
                state.get("opened_ms"),
                state.get("next_trial_ms"),
                int(state.get("updated_ms") or now_ms()),
            ),
        )

    # --- prompts --------------------------------------------------------------------------
    def record_prompt(self, ref: PromptRef, created_ms: int | None = None) -> None:
        """Store the prompt text once per (name, version); re-recording is a no-op."""
        self.c.execute(
            "INSERT OR IGNORE INTO prompts(name, version, hash, text, created_ms) VALUES (?,?,?,?,?)",
            (
                ref.name,
                ref.version,
                ref.hash,
                ref.text,
                created_ms if created_ms is not None else now_ms(),
            ),
        )

    def get_prompt(self, name: str, version: int) -> PromptRef | None:
        row = self.c.execute(
            "SELECT * FROM prompts WHERE name=? AND version=?", (name, version)
        ).fetchone()
        if row is None:
            return None
        return PromptRef(
            name=row["name"], version=int(row["version"]), hash=row["hash"], text=row["text"]
        )

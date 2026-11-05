"""Learning-system persistence (migration 008; LEARNING_SYSTEM §1, §3, §3.5; D-046, D-047).

Owns `champions`, `experiments`, `experiment_events` (append-only), `bundle_state` and
`allocation_log`. `shadow_trades`, `monitor_events`, `trades`, `bundles` and `hypotheses` are only
READ here (they are written by the shadow simulator, the monitors, the journal, the trial ledger and
the bot registry). Nothing in this module touches config (R-034).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from goldtrader.learning.types import (
    ACTIVE_STAGES,
    Change,
    Experiment,
    Stage,
    StageDecision,
)

WEEK_MS = 7 * 86_400_000
PAUSE_LEVELS = ("pause", "retire")
OPEN_STAGES = (Stage.PROPOSED, *ACTIVE_STAGES)


def _stable(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_json_default)


def _json_default(o: Any) -> Any:
    """numpy scalars and other non-JSON values → plain Python (stats blobs)."""
    item = getattr(o, "item", None)
    if callable(item):
        return item()
    return str(o)


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if r is None else dict(r)


def _rows(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(r) for r in cur]


def experiment_from_row(r: Mapping[str, Any]) -> Experiment:
    ch = json.loads(r["change_json"])
    return Experiment(
        experiment_id=r["experiment_id"],
        strategy_id=r["strategy_id"],
        candidate_bundle_id=r["candidate_bundle_id"],
        champion_bundle_id=r["champion_bundle_id"],
        change=Change(
            change_type=ch["change_type"],
            key=ch["key"],
            value=ch["value"],
            old_value=ch.get("old_value"),
        ),
        pre_registered_metric=r["pre_registered_metric"],
        stage=Stage(r["stage"]),
        stage_entered_ms=int(r["stage_entered_ms"]),
        created_ms=int(r["created_ms"]),
        hypothesis_id=r["hypothesis_id"],
        regime_scope=r["regime_scope"],
        closed_ms=r["closed_ms"],
        close_reason=r["close_reason"],
        stats=json.loads(r["stats_json"] or "{}"),
    )


class LearningStore:
    """Thin SQL over the learning tables. Every method is a single statement or a small
    transaction; the connection is in autocommit mode (db/connect.py)."""

    def __init__(self, conn: sqlite3.Connection):
        self.c = conn

    # --- champions (R-033 pointer per strategy; §3.5 rollback keeps the previous) ---------------
    def get_champion(self, strategy_id: str) -> dict[str, Any] | None:
        return _row(
            self.c.execute("SELECT * FROM champions WHERE strategy_id=?", (strategy_id,)).fetchone()
        )

    def set_champion(self, strategy_id: str, bundle_id: str, reason: str, now_ms: int) -> None:
        """Point `strategy_id` at `bundle_id`, keeping the current bundle as `previous_bundle_id`
        so a rollback (§3.5) is a pointer change. Nothing is deleted."""
        self.c.execute(
            "INSERT INTO champions(strategy_id, bundle_id, since_ms, previous_bundle_id, reason) "
            "VALUES (?,?,?,NULL,?) ON CONFLICT(strategy_id) DO UPDATE SET "
            "previous_bundle_id=champions.bundle_id, bundle_id=excluded.bundle_id, "
            "since_ms=excluded.since_ms, reason=excluded.reason",
            (strategy_id, bundle_id, now_ms, reason),
        )

    # --- bundles (read-only view of the trial ledger's immutable bundles, R-033) ----------------
    def bundle(self, bundle_id: str) -> dict[str, Any] | None:
        r = _row(self.c.execute("SELECT * FROM bundles WHERE bundle_id=?", (bundle_id,)).fetchone())
        if r is not None:
            r["params"] = json.loads(r["params_json"])
        return r

    # --- experiments (D-046) -------------------------------------------------------------------
    def create(self, exp: Experiment) -> None:
        ch = exp.change
        self.c.execute(
            "INSERT INTO experiments(experiment_id, strategy_id, candidate_bundle_id, "
            "champion_bundle_id, hypothesis_id, change_type, change_json, pre_registered_metric, "
            "regime_scope, stage, stage_entered_ms, created_ms, closed_ms, close_reason, stats_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                exp.experiment_id,
                exp.strategy_id,
                exp.candidate_bundle_id,
                exp.champion_bundle_id,
                exp.hypothesis_id,
                ch.change_type,
                _stable(
                    {
                        "change_type": ch.change_type,
                        "key": ch.key,
                        "value": ch.value,
                        "old_value": ch.old_value,
                    }
                ),
                exp.pre_registered_metric,
                exp.regime_scope,
                exp.stage.value,
                exp.stage_entered_ms,
                exp.created_ms,
                exp.closed_ms,
                exp.close_reason,
                _stable(exp.stats),
            ),
        )

    def get(self, experiment_id: str) -> Experiment | None:
        r = self.c.execute(
            "SELECT * FROM experiments WHERE experiment_id=?", (experiment_id,)
        ).fetchone()
        return None if r is None else experiment_from_row(r)

    def active(self, strategy_id: str) -> list[Experiment]:
        """Experiments not yet closed (proposed, offline, demo, shadow, limited_live)."""
        marks = ",".join("?" * len(OPEN_STAGES))
        cur = self.c.execute(
            f"SELECT * FROM experiments WHERE strategy_id=? AND stage IN ({marks}) "
            "ORDER BY created_ms",
            (strategy_id, *[s.value for s in OPEN_STAGES]),
        )
        return [experiment_from_row(r) for r in cur]

    def in_stage(self, strategy_id: str, stage: Stage) -> list[Experiment]:
        cur = self.c.execute(
            "SELECT * FROM experiments WHERE strategy_id=? AND stage=? ORDER BY created_ms",
            (strategy_id, stage.value),
        )
        return [experiment_from_row(r) for r in cur]

    def promoted_for(self, bundle_id: str) -> Experiment | None:
        """The experiment that promoted `bundle_id` (for §3.5 rollback)."""
        r = self.c.execute(
            "SELECT * FROM experiments WHERE candidate_bundle_id=? AND stage=? "
            "ORDER BY closed_ms DESC LIMIT 1",
            (bundle_id, Stage.PROMOTED.value),
        ).fetchone()
        return None if r is None else experiment_from_row(r)

    def update_stage(self, experiment_id: str, stage: Stage, now_ms: int) -> None:
        self.c.execute(
            "UPDATE experiments SET stage=?, stage_entered_ms=? WHERE experiment_id=?",
            (stage.value, now_ms, experiment_id),
        )

    def close(self, experiment_id: str, stage: Stage, reason: str, now_ms: int) -> None:
        """Terminal stages: promoted | rejected | rolled_back."""
        if stage not in (Stage.PROMOTED, Stage.REJECTED, Stage.ROLLED_BACK):
            raise ValueError(f"{stage} is not a terminal stage")
        self.c.execute(
            "UPDATE experiments SET stage=?, stage_entered_ms=?, closed_ms=?, close_reason=? "
            "WHERE experiment_id=?",
            (stage.value, now_ms, now_ms, reason, experiment_id),
        )

    def set_stats(self, experiment_id: str, stats: Mapping[str, Any]) -> dict[str, Any]:
        """Merge `stats` into the experiment's stats blob (top-level keys replace)."""
        r = self.c.execute(
            "SELECT stats_json FROM experiments WHERE experiment_id=?", (experiment_id,)
        ).fetchone()
        if r is None:
            raise KeyError(experiment_id)
        merged = {**json.loads(r["stats_json"] or "{}"), **dict(stats)}
        self.c.execute(
            "UPDATE experiments SET stats_json=? WHERE experiment_id=?",
            (_stable(merged), experiment_id),
        )
        return json.loads(_stable(merged))

    # --- experiment_events: append-only audit (§3 "every decision is logged") -----------------
    def log_event(self, decision: StageDecision, now_ms: int) -> int:
        cur = self.c.execute(
            "INSERT INTO experiment_events(experiment_id, at_ms, from_stage, to_stage, gate_json, "
            "actor) VALUES (?,?,?,?,?,?)",
            (
                decision.experiment_id,
                now_ms,
                decision.from_stage.value,
                decision.to_stage.value,
                _stable(decision.as_dict()),
                decision.actor,
            ),
        )
        return int(cur.lastrowid)

    def events(self, experiment_id: str) -> list[dict[str, Any]]:
        rows = _rows(
            self.c.execute(
                "SELECT * FROM experiment_events WHERE experiment_id=? ORDER BY id",
                (experiment_id,),
            )
        )
        for r in rows:
            r["gate"] = json.loads(r.pop("gate_json"))
        return rows

    # --- bundle_state (§4: pause / retire; the owner un-pauses, never code) --------------------
    def status(self, bundle_id: str) -> str:
        r = self.c.execute(
            "SELECT status FROM bundle_state WHERE bundle_id=?", (bundle_id,)
        ).fetchone()
        return "active" if r is None else str(r["status"])

    def set_status(
        self, bundle_id: str, status: str, now_ms: int, reason: str | None = None
    ) -> None:
        if status not in ("active", "paused", "retired"):
            raise ValueError(f"unknown bundle status {status!r}")
        self.c.execute(
            "INSERT INTO bundle_state(bundle_id, status, since_ms, reason) VALUES (?,?,?,?) "
            "ON CONFLICT(bundle_id) DO UPDATE SET status=excluded.status, "
            "since_ms=excluded.since_ms, reason=excluded.reason",
            (bundle_id, status, now_ms, reason),
        )

    # --- allocation_log (D-047) -----------------------------------------------------------------
    def route(self, proposal_id: str, experiment_id: str, routed_to: str, now_ms: int) -> None:
        if routed_to not in ("champion", "challenger"):
            raise ValueError(routed_to)
        self.c.execute(
            "INSERT OR IGNORE INTO allocation_log(proposal_id, at_ms, experiment_id, routed_to) "
            "VALUES (?,?,?,?)",
            (proposal_id, now_ms, experiment_id, routed_to),
        )

    def counts(self, experiment_id: str) -> dict[str, int]:
        out = {"champion": 0, "challenger": 0}
        for r in self.c.execute(
            "SELECT routed_to, COUNT(*) AS n FROM allocation_log WHERE experiment_id=? "
            "GROUP BY routed_to",
            (experiment_id,),
        ):
            out[str(r["routed_to"])] = int(r["n"])
        return out

    # --- read helpers over tables other modules write ------------------------------------------
    def trades_for_bundle(self, bundle_id: str, since_ms: int = 0) -> list[dict[str, Any]]:
        """Closed journal trades of a bundle (demo or live account), oldest first."""
        return _rows(
            self.c.execute(
                "SELECT * FROM trades WHERE bundle_id=? AND exit_ms>=? ORDER BY exit_ms",
                (bundle_id, since_ms),
            )
        )

    def shadow_trades_for(self, bundle_id: str, since_ms: int = 0) -> list[dict[str, Any]]:
        """Closed shadow (simulated) trades of a bundle (D-047), oldest first."""
        return _rows(
            self.c.execute(
                "SELECT * FROM shadow_trades WHERE bundle_id=? AND status='closed' "
                "AND exit_ms IS NOT NULL AND exit_ms>=? ORDER BY exit_ms",
                (bundle_id, since_ms),
            )
        )

    def monitor_pauses(self, bundle_id: str, since_ms: int) -> list[dict[str, Any]]:
        """Monitor events at level pause or retire for a bundle since `since_ms` (§4, §3.5)."""
        marks = ",".join("?" * len(PAUSE_LEVELS))
        return _rows(
            self.c.execute(
                f"SELECT * FROM monitor_events WHERE bundle_id=? AND at_ms>=? "
                f"AND level IN ({marks}) ORDER BY at_ms",
                (bundle_id, since_ms, *PAUSE_LEVELS),
            )
        )

    def weekly_proposal_count(self, strategy_id: str, now_ms: int) -> int:
        """Experiments created for `strategy_id` in the trailing 7 days (§3.1 cap)."""
        return int(
            self.c.execute(
                "SELECT COUNT(*) FROM experiments WHERE strategy_id=? AND created_ms>?",
                (strategy_id, now_ms - WEEK_MS),
            ).fetchone()[0]
        )

    def offline_batch(self, since_ms: int) -> list[Experiment]:
        """Experiments whose offline gate ran at or after `since_ms` (the week's batch for BH,
        §3.2), whatever stage they have reached since."""
        out: list[Experiment] = []
        for r in self.c.execute(
            "SELECT * FROM experiments WHERE created_ms>=? ORDER BY created_ms", (since_ms,)
        ):
            exp = experiment_from_row(r)
            off = exp.stats.get("offline")
            if isinstance(off, dict) and off.get("perm_p") is not None:
                out.append(exp)
        return out

    def hypothesis(self, hypothesis_id: int | str) -> dict[str, Any] | None:
        return _row(
            self.c.execute("SELECT * FROM hypotheses WHERE id=?", (int(hypothesis_id),)).fetchone()
        )

    def set_hypothesis_status(self, hypothesis_id: int | str, status: str) -> None:
        """The only write outside the learning tables: the intake verdict on a hypothesis row
        (`hypotheses.status`, migration 005)."""
        self.c.execute(
            "UPDATE hypotheses SET status=? WHERE id=?", (status[:120], int(hypothesis_id))
        )

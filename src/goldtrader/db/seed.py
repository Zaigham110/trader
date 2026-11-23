"""Seed a new instance's database from the demo one: what was learned, never what was traded.

A live instance has its own database (D-081) and starts empty. Prices, the calendar and what the
learning loop collected are not tied to an account, so they can be carried over. Everything that
belongs to an account stays behind and is never read here: trades, positions, fills, orders,
intents, account snapshots, proposals, decision cycles, incidents, alerts, chat, AI cost, config
versions, symbol specs, heartbeats, cursors.

This module works from an allow-list. A table that is not named below is not touched, and a row
that already exists in the target is left as it is, so running it twice changes nothing.

    python -m goldtrader.db.seed --from state\\demo.sqlite --to state_live\\live.sqlite --dry-run

Groups:
  market    bars, cot_reports, events
  learning  lessons (marked as coming from demo), playbook, decision_scores, strategy_ideas
  paper     paper_candidates and their closed paper trades (off unless asked for: the live brain
            would then keep running every paper test too)
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from goldtrader.db.connect import connect, migrate

GROUPS: dict[str, tuple[str, ...]] = {
    "market": ("bars", "cot_reports", "events"),
    "learning": ("lessons", "playbook", "decision_scores", "strategy_ideas"),
    "paper": ("paper_candidates", "shadow_trades"),
}
DEFAULT_GROUPS = ("market", "learning")
# Never copied, whatever is asked. Kept as a list so a test can prove no group names one of them.
ACCOUNT_TABLES = frozenset(
    {
        "trades",
        "positions",
        "fills",
        "orders",
        "order_attempts",
        "intents",
        "account_snapshots",
        "proposals",
        "brain_cycles",
        "risk_budgets",
        "incidents",
        "alerts",
        "alert_deliveries",
        "reconcile_state",
        "heartbeats",
        "config_versions",
        "symbol_specs",
        "postmortems",
        "market_snapshots",
        "chat_messages",
        "chat_actions",
        "pending_confirmations",
        "llm_calls",
        "agent_runs",
        "agent_inputs",
        "notify_cursors",
        "bundles",
        "champions",
        "bundle_state",
        "experiments",
        "experiment_events",
        "allocation_log",
        "monitor_events",
    }
)


def _cols(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info([{table}])")]


def _shared(src: sqlite3.Connection, dst: sqlite3.Connection, table: str, skip=()) -> list[str]:
    have = set(_cols(dst, table))
    return [c for c in _cols(src, table) if c in have and c not in skip]


def _copy(src, dst, table: str, *, skip=(), where: str = "", fix=None) -> int:
    """INSERT OR IGNORE the shared columns of `table`; returns how many rows were new."""
    cols = _shared(src, dst, table, skip)
    if not cols:
        return 0
    rows = src.execute(f"SELECT {', '.join(cols)} FROM [{table}] {where}").fetchall()
    if fix is not None:
        rows = [fix(dict(zip(cols, r, strict=True))) for r in rows]
        rows = [tuple(r[c] for c in cols) for r in rows]
    before = dst.execute(f"SELECT COUNT(*) FROM [{table}]").fetchone()[0]
    sql = (
        f"INSERT OR IGNORE INTO [{table}]({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
    )
    dst.executemany(sql, [tuple(r) for r in rows])
    return dst.execute(f"SELECT COUNT(*) FROM [{table}]").fetchone()[0] - before


def seed(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    groups: tuple[str, ...] = DEFAULT_GROUPS,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Copy the allow-listed tables of `groups` from `src` into `dst`. Returns new rows per table.
    Nothing in `src` is written; with `dry_run` nothing in `dst` is kept either."""
    unknown = [g for g in groups if g not in GROUPS]
    if unknown:
        raise ValueError(f"unknown group(s) {unknown}; choose from {sorted(GROUPS)}")
    tables = [t for g in groups for t in GROUPS[g]]
    if ACCOUNT_TABLES.intersection(tables):  # cannot happen unless GROUPS is edited wrongly
        raise RuntimeError("an account table is in the copy list")
    paper = "paper" in groups
    out: dict[str, int] = {}
    dst.execute("BEGIN")
    try:
        for t in tables:
            if t == "lessons":
                # the lesson text is what the agents read; its trade belongs to the demo account
                def fix(r: dict) -> dict:
                    r["postmortem_id"] = None
                    r["trade_id"] = f"demo:{r['trade_id']}" if r.get("trade_id") else None
                    return r

                out[t] = _copy(src, dst, t, skip=("id",), fix=fix)
            elif t == "playbook":
                cols = _shared(src, dst, t, ("id",))
                have = {
                    (r[0], r[1], r[2]) for r in dst.execute("SELECT night, tag, text FROM playbook")
                }
                rows = [
                    tuple(r)
                    for r in src.execute(f"SELECT {', '.join(cols)} FROM playbook ORDER BY id")
                    if (r["night"], r["tag"], r["text"]) not in have
                ]
                dst.executemany(
                    f"INSERT INTO playbook({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                    rows,
                )
                out[t] = len(rows)
            elif t == "strategy_ideas":

                def fix(r: dict) -> dict:
                    if not paper:
                        r["paper_bundle_id"] = None  # its paper test is not coming along
                    return r

                out[t] = _copy(src, dst, t, skip=("id",), fix=fix)
            elif t == "shadow_trades":
                where = "WHERE status='closed' AND bundle_id IN (SELECT bundle_id FROM paper_candidates)"
                out[t] = _copy(src, dst, t, where=where)
            else:
                out[t] = _copy(src, dst, t)
    except Exception:
        dst.execute("ROLLBACK")
        raise
    dst.execute("ROLLBACK" if dry_run else "COMMIT")
    return out


def seed_files(
    source: Path, target: Path, groups: tuple[str, ...] = DEFAULT_GROUPS, *, dry_run: bool = False
) -> dict[str, int]:
    """Open `source` read-only and `target` (created and migrated if new), then `seed`."""
    source, target = Path(source), Path(target)
    if not source.exists():
        raise FileNotFoundError(f"source database {source} not found")
    if source.resolve() == target.resolve():
        raise ValueError("source and target are the same database")
    src = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    src.row_factory = sqlite3.Row
    dst = connect(target)
    try:
        migrate(dst)
        return seed(src, dst, groups, dry_run=dry_run)
    finally:
        src.close()
        dst.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--from", dest="source", required=True, type=Path)
    p.add_argument("--to", dest="target", required=True, type=Path)
    p.add_argument("--paper", action="store_true", help="also copy the paper tests")
    p.add_argument("--dry-run", action="store_true", help="count only; keep nothing")
    a = p.parse_args(argv)
    groups = DEFAULT_GROUPS + (("paper",) if a.paper else ())
    try:
        out = seed_files(a.source, a.target, groups, dry_run=a.dry_run)
    except (FileNotFoundError, ValueError) as e:
        print(f"not seeded: {e}")
        return 1
    verb = "would add" if a.dry_run else "added"
    for table, n in out.items():
        print(f"{table:18} {verb} {n:>7,} row(s)")
    print(f"account tables copied: 0 (never). Target: {a.target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""SQLite connection and forward-only migrations (D-024)."""

from __future__ import annotations

import sqlite3
import time
from importlib import resources
from pathlib import Path


def now_ms() -> int:
    return int(time.time() * 1000)


def connect(path: str | Path | None) -> sqlite3.Connection:
    """Open (or create) the database with WAL, foreign keys and a busy timeout.

    `None` or ':memory:' gives an in-memory database for tests.
    """
    target = ":memory:" if path in (None, ":memory:") else str(path)
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if target != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _migration_files() -> list[tuple[int, str]]:
    pkg = resources.files("goldtrader.db") / "migrations"
    out: list[tuple[int, str]] = []
    for entry in sorted(p.name for p in pkg.iterdir() if p.name.endswith(".sql")):
        version = int(entry.split("_", 1)[0])
        out.append((version, (pkg / entry).read_text(encoding="utf-8")))
    return out


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if row is None:
        return 0
    v = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    return int(v or 0)


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations in order; returns the resulting schema version."""
    have = current_version(conn)
    for version, sql in _migration_files():
        if version <= have:
            continue
        with conn:
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_version(version, applied_ms) VALUES (?, ?)", (version, now_ms())
            )
        have = version
    return have

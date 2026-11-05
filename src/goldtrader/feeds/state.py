"""`feed_state` rows (migration 007): conditional-request validators and the last outcome per feed.

Shared by :class:`goldtrader.feeds.news.NewsFetcher` and
:class:`goldtrader.feeds.calendar_store.CalendarRefresher` (D-038). Plain SQL, no ORM.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FeedState:
    """What we remember about one feed between polls."""

    name: str
    etag: str | None = None
    last_modified: str | None = None
    last_fetch_ms: int | None = None
    last_error: str | None = None


def load_feed_state(conn: sqlite3.Connection, name: str) -> FeedState:
    """The stored state for ``name`` (an empty :class:`FeedState` when never fetched)."""
    row = conn.execute(
        "SELECT name, etag, last_modified, last_fetch_ms, last_error FROM feed_state WHERE name=?",
        (name,),
    ).fetchone()
    if row is None:
        return FeedState(name=name)
    return FeedState(
        name=row[0],
        etag=row[1],
        last_modified=row[2],
        last_fetch_ms=row[3],
        last_error=row[4],
    )


def save_feed_state(
    conn: sqlite3.Connection,
    name: str,
    *,
    etag: str | None,
    last_modified: str | None,
    last_fetch_ms: int,
    last_error: str | None,
) -> None:
    """Upsert the row for ``name``. ``last_error`` None means the last attempt succeeded."""
    conn.execute(
        "INSERT INTO feed_state(name, etag, last_modified, last_fetch_ms, last_error) "
        "VALUES (?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET etag=excluded.etag, "
        "last_modified=excluded.last_modified, last_fetch_ms=excluded.last_fetch_ms, "
        "last_error=excluded.last_error",
        (name, etag, last_modified, last_fetch_ms, last_error),
    )


def failing_feeds(conn: sqlite3.Connection) -> list[FeedState]:
    """Feeds whose last attempt failed (health input, AGENTS §3.1 escalation)."""
    rows = conn.execute(
        "SELECT name, etag, last_modified, last_fetch_ms, last_error FROM feed_state "
        "WHERE last_error IS NOT NULL ORDER BY name"
    ).fetchall()
    return [
        FeedState(name=r[0], etag=r[1], last_modified=r[2], last_fetch_ms=r[3], last_error=r[4])
        for r in rows
    ]

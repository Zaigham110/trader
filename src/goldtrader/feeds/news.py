"""News feeds → ``news_items`` (D-038; ARCHITECTURE.md §2.2 ``news``; AGENTS.md §3.1 inputs).

* :class:`NewsStore` is the only writer of ``news_items`` (migration 005). ``processed_ms`` NULL
  means the Research bot has not seen the item yet; :meth:`NewsStore.mark_processed` stamps it.
* :class:`NewsFetcher` polls every enabled :class:`FeedConfig`, honouring the stored
  ``feed_state`` validators (ETag / Last-Modified) and ``FeedsConfig.max_item_age_hours``. A failing
  feed is recorded (``feed_state.last_error``) and never raises: one broken publisher must not stop
  the others or the brain's cycle (R-021 degrade gracefully).
* :func:`news_for_research` hands items to the Research bot in exactly the shape
  ``bots.research`` consumes (``id``, ``title``, ``source``, ``published_ms`` / ``published_at``,
  ``body`` ≤ 1,500 chars). The text is still raw here; ``wrap_untrusted`` defangs it (R-024).

Provenance (source, url, published and fetched times) is kept per item for the journal (R-030).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from goldtrader.config.schema import FeedsConfig
from goldtrader.feeds.rss import BODY_MAX_CHARS, FeedItem, FetchResult, fetch_feed, parse_feed
from goldtrader.feeds.state import load_feed_state, save_feed_state

HOUR_MS = 3_600_000

FetchFn = Callable[..., FetchResult]
"""``fetch(url, *, etag=None, last_modified=None, timeout_s=10) -> FetchResult``."""


def fmt_ms(ms: int) -> str:
    """Epoch ms → ``YYYY-MM-DD HH:MM UTC`` (the same rendering ``bots.common.fmt_ms`` uses)."""
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


class NewsStore:
    """Reads and writes ``news_items``. Plain SQL; the connection is shared with the brain."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.c = conn

    def upsert_items(self, items: Iterable[FeedItem], fetched_ms: int) -> int:
        """Insert new items (deduplicated by ``item_id``); existing rows are left untouched so the
        ``processed_ms`` flag survives a re-fetch. Items without a date get ``fetched_ms`` as their
        publication time. Returns the number of rows actually inserted."""
        new = 0
        with self.c:
            for it in items:
                cur = self.c.execute(
                    "INSERT OR IGNORE INTO news_items(item_id, source, title, url, published_ms, "
                    "body, fetched_ms, processed_ms) VALUES (?,?,?,?,?,?,?,NULL)",
                    (
                        it.item_id,
                        it.source,
                        it.title,
                        it.link or None,
                        it.published_ms if it.published_ms is not None else fetched_ms,
                        it.body[:BODY_MAX_CHARS],
                        fetched_ms,
                    ),
                )
                new += int(cur.rowcount or 0)
        return new

    def unprocessed(self, limit: int = 20) -> list[sqlite3.Row]:
        """Items the Research bot has not processed yet, newest first."""
        return list(
            self.c.execute(
                "SELECT * FROM news_items WHERE processed_ms IS NULL "
                "ORDER BY published_ms DESC, item_id LIMIT ?",
                (int(limit),),
            )
        )

    def mark_processed(self, item_ids: Iterable[str], now_ms: int) -> int:
        """Stamp ``processed_ms`` on ``item_ids`` (rows already stamped keep their first stamp)."""
        ids = [str(i) for i in item_ids]
        if not ids:
            return 0
        with self.c:
            cur = self.c.executemany(
                "UPDATE news_items SET processed_ms=? WHERE item_id=? AND processed_ms IS NULL",
                [(int(now_ms), i) for i in ids],
            )
        return int(cur.rowcount or 0)

    def recent(self, hours: float, now_ms: int, limit: int = 200) -> list[sqlite3.Row]:
        """Items published within the last ``hours`` (processed or not), newest first."""
        since = int(now_ms - hours * HOUR_MS)
        return list(
            self.c.execute(
                "SELECT * FROM news_items WHERE published_ms >= ? AND published_ms <= ? "
                "ORDER BY published_ms DESC, item_id LIMIT ?",
                (since, int(now_ms), int(limit)),
            )
        )

    def count(self) -> int:
        return int(self.c.execute("SELECT COUNT(*) FROM news_items").fetchone()[0])


class NewsFetcher:
    """Polls the configured feeds into a :class:`NewsStore`.

    ``fetch`` has :func:`goldtrader.feeds.rss.fetch_feed`'s keyword signature and is injected in
    tests. ``clock_ms`` supplies "now" so the age filter is deterministic.
    """

    def __init__(
        self,
        cfg: FeedsConfig,
        store: NewsStore,
        clock_ms: Callable[[], int],
        fetch: FetchFn = fetch_feed,
        *,
        timeout_s: float = 10,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.clock_ms = clock_ms
        self.fetch = fetch
        self.timeout_s = timeout_s

    def due(self, now_ms: int | None = None) -> bool:
        """True when the oldest enabled feed was last fetched more than ``poll_minutes`` ago."""
        now = self.clock_ms() if now_ms is None else now_ms
        interval = self.cfg.poll_minutes * 60_000
        for feed in self.cfg.news:
            if not feed.enabled:
                continue
            st = load_feed_state(self.store.c, feed.name)
            if st.last_fetch_ms is None or now - st.last_fetch_ms >= interval:
                return True
        return False

    def run_once(self) -> dict[str, tuple[int, str | None]]:
        """Fetch every enabled feed once. Returns ``{name: (new_items, error)}``; never raises."""
        results: dict[str, tuple[int, str | None]] = {}
        for feed in self.cfg.news:
            if not feed.enabled:
                continue
            results[feed.name] = self._run_feed(feed.name, feed.url)
        return results

    def _run_feed(self, name: str, url: str) -> tuple[int, str | None]:
        now = self.clock_ms()
        state = load_feed_state(self.store.c, name)
        try:
            res = self.fetch(
                url, etag=state.etag, last_modified=state.last_modified, timeout_s=self.timeout_s
            )
        except Exception as e:  # a custom fetch that raises is still isolated here
            res = FetchResult(0, "", error=f"{type(e).__name__}: {e}")
        new = 0
        error: str | None = None
        etag, last_modified = state.etag, state.last_modified
        if res.unchanged:
            pass
        elif not res.ok:
            error = res.error or f"http {res.status}"
        else:
            items, parse_error = parse_feed(res.text, name)
            if parse_error is not None:
                error = parse_error
            else:
                fresh = self._filter_age(items, now)
                try:
                    new = self.store.upsert_items(fresh, now)
                except sqlite3.Error as e:
                    error = f"db error: {e}"
                else:
                    etag, last_modified = res.etag, res.last_modified
        save_feed_state(
            self.store.c,
            name,
            etag=etag,
            last_modified=last_modified,
            last_fetch_ms=now,
            last_error=error,
        )
        return new, error

    def _filter_age(self, items: list[FeedItem], now_ms: int) -> list[FeedItem]:
        """Drop items older than ``max_item_age_hours``; undated items are kept (dated at fetch)."""
        oldest = now_ms - self.cfg.max_item_age_hours * HOUR_MS
        return [it for it in items if it.published_ms is None or it.published_ms >= oldest]


def news_for_research(
    store: NewsStore, limit: int = 20, *, max_body_chars: int = BODY_MAX_CHARS
) -> list[dict[str, Any]]:
    """Unprocessed items as the Research bot's ``BotContext.news_items`` (AGENTS §3.1).

    Each dict carries ``id`` and ``item_id`` (same value; ``bots.research.input_ids`` reads either),
    ``title``, ``source``, ``published_ms``, ``published_at`` (``YYYY-MM-DD HH:MM UTC``), ``url`` and
    ``body`` ≤ ``max_body_chars``. The caller marks them processed after the run.
    """
    out: list[dict[str, Any]] = []
    for row in store.unprocessed(limit):
        published_ms = int(row["published_ms"])
        body = row["body"] or ""
        if len(body) > max_body_chars:
            body = body[: max_body_chars - 1].rstrip() + "…"
        out.append(
            {
                "id": row["item_id"],
                "item_id": row["item_id"],
                "title": row["title"],
                "source": row["source"],
                "published_ms": published_ms,
                "published_at": fmt_ms(published_ms),
                "url": row["url"],
                "body": body,
            }
        )
    return out

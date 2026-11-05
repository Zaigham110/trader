"""NewsStore / NewsFetcher (D-038, R-021): dedupe, age filter, 304 handling, error isolation, and
the Research bot input shape (AGENTS §3.1)."""

from __future__ import annotations

import sqlite3

from goldtrader.bots.common import wrap_untrusted
from goldtrader.bots.research import input_ids
from goldtrader.config.schema import FeedConfig, FeedsConfig
from goldtrader.db import connect, migrate
from goldtrader.feeds.news import NewsFetcher, NewsStore, news_for_research
from goldtrader.feeds.rss import FeedItem, FetchResult
from goldtrader.feeds.state import failing_feeds, load_feed_state

NOW = 1_793_887_200_000  # 2026-11-05 14:00 UTC
HOUR = 3_600_000


def _db() -> sqlite3.Connection:
    conn = connect(None)
    assert migrate(conn) >= 7
    return conn


def _item(i: int, published_ms: int | None = NOW - HOUR, source: str = "fed_press") -> FeedItem:
    return FeedItem(
        item_id=f"{source}:n{i}",
        source=source,
        title=f"Title {i}",
        link=f"https://example.test/{i}",
        published_ms=published_ms,
        body=f"Body {i}",
    )


def _rss(*items: tuple[str, str]) -> str:
    body = "".join(
        f"<item><title>{title}</title><link>https://x.test/{guid}</link><guid>{guid}</guid>"
        f"<pubDate>{date}</pubDate><description>d</description></item>"
        for guid, (title, date) in ((g, (t, d)) for g, t, d in items)
    )
    return f'<rss version="2.0"><channel><title>t</title>{body}</channel></rss>'


def test_store_dedupes_and_keeps_processed_flag():
    conn = _db()
    store = NewsStore(conn)
    assert store.upsert_items([_item(1), _item(2)], NOW) == 2
    assert store.upsert_items([_item(1), _item(3)], NOW + 1) == 1  # n1 is a duplicate
    assert store.count() == 3
    assert [r["item_id"] for r in store.unprocessed(10)] == [
        "fed_press:n1",
        "fed_press:n2",
        "fed_press:n3",
    ]
    assert store.mark_processed(["fed_press:n1"], NOW) == 1
    assert store.mark_processed(["fed_press:n1"], NOW + 5) == 0  # first stamp is kept
    assert (
        conn.execute("SELECT processed_ms FROM news_items WHERE item_id='fed_press:n1'").fetchone()[
            0
        ]
        == NOW
    )
    assert [r["item_id"] for r in store.unprocessed(10)] == ["fed_press:n2", "fed_press:n3"]
    # a re-fetch of n1 does not reset processed_ms
    store.upsert_items([_item(1)], NOW + 10)
    assert (
        conn.execute("SELECT processed_ms FROM news_items WHERE item_id='fed_press:n1'").fetchone()[
            0
        ]
        == NOW
    )
    # undated items are dated at fetch time
    store.upsert_items([_item(9, published_ms=None)], NOW + 20)
    assert (
        conn.execute("SELECT published_ms FROM news_items WHERE item_id='fed_press:n9'").fetchone()[
            0
        ]
        == NOW + 20
    )
    assert [r["item_id"] for r in store.recent(2, NOW + 20)] == [
        "fed_press:n9",
        "fed_press:n1",
        "fed_press:n2",
        "fed_press:n3",
    ]
    assert store.recent(0.5, NOW + 20) and len(store.recent(0.5, NOW + 20)) == 1


def _cfg(**over) -> FeedsConfig:
    base = dict(
        news=(
            FeedConfig(name="fed_press", url="https://fed.test/feed"),
            FeedConfig(name="bls_latest", url="https://bls.test/feed"),
            FeedConfig(name="disabled", url="https://off.test/feed", enabled=False),
        ),
        max_item_age_hours=48,
    )
    base.update(over)
    return FeedsConfig(**base)


def test_fetcher_filters_old_items_uses_validators_and_isolates_errors():
    conn = _db()
    store = NewsStore(conn)
    calls: list[tuple[str, str | None, str | None]] = []
    fresh = "Thu, 05 Nov 2026 13:00:00 GMT"
    stale = "Sun, 01 Nov 2026 13:00:00 GMT"  # > 48 h old

    def fetch(url, *, etag=None, last_modified=None, timeout_s=10):
        calls.append((url, etag, last_modified))
        if "fed" in url:
            if etag == '"v1"':
                return FetchResult(304, "", etag=etag, last_modified=last_modified)
            return FetchResult(
                200,
                _rss(("a", "Fresh A", fresh), ("b", "Stale B", stale), ("c", "No date", "junk")),
                etag='"v1"',
                last_modified="Thu, 05 Nov 2026 13:00:00 GMT",
            )
        return FetchResult(0, "", error="url error: connection refused")

    fetcher = NewsFetcher(_cfg(), store, lambda: NOW, fetch=fetch)
    assert fetcher.due()
    res = fetcher.run_once()
    assert set(res) == {"fed_press", "bls_latest"}  # the disabled feed is never fetched
    assert res["fed_press"] == (2, None)  # a + undated c; stale b dropped
    assert res["bls_latest"][0] == 0 and "connection refused" in res["bls_latest"][1]
    ids = {r["item_id"] for r in store.unprocessed(10)}
    assert ids == {"fed_press:a", "fed_press:c"}
    st = load_feed_state(conn, "fed_press")
    assert st.etag == '"v1"' and st.last_fetch_ms == NOW and st.last_error is None
    bad = load_feed_state(conn, "bls_latest")
    assert bad.last_error and bad.etag is None
    assert [f.name for f in failing_feeds(conn)] == ["bls_latest"]
    assert not fetcher.due(NOW + 60_000) and fetcher.due(NOW + 11 * 60_000)

    # second poll: validators are sent, 304 keeps everything, nothing new
    res2 = fetcher.run_once()
    assert res2["fed_press"] == (0, None)
    assert calls[2] == ("https://fed.test/feed", '"v1"', "Thu, 05 Nov 2026 13:00:00 GMT")
    assert store.count() == 2


def test_fetcher_survives_raising_fetch_and_malformed_xml():
    conn = _db()
    store = NewsStore(conn)

    def fetch(url, **kw):
        if "fed" in url:
            raise RuntimeError("socket exploded")
        return FetchResult(200, "<rss><channel><item>", etag=None, last_modified=None)

    res = NewsFetcher(_cfg(), store, lambda: NOW, fetch=fetch).run_once()
    assert res["fed_press"][0] == 0 and "socket exploded" in res["fed_press"][1]
    assert res["bls_latest"][0] == 0 and "parse error" in res["bls_latest"][1]
    assert load_feed_state(conn, "bls_latest").last_error.startswith("xml parse error")
    assert store.count() == 0


def test_news_for_research_matches_bot_input_shape():
    conn = _db()
    store = NewsStore(conn)
    store.upsert_items([_item(1), _item(2, published_ms=NOW - 2 * HOUR, source="bls_latest")], NOW)
    conn.execute("UPDATE news_items SET body=? WHERE item_id='fed_press:n1'", ("x" * 2000,))
    items = news_for_research(store, limit=20)
    assert [it["id"] for it in items] == ["fed_press:n1", "bls_latest:n2"]  # newest first
    assert input_ids(items) == ["fed_press:n1", "bls_latest:n2"]
    first = items[0]
    assert first["item_id"] == first["id"] and first["source"] == "fed_press"
    assert first["title"] == "Title 1" and first["url"] == "https://example.test/1"
    assert first["published_ms"] == NOW - HOUR
    assert first["published_at"] == "2026-11-05 13:00 UTC"
    assert len(first["body"]) <= 1500
    text = wrap_untrusted(items)
    assert text.count("<untrusted_news id=") == 2
    assert 'id="fed_press:n1"' in text and 'published_at="2026-11-05 13:00 UTC"' in text
    assert news_for_research(store, limit=1)[0]["id"] == "fed_press:n1"
    store.mark_processed([i["id"] for i in items], NOW)
    assert news_for_research(store) == []

"""CalendarStore / CalendarRefresher (D-038; R-006 inputs): upsert, lookups, gateway window, and a
refresh from a fake BLS feed plus the committed FOMC dates and LBMA fixes."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from goldtrader.config.schema import FeedsConfig
from goldtrader.db import connect, migrate
from goldtrader.feeds.calendar_store import CalendarRefresher, CalendarStore
from goldtrader.feeds.rss import FetchResult
from goldtrader.feeds.state import load_feed_state
from goldtrader.session.calendar import Event, parse_bls_ics

NOW = 1_793_887_200_000  # Thu 2026-11-05 14:00 UTC
HOUR = 3_600_000

BLS_ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:bls-2026-11-06-empsit
DTSTART;TZID=America/New_York:20261106T083000
SUMMARY:Employment Situation for October 2026
END:VEVENT
BEGIN:VEVENT
UID:bls-2026-11-10-cpi
DTSTART;TZID=America/New_York:20261110T083000
SUMMARY:Consumer Price Index for October 2026
END:VEVENT
BEGIN:VEVENT
UID:bls-2026-11-13-ppi
DTSTART;TZID=America/New_York:20261113T083000
SUMMARY:Producer Price Index for October 2026
END:VEVENT
END:VCALENDAR
"""

FF_JSON = """[
 {"title": "ISM Manufacturing PMI", "country": "USD", "date": "2026-11-05T10:00:00-05:00",
  "impact": "High", "forecast": "49.5", "previous": "49.1"},
 {"title": "Bank Holiday", "country": "JPY", "date": "2026-11-05T00:00:00-05:00",
  "impact": "Holiday", "forecast": "", "previous": ""}
]"""


def _db() -> sqlite3.Connection:
    conn = connect(None)
    assert migrate(conn) >= 7
    return conn


def _ev(ms: int, title: str, impact: str = "medium", **over) -> Event:
    base = dict(
        time_utc=datetime.fromtimestamp(ms / 1000, tz=UTC),
        title=title,
        country="US",
        impact=impact,
        source="test",
        actual=None,
        forecast=None,
        previous=None,
        event_id=f"test:{title}",
    )
    base.update(over)
    return Event(**base)


def test_store_upsert_dedupe_and_lookups():
    conn = _db()
    store = CalendarStore(conn)
    evs = [
        _ev(NOW - 30 * HOUR, "yesterday", "high"),
        _ev(NOW + 2 * HOUR, "soon medium"),
        _ev(NOW + 5 * HOUR, "soon high", "high", forecast=1.0),
        _ev(NOW + 30 * HOUR, "tomorrow high", "high"),
        _ev(NOW + 70 * HOUR, "far"),
    ]
    assert store.upsert_events(evs, NOW) == 5
    # re-upsert: same ids are not new; actual fills in, forecast is kept when the refetch lacks it
    again = _ev(NOW + 5 * HOUR, "soon high", "high", actual=2.0)
    assert store.upsert_events([again, evs[1]], NOW + 1) == 0
    assert store.count() == 5
    row = conn.execute(
        "SELECT actual, forecast FROM events WHERE event_id='test:soon high'"
    ).fetchone()
    assert (row[0], row[1]) == (2.0, 1.0)

    up = store.upcoming(NOW, hours=48)
    assert [e.title for e in up] == ["soon medium", "soon high", "tomorrow high"]
    assert all(e.time_utc.tzinfo is not None for e in up)
    nxt = store.next_high_impact(NOW)
    assert nxt is not None and nxt[0].title == "soon high" and nxt[1] == 300.0
    assert nxt[0].actual == 2.0 and nxt[0].forecast == 1.0
    assert store.next_high_impact(NOW + 100 * HOUR) is None
    assert (
        store.next_high_impact(NOW + 5 * HOUR)[1] == 0.0
    )  # at the release instant it still counts

    # gateway window: UTC day of NOW ± 1 day → [Nov 4 00:00, Nov 7 00:00)
    gw = store.events_for_gateway(NOW)
    assert [e.title for e in gw] == ["yesterday", "soon medium", "soon high", "tomorrow high"]
    fn = store.events_fn(lambda: NOW + 40 * HOUR)
    assert [e.title for e in fn()] == ["tomorrow high", "far"]
    assert store.purge_before(NOW) == 1 and store.count() == 4


def test_refresher_pulls_bls_fomc_fixes_and_optional_forexfactory(tmp_path):
    conn = _db()
    store = CalendarStore(conn)
    calls: list[tuple[str, str | None]] = []

    def fetch(url, *, etag=None, last_modified=None, timeout_s=10):
        calls.append((url, etag))
        if url.endswith("bls.ics"):
            if etag == '"ics1"':
                return FetchResult(304, "", etag=etag, last_modified=last_modified)
            return FetchResult(200, BLS_ICS, etag='"ics1"', last_modified=None)
        if url.endswith(".json"):
            return FetchResult(200, FF_JSON)
        raise AssertionError(url)

    fomc = tmp_path / "fomc.yaml"
    fomc.write_text(
        "timezone: America/New_York\ndecision_time: '14:00'\nmeetings:\n"
        "  - {start: 2026-12-08, end: 2026-12-09, sep: true}\n"
    )
    cfg = FeedsConfig(bls_ics_url="https://bls.test/bls.ics")
    ref = CalendarRefresher(cfg, store, lambda: NOW, fetch=fetch, fomc_yaml_path=fomc)
    assert ref.due()
    res = ref.run_once()
    assert res["bls"] == (3, None)
    assert res["fomc"] == (1, None)
    assert res["lbma"] == (4, None)  # Thu + Fri × 2 fixes; Sat 2026-11-07 has no auction
    assert "forexfactory" not in res  # not configured (KU-013)
    assert store.count() == 3 + 1 + 4

    titles = {e.event_id for e in store.upcoming(NOW, hours=24 * 40)}
    assert "bls:bls-2026-11-06-empsit" in titles and "fomc:2026-12-09" in titles
    assert any(t.startswith("lbma:2026-11-05:") for t in titles)
    nxt = store.next_high_impact(NOW)
    assert nxt is not None and nxt[0].event_id == "bls:bls-2026-11-06-empsit"
    assert nxt[1] == (parse_bls_ics(BLS_ICS)[0].time_utc.timestamp() * 1000 - NOW) / 60_000
    st = load_feed_state(conn, "bls")
    assert st.etag == '"ics1"' and st.last_fetch_ms == NOW and st.last_error is None
    assert not ref.due(NOW + HOUR) and ref.due(NOW + 13 * HOUR)

    # second run: 304 for BLS, FOMC/fixes already known → nothing new, no errors
    res2 = ref.run_once()
    assert res2 == {"bls": (0, None), "fomc": (0, None), "lbma": (0, None)}
    assert calls[-1] == ("https://bls.test/bls.ics", '"ics1"')

    # owner enables ForexFactory → pulled, deduped by id, actual/forecast parsed
    cfg2 = FeedsConfig(
        bls_ics_url="https://bls.test/bls.ics", forexfactory_json_url="https://ff.test/week.json"
    )
    ref2 = CalendarRefresher(cfg2, store, lambda: NOW, fetch=fetch, fomc_yaml_path=fomc)
    res3 = ref2.run_once()
    assert res3["forexfactory"] == (2, None)
    ism = [e for e in store.upcoming(NOW, 24) if e.title == "ISM Manufacturing PMI"][0]
    assert ism.impact == "high" and ism.forecast == 49.5 and ism.source == "forexfactory"
    assert ref2.run_once()["forexfactory"] == (0, None)


def test_refresher_records_errors_without_raising(tmp_path):
    conn = _db()
    store = CalendarStore(conn)

    def fetch(url, **kw):
        if url.endswith(".json"):
            return FetchResult(200, "{not json")
        return FetchResult(503, "", error="http 503: unavailable")

    cfg = FeedsConfig(forexfactory_json_url="https://ff.test/x.json")
    missing_yaml = tmp_path / "nope.yaml"
    res = CalendarRefresher(
        cfg, store, lambda: NOW, fetch=fetch, fomc_yaml_path=missing_yaml
    ).run_once()
    assert res["bls"][0] == 0 and "503" in res["bls"][1]
    assert res["fomc"][0] == 0 and "fomc yaml" in res["fomc"][1]
    assert res["lbma"][1] is None  # pure code, always works
    assert res["forexfactory"][0] == 0 and "parse error" in res["forexfactory"][1]
    assert load_feed_state(conn, "bls").last_error.startswith("http 503")
    assert load_feed_state(conn, "forexfactory").last_error.startswith("parse error")

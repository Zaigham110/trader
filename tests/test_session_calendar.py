"""Calendar tests (R-006): BLS iCal, ForexFactory JSON, FOMC YAML, LBMA fix times, windows."""

from datetime import UTC, date, datetime, timedelta

import pytest

from goldtrader.config.schema import EventWindow, SessionConfig
from goldtrader.session.calendar import (
    FOMC_YAML_PATH,
    Event,
    EventWindows,
    dedupe_events,
    fix_events,
    load_bundled_fomc,
    load_fomc_dates,
    parse_bls_ics,
    parse_forexfactory_json,
    to_float,
)
from goldtrader.types import SessionState


def utc(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=UTC)


def ev(when, title="x", impact="high", **kw):
    base = {
        "country": "US",
        "source": "t",
        "actual": None,
        "forecast": None,
        "previous": None,
        "event_id": f"t:{title}:{when.isoformat()}",
    }
    base.update(kw)
    return Event(time_utc=when, title=title, impact=impact, **base)


BLS_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//BLS//Schedule//EN
BEGIN:VTIMEZONE
TZID:America/New_York
END:VTIMEZONE
BEGIN:VEVENT
UID:bls-2026-11-06-empsit
DTSTART;TZID=America/New_York:20261106T083000
DTEND;TZID=America/New_York:20261106T083000
SUMMARY:Employment Situation for October 2026
DESCRIPTION:Monthly release\\, 8:30 a.m. (ET)
END:VEVENT
BEGIN:VEVENT
UID:bls-2026-11-10-cpi
DTSTART;TZID=America/New_York:20261110T083000
SUMMARY:Consumer Price Index for October 2026
END:VEVENT
BEGIN:VEVENT
UID:bls-2026-11-13-ppi
DTSTART;TZID=America/New_York:20261113T083000
SUMMARY:Producer Price Index for Octo
 ber 2026
END:VEVENT
BEGIN:VEVENT
UID:bls-2026-07-15-realearn
DTSTART;TZID=America/New_York:20260715T083000
SUMMARY:Real Earnings for June 2026
END:VEVENT
BEGIN:VEVENT
UID:bls-utc
DTSTART:20261120T133000Z
SUMMARY:State Employment and Unemployment for October 2026
END:VEVENT
BEGIN:VEVENT
UID:bls-allday
DTSTART;VALUE=DATE:20261126
SUMMARY:Thanksgiving Day (BLS closed)
END:VEVENT
END:VCALENDAR
"""


def test_parse_bls_ics_times_and_impact():
    events = parse_bls_ics(BLS_ICS)
    by_title = {e.title: e for e in events}
    nfp = by_title["Employment Situation for October 2026"]
    assert nfp.time_utc == utc(2026, 11, 6, 13, 30)  # EST: 08:30 ET = 13:30 UTC
    assert nfp.impact == "high" and nfp.country == "US" and nfp.source == "bls"
    assert nfp.event_id == "bls:bls-2026-11-06-empsit"
    assert by_title["Consumer Price Index for October 2026"].impact == "high"
    ppi = by_title["Producer Price Index for October 2026"]  # folded line reassembled
    assert ppi.impact == "medium"
    real = by_title["Real Earnings for June 2026"]
    assert real.time_utc == utc(2026, 7, 15, 12, 30)  # EDT: 08:30 ET = 12:30 UTC
    assert by_title["State Employment and Unemployment for October 2026"].time_utc == utc(
        2026, 11, 20, 13, 30
    )
    assert "Thanksgiving Day (BLS closed)" not in by_title  # all-day entries skipped
    assert events == sorted(events, key=lambda e: e.time_utc)


def test_parse_bls_ics_accepts_bytes_and_crlf():
    events = parse_bls_ics(BLS_ICS.replace("\n", "\r\n").encode())
    assert len(events) == 5


FF_JSON = """[
  {"title": "Non-Farm Employment Change", "country": "USD",
   "date": "2026-11-06T08:30:00-05:00", "impact": "High",
   "forecast": "50K", "previous": "22K"},
  {"title": "Unemployment Rate", "country": "USD",
   "date": "2026-11-06T08:30:00-05:00", "impact": "High",
   "forecast": "4.4%", "previous": "4.3%", "actual": "4.5%"},
  {"title": "Bank Holiday", "country": "JPY",
   "date": "2026-11-03T00:00:00-05:00", "impact": "Holiday", "forecast": "", "previous": ""},
  {"title": "Core CPI m/m", "country": "USD",
   "date": "2026-07-14T08:30:00-04:00", "impact": "Medium", "forecast": "0.3%", "previous": "0.2%"},
  {"title": "Non-Farm Employment Change", "country": "USD",
   "date": "2026-11-06T08:30:00-05:00", "impact": "High", "forecast": "50K", "previous": "22K"},
  {"title": "Speech", "country": "EUR", "date": "", "impact": "Low"}
]"""


def test_parse_forexfactory_json():
    events = parse_forexfactory_json(FF_JSON)
    assert len(events) == 4  # duplicate NFP + no-date dropped
    nfp = next(e for e in events if e.title == "Non-Farm Employment Change")
    assert nfp.time_utc == utc(2026, 11, 6, 13, 30)  # -05:00 → UTC
    assert nfp.impact == "high" and nfp.country == "USD" and nfp.source == "forexfactory"
    assert nfp.forecast == 50_000.0 and nfp.previous == 22_000.0 and nfp.actual is None
    ur = next(e for e in events if e.title == "Unemployment Rate")
    assert ur.actual == 4.5 and ur.forecast == 4.4
    cpi = next(e for e in events if e.title == "Core CPI m/m")
    assert cpi.time_utc == utc(2026, 7, 14, 12, 30) and cpi.impact == "medium"
    hol = next(e for e in events if e.title == "Bank Holiday")
    assert hol.impact == "holiday" and hol.forecast is None


def test_to_float():
    assert to_float("0.3%") == 0.3
    assert to_float("-50K") == -50_000.0
    assert to_float("1,234.5") == 1234.5
    assert to_float("2.1B") == 2.1e9
    assert to_float("") is None and to_float(None) is None and to_float("n/a") is None
    assert to_float(3) == 3.0


def test_load_fomc_dates_bundled_file():
    events = load_bundled_fomc()
    assert FOMC_YAML_PATH.exists()
    assert len(events) == 8
    assert all(e.impact == "high" and e.source == "fomc" for e in events)
    jan = events[0]
    assert jan.time_utc == utc(2026, 1, 28, 19, 0)  # 14:00 EST
    jun = next(e for e in events if e.event_id == "fomc:2026-06-17")
    assert jun.time_utc == utc(2026, 6, 17, 18, 0)  # 14:00 EDT
    assert "(SEP)" in jun.title
    assert events[-1].event_id == "fomc:2026-12-09"
    assert "2027" in FOMC_YAML_PATH.read_text()


def test_load_fomc_dates_bare_list_and_time_override():
    text = """
meetings:
  - 2026-03-18
  - {end: 2026-04-29, time: "14:30"}
  - 2026-09-16T18:00:00+00:00
"""
    events = load_fomc_dates(text)
    assert [e.time_utc for e in events] == [
        utc(2026, 3, 18, 18, 0),
        utc(2026, 4, 29, 18, 30),
        utc(2026, 9, 16, 18, 0),
    ]


def test_fix_events_london_to_utc():
    jan = fix_events(date(2026, 1, 15))
    assert [e.time_utc for e in jan] == [utc(2026, 1, 15, 10, 30), utc(2026, 1, 15, 15, 0)]
    jul = fix_events(utc(2026, 7, 15, 3))
    assert [e.time_utc for e in jul] == [utc(2026, 7, 15, 9, 30), utc(2026, 7, 15, 14, 0)]
    assert all(e.impact == "fix" and e.source == "lbma" for e in jan)
    assert "AM" in jan[0].title and "PM" in jan[1].title
    assert fix_events(date(2026, 1, 17)) == [] and fix_events(date(2026, 1, 18)) == []
    assert len(fix_events(date(2026, 1, 15), ("15:00",))) == 1


def test_dedupe_events():
    a = ev(utc(2026, 1, 1, 12), "NFP", source="bls")
    b = ev(utc(2026, 1, 1, 12), "nfp ", source="ff")
    c = ev(utc(2026, 1, 1, 11), "CPI")
    out = dedupe_events([a, b, c])
    assert out == [c, a]


def test_event_requires_aware_time_and_valid_impact():
    with pytest.raises(ValueError):
        ev(datetime(2026, 1, 1, 12))
    with pytest.raises(ValueError):
        ev(utc(2026, 1, 1, 12), impact="huge")


@pytest.fixture
def windows():
    return EventWindows(
        SessionConfig(
            news_high=EventWindow(before_min=30, after_min=15),
            fix_window=EventWindow(before_min=3, after_min=3),
        )
    )


def test_event_windows_news(windows):
    nfp = ev(utc(2026, 11, 6, 13, 30), "NFP", "high")
    med = ev(utc(2026, 11, 6, 15, 0), "Medium thing", "medium")
    assert windows.active(utc(2026, 11, 6, 13, 0), [nfp]) == (SessionState.NEWS_WINDOW, nfp)
    assert windows.active(utc(2026, 11, 6, 13, 45), [nfp]) == (SessionState.NEWS_WINDOW, nfp)
    assert windows.active(utc(2026, 11, 6, 12, 59, 59), [nfp]) == (None, None)
    assert windows.active(utc(2026, 11, 6, 13, 45, 1), [nfp]) == (None, None)
    assert windows.active(utc(2026, 11, 6, 14, 50), [med]) == (None, None)


def test_event_windows_fix_and_precedence(windows):
    fixes = fix_events(date(2026, 1, 15))
    pm = fixes[1]
    assert windows.active(utc(2026, 1, 15, 14, 57), fixes) == (SessionState.FIX_WINDOW, pm)
    assert windows.active(utc(2026, 1, 15, 15, 3), fixes) == (SessionState.FIX_WINDOW, pm)
    assert windows.active(utc(2026, 1, 15, 15, 3, 1), fixes) == (None, None)
    news = ev(utc(2026, 1, 15, 15, 0), "FOMC", "high")
    state, hit = windows.active(utc(2026, 1, 15, 14, 58), fixes + [news])
    assert state is SessionState.NEWS_WINDOW and hit is news


def test_event_windows_nearest_event_wins(windows):
    a = ev(utc(2026, 1, 15, 13, 30), "A", "high")
    b = ev(utc(2026, 1, 15, 13, 40), "B", "high")
    assert windows.active(utc(2026, 1, 15, 13, 38), [a, b])[1] is b
    assert windows.active(utc(2026, 1, 15, 13, 10), [a, b])[1] is a


def test_fixes_around_covers_neighbouring_days(windows):
    fx = windows.fixes_around(utc(2026, 1, 15, 0, 5))
    days = {e.time_utc.date() for e in fx}
    assert days == {date(2026, 1, 14), date(2026, 1, 15), date(2026, 1, 16)}
    assert utc(2026, 1, 15, 15) in {e.time_utc for e in fx}
    assert utc(2026, 1, 15, 15) - timedelta(days=1) in {e.time_utc for e in fx}

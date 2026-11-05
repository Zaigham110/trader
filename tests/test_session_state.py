"""SessionClock precedence tests (R-002, R-006): broker override, schedule, stale, buffer,
news / fix windows, trade_only_hours."""
import logging
from datetime import UTC, datetime

import pytest

from goldtrader.config.schema import EventWindow, SessionConfig
from goldtrader.mt5 import constants as K
from goldtrader.session.calendar import Event
from goldtrader.session.state import SessionClock, SessionDecision
from goldtrader.types import SessionState as S


def utc(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=UTC)


def high(when, title="NFP"):
    return Event(time_utc=when, title=title, country="US", impact="high", source="t",
                 actual=None, forecast=None, previous=None, event_id=f"t:{title}")


# Wed 2026-01-14 (winter) is the reference day. Open 00:00-21:58, break 21:58-23:00:30,
# close-only 23:00:30-23:02, open 23:02. Fixes: 10:30 and 15:00 UTC (GMT in January).
WED_OPEN = utc(2026, 1, 14, 12, 0)
WED_BREAK = utc(2026, 1, 14, 22, 30)
WED_CO = utc(2026, 1, 14, 23, 1)


@pytest.fixture
def clock():
    return SessionClock(SessionConfig())


def test_plain_open(clock):
    d = clock.state_at(WED_OPEN)
    assert isinstance(d, SessionDecision)
    assert d.state is S.OPEN and d.reason == "open" and d.entries_allowed
    assert d.seconds_to_close == (9 * 60 + 58) * 60
    assert d.next_transition == utc(2026, 1, 14, 21, 58)


def test_schedule_closed_and_close_only(clock):
    d = clock.state_at(WED_BREAK)
    assert d.state is S.CLOSED and d.reason == "schedule_closed" and not d.entries_allowed
    assert d.seconds_to_close is None and d.next_transition == utc(2026, 1, 14, 23, 0, 30)
    d = clock.state_at(WED_CO)
    assert d.state is S.CLOSE_ONLY and d.reason == "schedule_close_only"
    assert d.next_transition == utc(2026, 1, 14, 23, 2)


def test_naive_rejected(clock):
    with pytest.raises(ValueError, match="aware"):
        clock.state_at(datetime(2026, 1, 14, 12))


# -- broker precedence ----------------------------------------------------------------------------

def test_broker_disabled_overrides_open_schedule(clock, caplog):
    with caplog.at_level(logging.WARNING, logger="goldtrader.session.state"):
        d = clock.state_at(WED_OPEN, trade_mode=K.SYMBOL_TRADE_MODE_DISABLED)
    assert d.state is S.CLOSED and d.reason == "broker_overrides_schedule:disabled"
    assert not d.entries_allowed
    assert "DISABLED while schedule says OPEN" in caplog.text


def test_broker_disabled_agreeing_with_schedule(clock):
    d = clock.state_at(WED_BREAK, trade_mode=K.SYMBOL_TRADE_MODE_DISABLED)
    assert d.state is S.CLOSED and d.reason == "broker_disabled"


def test_broker_close_only_overrides_open_schedule(clock):
    d = clock.state_at(WED_OPEN, trade_mode=K.SYMBOL_TRADE_MODE_CLOSEONLY)
    assert d.state is S.CLOSE_ONLY and d.reason == "broker_overrides_schedule:close_only"
    d = clock.state_at(WED_CO, trade_mode=K.SYMBOL_TRADE_MODE_CLOSEONLY)
    assert d.state is S.CLOSE_ONLY and d.reason == "broker_close_only"


def test_broker_full_defers_to_closed_schedule(clock, caplog):
    with caplog.at_level(logging.WARNING, logger="goldtrader.session.state"):
        d = clock.state_at(WED_BREAK, trade_mode=K.SYMBOL_TRADE_MODE_FULL)
    assert d.state is S.CLOSED and d.reason == "schedule_closed"
    assert "schedule wins" in caplog.text


def test_broker_full_open_continues(clock):
    d = clock.state_at(WED_OPEN, trade_mode=K.SYMBOL_TRADE_MODE_FULL, last_tick_age_s=0.2)
    assert d.state is S.OPEN and d.entries_allowed


def test_broker_long_short_only(clock):
    d = clock.state_at(WED_OPEN, trade_mode=K.SYMBOL_TRADE_MODE_LONGONLY)
    assert d.state is S.OPEN and d.reason == "broker_long_only" and d.entries_allowed
    d = clock.state_at(WED_OPEN, trade_mode=K.SYMBOL_TRADE_MODE_SHORTONLY)
    assert d.state is S.OPEN and d.reason == "broker_short_only"
    # still subject to the schedule
    d = clock.state_at(WED_BREAK, trade_mode=K.SYMBOL_TRADE_MODE_LONGONLY)
    assert d.state is S.CLOSED


def test_unknown_trade_mode_fails_closed(clock):
    d = clock.state_at(WED_OPEN, trade_mode=99)
    assert d.state is S.CLOSED and d.reason == "broker_trade_mode_unknown"


# -- stale / buffer -------------------------------------------------------------------------------

def test_stale_ticks(clock):
    d = clock.state_at(WED_OPEN, trade_mode=K.SYMBOL_TRADE_MODE_FULL, last_tick_age_s=10.5)
    assert d.state is S.STALE and d.reason.startswith("stale_ticks:")
    assert not d.entries_allowed
    d = clock.state_at(WED_OPEN, last_tick_age_s=10.0)   # equal to threshold is fresh
    assert d.state is S.OPEN


def test_stale_not_reported_when_schedule_closed(clock):
    d = clock.state_at(WED_BREAK, last_tick_age_s=500)
    assert d.state is S.CLOSED


def test_pre_break_buffer(clock):
    d = clock.state_at(utc(2026, 1, 14, 21, 44))          # 14 min to the 21:58 break
    assert d.state is S.PRE_BREAK_BUFFER and d.reason == "pre_break_buffer:840s"
    assert d.seconds_to_close == 840 and not d.entries_allowed
    d = clock.state_at(utc(2026, 1, 14, 21, 43))          # exactly 15 min → inside (<=)
    assert d.state is S.PRE_BREAK_BUFFER
    d = clock.state_at(utc(2026, 1, 14, 21, 42, 59))
    assert d.state is S.OPEN


def test_pre_break_buffer_summer_and_friday(clock):
    assert clock.state_at(utc(2026, 7, 15, 20, 50)).state is S.PRE_BREAK_BUFFER
    assert clock.state_at(utc(2026, 7, 15, 21, 50)).state is S.CLOSED
    assert clock.state_at(utc(2026, 1, 16, 21, 50)).state is S.PRE_BREAK_BUFFER


def test_stale_beats_buffer(clock):
    d = clock.state_at(utc(2026, 1, 14, 21, 50), last_tick_age_s=60)
    assert d.state is S.STALE


# -- event windows --------------------------------------------------------------------------------

def test_news_window(clock):
    nfp = high(utc(2026, 1, 14, 13, 30))
    d = clock.state_at(utc(2026, 1, 14, 13, 5), events=[nfp])
    assert d.state is S.NEWS_WINDOW and d.reason == "news:NFP" and not d.entries_allowed
    assert clock.state_at(utc(2026, 1, 14, 13, 46), events=[nfp]).state is S.OPEN
    assert clock.state_at(utc(2026, 1, 14, 13, 45), events=[nfp]).state is S.NEWS_WINDOW


def test_fix_window_is_automatic(clock):
    d = clock.state_at(utc(2026, 1, 14, 14, 58))
    assert d.state is S.FIX_WINDOW and d.reason.startswith("fix:LBMA gold price PM fix")
    assert clock.state_at(utc(2026, 1, 14, 10, 28)).state is S.FIX_WINDOW
    assert clock.state_at(utc(2026, 1, 14, 10, 34)).state is S.OPEN
    # July: 15:00 London = 14:00 UTC
    assert clock.state_at(utc(2026, 7, 15, 14, 1)).state is S.FIX_WINDOW
    assert clock.state_at(utc(2026, 7, 15, 15, 1)).state is S.OPEN


def test_fix_window_can_be_disabled():
    c = SessionClock(SessionConfig(), include_fixes=False)
    assert c.state_at(utc(2026, 1, 14, 14, 58)).state is S.OPEN


def test_news_beats_fix_and_buffer_beats_news(clock):
    fomc = high(utc(2026, 1, 14, 15, 0), "FOMC")
    d = clock.state_at(utc(2026, 1, 14, 14, 58), events=[fomc])
    assert d.state is S.NEWS_WINDOW
    late = high(utc(2026, 1, 14, 22, 0), "Late")
    d = clock.state_at(utc(2026, 1, 14, 21, 50), events=[late])
    assert d.state is S.PRE_BREAK_BUFFER


def test_closed_beats_news(clock):
    d = clock.state_at(WED_BREAK, events=[high(WED_BREAK)])
    assert d.state is S.CLOSED


# -- trade_only_hours -----------------------------------------------------------------------------

def test_trade_only_hours():
    c = SessionClock(SessionConfig(trade_only_hours_utc=(7, 17)))
    d = c.state_at(utc(2026, 1, 14, 6, 59))
    assert d.state is S.OPEN and not d.entries_allowed and d.reason == "outside_trade_only_hours"
    d = c.state_at(utc(2026, 1, 14, 7, 0))
    assert d.state is S.OPEN and d.entries_allowed and d.reason == "open"
    d = c.state_at(utc(2026, 1, 14, 16, 59))
    assert d.entries_allowed
    d = c.state_at(utc(2026, 1, 14, 17, 0))
    assert d.state is S.OPEN and not d.entries_allowed
    # restriction does not mask stronger states
    assert c.state_at(utc(2026, 1, 14, 3, 0), last_tick_age_s=99).state is S.STALE
    assert c.state_at(utc(2026, 1, 14, 18, 0), events=[high(utc(2026, 1, 14, 18, 0))]).state \
        is S.NEWS_WINDOW
    d = c.state_at(utc(2026, 1, 14, 3, 0), trade_mode=K.SYMBOL_TRADE_MODE_LONGONLY)
    assert d.reason == "outside_trade_only_hours;broker_long_only" and not d.entries_allowed


def test_trade_only_hours_wrapping_midnight():
    c = SessionClock(SessionConfig(trade_only_hours_utc=(22, 2)))
    assert c.state_at(utc(2026, 1, 14, 23, 30)).entries_allowed
    assert c.state_at(utc(2026, 1, 15, 1, 0)).entries_allowed
    assert not c.state_at(utc(2026, 1, 15, 2, 0)).entries_allowed


def test_entries_allowed_only_for_plain_open(clock):
    assert clock.state_at(WED_OPEN).entries_allowed
    for d in (clock.state_at(WED_BREAK), clock.state_at(WED_CO),
              clock.state_at(WED_OPEN, last_tick_age_s=99),
              clock.state_at(utc(2026, 1, 14, 21, 50)),
              clock.state_at(utc(2026, 1, 14, 14, 59)),
              clock.state_at(WED_OPEN, trade_mode=K.SYMBOL_TRADE_MODE_CLOSEONLY)):
        assert not d.entries_allowed


def test_dst_mode_from_config_is_honoured():
    forced_summer = SessionClock(SessionConfig(dst_mode="summer"))
    assert forced_summer.state_at(utc(2026, 1, 14, 21, 0)).state is S.CLOSED
    forced_winter = SessionClock(SessionConfig(dst_mode="winter"))
    assert forced_winter.state_at(utc(2026, 7, 15, 21, 0)).state is S.OPEN


def test_custom_windows_config():
    cfg = SessionConfig(news_high=EventWindow(before_min=60, after_min=30),
                        fix_window=EventWindow(before_min=1, after_min=1))
    c = SessionClock(cfg)
    nfp = high(utc(2026, 1, 14, 13, 30))
    assert c.state_at(utc(2026, 1, 14, 12, 31), events=[nfp]).state is S.NEWS_WINDOW
    assert c.state_at(utc(2026, 1, 14, 14, 58)).state is S.OPEN
    assert c.state_at(utc(2026, 1, 14, 14, 59)).state is S.FIX_WINDOW

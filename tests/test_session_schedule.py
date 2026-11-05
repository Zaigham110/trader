"""Schedule tests (R-002, KU-008): Exness XAUUSD winter/summer hours, DST boundaries 2026."""

from datetime import UTC, datetime, timedelta

import pytest

from goldtrader.config.schema import HoursConfig
from goldtrader.session.schedule import (
    Schedule,
    ScheduleState,
    parse_hms,
    us_dst_bounds,
    us_dst_in_effect,
)

OPEN, CO, CLOSED = ScheduleState.OPEN, ScheduleState.CLOSE_ONLY, ScheduleState.CLOSED


def utc(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=UTC)


@pytest.fixture
def sched():
    return Schedule(HoursConfig(), "auto")


# 2026 weekdays used below: 2026-01-14 Wed (winter), 2026-07-15 Wed (summer),
# 2026-01-12 Mon, 2026-01-11 Sun, 2026-01-16 Fri, 2026-01-17 Sat.

# -- DST ------------------------------------------------------------------------------------------


def test_us_dst_bounds_2026():
    start, end = us_dst_bounds(2026)
    assert start == utc(2026, 3, 8, 7)
    assert end == utc(2026, 11, 1, 6)


def test_us_dst_in_effect_at_boundaries():
    assert not us_dst_in_effect(utc(2026, 3, 8, 6, 59, 59))
    assert us_dst_in_effect(utc(2026, 3, 8, 7))
    assert us_dst_in_effect(utc(2026, 11, 1, 5, 59, 59))
    assert not us_dst_in_effect(utc(2026, 11, 1, 6))
    assert not us_dst_in_effect(utc(2026, 1, 15, 12))
    assert us_dst_in_effect(utc(2026, 7, 15, 12))


def test_us_dst_rejects_naive():
    with pytest.raises(ValueError, match="aware"):
        us_dst_in_effect(datetime(2026, 7, 15, 12))


def test_dst_mode_forcing():
    assert not Schedule(HoursConfig(), "winter").is_dst(utc(2026, 7, 15))
    assert Schedule(HoursConfig(), "summer").is_dst(utc(2026, 1, 15))
    auto = Schedule(HoursConfig(), "auto")
    assert auto.is_dst(utc(2026, 7, 15)) and not auto.is_dst(utc(2026, 1, 15))


def test_bad_dst_mode_rejected():
    with pytest.raises(ValueError):
        Schedule(HoursConfig(), "sometimes")  # type: ignore[arg-type]


# -- weekday states --------------------------------------------------------------------------------


def test_wednesday_break_shifts_with_dst(sched):
    # Winter break 21:58-23:02, summer break 20:58-22:02 (docs/research/02 §3). 21:59 is inside
    # the break in BOTH seasons; 20:59 and 22:30 are the instants that tell the seasons apart.
    assert sched.state_at(utc(2026, 1, 14, 21, 59)) is CLOSED
    assert sched.state_at(utc(2026, 7, 15, 21, 59)) is CLOSED
    assert sched.state_at(utc(2026, 1, 14, 20, 59)) is OPEN
    assert sched.state_at(utc(2026, 7, 15, 20, 59)) is CLOSED
    assert sched.state_at(utc(2026, 1, 14, 22, 30)) is CLOSED
    assert sched.state_at(utc(2026, 7, 15, 22, 30)) is OPEN


def test_summer_break_is_one_hour_earlier(sched):
    assert sched.state_at(utc(2026, 7, 15, 20, 57, 59)) is OPEN
    assert sched.state_at(utc(2026, 7, 15, 20, 58)) is CLOSED
    assert sched.state_at(utc(2026, 7, 15, 22, 1)) is CO
    assert sched.state_at(utc(2026, 7, 15, 22, 2)) is OPEN
    assert sched.state_at(utc(2026, 7, 15, 23, 1)) is OPEN


def test_monday_winter_close_only_and_reopen(sched):
    assert sched.state_at(utc(2026, 1, 12, 23, 1)) is CO
    assert sched.state_at(utc(2026, 1, 12, 23, 0, 29)) is CLOSED
    assert sched.state_at(utc(2026, 1, 12, 23, 0, 30)) is CO
    assert sched.state_at(utc(2026, 1, 12, 23, 3)) is OPEN
    assert sched.state_at(utc(2026, 1, 12, 23, 59, 59)) is OPEN
    assert sched.state_at(utc(2026, 1, 13, 0, 0)) is OPEN


def test_sunday_winter(sched):
    assert sched.state_at(utc(2026, 1, 11, 0, 0)) is CLOSED
    assert sched.state_at(utc(2026, 1, 11, 23, 0)) is CLOSED
    assert sched.state_at(utc(2026, 1, 11, 23, 4)) is CO
    assert sched.state_at(utc(2026, 1, 11, 23, 6)) is OPEN


def test_sunday_summer(sched):
    assert sched.state_at(utc(2026, 7, 12, 22, 0)) is CLOSED
    assert sched.state_at(utc(2026, 7, 12, 22, 4)) is CO
    assert sched.state_at(utc(2026, 7, 12, 22, 6)) is OPEN


def test_friday_and_saturday(sched):
    assert sched.state_at(utc(2026, 1, 16, 21, 57)) is OPEN
    assert sched.state_at(utc(2026, 1, 16, 22, 30)) is CLOSED
    assert sched.state_at(utc(2026, 1, 16, 23, 30)) is CLOSED
    for h in (0, 5, 12, 23):
        assert sched.state_at(utc(2026, 1, 17, h, 30)) is CLOSED


# -- seconds_to_close / next_transition ---------------------------------------------------------


def test_seconds_to_close_winter_wednesday(sched):
    assert sched.seconds_to_close(utc(2026, 1, 14, 21, 40)) == 1080


def test_seconds_to_close_none_when_not_open(sched):
    assert sched.seconds_to_close(utc(2026, 1, 14, 22, 30)) is None
    assert sched.seconds_to_close(utc(2026, 1, 17, 12)) is None


def test_seconds_to_close_after_reopen_runs_to_next_day_break(sched):
    # Mon 23:30 winter → Tue 21:58 = 22 h 28 min
    assert sched.seconds_to_close(utc(2026, 1, 12, 23, 30)) == (22 * 60 + 28) * 60


def test_seconds_to_close_friday_summer(sched):
    assert sched.seconds_to_close(utc(2026, 7, 17, 20, 0)) == 58 * 60


def test_next_transition_chain_winter_wednesday(sched):
    t = utc(2026, 1, 14, 21, 0)
    expect = [
        (utc(2026, 1, 14, 21, 58), CLOSED),
        (utc(2026, 1, 14, 23, 0, 30), CO),
        (utc(2026, 1, 14, 23, 2), OPEN),
        (utc(2026, 1, 15, 21, 58), CLOSED),
    ]
    for when, state in expect:
        got = sched.next_transition(t)
        assert got == (when, state)
        t = when


def test_next_transition_over_weekend(sched):
    # Friday 22:30 winter → next change is Sunday close-only at 23:01:30
    assert sched.next_transition(utc(2026, 1, 16, 22, 30)) == (utc(2026, 1, 18, 23, 1, 30), CO)
    assert sched.next_transition(utc(2026, 1, 17, 12)) == (utc(2026, 1, 18, 23, 1, 30), CO)
    assert sched.next_transition(utc(2026, 1, 18, 23, 2)) == (utc(2026, 1, 18, 23, 5), OPEN)


def test_next_transition_across_dst_start_weekend(sched):
    # Fri 2026-03-06 (winter) close at 21:58; Sunday 2026-03-08 after 07:00 UTC is summer:
    # close-only starts 22:01:30, open 22:05.
    assert sched.next_transition(utc(2026, 3, 6, 21, 0)) == (utc(2026, 3, 6, 21, 58), CLOSED)
    assert sched.next_transition(utc(2026, 3, 6, 22, 0)) == (utc(2026, 3, 8, 22, 1, 30), CO)
    assert sched.state_at(utc(2026, 3, 8, 22, 6)) is OPEN
    assert sched.state_at(utc(2026, 3, 9, 20, 59)) is CLOSED


def test_next_transition_across_dst_end_weekend(sched):
    # Fri 2026-10-30 (summer) closes 20:58; Sunday 2026-11-01 after 06:00 UTC is winter.
    assert sched.next_transition(utc(2026, 10, 30, 20, 0)) == (utc(2026, 10, 30, 20, 58), CLOSED)
    assert sched.next_transition(utc(2026, 10, 30, 21, 0)) == (utc(2026, 11, 1, 23, 1, 30), CO)
    assert sched.state_at(utc(2026, 11, 2, 21, 59)) is CLOSED


def test_next_transition_is_strictly_after_and_state_changes(sched):
    t = utc(2026, 1, 11, 0, 0)
    for _ in range(40):
        before = sched.state_at(t)
        when, state = sched.next_transition(t)
        assert when > t
        assert state != before
        assert sched.state_at(when) is state
        assert sched.state_at(when - timedelta(seconds=1)) is before
        t = when


# -- input validation / helpers ------------------------------------------------------------------


def test_naive_datetime_rejected(sched):
    naive = datetime(2026, 1, 14, 12, 0)
    for fn in (
        sched.state_at,
        sched.next_transition,
        sched.seconds_to_close,
        sched.is_dst,
        sched.in_maintenance,
    ):
        with pytest.raises(ValueError, match="aware"):
            fn(naive)


def test_non_utc_aware_is_converted(sched):
    from zoneinfo import ZoneInfo

    # 16:59 New York EST = 21:59 UTC on Wed 14 Jan → CLOSED
    ny = datetime(2026, 1, 14, 16, 59, tzinfo=ZoneInfo("America/New_York"))
    assert sched.state_at(ny) is CLOSED


def test_maintenance_window(sched):
    assert sched.in_maintenance(utc(2026, 1, 17, 6))
    assert not sched.in_maintenance(utc(2026, 1, 17, 9))
    assert not sched.in_maintenance(utc(2026, 1, 14, 6))
    assert sched.in_maintenance(utc(2026, 7, 18, 4, 30))
    assert not sched.in_maintenance(utc(2026, 7, 18, 8, 30))
    assert sched.maintenance_window(utc(2026, 1, 17).date()) == (
        utc(2026, 1, 17, 5),
        utc(2026, 1, 17, 9),
    )
    assert sched.maintenance_window(utc(2026, 1, 14).date()) is None


def test_parse_hms():
    assert parse_hms("23:00:30") == 23 * 3600 + 30
    assert parse_hms("10:30") == 10 * 3600 + 30 * 60
    with pytest.raises(ValueError):
        parse_hms("25:00")


def test_bad_hours_config_rejected():
    with pytest.raises(ValueError):
        Schedule(HoursConfig(close_only_start="21:00:00"))

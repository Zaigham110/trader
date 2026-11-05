"""Exness XAUUSD trading schedule in server time (UTC+0, fixed).

Implements the schedule half of R-002 (derive session state from the Exness schedule, handle the
US-DST shift) and resolves KU-008 (server clock fixed at UTC+0; the *schedule* moves with US DST).
The broker's ``symbol_info().trade_mode`` takes precedence over this module in
:class:`goldtrader.session.state.SessionClock`; this module is the fallback and the look-ahead
(``seconds_to_close`` feeds the R-006 pre-break buffer).

Facts (Verified 2026-11-04, docs/research/02_exness_mt5.md §3 and §10; docs/MT5_EXNESS_INTEGRATION.md
§3), winter = US standard time:

* Mon–Thu: open 00:00–21:58, closed 21:58–23:00:30, close-only 23:00:30–23:02, open 23:02–23:59:59.
* Sunday: closed until 23:01:30, close-only 23:01:30–23:05, open from 23:05.
* Friday: open 00:00–21:58, then closed. Saturday: closed all day.
* Summer (US DST): one hour earlier (daily break 20:58–22:02, Sunday open 22:05).
* Weekend server maintenance Sat/Sun 05:00–09:00 UTC winter (04:00–08:00 summer); already inside
  closed time but exposed through :meth:`Schedule.in_maintenance`.

Times in :class:`HoursConfig` are winter times; ``summer_shift_hours`` (−1) is added while US DST
is in effect. The Sunday close-only start is not in ``HoursConfig``; it is a constructor argument
defaulting to the verified 23:01:30 (winter).

All public methods take timezone-aware datetimes and reject naive ones. Aware non-UTC datetimes
are converted to UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Literal

from goldtrader.config.schema import HoursConfig

DstMode = Literal["auto", "winter", "summer"]

SECONDS_PER_DAY = 86_400

#: Weekend maintenance window (winter, UTC). Summer is one hour earlier (docs/research/02 §10).
MAINTENANCE_START_WINTER = "05:00:00"
MAINTENANCE_END_WINTER = "09:00:00"
#: Default Sunday close-only start (winter). Verified from the Exness DST-update article; the
#: exact second is the article's figure and should be re-checked against trade_mode polling.
SUNDAY_CLOSE_ONLY_START_WINTER = "23:01:30"


class ScheduleState(StrEnum):
    """What the published schedule says about XAUUSD at an instant."""

    OPEN = "OPEN"
    CLOSE_ONLY = "CLOSE_ONLY"
    CLOSED = "CLOSED"


def require_utc(dt: datetime, name: str = "dt_utc") -> datetime:
    """Return ``dt`` as an aware UTC datetime; raise ``ValueError`` for a naive one."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware (UTC); got naive {dt!r}")
    return dt.astimezone(UTC)


def parse_hms(text: str) -> int:
    """Parse ``HH:MM`` or ``HH:MM:SS`` into seconds since midnight."""
    parts = text.strip().split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"bad time {text!r}; expected HH:MM or HH:MM:SS")
    h, m = int(parts[0]), int(parts[1])
    s = int(parts[2]) if len(parts) == 3 else 0
    if not (0 <= h < 24 and 0 <= m < 60 and 0 <= s < 60):
        raise ValueError(f"bad time {text!r}")
    return h * 3600 + m * 60 + s


def _nth_sunday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (6 - first.weekday()) % 7  # Monday=0 ... Sunday=6
    return first + timedelta(days=offset + 7 * (n - 1))


def us_dst_bounds(year: int) -> tuple[datetime, datetime]:
    """Return the UTC instants at which US DST starts and ends in ``year``.

    US rule (since 2007): starts the second Sunday of March at 02:00 local Eastern
    (= 07:00 UTC, EST), ends the first Sunday of November at 02:00 local (= 06:00 UTC, EDT).
    """
    start = datetime.combine(_nth_sunday(year, 3, 2), time(7, 0), tzinfo=UTC)
    end = datetime.combine(_nth_sunday(year, 11, 1), time(6, 0), tzinfo=UTC)
    return start, end


def us_dst_in_effect(dt_utc: datetime) -> bool:
    """True while US daylight saving time is in effect at the aware instant ``dt_utc`` (KU-008)."""
    dt = require_utc(dt_utc)
    start, end = us_dst_bounds(dt.year)
    return start <= dt < end


@dataclass(frozen=True, slots=True)
class Segment:
    """A run of one schedule state starting at ``start`` seconds since midnight UTC."""

    start: int
    state: ScheduleState


class Schedule:
    """The Exness XAUUSD weekly schedule, winter times shifted while US DST is in effect (R-002)."""

    def __init__(
        self,
        cfg: HoursConfig,
        dst_mode: DstMode = "auto",
        *,
        sunday_close_only_start: str = SUNDAY_CLOSE_ONLY_START_WINTER,
        maintenance_start: str = MAINTENANCE_START_WINTER,
        maintenance_end: str = MAINTENANCE_END_WINTER,
    ) -> None:
        if dst_mode not in ("auto", "winter", "summer"):
            raise ValueError(f"dst_mode must be auto|winter|summer, got {dst_mode!r}")
        self.cfg = cfg
        self.dst_mode: DstMode = dst_mode
        self._shift_s = int(cfg.summer_shift_hours) * 3600
        self._weekday_open = parse_hms(cfg.weekday_open)
        self._weekday_close = parse_hms(cfg.weekday_close)
        self._close_only_start = parse_hms(cfg.close_only_start)
        self._reopen = parse_hms(cfg.reopen)
        self._sunday_close_only = parse_hms(sunday_close_only_start)
        self._sunday_open = parse_hms(cfg.sunday_open)
        self._friday_close = parse_hms(cfg.friday_close)
        self._maint_start = parse_hms(maintenance_start)
        self._maint_end = parse_hms(maintenance_end)
        if not (self._weekday_close < self._close_only_start < self._reopen):
            raise ValueError("HoursConfig: need weekday_close < close_only_start < reopen")
        if not (self._sunday_close_only < self._sunday_open):
            raise ValueError("Schedule: need sunday_close_only_start < sunday_open")

    # -- DST --------------------------------------------------------------------------------

    def is_dst(self, dt_utc: datetime) -> bool:
        """Whether the summer schedule applies at ``dt_utc`` (``dst_mode`` may force it)."""
        dt = require_utc(dt_utc)
        if self.dst_mode == "winter":
            return False
        if self.dst_mode == "summer":
            return True
        return us_dst_in_effect(dt)

    def _shift(self, dst: bool) -> int:
        return self._shift_s if dst else 0

    # -- per-day segments ---------------------------------------------------------------------

    def _segments(self, weekday: int, dst: bool) -> list[Segment]:
        """Segments for a UTC day (``weekday`` 0=Mon..6=Sun), sorted by start.

        ``weekday_open`` of 00:00 is the continuation of the previous day's open and is not
        shifted below midnight; every other transition moves by the summer shift.
        """
        sh = self._shift(dst)
        o, c, co = ScheduleState.OPEN, ScheduleState.CLOSED, ScheduleState.CLOSE_ONLY
        if weekday == 5:  # Saturday
            return [Segment(0, c)]
        if weekday == 6:  # Sunday
            segs = [
                Segment(0, c),
                Segment(self._sunday_close_only + sh, co),
                Segment(self._sunday_open + sh, o),
            ]
        elif weekday == 4:  # Friday
            segs = [
                Segment(max(0, self._weekday_open + sh), o),
                Segment(self._friday_close + sh, c),
            ]
        else:  # Mon-Thu
            segs = [
                Segment(max(0, self._weekday_open + sh), o),
                Segment(self._weekday_close + sh, c),
                Segment(self._close_only_start + sh, co),
                Segment(self._reopen + sh, o),
            ]
        if segs[0].start > 0:
            segs.insert(0, Segment(0, c))
        return [s for s in segs if 0 <= s.start < SECONDS_PER_DAY]

    @staticmethod
    def _state_in(segs: list[Segment], sod: int) -> ScheduleState:
        state = segs[0].state
        for seg in segs:
            if seg.start <= sod:
                state = seg.state
            else:
                break
        return state

    @staticmethod
    def _midnight(dt: datetime) -> datetime:
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)

    def _day_segments(self, day_start: datetime) -> list[Segment]:
        """Segments for the UTC day beginning at ``day_start`` (DST judged at that day's noon)."""
        noon = day_start + timedelta(hours=12)
        return self._segments(day_start.weekday(), self.is_dst(noon))

    # -- public queries -----------------------------------------------------------------------

    def state_at(self, dt_utc: datetime) -> ScheduleState:
        """Schedule state at ``dt_utc`` (aware; naive raises ``ValueError``)."""
        dt = require_utc(dt_utc)
        sod = dt.hour * 3600 + dt.minute * 60 + dt.second
        segs = self._segments(dt.weekday(), self.is_dst(dt))
        return self._state_in(segs, sod)

    def transitions(self, dt_utc: datetime, max_days: int = 9):
        """Yield ``(datetime, ScheduleState)`` for each state change strictly after ``dt_utc``."""
        dt = require_utc(dt_utc)
        current = self.state_at(dt)
        sod = dt.hour * 3600 + dt.minute * 60 + dt.second + dt.microsecond / 1e6
        day = self._midnight(dt)
        for i in range(max_days):
            segs = self._day_segments(day)
            for seg in segs:
                if i == 0 and seg.start <= sod:
                    continue
                if seg.state != current:
                    current = seg.state
                    yield day + timedelta(seconds=seg.start), current
            day += timedelta(days=1)

    def next_transition(self, dt_utc: datetime) -> tuple[datetime, ScheduleState]:
        """The next instant at which the state changes, and the state it changes to."""
        for when, state in self.transitions(dt_utc):
            return when, state
        raise RuntimeError("schedule has no transitions within the look-ahead window")

    def seconds_to_close(self, dt_utc: datetime) -> int | None:
        """Seconds from ``dt_utc`` until the next CLOSED or CLOSE_ONLY start; None unless OPEN.

        Feeds the R-006 pre-break flatten buffer. Floor of the exact difference.
        """
        dt = require_utc(dt_utc)
        if self.state_at(dt) is not ScheduleState.OPEN:
            return None
        for when, state in self.transitions(dt):
            if state in (ScheduleState.CLOSED, ScheduleState.CLOSE_ONLY):
                return int((when - dt).total_seconds())
        return None

    def in_maintenance(self, dt_utc: datetime) -> bool:
        """True inside the weekend server-maintenance window (Sat/Sun; informational, R-002)."""
        dt = require_utc(dt_utc)
        if dt.weekday() not in (5, 6):
            return False
        sh = self._shift(self.is_dst(dt))
        sod = dt.hour * 3600 + dt.minute * 60 + dt.second
        return self._maint_start + sh <= sod < self._maint_end + sh

    def maintenance_window(self, day: date | datetime) -> tuple[datetime, datetime] | None:
        """The maintenance window on a Saturday/Sunday UTC date, or None on other days."""
        d = day.date() if isinstance(day, datetime) else day
        if d.weekday() not in (5, 6):
            return None
        start = datetime.combine(d, time(0), tzinfo=UTC)
        sh = self._shift(self.is_dst(start + timedelta(hours=12)))
        return (
            start + timedelta(seconds=self._maint_start + sh),
            start + timedelta(seconds=self._maint_end + sh),
        )

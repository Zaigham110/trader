"""Session state resolver: broker trade_mode first, schedule second, then data and event gates.

R-002: derive session state from ``symbol_info().trade_mode`` and the Exness schedule, never from a
hard-coded clock alone; handle the US-DST schedule shift (KU-008, resolved: server clock fixed at
UTC+0, schedule shifts). R-006: block entries inside the pre-break buffer and around high-impact
releases / FOMC / LBMA fix starts.

Precedence of :meth:`SessionClock.state_at` (most restrictive information wins, fail closed):

1. broker ``trade_mode`` when provided: DISABLED → CLOSED, CLOSEONLY → CLOSE_ONLY (both override
   the schedule; a disagreement is logged with reason ``broker_overrides_schedule``); FULL continues;
   LONGONLY / SHORTONLY continue and, if the result is OPEN, carry the reason
   ``broker_long_only`` / ``broker_short_only`` (the execution engine must respect the side);
   an unknown value → CLOSED (``broker_trade_mode_unknown``).
2. schedule CLOSED / CLOSE_ONLY.
3. STALE when ``last_tick_age_s`` exceeds ``stale_tick_seconds`` while the schedule says open.
4. PRE_BREAK_BUFFER when ``seconds_to_close`` ≤ ``pre_break_buffer_min`` × 60.
5. NEWS_WINDOW / FIX_WINDOW from :class:`EventWindows` (fix events are generated from
   ``fix_times_london`` unless ``include_fixes=False``).
6. ``trade_only_hours_utc`` outside its range → OPEN with ``entries_allowed=False``.
7. OPEN with ``entries_allowed=True``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from goldtrader.config.schema import SessionConfig
from goldtrader.mt5 import constants as K
from goldtrader.session.calendar import Event, EventWindows
from goldtrader.session.schedule import Schedule, ScheduleState, require_utc
from goldtrader.types import SessionState

log = logging.getLogger(__name__)

_SCHEDULE_TO_SESSION = {
    ScheduleState.OPEN: SessionState.OPEN,
    ScheduleState.CLOSE_ONLY: SessionState.CLOSE_ONLY,
    ScheduleState.CLOSED: SessionState.CLOSED,
}


@dataclass(frozen=True, slots=True)
class SessionDecision:
    """Result of :meth:`SessionClock.state_at`.

    ``entries_allowed`` is True only for a plain OPEN inside ``trade_only_hours_utc`` (or with no
    restriction configured). ``seconds_to_close`` and ``next_transition`` come from the schedule
    regardless of which rule decided ``state``.
    """

    state: SessionState
    reason: str
    seconds_to_close: int | None
    next_transition: datetime | None
    entries_allowed: bool = False


class SessionClock:
    """Combine broker trade_mode, the Exness schedule, tick freshness and event windows (R-002)."""

    def __init__(self, cfg: SessionConfig, *, include_fixes: bool = True) -> None:
        self.cfg = cfg
        self.schedule = Schedule(cfg.hours, cfg.dst_mode)
        self.windows = EventWindows(cfg)
        self.include_fixes = include_fixes

    def _in_trade_hours(self, dt: datetime) -> bool:
        hours = self.cfg.trade_only_hours_utc
        if hours is None:
            return True
        start, end = hours
        h = dt.hour
        if start <= end:
            return start <= h < end
        return h >= start or h < end  # window wraps midnight

    def state_at(
        self,
        dt_utc: datetime,
        *,
        trade_mode: int | None = None,
        last_tick_age_s: float | None = None,
        events: list[Event] | tuple[Event, ...] = (),
    ) -> SessionDecision:
        """Resolve the session state at ``dt_utc`` (aware; naive raises ``ValueError``)."""
        dt = require_utc(dt_utc)
        sched = self.schedule.state_at(dt)
        stc = self.schedule.seconds_to_close(dt)
        nxt_when, _ = self.schedule.next_transition(dt)

        def decide(state: SessionState, reason: str, *, entries: bool = False) -> SessionDecision:
            return SessionDecision(state=state, reason=reason, seconds_to_close=stc,
                                   next_transition=nxt_when, entries_allowed=entries)

        side_note = ""
        if trade_mode is not None:
            if trade_mode == K.SYMBOL_TRADE_MODE_DISABLED:
                reason = "broker_disabled"
                if sched is not ScheduleState.CLOSED:
                    reason = "broker_overrides_schedule:disabled"
                    log.warning("broker trade_mode DISABLED while schedule says %s at %s",
                                sched.value, dt.isoformat())
                return decide(SessionState.CLOSED, reason)
            if trade_mode == K.SYMBOL_TRADE_MODE_CLOSEONLY:
                reason = "broker_close_only"
                if sched is not ScheduleState.CLOSE_ONLY:
                    reason = "broker_overrides_schedule:close_only"
                    log.warning("broker trade_mode CLOSEONLY while schedule says %s at %s",
                                sched.value, dt.isoformat())
                return decide(SessionState.CLOSE_ONLY, reason)
            if trade_mode == K.SYMBOL_TRADE_MODE_LONGONLY:
                side_note = "broker_long_only"
            elif trade_mode == K.SYMBOL_TRADE_MODE_SHORTONLY:
                side_note = "broker_short_only"
            elif trade_mode != K.SYMBOL_TRADE_MODE_FULL:
                log.error("unknown symbol trade_mode %r at %s; failing closed", trade_mode,
                          dt.isoformat())
                return decide(SessionState.CLOSED, "broker_trade_mode_unknown")
            if sched is not ScheduleState.OPEN:
                log.warning("broker trade_mode %s while schedule says %s at %s; schedule wins",
                            trade_mode, sched.value, dt.isoformat())

        if sched is ScheduleState.CLOSED:
            return decide(SessionState.CLOSED, "schedule_closed")
        if sched is ScheduleState.CLOSE_ONLY:
            return decide(SessionState.CLOSE_ONLY, "schedule_close_only")

        if last_tick_age_s is not None and last_tick_age_s > self.cfg.stale_tick_seconds:
            return decide(SessionState.STALE,
                          f"stale_ticks:{last_tick_age_s:.1f}s>{self.cfg.stale_tick_seconds}s")

        if stc is not None and stc <= self.cfg.pre_break_buffer_min * 60:
            return decide(SessionState.PRE_BREAK_BUFFER, f"pre_break_buffer:{stc}s")

        all_events: list[Event] = list(events)
        if self.include_fixes:
            all_events.extend(self.windows.fixes_around(dt))
        win_state, ev = self.windows.active(dt, all_events)
        if win_state is SessionState.NEWS_WINDOW and ev is not None:
            return decide(SessionState.NEWS_WINDOW, f"news:{ev.title}")
        if win_state is SessionState.FIX_WINDOW and ev is not None:
            return decide(SessionState.FIX_WINDOW, f"fix:{ev.title}")

        if not self._in_trade_hours(dt):
            reason = "outside_trade_only_hours"
            if side_note:
                reason = f"{reason};{side_note}"
            return decide(SessionState.OPEN, reason, entries=False)

        return decide(SessionState.OPEN, side_note or "open", entries=True)

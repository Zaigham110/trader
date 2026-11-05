"""Session module: Exness XAUUSD schedule with US-DST shift, event calendar windows and the
session-state resolver (R-002, R-006, KU-008). Pure code, no network, aware UTC datetimes only.
"""

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
)
from goldtrader.session.schedule import (
    Schedule,
    ScheduleState,
    require_utc,
    us_dst_bounds,
    us_dst_in_effect,
)
from goldtrader.session.state import SessionClock, SessionDecision

__all__ = [
    "FOMC_YAML_PATH",
    "Event",
    "EventWindows",
    "Schedule",
    "ScheduleState",
    "SessionClock",
    "SessionDecision",
    "dedupe_events",
    "fix_events",
    "load_bundled_fomc",
    "load_fomc_dates",
    "parse_bls_ics",
    "parse_forexfactory_json",
    "require_utc",
    "us_dst_bounds",
    "us_dst_in_effect",
]

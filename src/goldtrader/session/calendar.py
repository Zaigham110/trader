"""Economic-event calendar: parsers, LBMA fix times and the R-006 entry-blocking windows.

R-006: block new entries in configurable windows around high-impact releases, FOMC decisions and
the LBMA gold price fix starts (defaults in docs/RISK_POLICY.md §1: high impact −30/+15 min, fix
−3/+3 min). Sources (docs/research/01_gold_trading.md §4.3, Verified 2026-11-04):

* BLS release calendar iCal feed (CPI, Employment Situation, PPI, Real Earnings, ...; all times ET)
  → :func:`parse_bls_ics`.
* Federal Reserve FOMC calendar → committed YAML (``fomc_2026_2027.yaml``) → :func:`load_fomc_dates`.
* ForexFactory weekly JSON (fields ``title, country, date, impact, forecast, previous``; licence
  Unknown, convenience layer only) → :func:`parse_forexfactory_json`.
* LBMA Gold Price at 10:30 and 15:00 London (docs/research/01 §2.2) → :func:`fix_events`.

Everything here is pure: parsers take text, no network, no clock. All ``Event.time_utc`` values are
aware UTC datetimes.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yaml

from goldtrader.config.schema import SessionConfig
from goldtrader.session.schedule import parse_hms, require_utc
from goldtrader.types import SessionState

Impact = Literal["high", "medium", "low", "holiday", "fix"]

NEW_YORK = ZoneInfo("America/New_York")
LONDON = ZoneInfo("Europe/London")

#: BLS release titles treated as high impact for gold (docs/research/01 §1.5, §4.3).
BLS_HIGH_IMPACT: tuple[str, ...] = ("employment situation", "consumer price index")
#: Titles the BLS parser recognises (others are kept as medium with their raw title).
BLS_KNOWN_RELEASES: tuple[str, ...] = (
    "employment situation", "consumer price index", "producer price index", "real earnings",
    "job openings and labor turnover", "employment cost index", "productivity and costs",
    "import and export price indexes", "state employment and unemployment",
    "metropolitan area employment and unemployment", "county employment and wages",
    "employment situation of veterans", "union members", "usual weekly earnings",
)


@dataclass(frozen=True, slots=True)
class Event:
    """One scheduled event. ``time_utc`` is aware UTC; ``impact`` drives the R-006 windows."""

    time_utc: datetime
    title: str
    country: str
    impact: Impact
    source: str
    actual: float | None
    forecast: float | None
    previous: float | None
    event_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "time_utc", require_utc(self.time_utc, "Event.time_utc"))
        if self.impact not in ("high", "medium", "low", "holiday", "fix"):
            raise ValueError(f"bad impact {self.impact!r}")


def _short_hash(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def dedupe_events(events: list[Event] | tuple[Event, ...]) -> list[Event]:
    """Drop events with the same ``(time_utc, title)`` keeping the first; sorted by time."""
    seen: set[tuple[datetime, str]] = set()
    out: list[Event] = []
    for ev in events:
        key = (ev.time_utc, ev.title.strip().lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(ev)
    out.sort(key=lambda e: e.time_utc)
    return out


# -- numeric fields -----------------------------------------------------------------------------

_NUM_RE = re.compile(r"^\s*([-+]?\d[\d,]*\.?\d*|[-+]?\.\d+)\s*([%KMBT]?)\s*$", re.IGNORECASE)
_SUFFIX = {"": 1.0, "%": 1.0, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}


def to_float(value: Any) -> float | None:
    """Parse calendar numbers such as ``"0.3%"``, ``"-50K"``, ``"225K"``, ``3.1``; None if absent."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    m = _NUM_RE.match(text)
    if not m:
        return None
    num = float(m.group(1).replace(",", ""))
    return num * _SUFFIX[m.group(2).upper()]


# -- BLS iCal ------------------------------------------------------------------------------------

def _unfold_ics(text: str) -> list[str]:
    """RFC 5545 line unfolding: a line starting with space/tab continues the previous one."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def _ics_unescape(value: str) -> str:
    return (value.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",")
            .replace("\\;", ";").replace("\\\\", "\\")).strip()


def _parse_ics_dt(params: dict[str, str], value: str) -> datetime | None:
    """DTSTART value with optional TZID / VALUE=DATE / trailing Z → aware UTC (None for all-day)."""
    value = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or (len(value) == 8 and value.isdigit()):
        return None
    utc = value.endswith("Z")
    core = value[:-1] if utc else value
    try:
        naive = datetime.strptime(core, "%Y%m%dT%H%M%S")
    except ValueError:
        try:
            naive = datetime.strptime(core, "%Y%m%dT%H%M")
        except ValueError:
            return None
    if utc:
        return naive.replace(tzinfo=UTC)
    tzid = params.get("TZID")
    tz = ZoneInfo(tzid) if tzid else NEW_YORK  # BLS publishes Eastern times (docs/research/01 §4.3)
    return naive.replace(tzinfo=tz).astimezone(UTC)


def _ics_events(lines: list[str]) -> list[dict[str, tuple[dict[str, str], str]]]:
    events: list[dict[str, tuple[dict[str, str], str]]] = []
    cur: dict[str, tuple[dict[str, str], str]] | None = None
    for line in lines:
        if line == "BEGIN:VEVENT":
            cur = {}
            continue
        if line == "END:VEVENT":
            if cur is not None:
                events.append(cur)
            cur = None
            continue
        if cur is None or ":" not in line:
            continue
        head, _, value = line.partition(":")
        name, *param_parts = head.split(";")
        params: dict[str, str] = {}
        for p in param_parts:
            k, _, v = p.partition("=")
            params[k.upper()] = v.strip('"')
        cur[name.upper()] = (params, value)
    return events


def bls_impact(title: str) -> Impact:
    """High for the Employment Situation and CPI releases, medium for every other BLS release."""
    low = title.lower()
    return "high" if any(k in low for k in BLS_HIGH_IMPACT) else "medium"


def parse_bls_ics(text: str | bytes) -> list[Event]:
    """Parse the BLS news-release iCal feed into events (all-day entries are skipped).

    DTSTART may carry ``TZID=America/New_York`` (the feed's convention), be UTC (``Z``) or be
    floating, which is read as Eastern. Impact per :func:`bls_impact`; country ``US``; source ``bls``.
    """
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    out: list[Event] = []
    for props in _ics_events(_unfold_ics(text)):
        if "DTSTART" not in props or "SUMMARY" not in props:
            continue
        when = _parse_ics_dt(*props["DTSTART"])
        if when is None:
            continue
        title = _ics_unescape(props["SUMMARY"][1])
        uid = _ics_unescape(props["UID"][1]) if "UID" in props else ""
        event_id = f"bls:{uid}" if uid else "bls:" + _short_hash(when.isoformat(), title)
        out.append(Event(time_utc=when, title=title, country="US", impact=bls_impact(title),
                         source="bls", actual=None, forecast=None, previous=None,
                         event_id=event_id))
    return dedupe_events(out)


# -- ForexFactory JSON ----------------------------------------------------------------------------

_FF_IMPACT: dict[str, Impact] = {"high": "high", "medium": "medium", "low": "low",
                                 "holiday": "holiday", "non-economic": "low"}


def parse_forexfactory_json(text: str | bytes) -> list[Event]:
    """Parse the ForexFactory weekly JSON array (``title, country, date, impact, forecast,
    previous`` and, when present, ``actual``). ``date`` is ISO 8601 with an offset (−05:00 / −04:00)
    and is converted to UTC. Unknown impact strings become ``low``; entries without a parsable date
    are dropped. Source ``forexfactory``.
    """
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("events") or data.get("data") or []
    out: list[Event] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        raw_date = str(item.get("date", "")).strip()
        if not raw_date:
            continue
        try:
            when = datetime.fromisoformat(raw_date)
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=NEW_YORK)  # feed is Eastern; offset normally present
        when = when.astimezone(UTC)
        title = str(item.get("title", "")).strip()
        country = str(item.get("country", "")).strip().upper()
        impact = _FF_IMPACT.get(str(item.get("impact", "")).strip().lower(), "low")
        event_id = "ff:" + _short_hash(when.isoformat(), title, country)
        out.append(Event(time_utc=when, title=title, country=country, impact=impact,
                         source="forexfactory", actual=to_float(item.get("actual")),
                         forecast=to_float(item.get("forecast")),
                         previous=to_float(item.get("previous")), event_id=event_id))
    return dedupe_events(out)


# -- FOMC YAML -----------------------------------------------------------------------------------

def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip())


def load_fomc_dates(yaml_text: str | bytes) -> list[Event]:
    """Load FOMC decision events from the committed YAML (``fomc_2026_2027.yaml``).

    Format: a mapping with ``timezone`` (default America/New_York), ``decision_time`` (default
    ``14:00``, the statement release) and ``meetings``, a list of ``{start, end}`` dates (decision on
    ``end``) or bare decision dates / ISO datetimes. Impact ``high``; source ``fomc``.
    """
    if isinstance(yaml_text, bytes):
        yaml_text = yaml_text.decode("utf-8")
    doc = yaml.safe_load(yaml_text) or {}
    if isinstance(doc, list):
        doc = {"meetings": doc}
    tz = ZoneInfo(str(doc.get("timezone", "America/New_York")))
    default_sec = parse_hms(str(doc.get("decision_time", "14:00")))
    out: list[Event] = []
    for entry in doc.get("meetings") or []:
        note = ""
        if isinstance(entry, dict):
            when_raw = entry.get("decision") or entry.get("end") or entry.get("date")
            sec = parse_hms(str(entry["time"])) if "time" in entry else default_sec
            note = str(entry.get("note", "")).strip()
            if entry.get("sep"):
                note = (note + " " if note else "") + "(SEP)"
        else:
            when_raw, sec = entry, default_sec
        if isinstance(when_raw, datetime) and when_raw.tzinfo is not None:
            when = when_raw.astimezone(UTC)
        else:
            d = _as_date(when_raw)
            local = datetime.combine(d, time(0), tzinfo=tz) + timedelta(seconds=sec)
            when = local.astimezone(UTC)
        title = "FOMC rate decision" + (f" {note}" if note else "")
        out.append(Event(time_utc=when, title=title, country="US", impact="high", source="fomc",
                         actual=None, forecast=None, previous=None,
                         event_id=f"fomc:{when.date().isoformat()}"))
    return dedupe_events(out)


#: The FOMC calendar shipped with the package (2026 entered; 2027 to be added).
FOMC_YAML_PATH = Path(__file__).with_name("fomc_2026_2027.yaml")


def load_bundled_fomc() -> list[Event]:
    """Load :data:`FOMC_YAML_PATH` (the committed FOMC calendar)."""
    return load_fomc_dates(FOMC_YAML_PATH.read_text(encoding="utf-8"))


# -- LBMA fix -------------------------------------------------------------------------------------

def fix_events(date_utc: date | datetime,
               times_london: tuple[str, ...] = ("10:30", "15:00")) -> list[Event]:
    """LBMA gold price auction starts on ``date_utc`` as UTC events with impact ``fix``.

    London local times (Europe/London; BST or GMT by date) are converted with zoneinfo, so
    15:00 London is 15:00 UTC in January and 14:00 UTC in July. No fixes on Saturday or Sunday.
    UK bank holidays (no auction) are not modelled: a window then is a harmless false positive.
    """
    d = date_utc.date() if isinstance(date_utc, datetime) else date_utc
    if d.weekday() >= 5:
        return []
    out: list[Event] = []
    for i, t in enumerate(times_london):
        sec = parse_hms(t)
        local = datetime.combine(d, time(0), tzinfo=LONDON) + timedelta(seconds=sec)
        when = local.astimezone(UTC)
        label = "AM" if sec < 12 * 3600 else "PM"
        out.append(Event(time_utc=when, title=f"LBMA gold price {label} fix ({t} London)",
                         country="GB", impact="fix", source="lbma", actual=None, forecast=None,
                         previous=None, event_id=f"lbma:{d.isoformat()}:{i}:{t}"))
    return out


# -- windows --------------------------------------------------------------------------------------

class EventWindows:
    """R-006 windows: NEWS_WINDOW around high-impact events, FIX_WINDOW around fix starts."""

    def __init__(self, cfg: SessionConfig) -> None:
        self.cfg = cfg
        self._news_before = timedelta(minutes=cfg.news_high.before_min)
        self._news_after = timedelta(minutes=cfg.news_high.after_min)
        self._fix_before = timedelta(minutes=cfg.fix_window.before_min)
        self._fix_after = timedelta(minutes=cfg.fix_window.after_min)

    def fixes_around(self, dt_utc: datetime) -> list[Event]:
        """Fix events for the UTC day of ``dt_utc`` and its neighbours (covers midnight edges)."""
        d = require_utc(dt_utc).date()
        out: list[Event] = []
        for delta in (-1, 0, 1):
            out.extend(fix_events(d + timedelta(days=delta), self.cfg.fix_times_london))
        return out

    def active(self, dt_utc: datetime,
               events: list[Event] | tuple[Event, ...]) -> tuple[SessionState | None, Event | None]:
        """Return ``(NEWS_WINDOW, event)`` if inside a high-impact window, ``(FIX_WINDOW, event)``
        if inside a fix window, else ``(None, None)``. Windows are inclusive at both ends; the
        nearest event wins within a class; news takes precedence over fix.
        """
        dt = require_utc(dt_utc)
        news_hit: Event | None = None
        fix_hit: Event | None = None
        for ev in dedupe_events(events):
            if ev.impact == "high":
                before, after, hit = self._news_before, self._news_after, news_hit
            elif ev.impact == "fix":
                before, after, hit = self._fix_before, self._fix_after, fix_hit
            else:
                continue
            inside = ev.time_utc - before <= dt <= ev.time_utc + after
            if inside and (hit is None or abs(ev.time_utc - dt) < abs(hit.time_utc - dt)):
                if ev.impact == "high":
                    news_hit = ev
                else:
                    fix_hit = ev
        if news_hit is not None:
            return SessionState.NEWS_WINDOW, news_hit
        if fix_hit is not None:
            return SessionState.FIX_WINDOW, fix_hit
        return None, None

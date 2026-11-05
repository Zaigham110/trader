"""Calendar events → ``events`` table and back to :class:`Event` (D-038; ARCHITECTURE.md §2.2
``calendar``; R-006 windows).

* :class:`CalendarStore` persists :class:`goldtrader.session.calendar.Event` rows (migration 007)
  and answers the three questions the system asks: what is coming (``upcoming``), when is the next
  high-impact release (``next_high_impact``, the brain's "minutes to event"), and which events the
  gateway must know for today's R-006 windows (``events_for_gateway`` → ``GatewayService.events_fn``).
* :class:`CalendarRefresher` pulls the sources listed in docs/research/01_gold_trading.md §4.3:
  the BLS iCal feed (conditional GET), the committed FOMC YAML, the LBMA fix times for the next
  three days and, only when the owner set ``forexfactory_json_url`` (KU-013), the ForexFactory
  weekly JSON. Parsers are reused from ``session.calendar``; nothing is duplicated here.

All times are epoch ms UTC in the table and aware UTC datetimes in :class:`Event`.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from goldtrader.config.schema import FeedsConfig
from goldtrader.feeds.rss import FetchResult, fetch_feed
from goldtrader.feeds.state import load_feed_state, save_feed_state
from goldtrader.session.calendar import (
    FOMC_YAML_PATH,
    Event,
    Impact,
    dedupe_events,
    fix_events,
    load_fomc_dates,
    parse_bls_ics,
    parse_forexfactory_json,
)

DAY_MS = 86_400_000
HOUR_MS = 3_600_000

FetchFn = Callable[..., FetchResult]


def _to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _utc_midnight_ms(now_ms: int) -> int:
    return now_ms - now_ms % DAY_MS


class CalendarStore:
    """Reads and writes the ``events`` table."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.c = conn

    # -- writes ------------------------------------------------------------------------------------
    def upsert_events(self, events: Iterable[Event], fetched_ms: int) -> int:
        """Insert or refresh events by ``event_id``. Re-fetched rows update time, title and the
        actual / forecast / previous numbers (a release's ``actual`` appears after the fact), so the
        store converges on the publisher's latest view. Returns the number of new ids."""
        evs = dedupe_events(list(events))
        if not evs:
            return 0
        ids = [e.event_id for e in evs]
        existing: set[str] = set()
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            marks = ",".join("?" * len(chunk))
            existing.update(
                r[0]
                for r in self.c.execute(
                    f"SELECT event_id FROM events WHERE event_id IN ({marks})", chunk
                )
            )
        with self.c:
            self.c.executemany(
                "INSERT INTO events(event_id, time_ms, title, country, impact, source, actual, "
                "forecast, previous, fetched_ms) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(event_id) DO UPDATE SET time_ms=excluded.time_ms, "
                "title=excluded.title, country=excluded.country, impact=excluded.impact, "
                "source=excluded.source, actual=COALESCE(excluded.actual, events.actual), "
                "forecast=COALESCE(excluded.forecast, events.forecast), "
                "previous=COALESCE(excluded.previous, events.previous), "
                "fetched_ms=excluded.fetched_ms",
                [
                    (
                        e.event_id,
                        _to_ms(e.time_utc),
                        e.title,
                        e.country,
                        e.impact,
                        e.source,
                        e.actual,
                        e.forecast,
                        e.previous,
                        int(fetched_ms),
                    )
                    for e in evs
                ],
            )
        return len(set(ids) - existing)

    def purge_before(self, before_ms: int) -> int:
        """Delete events older than ``before_ms`` (housekeeping; past events are not journal)."""
        with self.c:
            cur = self.c.execute("DELETE FROM events WHERE time_ms < ?", (int(before_ms),))
        return int(cur.rowcount or 0)

    # -- reads -------------------------------------------------------------------------------------
    @staticmethod
    def _event(row: sqlite3.Row) -> Event:
        impact: Impact = row["impact"]
        return Event(
            time_utc=datetime.fromtimestamp(row["time_ms"] / 1000, tz=UTC),
            title=row["title"],
            country=row["country"],
            impact=impact,
            source=row["source"],
            actual=row["actual"],
            forecast=row["forecast"],
            previous=row["previous"],
            event_id=row["event_id"],
        )

    def between(self, start_ms: int, end_ms: int) -> list[Event]:
        """Events with ``start_ms <= time_ms < end_ms``, ordered by time."""
        rows = self.c.execute(
            "SELECT * FROM events WHERE time_ms >= ? AND time_ms < ? ORDER BY time_ms, event_id",
            (int(start_ms), int(end_ms)),
        ).fetchall()
        return [self._event(r) for r in rows]

    def upcoming(self, now_ms: int, hours: float = 48) -> list[Event]:
        """Events from ``now_ms`` up to ``hours`` ahead (the brain's "upcoming events" feature)."""
        return self.between(now_ms, int(now_ms + hours * HOUR_MS))

    def next_high_impact(self, now_ms: int) -> tuple[Event, float] | None:
        """The next ``high`` event at or after ``now_ms`` and the minutes until it, or None."""
        row = self.c.execute(
            "SELECT * FROM events WHERE impact='high' AND time_ms >= ? "
            "ORDER BY time_ms, event_id LIMIT 1",
            (int(now_ms),),
        ).fetchone()
        if row is None:
            return None
        ev = self._event(row)
        return ev, (row["time_ms"] - now_ms) / 60_000.0

    def events_for_gateway(self, now_ms: int) -> list[Event]:
        """Events for the UTC day of ``now_ms`` ± 1 day: what ``SessionClock.state_at`` needs to
        resolve R-006 windows across midnight (``GatewayService.events_fn``)."""
        start = _utc_midnight_ms(now_ms) - DAY_MS
        return self.between(start, start + 3 * DAY_MS)

    def events_fn(self, clock_ms: Callable[[], int]) -> Callable[[], list[Event]]:
        """A zero-argument ``events_fn`` for ``GatewayService`` bound to ``clock_ms``."""
        return lambda: self.events_for_gateway(clock_ms())

    def count(self) -> int:
        return int(self.c.execute("SELECT COUNT(*) FROM events").fetchone()[0])


class CalendarRefresher:
    """Refreshes the :class:`CalendarStore` from every configured source. Never raises."""

    FIX_DAYS_AHEAD = 3

    def __init__(
        self,
        cfg: FeedsConfig,
        store: CalendarStore,
        clock_ms: Callable[[], int],
        fetch: FetchFn = fetch_feed,
        fomc_yaml_path: Path | str = FOMC_YAML_PATH,
        *,
        fix_times_london: tuple[str, ...] = ("10:30", "15:00"),
        timeout_s: float = 10,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.clock_ms = clock_ms
        self.fetch = fetch
        self.fomc_yaml_path = Path(fomc_yaml_path)
        self.fix_times_london = fix_times_london
        self.timeout_s = timeout_s

    def due(self, now_ms: int | None = None) -> bool:
        """True when the BLS feed was last fetched more than ``calendar_refresh_hours`` ago."""
        now = self.clock_ms() if now_ms is None else now_ms
        st = load_feed_state(self.store.c, "bls")
        return st.last_fetch_ms is None or now - st.last_fetch_ms >= (
            self.cfg.calendar_refresh_hours * HOUR_MS
        )

    def run_once(self) -> dict[str, tuple[int, str | None]]:
        """Pull every source once. Returns ``{source: (new_events, error)}``."""
        out: dict[str, tuple[int, str | None]] = {}
        out["bls"] = self._remote("bls", self.cfg.bls_ics_url, parse_bls_ics)
        out["fomc"] = self._fomc()
        out["lbma"] = self._fixes()
        if self.cfg.forexfactory_json_url:
            out["forexfactory"] = self._remote(
                "forexfactory", self.cfg.forexfactory_json_url, parse_forexfactory_json
            )
        return out

    # -- sources -----------------------------------------------------------------------------------
    def _remote(
        self, name: str, url: str, parser: Callable[[str], list[Event]]
    ) -> tuple[int, str | None]:
        now = self.clock_ms()
        state = load_feed_state(self.store.c, name)
        try:
            res = self.fetch(
                url, etag=state.etag, last_modified=state.last_modified, timeout_s=self.timeout_s
            )
        except Exception as e:
            res = FetchResult(0, "", error=f"{type(e).__name__}: {e}")
        new = 0
        error: str | None = None
        etag, last_modified = state.etag, state.last_modified
        if res.unchanged:
            pass
        elif not res.ok:
            error = res.error or f"http {res.status}"
        else:
            try:
                events = parser(res.text)
            except (ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
                error = f"parse error: {type(e).__name__}: {e}"
            else:
                try:
                    new = self.store.upsert_events(events, now)
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

    def _fomc(self) -> tuple[int, str | None]:
        now = self.clock_ms()
        try:
            events = load_fomc_dates(self.fomc_yaml_path.read_text(encoding="utf-8"))
            new = self.store.upsert_events(events, now)
        except Exception as e:
            return 0, f"fomc yaml: {type(e).__name__}: {e}"
        return new, None

    def _fixes(self) -> tuple[int, str | None]:
        now = self.clock_ms()
        today = datetime.fromtimestamp(now / 1000, tz=UTC).date()
        events: list[Event] = []
        for d in range(self.FIX_DAYS_AHEAD):
            events.extend(fix_events(today + timedelta(days=d), self.fix_times_london))
        try:
            return self.store.upsert_events(events, now), None
        except sqlite3.Error as e:
            return 0, f"db error: {e}"

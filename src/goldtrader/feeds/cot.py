"""Gold futures positioning from the CFTC Commitments of Traders report (D-060).

Source: the CFTC public reporting API (US government open data, no key), dataset `72hh-3qpy`
(disaggregated, futures only), contract 088691 (COMEX gold). Each row gives positions as of a
Tuesday; the CFTC publishes a few days later and has announced a move to more frequent releases,
so nothing here assumes a weekday: the refresher polls and keeps whatever is newest.

Use: slow context for the Strategy and Risk-advisor prompts. Published evidence that positioning
extremes predict gold is mixed (speculator positions largely follow past returns), so the line
says what it is and code never sizes or gates on it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

GOLD_CODE = "088691"
URL = (
    "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"
    f"?cftc_contract_market_code={GOLD_CODE}"
    "&$select=report_date_as_yyyy_mm_dd,m_money_positions_long_all,"
    "m_money_positions_short_all,open_interest_all"
    "&$order=report_date_as_yyyy_mm_dd%20DESC&$limit=170"
)
USER_AGENT = "goldtrader-feeds/1.0"
LOOKBACK_REPORTS = 156  # about three years of weekly reports
STALE_DAYS = 21
REFRESH_HOURS = 12


def http_get(url: str, timeout_s: float = 20.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 - fixed https URL
        return resp.read().decode("utf-8")


def parse(text: str) -> list[tuple[str, int, int, int | None]]:
    """API JSON → (report_date, mm_long, mm_short, open_interest); malformed rows are dropped."""
    data = json.loads(text)
    out: list[tuple[str, int, int, int | None]] = []
    for row in data if isinstance(data, list) else []:
        try:
            day = str(row["report_date_as_yyyy_mm_dd"])[:10]
            datetime.strptime(day, "%Y-%m-%d")
            long_, short = (
                int(float(row["m_money_positions_long_all"])),
                int(float(row["m_money_positions_short_all"])),
            )
            oi = row.get("open_interest_all")
            out.append((day, long_, short, int(float(oi)) if oi is not None else None))
        except (KeyError, TypeError, ValueError):
            continue
    return out


class CotStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.c = conn

    def upsert(self, rows: list[tuple[str, int, int, int | None]], fetched_ms: int) -> int:
        n = 0
        for day, long_, short, oi in rows:
            self.c.execute(
                "INSERT INTO cot_reports(report_date, mm_long, mm_short, open_interest, fetched_ms) "
                "VALUES (?,?,?,?,?) ON CONFLICT(report_date) DO UPDATE SET mm_long=excluded.mm_long, "
                "mm_short=excluded.mm_short, open_interest=excluded.open_interest, "
                "fetched_ms=excluded.fetched_ms",
                (day, long_, short, oi, fetched_ms),
            )
            n += 1
        return n

    def summary(self, now_ms: int) -> dict[str, Any] | None:
        """Latest report against the last ~3 years, or None when nothing usable is stored."""
        rows = self.c.execute(
            "SELECT report_date, mm_long, mm_short, open_interest FROM cot_reports "
            "ORDER BY report_date DESC LIMIT ?",
            (LOOKBACK_REPORTS,),
        ).fetchall()
        if len(rows) < 2:
            return None
        nets = [int(r[1]) - int(r[2]) for r in rows]
        day = datetime.strptime(rows[0][0], "%Y-%m-%d").replace(tzinfo=UTC)
        age_days = (now_ms / 1000 - day.timestamp()) / 86400
        if age_days > STALE_DAYS:
            return None
        below = sum(1 for n in nets if n < nets[0])
        return {
            "report_date": rows[0][0],
            "age_days": round(age_days, 1),
            "net": nets[0],
            "long": int(rows[0][1]),
            "short": int(rows[0][2]),
            "change": nets[0] - nets[1],
            "percentile": round(100.0 * below / (len(nets) - 1)),
            "weeks": len(nets),
            "open_interest": rows[0][3],
        }

    def line(self, now_ms: int) -> str:
        s = self.summary(now_ms)
        if s is None:
            return ""
        side = "long" if s["net"] >= 0 else "short"
        return (
            f"COMEX gold managed-money net {side} {abs(s['net']):,} contracts as of "
            f"{s['report_date']} ({s['age_days']:.0f} days old); higher than {s['percentile']}% of "
            f"the last {s['weeks']} reports; change since the previous report {s['change']:+,}. "
            "Slow weekly context, not a timing signal."
        )


class CotRefresher:
    def __init__(
        self,
        store: CotStore,
        clock_ms: Callable[[], int],
        *,
        fetch: Callable[[str], str] | None = None,
    ) -> None:
        self.store = store
        self.clock_ms = clock_ms
        self.fetch = fetch or http_get

    def run_once(self) -> tuple[int, str | None]:
        """Fetch and store. Never raises; returns (rows stored, error text or None)."""
        try:
            rows = parse(self.fetch(URL))
        except Exception as e:  # noqa: BLE001 - a feed problem must never reach the scheduler
            log.warning("COT refresh failed: %s: %s", type(e).__name__, e)
            return 0, f"{type(e).__name__}: {e}"[:200]
        if not rows:
            return 0, "no rows"
        return self.store.upsert(rows, self.clock_ms()), None

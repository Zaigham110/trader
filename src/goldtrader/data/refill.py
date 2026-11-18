"""Refill missing 1-minute bars from the terminal's history when the gateway starts (D-067).

The gateway records bars only while it runs. After an outage (a crash, a frozen loop ended by the
watchdog, the PC asleep) the stored history has a hole, and everything that replays prices
(channel-signal scoring, graded decisions, the paper ledger's features, backtests) is wrong across
it. This pulls the last few days from the terminal and stores only the minutes that are missing:
bars the gateway recorded itself, from real ticks, are never overwritten.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from goldtrader.db.repo import Repo

log = logging.getLogger(__name__)

MAX_DAYS = 7
SKIP_RECENT_S = 60  # the minute still being formed belongs to the live feed


def refill_missing_bars(
    client: Any, repo: Repo, symbol: str, timeframe_m1: int, now_s: int, days: int = MAX_DAYS
) -> int:
    """Store history bars for minutes that have no stored bar. Returns how many were added.
    Does nothing on a database with no bars at all: first setup uses `goldctl backfill`."""
    start_s = now_s - days * 86_400
    have = {
        int(r[0])
        for r in repo.c.execute(
            "SELECT time_s FROM bars WHERE symbol=? AND seconds=60 AND time_s>=?",
            (symbol, start_s),
        )
    }
    if (
        not have
        and not repo.c.execute(
            "SELECT 1 FROM bars WHERE symbol=? AND seconds=60 LIMIT 1", (symbol,)
        ).fetchone()
    ):
        return 0
    bars = (
        client.copy_rates_range(
            symbol,
            timeframe_m1,
            datetime.fromtimestamp(start_s, UTC),
            datetime.fromtimestamp(now_s, UTC),
        )
        or []
    )
    missing = [b for b in bars if b.time not in have and b.time + SKIP_RECENT_S <= now_s]
    if not missing:
        return 0
    n = repo.upsert_bars(symbol, missing, "mt5_refill")
    log.info(
        "refilled %d missing 1-minute bars (%s .. %s UTC)",
        n,
        datetime.fromtimestamp(missing[0].time, UTC).strftime("%m-%d %H:%M"),
        datetime.fromtimestamp(missing[-1].time, UTC).strftime("%m-%d %H:%M"),
    )
    return n

"""Missing 1-minute bars are refilled from the terminal history at gateway start (D-067)."""

from __future__ import annotations

from goldtrader.data.refill import SKIP_RECENT_S, refill_missing_bars
from goldtrader.db import Repo, connect, migrate
from goldtrader.types import Bar

SYM = "XAUUSDm"
T0 = 1_794_960_000  # a whole minute


def _bar(t: int, px: float) -> Bar:
    return Bar(
        time=t, seconds=60, ticks=5,
        bid_open=px, bid_high=px + 0.5, bid_low=px - 0.5, bid_close=px,
        ask_open=px + 0.2, ask_high=px + 0.7, ask_low=px - 0.3, ask_close=px + 0.2,
    )  # fmt: skip


class Terminal:
    def __init__(self, bars):
        self.bars, self.asked = bars, []

    def copy_rates_range(self, symbol, timeframe, date_from, date_to):
        self.asked.append((symbol, timeframe))
        lo, hi = date_from.timestamp(), date_to.timestamp()
        return [b for b in self.bars if lo <= b.time <= hi]


def test_only_missing_minutes_are_added_and_live_bars_are_never_overwritten():
    conn = connect(None)
    migrate(conn)
    repo = Repo(conn)
    live = [_bar(T0 + 60 * i, 4000.0) for i in range(10)] + [
        _bar(T0 + 60 * i, 4000.0) for i in range(40, 50)
    ]
    repo.upsert_bars(SYM, live, "live")
    history = [_bar(T0 + 60 * i, 3990.0) for i in range(0, 60)]  # the terminal's own version
    now = T0 + 60 * 60
    term = Terminal(history)
    added = refill_missing_bars(term, repo, SYM, 1, now)
    assert added == 30 + 10 and term.asked == [(SYM, 1)]  # the hole and every closed minute
    rows = dict(conn.execute("SELECT time_s, source FROM bars").fetchall())
    px = dict(conn.execute("SELECT time_s, bid_close FROM bars").fetchall())
    assert rows[T0] == "live" and px[T0] == 4000.0  # recorded from real ticks: untouched
    assert rows[T0 + 60 * 20] == "mt5_refill" and px[T0 + 60 * 20] == 3990.0
    assert max(rows) <= now - SKIP_RECENT_S  # the forming minute belongs to the live feed
    assert refill_missing_bars(term, repo, SYM, 1, now) == 0  # nothing left to add
    assert refill_missing_bars(Terminal([]), repo, SYM, 1, now) == 0
    # a brand-new database is left to `goldctl backfill`
    empty = connect(None)
    migrate(empty)
    t2 = Terminal(history)
    assert refill_missing_bars(t2, Repo(empty), SYM, 1, now) == 0 and t2.asked == []

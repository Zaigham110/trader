"""Play a channel signal forward on our own 1-minute bid/ask bars (D-061).

The signal is taken exactly as first posted. A buy is filled at the ask and closed at the bid (a
sell the other way round), so the real spread is paid. The result is in R: multiples of the
distance between entry and stop. The first target is the exit; a channel that sets a tiny first
target to claim a high win rate shows up as small wins against full-size losses.

Rules, chosen to be simple and never kinder than a real account would be:
  * no stop in the message  -> `no_stop`, not scored (R cannot be defined);
  * prices more than 3% away from our market -> `off_market` (another instrument, or a typo);
  * "now" / no entry price  -> filled at the first bar after the post;
  * an entry price or zone  -> filled when the price first reaches it, within ENTRY_EXPIRY_S,
    otherwise `expired`; a signal whose stop is reached before its entry is `expired` too;
  * stop and target inside the same bar -> the stop counts;
  * still open after MAX_HOLD_S -> closed at the market (`timeout`).
"""

from __future__ import annotations

from typing import Any

from goldtrader.types import Bar

ENTRY_EXPIRY_S = 4 * 3600
MAX_HOLD_S = 3 * 86400
OFF_MARKET = 0.03
MARKET_GAP_S = 600
ZONE_TOL = 0.0003  # a quote this close to the zone counts as inside it (about 1 USD on gold)

FINAL = frozenset({"closed", "expired", "no_stop", "off_market", "late"})


def resolve(sig: dict[str, Any], bars: list[Bar], now_s: int) -> dict[str, Any] | None:
    """Column updates for one signal, or None when nothing new can be said yet.

    `sig` needs posted_ms, side, entry_lo, entry_hi, stop, targets (nearest first).
    `bars` are 1-minute bars in time order covering at least [posted, now].
    """
    posted_s = int(sig["posted_ms"]) // 1000
    long_ = int(sig["side"]) > 0
    stop = sig["stop"]
    if stop is None:
        return {"status": "no_stop"}
    later = [b for b in bars if b.time >= posted_s]
    if not later:
        return {"status": "expired", "exit_why": "no_prices"} if _old(posted_s, now_s) else None
    first = later[0]
    ref = (first.bid_open + first.ask_open) / 2.0
    lo, hi = sig["entry_lo"], sig["entry_hi"]
    near = [stop] + ([lo, hi] if lo is not None else [])
    if any(abs(p - ref) / ref > OFF_MARKET for p in near):
        return {"status": "off_market"}

    entry: float | None = None
    entry_i = 0
    check_stop = check_target = True
    if lo is None:  # at the market
        if first.time - posted_s > MARKET_GAP_S:
            return {"status": "expired", "exit_why": "market_closed"}
        entry = first.ask_open if long_ else first.bid_open
    else:
        tol = ZONE_TOL * ref
        for i, b in enumerate(later):
            if b.time - posted_s > ENTRY_EXPIRY_S:
                break
            px = b.ask_open if long_ else b.bid_open
            high = b.ask_high if long_ else b.bid_high
            low = b.ask_low if long_ else b.bid_low
            if lo - tol <= px <= hi + tol:
                entry, entry_i = px, i
            elif px > hi and low <= hi:  # came down into the zone: target order unknown this bar
                entry, entry_i, check_target = hi, i, False
            elif px < lo and high >= lo:  # rose into the zone: stop order unknown this bar
                entry, entry_i, check_stop = lo, i, False
            if entry is not None:
                break
            # the stop was reached before the entry: the idea is dead
            if (b.bid_low <= stop) if long_ else (b.ask_high >= stop):
                return {"status": "expired", "exit_why": "stop_before_entry"}
        if entry is None:
            if later[-1].time - posted_s > ENTRY_EXPIRY_S or _old(posted_s, now_s):
                return {"status": "expired", "exit_why": "entry_not_reached"}
            return None
    dist = (entry - stop) if long_ else (stop - entry)
    if dist <= 0:
        return {"status": "off_market"}
    targets = sig.get("targets") or []
    tp = targets[0] if targets else None
    if tp is not None and ((tp <= entry) if long_ else (tp >= entry)):
        tp = None  # the price had already passed the first target when the signal filled
    entry_bar = later[entry_i]
    base = {"entry_px": round(entry, 3), "entry_ms": entry_bar.time * 1000}

    def done(price: float, why: str, b: Bar) -> dict[str, Any]:
        r = ((price - entry) if long_ else (entry - price)) / dist
        return {
            **base,
            "status": "closed",
            "exit_px": round(price, 3),
            "exit_ms": (b.time + b.seconds) * 1000,
            "exit_why": why,
            "r": round(r, 3),
        }

    for j, b in enumerate(later[entry_i:]):
        at_fill = j == 0
        if not at_fill and b.time - entry_bar.time > MAX_HOLD_S:
            return done(b.bid_open if long_ else b.ask_open, "timeout", b)
        if long_:
            if (check_stop or not at_fill) and b.bid_low <= stop:
                return done(stop if at_fill else min(stop, b.bid_open), "stop", b)
            if tp is not None and (check_target or not at_fill) and b.bid_high >= tp:
                return done(tp, "target", b)
        else:
            if (check_stop or not at_fill) and b.ask_high >= stop:
                return done(stop if at_fill else max(stop, b.ask_open), "stop", b)
            if tp is not None and (check_target or not at_fill) and b.ask_low <= tp:
                return done(tp, "target", b)
    return {**base, "status": "filled"}


def _old(posted_s: int, now_s: int) -> bool:
    return now_s - posted_s > ENTRY_EXPIRY_S + 3 * 86400  # long enough to cover a weekend

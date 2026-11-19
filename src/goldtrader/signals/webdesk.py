"""Record trade plans from an analysis page that publishes them as JSON (D-071).

Used, with its owner's permission, for the "XAUUSD Analyst Desk" page: its feed lists independent
analysts (day, swing, tactical), each with a plan: direction, entry zone, invalidation and targets,
and a flag saying the plan's own rules are satisfied (`execution_ready`).

A plan is recorded once, the first time it is seen ready, and from then on it is an ordinary
channel signal: frozen as first seen, played forward on our own prices, alerted and shown to the
Strategy agent with its measured record. Only numbers and fixed words are taken from the feed; no
free text from it is stored or shown. Read-only: one GET a minute, nothing is ever sent.

The page prices gold on its own broker feed. Ours can differ by a fraction of a dollar, which
matters for the narrowest zones; the scorer's zone tolerance (about 1 USD) covers most of it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
import urllib.request
from collections.abc import Callable
from typing import Any

from goldtrader.signals.service import SignalService

log = logging.getLogger(__name__)

ANALYSTS = {"day": 1, "swing": 2, "tactical": 3}
ID_BASE = -900_000_000  # channel ids of web analysts; far from any Telegram chat id in use
POLL_S = 60
MAX_BYTES = 2_000_000
USER_AGENT = "goldtrader-signal-reader/1"

Fetch = Callable[[str], str]


def http_get(url: str) -> str:
    if not url.startswith("https://"):
        raise ValueError("only https sources are read")
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )  # noqa: S310
    with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - https only, checked above
        return r.read(MAX_BYTES).decode("utf-8", "replace")


def channel_id(source_id: int, analyst: str) -> int:
    return ID_BASE - source_id * 10 - ANALYSTS[analyst]


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and 0 < f < 1e6 else None


def ready_plans(payload: Any) -> list[dict[str, Any]]:
    """The plans whose own rules say "act now", as plain numbers. Anything malformed, not ready
    or geometrically impossible is left out."""
    try:
        plans = payload["analysis"]["independent_plans"]
    except (KeyError, TypeError):
        return []
    out: list[dict[str, Any]] = []
    for analyst in ANALYSTS:
        p = plans.get(analyst) if isinstance(plans, dict) else None
        if not isinstance(p, dict) or p.get("execution_ready") is not True:
            continue
        g = p.get("geometry")
        side = {"BUY": 1, "SELL": -1}.get(str(p.get("direction")))
        pid = p.get("plan_id")
        if not isinstance(g, dict) or side is None or not isinstance(pid, str) or not pid:
            continue
        lo, hi, stop = (
            _num(g.get("entry_low")),
            _num(g.get("entry_high")),
            _num(g.get("invalidation")),
        )
        targets = [t for t in (_num(x) for x in (g.get("targets") or [])[:6]) if t is not None]
        if lo is None or hi is None or stop is None or lo > hi or not targets:
            continue
        ok = (
            (stop < lo and all(t > hi for t in targets))
            if side > 0
            else (stop > hi and all(t < lo for t in targets))
        )
        if not ok:
            continue
        out.append(
            {
                "analyst": analyst,
                "plan_id": pid[:200],
                "side": side,
                "lo": lo,
                "hi": hi,
                "stop": stop,
                "targets": targets,
            }
        )
    return out


def plan_text(p: dict[str, Any]) -> str:
    """The plan in the wording our own parser reads. Built from numbers only."""
    side = "BUY" if p["side"] > 0 else "SELL"
    tps = " ".join(f"TP {t:.2f}" for t in p["targets"])
    return f"XAUUSD {side} {p['lo']:.2f}-{p['hi']:.2f} SL {p['stop']:.2f} {tps}"


def msg_id(plan_id: str) -> int:
    return int(hashlib.sha256(plan_id.encode()).hexdigest()[:12], 16) + (1 << 48)


def add_source(
    conn: sqlite3.Connection, svc: SignalService, name: str, url: str, now_ms: int
) -> int:
    if not url.startswith("https://"):
        raise ValueError("only https sources are read")
    conn.execute(
        "INSERT INTO signal_web_sources(name, url, active, added_ms) VALUES (?,?,1,?) "
        "ON CONFLICT(url) DO UPDATE SET name=excluded.name, active=1",
        (name[:80], url, now_ms),
    )
    sid = int(conn.execute("SELECT id FROM signal_web_sources WHERE url=?", (url,)).fetchone()[0])
    for analyst in ANALYSTS:
        svc.add_channel(channel_id(sid, analyst), f"{name}: {analyst}"[:120], gold_only=True)
    return sid


class DeskPoller:
    """`fetch_all` does the network part and may run in a worker thread; `apply` touches the
    database and runs where the service lives."""

    def __init__(
        self, svc: SignalService, conn: sqlite3.Connection, clock_ms: Callable[[], int],
        fetch: Fetch = http_get, every_s: int = POLL_S,
    ) -> None:  # fmt: skip
        self.svc, self.c, self.clock_ms, self.fetch, self.every_s = (
            svc,
            conn,
            clock_ms,
            fetch,
            every_s,
        )
        self._last_ms = 0
        self._sources: list[tuple[int, str]] = []

    def due(self) -> bool:
        now = self.clock_ms()
        if now - self._last_ms < self.every_s * 1000:
            return False
        self._last_ms = now
        self._sources = [
            (int(r[0]), str(r[1]))
            for r in self.c.execute("SELECT id, url FROM signal_web_sources WHERE active=1")
        ]
        return bool(self._sources)

    def fetch_all(self) -> list[tuple[int, Any, str | None]]:
        out: list[tuple[int, Any, str | None]] = []
        for sid, url in self._sources:
            try:
                out.append((sid, json.loads(self.fetch(url)), None))
            except Exception as e:  # noqa: BLE001 - a page being down must not stop the reader
                out.append((sid, None, f"{type(e).__name__}"[:80]))
        return out

    def apply(self, results: list[tuple[int, Any, str | None]]) -> int:
        now, n = self.clock_ms(), 0
        for sid, payload, err in results:
            if err is not None:
                self.c.execute("UPDATE signal_web_sources SET last_error=? WHERE id=?", (err, sid))
                continue
            self.c.execute(
                "UPDATE signal_web_sources SET last_ok_ms=?, last_error=NULL WHERE id=?", (now, sid)
            )
            watched = self.svc.watched()
            for p in ready_plans(payload):
                cid = channel_id(sid, p["analyst"])
                if cid not in watched:
                    continue
                if self.svc.on_message(cid, msg_id(p["plan_id"]), now, plan_text(p)) == "signal":
                    n += 1
        return n

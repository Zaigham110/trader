"""Per-channel scorecard read straight from the tables (D-061). sqlite only, so the read-only
dashboard can import it."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

MIN_FOR_VERDICT = 30
FRESH_MIN = 45  # a signal older than this is no longer shown to the Strategy agent


def record_text(conn: sqlite3.Connection, channel_id: int) -> str:
    """A channel's measured record in a few words, e.g. '12 finished, 5 won, total -3.20 R'."""
    n, wins, total = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(r > 0),0), COALESCE(SUM(r),0) FROM signals "
        "WHERE channel_id=? AND status='closed'",
        (channel_id,),
    ).fetchone()
    if not n:
        return "no finished signals measured yet"
    few = "" if n >= MIN_FOR_VERDICT else ", too few to judge"
    return f"{n} finished, {wins} won, total {total:+.2f} R{few}"


def describe(
    side: int, lo: float | None, hi: float | None, stop: float | None, targets: list
) -> str:
    """Numbers only, never the channel's own wording (channel text is untrusted)."""
    entry = "at market" if lo is None else f"{lo:g}" if lo == hi else f"{lo:g}-{hi:g}"
    out = f"{'BUY' if side > 0 else 'SELL'} gold, entry {entry}"
    out += f", stop {stop:g}" if stop is not None else ", no stop given"
    if targets:
        out += f", first target {targets[0]:g}"
    return out


def fresh_line(conn: sqlite3.Connection, now_ms: int, limit: int = 3) -> str:
    """Signals posted in the last FRESH_MIN minutes, for the Strategy agent's context (D-062)."""
    try:
        rows = conn.execute(
            "SELECT g.channel_id, c.name, g.posted_ms, g.side, g.entry_lo, g.entry_hi, g.stop, "
            "g.targets_json FROM signals g JOIN signal_channels c ON c.id=g.channel_id "
            "WHERE g.status IN ('open','filled','no_stop') AND g.posted_ms >= ? AND c.active=1 "
            "ORDER BY g.posted_ms DESC LIMIT ?",
            (now_ms - FRESH_MIN * 60_000, limit),
        ).fetchall()
    except sqlite3.OperationalError:  # database older than v13
        return ""
    parts = []
    for cid, name, posted_ms, side, lo, hi, stop, targets_json in rows:
        age = max(0, round((now_ms - posted_ms) / 60_000))
        what = describe(side, lo, hi, stop, json.loads(targets_json))
        parts.append(f"{name}, {age} min ago: {what} (channel record: {record_text(conn, cid)})")
    return "; ".join(parts)


def scorecard(conn: sqlite3.Connection, now_ms: int, recent: int = 25) -> dict[str, Any] | None:
    """None when the signal tables do not exist yet (database older than v13)."""
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='signal_channels'"
    ).fetchone():
        return None
    hb = conn.execute("SELECT last_ms FROM heartbeats WHERE process='signals'").fetchone()
    channels: list[dict[str, Any]] = []
    for cid, name, username, gold_only, active, added_ms in conn.execute(
        "SELECT id, name, username, gold_only, active, added_ms FROM signal_channels "
        "ORDER BY active DESC, name"
    ).fetchall():
        m = conn.execute(
            "SELECT COUNT(*), MAX(received_ms), SUM(parsed='unclear') FROM signal_messages "
            "WHERE channel_id=?",
            (cid,),
        ).fetchone()
        s = conn.execute(
            "SELECT COUNT(*), SUM(status='closed'), SUM(status='closed' AND r > 0), "
            "COALESCE(SUM(CASE WHEN status='closed' THEN r END),0), "
            "SUM(status IN ('open','filled')), SUM(status='expired'), SUM(status='no_stop'), "
            "SUM(status='off_market'), SUM(status='late') FROM signals WHERE channel_id=?",
            (cid,),
        ).fetchone()
        changed = conn.execute(
            "SELECT SUM(m.edit_count > 0), SUM(m.deleted_ms IS NOT NULL) FROM signals g "
            "JOIN signal_messages m ON m.channel_id=g.channel_id AND m.msg_id=g.msg_id "
            "WHERE g.channel_id=?",
            (cid,),
        ).fetchone()
        closed = int(s[1] or 0)
        channels.append(
            {
                "id": cid,
                "name": name,
                "username": username,
                "gold_only": bool(gold_only),
                "active": bool(active),
                "added_ms": added_ms,
                "messages": int(m[0] or 0),
                "last_message_ms": m[1],
                "unclear": int(m[2] or 0),
                "signals": int(s[0] or 0),
                "closed": closed,
                "wins": int(s[2] or 0),
                "sum_r": round(float(s[3] or 0.0), 2),
                "mean_r": round(float(s[3]) / closed, 3) if closed else None,
                "running": int(s[4] or 0),
                "expired": int(s[5] or 0),
                "no_stop": int(s[6] or 0),
                "off_market": int(s[7] or 0),
                "late": int(s[8] or 0),
                "edited": int(changed[0] or 0),
                "deleted": int(changed[1] or 0),
                "enough": closed >= MIN_FOR_VERDICT,
            }
        )
    rows = conn.execute(
        "SELECT g.posted_ms, c.name, g.side, g.entry_lo, g.entry_hi, g.stop, g.targets_json, "
        "g.parser, g.status, g.entry_px, g.exit_px, g.exit_why, g.r, m.text, m.edit_count, "
        "m.deleted_ms FROM signals g "
        "JOIN signal_messages m ON m.channel_id=g.channel_id AND m.msg_id=g.msg_id "
        "LEFT JOIN signal_channels c ON c.id=g.channel_id ORDER BY g.posted_ms DESC LIMIT ?",
        (recent,),
    ).fetchall()
    latest = [
        {
            "posted_ms": r[0],
            "channel": r[1] or "?",
            "side": "buy" if r[2] > 0 else "sell",
            "entry_lo": r[3],
            "entry_hi": r[4],
            "stop": r[5],
            "targets": json.loads(r[6]),
            "parser": r[7],
            "status": r[8],
            "entry_px": r[9],
            "exit_px": r[10],
            "exit_why": r[11],
            "r": r[12],
            "text": (r[13] or "")[:280],
            "edited": bool(r[14]),
            "deleted": r[15] is not None,
        }
        for r in rows
    ]
    return {
        "reader_last_ms": hb[0] if hb else None,
        "reader_running": bool(hb) and now_ms - int(hb[0]) < 120_000,
        "channels": channels,
        "latest": latest,
        "min_for_verdict": MIN_FOR_VERDICT,
    }

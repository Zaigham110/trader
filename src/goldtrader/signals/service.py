"""Record channel messages, extract signals, score them (D-061). No Telegram or model import here:
the Telegram adapter calls `on_message` / `on_edit` / `on_delete`, and the model is an injected
callable, so everything in this file runs in tests without a network.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from typing import Any

from goldtrader.db.repo import Repo
from goldtrader.signals import parse as P
from goldtrader.signals import report as R
from goldtrader.signals import score as S

log = logging.getLogger(__name__)

MAX_TEXT = 4000
LATE_S = 300  # a message first seen this long after it was posted is not scored
MAX_ALERTS_PER_HOUR = 8
AiFn = Callable[[str], tuple[Any, float]]
"""text -> (model output dict or None, cost in USD). Raises on failure."""


class SignalService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        symbol: str,
        clock_ms: Callable[[], int],
        *,
        ai: AiFn | None = None,
        ai_daily_cap_usd: float = 0.25,
        alerts: bool = True,
    ) -> None:
        self.c = conn
        self.symbol = symbol
        self.clock_ms = clock_ms
        self.ai = ai
        self.ai_daily_cap_usd = ai_daily_cap_usd
        self.alerts = alerts

    # ------------------------------------------------------------------------------- channels
    def watched(self) -> dict[int, bool]:
        """Active channel id -> gold_only."""
        return {
            int(r[0]): bool(r[1])
            for r in self.c.execute("SELECT id, gold_only FROM signal_channels WHERE active=1")
        }

    def add_channel(
        self, chat_id: int, name: str, *, username: str | None = None, gold_only: bool = False
    ) -> None:
        self.c.execute(
            "INSERT INTO signal_channels(id, name, username, gold_only, active, added_ms) "
            "VALUES (?,?,?,?,1,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
            "username=COALESCE(excluded.username, username), gold_only=excluded.gold_only, active=1",
            (chat_id, name[:120], username, int(gold_only), self.clock_ms()),
        )

    def remove_channel(self, chat_id: int) -> bool:
        cur = self.c.execute("UPDATE signal_channels SET active=0 WHERE id=?", (chat_id,))
        return cur.rowcount > 0

    # ------------------------------------------------------------------------------- messages
    def on_message(
        self, chat_id: int, msg_id: int, posted_ms: int, text: str, *, edited: bool = False
    ) -> str | None:
        """Store a message once and parse it. Returns its parse state, None if already known."""
        text = (text or "").strip()[:MAX_TEXT]
        if not text:
            return None
        now = self.clock_ms()
        cur = self.c.execute(
            "INSERT OR IGNORE INTO signal_messages(channel_id, msg_id, posted_ms, received_ms, "
            "text, seen_edited) VALUES (?,?,?,?,?,?)",
            (chat_id, msg_id, posted_ms, now, text, int(edited)),
        )
        if cur.rowcount == 0:
            return None
        gold_only = self.watched().get(chat_id, False)
        parsed, how, cost = self._parse(text, gold_only, now)
        state = "signal" if parsed else how
        self.c.execute(
            "UPDATE signal_messages SET parsed=?, ai_cost_usd=? WHERE channel_id=? AND msg_id=?",
            (state, cost, chat_id, msg_id),
        )
        if parsed is not None:
            late = edited or (now - posted_ms) > LATE_S * 1000
            self.c.execute(
                "INSERT OR IGNORE INTO signals(channel_id, msg_id, posted_ms, side, entry_lo, "
                "entry_hi, stop, targets_json, parser, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    chat_id,
                    msg_id,
                    posted_ms,
                    parsed.side,
                    parsed.entry_lo,
                    parsed.entry_hi,
                    parsed.stop,
                    json.dumps(list(parsed.targets)),
                    how,
                    "late" if late else "open",
                ),
            )
            if self.alerts and not late:
                self._alert(chat_id, parsed)
        return state

    def _alert(self, chat_id: int, p: P.Parsed) -> None:
        """Queue an alert; the brain's dispatcher sends it through the owner's bot (D-062)."""
        row = self.c.execute("SELECT name FROM signal_channels WHERE id=?", (chat_id,)).fetchone()
        name = row[0] if row else str(chat_id)
        recent = self.c.execute(
            "SELECT COUNT(*) FROM alerts WHERE kind='channel_signal' AND created_ms >= ?",
            (self.clock_ms() - 3_600_000,),
        ).fetchone()[0]
        if recent >= MAX_ALERTS_PER_HOUR:  # a noisy channel must not flood the owner's phone
            return
        what = R.describe(p.side, p.entry_lo, p.entry_hi, p.stop, list(p.targets))
        self.c.execute(
            "INSERT INTO alerts(created_ms, severity, kind, message, details_json) "
            "VALUES (?,'info','channel_signal',?,'{}')",
            (
                self.clock_ms(),
                f"{name}: {what}. Channel record: {R.record_text(self.c, chat_id)}. "
                "Measured only, not traded automatically.",
            ),
        )

    def _parse(self, text: str, gold_only: bool, now_ms: int) -> tuple[P.Parsed | None, str, float]:
        """(signal or None, 'rules' | 'ai' | 'not_signal' | 'unclear', model cost)."""
        got = P.parse(text, assume_gold=gold_only)
        if got is not None:
            return got, "rules", 0.0
        if not (P.is_gold(text, gold_only) and P.looks_like_signal(text)):
            return None, "not_signal", 0.0
        if self.ai is None or self._ai_spent_today(now_ms) >= self.ai_daily_cap_usd:
            return None, "unclear", 0.0
        try:
            out, cost = self.ai(text[:1500])
        except Exception as e:  # noqa: BLE001 - the model being down must not stop the reader
            log.warning("signal model call failed: %s", type(e).__name__)
            return None, "unclear", 0.0
        got = P.from_ai(out, text)
        if got is not None:
            return got, "ai", cost
        answered_no = isinstance(out, dict) and out.get("is_signal") is False
        return None, "not_signal" if answered_no else "unclear", cost

    def _ai_spent_today(self, now_ms: int) -> float:
        day = now_ms - now_ms % 86_400_000
        return float(
            self.c.execute(
                "SELECT COALESCE(SUM(ai_cost_usd),0) FROM signal_messages WHERE received_ms >= ?",
                (day,),
            ).fetchone()[0]
        )

    def on_edit(self, chat_id: int, msg_id: int, text: str, posted_ms: int | None = None) -> None:
        text = (text or "").strip()[:MAX_TEXT]
        row = self.c.execute(
            "SELECT text, last_text FROM signal_messages WHERE channel_id=? AND msg_id=?",
            (chat_id, msg_id),
        ).fetchone()
        if row is None:  # first seen as an edit: keep it, never score it
            if posted_ms is not None:
                self.on_message(chat_id, msg_id, posted_ms, text, edited=True)
            return
        if text == (row[1] if row[1] is not None else row[0]):
            return  # reactions and view counters also arrive as "edits"
        self.c.execute(
            "UPDATE signal_messages SET edit_count=edit_count+1, last_edit_ms=?, last_text=? "
            "WHERE channel_id=? AND msg_id=?",
            (self.clock_ms(), text, chat_id, msg_id),
        )

    def on_delete(self, chat_id: int | None, msg_ids: list[int]) -> int:
        """Telegram names the chat only for channels and supergroups; small groups share one id
        space, so a delete without a chat is matched against the watched small groups."""
        n = 0
        for mid in msg_ids:
            if chat_id is not None:
                cur = self.c.execute(
                    "UPDATE signal_messages SET deleted_ms=? WHERE channel_id=? AND msg_id=? "
                    "AND deleted_ms IS NULL",
                    (self.clock_ms(), chat_id, mid),
                )
            else:
                cur = self.c.execute(
                    "UPDATE signal_messages SET deleted_ms=? WHERE msg_id=? AND deleted_ms IS NULL "
                    "AND channel_id > -1000000000000",
                    (self.clock_ms(), mid),
                )
            n += cur.rowcount
        return n

    # --------------------------------------------------------------------------------- scoring
    def score(self) -> int:
        """Advance every unfinished signal on the stored bars. Returns how many rows changed."""
        rows = self.c.execute(
            "SELECT id, posted_ms, side, entry_lo, entry_hi, stop, targets_json, status "
            "FROM signals WHERE status IN ('open','filled') ORDER BY posted_ms"
        ).fetchall()
        if not rows:
            return 0
        now_s = self.clock_ms() // 1000
        start_s = int(rows[0][1]) // 1000 - 60
        bars = Repo(self.c).load_bars(self.symbol, 60, start_s, now_s + 60)
        changed = 0
        for r in rows:
            sig = {
                "posted_ms": r[1],
                "side": r[2],
                "entry_lo": r[3],
                "entry_hi": r[4],
                "stop": r[5],
                "targets": json.loads(r[6]),
            }
            upd = S.resolve(sig, bars, now_s)
            if not upd or (upd["status"] == r[7] == "filled"):
                continue
            cols = ", ".join(f"{k}=?" for k in upd)
            self.c.execute(f"UPDATE signals SET {cols} WHERE id=?", (*upd.values(), r[0]))  # noqa: S608
            changed += 1
        return changed


def make_ai(provider: Any) -> AiFn:
    """Wrap a ModelProvider (cheapest tier) as the fallback reader."""
    from goldtrader.llm.provider import ModelRequest

    def ask(text: str) -> tuple[Any, float]:
        resp = provider.complete(
            ModelRequest(
                tier="fast",
                system=P.AI_SYSTEM,
                messages=[{"role": "user", "content": "Message:\n" + text}],
                output_schema=P.ai_schema,
                max_tokens=300,
                timeout_s=20.0,
            )
        )
        return resp.parsed_output, float(resp.cost_usd)

    return ask

"""Alert outbox: ``alerts`` rows → Telegram, with cursors, quiet hours and bounded retries.

Pattern ported from the old platform's ``alerts.ts`` (research/05 §7): the dispatcher reads
``alerts`` rows whose ``delivered_ms IS NULL`` in id order, formats them, sends them to the owner
chat and marks ``delivered_ms``. Quiet hours hold back non-critical alerts (they stay pending until
the window ends); critical ones always go out (RISK_POLICY.md §7 step 1: "critical bypasses quiet
hours"). Each alert gets at most eight send attempts with exponential backoff, tracked in
``alert_deliveries`` (migration 003). :func:`format_trade_alert` builds the fill message required
by ARCHITECTURE.md §3.1 step 8 and D-017: the real "risk X% of equity" line plus a provenance line.
Texts are built from structured fields only — never raw news (R-024) — and pass through a
:class:`~goldtrader.notify.redact.Redactor` before leaving (R-053).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from goldtrader.notify.redact import Redactor
from goldtrader.notify.telegram import TelegramError, TelegramSender

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 8
BACKOFF_BASE_S = 30.0
BACKOFF_MAX_S = 3600.0
DETAILS_MAX_CHARS = 200
CURSOR_LAST_ID = "alerts_last_id"
CURSOR_LAST_RUN = "alerts_last_run_ms"

SEVERITY_ORDER = ("info", "warning", "error", "critical")


def _to_ms(now_utc: datetime) -> int:
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=UTC)
    return int(now_utc.timestamp() * 1000)


def _utc_hour(now_utc: datetime) -> int:
    if now_utc.tzinfo is None:
        return now_utc.hour
    return now_utc.astimezone(UTC).hour


def in_quiet_hours(now_utc: datetime, quiet_hours: tuple[int, int] | None) -> bool:
    """True when ``now_utc`` falls inside ``(start_hour, end_hour)`` UTC; wraps past midnight.

    ``None`` or ``start == end`` means no quiet window. The window is ``[start, end)``.
    """
    if quiet_hours is None:
        return False
    start, end = quiet_hours
    if start == end:
        return False
    h = _utc_hour(now_utc)
    if start < end:
        return start <= h < end
    return h >= start or h < end


def backoff_seconds(
    attempts: int, base_s: float = BACKOFF_BASE_S, max_s: float = BACKOFF_MAX_S
) -> float:
    """Delay before the next try after ``attempts`` failures: base × 2^(attempts−1), capped."""
    if attempts <= 0:
        return 0.0
    return min(max_s, base_s * (2 ** (attempts - 1)))


def _details_line(details_json: str | None, limit: int = DETAILS_MAX_CHARS) -> str:
    if not details_json:
        return ""
    try:
        details = json.loads(details_json)
    except (json.JSONDecodeError, TypeError):
        return str(details_json)[:limit]
    if not isinstance(details, Mapping) or not details:
        return ""
    parts = []
    for k in sorted(details):
        v = details[k]
        if isinstance(v, float):
            v = f"{v:.4g}"
        elif not isinstance(v, str | int | bool | type(None)):
            v = json.dumps(v, sort_keys=True, separators=(",", ":"), default=str)
        parts.append(f"{k}={v}")
    line = " · ".join(parts)
    return line if len(line) <= limit else line[: limit - 1] + "…"


def format_alert(row: Mapping[str, Any]) -> str:
    """``[SEVERITY] kind: message`` plus a short ``key=value`` details line when present."""
    sev = str(row.get("severity") or "info").upper()
    kind = str(row.get("kind") or "alert")
    message = str(row.get("message") or "").strip()
    text = f"[{sev}] {kind}: {message}"
    details = _details_line(row.get("details_json"))
    if details:
        text += f"\n{details}"
    return text


def _fmt_price(x: Any) -> str:
    try:
        return f"{float(x):.2f}"
    except (TypeError, ValueError):
        return str(x)


def format_trade_alert(trade: Mapping[str, Any]) -> str:
    """Telegram text for a fill (ARCHITECTURE.md §3.1 step 8).

    Mandatory lines (D-017: "every trade displays the real risk percentage"):

    * ``risk X% of equity`` from ``trade['risk_pct_of_equity']`` (a percentage, e.g. ``12.5``);
      a missing value raises ``ValueError`` — a trade alert without it must not be sent;
    * a provenance line from ``trade['provenance']`` or ``trade['source']`` + ``trade['as_of_ms']``
      (default source: ``MT5 order_send / history_deals_get``).

    Optional fields: ``symbol``, ``side``, ``volume``, ``entry_price``, ``sl``, ``tp``,
    ``risk_amount``, ``equity``, ``currency``, ``bundle_id``, ``order_ticket`` / ``position_id``,
    ``stop_atr``, ``target_atr``, ``mode`` (demo / live).
    """
    risk_pct = trade.get("risk_pct_of_equity")
    if risk_pct is None:
        raise ValueError("trade alert requires risk_pct_of_equity (D-017)")
    side = str(trade.get("side") or "?").upper()
    symbol = str(trade.get("symbol") or "XAUUSD")
    volume = trade.get("volume")
    mode = str(trade.get("mode") or "").upper()
    head = f"{'[' + mode + '] ' if mode else ''}Filled {side}"
    if volume is not None:
        head += f" {float(volume):.2f} lot"
    head += f" {symbol}"
    if trade.get("entry_price") is not None:
        head += f" @ {_fmt_price(trade['entry_price'])}"
    lines = [head]

    levels = []
    if trade.get("sl") is not None:
        s = f"stop {_fmt_price(trade['sl'])}"
        if trade.get("stop_atr") is not None:
            s += f" ({float(trade['stop_atr']):.1f} ATR)"
        levels.append(s)
    else:
        levels.append("stop MISSING")
    if trade.get("tp") is not None:
        t = f"target {_fmt_price(trade['tp'])}"
        if trade.get("target_atr") is not None:
            t += f" ({float(trade['target_atr']):.1f} ATR)"
        levels.append(t)
    lines.append(" · ".join(levels))

    risk_line = f"risk {float(risk_pct):.1f}% of equity"
    if trade.get("risk_amount") is not None and trade.get("equity") is not None:
        cur = str(trade.get("currency") or "USD")
        risk_line += (
            f" ({float(trade['risk_amount']):.2f} {cur} of {float(trade['equity']):.2f} {cur})"
        )
    lines.append(risk_line)

    ids = []
    if trade.get("bundle_id"):
        ids.append(f"bundle {trade['bundle_id']}")
    if trade.get("order_ticket") is not None:
        ids.append(f"ticket {trade['order_ticket']}")
    if trade.get("position_id") is not None:
        ids.append(f"position {trade['position_id']}")
    if ids:
        lines.append(" · ".join(ids))

    provenance = trade.get("provenance")
    if not provenance:
        source = trade.get("source") or "MT5 order_send / history_deals_get"
        as_of = trade.get("as_of_ms")
        when = (
            datetime.fromtimestamp(int(as_of) / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
            if as_of is not None
            else "now"
        )
        provenance = f"{source}, as of {when}"
    lines.append(f"source: {provenance}")
    return "\n".join(lines)


class AlertDispatcher:
    """Deliver pending ``alerts`` rows to the owner chat; see the module docstring.

    ``quiet_hours`` is ``(start_hour, end_hour)`` in UTC or ``None``. ``critical_kinds`` are alert
    kinds that bypass quiet hours in addition to rows with ``severity == 'critical'``
    (e.g. ``kill_switch``, ``incident``, ``stop_missing``). Delivery failures stop the current
    pass (one failure per pass keeps the attempt budget of the backlog intact when Telegram is down).
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        sender: TelegramSender,
        owner_chat_id: int | str,
        quiet_hours: tuple[int, int] | None = None,
        critical_kinds: Iterable[str] = (),
        *,
        max_attempts: int = MAX_ATTEMPTS,
        backoff_base_s: float = BACKOFF_BASE_S,
        backoff_max_s: float = BACKOFF_MAX_S,
        redactor: Redactor | None = None,
    ):
        self.c = conn
        self.sender = sender
        self.owner_chat_id = owner_chat_id
        self.quiet_hours = quiet_hours
        self.critical_kinds = set(critical_kinds)
        self.max_attempts = max_attempts
        self.backoff_base_s = backoff_base_s
        self.backoff_max_s = backoff_max_s
        self.redactor = redactor or Redactor()

    # --- queries --------------------------------------------------------------------------
    def is_critical(self, severity: str | None, kind: str | None) -> bool:
        return (severity or "").lower() == "critical" or (kind or "") in self.critical_kinds

    def pending(self) -> list[sqlite3.Row]:
        """Undelivered alerts that have not been given up on, oldest first."""
        return list(
            self.c.execute(
                "SELECT a.id, a.created_ms, a.severity, a.kind, a.message, a.details_json, "
                "COALESCE(d.attempts, 0) AS attempts, d.next_attempt_ms, d.gave_up_ms "
                "FROM alerts a LEFT JOIN alert_deliveries d ON d.alert_id = a.id "
                "WHERE a.delivered_ms IS NULL AND d.gave_up_ms IS NULL ORDER BY a.id"
            )
        )

    def cursor(self, name: str) -> int | None:
        row = self.c.execute("SELECT value FROM notify_cursors WHERE name=?", (name,)).fetchone()
        return int(row[0]) if row else None

    def _set_cursor(self, name: str, value: int, now_ms: int) -> None:
        self.c.execute(
            "INSERT INTO notify_cursors(name, value, updated_ms) VALUES (?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_ms=excluded.updated_ms",
            (name, value, now_ms),
        )

    # --- delivery -------------------------------------------------------------------------
    def dispatch_pending(self, now_utc: datetime) -> int:
        """Send every due alert; return how many were delivered in this pass."""
        now_ms = _to_ms(now_utc)
        quiet = in_quiet_hours(now_utc, self.quiet_hours)
        delivered = 0
        for row in self.pending():
            if quiet and not self.is_critical(row["severity"], row["kind"]):
                continue
            if row["next_attempt_ms"] is not None and row["next_attempt_ms"] > now_ms:
                continue
            text = self.redactor.redact(format_alert(dict(row)))
            try:
                self.sender.send(self.owner_chat_id, text)
            except TelegramError as e:
                self._record_failure(int(row["id"]), int(row["attempts"]), str(e), now_ms)
                break
            with self.c:
                self.c.execute(
                    "UPDATE alerts SET delivered_ms=? WHERE id=? AND delivered_ms IS NULL",
                    (now_ms, row["id"]),
                )
                self._set_cursor(CURSOR_LAST_ID, int(row["id"]), now_ms)
            delivered += 1
        with self.c:
            self._set_cursor(CURSOR_LAST_RUN, now_ms, now_ms)
        return delivered

    def _record_failure(self, alert_id: int, attempts: int, error: str, now_ms: int) -> None:
        attempts += 1
        gave_up = now_ms if attempts >= self.max_attempts else None
        next_ms = None
        if gave_up is None:
            next_ms = now_ms + int(
                backoff_seconds(attempts, self.backoff_base_s, self.backoff_max_s) * 1000
            )
        err = self.redactor.redact(error)[:500]
        with self.c:
            self.c.execute(
                "INSERT INTO alert_deliveries(alert_id, attempts, next_attempt_ms, last_error, "
                "gave_up_ms) VALUES (?,?,?,?,?) ON CONFLICT(alert_id) DO UPDATE SET "
                "attempts=excluded.attempts, next_attempt_ms=excluded.next_attempt_ms, "
                "last_error=excluded.last_error, gave_up_ms=excluded.gave_up_ms",
                (alert_id, attempts, next_ms, err, gave_up),
            )
        if gave_up is not None:
            log.error("alert %s given up after %s attempts: %s", alert_id, attempts, err)
        else:
            log.warning("alert %s send failed (attempt %s): %s", alert_id, attempts, err)

    def attempts(self, alert_id: int) -> int:
        row = self.c.execute(
            "SELECT attempts FROM alert_deliveries WHERE alert_id=?", (alert_id,)
        ).fetchone()
        return int(row[0]) if row else 0

    def gave_up(self, alert_id: int) -> bool:
        row = self.c.execute(
            "SELECT gave_up_ms FROM alert_deliveries WHERE alert_id=?", (alert_id,)
        ).fetchone()
        return bool(row and row[0] is not None)

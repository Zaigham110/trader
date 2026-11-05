"""`Reconciler`: broker truth vs journal, every 60 s and at start (R-008; ARCHITECTURE.md §2.1).

`run_once(now_ms)` compares `positions_get()` with the journal's open positions:

* a journaled position without a stop on the broker → `engine.ensure_stop()` with the journaled
  stop; no stop known → close it (R-004);
* a broker position the journal does not know (manual trade) → `unknown_position` incident and the
  position is adopted; it gets a protective stop at `stop_atr_max × atr` from the open price when
  `atr` is given, else an alert only;
* a journaled position no longer at the broker → its deals are ingested
  (`history_deals_get(position=…)`) and the trade is booked with the exit reason inferred from the
  OUT deal (`sl` → 'stop', `tp` → 'target', else 'closed'); net P&L equals the deal history
  (R-054);
* pending orders (the system places none) are reported as discrepancies;
* every deal since the persisted cursor (`reconcile_state.deals_cursor_ms`) is ingested.

The reconciler never calls `order_send` itself: repairs and closes go through the engine (R-003).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from goldtrader.execution.engine import ExecutionEngine
from goldtrader.execution.journal import Journal
from goldtrader.mt5 import constants as K
from goldtrader.mt5.protocol import MT5Client
from goldtrader.types import Deal, Position, SymbolSpec

DEALS_CURSOR = "deals_cursor_ms"


@dataclass
class ReconcileReport:
    positions_checked: int = 0
    stops_repaired: int = 0
    positions_closed_no_stop: int = 0
    unknown_positions: int = 0
    closed_detected: int = 0
    deals_ingested: int = 0
    discrepancies: list[str] = field(default_factory=list)


def exit_reason_from_deals(deals: list[Deal]) -> str:
    """'stop' / 'target' / 'closed' from the OUT deals' comments (Exness writes `[sl]` / `[tp]`)."""
    outs = [d for d in deals if d.entry in (K.DEAL_ENTRY_OUT, K.DEAL_ENTRY_OUT_BY)]
    for d in outs:
        c = d.comment.lower()
        if "sl" in c:
            return "stop"
        if "tp" in c:
            return "target"
    return "closed"


class Reconciler:
    def __init__(
        self, client: MT5Client, spec: SymbolSpec, journal: Journal, engine: ExecutionEngine
    ) -> None:
        self.client = client
        self.spec = spec
        self.journal = journal
        self.engine = engine

    # ------------------------------------------------------------------ main entry point
    def run_once(self, now_ms: int, *, atr: float | None = None) -> ReconcileReport:
        report = ReconcileReport()
        broker = {p.ticket: p for p in self.client.positions_get(symbol=self.spec.name)}
        journaled = {int(r["position_id"]): r for r in self.journal.open_positions()}

        for ticket, pos in broker.items():
            row = journaled.get(ticket)
            if row is None:
                self._adopt(pos, atr, report)
                continue
            report.positions_checked += 1
            self.journal.touch_position(ticket, now_ms)
            if abs(pos.volume - row["volume"]) > 1e-9:
                report.discrepancies.append(
                    f"position {ticket}: broker volume {pos.volume} != journal {row['volume']}"
                )
            if not pos.sl:
                self._repair_stop(pos, row["sl"], report)
            elif row["sl"] is not None and abs(pos.sl - row["sl"]) >= self.spec.point / 2.0:
                report.discrepancies.append(
                    f"position {ticket}: broker sl {pos.sl} != journal {row['sl']}; journal updated"
                )
                self.journal.set_position_stop(ticket, pos.sl, pos.tp or None)
            elif (pos.tp or None) != row["tp"]:
                self.journal.set_position_stop(ticket, pos.sl, pos.tp or None)

        for ticket in journaled:
            if ticket not in broker:
                self._book_closed(ticket, report)

        for order in self.client.orders_get(symbol=self.spec.name):
            report.discrepancies.append(
                f"pending order {order.ticket} ({order.type}) exists; the system places none"
            )

        report.deals_ingested += self._ingest_since_cursor(now_ms)
        if report.discrepancies:
            self.journal.alert(
                "warning",
                "reconcile_discrepancy",
                "; ".join(report.discrepancies)[:1000],
                {"count": len(report.discrepancies)},
            )
        return report

    # ------------------------------------------------------------------ steps
    def _repair_stop(self, pos: Position, journaled_sl: float | None, report: ReconcileReport):
        if journaled_sl and self.engine.ensure_stop(pos.ticket, journaled_sl):
            report.stops_repaired += 1
            report.discrepancies.append(f"position {pos.ticket}: stop missing, repaired")
            return
        self.journal.record_incident(
            "stop_missing",
            "critical",
            f"position {pos.ticket} has no stop and it could not be repaired; closing (R-004)",
        )
        self.journal.alert("critical", "stop_missing", f"position {pos.ticket} closed: no stop")
        res = self.engine.close(pos.ticket, "no_stop")
        if res.status == "executed":
            report.positions_closed_no_stop += 1
        else:
            report.discrepancies.append(f"position {pos.ticket}: close failed: {res.detail}")

    def _adopt(self, pos: Position, atr: float | None, report: ReconcileReport) -> None:
        report.unknown_positions += 1
        self.journal.record_incident(
            "unknown_position",
            "warning",
            f"position {pos.ticket} ({pos.side} {pos.volume} @ {pos.price_open}) not in journal",
        )
        self.journal.upsert_position(pos, source="adopted")
        self._ingest_position_deals(pos.ticket)
        if pos.sl:
            report.discrepancies.append(f"position {pos.ticket}: unknown, adopted (has stop)")
            return
        if atr is None or atr <= 0:
            self.journal.alert(
                "critical",
                "unknown_position_no_stop",
                f"position {pos.ticket} adopted without a stop and no ATR to place one",
            )
            report.discrepancies.append(f"position {pos.ticket}: unknown, adopted, NO STOP")
            return
        dist = self.engine.cfg.owner.risk.stop_atr_max * atr
        stop = pos.price_open - dist if pos.type == K.POSITION_TYPE_BUY else pos.price_open + dist
        stop = round(stop, self.spec.digits)
        if self.engine.ensure_stop(pos.ticket, stop):
            report.stops_repaired += 1
            report.discrepancies.append(f"position {pos.ticket}: unknown, adopted, stop {stop}")
        else:
            self.journal.alert(
                "critical",
                "unknown_position_no_stop",
                f"position {pos.ticket} adopted; protective stop {stop} could not be set",
            )
            report.discrepancies.append(f"position {pos.ticket}: unknown, adopted, stop FAILED")

    def _book_closed(self, ticket: int, report: ReconcileReport) -> None:
        deals = self._ingest_position_deals(ticket)
        report.deals_ingested += deals[0]
        all_deals = deals[1]
        row = self.journal.position(ticket)
        reason = exit_reason_from_deals(all_deals)
        requested = None
        if row is not None:
            requested = row["sl"] if reason == "stop" else row["tp"] if reason == "target" else None
        tick = self.client.symbol_info_tick(self.spec.name)
        spread = tick.spread if tick is not None else None
        trade = self.journal.book_trade(
            ticket, reason, exit_requested_price=requested, exit_spread=spread
        )
        if trade is None:
            report.discrepancies.append(f"position {ticket}: gone from broker, no exit deal found")
            self.journal.mark_position_closed(ticket, "unknown")
        else:
            report.closed_detected += 1
            self.engine.throttle.forget(ticket)

    def _ingest_position_deals(self, ticket: int) -> tuple[int, list[Deal]]:
        deals = self.client.history_deals_get(
            datetime.fromtimestamp(0, UTC),
            datetime.fromtimestamp(4_102_444_800, UTC),
            position=ticket,
        )
        new = sum(1 for d in deals if self.journal.record_fill(d))
        return new, deals

    def _ingest_since_cursor(self, now_ms: int) -> int:
        cursor = self.journal.get_cursor(DEALS_CURSOR, 0)
        deals = self.client.history_deals_get(
            datetime.fromtimestamp(cursor / 1000.0, UTC),
            datetime.fromtimestamp(now_ms / 1000.0 + 1.0, UTC),
        )
        new = 0
        latest = cursor
        for d in deals:
            if d.symbol != self.spec.name or d.time_msc < cursor:
                continue
            if self.journal.record_fill(d):
                new += 1
            latest = max(latest, d.time_msc + 1)
        if latest != cursor:
            self.journal.set_cursor(DEALS_CURSOR, latest)
        return new


__all__ = ["DEALS_CURSOR", "ReconcileReport", "Reconciler", "exit_reason_from_deals"]

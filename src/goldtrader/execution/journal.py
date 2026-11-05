"""`Journal`: the execution engine's write path into SQLite (docs/ARCHITECTURE.md §2.1 `journal`).

Writes `order_attempts` / `orders` (every outbound request and result, requested vs filled price,
slippage — R-007), `fills` (one row per broker deal), `positions` (upsert, open/closed, last seen),
`trades` (one closed round trip per position, net of all costs — R-054), `market_snapshots` (one
row per order), `incidents`, `alerts`, `intents` (idempotency) and the reconciler cursors
(`reconcile_state`, R-008). Schema: migrations 001 and 004.

Cost attribution of a closed trade (R-054), so that it reconciles to `history_deals_get()`:

* `gross_pnl`  = Σ `deal.profit` over the position's deals. MT5 computes `profit` from the actual
  fill prices, so the spread and any slippage are already *inside* this number.
* `commission` = Σ `deal.commission`, `swap` = Σ `deal.swap` (both are signed by the broker, costs
  negative).
* `net_pnl`    = gross_pnl + commission + swap — exactly the sum the broker's history shows for the
  position, the reconciliation target.
* `spread_cost` = (half-spread at entry + half-spread at exit) × volume × contract: what the trade
  paid for crossing from mid to the touch at each fill, measured against the bid/ask recorded at
  fill time (or, for a broker-side close the reconciler ingests, the spread of the tick at
  ingestion — flagged in the trade's `exit_reason` docs, not in the number).
* `slippage_cost` = (|entry fill − entry requested| + |exit fill − exit requested|) × volume ×
  contract. "Requested" is the price in our `OrderRequest` for engine fills and the SL / TP level
  for broker-side closes.
* Hence the mid-to-mid "signal" P&L is `gross_pnl + spread_cost ± slippage`; spread and slippage
  are *attribution* of the gross number, never added to it again.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from goldtrader.contracts import Intent, IntentResult
from goldtrader.db.connect import now_ms
from goldtrader.mt5 import constants as K
from goldtrader.risk.types import MarketState
from goldtrader.types import Deal, OrderCheckResult, OrderRequest, OrderResult, Position

KILL_INCIDENT_KIND = "kill_switch"


def _stable(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _vwap(rows: list[sqlite3.Row]) -> tuple[float, float]:
    """(total volume, volume-weighted price) over fill rows."""
    vol = sum(r["volume"] for r in rows)
    if vol <= 0:
        return 0.0, 0.0
    return vol, sum(r["volume"] * r["price"] for r in rows) / vol


def slippage_points(order_type: int, requested: float, filled: float, point: float) -> float:
    """Signed slippage in points, adverse positive: a BUY filled above / a SELL filled below."""
    if not requested or not filled or point <= 0:
        return 0.0
    diff = filled - requested if order_type == K.ORDER_TYPE_BUY else requested - filled
    return round(diff / point, 3)


class Journal:
    """SQLite journal for the execution engine and reconciler. One connection, explicit SQL."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], int] | None = None,
        point: float = 0.01,
        digits: int = 2,
        contract_size: float = 100.0,
    ) -> None:
        self.c = conn
        self.clock = clock or now_ms
        self.point = point
        self.digits = digits
        self.contract_size = contract_size

    # ------------------------------------------------------------------ order attempts / orders
    def record_attempt(
        self,
        *,
        kind: str,
        stage: str,
        request: OrderRequest | None,
        result: OrderResult | OrderCheckResult | None,
        attempt: int = 1,
        intent_id: str | None = None,
        bundle_id: str | None = None,
        position_id: int | None = None,
        risk_code: str | None = None,
        requested_price: float | None = None,
    ) -> int:
        """One row per outbound request / result (or per risk rejection, stage='risk')."""
        filled: float | None = None
        slip: float | None = None
        ticket: int | None = None
        volume: float | None = request.volume if request is not None else None
        retcode: int | None = result.retcode if result is not None else None
        if isinstance(result, OrderResult):
            ticket = result.order or None
            if result.price and request is not None:
                filled = result.price
                slip = slippage_points(request.type, requested_price or 0.0, filled, self.point)
            if result.volume:
                volume = result.volume
        cur = self.c.execute(
            "INSERT INTO order_attempts(sent_ms, kind, stage, attempt, intent_id, bundle_id, "
            "position_id, order_ticket, request_json, result_json, retcode, risk_code, "
            "requested_price, filled_price, slippage_points, volume) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.clock(),
                kind,
                stage,
                attempt,
                intent_id,
                bundle_id,
                position_id,
                ticket,
                _stable(request.to_mt5()) if request is not None else None,
                _stable(asdict(result)) if result is not None else None,
                retcode,
                risk_code,
                requested_price,
                filled,
                slip,
                volume,
            ),
        )
        return int(cur.lastrowid)

    def record_order(
        self,
        request: OrderRequest,
        result: OrderResult,
        *,
        kind: str,
        intent_id: str | None,
        bundle_id: str | None,
        position_id: int | None,
        requested_price: float,
    ) -> None:
        """`orders` row for a request the broker gave a ticket (R-007: requested vs filled)."""
        if not result.order:
            return
        filled = result.price or None
        slip = (
            slippage_points(request.type, requested_price, filled, self.point)
            if filled is not None
            else None
        )
        self.c.execute(
            "INSERT OR REPLACE INTO orders(order_ticket, intent_id, bundle_id, sent_ms, "
            "request_json, result_json, retcode, requested_price, filled_price, slippage_points, "
            "volume, kind, action, position_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                result.order,
                intent_id,
                bundle_id,
                self.clock(),
                _stable(request.to_mt5()),
                _stable(asdict(result)),
                result.retcode,
                requested_price,
                filled,
                slip,
                result.volume or request.volume,
                kind,
                request.action,
                position_id,
            ),
        )

    def requested_price(self, order_ticket: int) -> float | None:
        row = self.c.execute(
            "SELECT requested_price FROM orders WHERE order_ticket=?", (order_ticket,)
        ).fetchone()
        return None if row is None or row[0] is None else float(row[0])

    # ------------------------------------------------------------------ fills
    def record_fill(
        self, deal: Deal, *, bid: float | None = None, ask: float | None = None
    ) -> bool:
        """Insert a deal as a fill; returns False when the deal ticket was already journaled."""
        cur = self.c.execute(
            "INSERT OR IGNORE INTO fills(deal_ticket, order_ticket, position_id, time_ms, type, "
            "entry, volume, price, commission, swap, profit, bid_at_fill, ask_at_fill) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                deal.ticket,
                deal.order,
                deal.position_id,
                deal.time_msc,
                deal.type,
                deal.entry,
                deal.volume,
                deal.price,
                deal.commission,
                deal.swap,
                deal.profit,
                bid,
                ask,
            ),
        )
        return cur.rowcount == 1

    def fills_for(self, position_id: int) -> list[sqlite3.Row]:
        return list(
            self.c.execute(
                "SELECT * FROM fills WHERE position_id=? ORDER BY time_ms, deal_ticket",
                (position_id,),
            )
        )

    # ------------------------------------------------------------------ positions
    def upsert_position(
        self,
        pos: Position,
        *,
        bundle_id: str | None = None,
        proposal_id: str | None = None,
        intent_id: str | None = None,
        initial_stop: float | None = None,
        risk_pct: float | None = None,
        risk_usd: float | None = None,
        source: str = "engine",
    ) -> None:
        """Insert or refresh an open position; identity columns are only set when given."""
        now = self.clock()
        self.c.execute(
            "INSERT INTO positions(position_id, bundle_id, symbol, side, volume, price_open, sl, "
            "tp, opened_ms, status, last_seen_ms, proposal_id, intent_id, magic, initial_stop, "
            "risk_pct, risk_usd, source) VALUES (?,?,?,?,?,?,?,?,?,'open',?,?,?,?,?,?,?,?) "
            "ON CONFLICT(position_id) DO UPDATE SET volume=excluded.volume, sl=excluded.sl, "
            "tp=excluded.tp, last_seen_ms=excluded.last_seen_ms, status='open', "
            "bundle_id=COALESCE(excluded.bundle_id, positions.bundle_id), "
            "proposal_id=COALESCE(excluded.proposal_id, positions.proposal_id), "
            "intent_id=COALESCE(excluded.intent_id, positions.intent_id), "
            "initial_stop=COALESCE(positions.initial_stop, excluded.initial_stop), "
            "risk_pct=COALESCE(positions.risk_pct, excluded.risk_pct), "
            "risk_usd=COALESCE(positions.risk_usd, excluded.risk_usd)",
            (
                pos.ticket,
                bundle_id,
                pos.symbol,
                pos.side,
                pos.volume,
                pos.price_open,
                pos.sl or None,
                pos.tp or None,
                pos.time_msc,
                now,
                proposal_id,
                intent_id,
                pos.magic,
                initial_stop,
                risk_pct,
                risk_usd,
                source,
            ),
        )

    def set_position_stop(self, position_id: int, sl: float | None, tp: float | None) -> None:
        self.c.execute(
            "UPDATE positions SET sl=?, tp=?, last_seen_ms=? WHERE position_id=?",
            (sl or None, tp or None, self.clock(), position_id),
        )

    def touch_position(self, position_id: int, seen_ms: int | None = None) -> None:
        self.c.execute(
            "UPDATE positions SET last_seen_ms=? WHERE position_id=?",
            (seen_ms if seen_ms is not None else self.clock(), position_id),
        )

    def mark_position_closed(
        self, position_id: int, exit_reason: str, closed_ms: int | None = None
    ) -> None:
        self.c.execute(
            "UPDATE positions SET status='closed', closed_ms=?, exit_reason=? WHERE position_id=?",
            (closed_ms if closed_ms is not None else self.clock(), exit_reason, position_id),
        )

    def position(self, position_id: int) -> sqlite3.Row | None:
        return self.c.execute(
            "SELECT * FROM positions WHERE position_id=?", (position_id,)
        ).fetchone()

    def open_positions(self) -> list[sqlite3.Row]:
        return list(
            self.c.execute("SELECT * FROM positions WHERE status='open' ORDER BY opened_ms")
        )

    # ------------------------------------------------------------------ trades (R-054)
    def book_trade(
        self,
        position_id: int,
        exit_reason: str,
        *,
        exit_requested_price: float | None = None,
        exit_spread: float | None = None,
    ) -> dict[str, Any] | None:
        """Book the closed round trip of `position_id` from its fills (see the module docstring).

        `exit_requested_price` is the SL / TP level for broker-side closes whose order we never
        sent; `exit_spread` is the spread to assume for exit fills that carry no bid/ask (the
        reconciler passes the spread of the current tick). Returns the trade row as a dict, or
        None when the position has no exit fill yet.
        """
        pos = self.position(position_id)
        if pos is None:
            return None
        fills = self.fills_for(position_id)
        entries = [f for f in fills if f["entry"] in (K.DEAL_ENTRY_IN, K.DEAL_ENTRY_INOUT)]
        exits = [f for f in fills if f["entry"] in (K.DEAL_ENTRY_OUT, K.DEAL_ENTRY_OUT_BY)]
        if not exits:
            return None
        contract = self.contract_size
        volume, entry_price = _vwap(entries)
        if not entries:
            volume, entry_price = sum(f["volume"] for f in exits), pos["price_open"]
        exit_volume, exit_price = _vwap(exits)
        gross = sum(f["profit"] for f in fills)
        commission = sum(f["commission"] for f in fills)
        swap = sum(f["swap"] for f in fills)
        net = gross + commission + swap

        spread_cost = 0.0
        for f in entries:
            if f["bid_at_fill"] and f["ask_at_fill"]:
                spread_cost += (f["ask_at_fill"] - f["bid_at_fill"]) / 2.0 * f["volume"] * contract
        for f in exits:
            if f["bid_at_fill"] and f["ask_at_fill"]:
                half = (f["ask_at_fill"] - f["bid_at_fill"]) / 2.0
            elif exit_spread is not None:
                half = exit_spread / 2.0
            else:
                half = 0.0
            spread_cost += half * f["volume"] * contract

        slippage_cost = 0.0
        for f in entries:
            req_px = self.requested_price(f["order_ticket"]) if f["order_ticket"] else None
            if req_px:
                slippage_cost += abs(f["price"] - req_px) * f["volume"] * contract
        for f in exits:
            req_px = self.requested_price(f["order_ticket"]) if f["order_ticket"] else None
            if req_px is None:
                req_px = exit_requested_price
            if req_px:
                slippage_cost += abs(f["price"] - req_px) * f["volume"] * contract

        r_multiple: float | None = None
        initial_stop = pos["initial_stop"]
        if initial_stop and entry_price:
            risk_usd = abs(entry_price - initial_stop) * volume * contract
            if risk_usd > 0:
                r_multiple = net / risk_usd

        row = {
            "trade_id": f"T{position_id}",
            "position_id": position_id,
            "bundle_id": pos["bundle_id"],
            "proposal_id": pos["proposal_id"],
            "side": pos["side"],
            "volume": round(exit_volume or volume, 8),
            "entry_ms": entries[0]["time_ms"] if entries else pos["opened_ms"],
            "exit_ms": exits[-1]["time_ms"],
            "entry_price": round(entry_price, self.digits),
            "exit_price": round(exit_price, self.digits),
            "gross_pnl": round(gross, 2),
            "spread_cost": round(spread_cost, 4),
            "slippage_cost": round(slippage_cost, 4),
            "commission": round(commission, 2),
            "swap": round(swap, 2),
            "net_pnl": round(net, 2),
            "r_multiple": r_multiple,
            "exit_reason": exit_reason,
            "risk_pct_of_equity": pos["risk_pct"],
        }
        cols = list(row)
        self.c.execute(
            f"INSERT OR REPLACE INTO trades({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [row[c] for c in cols],
        )
        self.mark_position_closed(position_id, exit_reason, closed_ms=row["exit_ms"])
        return row

    def trade(self, position_id: int) -> sqlite3.Row | None:
        return self.c.execute("SELECT * FROM trades WHERE position_id=?", (position_id,)).fetchone()

    # ------------------------------------------------------------------ market snapshots
    def record_snapshot(
        self,
        ref_type: str,
        ref_id: str,
        market: MarketState,
        *,
        regime: str | None = None,
        minutes_to_event: float | None = None,
        next_event: str | None = None,
    ) -> int:
        cur = self.c.execute(
            "INSERT INTO market_snapshots(time_ms, ref_type, ref_id, bid, ask, spread_pips, atr, "
            "regime, session_state, minutes_to_event, next_event) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.clock(),
                ref_type,
                ref_id,
                market.bid,
                market.ask,
                market.spread_pips,
                market.atr,
                regime if regime is not None else market.spread_regime,
                market.session.value,
                minutes_to_event,
                next_event,
            ),
        )
        return int(cur.lastrowid)

    # ------------------------------------------------------------------ incidents / alerts
    def record_incident(self, kind: str, severity: str, summary: str) -> int:
        cur = self.c.execute(
            "INSERT INTO incidents(opened_ms, severity, kind, summary) VALUES (?,?,?,?)",
            (self.clock(), severity, kind, summary),
        )
        return int(cur.lastrowid)

    def open_incident(self, kind: str) -> sqlite3.Row | None:
        return self.c.execute(
            "SELECT * FROM incidents WHERE kind=? AND closed_ms IS NULL ORDER BY id DESC LIMIT 1",
            (kind,),
        ).fetchone()

    def close_incidents(self, kind: str) -> int:
        now = self.clock()
        cur = self.c.execute(
            "UPDATE incidents SET closed_ms=?, acknowledged_ms=COALESCE(acknowledged_ms, ?) "
            "WHERE kind=? AND closed_ms IS NULL",
            (now, now, kind),
        )
        return cur.rowcount

    def alert(self, severity: str, kind: str, message: str, details: dict | None = None) -> int:
        cur = self.c.execute(
            "INSERT INTO alerts(created_ms, severity, kind, message, details_json) "
            "VALUES (?,?,?,?,?)",
            (self.clock(), severity, kind, message, _stable(details or {})),
        )
        return int(cur.lastrowid)

    def alerts(self, kind: str | None = None) -> list[sqlite3.Row]:
        if kind is None:
            return list(self.c.execute("SELECT * FROM alerts ORDER BY id"))
        return list(self.c.execute("SELECT * FROM alerts WHERE kind=? ORDER BY id", (kind,)))

    def kill_active(self) -> bool:
        """The kill switch persists as an open `kill_switch` incident (RISK_POLICY.md §3)."""
        return self.open_incident(KILL_INCIDENT_KIND) is not None

    # ------------------------------------------------------------------ intents
    def record_intent(self, intent: Intent) -> None:
        self.c.execute(
            "INSERT OR IGNORE INTO intents(intent_id, created_ms, proposal_id, kind, payload_json, "
            "status, source) VALUES (?,?,?,?,?,'received',?)",
            (
                intent.intent_id,
                intent.created_ms,
                intent.proposal_id,
                intent.kind,
                intent.model_dump_json(),
                intent.source,
            ),
        )

    def finish_intent(self, result: IntentResult) -> None:
        self.c.execute(
            "UPDATE intents SET status=?, risk_code=?, handled_ms=?, result_json=? WHERE intent_id=?",
            (
                result.status,
                result.risk_code,
                self.clock(),
                result.model_dump_json(),
                result.intent_id,
            ),
        )

    def intent_result(self, intent_id: str) -> IntentResult | None:
        row = self.c.execute(
            "SELECT result_json FROM intents WHERE intent_id=?", (intent_id,)
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return IntentResult.model_validate_json(row[0])

    # ------------------------------------------------------------------ reconciler cursors
    def get_cursor(self, name: str, default: int = 0) -> int:
        row = self.c.execute("SELECT value FROM reconcile_state WHERE name=?", (name,)).fetchone()
        return default if row is None else int(row[0])

    def set_cursor(self, name: str, value: int) -> None:
        self.c.execute(
            "INSERT INTO reconcile_state(name, value, updated_ms) VALUES (?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_ms=excluded.updated_ms",
            (name, value, self.clock()),
        )


__all__ = ["KILL_INCIDENT_KIND", "Journal", "slippage_points"]

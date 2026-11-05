"""`ExecutionEngine`: THE ONLY ORDER PLACER (R-003; docs/ARCHITECTURE.md §2.1 `execution`).

Every entry runs `check_entry`, then `order_calc_margin`, then `order_check`, then `order_send` with
SL/TP attached; every stop move runs `check_stop_move`; every close runs `check_close`. The risk
engine is never bypassed (D-006). After a fill the engine verifies the broker holds the stop and,
when it cannot be set, closes the position at once and records a critical incident (R-004). Every
outbound request and result is journaled with requested vs filled price (R-007). Deterministic
client-side identity: `magic` = 31-bit hash of the bundle id, `comment` = proposal id.

Retcodes (docs/MT5_EXNESS_INTEGRATION.md §4, docs/ARCHITECTURE.md §5): ok → journal; transient →
one retry after `Throttle.backoff()` with a refreshed price (two attempts in all, longer backoff on
10024); fatal → failed, journaled, alert for 10019 / 10016 / 10030 / 10017 / 10027; 10010
DONE_PARTIAL → the actual volume is journaled and protected.

Kill switch (RISK_POLICY.md §3): `kill()` closes everything, cancels pending orders and blocks
entries until `reset_kill()` is called with an owner-confirmed token; the state persists as an open
`kill_switch` incident in the journal so a restart starts killed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from goldtrader.config.schema import Config
from goldtrader.contracts import IntentResult
from goldtrader.execution.journal import KILL_INCIDENT_KIND, Journal
from goldtrader.execution.throttle import Throttle
from goldtrader.mt5 import constants as K
from goldtrader.mt5.protocol import MT5Client, classify_retcode
from goldtrader.risk.engine import check_close, check_entry, check_stop_move
from goldtrader.risk.types import (
    AccountState,
    Approved,
    EntryRequest,
    MarketState,
    RejectCode,
    Rejected,
    RiskContext,
    StopMoveRequest,
)
from goldtrader.types import (
    Deal,
    OrderRequest,
    OrderResult,
    Position,
    SessionState,
    SymbolSpec,
    Tick,
)

DEFAULT_DEVIATION_POINTS = 10
MAX_ATTEMPTS = 2
#: fatal retcodes that page the owner (docs/ARCHITECTURE.md §5 "Order rejected")
ALERT_RETCODES = frozenset(
    {
        K.TRADE_RETCODE_NO_MONEY,
        K.TRADE_RETCODE_INVALID_STOPS,
        K.TRADE_RETCODE_INVALID_FILL,
        K.TRADE_RETCODE_TRADE_DISABLED,
        K.TRADE_RETCODE_CLIENT_DISABLES_AT,
    }
)


def magic_from_bundle(bundle_id: str) -> int:
    """Stable 31-bit magic number for a bundle id (same id → same magic on every machine)."""
    digest = hashlib.sha256(bundle_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def filling_for(spec: SymbolSpec) -> int:
    """ORDER_FILLING_* from the probed `filling_mode` bitmask (KU-012): FOK, else IOC, else RETURN."""
    if spec.filling_mode & K.SYMBOL_FILLING_FOK:
        return K.ORDER_FILLING_FOK
    if spec.filling_mode & K.SYMBOL_FILLING_IOC:
        return K.ORDER_FILLING_IOC
    return K.ORDER_FILLING_RETURN


def _retcode_text(res: OrderResult) -> str:
    return f"{K.retcode_name(res.retcode)} ({res.retcode}) {res.comment}".strip()


class ExecutionEngine:
    """See the module docstring. One instance per gateway process; not thread-safe by design."""

    def __init__(
        self,
        client: MT5Client,
        spec: SymbolSpec,
        cfg: Config,
        journal: Journal,
        *,
        clock: Callable[[], int],
        throttle: Throttle | None = None,
        deviation_points: int = DEFAULT_DEVIATION_POINTS,
    ) -> None:
        self.client = client
        self.spec = spec
        self.cfg = cfg
        self.journal = journal
        self.clock = clock
        self.throttle = throttle or Throttle(clock=clock)
        self.deviation = deviation_points
        self.killed: bool = journal.kill_active()

    # ================================================================== entries (R-003, R-004)
    def enter(
        self,
        req: EntryRequest,
        acct: AccountState,
        market: MarketState,
        ctx: RiskContext,
        *,
        intent_id: str | None = None,
        regime: str | None = None,
        minutes_to_event: float | None = None,
    ) -> IntentResult:
        """Open a position: check_entry → order_calc_margin → order_check → order_send → stop check."""
        iid = intent_id or req.proposal_id
        ctx = replace(ctx, kill_switch=ctx.kill_switch or self.killed)
        verdict = check_entry(req, acct, market, self.cfg, ctx)
        if isinstance(verdict, Rejected):
            return self._risk_reject("entry", iid, req.bundle_id, verdict)
        assert isinstance(verdict, Approved)
        tick = self._tick()
        if tick is None:
            return self._fail(iid, "no_tick")

        order_type = K.ORDER_TYPE_BUY if req.side == "long" else K.ORDER_TYPE_SELL
        volume = round(verdict.volume, 8)
        price = self._entry_price(order_type, tick)

        # fresh margin number (R-003 "margin via order_calc_margin")
        margin = self.client.order_calc_margin(order_type, self.spec.name, volume, price)
        margin_reject = self._margin_rule(margin, acct)
        if margin_reject is not None:
            return self._risk_reject("entry", iid, req.bundle_id, margin_reject)

        request = OrderRequest(
            action=K.TRADE_ACTION_DEAL,
            symbol=self.spec.name,
            volume=volume,
            type=order_type,
            price=price,
            sl=verdict.stop_price,
            tp=verdict.target_price or 0.0,
            deviation=self.deviation,
            magic=magic_from_bundle(req.bundle_id),
            comment=req.proposal_id[:31],
            type_filling=filling_for(self.spec),
            type_time=K.ORDER_TIME_GTC,
        )
        ident = dict(intent_id=iid, bundle_id=req.bundle_id)

        self.throttle.acquire()
        check = self.client.order_check(request)
        self.journal.record_attempt(
            kind="entry",
            stage="check",
            request=request,
            result=check,
            requested_price=price,
            **ident,
        )
        if check is None:
            return self._fail(iid, "order_check returned None")
        if check.retcode not in (0, K.TRADE_RETCODE_DONE):
            return IntentResult(
                intent_id=iid,
                status="rejected",
                detail=f"order_check: {K.retcode_name(check.retcode)} ({check.retcode}) "
                f"{check.comment}".strip(),
            )

        def refresh(r: OrderRequest) -> None:
            t = self._tick()
            if t is not None:
                r.price = self._entry_price(r.type, t)

        result, status = self._send(request, kind="entry", refresh=refresh, **ident)
        if result is None or status != "ok":
            return self._fail(iid, status)

        position_id = result.order
        self.journal.record_order(
            request,
            result,
            kind="entry",
            position_id=position_id,
            requested_price=request.price,
            **ident,
        )
        self.journal.record_snapshot(
            "order",
            str(position_id),
            market,
            regime=regime,
            minutes_to_event=minutes_to_event,
        )
        deal = self._deal_for(result, position_id)
        if deal is not None:
            self.journal.record_fill(deal, bid=result.bid, ask=result.ask)
        pos = self._position(position_id)
        detail = "partial" if result.retcode == K.TRADE_RETCODE_DONE_PARTIAL else ""
        if pos is not None:
            self.journal.upsert_position(
                pos,
                bundle_id=req.bundle_id,
                proposal_id=req.proposal_id,
                intent_id=iid,
                initial_stop=verdict.stop_price,
                risk_pct=verdict.risk_pct,
                risk_usd=verdict.risk_usd,
            )
            # R-004: the stop must be on the broker; otherwise close now.
            if not self._stop_present(pos, verdict.stop_price):
                if not self.ensure_stop(position_id, verdict.stop_price, verdict.target_price):
                    self.journal.record_incident(
                        "stop_failed",
                        "critical",
                        f"position {position_id} has no stop; closing (R-004)",
                    )
                    self.journal.alert(
                        "critical", "stop_failed", f"position {position_id}: stop could not be set"
                    )
                    self.close(position_id, "no_stop", market, ctx, intent_id=iid)
                    return IntentResult(
                        intent_id=iid,
                        status="failed",
                        detail="stop_failed_closed",
                        order_ticket=result.order,
                        position_id=position_id,
                        filled_price=result.price,
                        volume=result.volume,
                    )
        return IntentResult(
            intent_id=iid,
            status="executed",
            detail=detail,
            order_ticket=result.order,
            position_id=position_id,
            filled_price=result.price,
            volume=result.volume,
        )

    # ================================================================== stops
    def move_stop(
        self,
        position_id: int,
        new_stop: float,
        market: MarketState,
        ctx: RiskContext,
        *,
        intent_id: str | None = None,
    ) -> IntentResult:
        """Move a stop toward profit through `check_stop_move`, then TRADE_ACTION_SLTP (tp kept)."""
        iid = intent_id or f"move_stop:{position_id}"
        pos = self._position(position_id)
        if pos is None:
            return self._risk_reject(
                "sltp", iid, None, Rejected(RejectCode.POSITION_UNKNOWN), position_id=position_id
            )
        if self.throttle.coalesce(position_id, round(new_stop, self.spec.digits), pos.tp):
            return IntentResult(
                intent_id=iid, status="ignored", detail="coalesced", position_id=position_id
            )
        verdict = check_stop_move(
            StopMoveRequest(
                position_id=position_id,
                side=pos.side,
                new_stop=new_stop,
                current_stop=pos.sl,
                price_open=pos.price_open,
            ),
            market,
            ctx,
        )
        if isinstance(verdict, Rejected):
            return self._risk_reject("sltp", iid, None, verdict, position_id=position_id)
        stop = verdict.stop_price
        ok = self._send_sltp(position_id, pos, stop, pos.tp, intent_id=iid)
        if ok is None:
            return self._fail(iid, "sltp_failed", position_id=position_id)
        self.throttle.note_sent(position_id, stop, pos.tp)
        return IntentResult(
            intent_id=iid, status="executed", position_id=position_id, detail=f"sl={stop}"
        )

    def ensure_stop(self, position_id: int, stop_price: float, tp: float | None = None) -> bool:
        """Make sure the broker holds `stop_price` as SL for the position (R-004, R-008).

        A repair, not a trailing move: it is not gated by `check_stop_move` and bypasses coalescing.
        Returns True when the stop is in place afterwards.
        """
        pos = self._position(position_id)
        if pos is None:
            return False
        if self._stop_present(pos, stop_price):
            return True
        tp_level = tp if tp is not None else pos.tp
        self.throttle.forget(position_id)
        res = self._send_sltp(
            position_id, pos, stop_price, tp_level or 0.0, intent_id=f"ensure_stop:{position_id}"
        )
        if res is None:
            return False
        after = self._position(position_id)
        return after is not None and self._stop_present(after, stop_price)

    # ================================================================== closes
    def close(
        self,
        position_id: int,
        reason: str,
        market: MarketState | None = None,
        ctx: RiskContext | None = None,
        *,
        intent_id: str | None = None,
    ) -> IntentResult:
        """Close a position at market (opposite DEAL with `position=`) after `check_close`."""
        iid = intent_id or f"close:{position_id}"
        pos = self._position(position_id)
        market = market if market is not None else self._market_now()
        ctx = ctx if ctx is not None else RiskContext(now_ms=self.clock())
        verdict = check_close(pos is not None, market, ctx)
        if isinstance(verdict, Rejected):
            return self._risk_reject("close", iid, None, verdict, position_id=position_id)
        assert pos is not None
        tick = self._tick()
        if tick is None:
            return self._fail(iid, "no_tick", position_id=position_id)
        close_type = K.ORDER_TYPE_SELL if pos.type == K.POSITION_TYPE_BUY else K.ORDER_TYPE_BUY
        request = OrderRequest(
            action=K.TRADE_ACTION_DEAL,
            symbol=pos.symbol,
            volume=pos.volume,
            type=close_type,
            price=self._entry_price(close_type, tick),
            deviation=self.deviation,
            magic=pos.magic,
            position=position_id,
            comment=reason[:31],
            type_filling=filling_for(self.spec),
            type_time=K.ORDER_TIME_GTC,
        )
        journaled = self.journal.position(position_id)
        bundle_id = journaled["bundle_id"] if journaled is not None else None

        def refresh(r: OrderRequest) -> None:
            t = self._tick()
            if t is not None:
                r.price = self._entry_price(r.type, t)

        result, status = self._send(
            request,
            kind="close",
            refresh=refresh,
            intent_id=iid,
            bundle_id=bundle_id,
            position_id=position_id,
        )
        if result is None or status != "ok":
            return self._fail(iid, status, position_id=position_id)
        self.journal.record_order(
            request,
            result,
            kind="close",
            intent_id=iid,
            bundle_id=bundle_id,
            position_id=position_id,
            requested_price=request.price,
        )
        if journaled is None:
            self.journal.upsert_position(pos, source="adopted")
        deal = self._deal_for(result, position_id)
        if deal is not None:
            self.journal.record_fill(deal, bid=result.bid, ask=result.ask)
        remaining = self._position(position_id)
        if remaining is None:
            self.journal.book_trade(position_id, reason)
        else:
            self.journal.upsert_position(remaining)
        self.throttle.forget(position_id)
        return IntentResult(
            intent_id=iid,
            status="executed",
            detail=reason,
            order_ticket=result.order,
            position_id=position_id,
            filled_price=result.price,
            volume=result.volume,
        )

    def close_all(
        self,
        reason: str,
        market: MarketState | None = None,
        ctx: RiskContext | None = None,
        *,
        intent_id: str | None = None,
    ) -> list[IntentResult]:
        """Close every open position of the traded symbol."""
        out: list[IntentResult] = []
        for pos in self.client.positions_get(symbol=self.spec.name):
            out.append(
                self.close(
                    pos.ticket,
                    reason,
                    market,
                    ctx,
                    intent_id=f"{intent_id}:{pos.ticket}" if intent_id else None,
                )
            )
        return out

    def cancel_pending(self) -> list[IntentResult]:
        """Remove every pending order of the traded symbol (TRADE_ACTION_REMOVE)."""
        out: list[IntentResult] = []
        for order in self.client.orders_get(symbol=self.spec.name):
            request = OrderRequest(
                action=K.TRADE_ACTION_REMOVE,
                symbol=order.symbol,
                volume=0.0,
                type=order.type,
                order=order.ticket,
                magic=order.magic,
            )
            iid = f"cancel:{order.ticket}"
            result, status = self._send(request, kind="remove", intent_id=iid)
            ok = result is not None and status == "ok"
            out.append(
                IntentResult(
                    intent_id=iid,
                    status="executed" if ok else "failed",
                    detail="" if ok else status,
                    order_ticket=order.ticket,
                )
            )
        return out

    # ================================================================== kill switch (RISK_POLICY §3)
    def kill(self, reason: str) -> list[IntentResult]:
        """Close all, cancel pending, block entries; persisted as an open `kill_switch` incident."""
        results = self.close_all(reason, intent_id="kill")
        results.extend(self.cancel_pending())
        self.killed = True
        if not self.journal.kill_active():
            self.journal.record_incident(KILL_INCIDENT_KIND, "critical", reason)
        self.journal.alert("critical", KILL_INCIDENT_KIND, f"kill switch: {reason}")
        return results

    def reset_kill(self, owner_token_ok: bool) -> bool:
        """Owner-only reset: closes the kill incident and allows entries again."""
        if not owner_token_ok:
            return False
        self.journal.close_incidents(KILL_INCIDENT_KIND)
        self.killed = False
        return True

    # ================================================================== internals
    def _send(
        self,
        request: OrderRequest,
        *,
        kind: str,
        intent_id: str | None,
        bundle_id: str | None = None,
        position_id: int | None = None,
        refresh: Callable[[OrderRequest], None] | None = None,
    ) -> tuple[OrderResult | None, str]:
        """order_send with the retcode policy; returns (last result, 'ok' | failure text).

        Every attempt is journaled before this returns (R-007).
        """
        result: OrderResult | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self.throttle.acquire()
            result = self.client.order_send(request)
            self.journal.record_attempt(
                kind=kind,
                stage="send",
                request=request,
                result=result,
                attempt=attempt,
                intent_id=intent_id,
                bundle_id=bundle_id,
                position_id=position_id,
                requested_price=request.price or None,
            )
            if result is None:
                return None, "order_send returned None"
            cls = classify_retcode(result.retcode)
            if cls == "ok":
                self.throttle.reset_backoff()
                return result, "ok"
            if cls == "transient" and attempt < MAX_ATTEMPTS:
                self.throttle.backoff(result.retcode)
                if refresh is not None:
                    refresh(request)
                continue
            if cls == "fatal" and result.retcode in ALERT_RETCODES:
                self.journal.alert(
                    "error",
                    "order_rejected",
                    f"{kind}: {_retcode_text(result)}",
                    {"retcode": result.retcode, "position_id": position_id},
                )
            return result, _retcode_text(result)
        return result, "retries exhausted"

    def _send_sltp(
        self, position_id: int, pos: Position, sl: float, tp: float, *, intent_id: str
    ) -> OrderResult | None:
        request = OrderRequest(
            action=K.TRADE_ACTION_SLTP,
            symbol=pos.symbol,
            volume=pos.volume,
            type=pos.type,
            sl=sl,
            tp=tp or 0.0,
            magic=pos.magic,
            position=position_id,
        )
        result, status = self._send(
            request, kind="sltp", intent_id=intent_id, position_id=position_id
        )
        if result is None or status != "ok":
            return None
        self.journal.set_position_stop(position_id, sl, tp or None)
        return result

    def _risk_reject(
        self,
        kind: str,
        intent_id: str,
        bundle_id: str | None,
        verdict: Rejected,
        *,
        position_id: int | None = None,
    ) -> IntentResult:
        self.journal.record_attempt(
            kind=kind,
            stage="risk",
            request=None,
            result=None,
            intent_id=intent_id,
            bundle_id=bundle_id,
            position_id=position_id,
            risk_code=verdict.code.value,
        )
        return IntentResult(
            intent_id=intent_id,
            status="rejected",
            risk_code=verdict.code.value,
            detail=verdict.detail,
            position_id=position_id,
        )

    @staticmethod
    def _fail(intent_id: str, detail: str, *, position_id: int | None = None) -> IntentResult:
        return IntentResult(
            intent_id=intent_id, status="failed", detail=detail, position_id=position_id
        )

    def _margin_rule(self, margin: float | None, acct: AccountState) -> Rejected | None:
        """The risk engine's margin checks, re-run with the broker's own `order_calc_margin`."""
        if margin is None:
            return Rejected(RejectCode.MARGIN_INSUFFICIENT, "order_calc_margin returned None")
        if margin > acct.margin_free:
            return Rejected(RejectCode.MARGIN_INSUFFICIENT, f"need {margin:.2f}")
        used = acct.equity / acct.margin_level * 100.0 if acct.margin_level else 0.0
        total = used + margin
        projected = acct.equity / total * 100.0 if total > 0 else float("inf")
        if projected < self.cfg.owner.risk.min_margin_level_pct:
            return Rejected(RejectCode.MARGIN_LEVEL, f"{projected:.0f}%")
        return None

    def _tick(self) -> Tick | None:
        return self.client.symbol_info_tick(self.spec.name)

    @staticmethod
    def _entry_price(order_type: int, tick: Tick) -> float:
        return tick.ask if order_type == K.ORDER_TYPE_BUY else tick.bid

    def _position(self, position_id: int) -> Position | None:
        rows = self.client.positions_get(ticket=position_id)
        return rows[0] if rows else None

    def _stop_present(self, pos: Position, stop_price: float) -> bool:
        return bool(pos.sl) and abs(pos.sl - stop_price) < self.spec.point / 2.0

    def _deal_for(self, result: OrderResult, position_id: int) -> Deal | None:
        if not result.deal:
            return None
        deals = self.client.history_deals_get(
            datetime.fromtimestamp(0, UTC),
            datetime.fromtimestamp(self.clock() / 1000.0 + 86400, UTC),
            position=position_id,
        )
        for d in deals:
            if d.ticket == result.deal:
                return d
        return None

    def _market_now(self) -> MarketState:
        """Minimal MarketState from the live tick for `check_close` when the caller has none."""
        tick = self._tick()
        fresh = self.client.symbol_info(self.spec.name)
        spec = fresh or self.spec
        bid = tick.bid if tick else 0.0
        ask = tick.ask if tick else 0.0
        return MarketState(
            bid=bid,
            ask=ask,
            point=spec.point,
            digits=spec.digits,
            atr=None,
            spread_pips=(ask - bid) / spec.point if tick and spec.point > 0 else 0.0,
            spread_regime="unknown",
            session=SessionState.OPEN if tick else SessionState.STALE,
            entries_allowed=False,
            tick_age_s=0.0,
            trade_mode=spec.trade_mode,
            contract_size=spec.trade_contract_size,
            volume_min=spec.volume_min,
            volume_max=spec.volume_max,
            volume_step=spec.volume_step,
            stops_level_points=spec.trade_stops_level,
            freeze_level_points=spec.trade_freeze_level,
            margin_per_lot=None,
        )


__all__ = [
    "ALERT_RETCODES",
    "DEFAULT_DEVIATION_POINTS",
    "ExecutionEngine",
    "filling_for",
    "magic_from_bundle",
]

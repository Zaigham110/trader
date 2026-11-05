"""Builds the risk-engine inputs from live sources (ARCHITECTURE §2.1, §3.1 step 7).

`StateBuilder.snapshot()` is the single place where MT5 account data, live features, the session
clock, the journal and the gateway flags are combined into `AccountState`, `MarketState`,
`RiskContext` and the `GatewayState` read model. Everything downstream is pure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from goldtrader.config.loader import owner_hash
from goldtrader.config.schema import Config
from goldtrader.contracts import GatewayState
from goldtrader.gateway.market import LiveMarket
from goldtrader.mt5 import constants as K
from goldtrader.mt5.protocol import MT5Client
from goldtrader.risk.types import AccountState, MarketState, RiskContext
from goldtrader.session.calendar import Event
from goldtrader.session.state import SessionClock
from goldtrader.types import Position, SessionState, SymbolSpec


@dataclass
class GatewayFlags:
    kill_switch: bool = False
    paused: bool = False
    spec_ok: bool = True
    incident_open: bool = False
    owner_hash_expected: str = ""


@dataclass
class PnLTracker:
    """Realised + floating P&L per trading day and week, and the equity peak (drawdown base)."""

    start_equity_day: float = 0.0
    start_equity_week: float = 0.0
    day_key: int | None = None
    week_key: int | None = None
    equity_peak: float = 0.0
    live_start_equity: float = 0.0
    orders_ms: list[int] = field(default_factory=list)
    consecutive_losses: int = 0
    cooldown_until_ms: int = 0

    def roll(self, now_s: int, equity: float) -> None:
        day = now_s // 86400
        week = (now_s + 3 * 86400) // (7 * 86400)
        if self.day_key != day:
            self.day_key, self.start_equity_day = day, equity
        if self.week_key != week:
            self.week_key, self.start_equity_week = week, equity
        if self.live_start_equity <= 0:
            self.live_start_equity = equity
        self.equity_peak = max(self.equity_peak, equity)

    def note_order(self, now_ms: int) -> None:
        self.orders_ms.append(now_ms)
        self.orders_ms = [t for t in self.orders_ms if now_ms - t < 60_000]

    def orders_last_minute(self, now_ms: int) -> int:
        return sum(1 for t in self.orders_ms if now_ms - t < 60_000)

    def note_trade_result(self, net_pnl: float, now_ms: int, cfg: Config) -> None:
        r = cfg.owner.risk
        if net_pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= r.consecutive_loss_pause:
                self.cooldown_until_ms = now_ms + r.cooldown_min * 60_000
                self.consecutive_losses = 0
        else:
            self.consecutive_losses = 0


@dataclass
class Snapshot:
    account: AccountState
    market: MarketState
    ctx: RiskContext
    session: SessionState
    entries_allowed: bool
    positions: list[Position]
    view: GatewayState


class StateBuilder:
    def __init__(
        self,
        client: MT5Client,
        spec: SymbolSpec,
        cfg: Config,
        market: LiveMarket,
        clock: SessionClock,
        flags: GatewayFlags,
        pnl: PnLTracker,
    ) -> None:
        self.client = client
        self.spec = spec
        self.cfg = cfg
        self.market = market
        self.clock = clock
        self.flags = flags
        self.pnl = pnl
        self.flags.owner_hash_expected = owner_hash(cfg)

    def snapshot(
        self, now_ms: int, events: list[Event] | tuple[Event, ...] = ()
    ) -> Snapshot | None:
        acct = self.client.account_info()
        term = self.client.terminal_info()
        if acct is None or term is None:
            return None
        positions = list(self.client.positions_get(symbol=self.spec.name))
        live_spec = self.client.symbol_info(self.spec.name)
        trade_mode = live_spec.trade_mode if live_spec is not None else K.SYMBOL_TRADE_MODE_DISABLED
        tick = self.market.last_tick
        tick_age = self.market.tick_age_s(now_ms)
        feats = self.market.features()
        now_s = now_ms // 1000
        self.pnl.roll(now_s, acct.equity)
        decision = self.clock.state_at(
            datetime.fromtimestamp(now_s, UTC),
            trade_mode=trade_mode,
            last_tick_age_s=tick_age if tick is not None else None,
            events=events,
        )
        connected = bool(term.connected)
        session = decision.state if connected else SessionState.STALE
        entries_allowed = decision.entries_allowed and connected and term.trade_allowed
        margin_per_lot = None
        if tick is not None:
            margin_per_lot = self.client.order_calc_margin(
                K.ORDER_TYPE_BUY, self.spec.name, 1.0, tick.ask
            )
        account = AccountState(
            equity=acct.equity,
            balance=acct.balance,
            margin_free=acct.margin_free,
            margin_level=acct.margin_level if acct.margin > 0 else None,
            leverage=acct.leverage,
            open_positions=len(positions),
            day_pnl=acct.equity - self.pnl.start_equity_day,
            week_pnl=acct.equity - self.pnl.start_equity_week,
            equity_peak=self.pnl.equity_peak,
            orders_last_minute=self.pnl.orders_last_minute(now_ms),
            consecutive_losses=self.pnl.consecutive_losses,
            cooldown_until_ms=self.pnl.cooldown_until_ms,
            start_equity_day=self.pnl.start_equity_day,
            start_equity_week=self.pnl.start_equity_week,
            live_start_equity=self.pnl.live_start_equity,
        )
        market = MarketState(
            bid=tick.bid if tick else float("nan"),
            ask=tick.ask if tick else float("nan"),
            point=self.spec.point,
            digits=self.spec.digits,
            atr=feats.atr if feats else None,
            spread_pips=feats.spread_pips
            if feats
            else ((tick.ask - tick.bid) / 0.01 if tick else float("inf")),
            spread_regime=feats.spread_regime if feats else "unknown",
            session=session,
            entries_allowed=entries_allowed,
            tick_age_s=tick_age,
            trade_mode=trade_mode,
            contract_size=self.spec.trade_contract_size,
            volume_min=self.spec.volume_min,
            volume_max=self.spec.volume_max,
            volume_step=self.spec.volume_step,
            stops_level_points=self.spec.trade_stops_level,
            freeze_level_points=self.spec.trade_freeze_level,
            margin_per_lot=margin_per_lot,
        )
        ctx = RiskContext(
            kill_switch=self.flags.kill_switch,
            paused=self.flags.paused,
            owner_hash_ok=(owner_hash(self.cfg) == self.flags.owner_hash_expected),
            spec_ok=self.flags.spec_ok,
            incident_open=self.flags.incident_open,
            now_ms=now_ms,
        )
        view = GatewayState(
            time_ms=now_ms,
            connected=connected,
            trade_allowed=bool(term.trade_allowed),
            session=session.value,
            entries_allowed=entries_allowed,
            bid=tick.bid if tick else None,
            ask=tick.ask if tick else None,
            spread_pips=market.spread_pips if tick else None,
            atr=market.atr,
            tick_age_s=tick_age if tick else None,
            equity=acct.equity,
            balance=acct.balance,
            margin_free=acct.margin_free,
            margin_level=acct.margin_level if acct.margin > 0 else None,
            open_positions=len(positions),
            kill_switch=self.flags.kill_switch,
            paused=self.flags.paused,
            owner_hash=self.flags.owner_hash_expected,
            spec_ok=self.flags.spec_ok,
            incident_open=self.flags.incident_open,
            day_pnl=account.day_pnl,
            positions=[
                {
                    "ticket": p.ticket,
                    "side": p.side,
                    "volume": p.volume,
                    "price_open": p.price_open,
                    "sl": p.sl,
                    "tp": p.tp,
                    "profit": p.profit,
                }
                for p in positions
            ],
        )
        return Snapshot(
            account=account,
            market=market,
            ctx=ctx,
            session=session,
            entries_allowed=entries_allowed,
            positions=positions,
            view=view,
        )

"""Gateway process loop (ARCHITECTURE §2.1, §3.2, §5; RISK_POLICY §3, §5).

Owns the terminal connection, the live market, the state builder, the execution engine, the
reconciler and the loopback intents API. Safety rules enforced here, in code, every step:
  * pre-break / close-only / closed with an open position → flatten (D-020);
  * drawdown at or beyond the limit → kill switch;
  * disconnected for longer than `kill_on_disconnect_min` with an unprotected position → kill switch;
  * critical spec change → fail closed (entries blocked) and incident;
  * reconcile every 60 s; heartbeat every 10 s; account snapshot every 60 s.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from goldtrader.config.loader import config_hash, owner_hash
from goldtrader.config.schema import Config
from goldtrader.contracts import GatewayState, Intent, IntentResult
from goldtrader.db.repo import Repo
from goldtrader.execution.engine import ExecutionEngine
from goldtrader.execution.intents import IntentHandler
from goldtrader.execution.journal import Journal
from goldtrader.execution.reconciler import Reconciler
from goldtrader.gateway.api import GatewayAPI
from goldtrader.gateway.market import LiveMarket
from goldtrader.gateway.state import GatewayFlags, PnLTracker, Snapshot, StateBuilder
from goldtrader.mt5 import constants as K
from goldtrader.mt5.protocol import MT5Client
from goldtrader.mt5.spec import critical_changes, diff_specs, find_gold_symbol, read_spec
from goldtrader.session.calendar import Event
from goldtrader.session.state import SessionClock
from goldtrader.types import SessionState

log = logging.getLogger(__name__)


@dataclass
class GatewayCounters:
    steps: int = 0
    ticks: int = 0
    bars_closed: int = 0
    flattens: int = 0
    kills: int = 0
    reconciles: int = 0
    last_reconcile_ms: int = 0
    last_heartbeat_ms: int = 0
    last_account_ms: int = 0
    last_spec_check_ms: int = 0
    disconnected_since_ms: int | None = None
    last_error: str | None = None
    errors: int = 0
    discrepancies: list[str] = field(default_factory=list)


class GatewayService:
    def __init__(
        self,
        client: MT5Client,
        cfg: Config,
        repo: Repo,
        *,
        clock_ms: Callable[[], int] | None = None,
        token: str | None = None,
        port: int = 8750,
        events_fn: Callable[[], list[Event]] | None = None,
        start_api: bool = True,
    ) -> None:
        self.client = client
        self.cfg = cfg
        self.repo = repo
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.token = token or secrets.token_hex(24)
        self.port = port
        self.events_fn = events_fn or (lambda: [])
        self.start_api = start_api
        self.flags = GatewayFlags()
        self.pnl = PnLTracker()
        self.counters = GatewayCounters()
        self._lock = threading.RLock()
        self._snapshot: Snapshot | None = None
        self.api: GatewayAPI | None = None
        self.started = False

    # ----------------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if not self.client.initialize():
            raise RuntimeError("MT5 initialize() failed")
        self.symbol = find_gold_symbol(self.client, preferred=self.cfg.account.symbol)
        self.client.symbol_select(self.symbol, True)
        self.spec = read_spec(self.client, self.symbol)
        prev = self.repo.latest_spec(self.symbol)
        changed = diff_specs(prev, self.spec) if prev else []
        crit = critical_changes(prev, self.spec) if prev else []
        self.repo.record_spec(self.spec, changed, bool(crit))
        self.repo.record_config(
            config_hash(self.cfg), owner_hash(self.cfg), "gateway", self.cfg.model_dump(mode="json")
        )
        self.market = LiveMarket(self.symbol, self.repo)
        self.market.preload(self.repo.load_bars(self.symbol, 60)[-30000:])
        self.clock = SessionClock(self.cfg.session)
        self.state = StateBuilder(
            self.client, self.spec, self.cfg, self.market, self.clock, self.flags, self.pnl
        )
        self.journal = Journal(
            self.repo.c,
            clock=self.clock_ms,
            point=self.spec.point,
            digits=self.spec.digits,
            contract_size=self.spec.trade_contract_size,
        )
        self.engine = ExecutionEngine(
            self.client, self.spec, self.cfg, self.journal, clock=self.clock_ms
        )
        self.handler = IntentHandler(self.engine, self._state_provider)
        self.reconciler = Reconciler(self.client, self.spec, self.journal, self.engine)
        if crit:
            self.flags.spec_ok = False
            self.journal.record_incident("spec_critical_change", "critical", ",".join(crit))
        if self.engine.killed:
            self.flags.kill_switch = True
        if self.start_api:
            self.api = GatewayAPI(
                self.token, self.view, self.handle_intent, self.health, port=self.port
            )
            self.api.start()
        self.started = True
        self.step()

    def stop(self) -> None:
        if self.api is not None:
            self.api.stop()
        self.client.shutdown()

    # ----------------------------------------------------------------------- API callbacks
    def view(self) -> GatewayState | None:
        with self._lock:
            return self._snapshot.view if self._snapshot else None

    def health(self) -> dict:
        c = self.counters
        return {
            "started": self.started,
            "steps": c.steps,
            "ticks": c.ticks,
            "bars_closed": c.bars_closed,
            "flattens": c.flattens,
            "kills": c.kills,
            "reconciles": c.reconciles,
            "errors": c.errors,
            "last_error": c.last_error,
            "kill_switch": self.flags.kill_switch,
            "paused": self.flags.paused,
            "spec_ok": self.flags.spec_ok,
            "incident_open": self.flags.incident_open,
            "discrepancies": c.discrepancies[-10:],
            "config_hash": config_hash(self.cfg),
            "owner_hash": owner_hash(self.cfg),
        }

    def _state_provider(self):
        with self._lock:
            snap = self._snapshot or self._refresh()
        return snap.account, snap.market, snap.ctx

    def handle_intent(self, intent: Intent) -> IntentResult:
        with self._lock:
            self._refresh()
            result = self.handler.handle(intent)
            if intent.kind == "kill" and result.status == "executed":
                self.flags.kill_switch = True
                self.counters.kills += 1
            if intent.kind == "resume" and result.status == "executed":
                # owner-confirmed resume also clears the kill switch (RISK_POLICY §3 reset rule)
                self.engine.reset_kill(
                    owner_token_ok=intent.source in ("owner_confirmed", "system")
                )
                self.flags.kill_switch = self.engine.killed
                self.counters.disconnected_since_ms = None
            if intent.kind in ("pause", "resume"):
                self.flags.paused = self.handler.paused
            if intent.kind == "enter" and result.status == "executed":
                self.pnl.note_order(self.clock_ms())
            return result

    # ----------------------------------------------------------------------- loop
    def _refresh(self) -> Snapshot:
        now = self.clock_ms()
        self.flags.incident_open = (
            self.journal.open_incident("kill_switch") is not None
            or self.journal.open_incident("spec_critical_change") is not None
            or self.journal.open_incident("stop_missing") is not None
        )
        snap = self.state.snapshot(now, self.events_fn())
        if snap is None:
            raise RuntimeError("account_info/terminal_info unavailable")
        self._snapshot = snap
        return snap

    def step(self) -> Snapshot | None:
        """One poll cycle. Never raises; errors are counted and the previous snapshot kept."""
        now = self.clock_ms()
        c = self.counters
        c.steps += 1
        try:
            with self._lock:
                tick = self.client.symbol_info_tick(self.symbol)
                if tick is not None:
                    c.ticks += 1
                    if self.market.on_tick(tick, now) is not None:
                        c.bars_closed += 1
                if self.market.flush_if_stale(now) is not None:
                    c.bars_closed += 1
                snap = self._refresh()
                if self._safety(snap, now):
                    snap = self._refresh()
                if now - c.last_heartbeat_ms >= 10_000:
                    self.repo.heartbeat(
                        "gateway",
                        "ok" if snap.view.connected else "disconnected",
                        {"session": snap.session.value, "kill": self.flags.kill_switch},
                    )
                    c.last_heartbeat_ms = now
                if now - c.last_account_ms >= 60_000:
                    acct = self.client.account_info()
                    if acct is not None:
                        self.repo.record_account(acct)
                    c.last_account_ms = now
                if now - c.last_reconcile_ms >= 60_000:
                    rep = self.reconciler.run_once(now, atr=snap.market.atr)
                    c.reconciles += 1
                    c.last_reconcile_ms = now
                    if rep.discrepancies:
                        c.discrepancies.extend(rep.discrepancies)
                if now - c.last_spec_check_ms >= 86_400_000:
                    self._recheck_spec()
                    c.last_spec_check_ms = now
                return snap
        except Exception as e:  # the loop must survive anything
            c.errors += 1
            c.last_error = f"{type(e).__name__}: {e}"
            log.exception("gateway step failed")
            return None

    def _safety(self, snap: Snapshot, now: int) -> bool:
        """Apply the code safety rules; returns True when an action changed state."""
        r = self.cfg.owner.risk
        c = self.counters
        acted = False
        has_pos = len(snap.positions) > 0
        # 1. intraday flatten
        if (
            has_pos
            and snap.session
            in (SessionState.PRE_BREAK_BUFFER, SessionState.CLOSE_ONLY, SessionState.CLOSED)
            and not self.flags.kill_switch
        ):
            if snap.market.trade_mode != K.SYMBOL_TRADE_MODE_DISABLED:
                self.engine.close_all("session_flatten")
                c.flattens += 1
                acted = True
        # 2. drawdown kill
        base = (
            snap.account.live_start_equity
            if self.cfg.owner.profile == "micro_live"
            else snap.account.equity_peak
        )
        if (
            base > 0
            and (base - snap.account.equity) / base * 100.0 >= r.max_drawdown_pct
            and not self.flags.kill_switch
        ):
            self.engine.kill(f"drawdown {((base - snap.account.equity) / base * 100.0):.1f}%")
            self.flags.kill_switch = True
            c.kills += 1
            acted = True
        # 3. disconnect with unprotected position
        if not snap.view.connected:
            if c.disconnected_since_ms is None:
                c.disconnected_since_ms = now
            unprotected = any(p.sl == 0 for p in snap.positions)
            if (
                unprotected
                and now - c.disconnected_since_ms >= r.kill_on_disconnect_min * 60_000
                and not self.flags.kill_switch
            ):
                self.engine.kill("disconnected with unprotected position")
                self.flags.kill_switch = True
                c.kills += 1
                acted = True
        else:
            c.disconnected_since_ms = None
        return acted

    def _recheck_spec(self) -> None:
        live = read_spec(self.client, self.symbol)
        crit = critical_changes(self.spec, live)
        changed = diff_specs(self.spec, live)
        if changed:
            self.repo.record_spec(live, changed, bool(crit))
        if crit:
            self.flags.spec_ok = False
            self.journal.record_incident("spec_critical_change", "critical", ",".join(crit))
            self.journal.alert("critical", "spec_critical_change", f"symbol spec changed: {crit}")

    def run(self, stop: threading.Event) -> None:
        d = self.cfg.data
        while not stop.is_set():
            snap = self.step()
            closed = snap is None or snap.session in (SessionState.CLOSED, SessionState.STALE)
            stop.wait((d.tick_poll_closed_s * 1000 if closed else d.tick_poll_ms) / 1000.0)

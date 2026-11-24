"""Brain process loop (ARCHITECTURE §2.2): scheduler → decision cycle, trade review, alerts,
heartbeat. No MT5 access; the gateway is reached only through its loopback API (AGENTS §1).
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

from goldtrader.bots.registry import BotRegistry
from goldtrader.brain.cycle import DecisionCycle
from goldtrader.brain.features import DbFeatures
from goldtrader.brain.scheduler import Job, Scheduler
from goldtrader.config.schema import Config
from goldtrader.db.repo import Repo
from goldtrader.bots.common import BotContext
from goldtrader.feeds.calendar_store import CalendarRefresher, CalendarStore
from goldtrader.feeds.cot import REFRESH_HOURS, CotRefresher, CotStore
from goldtrader.feeds.news import NewsFetcher, NewsStore, news_for_research
from goldtrader.gateway.api import GatewayClient
from goldtrader.llm.provider import ModelProvider
from goldtrader.llm.store import LLMStore
from goldtrader.notify.alerts import AlertDispatcher
from goldtrader.notify.telegram import TelegramSender
from goldtrader.report import veto

log = logging.getLogger(__name__)

PAPER_EXPERIMENT = "paper"  # shadow_trades.experiment_id of owner paper candidates (D-059)


def _spawn_detached(cmd: list[str]) -> object:
    """Start the nightly learning run outside this process."""
    import subprocess

    return subprocess.Popen(  # noqa: S603 - fixed argv built by this module
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL
    )


class BrainService:
    def __init__(
        self,
        cfg: Config,
        repo: Repo,
        gateway: GatewayClient,
        provider: ModelProvider | None,
        *,
        sender: TelegramSender | None = None,
        owner_chat_id: int | None = None,
        clock_ms: Callable[[], int] | None = None,
        cycle_every_s: int = 300,
        chat_updates=None,
        config_path=None,
        feed_fetch=None,
        cot_fetch=None,
        spawn=None,
    ) -> None:
        self.cfg = cfg
        self.repo = repo
        self.gateway = gateway
        self.config_path = config_path
        self.provider_name = (
            "anthropic" if type(provider).__name__ == "AnthropicProvider" else "none"
        )
        self.spawn = spawn or _spawn_detached
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        enabled = [s for s in cfg.strategies if s.enabled]
        if not enabled:
            raise ValueError("no enabled strategy in config")
        self.strat = enabled[0]
        self.store = LLMStore(repo.c)
        self.registry = BotRegistry(
            cfg, provider, self.store, repo.c, strat=self.strat, clock_ms=self.clock_ms
        )
        self.feats = DbFeatures(
            repo, cfg.account.symbol, self.strat, clock=lambda: self.clock_ms() / 1000.0
        )
        self._last_state = None
        # learning (Phase 6): nightly job, shadow feed and the limited-live router (D-047)
        from goldtrader.brain.learning_job import LearningJob
        from goldtrader.types import SymbolSpec

        spec = SymbolSpec(
            name=cfg.account.symbol,
            digits=2,
            point=0.01,
            trade_tick_size=0.01,
            trade_tick_value=1.0,
            trade_contract_size=100.0,
            volume_min=0.01,
            volume_max=200.0,
            volume_step=0.01,
            volume_limit=0.0,
            trade_stops_level=0,
            trade_freeze_level=0,
            trade_mode=4,
            trade_exemode=2,
            filling_mode=3,
            swap_mode=1,
            swap_long=0.0,
            swap_short=0.0,
            swap_rollover3days=3,
            margin_initial=0.0,
            margin_hedged=0.0,
            currency_base="XAU",
            currency_profit="USD",
            currency_margin="USD",
            spread=20,
            spread_float=True,
        )
        live_spec = repo.latest_spec(cfg.account.symbol)
        self.learning = LearningJob(
            cfg,
            repo,
            self.strat,
            self.clock_ms,
            spec=live_spec or spec,
            frame_fn=lambda: self.feats._frame(),
        )
        self._last_shadow_bar: int | None = None
        self.cycle = DecisionCycle(
            cfg,
            self.strat,
            self.registry,
            gateway,
            repo.c,
            features_fn=lambda: self.feats.features(self._last_state),
            signal_fn=self.feats.signal,
            news_fn=self._recent_news,
            trades_fn=self._recent_trades,
            lessons_fn=self._lessons,
            calibration_fn=self._calibration,
            clock_ms=self.clock_ms,
            event_fn=self._next_event,
            positioning_fn=lambda: self.cot_store.line(self.clock_ms()),
            signals_fn=self._channel_signals,
        )
        self.dispatcher = None
        if sender is not None and owner_chat_id is not None:
            self.dispatcher = AlertDispatcher(
                repo.c,
                sender,
                owner_chat_id,
                critical_kinds={"kill_switch", "stop_missing", "spec_critical_change"},
            )
        # feeds (D-038): news -> Research bot; calendar -> events table read by the gateway
        self.news_store = NewsStore(repo.c)
        # slow positioning context (D-060): CFTC gold futures report, polled twice a day
        self.cot_store = CotStore(repo.c)
        self.cot_refresher = CotRefresher(
            self.cot_store,
            self.clock_ms,
            # a caller that injects the feed transport (tests) must not reach the network
            fetch=cot_fetch or ((lambda _url: "[]") if feed_fetch is not None else None),
        )
        self.last_cot: tuple[int, str | None] = (0, None)
        self.calendar_store = CalendarStore(repo.c)
        fetch_kw = {"fetch": feed_fetch} if feed_fetch is not None else {}
        self.news_fetcher = NewsFetcher(cfg.feeds, self.news_store, self.clock_ms, **fetch_kw)
        self.calendar_refresher = CalendarRefresher(
            cfg.feeds,
            self.calendar_store,
            self.clock_ms,
            fix_times_london=tuple(cfg.session.fix_times_london),
            **fetch_kw,
        )
        # chat (D-039): only with a sender, an updates transport and the owner chat id
        self.chat = None
        if (
            sender is not None
            and chat_updates is not None
            and owner_chat_id is not None
            and cfg.chat.enabled
        ):
            from goldtrader.chat.telegram_loop import ChatService

            self.chat = ChatService(
                cfg,
                repo.c,
                provider,
                self.store,
                gateway,
                sender,
                chat_updates,
                owner_chat_id,
                self.clock_ms,
                features_fn=lambda: self.feats.features(self._last_state),
                config_path=config_path,
            )
        # dashboard chat (D-057): questions typed into the local page; needs no Telegram
        self.dashboard_chat = None
        if cfg.chat.enabled:
            from goldtrader.chat.local import DashboardChatService

            self.dashboard_chat = DashboardChatService(
                cfg,
                repo.c,
                provider,
                self.store,
                gateway,
                self.clock_ms,
                features_fn=lambda: self.feats.features(self._last_state),
                config_path=config_path,
            )
        self.scheduler = Scheduler(clock=lambda: self.clock_ms() / 1000.0)
        # Real time (D-072): look at every 1-minute bar as it closes and decide at once when
        # the rule gives a signal, instead of noticing one bar in five. Registered first so a
        # triggered cycle runs in the same tick.
        self._watched_bar: int | None = None
        self.scheduler.add(Job("bar_watch", self._bar_watch_job, every_seconds=2))
        self.scheduler.add(
            Job("cycle", self._cycle_job, every_seconds=cycle_every_s, only_when=self._gateway_open)
        )
        self.scheduler.add(Job("news", self._news_job, every_seconds=cfg.feeds.poll_minutes * 60))
        self.scheduler.add(
            Job(
                "calendar",
                self._calendar_job,
                every_seconds=cfg.feeds.calendar_refresh_hours * 3600,
            )
        )
        self.scheduler.add(
            Job(
                "weekly_research",
                self._weekly_research_job,
                daily_at_utc="06:00",
                only_when=self._is_sunday,
            )
        )
        self.scheduler.add(Job("learning", self._learning_job, daily_at_utc="23:30"))
        self.scheduler.add(Job("cot", self._cot_job, every_seconds=REFRESH_HOURS * 3600))
        self.scheduler.add(Job("veto_report", self._veto_report_job, every_seconds=900))
        self.scheduler.add(Job("learning_loop", self._learning_loop_job, every_seconds=300))
        self.scheduler.add(Job("shadow", self._shadow_job, every_seconds=60))
        self.scheduler.add(Job("review", self._review_job, every_seconds=60))
        self.scheduler.add(Job("alerts", self._alerts_job, every_seconds=10))
        self.scheduler.add(Job("heartbeat", self._heartbeat_job, every_seconds=10))
        self.scheduler.add(Job("gateway_watch", self._gateway_watch_job, every_seconds=60))
        if self.chat is not None:
            self.scheduler.add(
                Job("chat", self._chat_job, every_seconds=max(1, cfg.chat.poll_seconds))
            )
        if self.dashboard_chat is not None:
            self.scheduler.add(Job("dashboard_chat", self._dashboard_chat_job, every_seconds=1))
        self.last_cycle = None
        self.reviewed: set[str] = set()
        self.last_news: dict = {}
        self.last_calendar: dict = {}

    # ------------------------------------------------------------------ jobs
    def _gateway_open(self) -> bool:
        st = self.gateway.state()
        self._last_state = st
        return st is not None and st.session == "OPEN" and st.entries_allowed and not st.kill_switch

    def _bar_watch_job(self) -> None:
        t = self.feats.latest_bar_time()
        if t is None or t == self._watched_bar:
            return
        first, self._watched_bar = self._watched_bar is None, t
        if first:
            return  # the bar that was already there at start is not news
        self.feats.refresh()
        if self.clock_ms() / 1000.0 - (t + 60) > 30:
            return  # an old bar arriving late (refill after an outage) is not a signal
        if self.feats.signal() != 0:
            self.scheduler.trigger("cycle")

    def _cycle_job(self) -> None:
        self.last_cycle = self.cycle.run_once()

    def _review_job(self) -> None:
        rows = self.repo.c.execute(
            "SELECT t.* FROM trades t LEFT JOIN postmortems p ON p.trade_id = t.trade_id "
            "WHERE p.id IS NULL ORDER BY t.exit_ms LIMIT 5"
        ).fetchall()
        for r in rows:
            trade = dict(r)
            if trade["trade_id"] in self.reviewed:
                continue
            self.reviewed.add(trade["trade_id"])
            self.cycle.review_closed_trade(trade)

    def _news_job(self) -> None:
        """Fetch feeds, then run the Research bot on unprocessed items and mark them processed."""
        self.last_news = self.news_fetcher.run_once()
        items = news_for_research(self.news_store, limit=20)
        if not items:
            return
        now = self.clock_ms()
        ctx = BotContext(
            features=self.feats.features(self._last_state),
            gateway_state=self._last_state,
            signal_side=0,
            now_ms=now,
            cfg=self.cfg,
            news_items=items,
        )
        self.registry.run("research", ctx)
        self.news_store.mark_processed([i["item_id"] for i in items], now)

    def _learning_job(self) -> None:
        self.learning.nightly()

    def _shadow_job(self) -> None:
        """Feed each newly closed 1-minute bar to the shadow ledger with the session state."""
        self._sync_paper_candidates()
        if not self.learning.shadow.bundles:  # a property (list of ids), not a method
            return
        df = self.feats._frame()
        if df is None or df.empty:
            return
        last = df.iloc[-1]
        t = int(last["time"])
        if self._last_shadow_bar is not None and t <= self._last_shadow_bar:
            return
        self._last_shadow_bar = t
        st = self._last_state or self.gateway.state()
        session = st.session if st is not None else "CLOSED"
        entries = bool(st.entries_allowed) if st is not None else False
        self.learning.shadow.on_bar(last, self.feats.features(st), session, entries)

    def _is_sunday(self) -> bool:
        return datetime.fromtimestamp(self.clock_ms() / 1000, UTC).weekday() == 6

    def _weekly_research_job(self) -> None:
        """Weekly hypothesis generation (LEARNING_SYSTEM §3.1); intake happens in the learning job."""
        now = self.clock_ms()
        ctx = BotContext(
            features=self.feats.features(self._last_state),
            gateway_state=self._last_state,
            signal_side=0,
            now_ms=now,
            cfg=self.cfg,
            recent_trades=self._recent_trades(),
            lessons=self.registry.lessons(10),
        )
        self.registry.run("weekly_research", ctx)

    def _calendar_job(self) -> None:
        self.last_calendar = self.calendar_refresher.run_once()

    def _chat_job(self) -> None:
        """Poll Telegram. A failed poll blocks this single-threaded loop for the whole network
        timeout, so after a failure wait a minute before trying again."""
        if self.chat is None:
            return
        now = self.clock_ms()
        if now < getattr(self, "_chat_retry_ms", 0):
            return
        if self.chat.poll_once(timeout_s=0) == ["error:get_updates"]:
            self._chat_retry_ms = now + 60_000

    def _sync_paper_candidates(self) -> None:
        """Register the owner's active paper candidates (D-059) with the shadow ledger and drop
        the ones switched off. Simulation only: the ledger has no path to an order."""
        try:
            rows = self.repo.c.execute(
                "SELECT bundle_id, kind, params_json, active FROM paper_candidates"
            ).fetchall()
        except Exception:  # noqa: BLE001 - table missing on an old database
            return
        have = set(self.learning.shadow.bundles)
        for r in rows:
            bid = r["bundle_id"]
            if r["active"] and bid not in have:
                try:
                    self.learning.shadow.register(
                        PAPER_EXPERIMENT, bid, r["kind"], json.loads(r["params_json"])
                    )
                    log.info("paper candidate registered: %s", bid)
                except Exception:  # noqa: BLE001 - a bad row must not stop the shadow feed
                    log.exception("paper candidate %s could not be registered", bid)
            elif not r["active"] and bid in have:
                self.learning.shadow.unregister(bid)

    def _cot_job(self) -> None:
        self.last_cot = self.cot_refresher.run_once()

    def _veto_report_job(self) -> None:
        """Refresh the stored "do the AI vetoes help?" report the dashboard reads (D-060)."""
        now = self.clock_ms()
        veto.store(
            self.repo.c, veto.REPORT_NAME, veto.build(self.repo.c, self.cfg, self.strat, now), now
        )
        # the go-live checklist for the dashboard's Risk & health page (D-083); the dashboard
        # itself loads no trading code, so it reads the stored result
        try:
            from goldtrader.report.scorecard import go_live_checklist

            checks = go_live_checklist(self.repo.c, self.cfg, now)
            body = {"checks": [{"name": n, "ok": bool(ok), "detail": d} for n, ok, d in checks]}
            veto.store(self.repo.c, "golive", body, now)
        except Exception:  # noqa: BLE001 - a report must never stop the loop
            log.exception("go-live checklist failed")

    def _learning_loop_job(self) -> None:
        """Learning loop (D-064): grade new signal decisions, judge the paper candidates and,
        once per trading day, start the nightly review in its own process (it runs backtests
        and model calls that must not hold up this loop)."""
        from goldtrader.learning import loop

        now = self.clock_ms()
        loop.score_decisions(self.repo.c, self.cfg, self.strat, now)
        loop.review_paper(self.repo.c, now)
        veto.store(self.repo.c, "learning", loop.status(self.repo.c, now), now)
        night = loop.nightly_due(self.repo.c, now)
        if night is None or self.config_path is None:
            return
        db = self.repo.c.execute("PRAGMA database_list").fetchone()[2]
        if not db:
            return
        loop.mark_started(self.repo.c, night, now)
        self.spawn(
            [
                sys.executable,
                "-c",
                "from goldtrader.cli import app; app()",
                "learn-nightly",
                "--config",
                str(self.config_path),
                "--db",
                str(db),
                "--provider",
                self.provider_name,
                "--night",
                night,
            ]
        )
        log.info("nightly learning started for %s", night)

    def _lessons(self) -> list[str]:
        """The playbook written by the nightly review, then the newest post-mortem lessons."""
        from goldtrader.learning import loop

        try:
            book = loop.active_playbook(self.repo.c)
        except Exception:  # noqa: BLE001 - optional context must never stop a cycle
            book = []
        return (book + self.registry.lessons(5))[:8]

    def _calibration(self) -> str:
        from goldtrader.learning import loop

        try:
            return loop.calibration_line(self.repo.c, self.clock_ms())
        except Exception:  # noqa: BLE001
            return ""

    def _channel_signals(self) -> str:
        """Fresh Telegram channel signals for the Strategy agent (D-062). Numbers and the channel's
        measured record only; never a channel's own wording."""
        from goldtrader.signals.report import fresh_line

        try:
            return fresh_line(self.repo.c, self.clock_ms())
        except Exception:  # noqa: BLE001 - optional context must never stop a cycle
            return ""

    def _next_event(self) -> tuple[str, float] | None:
        """Next high-impact calendar event and the minutes to it, for the Strategy and
        Risk-advisor context (AGENTS §3.2, §3.3). Without it they always saw "none scheduled"."""
        try:
            nxt = self.calendar_store.next_high_impact(self.clock_ms())
        except Exception:  # noqa: BLE001 - a calendar problem must not stop the cycle
            log.exception("next event lookup failed")
            return None
        return (nxt[0].title, float(nxt[1])) if nxt is not None else None

    def _dashboard_chat_job(self) -> None:
        if self.dashboard_chat is not None:
            self.dashboard_chat.poll_once(timeout_s=0)

    def _alerts_job(self) -> None:
        if self.dispatcher is not None:
            self.dispatcher.dispatch_pending(datetime.fromtimestamp(self.clock_ms() / 1000, UTC))

    def _gateway_watch_job(self) -> None:
        """Tell the owner when the gateway stops reporting (D-065). Heartbeats are stamped
        with real time, so this compares against real time too."""
        from goldtrader.process.watchdog import watch_heartbeat

        watch_heartbeat(self.repo.c, "gateway", int(time.time() * 1000))

    def _heartbeat_job(self) -> None:
        self.repo.heartbeat(
            "brain", "ok", {"last_cycle": self.last_cycle.status if self.last_cycle else None}
        )

    # ------------------------------------------------------------------ data readers
    def _recent_news(self) -> list[dict]:
        since = self.clock_ms() - 6 * 3600 * 1000
        try:
            rows = self.repo.c.execute(
                "SELECT * FROM news_events WHERE created_ms >= ? ORDER BY created_ms DESC LIMIT 20",
                (since,),
            ).fetchall()
        except Exception:  # table absent on a very old schema
            return []
        return [dict(r) for r in rows]

    def _recent_trades(self) -> list[dict]:
        rows = self.repo.c.execute("SELECT * FROM trades ORDER BY exit_ms DESC LIMIT 10").fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ loop
    def tick(self) -> list[str]:
        return self.scheduler.tick()

    def run(self, stop: threading.Event, interval_s: float = 1.0, on_tick=None) -> None:
        """`on_tick` is called after every pass of the scheduler, including one that raised: the
        loop is then still turning. Only a call that never returns stops it (the watchdog, D-078)."""
        while not stop.is_set():
            try:
                self.tick()
            except Exception:  # the scheduler isolates jobs; this guards the loop itself
                log.exception("brain tick failed")
            if on_tick is not None:
                on_tick()
            stop.wait(interval_s)

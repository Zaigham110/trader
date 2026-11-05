"""Brain process loop (ARCHITECTURE §2.2): scheduler → decision cycle, trade review, alerts,
heartbeat. No MT5 access; the gateway is reached only through its loopback API (AGENTS §1).
"""

from __future__ import annotations

import logging
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
from goldtrader.feeds.news import NewsFetcher, NewsStore, news_for_research
from goldtrader.gateway.api import GatewayClient
from goldtrader.llm.provider import ModelProvider
from goldtrader.llm.store import LLMStore
from goldtrader.notify.alerts import AlertDispatcher
from goldtrader.notify.telegram import TelegramSender

log = logging.getLogger(__name__)


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
    ) -> None:
        self.cfg = cfg
        self.repo = repo
        self.gateway = gateway
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
            lessons_fn=lambda: self.registry.lessons(5),
            clock_ms=self.clock_ms,
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
        self.scheduler = Scheduler(clock=lambda: self.clock_ms() / 1000.0)
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
        self.scheduler.add(Job("review", self._review_job, every_seconds=60))
        self.scheduler.add(Job("alerts", self._alerts_job, every_seconds=10))
        self.scheduler.add(Job("heartbeat", self._heartbeat_job, every_seconds=10))
        if self.chat is not None:
            self.scheduler.add(
                Job("chat", self._chat_job, every_seconds=max(1, cfg.chat.poll_seconds))
            )
        self.last_cycle = None
        self.reviewed: set[str] = set()
        self.last_news: dict = {}
        self.last_calendar: dict = {}

    # ------------------------------------------------------------------ jobs
    def _gateway_open(self) -> bool:
        st = self.gateway.state()
        self._last_state = st
        return st is not None and st.session == "OPEN" and st.entries_allowed and not st.kill_switch

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

    def _calendar_job(self) -> None:
        self.last_calendar = self.calendar_refresher.run_once()

    def _chat_job(self) -> None:
        if self.chat is not None:
            self.chat.poll_once(timeout_s=0)

    def _news_job(self) -> None:
        """Fetch feeds, run the Research bot on unprocessed items, mark them processed."""
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

    def _calendar_job(self) -> None:
        self.last_calendar = self.calendar_refresher.run_once()

    def _chat_job(self) -> None:
        if self.chat is not None:
            self.chat.poll_once(timeout_s=0)

    def _alerts_job(self) -> None:
        if self.dispatcher is not None:
            self.dispatcher.dispatch_pending(datetime.fromtimestamp(self.clock_ms() / 1000, UTC))

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

    def run(self, stop: threading.Event, interval_s: float = 1.0) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except Exception:  # the scheduler isolates jobs; this guards the loop itself
                log.exception("brain tick failed")
            stop.wait(interval_s)

"""Configuration schema (R-009, R-010). Loaded from YAML, validated, frozen.

Two sections:
  * everything outside `owner` may be changed by the owner through Telegram confirm flows and, via
    the learning pipeline, by promoted bundles;
  * `owner` (risk limits, permissions, safety, profile, live mode) is owner-only, hash-checked at
    startup and before every order, and never writable by bots (D-007).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from goldtrader.config import ceilings as C

Profile = Literal["normal", "micro_live"]


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AccountConfig(Frozen):
    symbol: str = "XAUUSDm"  # Standard suffix -m (D-016); verified on account (KU-019)
    environment: Literal["demo", "live"] = "demo"
    account_type: Literal["standard", "standard_cent", "pro", "raw_spread", "zero"] = "standard"
    commission_per_lot_per_side_usd: float = 0.0  # Standard: 0; Raw 3.5; Zero 5.5 (Verified)
    leverage_hint: int = 500  # recommendation only; real value from account_info()


class HoursConfig(Frozen):
    """Exness XAUUSD daily schedule in server time (UTC+0). Verified 2026-11-04; shifts with US DST."""

    weekday_open: str = "00:00:00"
    weekday_close: str = "21:58:00"  # winter; summer 20:58
    close_only_start: str = "23:00:30"  # winter; summer 22:00:30
    reopen: str = "23:02:00"  # winter; summer 22:02
    sunday_open: str = "23:05:00"  # winter; summer 22:05
    friday_close: str = "21:58:00"  # winter; summer 20:58
    summer_shift_hours: int = -1  # applied when US DST is in effect


class EventWindow(Frozen):
    before_min: int
    after_min: int


class SessionConfig(Frozen):
    hours: HoursConfig = HoursConfig()
    dst_mode: Literal["auto", "winter", "summer"] = "auto"
    pre_break_buffer_min: int = 15
    stale_tick_seconds: int = 10
    news_high: EventWindow = EventWindow(before_min=30, after_min=15)
    fix_window: EventWindow = EventWindow(before_min=3, after_min=3)
    fix_times_london: tuple[str, ...] = ("10:30", "15:00")
    trade_only_hours_utc: tuple[int, int] | None = None  # e.g. (7, 17) restricts entries


class StrategyConfig(Frozen):
    id: str
    kind: Literal["trend_atr", "breakout_session", "flat"] = "flat"
    enabled: bool = False
    mode: Literal["shadow", "demo", "live"] = "shadow"
    params: dict[str, float] = Field(default_factory=dict)
    stop_atr_min: float = 0.3
    stop_atr_max: float = 1.5
    min_reward_risk: float = 1.0
    allow_discretionary: bool = False
    fallback_mode: Literal["flat"] = "flat"


class DataConfig(Frozen):
    tick_poll_ms: int = 250
    tick_poll_closed_s: int = 5
    tick_dir: str = "data/ticks"
    bars_dir: str = "data/bars"
    db_dir: str = "state"


class LLMTier(Frozen):
    model: str
    timeout_s: int
    max_cost_usd: float
    effort: Literal["low", "medium", "high"] = "low"


class LLMConfig(Frozen):
    provider: Literal["anthropic", "local", "fake"] = "anthropic"
    tiers: dict[str, LLMTier] = Field(
        default_factory=lambda: {
            "fast": LLMTier(
                model="claude-haiku-5-5", timeout_s=20, max_cost_usd=0.02, effort="low"
            ),
            "standard": LLMTier(
                model="claude-sonnet-5-5", timeout_s=45, max_cost_usd=0.10, effort="medium"
            ),
            "deep": LLMTier(
                model="claude-opus-5-5", timeout_s=900, max_cost_usd=5.0, effort="high"
            ),
        }
    )
    daily_budget_usd: float = 3.0
    monthly_budget_usd: float = 60.0


class FeedConfig(Frozen):
    name: str
    url: str
    kind: Literal["rss", "atom", "auto"] = "auto"
    enabled: bool = True


class FeedsConfig(Frozen):
    """News and calendar sources (D-038). Defaults are official publishers only."""

    news: tuple[FeedConfig, ...] = (
        FeedConfig(name="fed_press", url="https://www.federalreserve.gov/feeds/press_all.xml"),
        FeedConfig(name="bls_latest", url="https://www.bls.gov/feed/bls_latest.rss"),
    )
    poll_minutes: int = 10
    bls_ics_url: str = "https://www.bls.gov/schedule/news_release/bls.ics"
    forexfactory_json_url: str | None = None  # owner enables after reading the terms (KU-013)
    calendar_refresh_hours: int = 12
    max_item_age_hours: int = 48


class ChatConfig(Frozen):
    """Telegram chat assistant (D-039)."""

    enabled: bool = True
    poll_seconds: int = 2
    confirm_ttl_s: int = 120
    history_turns: int = 12
    tier: str = "standard"
    deep_tier_on_request: bool = True
    quiet_hours_utc: tuple[int, int] | None = None


class RiskLimits(Frozen):
    risk_per_trade_pct: float = 0.5
    daily_loss_pct: float = 2.0
    weekly_loss_pct: float = 4.0
    max_drawdown_pct: float = 10.0
    max_positions: int = 1
    max_volume_lots: float = 0.01
    max_spread_pips: float = 60.0
    spread_median_multiple: float = 2.0
    max_orders_per_minute: int = 5
    min_reward_risk: float = 1.0
    stop_atr_min: float = 0.3
    stop_atr_max: float = 1.5
    consecutive_loss_pause: int = 3
    cooldown_min: int = 60
    min_margin_level_pct: float = 300.0
    min_equity_to_enter_usd: float = 0.0
    kill_on_disconnect_min: int = 5


class OwnerConfig(Frozen):
    """Owner-only section. Bots have no write path (R-010, R-034)."""

    profile: Profile = "normal"
    mode: Literal["shadow", "demo", "live"] = "demo"
    risk: RiskLimits = RiskLimits()
    prohibited_logic: tuple[str, ...] = ("martingale", "grid", "averaging_down")
    bot_permissions: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    telegram_owner_chat_id: int | None = None


class Config(Frozen):
    version: int = 1
    account: AccountConfig = AccountConfig()
    session: SessionConfig = SessionConfig()
    data: DataConfig = DataConfig()
    strategies: tuple[StrategyConfig, ...] = ()
    llm: LLMConfig = LLMConfig()
    feeds: FeedsConfig = FeedsConfig()
    chat: ChatConfig = ChatConfig()
    owner: OwnerConfig = OwnerConfig()

    @model_validator(mode="after")
    def _check_ceilings(self) -> Config:
        c = C.BY_PROFILE[self.owner.profile]
        r = self.owner.risk
        errors: list[str] = []

        def over(name: str, value: float, limit: float) -> None:
            if value > limit:
                errors.append(
                    f"owner.risk.{name}={value} exceeds ceiling {limit} for profile "
                    f"{self.owner.profile}"
                )

        def under(name: str, value: float, limit: float) -> None:
            if value < limit:
                errors.append(f"{name}={value} below floor {limit}")

        over("risk_per_trade_pct", r.risk_per_trade_pct, c.risk_per_trade_pct)
        over("daily_loss_pct", r.daily_loss_pct, c.daily_loss_pct)
        over("weekly_loss_pct", r.weekly_loss_pct, c.weekly_loss_pct)
        over("max_drawdown_pct", r.max_drawdown_pct, c.max_drawdown_pct)
        over("max_positions", r.max_positions, c.max_positions)
        over("max_volume_lots", r.max_volume_lots, c.max_volume_lots)
        over("max_spread_pips", r.max_spread_pips, c.max_spread_pips)
        over("max_orders_per_minute", r.max_orders_per_minute, c.max_orders_per_minute)
        over("stop_atr_max", r.stop_atr_max, c.max_stop_atr)
        under("owner.risk.min_reward_risk", r.min_reward_risk, c.min_reward_risk)
        under("owner.risk.cooldown_min", r.cooldown_min, c.min_cooldown_min)
        under("owner.risk.min_margin_level_pct", r.min_margin_level_pct, c.min_margin_level_pct)
        under(
            "session.pre_break_buffer_min",
            self.session.pre_break_buffer_min,
            c.min_pre_break_buffer_min,
        )
        under(
            "session.news_high.before_min", self.session.news_high.before_min, c.min_news_before_min
        )
        under("session.news_high.after_min", self.session.news_high.after_min, c.min_news_after_min)
        under(
            "session.fix_window.before_min",
            self.session.fix_window.before_min,
            c.min_fix_window_min,
        )
        if r.stop_atr_min <= 0 or r.stop_atr_min > r.stop_atr_max:
            errors.append("owner.risk.stop_atr_min must be > 0 and <= stop_atr_max")
        if self.owner.mode == "live" and self.account.environment != "live":
            errors.append("owner.mode=live requires account.environment=live")
        if self.owner.profile == "micro_live" and r.max_volume_lots > 0.01:
            errors.append("micro_live profile allows at most 0.01 lot")
        for s in self.strategies:
            if s.kind in self.owner.prohibited_logic:
                errors.append(f"strategy {s.id} uses prohibited logic {s.kind}")
            if s.stop_atr_max > c.max_stop_atr:
                errors.append(f"strategy {s.id}.stop_atr_max exceeds ceiling {c.max_stop_atr}")
        if errors:
            raise ValueError("; ".join(errors))
        return self

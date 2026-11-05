"""Hard ceilings compiled into code (RISK_POLICY.md §1–2). Config cannot exceed them.

Changing these requires an owner decision recorded in docs/DECISIONS.md. Two profiles:
`normal` for ordinary capital and `micro_live` for the owner exception D-017.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Ceilings:
    risk_per_trade_pct: float
    daily_loss_pct: float
    weekly_loss_pct: float
    max_drawdown_pct: float
    max_positions: int
    max_volume_lots: float
    max_spread_pips: float
    max_orders_per_minute: int
    min_pre_break_buffer_min: int
    min_news_before_min: int
    min_news_after_min: int
    min_fix_window_min: int
    max_stop_atr: float
    min_reward_risk: float
    min_cooldown_min: int
    min_margin_level_pct: float


NORMAL = Ceilings(
    risk_per_trade_pct=2.0,
    daily_loss_pct=5.0,
    weekly_loss_pct=10.0,
    max_drawdown_pct=20.0,
    max_positions=3,
    max_volume_lots=1.0,
    max_spread_pips=100.0,
    max_orders_per_minute=20,
    min_pre_break_buffer_min=5,
    min_news_before_min=15,
    min_news_after_min=5,
    min_fix_window_min=1,
    max_stop_atr=3.0,
    min_reward_risk=0.5,
    min_cooldown_min=15,
    min_margin_level_pct=150.0,
)

# Owner exception D-017: live capital under 50 USD on a Standard account (1 oz minimum).
MICRO_LIVE = Ceilings(
    risk_per_trade_pct=30.0,
    daily_loss_pct=40.0,
    weekly_loss_pct=60.0,
    max_drawdown_pct=60.0,
    max_positions=1,
    max_volume_lots=0.01,
    max_spread_pips=100.0,
    max_orders_per_minute=20,
    min_pre_break_buffer_min=5,
    min_news_before_min=15,
    min_news_after_min=5,
    min_fix_window_min=1,
    max_stop_atr=3.0,
    min_reward_risk=0.5,
    min_cooldown_min=15,
    min_margin_level_pct=150.0,
)

BY_PROFILE = {"normal": NORMAL, "micro_live": MICRO_LIVE}

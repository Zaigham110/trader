"""Cost model for backtests and live P&L attribution (D-009, D-014, R-007).

Costs are always taken from the bar's own bid/ask (never a fixed spread). The multiplier stresses
the observed spread; slippage is added against the trader on every fill; stops fill at the worse
of the stop level and the first available price after the gap.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from goldtrader.types import SymbolSpec


@dataclass(frozen=True)
class CostModel:
    spread_multiplier: float = 1.0        # 1.5 in the offline gate (LEARNING_SYSTEM §3.2)
    slippage_points: float = 1.0          # per market fill, against the trader (Assumption)
    commission_per_lot_per_side_usd: float = 0.0   # Standard account: 0 (Verified)
    stop_gap_fill: bool = True            # fill stops at the worse of level and next price

    def as_dict(self) -> dict:
        return asdict(self)

    def entry_price(self, side: str, bid: float, ask: float, spec: SymbolSpec) -> float:
        """Market entry: long pays ask, short receives bid; stressed spread and slippage applied."""
        half_extra = (ask - bid) * (self.spread_multiplier - 1.0) / 2.0
        slip = self.slippage_points * spec.point
        if side == "long":
            return ask + half_extra + slip
        return bid - half_extra - slip

    def exit_price(self, side: str, bid: float, ask: float, spec: SymbolSpec) -> float:
        half_extra = (ask - bid) * (self.spread_multiplier - 1.0) / 2.0
        slip = self.slippage_points * spec.point
        if side == "long":
            return bid - half_extra - slip
        return ask + half_extra + slip

    def commission(self, volume: float) -> float:
        """Round-trip commission in USD (two sides)."""
        return 2.0 * self.commission_per_lot_per_side_usd * volume

    @staticmethod
    def spread_cost(volume: float, bid: float, ask: float, spec: SymbolSpec,
                    multiplier: float = 1.0) -> float:
        """USD cost of crossing the (stressed) spread once for this volume."""
        return (ask - bid) * multiplier * volume * spec.trade_contract_size

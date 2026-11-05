"""Symbol specification helpers (docs/ARCHITECTURE.md §2.1 `spec`, R-001, KU-001).

`read_spec` reads `symbol_info()` through any `MT5Client`; `diff_specs` / `critical_changes` compare
two readings so the gateway can fail closed when a critical field (SymbolSpec.CRITICAL: contract
size, volume step, digits, point, trade mode, execution mode, filling mode, profit currency)
changes between startup and the daily re-read. `find_gold_symbol` resolves the account-suffixed
gold symbol (KU-019) and `derive_money` reproduces the money math of
docs/MT5_EXNESS_INTEGRATION.md §2.
"""
from __future__ import annotations

from dataclasses import fields

from goldtrader.mt5.protocol import MT5Client, MT5Error
from goldtrader.types import SymbolSpec

# Fields that legitimately change tick by tick and are never reported as a spec change.
VOLATILE_FIELDS: frozenset[str] = frozenset({"spread"})


def read_spec(client: MT5Client, symbol: str) -> SymbolSpec:
    """Select `symbol` in Market Watch and return its `SymbolSpec`; raises MT5Error if unknown."""
    if not client.symbol_select(symbol, True):
        raise MT5Error(f"symbol_select({symbol!r}) failed: {client.last_error()}")
    spec = client.symbol_info(symbol)
    if spec is None:
        raise MT5Error(f"symbol_info({symbol!r}) returned None: {client.last_error()}")
    return spec


def diff_specs(old: SymbolSpec, new: SymbolSpec) -> list[str]:
    """Names of the fields whose value differs (declaration order; `spread` is ignored)."""
    return [
        f.name for f in fields(SymbolSpec)
        if f.name != "CRITICAL" and f.name not in VOLATILE_FIELDS
        and getattr(old, f.name) != getattr(new, f.name)
    ]


def critical_changes(old: SymbolSpec, new: SymbolSpec) -> list[str]:
    """Subset of `diff_specs` that must fail closed (docs/ARCHITECTURE.md §5 "Spec change")."""
    changed = set(diff_specs(old, new))
    return [name for name in old.CRITICAL if name in changed]  # slots: read via instance


def find_gold_symbol(client: MT5Client, preferred: str = "XAUUSDm") -> str:
    """Resolve the gold symbol on this account from `symbols_get(group='*XAU*')`.

    Order: the exact `preferred` name; else the first symbol starting with `XAUUSD` that is not
    the 24/7 contract (`XAUUSD247*`, a separate fixed-margin instrument). Raises MT5Error when
    none is found so the gateway never trades a guessed name.
    """
    names = client.symbols_get(group="*XAU*")
    if preferred in names:
        return preferred
    for name in names:
        if name.startswith("XAUUSD") and not name.startswith("XAUUSD247"):
            return name
    raise MT5Error(f"no gold symbol found (preferred {preferred!r}; candidates {names})")


def derive_money(spec: SymbolSpec, price: float, volume: float, leverage: int) -> dict[str, float]:
    """Money math from the spec (docs/MT5_EXNESS_INTEGRATION.md §2, Verified formulas).

    * `pip_value_per_lot`: profit-currency value of one `point` move on 1.00 lot
      (= contract_size × point; 1.00 USD for gold).
    * `usd_per_point_for_volume`: the same for `volume` lots.
    * `usd_per_unit_for_volume`: P&L of a 1.00 price move on `volume` lots.
    * `margin_dynamic`: `volume × contract × price / leverage` (dynamic-leverage instruments).
    * `notional`: `volume × contract × price`.
    """
    if leverage <= 0:
        raise ValueError("leverage must be positive")
    contract = spec.trade_contract_size
    pip_value_per_lot = contract * spec.point
    return {
        "pip_value_per_lot": round(pip_value_per_lot, 6),
        "usd_per_point_for_volume": round(pip_value_per_lot * volume, 6),
        "usd_per_unit_for_volume": round(contract * volume, 6),
        "notional": round(volume * contract * price, 2),
        "margin_dynamic": round(volume * contract * price / leverage, 2),
    }


__all__ = [
    "VOLATILE_FIELDS",
    "critical_changes",
    "derive_money",
    "diff_specs",
    "find_gold_symbol",
    "read_spec",
]

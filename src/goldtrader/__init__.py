"""goldtrader — AI gold (XAUUSD) trading system for Exness via MetaTrader 5.

Package layout (see docs/ARCHITECTURE.md):
  config/   versioned YAML config, owner section, ceilings, hashes      (R-009, R-010)
  mt5/      MT5 client protocol, Windows adapter, FakeMT5, spec reader  (R-001)
  session/  Exness gold schedule, DST, session state, event windows     (R-002, R-006)
  data/     Exness tick CSV loader, resampling, features, synthetic     (Phase 3)
  backtest/ engine, costs, metrics, validation statistics, ledger       (Phase 3)
  db/       SQLite schema and repositories                              (D-024)
"""

__version__ = "0.1.0"

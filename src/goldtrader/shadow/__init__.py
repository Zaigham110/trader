"""shadow — simulate candidate bundles on live bars; orders are logged, never sent (D-047,
LEARNING_SYSTEM.md §3.3 shadow gate). Imports nothing from the broker, execution or gateway state."""

from goldtrader.shadow.ledger import ShadowLedger, ShadowPosition

__all__ = ["ShadowLedger", "ShadowPosition"]

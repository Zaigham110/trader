"""MT5 access layer. Only `goldtrader.mt5.real` imports the MetaTrader5 package (Windows only)."""

from goldtrader.mt5.fake import FakeMT5  # noqa: F401
from goldtrader.mt5.protocol import (  # noqa: F401
    MT5Client,
    MT5Error,
    MT5NotConnected,
    MT5OrderRejected,
    MT5TradeDisabled,
    classify_retcode,
)
from goldtrader.mt5.spec import (  # noqa: F401
    critical_changes,
    derive_money,
    diff_specs,
    find_gold_symbol,
    read_spec,
)

"""execution — the only code path that places, modifies or closes orders (R-003).

`ExecutionEngine` (orders, stops, closes, kill switch), `Journal` (orders / fills / positions /
trades / snapshots / incidents / alerts, R-007, R-054), `Reconciler` (broker vs journal every 60 s,
R-008), `IntentHandler` (brain intents → engine) and `Throttle` (≤ 1 request/s, backoff on 10024).
"""

from goldtrader.execution.engine import (  # noqa: F401
    ALERT_RETCODES,
    DEFAULT_DEVIATION_POINTS,
    ExecutionEngine,
    filling_for,
    magic_from_bundle,
)
from goldtrader.execution.intents import IntentHandler, StateProvider  # noqa: F401
from goldtrader.execution.journal import KILL_INCIDENT_KIND, Journal, slippage_points  # noqa: F401
from goldtrader.execution.reconciler import (  # noqa: F401
    ReconcileReport,
    Reconciler,
    exit_reason_from_deals,
)
from goldtrader.execution.throttle import Throttle  # noqa: F401

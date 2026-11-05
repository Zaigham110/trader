"""learning — trial ledger intake, offline gate, promotion pipeline, monitors, rollback
(LEARNING_SYSTEM.md). Has no write path to the owner section of config (R-034, D-045)."""

from goldtrader.learning.evalue import EProcess, daily_pairs  # noqa: F401
from goldtrader.learning.gates import LearningGates, gates_for  # noqa: F401
from goldtrader.learning.immutable import OwnerGuard, assert_owner_untouched, guard  # noqa: F401
from goldtrader.learning.intake import ensure_champion, hypothesis_to_change, propose  # noqa: F401
from goldtrader.learning.offline import run_offline  # noqa: F401
from goldtrader.learning.pipeline import Pipeline, allocation_bucket  # noqa: F401
from goldtrader.learning.store import LearningStore  # noqa: F401
from goldtrader.learning.types import (  # noqa: F401
    ACTIVE_STAGES,
    NEXT_STAGE,
    Change,
    Experiment,
    GateCheck,
    Stage,
    StageDecision,
)

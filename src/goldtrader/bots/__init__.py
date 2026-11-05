"""bots — AgentSpecs for Research, Strategy, Risk-advisor and Review (AGENTS.md §3).

LLMs propose; code decides (AGENTS §1). Nothing here imports MT5, the execution engine or the
gateway state builder; bots receive a `BotContext` and return schema-validated proposals that
`brain.gates` and the gateway's risk engine judge.
"""

from goldtrader.bots.common import (  # noqa: F401
    BotContext,
    ContextCell,
    clip,
    context_hash,
    defang,
    wrap_untrusted,
)
from goldtrader.bots.registry import (  # noqa: F401
    BOT_NAMES,
    DEFAULT_PERMISSIONS,
    BotRegistry,
    active_strategy,
    check_permissions,
)
from goldtrader.bots.research import (  # noqa: F401
    Hypothesis,
    HypothesesOut,
    flag_lookahead,
    research_spec,
    weekly_research_spec,
)
from goldtrader.bots.review import (  # noqa: F401
    Brief,
    BriefSection,
    attribution,
    daily_brief_spec,
    dedupe_lessons,
    review_spec,
)
from goldtrader.bots.risk_advisor import RiskBudget, hard_rules, risk_advisor_spec  # noqa: F401
from goldtrader.bots.strategy import strategy_spec  # noqa: F401

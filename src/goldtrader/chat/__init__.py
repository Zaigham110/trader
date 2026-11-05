"""Telegram chat assistant (AGENTS.md §3.5; PRD R-040..R-043; D-021, D-039).

Read-only tools with provenance (`tools`), prepare-then-confirm actions (`actions`), the Chat bot
spec and post-processor (`agent`), and the owner-only long-polling loop (`telegram_loop`). Nothing
here imports MT5 or the execution engine; confirmed actions reach the gateway only as intents with
``source="owner_confirmed"`` or rewrite the non-owner config through the same schema.
"""

from goldtrader.chat.actions import (  # noqa: F401
    ACTION_TYPES,
    ActionContext,
    ActionError,
    Prepared,
    execute_confirmed,
    prepare_action,
)
from goldtrader.chat.agent import ChatReply, ChatTurn, chat_spec, postprocess  # noqa: F401
from goldtrader.chat.telegram_loop import (  # noqa: F401
    ChatService,
    FakeTelegramUpdates,
    HttpTelegramUpdates,
    TelegramUpdates,
    status_text,
)
from goldtrader.chat.tools import ToolContext, chat_tool_specs, chat_tools, scorecard  # noqa: F401

"""Owner notifications: Telegram sender, alert outbox, confirmation primitives, log redaction.

ARCHITECTURE.md §2.2 `telegram` row. Chat itself (the Chat bot) is Phase 5; this package holds the
alert sender, the outbox with cursors and quiet hours, the confirm-button state machine primitives
and the redaction filter (R-053).
"""

from goldtrader.notify.alerts import AlertDispatcher, format_alert, format_trade_alert
from goldtrader.notify.confirm import ConfirmError, ConfirmResult, ConfirmStore, PendingAction
from goldtrader.notify.redact import Redactor, RedactingFilter, install, scan_for_secrets
from goldtrader.notify.telegram import (
    Button,
    FakeTelegramSender,
    HttpTelegramSender,
    TelegramError,
    TelegramSender,
)

__all__ = [
    "AlertDispatcher",
    "Button",
    "ConfirmError",
    "ConfirmResult",
    "ConfirmStore",
    "FakeTelegramSender",
    "HttpTelegramSender",
    "PendingAction",
    "RedactingFilter",
    "Redactor",
    "TelegramError",
    "TelegramSender",
    "format_alert",
    "format_trade_alert",
    "install",
    "scan_for_secrets",
]

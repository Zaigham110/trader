"""Secret redaction for logs and outgoing text (R-053).

Credentials must never reach config, logs or Telegram. :class:`Redactor` replaces every configured
secret and every common token shape (Telegram bot tokens, Anthropic API keys) with ``***``;
:class:`RedactingFilter` applies it to log records *and* their formatting arguments so a secret
passed as ``%s`` is masked too; :func:`install` attaches the filter to a logger and its handlers.
:func:`scan_for_secrets` is the test helper that greps a log capture for leaks (ARCHITECTURE.md §6).
"""

from __future__ import annotations

import logging
import re
import traceback
from collections.abc import Iterable, Mapping
from typing import Any

MASK = "***"

#: Token shapes redacted even when they were never registered as a secret.
TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Telegram bot token: <bot id>:<35 url-safe chars>
    re.compile(r"(?<![0-9A-Za-z_-])\d{8,10}:[A-Za-z0-9_-]{35}(?![0-9A-Za-z_-])"),
    # Anthropic API key
    re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"),
)

_MIN_SECRET_LEN = 4  # shorter "secrets" would mask ordinary words (e.g. "1" in every number)


class Redactor:
    """Replace registered secrets and known token shapes with :data:`MASK`.

    Secrets shorter than four characters are ignored (they would blank out unrelated text);
    longer secrets are matched first so a secret that contains another is masked in one piece.
    """

    def __init__(self, secrets: Iterable[str] | None = None):
        cleaned = {s for s in (secrets or ()) if s and len(s) >= _MIN_SECRET_LEN}
        self._secrets: list[str] = sorted(cleaned, key=len, reverse=True)
        self._pattern: re.Pattern[str] | None = (
            re.compile("|".join(re.escape(s) for s in self._secrets)) if self._secrets else None
        )

    @property
    def secrets(self) -> tuple[str, ...]:
        return tuple(self._secrets)

    def add(self, secret: str) -> None:
        """Register one more secret (e.g. a password read after start-up)."""
        if secret and len(secret) >= _MIN_SECRET_LEN and secret not in self._secrets:
            self._secrets.append(secret)
            self._secrets.sort(key=len, reverse=True)
            self._pattern = re.compile("|".join(re.escape(s) for s in self._secrets))

    def redact(self, text: str) -> str:
        """Return ``text`` with every secret and token shape replaced by ``***``."""
        out = text
        if self._pattern is not None:
            out = self._pattern.sub(MASK, out)
        for pat in TOKEN_PATTERNS:
            out = pat.sub(MASK, out)
        return out

    __call__ = redact

    def redact_value(self, value: Any) -> Any:
        """Redact strings inside common containers; other values pass through unchanged."""
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, Mapping):
            return {k: self.redact_value(v) for k, v in value.items()}
        if isinstance(value, tuple):
            return tuple(self.redact_value(v) for v in value)
        if isinstance(value, list):
            return [self.redact_value(v) for v in value]
        if isinstance(value, BaseException):
            # Exceptions (urllib errors, MT5 errors) often embed URLs or passwords in their text.
            return self.redact(repr(value))
        return value


class RedactingFilter(logging.Filter):
    """``logging.Filter`` that masks secrets in ``record.msg``, ``record.args`` and exc text.

    Returns ``True`` always: it never drops a record, it only rewrites it in place.
    """

    def __init__(self, redactor: Redactor):
        super().__init__(name="")
        self.redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self.redactor.redact(record.msg)
        elif not isinstance(record.msg, int | float | bool | type(None)):
            record.msg = self.redactor.redact(str(record.msg))
        if record.args:
            record.args = self.redactor.redact_value(record.args)  # type: ignore[assignment]
        if record.exc_info and not record.exc_text and record.exc_info[0] is not None:
            # Pre-render the traceback so the formatter reuses this redacted text (it caches
            # exc_text) instead of formatting exc_info itself after the filter ran.
            record.exc_text = "".join(traceback.format_exception(*record.exc_info))
        if record.exc_text:
            record.exc_text = self.redactor.redact(record.exc_text)
        return True


def install(logger: logging.Logger, secrets: Iterable[str] | None = None) -> RedactingFilter:
    """Attach a :class:`RedactingFilter` to ``logger`` and to each of its handlers.

    The filter on the handlers also covers records propagated from child loggers; the filter on
    the logger itself covers records logged directly through it before any handler sees them.
    Idempotent per logger: a second call replaces the previous filter's secrets.
    """
    for existing in list(logger.filters):
        if isinstance(existing, RedactingFilter):
            logger.removeFilter(existing)
    for h in logger.handlers:
        for existing in list(h.filters):
            if isinstance(existing, RedactingFilter):
                h.removeFilter(existing)
    flt = RedactingFilter(Redactor(secrets))
    logger.addFilter(flt)
    for h in logger.handlers:
        h.addFilter(flt)
    return flt


def scan_for_secrets(text: str, secrets: Iterable[str] | None = None) -> list[str]:
    """Return every registered secret and token-shaped string found in ``text`` (R-053 test helper).

    An empty list means the capture is clean. Short secrets (< 4 chars) are ignored like in
    :class:`Redactor`.
    """
    found: list[str] = []
    for s in secrets or ():
        if s and len(s) >= _MIN_SECRET_LEN and s in text and s not in found:
            found.append(s)
    for pat in TOKEN_PATTERNS:
        for m in pat.finditer(text):
            if m.group(0) not in found:
                found.append(m.group(0))
    return found

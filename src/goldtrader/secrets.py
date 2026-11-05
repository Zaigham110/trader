"""Secret access (R-053). Secrets never live in config, logs, Telegram or the repository.

Resolution order: the process environment (`GOLDTRADER_<NAME>` or the legacy `MT5_*` names), then
the OS credential store through the optional `keyring` package (Windows Credential Manager on the
owner's PC). Values are returned once to the caller and never cached in module state.
"""

from __future__ import annotations

import os

SERVICE = "goldtrader"
KNOWN = (
    "MT5_LOGIN",
    "MT5_PASSWORD",
    "MT5_SERVER",
    "ANTHROPIC_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_OWNER_CHAT_ID",
    "GATEWAY_TOKEN",
    "ALLOW_LIVE",
)


class MissingSecret(KeyError):
    pass


def get_secret(name: str, *, required: bool = True) -> str | None:
    env = os.environ.get(f"GOLDTRADER_{name}") or os.environ.get(name)
    if env:
        return env
    try:
        import keyring  # type: ignore[import-not-found]

        v = keyring.get_password(SERVICE, name)
        if v:
            return v
    except Exception:  # keyring absent or backend unavailable: fall through
        pass
    if required:
        raise MissingSecret(
            f"secret {name} not found: set GOLDTRADER_{name} in the environment or store it with "
            f"`goldctl secret-set {name}` (keyring)"
        )
    return None


def set_secret(name: str, value: str) -> str:
    """Store in the OS credential store; returns where it went. Never echo the value."""
    import keyring  # type: ignore[import-not-found]

    keyring.set_password(SERVICE, name, value)
    return "keyring"


def all_secret_values() -> list[str]:
    """Known secret values present in this process, for log redaction."""
    out = []
    for n in KNOWN:
        v = get_secret(n, required=False)
        if v and n not in ("MT5_LOGIN", "MT5_SERVER", "TELEGRAM_OWNER_CHAT_ID"):
            out.append(v)
    return out

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
    "TELEGRAM_API_ID",  # signal reader (D-061): the owner's app id from my.telegram.org
    "TELEGRAM_API_HASH",
    "GATEWAY_TOKEN",
    "ALLOW_LIVE",
    # the live instance's own copies (D-081); see `scoped`
    "MT5_LOGIN_LIVE",
    "MT5_PASSWORD_LIVE",
    "MT5_SERVER_LIVE",
    "GATEWAY_TOKEN_LIVE",
    "TELEGRAM_BOT_TOKEN_LIVE",
)
# Secrets that belong to one instance. A live config reads only the `_LIVE` name and never falls
# back to the demo one: the live gateway must not log in with the demo account's details, the demo
# brain must not hold a token the live gateway accepts, and two brains cannot poll one Telegram bot.
SCOPED = ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "GATEWAY_TOKEN", "TELEGRAM_BOT_TOKEN")


def scoped(name: str, environment: str) -> str:
    """The secret name an instance uses: `<NAME>_LIVE` for a live config, else the plain name."""
    return f"{name}_LIVE" if environment == "live" and name in SCOPED else name


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


def inspect_secret(value: str | None) -> tuple[str, list[str]]:
    """Describe a secret's shape without revealing it: (summary, problems).

    Catches the Windows console paste failure where a hidden-input prompt receives Ctrl+V as the
    single character 0x16 (observed 2026-11-17), stray quotes and whitespace, and the wrong kind of
    Anthropic credential.
    """
    if value is None:
        return "absent", []
    problems: list[str] = []
    if value != value.strip():
        problems.append("leading/trailing whitespace")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        problems.append("control characters (Ctrl+V was typed into the prompt, not pasted)")
    elif any(ord(c) > 126 for c in value):
        problems.append("non-ASCII characters (paste artefact)")
    if len(value) >= 2 and value[0] in "'\"" and value[-1] in "'\"":
        problems.append("wrapped in quotes")
    if len(value.strip()) <= 2:
        problems.append("too short to be a real value")
    if value.startswith("sk-ant-oat"):
        problems.append("Claude Code OAuth token; the API needs a Console key (sk-ant-api...)")
    return f"length {len(value)}", problems


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
        if (
            v
            and not n.startswith(("MT5_LOGIN", "MT5_SERVER"))
            and n
            not in (
                "TELEGRAM_OWNER_CHAT_ID",
                "TELEGRAM_API_ID",
            )
        ):
            out.append(v)
    return out

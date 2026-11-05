"""Prompt registry: prompts are versioned data with hashes, not constants in bot files (R-025, R-033).

Every bot registers its system prompt once at import time; `prompts_hash()` over the whole registry
is stamped on bundles (`bundles.prompt_hashes_json`) so a prompt change is a tracked version change.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class PromptRef:
    """A registered prompt. `hash` is sha256(text)[:16]; the text travels with the ref."""

    name: str
    version: int
    hash: str
    text: str


class PromptRegistry:
    """In-process registry; the module-level functions use `DEFAULT`."""

    def __init__(self) -> None:
        self._prompts: dict[str, PromptRef] = {}

    def register(self, name: str, version: int, text: str) -> PromptRef:
        """Register `text` under `name`. Re-registering the same name is allowed only with the same
        version and text (idempotent imports); anything else raises so a silent prompt drift
        cannot happen."""
        ref = PromptRef(name=name, version=version, hash=_sha16(text), text=text)
        existing = self._prompts.get(name)
        if existing is not None and existing != ref:
            raise ValueError(
                f"prompt {name!r} already registered as v{existing.version} ({existing.hash}); "
                f"bump the version in a new name or clear the registry"
            )
        self._prompts[name] = ref
        return ref

    def get(self, name: str) -> PromptRef:
        try:
            return self._prompts[name]
        except KeyError as e:
            raise KeyError(f"prompt {name!r} is not registered") from e

    def all(self) -> list[PromptRef]:
        return [self._prompts[k] for k in sorted(self._prompts)]

    def prompts_hash(self) -> str:
        """sha256 (16 hex) over every (name, version, hash) in name order; changes when any prompt
        text changes (R-033 bundle stamping)."""
        h = hashlib.sha256()
        for ref in self.all():
            h.update(f"{ref.name}\x00{ref.version}\x00{ref.hash}\n".encode())
        return h.hexdigest()[:16]

    def clear(self) -> None:
        self._prompts.clear()


DEFAULT = PromptRegistry()


def register(name: str, version: int, text: str) -> PromptRef:
    return DEFAULT.register(name, version, text)


def get(name: str) -> PromptRef:
    return DEFAULT.get(name)


def prompts_hash() -> str:
    return DEFAULT.prompts_hash()

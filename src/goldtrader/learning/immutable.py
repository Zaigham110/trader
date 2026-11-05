"""Owner-section immutability guard for the learning package (LEARNING_SYSTEM §2, R-034, D-045).

The owner section of config (risk limits, permissions, safety, the learning gates) is hash-checked
at startup and before every order; the learning module must have no path that changes it. This
module enforces that at runtime: `OwnerGuard` remembers the owner hash a pipeline was built with
and `check()` raises `PermissionError` if the config it is handed later carries a different owner
section; `guard` wraps a method so the owner hash must be identical before and after the call.
`tests/test_learning_immutable.py` adds the static half: no learning module imports a config
writer, `yaml.dump`, the gateway, MT5 or the execution engine.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from goldtrader.config.loader import owner_hash


def assert_owner_untouched(cfg_before_hash: str, cfg_after_hash: str) -> None:
    """Raise PermissionError unless the two owner hashes are identical (R-034)."""
    if cfg_before_hash != cfg_after_hash:
        raise PermissionError(
            f"owner section changed during a learning call ({cfg_before_hash} → {cfg_after_hash}); "
            "the learning module may not touch owner config (R-034, D-045)"
        )


class OwnerGuard:
    """Pins the owner hash of the config a learning component was constructed with."""

    def __init__(self, cfg: Any) -> None:
        self.expected = owner_hash(cfg)

    def check(self, cfg: Any) -> str:
        """Return the hash when `cfg.owner` still matches; raise PermissionError otherwise."""
        h = owner_hash(cfg)
        assert_owner_untouched(self.expected, h)
        return h


def guard[F: Callable[..., Any]](fn: F) -> F:
    """Method decorator for objects with a `cfg` attribute: the owner hash of `self.cfg` must be
    the same before and after the call, and must equal the hash pinned by `self._owner_guard`
    when the object has one. Wrap `advance` / `promote` / `rollback` with it."""

    @functools.wraps(fn)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        before = owner_hash(self.cfg)
        pinned = getattr(self, "_owner_guard", None)
        if pinned is not None:
            assert_owner_untouched(pinned.expected, before)
        result = fn(self, *args, **kwargs)
        after = owner_hash(self.cfg)
        assert_owner_untouched(before, after)
        return result

    return wrapper  # type: ignore[return-value]

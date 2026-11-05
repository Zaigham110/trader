"""YAML → Config with validation and stable hashes (R-009, R-033)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from goldtrader.config.schema import Config


class ConfigError(Exception):
    pass


def _stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def config_hash(cfg: Config) -> str:
    return hashlib.sha256(_stable_json(cfg.model_dump(mode="json")).encode()).hexdigest()[:16]


def owner_hash(cfg: Config) -> str:
    """Hash of the owner-only section; checked at startup and before every order (RISK_POLICY §4)."""
    return hashlib.sha256(_stable_json(cfg.owner.model_dump(mode="json")).encode()).hexdigest()[:16]


def parse_config(raw: dict[str, Any]) -> Config:
    try:
        return Config.model_validate(raw or {})
    except ValidationError as e:
        raise ConfigError(str(e)) from e
    except ValueError as e:
        raise ConfigError(str(e)) from e


def load_config(path: str | Path) -> Config:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config file not found: {p}")
    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if raw is not None and not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")
    return parse_config(raw or {})

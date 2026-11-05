import pytest

from goldtrader.config import ConfigError, config_hash, load_config, owner_hash
from goldtrader.config.loader import parse_config


def test_demo_and_micro_live_profiles_load():
    demo = load_config("config/demo.yaml")
    micro = load_config("config/micro_live.yaml")
    assert demo.owner.profile == "normal" and micro.owner.profile == "micro_live"
    assert config_hash(demo) != config_hash(micro)
    assert len(owner_hash(demo)) == 16


def test_ceiling_violation_is_rejected():
    with pytest.raises(ConfigError, match="exceeds ceiling"):
        parse_config({"owner": {"profile": "normal", "risk": {"risk_per_trade_pct": 5.0}}})


def test_micro_live_cannot_exceed_one_hundredth_lot():
    with pytest.raises(ConfigError):
        parse_config(
            {
                "owner": {
                    "profile": "micro_live",
                    "risk": {"max_volume_lots": 0.02, "risk_per_trade_pct": 10},
                }
            }
        )


def test_prohibited_logic_is_rejected():
    raw = {"strategies": [{"id": "g", "kind": "grid"}]}
    with pytest.raises(ConfigError):
        parse_config(raw)


def test_live_mode_requires_live_environment():
    with pytest.raises(ConfigError, match="requires account.environment=live"):
        parse_config({"owner": {"mode": "live"}})


def test_owner_hash_changes_only_with_owner_section():
    a = parse_config({"session": {"pre_break_buffer_min": 20}})
    b = parse_config({"session": {"pre_break_buffer_min": 30}})
    c = parse_config({"owner": {"risk": {"daily_loss_pct": 1.0}}})
    assert owner_hash(a) == owner_hash(b)
    assert owner_hash(a) != owner_hash(c)
    assert config_hash(a) != config_hash(b)


def test_unknown_keys_are_rejected():
    with pytest.raises(ConfigError):
        parse_config({"owner": {"risk": {"typo_limit": 1}}})

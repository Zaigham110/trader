"""Spec helpers: read/diff/critical changes, gold symbol discovery, money math."""
from __future__ import annotations

from dataclasses import replace

import pytest

from goldtrader.mt5 import constants as K
from goldtrader.mt5.fake import DEFAULT_SPEC, FakeMT5
from goldtrader.mt5.protocol import MT5Error
from goldtrader.mt5.spec import (
    critical_changes,
    derive_money,
    diff_specs,
    find_gold_symbol,
    read_spec,
)


def test_read_spec_selects_and_returns_spec():
    fake = FakeMT5()
    spec = read_spec(fake, "XAUUSDm")
    assert spec.name == "XAUUSDm" and spec.trade_contract_size == 100.0
    assert "XAUUSDm" in fake.selected
    with pytest.raises(MT5Error, match="symbol_select"):
        read_spec(fake, "NOPE")


def test_diff_specs_lists_changed_fields_in_declaration_order():
    new = replace(DEFAULT_SPEC, swap_long=-25.0, volume_step=0.1, description="x")
    assert diff_specs(DEFAULT_SPEC, new) == ["volume_step", "swap_long", "description"]
    assert diff_specs(DEFAULT_SPEC, DEFAULT_SPEC) == []
    # spread moves tick by tick and is never a spec change
    assert diff_specs(DEFAULT_SPEC, replace(DEFAULT_SPEC, spread=99)) == []


def test_critical_changes_follow_symbolspec_critical():
    benign = replace(DEFAULT_SPEC, swap_long=-25.0, description="x")
    assert critical_changes(DEFAULT_SPEC, benign) == []
    bad = replace(DEFAULT_SPEC, trade_contract_size=1000.0, volume_step=0.1,
                  trade_mode=K.SYMBOL_TRADE_MODE_CLOSEONLY, swap_long=-1.0)
    assert critical_changes(DEFAULT_SPEC, bad) == ["trade_contract_size", "volume_step",
                                                   "trade_mode"]
    for name in DEFAULT_SPEC.CRITICAL:
        assert name in {f for f in dir(DEFAULT_SPEC) if not f.startswith("_")}


def test_find_gold_symbol_prefers_exact_then_prefix_and_skips_247():
    assert find_gold_symbol(FakeMT5(), preferred="XAUUSDm") == "XAUUSDm"
    fake = FakeMT5(symbols=["XAUUSD247m", "XAUEURm", "XAUUSDc", "EURUSDm"])
    assert find_gold_symbol(fake, preferred="XAUUSDm") == "XAUUSDc"
    assert find_gold_symbol(FakeMT5(symbols=["XAUUSD", "XAUUSD247"])) == "XAUUSD"
    with pytest.raises(MT5Error, match="no gold symbol"):
        find_gold_symbol(FakeMT5(symbols=["XAUUSD247m", "EURUSDm"]))
    with pytest.raises(MT5Error):
        find_gold_symbol(FakeMT5(symbols=["EURUSDm"]))


def test_derive_money_matches_integration_doc_examples():
    m = derive_money(DEFAULT_SPEC, price=4000.0, volume=0.01, leverage=500)
    assert m["pip_value_per_lot"] == pytest.approx(1.0)
    assert m["usd_per_point_for_volume"] == pytest.approx(0.01)
    assert m["usd_per_unit_for_volume"] == pytest.approx(1.0)
    assert m["margin_dynamic"] == pytest.approx(8.0)
    assert m["notional"] == pytest.approx(4000.0)
    assert derive_money(DEFAULT_SPEC, 4000.0, 0.01, 2000)["margin_dynamic"] == pytest.approx(2.0)
    assert derive_money(DEFAULT_SPEC, 4000.0, 0.01, 200)["margin_dynamic"] == pytest.approx(20.0)
    assert derive_money(DEFAULT_SPEC, 4000.0, 0.01, 100)["margin_dynamic"] == pytest.approx(40.0)
    with pytest.raises(ValueError):
        derive_money(DEFAULT_SPEC, 4000.0, 0.01, 0)

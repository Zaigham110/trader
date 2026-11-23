"""The Strategies page cannot drift from the code: every kind and parameter is described."""

import json
from dataclasses import fields

from goldtrader import strategy_docs
from goldtrader.backtest.strategies import build_strategy

KINDS = (
    "trend_atr",
    "trend_htf",
    "channel_break",
    "pullback_htf",
    "range_fade",
    "breakout_session",
    "flat",
)


def test_every_strategy_kind_and_parameter_is_described():
    assert set(strategy_docs.KINDS) == set(KINDS)
    for kind in KINDS:
        doc = strategy_docs.DOCS[kind]
        strat = build_strategy(kind, "x", {})
        params = {f.name for f in fields(strat)} - {"id", "kind"}
        missing = params - set(doc["params"])
        assert not missing, f"{kind}: parameters without an explanation: {sorted(missing)}"
        for key in ("name", "family", "summary", "enters", "exits"):
            assert doc[key].strip(), (kind, key)
        for name, p in doc["params"].items():
            assert p["label"] and p["meaning"] and "unit" in p, (kind, name)
        d = doc["diagram"]
        assert len(d["line"]) >= 2 and all(0 <= x <= 100 and 0 <= y <= 100 for x, y in d["line"])


def test_docs_are_plain_data_with_no_dashes_or_links():
    text = json.dumps({"docs": strategy_docs.DOCS, "common": strategy_docs.COMMON})
    assert "http" not in text
    assert set(strategy_docs.COMMON) == {"atr", "r", "trailing", "session", "paper"}

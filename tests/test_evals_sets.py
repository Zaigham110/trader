"""Eval seed sets (D-040; LOCAL_LLM §7): valid enums, coverage, pairs, generator reproducibility."""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from goldtrader.contracts import NewsEvent, PostMortem
from goldtrader.evals.generate_regimes import (
    ATR_PCT_DAILY_VALUES,
    OUT_PATH,
    generate,
    regime_label,
    render,
)
from goldtrader.evals.harness import HEADLINE_FIELDS, SETS_DIR, load_set

HEADLINE_ENUMS = {
    "relevance": ("none", "low", "medium", "high"),
    "event_type": (
        "fed",
        "inflation",
        "employment",
        "growth",
        "usd",
        "geopolitical",
        "flows",
        "other",
    ),
    "direction_for_gold": ("bullish", "bearish", "neutral", "unclear"),
    "magnitude": ("low", "medium", "high"),
    "horizon": ("minutes", "hours", "days"),
}
OUTCOME_CLASSES = (
    "signal_wrong",
    "timing",
    "stopped_by_noise",
    "news_shock",
    "execution",
    "correct_loss",
    "correct_win",
    "lucky_win",
)


def test_readme_marks_the_sets_as_synthetic_seeds():
    text = (SETS_DIR / "README.md").read_text(encoding="utf-8")
    assert "2026-11-05" in text and "SEED" in text.upper()
    assert "synthetic" in text and "D-040" in text and "none of" in text.lower()


def test_headlines_labels_validate_against_news_event_and_cover_every_enum():
    cases = load_set("headlines")
    assert len(cases) == 60
    assert len({c.id for c in cases}) == 60
    counts: dict[str, Counter] = {f: Counter() for f in HEADLINE_FIELDS}
    for c in cases:
        NewsEvent(item_id=c.id, confidence=1.0, summary="", **c.label)  # raises on a bad enum
        for f in HEADLINE_FIELDS:
            counts[f][c.label[f]] += 1
        dt = datetime.fromisoformat(c.inputs["published_at"].replace("Z", "+00:00"))
        assert dt.year == 2026
        assert c.inputs["title"] and c.inputs["body"] and c.inputs["source"].startswith("seed-")
        if c.label["relevance"] == "high":
            assert c.label["event_type"] != "other"  # the code clip would demote it
    for f, values in HEADLINE_ENUMS.items():
        for v in values:
            assert counts[f][v] >= 3, (f, v, counts[f])
    assert counts["relevance"]["none"] == 10
    assert counts["direction_for_gold"]["unclear"] >= 6


def test_headline_pairs_report_the_same_release_with_opposite_direction():
    cases = load_set("headlines")
    pairs: dict[str, list] = {}
    for c in cases:
        if c.pair_id:
            pairs.setdefault(c.pair_id, []).append(c)
    assert len(pairs) == 8
    for pid, members in pairs.items():
        assert len(members) == 2, pid
        a, b = members
        assert {a.label["direction_for_gold"], b.label["direction_for_gold"]} == {
            "bullish",
            "bearish",
        }
        assert a.label["event_type"] == b.label["event_type"]
        assert a.inputs["published_at"] == b.inputs["published_at"]
        assert a.label["relevance"] == b.label["relevance"] != "none"


def test_regimes_generator_reproduces_the_committed_file_byte_for_byte():
    committed = OUT_PATH.read_bytes()
    assert committed == render().encode("utf-8")
    assert OUT_PATH == SETS_DIR / "regimes_v1.jsonl"


def test_regimes_labels_follow_the_feature_thresholds():
    cases = load_set("regimes")
    assert len(cases) == 40 == len(ATR_PCT_DAILY_VALUES)
    counts = Counter(c.label["regime"] for c in cases)
    assert set(counts) == {"calm", "normal", "stress"} and min(counts.values()) >= 10
    for c in cases:
        f = c.inputs["features"]
        assert set(f) == {"atr_pct_daily", "rv_pctile", "trend_state", "spread_regime", "hour_utc"}
        assert c.label["regime"] == regime_label(f["atr_pct_daily"])
    # boundaries: 0.012 and 0.025 are normal; just under / over flip
    assert regime_label(0.012) == "normal" and regime_label(0.025) == "normal"
    assert regime_label(0.0119) == "calm" and regime_label(0.0251) == "stress"
    assert generate() == generate()  # deterministic


def test_reviews_cover_every_outcome_class_at_least_twice():
    cases = load_set("reviews")
    assert len(cases) == 20
    counts = Counter(c.label["outcome_class"] for c in cases)
    for oc in OUTCOME_CLASSES:
        assert counts[oc] >= 2, (oc, counts)
    assert "unreviewed" not in counts  # the code-only fallback class
    for c in cases:
        attr = c.inputs["attribution"]
        assert set(attr) == {"signal_pnl", "timing_pnl", "slippage", "costs"}
        PostMortem(outcome_class=c.label["outcome_class"], confidence=1.0, **attr)
        assert c.inputs["trade"]["trade_id"] == c.id
        assert c.inputs["rubric"] and all(isinstance(r, str) for r in c.inputs["rubric"])

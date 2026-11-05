"""Eval harness runners (D-040; LOCAL_LLM §7): perfect / wrong / failing FakeProvider scripts."""

from __future__ import annotations

from goldtrader.contracts import NewsExtraction, PostMortem
from goldtrader.evals.harness import (
    HEADLINE_FIELDS,
    EvalCase,
    EvalSummary,
    load_set,
    load_summary,
    macro_f1,
    memory_store,
    published_ms,
    run_headlines,
    run_regimes,
    run_reviews,
    save_summary,
)
from goldtrader.evals.specs import RegimeLabel, regime_spec, regime_table
from goldtrader.llm.fake import FakeProvider
from goldtrader.llm.provider import ProviderError
from tests.test_bots_helpers import cfg

HOUR = 3_600_000


def _perfect_headline(case: EvalCase, i: int) -> dict:
    if case.label["relevance"] == "none" and i % 2 == 0:
        return {"events": [], "dropped": [f"{case.id}: not about markets"]}
    return {
        "events": [{"item_id": case.id, "confidence": 0.9, "summary": "seed", **case.label}],
        "dropped": [],
    }


WRONG = {
    "relevance": "low",
    "event_type": "other",
    "direction_for_gold": "neutral",
    "magnitude": "low",
    "horizon": "days",
}


def _wrong_headline(case: EvalCase) -> dict:
    return {
        "events": [{"item_id": case.id, "confidence": 0.5, "summary": "seed", **WRONG}],
        "dropped": [],
    }


def test_headlines_perfect_agent_scores_one_everywhere():
    cases = load_set("headlines")
    fake = FakeProvider()
    fake.script("fast", [_perfect_headline(c, i) for i, c in enumerate(cases)])
    store = memory_store()
    s = run_headlines(fake, cfg(), cases, store=store)
    assert s.set_name == "headlines" and s.n == 60
    assert s.model_id == "claude-haiku-5-5" and len(s.prompt_hash) == 16
    assert s.schema_valid_rate == 1.0 and s.exact_match_rate == 1.0
    assert set(s.field_agreement) == set(HEADLINE_FIELDS)
    assert all(v == 1.0 for v in s.field_agreement.values())
    assert all(v == 1.0 for v in s.macro_f1.values())
    assert s.perturbation_flip_rate == 1.0
    assert s.total_cost_usd > 0 and s.p95_ms >= s.p50_ms >= 0
    assert fake.pending("fast") == 0  # one call per item: the id accounting ran 60 times
    assert store.c.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 60
    assert all(len(r.messages) == 1 for r in fake.requests)
    assert all(
        f'<untrusted_news id="{c.id}"' in str(r.messages) for r, c in zip(fake.requests, cases)
    )


def test_headlines_wrong_agent_drops_agreement_and_flip_rate():
    cases = load_set("headlines")
    fake = FakeProvider()
    fake.script("fast", [_wrong_headline(c) for c in cases])
    s = run_headlines(fake, cfg(), cases)
    assert s.schema_valid_rate == 1.0  # valid JSON, just wrong
    assert s.perturbation_flip_rate == 0.0
    assert s.field_agreement["direction_for_gold"] < 0.5
    assert s.field_agreement["relevance"] < 0.5 and s.exact_match_rate < 0.1
    assert s.macro_f1["direction_for_gold"] < s.field_agreement["direction_for_gold"] + 1e-9
    # relevance none items answered as events with relevance low are wrong on every field
    none_ids = [c.id for c in cases if c.label["relevance"] == "none"]
    assert len(none_ids) == 10 and s.field_agreement["event_type"] <= (60 - 10) / 60 + 1e-9


def test_headlines_failed_runs_count_as_schema_invalid_and_limit_truncates():
    cases = load_set("headlines")
    fake = FakeProvider()
    fake.script("fast", [ProviderError("boom"), _perfect_headline(cases[1], 1)])
    s = run_headlines(fake, cfg(), cases, limit=2)
    assert s.n == 2 and s.schema_valid_rate == 0.5
    assert s.field_agreement["relevance"] == 0.5 and s.exact_match_rate == 0.5
    assert s.perturbation_flip_rate == 0.0  # h001/h002 are pair p1; a failed member is no flip
    fake.script("fast", [_perfect_headline(cases[0], 0)])
    assert run_headlines(fake, cfg(), cases, limit=1).perturbation_flip_rate is None
    # a fixed `now` far in the future demotes `high` to `medium` (the real code clip runs)
    high = next(c for c in cases if c.label["relevance"] == "high")
    fake.script("fast", [_perfect_headline(high, 1)])
    s2 = run_headlines(
        fake, cfg(), [high], now_ms=published_ms(high.inputs["published_at"]) + 48 * HOUR
    )
    assert s2.field_agreement["relevance"] == 0.0 and s2.field_agreement["event_type"] == 1.0


def test_headlines_dropping_a_relevant_item_is_wrong_everywhere():
    cases = [c for c in load_set("headlines") if c.label["relevance"] == "high"][:1]
    fake = FakeProvider()
    fake.script("fast", [{"events": [], "dropped": [f"{cases[0].id}: dup"]}])
    s = run_headlines(fake, cfg(), cases)
    assert s.schema_valid_rate == 1.0 and all(v == 0.0 for v in s.field_agreement.values())


def test_regimes_perfect_and_wrong():
    cases = load_set("regimes")
    fake = FakeProvider()
    fake.script(
        "fast", [{"regime": c.label["regime"], "reason": "atr vs threshold"} for c in cases]
    )
    s = run_regimes(fake, cfg(), cases)
    assert s.set_name == "regimes" and s.n == 40
    assert s.field_agreement == {"regime": 1.0} and s.macro_f1 == {"regime": 1.0}
    assert s.schema_valid_rate == 1.0 and s.exact_match_rate == 1.0
    table = str(fake.requests[0].messages[0]["content"])
    assert "atr_pct_daily" in table and "0.0040" in table
    assert "1.2%" in fake.requests[0].system and "2.5%" in fake.requests[0].system

    fake.script("fast", [{"regime": "normal", "reason": "always normal"} for _ in cases])
    w = run_regimes(fake, cfg(), cases)
    assert abs(w.field_agreement["regime"] - 14 / 40) < 1e-9
    assert w.macro_f1["regime"] < w.field_agreement["regime"]  # one class only → poor macro-F1
    assert w.exact_match_rate == w.field_agreement["regime"]

    fake.script("fast", ["not json"] * 3)  # invalid output, resubmits exhausted, no fallback
    bad = run_regimes(fake, cfg(), cases, limit=1)
    assert bad.schema_valid_rate == 0.0 and bad.field_agreement["regime"] == 0.0


def test_regime_spec_shape():
    spec = regime_spec(cfg())
    assert spec.output_model is RegimeLabel and spec.tools == [] and spec.fallback is None
    assert spec.tier == "fast" and spec.system_prompt.name == "evals.regime.system"
    assert regime_table({"atr_pct_daily": 0.0123}).splitlines()[0].endswith("0.0123")
    assert "hour_utc" in regime_table({})


def _perfect_review(case: EvalCase) -> dict:
    return {
        "outcome_class": case.label["outcome_class"],
        **case.inputs["attribution"],
        "lessons": ["observe the stop distance in stress"],
        "confidence": 0.8,
    }


def test_reviews_perfect_wrong_class_and_wrong_numbers():
    cases = load_set("reviews")
    fake = FakeProvider()
    fake.script("standard", [_perfect_review(c) for c in cases])
    s = run_reviews(fake, cfg(), cases)
    assert s.set_name == "reviews" and s.n == 20 and s.model_id == "claude-sonnet-5-5"
    assert s.field_agreement == {"outcome_class": 1.0, "attribution_copied": 1.0}
    assert s.macro_f1 == {"outcome_class": 1.0}
    assert s.schema_valid_rate == 1.0 and s.exact_match_rate == 1.0
    assert s.perturbation_flip_rate is None
    assert "copy exactly" in str(fake.requests[0].messages[0]["content"])

    fake.script("standard", [{**_perfect_review(c), "outcome_class": "correct_win"} for c in cases])
    w = run_reviews(fake, cfg(), cases)
    assert abs(w.field_agreement["outcome_class"] - 3 / 20) < 1e-9
    assert w.field_agreement["attribution_copied"] == 1.0 and w.schema_valid_rate == 1.0
    assert w.exact_match_rate == w.field_agreement["outcome_class"]

    # wrong numbers: the real check() rejects, resubmits run out, fallback → degraded
    bad = {
        **_perfect_review(cases[0]),
        "signal_pnl": cases[0].inputs["attribution"]["signal_pnl"] + 1,
    }
    fake.script("standard", [bad, bad, bad])
    d = run_reviews(fake, cfg(), cases, limit=1)
    assert d.schema_valid_rate == 0.0
    assert d.field_agreement == {"outcome_class": 0.0, "attribution_copied": 0.0}


def test_macro_f1_and_published_ms():
    assert macro_f1([("a", "a"), ("b", "b")]) == 1.0
    assert macro_f1([("a", "b"), ("b", "a")]) == 0.0
    assert abs(macro_f1([("a", "a"), ("b", "a")]) - (2 / 3) / 2) < 1e-9  # class b has F1 0
    assert macro_f1([("a", None)]) == 0.0 and macro_f1([]) == 0.0
    assert published_ms("2026-07-14T12:30:00Z") == 1_784_032_200_000
    assert published_ms("2026-07-14T12:30:00+00:00") == published_ms("2026-07-14T12:30:00")
    assert published_ms(None) is None and published_ms("nope") is None and published_ms(5) == 5


def test_summary_round_trips_through_json(tmp_path):
    s = EvalSummary(
        set_name="headlines",
        model_id="m",
        prompt_hash="h" * 16,
        n=3,
        schema_valid_rate=1.0,
        field_agreement={"relevance": 0.9},
        macro_f1={"relevance": 0.8},
        exact_match_rate=0.7,
        p50_ms=10.5,
        p95_ms=20.0,
        total_cost_usd=0.001,
        perturbation_flip_rate=0.5,
        run_at="2026-11-05T00:00:00+00:00",
    )
    path = save_summary(s, tmp_path / "sub" / "s.json")
    assert path.exists() and load_summary(path) == s
    assert load_summary(path).model_dump() == s.model_dump()


def test_contracts_outputs_used_by_runners_are_the_real_ones():
    # the harness scores the production output models, not copies
    assert NewsExtraction.model_fields["events"] and PostMortem.model_fields["outcome_class"]

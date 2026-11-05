"""LOCAL_LLM §7 promotion gate (`compare`) and the CLI functions (`run_set`, `gate`)."""

from __future__ import annotations

from goldtrader.evals.cli import gate, run_set, summary_filename
from goldtrader.evals.harness import (
    EvalSummary,
    compare,
    load_set,
    load_summary,
    memory_store,
    save_summary,
)
from goldtrader.llm.fake import FakeProvider
from tests.test_bots_helpers import cfg


def _summary(**over) -> EvalSummary:
    base = dict(
        set_name="headlines",
        model_id="claude-haiku-5-5",
        prompt_hash="a" * 16,
        n=60,
        schema_valid_rate=1.0,
        field_agreement={"relevance": 0.90, "direction_for_gold": 0.80},
        macro_f1={"relevance": 0.85, "direction_for_gold": 0.75},
        exact_match_rate=0.70,
        p50_ms=1000.0,
        p95_ms=2000.0,
        total_cost_usd=0.5,
        perturbation_flip_rate=0.9,
        run_at="2026-11-05T00:00:00+00:00",
    )
    base.update(over)
    return EvalSummary(**base)


def test_compare_passes_inside_the_thresholds():
    base = _summary()
    cand = _summary(
        field_agreement={"relevance": 0.88, "direction_for_gold": 0.80},  # exactly 2 points
        exact_match_rate=0.68,
        p50_ms=1250.0,  # exactly +25%
        p95_ms=2400.0,
        perturbation_flip_rate=0.88,
        schema_valid_rate=0.98,
    )
    d = compare(base, cand)
    assert d.ok and d.reasons == []
    better = _summary(field_agreement={"relevance": 0.99, "direction_for_gold": 0.95}, p50_ms=10.0)
    assert compare(base, better).ok


def test_compare_fails_on_each_metric_just_past_the_threshold():
    base = _summary()
    d = compare(base, _summary(field_agreement={"relevance": 0.879, "direction_for_gold": 0.80}))
    assert not d.ok and any("field_agreement[relevance]" in r for r in d.reasons)
    d = compare(base, _summary(field_agreement={"relevance": 0.90}))
    assert not d.ok and any("direction_for_gold] missing" in r for r in d.reasons)
    d = compare(base, _summary(exact_match_rate=0.679))
    assert not d.ok and any(r.startswith("exact_match_rate") for r in d.reasons)
    d = compare(base, _summary(schema_valid_rate=0.979))
    assert not d.ok and any(r.startswith("schema_valid_rate") for r in d.reasons)
    d = compare(base, _summary(perturbation_flip_rate=0.87))
    assert not d.ok and any(r.startswith("perturbation_flip_rate") for r in d.reasons)
    d = compare(base, _summary(perturbation_flip_rate=None))
    assert not d.ok and any("perturbation_flip_rate missing" in r for r in d.reasons)
    d = compare(base, _summary(p50_ms=1251.0))
    assert not d.ok and any(r.startswith("p50_ms") for r in d.reasons)
    d = compare(base, _summary(p95_ms=2501.0))
    assert not d.ok and any(r.startswith("p95_ms") for r in d.reasons)
    d = compare(base, _summary(set_name="regimes"))
    assert not d.ok and any("set mismatch" in r for r in d.reasons)
    d = compare(base, _summary(n=0))
    assert not d.ok and any("no cases" in r for r in d.reasons)


def test_compare_thresholds_are_parameters():
    base = _summary()
    cand = _summary(field_agreement={"relevance": 0.85, "direction_for_gold": 0.80}, p50_ms=1400.0)
    assert not compare(base, cand).ok
    assert compare(base, cand, max_agreement_drop=0.05, max_latency_increase=0.5).ok
    # a baseline without a flip rate (regimes / reviews) does not demand one
    no_flip = _summary(set_name="regimes", perturbation_flip_rate=None)
    assert compare(no_flip, _summary(set_name="regimes", perturbation_flip_rate=None)).ok


def test_run_set_writes_a_summary_file_and_gate_reads_two(tmp_path):
    cases = load_set("regimes")
    fake = FakeProvider()
    fake.script("fast", [{"regime": c.label["regime"], "reason": "ok"} for c in cases])
    base, base_path = run_set("regimes", fake, cfg(), memory_store(), tmp_path / "out", limit=None)
    assert base_path.parent == tmp_path / "out" and base_path.name == summary_filename(base)
    assert base_path.name.startswith("regimes__claude-haiku-5-5__") and base_path.suffix == ".json"
    assert base.n == 40 and base.field_agreement["regime"] == 1.0

    fake.script("fast", [{"regime": "stress", "reason": "always stress"} for _ in cases])
    cand, cand_path = run_set("regimes", fake, cfg(), None, tmp_path / "out", limit=10)
    assert cand.n == 10 and cand_path != base_path and cand_path.parent == base_path.parent
    assert load_summary(base_path) == base and load_summary(cand_path) == cand  # no overwrite
    decision = gate(base_path, cand_path)
    assert not decision.ok and any("field_agreement[regime]" in r for r in decision.reasons)
    # latency noise from the fake provider must not be what fails the gate here
    same = save_summary(base, tmp_path / "same.json")
    assert gate(base_path, same).ok


def test_run_set_rejects_unknown_set(tmp_path):
    try:
        run_set("nope", FakeProvider(), cfg(), None, tmp_path)
    except KeyError as e:
        assert "unknown eval set" in str(e)
    else:
        raise AssertionError("expected KeyError")

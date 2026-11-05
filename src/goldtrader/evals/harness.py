"""Eval harness: run a labelled set through a bot spec and score it (D-040; LOCAL_LLM.md §7).

    cases = load_set("headlines")
    summary = run_headlines(provider, cfg, cases, store=store)
    decision = compare(load_summary(baseline), summary)       # LOCAL_LLM §7 promotion gate

Metrics (LOCAL_LLM §7): schema validity, per-field enum agreement and macro-F1, exact match,
p50 / p95 latency, cost, and for the headline set the perturbation flip rate of research/04 §2.1
(the two members of a `pair_id` report the same release with the opposite surprise sign; the
model's `direction_for_gold` must flip). Prompt changes are evaluated here on extraction accuracy,
never on P&L (LEARNING_SYSTEM §5); LLM-as-judge is not used anywhere in this module.

The headline and review runners execute the *real* `AgentSpec`s (`bots.research.research_spec`,
`bots.review.review_spec`) one item per call, so the id-accounting check, the code clips and the
attribution-copy check are exercised exactly as in production. A run that ends degraded or
failed counts as schema-invalid with no agreement.

Look-ahead (research/04 §2.1): the seed sets are synthetic; a summary is only evidence for
promotion when the set holds real items published after the model's training cutoff (D-040).
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from goldtrader.bots.common import BotContext
from goldtrader.bots.research import research_spec
from goldtrader.bots.review import ATTRIBUTION_KEYS, ATTRIBUTION_TOL, review_spec
from goldtrader.contracts import NewsExtraction, PostMortem
from goldtrader.db import connect, migrate
from goldtrader.evals.specs import RegimeLabel, regime_spec
from goldtrader.llm.provider import ModelProvider
from goldtrader.llm.runtime import AgentRun, AgentSpec, run_agent
from goldtrader.llm.store import LLMStore

SETS_DIR = Path(__file__).resolve().parent / "sets"

SETS: dict[str, tuple[str, str | None]] = {
    "headlines": ("headlines_v1.jsonl", None),
    "regimes": ("regimes_v1.jsonl", "regime"),
    "reviews": ("reviews_v1.jsonl", "outcome_class"),
}
"""set name → (file, field name a scalar `label` is wrapped into)."""

HEADLINE_FIELDS: tuple[str, ...] = (
    "relevance",
    "event_type",
    "direction_for_gold",
    "magnitude",
    "horizon",
)
DEFAULT_FRESHNESS_MS = 30 * 60 * 1000
"""Headline runs default `now` to 30 min after each item so the 24 h `high` demotion is not hit."""


# --- data ---------------------------------------------------------------------------------------
@dataclass(frozen=True)
class EvalCase:
    """One labelled item: `inputs` is what the bot sees, `label` the expected enum values."""

    id: str
    inputs: dict[str, Any]
    label: dict[str, Any]
    pair_id: str | None = None


@dataclass
class EvalResult:
    """Score of one case. `agreement` has one bool per compared field."""

    case_id: str
    ok_schema: bool
    agreement: dict[str, bool] = field(default_factory=dict)
    exact_match: bool = False
    latency_ms: int = 0
    cost_usd: float = 0.0
    output: dict[str, Any] | None = None
    error: str | None = None


class EvalSummary(BaseModel):
    """Aggregate of one set run (LOCAL_LLM §7 metrics). JSON via `save_summary` / `load_summary`."""

    model_config = ConfigDict(extra="forbid")

    set_name: str
    model_id: str
    prompt_hash: str
    n: int = Field(ge=0)
    schema_valid_rate: float = Field(ge=0.0, le=1.0)
    field_agreement: dict[str, float]
    macro_f1: dict[str, float]
    exact_match_rate: float = Field(ge=0.0, le=1.0)
    p50_ms: float = Field(ge=0.0)
    p95_ms: float = Field(ge=0.0)
    total_cost_usd: float = Field(ge=0.0)
    perturbation_flip_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    run_at: str


class GateDecision(BaseModel):
    """`compare()` verdict: `ok` and the reasons it failed (empty when it passed)."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    reasons: list[str] = Field(default_factory=list)


# --- loading ------------------------------------------------------------------------------------
def load_cases(path: str | Path, *, scalar_label_field: str | None = None) -> list[EvalCase]:
    """Read a JSONL set. Every key except `id`, `label` and `pair_id` becomes an input; a scalar
    `label` is wrapped as `{scalar_label_field: label}`."""
    cases: list[EvalCase] = []
    with Path(path).open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            label = row.get("label")
            if not isinstance(label, dict):
                if scalar_label_field is None:
                    raise ValueError(f"{path}:{line_no}: scalar label needs scalar_label_field")
                label = {scalar_label_field: label}
            inputs = {k: v for k, v in row.items() if k not in ("id", "label", "pair_id")}
            cases.append(
                EvalCase(
                    id=str(row["id"]), inputs=inputs, label=dict(label), pair_id=row.get("pair_id")
                )
            )
    return cases


def set_path(name: str) -> Path:
    try:
        file, _ = SETS[name]
    except KeyError as e:
        raise KeyError(f"unknown eval set {name!r}; known: {sorted(SETS)}") from e
    return SETS_DIR / file


def load_set(name: str) -> list[EvalCase]:
    """The committed set `name` (`headlines` | `regimes` | `reviews`)."""
    _, scalar = SETS[name] if name in SETS else (None, None)
    return load_cases(set_path(name), scalar_label_field=scalar)


def memory_store() -> LLMStore:
    """An in-memory `LLMStore` for runs whose ledger nobody keeps."""
    conn = connect(None)
    migrate(conn)
    return LLMStore(conn)


# --- runners ------------------------------------------------------------------------------------
def published_ms(published_at: Any) -> int | None:
    """ISO-8601 (`Z` or offset) → epoch ms; None when unreadable."""
    if published_at is None:
        return None
    if isinstance(published_at, int | float):
        return int(published_at)
    try:
        dt = datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def run_headlines(
    provider: ModelProvider,
    cfg: Any,
    cases: Sequence[EvalCase],
    *,
    store: LLMStore | None = None,
    now_ms: int | None = None,
    limit: int | None = None,
) -> EvalSummary:
    """Run the real Research extraction spec one item per call and score the five enum fields.

    An item the model puts in `dropped` is scored as relevance `none`: correct on every field when
    the label is `none`, wrong on every field otherwise. `now_ms=None` makes each item 30 min old
    (`DEFAULT_FRESHNESS_MS`); pass a fixed `now_ms` to exercise the 24 h `high` demotion.
    Perturbation (research/04 §2.1): for every complete `pair_id` the two predicted
    `direction_for_gold` values must be `bullish` and `bearish`, one each.
    """
    store = store or memory_store()
    spec = research_spec(cfg)
    results: list[EvalResult] = []
    directions: dict[str, dict[str, str | None]] = {}
    for case in _take(cases, limit):
        item = _news_item(case)
        now = now_ms if now_ms is not None else (item["published_ms"] or 0) + DEFAULT_FRESHNESS_MS
        ctx = BotContext(now_ms=now, cfg=cfg, news_items=[item])
        run = run_agent(spec, ctx, provider, store, now_ms=now)
        result, predicted = _score_headline(case, run)
        results.append(result)
        if case.pair_id:
            directions.setdefault(case.pair_id, {})[case.id] = predicted.get("direction_for_gold")
    flip = _flip_rate(directions)
    return summarise(
        "headlines", spec, provider, results, HEADLINE_FIELDS, perturbation_flip_rate=flip
    )


def run_regimes(
    provider: ModelProvider,
    cfg: Any,
    cases: Sequence[EvalCase],
    *,
    store: LLMStore | None = None,
    now_ms: int | None = None,
    limit: int | None = None,
) -> EvalSummary:
    """Run `evals.specs.regime_spec` on the feature tables and score `regime` against the label."""
    store = store or memory_store()
    spec = regime_spec(cfg)
    results: list[EvalResult] = []
    for case in _take(cases, limit):
        now = now_ms if now_ms is not None else _now_ms()
        ctx = {"features": case.inputs.get("features", {})}
        run = run_agent(spec, ctx, provider, store, now_ms=now)
        out = run.output if run.status == "ok" and isinstance(run.output, RegimeLabel) else None
        predicted = {"regime": out.regime} if out else {}
        results.append(_result(case, run, out, predicted, ("regime",)))
    return summarise("regimes", spec, provider, results, ("regime",))


def run_reviews(
    provider: ModelProvider,
    cfg: Any,
    cases: Sequence[EvalCase],
    *,
    store: LLMStore | None = None,
    now_ms: int | None = None,
    limit: int | None = None,
) -> EvalSummary:
    """Run the real Review post-mortem spec; score `outcome_class` and `attribution_copied` (the
    four code-computed numbers copied within `ATTRIBUTION_TOL`). The `rubric` strings travel with
    the case for human grading only (LEARNING_SYSTEM §5: LLM-as-judge is triage, never a gate)."""
    store = store or memory_store()
    spec = review_spec(cfg)
    results: list[EvalResult] = []
    for case in _take(cases, limit):
        attribution = {k: float(v) for k, v in dict(case.inputs.get("attribution", {})).items()}
        ctx = BotContext(
            now_ms=now_ms if now_ms is not None else _now_ms(),
            cfg=cfg,
            trade=dict(case.inputs.get("trade", {})),
            attribution=attribution,
        )
        run = run_agent(spec, ctx, provider, store, now_ms=ctx.now_ms)
        pm = run.finalized if isinstance(run.finalized, PostMortem) else run.output
        ok = run.status == "ok" and isinstance(pm, PostMortem)
        predicted: dict[str, Any] = {}
        if ok:
            copied = all(
                abs(float(getattr(pm, k)) - attribution.get(k, 0.0)) <= ATTRIBUTION_TOL
                for k in ATTRIBUTION_KEYS
            )
            predicted = {"outcome_class": pm.outcome_class, "attribution_copied": copied}
        result = _result(case, run, pm if ok else None, predicted, ("outcome_class",))
        result.agreement["attribution_copied"] = bool(predicted.get("attribution_copied", False))
        result.exact_match = all(result.agreement.values()) and ok
        results.append(result)
    return summarise("reviews", spec, provider, results, ("outcome_class",))


# --- scoring ------------------------------------------------------------------------------------
def summarise(
    set_name: str,
    spec: AgentSpec,
    provider: ModelProvider,
    results: Sequence[EvalResult],
    fields: Sequence[str],
    *,
    perturbation_flip_rate: float | None = None,
) -> EvalSummary:
    """Aggregate results into an `EvalSummary`. Macro-F1 is computed from each result's
    `output["_predicted"]` / `output["_label"]` (filled by the runners)."""
    n = len(results)
    agreement = {f: _rate([r.agreement.get(f, False) for r in results]) for f in fields}
    extra = {k for r in results for k in r.agreement} - set(fields)
    for f in sorted(extra):
        agreement[f] = _rate([r.agreement.get(f, False) for r in results])
    f1 = {f: _macro_f1(results, f) for f in fields}
    latencies = [float(r.latency_ms) for r in results]
    return EvalSummary(
        set_name=set_name,
        model_id=provider.model_for_tier(spec.tier),
        prompt_hash=spec.system_prompt.hash,
        n=n,
        schema_valid_rate=_rate([r.ok_schema for r in results]),
        field_agreement=agreement,
        macro_f1=f1,
        exact_match_rate=_rate([r.exact_match for r in results]),
        p50_ms=_percentile(latencies, 0.50),
        p95_ms=_percentile(latencies, 0.95),
        total_cost_usd=round(sum(r.cost_usd for r in results), 8),
        perturbation_flip_rate=perturbation_flip_rate,
        run_at=datetime.now(tz=UTC).isoformat(timespec="seconds"),
    )


def macro_f1(pairs: Sequence[tuple[Any, Any]]) -> float:
    """Unweighted mean F1 over every class seen in the labels or the predictions. A prediction of
    `None` (no output) counts as a miss for the label's class and never as its own class."""
    classes = sorted({str(y) for y, _ in pairs} | {str(p) for _, p in pairs if p is not None})
    if not classes:
        return 0.0
    scores: list[float] = []
    for c in classes:
        tp = sum(1 for y, p in pairs if str(y) == c and p is not None and str(p) == c)
        fp = sum(1 for y, p in pairs if p is not None and str(p) == c and str(y) != c)
        fn = sum(1 for y, p in pairs if str(y) == c and (p is None or str(p) != c))
        denom = 2 * tp + fp + fn
        scores.append(2 * tp / denom if denom else 0.0)
    return sum(scores) / len(scores)


def compare(
    baseline: EvalSummary,
    candidate: EvalSummary,
    *,
    max_agreement_drop: float = 0.02,
    max_latency_increase: float = 0.25,
) -> GateDecision:
    """LOCAL_LLM §7 promotion gate: the candidate must not regress any agreement metric (per-field
    agreement, exact match, schema validity, perturbation flip rate) by more than
    `max_agreement_drop` (2 points) nor p50 / p95 latency by more than `max_latency_increase`
    (25%). Both summaries must be of the same set."""
    reasons: list[str] = []
    if baseline.set_name != candidate.set_name:
        reasons.append(
            f"set mismatch: baseline {baseline.set_name!r}, candidate {candidate.set_name!r}"
        )
    if candidate.n == 0:
        reasons.append("candidate has no cases")
    for name, want in baseline.field_agreement.items():
        got = candidate.field_agreement.get(name)
        if got is None:
            reasons.append(f"field_agreement[{name}] missing from candidate")
        elif want - got > max_agreement_drop + 1e-12:
            reasons.append(_drop("field_agreement[" + name + "]", want, got, max_agreement_drop))
    for name, want, got in (
        ("exact_match_rate", baseline.exact_match_rate, candidate.exact_match_rate),
        ("schema_valid_rate", baseline.schema_valid_rate, candidate.schema_valid_rate),
    ):
        if want - got > max_agreement_drop + 1e-12:
            reasons.append(_drop(name, want, got, max_agreement_drop))
    if baseline.perturbation_flip_rate is not None:
        got_flip = candidate.perturbation_flip_rate
        if got_flip is None:
            reasons.append("perturbation_flip_rate missing from candidate")
        elif baseline.perturbation_flip_rate - got_flip > max_agreement_drop + 1e-12:
            want_flip = baseline.perturbation_flip_rate
            reasons.append(_drop("perturbation_flip_rate", want_flip, got_flip, max_agreement_drop))
    for name, want, got in (
        ("p50_ms", baseline.p50_ms, candidate.p50_ms),
        ("p95_ms", baseline.p95_ms, candidate.p95_ms),
    ):
        if got > want * (1.0 + max_latency_increase) + 1e-9:
            reasons.append(
                f"{name} {got:.0f} ms is more than {max_latency_increase:.0%} above baseline "
                f"{want:.0f} ms"
            )
    return GateDecision(ok=not reasons, reasons=reasons)


def save_summary(summary: EvalSummary, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(summary.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return p


def load_summary(path: str | Path) -> EvalSummary:
    return EvalSummary.model_validate_json(Path(path).read_text(encoding="utf-8"))


# --- internals ----------------------------------------------------------------------------------
def _take(cases: Sequence[EvalCase], limit: int | None) -> Sequence[EvalCase]:
    return cases if limit is None else cases[: max(int(limit), 0)]


def _now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


def _news_item(case: EvalCase) -> dict[str, Any]:
    inp = case.inputs
    return {
        "id": case.id,
        "source": inp.get("source", ""),
        "title": inp.get("title", ""),
        "body": inp.get("body", ""),
        "published_at": inp.get("published_at"),
        "published_ms": published_ms(inp.get("published_at")),
    }


def _score_headline(case: EvalCase, run: AgentRun) -> tuple[EvalResult, dict[str, Any]]:
    out = run.finalized if isinstance(run.finalized, NewsExtraction) else run.output
    ok = run.status == "ok" and isinstance(out, NewsExtraction)
    predicted: dict[str, Any] = {}
    if ok:
        event = next((e for e in out.events if e.item_id == case.id), None)
        if event is not None:
            predicted = {f: getattr(event, f) for f in HEADLINE_FIELDS}
        elif case.label.get("relevance") == "none":
            predicted = dict(case.label)  # dropping an irrelevant item is the right call
        else:
            predicted = {"relevance": "none"}  # dropped a relevant item: wrong everywhere else
    return _result(case, run, out if ok else None, predicted, HEADLINE_FIELDS), predicted


def _result(
    case: EvalCase,
    run: AgentRun,
    output: BaseModel | None,
    predicted: dict[str, Any],
    fields: Sequence[str],
) -> EvalResult:
    ok = output is not None
    agreement = {f: ok and predicted.get(f) == case.label.get(f) for f in fields}
    dumped = output.model_dump(mode="json") if output is not None else None
    return EvalResult(
        case_id=case.id,
        ok_schema=ok,
        agreement=agreement,
        exact_match=ok and all(agreement.values()),
        latency_ms=run.latency_ms,
        cost_usd=run.cost_usd,
        output={"_predicted": predicted, "_label": dict(case.label), "model": dumped},
        error=run.failure_reason if run.status != "ok" else None,
    )


def _macro_f1(results: Sequence[EvalResult], fld: str) -> float:
    pairs: list[tuple[Any, Any]] = []
    for r in results:
        out = r.output or {}
        label = (out.get("_label") or {}).get(fld)
        pred = (out.get("_predicted") or {}).get(fld) if r.ok_schema else None
        pairs.append((label, pred))
    return macro_f1(pairs)


def _flip_rate(directions: dict[str, dict[str, str | None]]) -> float | None:
    pairs = [d for d in directions.values() if len(d) == 2]
    if not pairs:
        return None
    flips = sum(1 for d in pairs if set(d.values()) == {"bullish", "bearish"})
    return flips / len(pairs)


def _rate(flags: Sequence[bool]) -> float:
    return sum(1 for f in flags if f) / len(flags) if flags else 0.0


def _percentile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    qs = statistics.quantiles(values, n=100, method="inclusive")
    idx = min(max(int(round(q * 100)) - 1, 0), len(qs) - 1)
    return float(qs[idx])


def _drop(name: str, want: float, got: float, limit: float) -> str:
    return (
        f"{name} dropped from {want:.3f} to {got:.3f} "
        f"({(want - got) * 100:.1f} points > {limit * 100:.0f} allowed)"
    )

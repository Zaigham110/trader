"""Eval commands for `goldctl` (D-040; LOCAL_LLM.md §7). Plain functions, no typer app: the main
CLI imports `run_set` and `gate` and wires the provider, config and store itself.

    summary, path = run_set("headlines", provider, cfg, store, out_dir, limit=None)
    decision = gate(baseline_path, candidate_path)          # LOCAL_LLM §7: ≤ 2 pts, ≤ 25% latency
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from goldtrader.evals.harness import (
    SETS,
    EvalCase,
    EvalSummary,
    GateDecision,
    compare,
    load_set,
    load_summary,
    run_headlines,
    run_regimes,
    run_reviews,
    save_summary,
)
from goldtrader.llm.provider import ModelProvider
from goldtrader.llm.store import LLMStore

Runner = Callable[..., EvalSummary]

RUNNERS: dict[str, Runner] = {
    "headlines": run_headlines,
    "regimes": run_regimes,
    "reviews": run_reviews,
}

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def summary_filename(summary: EvalSummary) -> str:
    """`<set>__<model>__<prompt_hash>__<run_at>.json`, every part made filesystem-safe."""
    parts = (summary.set_name, summary.model_id, summary.prompt_hash, summary.run_at)
    return "__".join(_UNSAFE.sub("-", p) for p in parts) + ".json"


def unique_path(path: Path) -> Path:
    """`path`, or `<stem>-2.json`, `<stem>-3.json`, ... when a run in the same second exists."""
    if not path.exists():
        return path
    for i in range(2, 10_000):
        candidate = path.with_name(f"{path.stem}-{i}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"too many summaries named like {path}")


def run_set(
    name: str,
    provider: ModelProvider,
    cfg: Any,
    store: LLMStore | None,
    out_dir: str | Path,
    limit: int | None = None,
    *,
    cases: Sequence[EvalCase] | None = None,
    now_ms: int | None = None,
) -> tuple[EvalSummary, Path]:
    """Run the committed set `name` (or the given `cases`) against `provider` through the real bot
    spec, write the `EvalSummary` JSON into `out_dir` and return it with the file path.

    The summary is only promotion evidence when the set holds real post-cutoff items (D-040,
    research/04 §2.1); the seed sets are synthetic.
    """
    if name not in SETS:
        raise KeyError(f"unknown eval set {name!r}; known: {sorted(SETS)}")
    runner = RUNNERS[name]
    selected = cases if cases is not None else load_set(name)
    summary = runner(provider, cfg, selected, store=store, now_ms=now_ms, limit=limit)
    path = save_summary(summary, unique_path(Path(out_dir) / summary_filename(summary)))
    return summary, path


def gate(
    baseline_path: str | Path,
    candidate_path: str | Path,
    *,
    max_agreement_drop: float = 0.02,
    max_latency_increase: float = 0.25,
) -> GateDecision:
    """Load two summaries and apply the LOCAL_LLM §7 promotion gate (`harness.compare`)."""
    return compare(
        load_summary(baseline_path),
        load_summary(candidate_path),
        max_agreement_drop=max_agreement_drop,
        max_latency_increase=max_latency_increase,
    )

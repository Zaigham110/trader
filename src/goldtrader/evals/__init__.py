"""evals — in-repo model-evaluation harness (D-040; LOCAL_LLM.md §7; LEARNING_SYSTEM.md §5).

Three versioned JSONL sets under `evals/sets/` (headline → NewsEvent fields, feature table → regime
label, trade journal → PostMortem class) are run against any `ModelProvider` through the real bot
`AgentSpec`s and scored for schema validity, enum agreement / macro-F1, latency and cost. The
promotion gate (`compare`) refuses a prompt or model change that regresses agreement by more than
2 points or latency by more than 25% (LOCAL_LLM §7). The seed sets are synthetic and must be
extended with real post-model-cutoff items before any promotion (D-040; research/04 §2.1).
"""

from goldtrader.evals.harness import (  # noqa: F401
    EvalCase,
    EvalResult,
    EvalSummary,
    GateDecision,
    compare,
    load_cases,
    load_set,
    load_summary,
    run_headlines,
    run_regimes,
    run_reviews,
    save_summary,
)

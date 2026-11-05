"""Hypothesis intake: Research-bot hypothesis → one immutable candidate bundle → experiment.

LEARNING_SYSTEM §3.1 (single change, pre-registered metric, ≤ 10 per week, look-ahead flag),
§2 (what may be learned: strategy parameters within configured ranges, rules from the library,
prompt versions), D-046 (candidate = champion + exactly one change), R-031 (every candidate is a
trial-ledger bundle), R-034 (nothing here writes config).

The Research bot's `change` text must be one of
    param:<name>=<float>      e.g. ``param:stop_atr=1.2``
    rule:kind=<kind>          e.g. ``rule:kind=breakout_session``
    prompt:<name>=<version>   e.g. ``prompt:strategy.system=3``
Anything else, or more than one change in the text, is refused (returns None).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

from goldtrader.backtest.gate import bundle_id
from goldtrader.config.schema import StrategyConfig
from goldtrader.db.repo import Repo
from goldtrader.learning.gates import LearningGates, gates_for
from goldtrader.learning.store import LearningStore
from goldtrader.learning.types import Change, Experiment, GateCheck, Stage, StageDecision

RULE_LIBRARY: tuple[str, ...] = ("trend_atr", "breakout_session")
"""Strategy kinds a `rule:` change may select (backtest/strategies.py `build_strategy`; `flat`
is the fallback, not a candidate)."""

METRICS: tuple[str, ...] = ("sharpe", "net_return_pct", "expectancy_r", "max_drawdown_pct")
"""Pre-registered metrics the offline stage can evaluate (§3.1)."""

_METRIC_ALIASES: tuple[tuple[str, str], ...] = (
    ("sharpe", "sharpe"),
    ("expectancy", "expectancy_r"),
    ("drawdown", "max_drawdown_pct"),
    ("return", "net_return_pct"),
    ("profit", "net_return_pct"),
)

_CHANGE_RE = re.compile(
    r"^\s*(param|rule|prompt)\s*:\s*([A-Za-z_][A-Za-z0-9_.\-]*)\s*=\s*(\S+)\s*$"
)
_INT_PARAMS = frozenset(
    {
        "ema_fast",
        "ema_slow",
        "atr_period",
        "range_start_hour",
        "range_end_hour",
        "trade_start_hour",
        "trade_end_hour",
    }
)
KNOWN_PARAMS: dict[str, frozenset[str]] = {
    "trend_atr": frozenset(
        {
            "stop_atr",
            "target_atr",
            "min_atr_pct",
            "max_atr_pctile",
            "entry_on_flip_only",
            "ema_fast",
            "ema_slow",
            "atr_period",
        }
    ),
    "breakout_session": frozenset(
        {
            "stop_atr",
            "target_atr",
            "range_start_hour",
            "range_end_hour",
            "trade_start_hour",
            "trade_end_hour",
            "ema_fast",
            "ema_slow",
            "atr_period",
        }
    ),
}
"""Parameters each library kind accepts (backtest/strategies.py `build_strategy` plus the feature
periods the features pipeline reads)."""


def normalise_metric(text: str) -> str:
    """Map the bot's free-text metric to one of METRICS; unknown text is returned lower-cased so
    the offline stage rejects it with a readable reason."""
    t = text.strip().lower()
    if t in METRICS:
        return t
    for needle, name in _METRIC_ALIASES:
        if needle in t:
            return name
    return t


def parse_change(text: str) -> tuple[str, str, str] | None:
    """`(change_type, key, raw_value)` for a well-formed single change, else None.

    Multi-change texts (``;``, ``,``, ``&``, ``and``, several ``=``) are refused (§3.1)."""
    if text.count("=") != 1 or re.search(r"[;,&]|\band\b", text):
        return None
    m = _CHANGE_RE.match(text)
    if m is None:
        return None
    return m.group(1), m.group(2), m.group(3)


def hypothesis_to_change(
    h: Mapping[str, Any], champion_params: Mapping[str, float], champion_kind: str
) -> Change | None:
    """Parse the hypothesis row's `change` into a Change against the champion, or None.

    The row's `change_type` (when present) must agree with the text's prefix. Values are typed
    here (float / int-valued float for parameters, str for kinds, int for prompt versions); bounds
    and library membership are checked in `propose`."""
    parsed = parse_change(str(h.get("change", "")))
    if parsed is None:
        return None
    ctype, key, raw = parsed
    declared = h.get("change_type")
    if declared is not None and str(declared) != ctype:
        return None
    if ctype == "param":
        try:
            value: Any = float(raw)
        except ValueError:
            return None
        if value != value or value in (float("inf"), float("-inf")):
            return None
        if key in _INT_PARAMS:
            if value != int(value):
                return None
            value = float(int(value))
        return Change("param", key, value, champion_params.get(key))
    if ctype == "rule":
        if key != "kind":
            return None
        return Change("rule", "kind", raw, champion_kind)
    # prompt:<name>=<version>
    try:
        version = int(raw)
    except ValueError:
        return None
    if version < 1:
        return None
    return Change("prompt", key, version, None)


# --- bounds (LEARNING_SYSTEM §2: "within configured ranges") ------------------------------------
def _stop_bounds(cfg: Any, strat: StrategyConfig) -> tuple[float, float, float]:
    """(stop_atr_min, stop_atr_max, min_reward_risk): the stricter of strategy and owner risk,
    the same rule the Strategy bot's `atr_bounds` applies."""
    r = cfg.owner.risk
    lo = max(strat.stop_atr_min, r.stop_atr_min)
    hi = min(strat.stop_atr_max, r.stop_atr_max)
    if lo > hi:
        lo, hi = r.stop_atr_min, r.stop_atr_max
    return lo, hi, max(strat.min_reward_risk, r.min_reward_risk)


def param_violation(
    kind: str, params: Mapping[str, float], cfg: Any, strat: StrategyConfig
) -> str | None:
    """Reason a parameter set is outside the learnable ranges, or None when it is admissible.

    Ranges (Assumption, from the strategy library's semantics): stop_atr within the owner /
    strategy stop bounds; target_atr ≥ stop_atr × min_reward_risk and ≤ 10 ATR; min_atr_pct in
    [0, 0.1]; max_atr_pctile in (0, 1]; entry_on_flip_only ∈ {0, 1}; EMA / ATR periods integers in
    [2, 500] with ema_fast < ema_slow; session hours integers in [0, 23] with start < end; unknown
    parameter names are refused."""
    lo, hi, rr = _stop_bounds(cfg, strat)
    known = KNOWN_PARAMS.get(kind)
    if known is None:
        return f"kind {kind!r} not in the rule library"
    for k in params:
        if k not in known:
            return f"unknown parameter {k!r} for {kind}"
    stop = float(params.get("stop_atr", 1.0))
    if not lo <= stop <= hi:
        return f"stop_atr={stop} outside [{lo}, {hi}]"
    target = float(params.get("target_atr", 1.5 if kind == "trend_atr" else 1.0))
    if not stop * rr <= target <= 10.0:
        return f"target_atr={target} outside [{stop * rr:.3g}, 10]"
    if "min_atr_pct" in params and not 0.0 <= float(params["min_atr_pct"]) <= 0.1:
        return f"min_atr_pct={params['min_atr_pct']} outside [0, 0.1]"
    if "max_atr_pctile" in params and not 0.0 < float(params["max_atr_pctile"]) <= 1.0:
        return f"max_atr_pctile={params['max_atr_pctile']} outside (0, 1]"
    if "entry_on_flip_only" in params and float(params["entry_on_flip_only"]) not in (0.0, 1.0):
        return "entry_on_flip_only must be 0 or 1"
    for k in ("ema_fast", "ema_slow", "atr_period"):
        if k in params and not (2 <= float(params[k]) <= 500 and float(params[k]).is_integer()):
            return f"{k}={params[k]} must be an integer in [2, 500]"
    if "ema_fast" in params or "ema_slow" in params:
        fast = float(params.get("ema_fast", 20))
        slow = float(params.get("ema_slow", 50))
        if fast >= slow:
            return f"ema_fast={fast} must be < ema_slow={slow}"
    hours = {k: params[k] for k in params if k.endswith("_hour")}
    for k, v in hours.items():
        if not (0 <= float(v) <= 23 and float(v).is_integer()):
            return f"{k}={v} must be an integer hour in [0, 23]"
    if kind == "breakout_session":
        rs, re_ = float(params.get("range_start_hour", 0)), float(params.get("range_end_hour", 7))
        ts, te = float(params.get("trade_start_hour", 7)), float(params.get("trade_end_hour", 12))
        if not (rs < re_ and ts < te):
            return "session range/trade windows must have start < end"
    return None


# --- champion and proposal ----------------------------------------------------------------------
def ensure_champion(
    store: LearningStore,
    repo: Repo,
    strat: StrategyConfig,
    cfg: Any,
    config_hash: str,
    now_ms: int,
) -> str:
    """Return the champion bundle id for `strat`, creating it from the config strategy when the
    strategy has none yet (the initial champion is the configured strategy, R-033)."""
    row = store.get_champion(strat.id)
    if row is not None:
        return str(row["bundle_id"])
    params = {k: float(v) for k, v in strat.params.items()}
    bid = bundle_id(strat.id, strat.kind, params, config_hash)
    repo.record_bundle(bid, strat.id, strat.kind, params, config_hash)
    store.set_champion(strat.id, bid, "initial champion from config", now_ms)
    store.set_status(bid, "active", now_ms, "initial champion")
    return bid


def _experiment_id(strategy_id: str, candidate: str, hypothesis_id: Any, now_ms: int) -> str:
    body = f"{strategy_id}|{candidate}|{hypothesis_id}|{now_ms}"
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def propose(
    store: LearningStore,
    repo: Repo,
    cfg: Any,
    strat: StrategyConfig,
    hypothesis_row: Mapping[str, Any],
    now_ms: int,
    config_hash: str,
    *,
    gates: LearningGates | None = None,
    reasons: list[str] | None = None,
) -> Experiment | None:
    """Turn a `hypotheses` row into an experiment in stage PROPOSED, or refuse (None).

    Refusals (§3.1, §2): weekly cap reached; `flagged_lookahead`; unparseable or multi-change
    text; a change that leaves the candidate equal to the champion; a parameter outside the
    learnable ranges; a rule kind not in the library. Accepted candidates are recorded as bundles
    (R-031) with the champion as parent, and the intake decision is logged as an event. When
    `reasons` is given the refusal reason is appended to it; the hypothesis row's `status` records
    the verdict either way."""
    g = gates_for(cfg, gates)
    h = dict(hypothesis_row)
    hid = h.get("id")

    def refuse(reason: str) -> None:
        if reasons is not None:
            reasons.append(reason)
        if hid is not None:
            store.set_hypothesis_status(hid, f"refused: {reason}")
        return None

    if store.weekly_proposal_count(strat.id, now_ms) >= g.max_proposals_per_week:
        return refuse(f"weekly cap {g.max_proposals_per_week} reached")
    if int(h.get("flagged_lookahead") or 0):
        return refuse("flagged_lookahead")

    champion_id = ensure_champion(store, repo, strat, cfg, config_hash, now_ms)
    champ = store.bundle(champion_id)
    champ_params: dict[str, float] = (
        {k: float(v) for k, v in champ["params"].items()}
        if champ is not None
        else {k: float(v) for k, v in strat.params.items()}
    )
    champ_kind = str(champ["strategy_kind"]) if champ is not None else strat.kind

    change = hypothesis_to_change(h, champ_params, champ_kind)
    if change is None:
        return refuse("change text is not a single param/rule/prompt change")

    cand_params = dict(champ_params)
    cand_kind = champ_kind
    prompt_hashes: dict[str, Any] | None = None
    if change.change_type == "param":
        if change.key in cand_params and float(cand_params[change.key]) == float(change.value):
            return refuse("no-op: candidate equals champion")
        cand_params[change.key] = float(change.value)
        why = param_violation(cand_kind, cand_params, cfg, strat)
        if why is not None:
            return refuse(why)
    elif change.change_type == "rule":
        if change.value not in RULE_LIBRARY:
            return refuse(f"rule kind {change.value!r} not in the library")
        if change.value == champ_kind:
            return refuse("no-op: candidate equals champion")
        cand_kind = str(change.value)
        # parameters the new kind does not know are dropped so the bundle is well-formed
        cand_params = {k: v for k, v in cand_params.items() if k in KNOWN_PARAMS[cand_kind]}
        why = param_violation(cand_kind, cand_params, cfg, strat)
        if why is not None:
            return refuse(why)
    else:  # prompt
        prompt_hashes = {str(change.key): int(change.value)}

    cand_id = bundle_id(
        strat.id,
        cand_kind,
        cand_params if prompt_hashes is None else {**cand_params, "__prompt__": prompt_hashes},
        config_hash,
        parent=champion_id,
    )
    if cand_id == champion_id:
        return refuse("no-op: candidate equals champion")
    repo.record_bundle(
        cand_id,
        strat.id,
        cand_kind,
        cand_params,
        config_hash,
        parent=champion_id,
        prompt_hashes=prompt_hashes,
    )
    exp = Experiment(
        experiment_id=_experiment_id(strat.id, cand_id, hid, now_ms),
        strategy_id=strat.id,
        candidate_bundle_id=cand_id,
        champion_bundle_id=champion_id,
        change=change,
        pre_registered_metric=normalise_metric(str(h.get("pre_registered_metric", ""))),
        stage=Stage.PROPOSED,
        stage_entered_ms=now_ms,
        created_ms=now_ms,
        hypothesis_id=None if hid is None else str(hid),
        regime_scope=h.get("regime_scope"),
        stats={
            "intake": {"title": h.get("title"), "candidate_kind": cand_kind, "params": cand_params}
        },
    )
    store.create(exp)
    store.log_event(
        StageDecision(
            experiment_id=exp.experiment_id,
            from_stage=Stage.PROPOSED,
            to_stage=Stage.PROPOSED,
            checks=[
                GateCheck(
                    "single_change", True, f"{change.change_type}:{change.key}={change.value}"
                ),
                GateCheck("weekly_cap", True, f"< {g.max_proposals_per_week}"),
                GateCheck("lookahead_flag", True, "not flagged"),
                GateCheck("bounds", True, "within configured ranges"),
            ],
            actor="intake",
        ),
        now_ms,
    )
    if hid is not None:
        store.set_hypothesis_status(hid, f"experiment:{exp.experiment_id}")
    return exp

"""Promotion pipeline: stage gates, promotion, rollback and limited-live routing.

LEARNING_SYSTEM §3 (R-032): proposed → offline → demo → shadow → limited_live → promoted, or
rejected / rolled_back; every stage decision is appended to `experiment_events` (§3 "each stage:
entry gate, exit gate, demotion triggers, automatic rollback"). Gates (D-045) are read from the
owner section and never written; `advance`, `promote` and `rollback` are wrapped by the owner-hash
guard (R-034). Nothing here writes config or reaches the gateway: shadow candidates are simulated
elsewhere and limited-live candidates only receive a *routing* answer (D-047).

Stage gates (defaults are the §3 numbers, Assumptions per research/04 §7):

* DEMO → SHADOW (§3.3): ≥ demo_min_days trading days (distinct exit days) and ≥ demo_min_trades
  candidate trades in the demo account since the stage began; bootstrap 95 % CI of expectancy after
  costs has lower bound > 0; zero monitor pauses for the candidate. A pause rejects; an
  insufficient sample waits; a sufficient sample with CI lower ≤ 0 rejects.
* SHADOW → LIMITED_LIVE (§3.3): ≥ shadow_min_days / shadow_min_trades closed shadow trades;
  measured slippage ≤ shadow_max_slippage_ratio × the cost model (mean `slippage_cost` per trade
  versus 2 × slippage_points × point × volume × contract); the paired e-process versus the champion
  starts here and its state is recorded; a mirrored-process rejection rejects.
* LIMITED_LIVE → PROMOTED (§3.4): ≥ live_min_days / live_min_trades; e-value ≥ threshold OR the CI
  criterion; no pause; the mirrored e-process rejects.
* Any stage → REJECTED when the experiment is older than max_experiment_days ("stop after
  6 months").
* §3.5 rollback: any monitor pause for the champion within rollback_window_days of its promotion
  reverts the pointer to the previous champion and marks the experiment rolled_back.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd

from goldtrader.backtest.costs import CostModel
from goldtrader.backtest.stats import bootstrap_ci
from goldtrader.config.schema import StrategyConfig
from goldtrader.db.repo import Repo
from goldtrader.learning.evalue import DAY_MS, EProcess, daily_pairs
from goldtrader.learning.gates import LearningGates, gates_for
from goldtrader.learning.immutable import OwnerGuard, guard
from goldtrader.learning.offline import run_offline
from goldtrader.learning.store import LearningStore
from goldtrader.learning.types import Experiment, GateCheck, Stage, StageDecision

DEFAULT_POINT = 0.01  # XAUUSD point (research/02 §5) used when no SymbolSpec is supplied
DEFAULT_CONTRACT = 100.0  # ounces per lot (Verified, research/02 §5)


def allocation_bucket(proposal_id: str) -> int:
    """Deterministic 0–99 bucket of a proposal id (sha256, not Python's salted hash) — D-047."""
    return int(hashlib.sha256(proposal_id.encode("utf-8")).hexdigest()[:8], 16) % 100


def distinct_days(trades: Sequence[Mapping[str, Any]]) -> int:
    return len({int(t["exit_ms"]) // DAY_MS for t in trades if t.get("exit_ms") is not None})


def expectancy_ci(
    trades: Sequence[Mapping[str, Any]], rng: np.random.Generator, key: str = "net_pnl"
) -> tuple[float, float] | None:
    """(point, lower 95 % bound) of the bootstrap CI of mean `key`; None with < 2 trades."""
    vals = [float(t[key]) for t in trades if t.get(key) is not None]
    if len(vals) < 2:
        return None
    ci = bootstrap_ci(vals, np.mean, n_boot=2000, alpha=0.05, rng=rng)
    return ci.point, ci.lower


class Pipeline:
    """Stage machine for one strategy's experiments (see module doc)."""

    def __init__(
        self,
        store: LearningStore,
        repo: Repo,
        cfg: Any,
        strat: StrategyConfig,
        clock_ms: Callable[[], int] | int,
        *,
        gates: LearningGates | None = None,
        spec: Any = None,
        cost: CostModel | None = None,
        rng: np.random.Generator | None = None,
    ) -> None:
        self.store = store
        self.repo = repo
        self.cfg = cfg
        self.strat = strat
        self._clock: Callable[[], int] = clock_ms if callable(clock_ms) else (lambda: int(clock_ms))
        self.gates = gates_for(cfg, gates)
        self.spec = spec
        self.cost = cost or CostModel(
            spread_multiplier=self.gates.spread_multiplier,
            commission_per_lot_per_side_usd=cfg.account.commission_per_lot_per_side_usd,
        )
        self.rng = rng or np.random.default_rng(0)
        self._owner_guard = OwnerGuard(cfg)

    def now(self) -> int:
        return int(self._clock())

    # --- stage advancement ---------------------------------------------------------------------
    @guard
    def advance(
        self,
        experiment: Experiment,
        *,
        df_features: pd.DataFrame | None = None,
        spec: Any = None,
    ) -> StageDecision:
        """Apply the gate of the experiment's current stage once and record the decision."""
        exp = experiment
        now = self.now()
        g = self.gates
        if exp.stage in (Stage.PROMOTED, Stage.REJECTED, Stage.ROLLED_BACK):
            raise ValueError(f"experiment {exp.experiment_id} is closed ({exp.stage})")
        age_days = (now - exp.created_ms) / DAY_MS
        if age_days > g.max_experiment_days:
            return self._apply(
                exp,
                Stage.REJECTED,
                [
                    GateCheck(
                        "max_experiment_days",
                        False,
                        f"{age_days:.0f} d > {g.max_experiment_days} d",
                    )
                ],
                now,
            )
        if exp.stage in (Stage.PROPOSED, Stage.OFFLINE):
            if df_features is None or (spec or self.spec) is None:
                raise ValueError("the offline stage needs df_features and a SymbolSpec")
            return run_offline(
                exp,
                self.store,
                self.repo,
                self.cfg,
                spec or self.spec,
                df_features,
                rng=self.rng,
                now_ms=now,
                gates=g,
            )
        if exp.stage == Stage.DEMO:
            to, checks = self._demo_gate(exp, now)
        elif exp.stage == Stage.SHADOW:
            to, checks = self._shadow_gate(exp, now, spec or self.spec)
        elif exp.stage == Stage.LIMITED_LIVE:
            to, checks = self._live_gate(exp, now)
        else:  # pragma: no cover - exhaustive
            raise ValueError(exp.stage)
        return self._apply(exp, to, checks, now)

    def _sample_checks(
        self, prefix: str, trades: Sequence[Mapping[str, Any]], min_days: int, min_trades: int
    ) -> tuple[bool, list[GateCheck]]:
        days, n = distinct_days(trades), len(trades)
        checks = [
            GateCheck(f"{prefix}_min_days", days >= min_days, f"{days} >= {min_days}"),
            GateCheck(f"{prefix}_min_trades", n >= min_trades, f"{n} >= {min_trades}"),
        ]
        return all(c.ok for c in checks), checks

    def _ci_check(self, trades: Sequence[Mapping[str, Any]]) -> GateCheck:
        ci = expectancy_ci(trades, self.rng)
        if ci is None:
            return GateCheck("expectancy_ci", False, "fewer than 2 trades")
        point, lower = ci
        return GateCheck(
            "expectancy_ci", lower > 0.0, f"expectancy {point:.2f} USD, 95% lower {lower:.2f} > 0"
        )

    def _pause_check(self, bundle: str, since_ms: int) -> GateCheck:
        pauses = self.store.monitor_pauses(bundle, since_ms)
        return GateCheck("no_pause", not pauses, f"{len(pauses)} pause/retire events")

    def _eprocess(self, exp: Experiment, cand: Sequence[Mapping[str, Any]]) -> EProcess:
        champ = self.store.trades_for_bundle(exp.champion_bundle_id, exp.stage_entered_ms)
        ep = EProcess.from_pairs(
            daily_pairs(cand, champ), self.gates.evalue_threshold, self.gates.evalue_bet_cap
        )
        self.store.set_stats(exp.experiment_id, {f"evalue_{exp.stage.value}": ep.state()})
        return ep

    def _demo_gate(self, exp: Experiment, now: int) -> tuple[Stage, list[GateCheck]]:
        g = self.gates
        trades = self.store.trades_for_bundle(exp.candidate_bundle_id, exp.stage_entered_ms)
        enough, checks = self._sample_checks("demo", trades, g.demo_min_days, g.demo_min_trades)
        pause = self._pause_check(exp.candidate_bundle_id, exp.stage_entered_ms)
        checks.append(pause)
        if not pause.ok:
            return Stage.REJECTED, checks
        if not enough:
            checks.append(GateCheck("expectancy_ci", False, "pending: sample not yet complete"))
            return exp.stage, checks
        ci = self._ci_check(trades)
        checks.append(ci)
        return (Stage.SHADOW if ci.ok else Stage.REJECTED), checks

    def _shadow_gate(self, exp: Experiment, now: int, spec: Any) -> tuple[Stage, list[GateCheck]]:
        g = self.gates
        shadow = self.store.shadow_trades_for(exp.candidate_bundle_id, exp.stage_entered_ms)
        enough, checks = self._sample_checks(
            "shadow", shadow, g.shadow_min_days, g.shadow_min_trades
        )
        ep = self._eprocess(exp, shadow)
        checks.append(
            GateCheck(
                "evalue_recorded",
                True,
                f"e={ep.e_value:.2f} mirror={ep.mirror_e_value:.2f} n={ep.n}",
            )
        )
        if ep.decision() == "reject":
            checks.append(GateCheck("mirror_eprocess", False, "champion beats challenger"))
            return Stage.REJECTED, checks
        if not enough:
            checks.append(GateCheck("slippage_ratio", False, "pending: sample not yet complete"))
            return exp.stage, checks
        ratio = self.slippage_ratio(shadow, spec)
        ok = ratio is not None and ratio <= g.shadow_max_slippage_ratio
        detail = (
            "no slippage measurements"
            if ratio is None
            else f"{ratio:.2f} <= {g.shadow_max_slippage_ratio}"
        )
        checks.append(GateCheck("slippage_ratio", ok, detail))
        return (Stage.LIMITED_LIVE if ok else Stage.REJECTED), checks

    def slippage_ratio(self, trades: Sequence[Mapping[str, Any]], spec: Any) -> float | None:
        """Mean measured `slippage_cost` per trade ÷ the cost model's expectation for the same
        volumes (two fills at `slippage_points` each)."""
        if not trades:
            return None
        point = float(getattr(spec, "point", DEFAULT_POINT)) if spec is not None else DEFAULT_POINT
        contract = (
            float(getattr(spec, "trade_contract_size", DEFAULT_CONTRACT))
            if spec is not None
            else DEFAULT_CONTRACT
        )
        measured = float(np.mean([float(t.get("slippage_cost") or 0.0) for t in trades]))
        expected = float(
            np.mean(
                [
                    2.0 * self.cost.slippage_points * point * float(t["volume"]) * contract
                    for t in trades
                ]
            )
        )
        if expected <= 0.0:
            return None
        return measured / expected

    def _live_gate(self, exp: Experiment, now: int) -> tuple[Stage, list[GateCheck]]:
        g = self.gates
        trades = self.store.trades_for_bundle(exp.candidate_bundle_id, exp.stage_entered_ms)
        enough, checks = self._sample_checks("live", trades, g.live_min_days, g.live_min_trades)
        pause = self._pause_check(exp.candidate_bundle_id, exp.stage_entered_ms)
        checks.append(pause)
        if not pause.ok:
            return Stage.REJECTED, checks
        ep = self._eprocess(exp, trades)
        e_ok = ep.e_value >= g.evalue_threshold
        checks.append(
            GateCheck("evalue", e_ok, f"e={ep.e_value:.2f} >= {g.evalue_threshold} (n={ep.n})")
        )
        if ep.decision() == "reject":
            checks.append(GateCheck("mirror_eprocess", False, "champion beats challenger"))
            return Stage.REJECTED, checks
        if not enough:
            checks.append(GateCheck("expectancy_ci", False, "pending: sample not yet complete"))
            return exp.stage, checks
        ci = self._ci_check(trades)
        checks.append(ci)
        return (Stage.PROMOTED if (e_ok or ci.ok) else exp.stage), checks

    def _apply(
        self, exp: Experiment, to: Stage, checks: list[GateCheck], now: int, actor: str = "pipeline"
    ) -> StageDecision:
        if to == Stage.PROMOTED:
            return self.promote(exp, checks=checks)
        d = StageDecision(exp.experiment_id, exp.stage, to, checks, actor=actor)
        if to == Stage.REJECTED:
            failed = ", ".join(c.name for c in checks if not c.ok) or exp.stage.value
            self.store.close(exp.experiment_id, Stage.REJECTED, f"{exp.stage.value}: {failed}", now)
            exp.closed_ms, exp.close_reason = now, f"{exp.stage.value}: {failed}"
        elif to != exp.stage:
            self.store.update_stage(exp.experiment_id, to, now)
        self.store.log_event(d, now)
        if to != exp.stage:
            exp.stage, exp.stage_entered_ms = to, now
        return d

    # --- promotion and rollback (§3.4, §3.5) ----------------------------------------------------
    @guard
    def promote(
        self, experiment: Experiment, *, checks: list[GateCheck] | None = None
    ) -> StageDecision:
        """Make the candidate the champion (previous kept), close the experiment as PROMOTED and
        close every other open experiment of the strategy as rejected('superseded')."""
        exp = experiment
        now = self.now()
        self.store.set_champion(
            exp.strategy_id,
            exp.candidate_bundle_id,
            f"promoted by experiment {exp.experiment_id}",
            now,
        )
        self.store.set_status(exp.candidate_bundle_id, "active", now, "promoted")
        self.store.close(exp.experiment_id, Stage.PROMOTED, "promoted", now)
        d = StageDecision(
            exp.experiment_id,
            exp.stage,
            Stage.PROMOTED,
            list(checks or []) + [GateCheck("promoted", True, "champion pointer moved")],
        )
        self.store.log_event(d, now)
        for other in self.store.active(exp.strategy_id):
            if other.experiment_id == exp.experiment_id:
                continue
            self.store.close(other.experiment_id, Stage.REJECTED, "superseded", now)
            self.store.log_event(
                StageDecision(
                    other.experiment_id,
                    other.stage,
                    Stage.REJECTED,
                    [GateCheck("superseded", False, f"by {exp.experiment_id}")],
                ),
                now,
            )
        exp.stage, exp.stage_entered_ms, exp.closed_ms, exp.close_reason = (
            Stage.PROMOTED,
            now,
            now,
            "promoted",
        )
        return d

    @guard
    def rollback(self, experiment: Experiment, reason: str) -> StageDecision:
        """Revert the champion pointer to the previous bundle and mark the experiment
        ROLLED_BACK; the rolled-back bundle is paused (the owner un-pauses). Nothing is deleted."""
        exp = experiment
        now = self.now()
        champ = self.store.get_champion(exp.strategy_id)
        checks = [GateCheck("rollback_trigger", False, reason)]
        if champ is not None and champ["bundle_id"] == exp.candidate_bundle_id:
            prev = champ.get("previous_bundle_id")
            if prev:
                self.store.set_champion(exp.strategy_id, prev, f"rollback: {reason}", now)
                checks.append(GateCheck("champion_reverted", True, f"→ {prev}"))
            else:
                checks.append(GateCheck("champion_reverted", False, "no previous champion"))
        else:
            checks.append(GateCheck("champion_reverted", False, "candidate is not the champion"))
        self.store.set_status(exp.candidate_bundle_id, "paused", now, f"rollback: {reason}")
        self.store.close(exp.experiment_id, Stage.ROLLED_BACK, reason, now)
        d = StageDecision(exp.experiment_id, exp.stage, Stage.ROLLED_BACK, checks, actor="monitor")
        self.store.log_event(d, now)
        exp.stage, exp.closed_ms, exp.close_reason = Stage.ROLLED_BACK, now, reason
        return d

    def check_rollback_triggers(self, now_ms: int | None = None) -> StageDecision | None:
        """§3.5: a monitor pause for the champion within `rollback_window_days` of its promotion
        rolls the promoting experiment back. Returns the decision, or None when nothing fired."""
        now = self.now() if now_ms is None else int(now_ms)
        champ = self.store.get_champion(self.strat.id)
        if champ is None:
            return None
        since = int(champ["since_ms"])
        if now - since > self.gates.rollback_window_days * DAY_MS:
            return None
        pauses = self.store.monitor_pauses(str(champ["bundle_id"]), since)
        if not pauses:
            return None
        exp = self.store.promoted_for(str(champ["bundle_id"]))
        if exp is None:
            return None  # the initial champion has nothing to roll back to
        p = pauses[0]
        return self.rollback(exp, f"{p['monitor']} {p['level']} at {p['at_ms']}")

    # --- limited-live routing (D-047) -----------------------------------------------------------
    def route(self, proposal_id: str, now_ms: int | None = None) -> tuple[str, str | None]:
        """(bundle_id, experiment_id | None) for a qualifying signal: during LIMITED_LIVE a
        deterministic `challenger_share` of proposal ids go to the active candidate; everything else
        (and every SHADOW-stage candidate) goes to the champion. Routing is logged."""
        now = self.now() if now_ms is None else int(now_ms)
        champ = self.store.get_champion(self.strat.id)
        champion_bundle = str(champ["bundle_id"]) if champ is not None else self.strat.id
        live = self.store.in_stage(self.strat.id, Stage.LIMITED_LIVE)
        if not live:
            return champion_bundle, None
        exp = live[0]
        if self.store.status(exp.candidate_bundle_id) != "active":
            return champion_bundle, None
        if allocation_bucket(proposal_id) < int(round(self.gates.challenger_share * 100)):
            self.store.route(proposal_id, exp.experiment_id, "challenger", now)
            return exp.candidate_bundle_id, exp.experiment_id
        self.store.route(proposal_id, exp.experiment_id, "champion", now)
        return champion_bundle, None

    def strategy_for_bundle(self, bundle_id: str) -> StrategyConfig:
        """The configured strategy with the bundle's kind and parameters (for the decision cycle's
        router, which needs a StrategyConfig variant alongside the bundle id)."""
        b = self.store.bundle(bundle_id)
        if b is None:
            return self.strat
        return self.strat.model_copy(
            update={
                "kind": b["strategy_kind"],
                "params": {k: float(v) for k, v in b["params"].items()},
            }
        )

"""Nightly learning job and shadow feed for the brain (LEARNING_SYSTEM.md §3, §4; D-045..D-047).

Order each night, after the daily break:
  1. make sure a champion bundle exists for the enabled strategy;
  2. intake new hypotheses from the weekly Research bot into experiments (single change, caps);
  3. run the offline gate for PROPOSED experiments on the stored bars;
  4. advance every active experiment through its stage gate;
  5. run the production monitors on the champion and apply pause/retire;
  6. check rollback triggers for a recently promoted champion;
  7. register SHADOW-stage candidates with the shadow ledger.
Nothing here writes to the owner section of config (OwnerGuard inside the pipeline).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from goldtrader.config.loader import config_hash
from goldtrader.config.schema import Config, StrategyConfig
from goldtrader.db.repo import Repo
from goldtrader.learning.intake import ensure_champion, propose
from goldtrader.learning.monitors import Monitors
from goldtrader.learning.offline import run_offline
from goldtrader.learning.pipeline import Pipeline
from goldtrader.learning.store import LearningStore
from goldtrader.learning.types import Stage
from goldtrader.shadow.ledger import ShadowLedger

log = logging.getLogger(__name__)


class LearningJob:
    def __init__(
        self,
        cfg: Config,
        repo: Repo,
        strat: StrategyConfig,
        clock_ms: Callable[[], int],
        *,
        spec: Any,
        frame_fn: Callable[[], Any],
    ) -> None:
        self.cfg = cfg
        self.repo = repo
        self.strat = strat
        self.clock_ms = clock_ms
        self.spec = spec
        self.frame_fn = frame_fn
        self.store = LearningStore(repo.c)
        self.pipeline = Pipeline(self.store, repo, cfg, strat, clock_ms, spec=spec)
        self.monitors = Monitors(repo.c, cfg.owner.learning, cfg.owner.risk, clock_ms)
        self.shadow = ShadowLedger(repo.c, cfg, spec, clock_ms, frame_provider=frame_fn)
        self.last_summary: dict[str, Any] = {}

    # ------------------------------------------------------------------ routing for the cycle
    def router(self, proposal_id: str) -> tuple[str, StrategyConfig]:
        bundle_id, _exp = self.pipeline.route(proposal_id)
        return bundle_id, self.pipeline.strategy_for_bundle(bundle_id)

    # ------------------------------------------------------------------ nightly
    def nightly(self) -> dict[str, Any]:
        now = self.clock_ms()
        chash = config_hash(self.cfg)
        summary: dict[str, Any] = {
            "at_ms": now,
            "intake": 0,
            "offline": [],
            "advanced": [],
            "monitors": [],
            "rollback": None,
            "shadow_registered": [],
        }
        champion = ensure_champion(self.store, self.repo, self.strat, self.cfg, chash, now)
        summary["champion"] = champion

        # 2. intake
        rows = self.repo.c.execute(
            "SELECT * FROM hypotheses WHERE status='proposed' ORDER BY created_ms LIMIT 20"
        ).fetchall()
        for r in rows:
            row = dict(r)
            try:
                exp = propose(self.store, self.repo, self.cfg, self.strat, row, now, chash)
            except Exception as e:  # noqa: BLE001
                log.exception("intake failed for hypothesis %s", row.get("id"))
                self.store.set_hypothesis_status(row["id"], f"error:{type(e).__name__}")
                continue
            if exp is not None:
                summary["intake"] += 1

        # 3. offline gate
        df = None
        for exp in self.store.in_stage(self.strat.id, Stage.PROPOSED):
            if df is None:
                df = self.frame_fn()
            if df is None or len(df) < 500:
                summary["offline"].append((exp.experiment_id, "insufficient_bars"))
                continue
            try:
                dec = run_offline(exp, self.store, self.repo, self.cfg, self.spec, df, now_ms=now)
                summary["offline"].append((exp.experiment_id, dec.to_stage.value))
            except Exception as e:  # noqa: BLE001
                log.exception("offline gate failed for %s", exp.experiment_id)
                summary["offline"].append((exp.experiment_id, f"error:{type(e).__name__}"))

        # 4. advance
        for exp in self.store.active(self.strat.id):
            if exp.stage is Stage.OFFLINE:
                continue  # offline handled above
            try:
                dec = self.pipeline.advance(exp, spec=self.spec)
                summary["advanced"].append((exp.experiment_id, exp.stage.value, dec.to_stage.value))
            except Exception as e:  # noqa: BLE001
                log.exception("advance failed for %s", exp.experiment_id)
                summary["advanced"].append(
                    (exp.experiment_id, exp.stage.value, f"error:{type(e).__name__}")
                )

        # 5. monitors on the champion
        try:
            results = self.monitors.run(
                champion,
                backtest_expectancy_r=None,
                backtest_sharpe=None,
                backtest_trade_pnls=None,
                expected_slippage_usd=None,
            )
            summary["monitors"] = [r.as_dict() for r in results]
        except Exception as e:  # noqa: BLE001
            log.exception("monitors failed")
            summary["monitors"] = [{"error": f"{type(e).__name__}: {e}"}]

        # 6. rollback triggers
        try:
            rb = self.pipeline.check_rollback_triggers(now)
            summary["rollback"] = rb.experiment_id if rb is not None else None
        except Exception as e:  # noqa: BLE001
            log.exception("rollback check failed")
            summary["rollback"] = f"error:{type(e).__name__}"

        # 7. shadow registrations
        for exp in self.store.in_stage(self.strat.id, Stage.SHADOW):
            b = self.store.bundle(exp.candidate_bundle_id)
            if b is None:
                continue
            params = b.get("params") or {}
            self.shadow.register(
                exp.experiment_id,
                exp.candidate_bundle_id,
                b.get("strategy_kind", self.strat.kind),
                params,
            )
            summary["shadow_registered"].append(exp.candidate_bundle_id)
        self.last_summary = summary
        return summary

    def status(self) -> dict[str, Any]:
        champ = self.store.get_champion(self.strat.id)
        active = self.store.active(self.strat.id)
        return {
            "champion": champ,
            "champion_status": self.monitors.status(champ["bundle_id"]) if champ else None,
            "experiments": [
                {
                    "id": e.experiment_id,
                    "stage": e.stage.value,
                    "change": e.change.__dict__,
                    "candidate": e.candidate_bundle_id,
                    "since_ms": e.stage_entered_ms,
                }
                for e in active
            ],
            "shadow_bundles": self.shadow.bundles(),
            "last_summary": self.last_summary,
        }

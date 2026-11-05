from datetime import UTC, datetime

import numpy as np

from goldtrader.backtest.gate import (
    GateThresholds,
    bundle_id,
    offline_gate,
    param_grid,
    render_report,
    sweep,
)
from goldtrader.backtest.metrics import compute_metrics
from goldtrader.config.loader import config_hash, parse_config
from goldtrader.data.features import add_features
from goldtrader.data.synthetic import generate_bars
from goldtrader.db import Repo, connect, migrate
from tests.test_backtest_engine import SPEC


def test_sweep_records_every_trial_and_gate_reports():
    df = add_features(generate_bars(datetime(2026, 1, 12, tzinfo=UTC), 15, seed=3, seconds=60))
    cfg = parse_config({"session": {"dst_mode": "winter"}})
    conn = connect(None)
    migrate(conn)
    repo = Repo(conn)
    grid = {"stop_atr": [0.8, 1.2], "target_atr": [1.2, 1.8]}
    assert len(param_grid(grid)) == 4
    sw = sweep(
        df,
        "trend_atr",
        "t",
        grid,
        cfg,
        SPEC,
        repo=repo,
        data_snapshot_id="synthetic",
        config_hash=config_hash(cfg),
    )
    assert repo.trial_count() == 4 and sw.daily_matrix.shape[1] == 4
    params, res, m = sw.runs[sw.best_index]
    rep = offline_gate(
        res,
        m,
        df,
        thresholds=GateThresholds(min_trades=5, min_days=5),
        ledger_sharpes=repo.trial_sharpes(),
        daily_matrix=sw.daily_matrix,
        rng=np.random.default_rng(1),
    )
    assert rep.n_trials_counted == 4
    assert set(rep.checks) >= {"min_trades", "min_days", "min_sharpe", "min_backtest_length"}
    # synthetic random-walk data must not pass a gate built for real edge
    assert rep.passed is False
    text = render_report(rep, "test")
    assert "Offline gate" in text and "| min_trades |" in text
    assert bundle_id("t", "trend_atr", params, "abc") != bundle_id("t", "trend_atr", params, "abd")


def test_gate_handles_no_trades():
    df = add_features(generate_bars(datetime(2026, 1, 12, tzinfo=UTC), 3, seed=1, seconds=60))
    cfg = parse_config({"session": {"dst_mode": "winter"}})
    from goldtrader.backtest.engine import run_backtest
    from goldtrader.backtest.strategies import FlatStrategy

    res = run_backtest(df, FlatStrategy(), cfg, SPEC)
    rep = offline_gate(
        res, compute_metrics(res), df, thresholds=GateThresholds(), ledger_sharpes=[]
    )
    assert rep.passed is False and rep.checks["min_trades"][0] is False

"""Production decay monitors (LEARNING_SYSTEM §4, research §3.1/§3.2): levels, persistence and the
active → paused → retired state machine with owner-only un-pause."""

from __future__ import annotations

import numpy as np
import pytest

from goldtrader.backtest.stats import monte_carlo_drawdown, sharpe
from goldtrader.config.schema import RiskLimits
from goldtrader.db import connect, migrate
from goldtrader.learning.monitors import (
    STATE_MONITOR,
    MonitorGates,
    MonitorResult,
    Monitors,
    highest_level,
)

GATES = MonitorGates(
    monitor_min_trades=20, monitor_sharpe_window_trades=10, monitor_fill_drift_trades=10
)
T0 = 1_760_000_000_000
HOUR = 3_600_000
DAY = 86_400_000


def _db():
    conn = connect(None)
    migrate(conn)
    return conn


def insert_trades(conn, bundle_id, rs, *, pnls=None, slips=None, start_ms=T0, step_ms=HOUR):
    """Hand-built closed trades for ``bundle_id``; exit_ms increases by ``step_ms``."""
    rs = list(rs)
    pnls = list(pnls) if pnls is not None else [r * 2.0 for r in rs]
    slips = list(slips) if slips is not None else [0.02] * len(rs)
    rows = []
    for i, (r, p, s) in enumerate(zip(rs, pnls, slips)):
        exit_ms = start_ms + (i + 1) * step_ms
        rows.append(
            (
                f"{bundle_id}-{start_ms}-{i}",
                bundle_id,
                "long",
                0.01,
                exit_ms - 600_000,
                exit_ms,
                4000.0,
                4000.0 + p,
                p + 0.2 + s,
                0.2,
                s,
                0.0,
                0.0,
                p,
                r,
                "stop" if p < 0 else "target",
            )
        )
    conn.executemany(
        "INSERT INTO trades(trade_id, bundle_id, side, volume, entry_ms, exit_ms, entry_price, "
        "exit_price, gross_pnl, spread_cost, slippage_cost, commission, swap, net_pnl, r_multiple, "
        "exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    return start_ms + len(rs) * step_ms


class Clock:
    def __init__(self, now=T0 + 10 * DAY):
        self.now = now

    def __call__(self):
        return self.now


def _mon(conn, clock=None, gates=GATES, **kw):
    return Monitors(conn, gates, RiskLimits(), clock or Clock(), **kw)


def _by(results, name):
    return next(r for r in results if r.monitor == name)


def _state_events(mon, bundle_id):
    return [e for e in mon.events(bundle_id) if e["monitor"] == STATE_MONITOR]


# --- levels ----------------------------------------------------------------------------------


def test_insufficient_sample_is_a_single_info_result():
    conn = _db()
    insert_trades(conn, "b", [0.5] * 5)
    mon = _mon(conn)
    res = mon.run(
        "b",
        backtest_expectancy_r=0.2,
        backtest_sharpe=0.3,
        backtest_trade_pnls=[1.0, -1.0],
        expected_slippage_usd=0.02,
    )
    assert res == [
        MonitorResult("sample", "info", 5.0, 20.0, "insufficient_sample: n=5 < 20"),
    ]
    assert mon.status("b") == "active"
    events = mon.events("b")
    assert len(events) == 1 and events[0]["monitor"] == "sample" and events[0]["level"] == "info"
    assert conn.execute("SELECT COUNT(*) FROM bundle_state").fetchone()[0] == 0


def test_stationary_series_runs_all_monitors_and_does_not_pause():
    rng = np.random.default_rng(4)  # a seed with no false alarm (ARL ~ 340 trades at k=0.5, h=4)
    bt_rs = rng.normal(0.3, 1.0, 500)
    live = rng.normal(0.3, 1.0, 80)
    conn = _db()
    insert_trades(conn, "b", live, pnls=live * 2.0, slips=[0.02] * 80)
    mon = _mon(conn)
    res = mon.run(
        "b",
        backtest_expectancy_r=0.3,
        backtest_sharpe=sharpe(bt_rs),
        backtest_trade_pnls=list(bt_rs * 2.0),
        expected_slippage_usd=0.02,
        backtest_trade_rs=list(bt_rs),
    )
    assert [r.monitor for r in res] == [
        "cusum",
        "rolling_sharpe",
        "mc_drawdown",
        "pchart",
        "fill_drift",
    ]
    assert highest_level(res) in ("info", "warn")
    assert _by(res, "cusum").level == "info" and _by(res, "cusum").threshold == pytest.approx(
        4.0 * bt_rs.std(ddof=1)
    )
    assert _by(res, "fill_drift").statistic == pytest.approx(1.0)
    assert mon.status("b") == "active"
    # every result was journaled, nothing else
    events = mon.events("b")
    assert [e["monitor"] for e in events] == [r.monitor for r in res]
    assert all(e["level"] == r.level for e, r in zip(events, res))


def test_regime_shift_breaches_cusum_and_pauses():
    rng = np.random.default_rng(5)
    bt_rs = rng.normal(0.3, 1.0, 500)
    live = [0.3] * 30 + [-1.2] * 40  # a persistent downward shift in mean
    conn = _db()
    # tiny P&L keeps the drawdown monitor quiet so the CUSUM decision is isolated
    insert_trades(conn, "b", live, pnls=[r * 0.001 for r in live])
    mon = _mon(conn)
    res = mon.run(
        "b",
        backtest_expectancy_r=0.3,
        backtest_sharpe=None,
        backtest_trade_pnls=list(bt_rs * 2.0),
        expected_slippage_usd=None,
        backtest_trade_rs=list(bt_rs),
    )
    c = _by(res, "cusum")
    assert c.level == "pause" and c.statistic > c.threshold and "breached_at=" in c.detail
    assert _by(res, "rolling_sharpe").level == "info"  # no reference -> info
    assert mon.status("b") == "paused"
    row = conn.execute("SELECT * FROM bundle_state WHERE bundle_id='b'").fetchone()
    assert (
        row["status"] == "paused" and "cusum" in row["reason"] and row["since_ms"] == mon.clock_ms()
    )
    (ev,) = _state_events(mon, "b")
    assert ev["level"] == "pause" and ev["detail"].startswith("active->paused: cusum")


def test_rolling_sharpe_below_band_for_two_windows_pauses():
    rng = np.random.default_rng(11)
    bt_rs = rng.normal(1.0, 0.5, 400)  # a strong backtest: per-trade SR 2
    live = list(rng.normal(1.0, 0.5, 20)) + list(rng.normal(-0.5, 0.5, 40))
    conn = _db()
    # tiny P&L against a wide backtest P&L null keeps the drawdown monitor quiet
    insert_trades(conn, "b", live, pnls=[r * 0.001 for r in live])
    mon = _mon(conn)
    res = mon.run(
        "b",
        backtest_expectancy_r=None,
        backtest_sharpe=sharpe(bt_rs),
        backtest_trade_pnls=list(rng.normal(0.5, 1.0, 400)),
        expected_slippage_usd=None,
        backtest_trade_rs=None,
    )
    rs = _by(res, "rolling_sharpe")
    assert rs.level == "pause" and rs.statistic < rs.threshold and "trailing_below=" in rs.detail
    assert mon.status("b") == "paused"


def test_drawdown_beyond_mc_p99_retires():
    rng = np.random.default_rng(7)
    bt_pnls = rng.normal(0.5, 1.0, 400)
    conn = _db()
    live_pnls = [-5.0] * 30
    insert_trades(conn, "b", [-2.5] * 30, pnls=live_pnls)
    mon = _mon(conn)
    res = mon.run(
        "b",
        backtest_expectancy_r=0.25,
        backtest_sharpe=None,
        backtest_trade_pnls=list(bt_pnls),
        expected_slippage_usd=None,
    )
    dd = _by(res, "mc_drawdown")
    assert dd.level == "retire" and dd.statistic == pytest.approx(150.0)
    assert _by(res, "cusum").level == "info" and "sigma_zero" in _by(res, "cusum").detail
    assert mon.status("b") == "retired"
    (ev,) = _state_events(mon, "b")
    assert ev["level"] == "retire" and ev["detail"].startswith("active->retired: mc_drawdown")
    with pytest.raises(ValueError):
        mon.unpause("b")


def test_drawdown_between_p95_and_p99_pauses():
    rng = np.random.default_rng(9)
    bt_pnls = rng.normal(0.5, 1.0, 400)
    mc = monte_carlo_drawdown(bt_pnls, n_perm=2000, rng=np.random.default_rng(20261105))
    target = (mc.dd_p95 + mc.dd_p99) / 2.0
    live_pnls = [1.0] * 10 + [-target / 10.0] * 10 + [0.5] * 10  # peak, then a DD of `target`
    conn = _db()
    insert_trades(conn, "b", [0.1] * 30, pnls=live_pnls)
    mon = _mon(conn)
    res = mon.run(
        "b",
        backtest_expectancy_r=0.0,
        backtest_sharpe=None,
        backtest_trade_pnls=list(bt_pnls),
        expected_slippage_usd=None,
    )
    dd = _by(res, "mc_drawdown")
    assert dd.level == "pause" and dd.statistic == pytest.approx(target)
    assert dd.threshold == pytest.approx(mc.dd_p95, rel=1e-6)
    assert mon.status("b") == "paused"


def test_mc_drawdown_falls_back_to_the_live_list():
    conn = _db()
    rng = np.random.default_rng(2)
    live = rng.normal(0.2, 1.0, 40)
    insert_trades(conn, "b", live, pnls=live)
    res = _mon(conn).run(
        "b",
        backtest_expectancy_r=None,
        backtest_sharpe=None,
        backtest_trade_pnls=None,
        expected_slippage_usd=None,
    )
    assert "null=live" in _by(res, "mc_drawdown").detail
    assert _by(res, "pchart").detail == "no_reference_hit_rate"


def test_fill_drift_pauses_on_the_last_window_only():
    conn = _db()
    # first 20 trades fine, last 10 (the fill-drift window) at 2.5x the model
    slips = [0.02] * 20 + [0.05] * 10
    insert_trades(conn, "b", [0.1] * 30, pnls=[0.2] * 30, slips=slips)
    mon = _mon(conn)
    res = mon.run(
        "b",
        backtest_expectancy_r=0.1,
        backtest_sharpe=None,
        backtest_trade_pnls=None,
        expected_slippage_usd=0.02,
    )
    fd = _by(res, "fill_drift")
    assert fd.level == "pause" and fd.statistic == pytest.approx(2.5) and fd.threshold == 1.5
    assert mon.status("b") == "paused"
    assert conn.execute("SELECT reason FROM bundle_state").fetchone()[0] == "fill_drift"
    # within the ratio -> no pause
    conn2 = _db()
    insert_trades(conn2, "b", [0.1] * 30, pnls=[0.2] * 30, slips=[0.029] * 30)
    res2 = _mon(conn2).run(
        "b",
        backtest_expectancy_r=0.1,
        backtest_sharpe=None,
        backtest_trade_pnls=None,
        expected_slippage_usd=0.02,
    )
    assert _by(res2, "fill_drift").level == "info" and _mon(conn2).status("b") == "active"


def test_hit_rate_pchart_warns_without_pausing():
    conn = _db()
    # 10 wins in 60 against a backtest hit rate of 0.6: p_hat 0.167 < lcl 0.41
    live_rs = ([1.0] + [-0.1] * 5) * 10
    live_pnls = [1.0 if r > 0 else -0.01 for r in live_rs]
    bt_pnls = [1.0] * 60 + [-1.0] * 40
    insert_trades(conn, "b", live_rs, pnls=live_pnls)
    mon = _mon(conn)
    res = mon.run(
        "b",
        backtest_expectancy_r=0.0,
        backtest_sharpe=None,
        backtest_trade_pnls=bt_pnls,
        expected_slippage_usd=None,
    )
    pc = _by(res, "pchart")
    assert pc.level == "warn" and pc.statistic == pytest.approx(1 / 6) and pc.threshold < 0.45
    assert highest_level(res) == "warn"
    assert mon.status("b") == "active" and _state_events(mon, "b") == []


# --- state machine ---------------------------------------------------------------------------


def _pause_by_fill_drift(conn, mon, start_ms):
    end = insert_trades(
        conn, "b", [0.1] * 30, pnls=[0.2] * 30, slips=[0.05] * 30, start_ms=start_ms
    )
    mon.run(
        "b",
        backtest_expectancy_r=0.1,
        backtest_sharpe=None,
        backtest_trade_pnls=None,
        expected_slippage_usd=0.02,
    )
    return end


def test_already_paused_bundle_is_not_escalated_by_rerun():
    conn = _db()
    clock = Clock()
    mon = _mon(conn, clock)
    _pause_by_fill_drift(conn, mon, T0)
    clock.now += HOUR
    mon.run(
        "b",
        backtest_expectancy_r=0.1,
        backtest_sharpe=None,
        backtest_trade_pnls=None,
        expected_slippage_usd=0.02,
    )
    assert mon.status("b") == "paused"
    assert [e["level"] for e in _state_events(mon, "b")] == ["pause"]


def test_owner_unpause_logs_and_resets_the_sample():
    conn = _db()
    clock = Clock()
    mon = _mon(conn, clock)
    _pause_by_fill_drift(conn, mon, T0)
    assert mon.status("b") == "paused"
    clock.now += HOUR
    assert mon.unpause("b", actor="owner", reason="execution reviewed") is True
    assert mon.status("b") == "active"
    ev = _state_events(mon, "b")[-1]
    assert ev["level"] == "info" and ev["detail"] == "paused->active by owner: execution reviewed"
    row = conn.execute("SELECT * FROM bundle_state WHERE bundle_id='b'").fetchone()
    assert row["status"] == "active" and row["since_ms"] == clock.now
    # trades before the un-pause no longer count: the next run sees an empty sample
    clock.now += HOUR
    res = mon.run(
        "b",
        backtest_expectancy_r=0.1,
        backtest_sharpe=None,
        backtest_trade_pnls=None,
        expected_slippage_usd=0.02,
    )
    assert res[0].monitor == "sample" and mon.status("b") == "active"
    assert mon.unpause("b") is False  # already active


def test_second_pause_within_90_days_retires():
    conn = _db()
    clock = Clock()
    mon = _mon(conn, clock)
    end = _pause_by_fill_drift(conn, mon, T0)
    clock.now += HOUR
    mon.unpause("b")
    clock.now += 30 * DAY
    _pause_by_fill_drift(conn, mon, max(end, clock.now - 29 * DAY))  # trades after the reset
    assert mon.status("b") == "retired"
    levels = [e["level"] for e in _state_events(mon, "b")]
    assert levels == ["pause", "info", "retire"]
    assert "second pause within 90 days" in _state_events(mon, "b")[-1]["detail"]
    with pytest.raises(ValueError):
        mon.unpause("b")


def test_pause_after_the_90_day_window_is_only_a_pause():
    conn = _db()
    clock = Clock()
    mon = _mon(conn, clock)
    _pause_by_fill_drift(conn, mon, T0)
    clock.now += HOUR
    mon.unpause("b")
    clock.now += 100 * DAY
    _pause_by_fill_drift(conn, mon, clock.now - 50 * DAY)
    assert mon.status("b") == "paused"
    assert [e["level"] for e in _state_events(mon, "b")] == ["pause", "info", "pause"]


def test_unpause_is_owner_only_and_never_automatic():
    conn = _db()
    mon = _mon(conn)
    _pause_by_fill_drift(conn, mon, T0)
    with pytest.raises(ValueError):
        mon.unpause("b", actor="monitor")
    with pytest.raises(ValueError):
        mon.unpause("b", actor="pipeline")
    assert mon.status("b") == "paused"
    # a clean run on fresh, good trades does not re-activate a paused bundle
    insert_trades(conn, "b", [0.5] * 30, pnls=[1.0] * 30, slips=[0.02] * 30, start_ms=T0 + 5 * DAY)
    mon.run(
        "b",
        backtest_expectancy_r=0.1,
        backtest_sharpe=None,
        backtest_trade_pnls=None,
        expected_slippage_usd=0.02,
    )
    assert mon.status("b") == "paused"


def test_gates_are_duck_typed_with_section_4_defaults():
    conn = _db()
    insert_trades(conn, "b", [0.1] * 30)

    class Partial:  # only one monitor_* field: the rest fall back to the §4 defaults
        monitor_fill_drift_ratio = 2.0

    mon = Monitors(conn, Partial(), RiskLimits(), Clock())
    res = mon.run(
        "b",
        backtest_expectancy_r=None,
        backtest_sharpe=None,
        backtest_trade_pnls=None,
        expected_slippage_usd=0.02,
    )
    assert res[0].monitor == "sample" and res[0].threshold == 50.0  # default min trades
    assert mon._g("monitor_fill_drift_ratio") == 2.0 and mon._g("monitor_cusum_h_sigma") == 4.0
    assert Monitors(conn, None, RiskLimits(), Clock()).gates == MonitorGates()


def test_real_learning_gates_object_is_accepted():
    """The owner-only ``LearningGates`` (owner.learning, D-045) carries the monitor_* fields."""
    schema = pytest.importorskip("goldtrader.config.schema")
    if not hasattr(schema, "LearningGates"):
        pytest.skip("LearningGates not in the config schema yet")
    from goldtrader.config.loader import parse_config

    cfg = parse_config({})
    gates = cfg.owner.learning
    for name in MonitorGates.__dataclass_fields__:
        assert hasattr(gates, name), f"LearningGates lacks {name}"
    conn = _db()
    insert_trades(conn, "b", [0.1] * 30, pnls=[0.2] * 30, slips=[0.05] * 30)
    mon = Monitors(conn, gates, cfg.owner.risk, Clock())
    assert mon._g("monitor_mc_pause_pctile") == gates.monitor_mc_pause_pctile
    res = mon.run(
        "b",
        backtest_expectancy_r=0.1,
        backtest_sharpe=None,
        backtest_trade_pnls=None,
        expected_slippage_usd=0.02,
    )
    assert res[0].monitor == "sample" and res[0].threshold == float(gates.monitor_min_trades)


def test_highest_level_order():
    mk = lambda lvl: MonitorResult("x", lvl, None, None, "")  # noqa: E731
    assert highest_level([]) == "info"
    assert highest_level([mk("info"), mk("warn")]) == "warn"
    assert highest_level([mk("pause"), mk("warn"), mk("retire")]) == "retire"
    assert mk("warn").as_dict()["level"] == "warn"

from goldtrader.db import Repo, connect, migrate
from goldtrader.types import Bar, SymbolSpec


def _spec(**over):
    base = {
        "name": "XAUUSDm",
        "digits": 2,
        "point": 0.01,
        "trade_tick_size": 0.01,
        "trade_tick_value": 1.0,
        "trade_contract_size": 100.0,
        "volume_min": 0.01,
        "volume_max": 200.0,
        "volume_step": 0.01,
        "volume_limit": 0.0,
        "trade_stops_level": 0,
        "trade_freeze_level": 0,
        "trade_mode": 4,
        "trade_exemode": 2,
        "filling_mode": 3,
        "swap_mode": 1,
        "swap_long": -30.0,
        "swap_short": 10.0,
        "swap_rollover3days": 3,
        "margin_initial": 0.0,
        "margin_hedged": 0.0,
        "currency_base": "XAU",
        "currency_profit": "USD",
        "currency_margin": "USD",
        "spread": 20,
        "spread_float": True,
    }
    base.update(over)
    return SymbolSpec(**base)


def test_migrate_is_idempotent_and_versioned():
    conn = connect(None)
    v = migrate(conn)
    assert v >= 1 and migrate(conn) == v  # idempotent; later phases add migrations
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"bars", "trial_ledger", "symbol_specs", "trades", "bundles"} <= tables


def test_spec_round_trip_and_bars():
    conn = connect(None)
    migrate(conn)
    repo = Repo(conn)
    assert repo.latest_spec("XAUUSDm") is None
    repo.record_spec(_spec(), [], False)
    assert repo.latest_spec("XAUUSDm") == _spec()
    bars = [
        Bar(
            time=60 * i,
            seconds=60,
            bid_open=1,
            bid_high=2,
            bid_low=0.5,
            bid_close=1.5,
            ask_open=1.2,
            ask_high=2.2,
            ask_low=0.7,
            ask_close=1.7,
            ticks=3,
        )
        for i in range(5)
    ]
    assert repo.upsert_bars("XAUUSDm", bars, "synthetic") == 5
    assert repo.upsert_bars("XAUUSDm", bars, "synthetic") == 5  # idempotent
    loaded = repo.load_bars("XAUUSDm", 60, start_s=60, end_s=240)
    assert [b.time for b in loaded] == [60, 120, 180]
    assert repo.bar_gaps("XAUUSDm", 60, [0, 60, 300]) == [300]


def test_trial_ledger_is_append_only():
    conn = connect(None)
    migrate(conn)
    repo = Repo(conn)
    rid = repo.append_trial(
        bundle_id="b1",
        change_type="param",
        description="x",
        data_snapshot_id="s",
        cost_model_json={"spread_mult": 1.5},
        n_trades=10,
        n_days=5,
        sharpe=0.3,
        created_by="owner",
        status="recorded",
    )
    assert rid == 1 and repo.trial_count() == 1 and repo.trial_sharpes() == [0.3]
    assert not any(m.startswith(("update_trial", "delete_trial")) for m in dir(repo))


def test_heartbeat_upsert_and_alert():
    conn = connect(None)
    migrate(conn)
    repo = Repo(conn)
    repo.heartbeat("gateway", "ok")
    repo.heartbeat("gateway", "degraded", {"why": "stale"})
    row = conn.execute("SELECT status FROM heartbeats WHERE process='gateway'").fetchone()
    assert row[0] == "degraded"
    assert repo.alert("critical", "kill_switch", "drawdown") == 1

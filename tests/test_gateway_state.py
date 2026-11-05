from datetime import UTC, datetime

from goldtrader.config.loader import parse_config
from goldtrader.data.resample import frame_to_bars
from goldtrader.data.synthetic import generate_bars, generate_gold_ticks
from goldtrader.db import Repo, connect, migrate
from goldtrader.gateway.market import LiveMarket
from goldtrader.gateway.state import GatewayFlags, PnLTracker, StateBuilder
from goldtrader.mt5.fake import FakeMT5
from goldtrader.risk import check_entry, EntryRequest
from goldtrader.session.state import SessionClock
from goldtrader.types import SessionState

START = datetime(2026, 1, 13, 8, 0, tzinfo=UTC)  # Tuesday 08:00 UTC winter → OPEN


def _market_with_history(repo=None):
    # three days of closed bars as warm history (so same-hour spread medians exist), then 12 hours
    # of live ticks fed one by one
    hist = generate_bars(START.replace(day=START.day - 3), 3, seed=10, seconds=60)
    ticks = generate_gold_ticks(START, 0.5, seed=11)  # 12 hours, 1 tick/s
    lm = LiveMarket("XAUUSDm", repo, window_bars=20000)
    lm.preload(frame_to_bars(hist, 60))
    closed = 0
    wall = START.timestamp() * 1000
    for t in ticks:
        if lm.on_tick(t, int(t.time_msc)):
            closed += 1
        wall = t.time_msc
    return lm, closed, int(wall)


def test_live_market_builds_bars_and_features():
    conn = connect(None)
    migrate(conn)
    repo = Repo(conn)
    lm, closed, wall = _market_with_history(repo)
    assert closed > 600
    f = lm.features()
    assert f is not None and f.atr is not None and f.atr > 0 and f.spread_pips > 0
    assert f.spread_regime in ("normal", "wide", "unknown")
    assert len(repo.load_bars("XAUUSDm", 60)) == closed  # preloaded bars are not re-written
    assert lm.tick_age_s(wall + 3000) == 3.0


def test_state_builder_feeds_risk_engine():
    client = FakeMT5()
    client.initialize()
    lm, _, wall = _market_with_history()
    last = lm.last_tick
    client.set_price(last.bid, last.ask, last.time_msc)
    cfg = parse_config(
        {"session": {"dst_mode": "winter"}, "owner": {"risk": {"max_volume_lots": 1.0}}}
    )
    spec = client.symbol_info("XAUUSDm")
    sb = StateBuilder(
        client, spec, cfg, lm, SessionClock(cfg.session), GatewayFlags(), PnLTracker()
    )
    snap = sb.snapshot(wall + 500)
    assert snap is not None
    assert snap.session is SessionState.OPEN and snap.entries_allowed
    assert snap.market.margin_per_lot is not None and snap.account.equity == 5000.0
    atr = snap.market.atr
    req = EntryRequest(
        "long",
        stop_price=last.ask - 0.8 * atr,
        target_price=last.ask + 1.2 * atr,
        bundle_id="b",
        proposal_id="p",
    )
    v = check_entry(req, snap.account, snap.market, cfg, snap.ctx)
    assert v.ok, v
    # disconnect → STALE → entries blocked
    client.connected = False
    snap2 = sb.snapshot(wall + 600)
    assert snap2.session is SessionState.STALE and not snap2.entries_allowed
    client.connected = True
    # owner hash mismatch is detected
    sb.flags.owner_hash_expected = "deadbeefdeadbeef"
    assert not sb.snapshot(wall + 700).ctx.owner_hash_ok


def test_pnl_tracker_rolls_days_and_cooldown():
    cfg = parse_config({})
    p = PnLTracker()
    p.roll(1_000_000, 100.0)
    assert p.start_equity_day == 100.0 and p.equity_peak == 100.0
    p.roll(1_000_000 + 86400, 120.0)
    assert p.start_equity_day == 120.0 and p.equity_peak == 120.0
    p.note_order(5_000)
    p.note_order(6_000)
    assert p.orders_last_minute(7_000) == 2 and p.orders_last_minute(70_000) == 0
    for _ in range(3):
        p.note_trade_result(-1.0, 10_000, cfg)
    assert p.cooldown_until_ms == 10_000 + 60 * 60_000 and p.consecutive_losses == 0

"""Risk engine unit tests and the D-032 property test (20,000 random cases)."""

import numpy as np
import pytest

from goldtrader.config.loader import parse_config
from goldtrader.risk import (
    AccountState,
    EntryRequest,
    MarketState,
    RejectCode,
    RiskContext,
    StopMoveRequest,
    check_close,
    check_entry,
    check_stop_move,
)
from goldtrader.types import SessionState


def mk_cfg(profile="normal", **risk):
    return parse_config({"owner": {"profile": profile, "risk": risk}})


def mk_acct(**over):
    base = dict(
        equity=5000.0,
        balance=5000.0,
        margin_free=5000.0,
        margin_level=None,
        leverage=500,
        open_positions=0,
        day_pnl=0.0,
        week_pnl=0.0,
        equity_peak=5000.0,
        orders_last_minute=0,
        consecutive_losses=0,
        cooldown_until_ms=0,
        start_equity_day=5000.0,
        start_equity_week=5000.0,
        live_start_equity=5000.0,
    )
    base.update(over)
    return AccountState(**base)


def mk_market(**over):
    base = dict(
        bid=4000.00,
        ask=4000.20,
        point=0.01,
        digits=2,
        atr=40.0,
        spread_pips=20.0,
        spread_regime="normal",
        session=SessionState.OPEN,
        entries_allowed=True,
        tick_age_s=0.5,
        trade_mode=4,
        contract_size=100.0,
        volume_min=0.01,
        volume_max=200.0,
        volume_step=0.01,
        stops_level_points=0,
        freeze_level_points=0,
        margin_per_lot=800.0,
    )
    base.update(over)
    return MarketState(**base)


CTX = RiskContext(now_ms=1_000_000)


def req(side="long", stop=3980.0, target=4040.0, volume=None):
    return EntryRequest(
        side=side,
        stop_price=stop,
        target_price=target,
        bundle_id="b",
        proposal_id="p",
        volume=volume,
    )


def test_happy_path_sizes_from_risk():
    v = check_entry(
        req(), mk_acct(), mk_market(), mk_cfg(risk_per_trade_pct=1.0, max_volume_lots=1.0), CTX
    )
    assert v.ok
    # 1% of 5000 = 50 USD; stop distance 20.2 USD × 100 oz = 2020 per lot → 0.02 lot
    assert v.volume == pytest.approx(0.02) and v.risk_pct <= 1.0 and v.reward_risk > 1.0


@pytest.mark.parametrize(
    "ctx,code",
    [
        (RiskContext(kill_switch=True), RejectCode.KILL_SWITCH),
        (RiskContext(paused=True), RejectCode.PAUSED),
        (RiskContext(owner_hash_ok=False), RejectCode.OWNER_HASH_MISMATCH),
        (RiskContext(spec_ok=False), RejectCode.SPEC_CRITICAL_CHANGE),
        (RiskContext(incident_open=True), RejectCode.INCIDENT_OPEN),
    ],
)
def test_global_blocks(ctx, code):
    assert check_entry(req(), mk_acct(), mk_market(), mk_cfg(), ctx).code == code


@pytest.mark.parametrize(
    "acct_over,code",
    [
        ({"day_pnl": -100.0}, RejectCode.DAILY_LOSS),
        ({"week_pnl": -200.0}, RejectCode.WEEKLY_LOSS),
        ({"equity": 4400.0}, RejectCode.DRAWDOWN),
        ({"open_positions": 1}, RejectCode.MAX_POSITIONS),
        ({"cooldown_until_ms": 2_000_000}, RejectCode.COOLDOWN),
        ({"orders_last_minute": 5}, RejectCode.ORDER_RATE),
    ],
)
def test_account_blocks(acct_over, code):
    assert check_entry(req(), mk_acct(**acct_over), mk_market(), mk_cfg(), CTX).code == code


@pytest.mark.parametrize(
    "m_over,code",
    [
        ({"session": SessionState.CLOSED}, RejectCode.SESSION_CLOSED),
        ({"session": SessionState.NEWS_WINDOW}, RejectCode.SESSION_CLOSED),
        ({"entries_allowed": False}, RejectCode.ENTRIES_NOT_ALLOWED),
        ({"trade_mode": 3}, RejectCode.NOT_FULL_TRADE_MODE),
        ({"tick_age_s": 30.0}, RejectCode.STALE_DATA),
        ({"spread_pips": 90.0}, RejectCode.SPREAD_TOO_WIDE),
        ({"spread_regime": "wide"}, RejectCode.SPREAD_REGIME_WIDE),
        ({"spread_regime": "unknown"}, RejectCode.SPREAD_REGIME_WIDE),
        ({"atr": None}, RejectCode.NO_ATR),
        ({"bid": float("nan")}, RejectCode.INVALID_PRICES),
    ],
)
def test_market_blocks(m_over, code):
    assert check_entry(req(), mk_acct(), mk_market(**m_over), mk_cfg(), CTX).code == code


def test_stop_and_target_geometry():
    cfg = mk_cfg(max_volume_lots=1.0)
    assert (
        check_entry(req(stop=4001.0), mk_acct(), mk_market(), cfg, CTX).code
        == RejectCode.STOP_WRONG_SIDE
    )
    assert (
        check_entry(req(stop=3999.99), mk_acct(), mk_market(), cfg, CTX).code
        == RejectCode.STOP_INSIDE_SPREAD
    )
    assert (
        check_entry(req(stop=3995.0), mk_acct(), mk_market(), cfg, CTX).code
        == RejectCode.STOP_TOO_TIGHT
    )
    assert (
        check_entry(req(stop=3900.0), mk_acct(), mk_market(), cfg, CTX).code
        == RejectCode.STOP_TOO_WIDE
    )
    assert (
        check_entry(req(target=3990.0), mk_acct(), mk_market(), cfg, CTX).code
        == RejectCode.TARGET_WRONG_SIDE
    )
    assert (
        check_entry(req(target=4010.0), mk_acct(), mk_market(), cfg, CTX).code
        == RejectCode.REWARD_RISK
    )
    s = check_entry(req("short", stop=4020.2, target=3960.0), mk_acct(), mk_market(), cfg, CTX)
    assert s.ok and s.stop_distance == pytest.approx(20.2)
    assert (
        check_entry(req("short", stop=4000.21), mk_acct(), mk_market(), cfg, CTX).code
        == RejectCode.STOP_INSIDE_SPREAD
    )
    assert check_entry(req(target=None), mk_acct(), mk_market(), cfg, CTX).ok


def test_explicit_volume_is_validated_never_raised():
    cfg = mk_cfg(risk_per_trade_pct=1.0, max_volume_lots=1.0)
    assert (
        check_entry(req(volume=0.015), mk_acct(), mk_market(), cfg, CTX).code
        == RejectCode.VOLUME_INVALID
    )
    assert (
        check_entry(req(volume=2.0), mk_acct(), mk_market(), cfg, CTX).code
        == RejectCode.VOLUME_ABOVE_MAX
    )
    assert (
        check_entry(req(volume=0.05), mk_acct(), mk_market(), cfg, CTX).code
        == RejectCode.RISK_PER_TRADE
    )
    assert check_entry(req(volume=0.01), mk_acct(), mk_market(), cfg, CTX).ok


def test_margin_checks():
    cfg = mk_cfg(max_volume_lots=1.0, risk_per_trade_pct=2.0)
    v = check_entry(req(), mk_acct(margin_free=5.0), mk_market(margin_per_lot=800.0), cfg, CTX)
    assert v.code == RejectCode.MARGIN_INSUFFICIENT
    # used margin 4000 (level 125%) + 32 required → projected ≈ 124% < 300%
    v = check_entry(req(), mk_acct(margin_level=125.0), mk_market(margin_per_lot=800.0), cfg, CTX)
    assert v.code == RejectCode.MARGIN_LEVEL


def test_micro_live_profile_accepts_oversized_risk_with_note():
    cfg = parse_config(
        {
            "owner": {
                "profile": "micro_live",
                "risk": {
                    "risk_per_trade_pct": 30,
                    "daily_loss_pct": 40,
                    "weekly_loss_pct": 60,
                    "max_drawdown_pct": 60,
                    "max_volume_lots": 0.01,
                    "stop_atr_min": 0.2,
                    "stop_atr_max": 0.6,
                    "min_equity_to_enter_usd": 30,
                },
            }
        }
    )
    acct = mk_acct(
        equity=50.0,
        balance=50.0,
        margin_free=50.0,
        equity_peak=50.0,
        start_equity_day=50.0,
        start_equity_week=50.0,
        live_start_equity=50.0,
    )
    m = mk_market(atr=40.0, margin_per_lot=800.0)
    v = check_entry(
        req(stop=3988.0, target=4012.4), acct, m, cfg, CTX
    )  # 12.2 USD stop = 24.4% of 50
    assert v.ok and v.volume == 0.01 and 24 < v.risk_pct < 25 and "D-017" in v.notes[0]
    v2 = check_entry(req(stop=3980.0, target=4020.4), acct, m, cfg, CTX)  # 20.2 USD = 40% > 30%
    assert v2.code == RejectCode.RISK_PER_TRADE
    acct_low = mk_acct(
        equity=25.0,
        balance=25.0,
        margin_free=25.0,
        equity_peak=50.0,
        live_start_equity=50.0,
        start_equity_day=25.0,
        start_equity_week=25.0,
    )
    assert (
        check_entry(req(stop=3988.0), acct_low, m, cfg, CTX).code == RejectCode.MIN_EQUITY
    )  # 50% dd < 60
    acct_dd = mk_acct(
        equity=19.0,
        balance=19.0,
        margin_free=19.0,
        equity_peak=50.0,
        live_start_equity=50.0,
        start_equity_day=19.0,
        start_equity_week=19.0,
    )
    assert (
        check_entry(req(stop=3988.0), acct_dd, m, cfg, CTX).code == RejectCode.DRAWDOWN
    )  # 62% dd from live start
    acct_mid = mk_acct(
        equity=28.0,
        balance=28.0,
        margin_free=28.0,
        equity_peak=50.0,
        live_start_equity=50.0,
        start_equity_day=28.0,
        start_equity_week=28.0,
    )
    assert check_entry(req(stop=3988.0), acct_mid, m, cfg, CTX).code == RejectCode.MIN_EQUITY


def test_stop_move_rules():
    m = mk_market()
    ok = check_stop_move(StopMoveRequest(1, "long", 3990.0, 3980.0, 3995.0), m, CTX)
    assert ok.ok
    assert (
        check_stop_move(StopMoveRequest(1, "long", 3975.0, 3980.0, 3995.0), m, CTX).code
        == RejectCode.STOP_MOVE_NOT_TOWARD_PROFIT
    )
    assert (
        check_stop_move(StopMoveRequest(1, "long", 3999.995, 3980.0, 3995.0), m, CTX).code
        == RejectCode.STOP_MOVE_ACROSS_PRICE
    )
    assert check_stop_move(StopMoveRequest(1, "short", 4010.0, 4020.0, 4005.0), m, CTX).ok
    assert (
        check_stop_move(StopMoveRequest(1, "short", 4025.0, 4020.0, 4005.0), m, CTX).code
        == RejectCode.STOP_MOVE_NOT_TOWARD_PROFIT
    )
    frozen = mk_market(freeze_level_points=500)
    assert (
        check_stop_move(StopMoveRequest(1, "long", 3999.0, 3998.0, 3995.0), frozen, CTX).code
        == RejectCode.STOP_FROZEN
    )
    assert (
        check_stop_move(
            StopMoveRequest(1, "long", 3990.0, 3980.0, 3995.0), m, RiskContext(owner_hash_ok=False)
        ).code
        == RejectCode.OWNER_HASH_MISMATCH
    )


def test_close_is_allowed_even_when_killed_or_close_only():
    assert check_close(True, mk_market(trade_mode=3), RiskContext(kill_switch=True)).ok
    assert check_close(False, mk_market(), CTX).code == RejectCode.POSITION_UNKNOWN
    assert check_close(True, mk_market(trade_mode=0), CTX).code == RejectCode.NOT_FULL_TRADE_MODE


def test_property_no_approved_order_breaks_a_limit():
    """D-032: 20,000 random configs/accounts/markets/requests. Every approval respects every limit."""
    rng = np.random.default_rng(2026)
    sessions = list(SessionState)
    regimes = ["normal", "normal", "wide", "unknown"]
    approved = 0
    for i in range(20_000):
        profile = "micro_live" if rng.random() < 0.3 else "normal"
        if profile == "normal":
            risk = dict(
                risk_per_trade_pct=float(rng.uniform(0.1, 2.0)),
                daily_loss_pct=float(rng.uniform(0.5, 5)),
                weekly_loss_pct=float(rng.uniform(5, 10)),
                max_drawdown_pct=float(rng.uniform(10, 20)),
                max_positions=int(rng.integers(1, 4)),
                max_volume_lots=float(rng.choice([0.01, 0.05, 0.1, 1.0])),
                max_spread_pips=float(rng.uniform(10, 100)),
                max_orders_per_minute=int(rng.integers(1, 20)),
                min_reward_risk=float(rng.uniform(0.5, 2.0)),
                stop_atr_min=0.2,
                stop_atr_max=float(rng.uniform(0.5, 3.0)),
                cooldown_min=60,
                min_margin_level_pct=float(rng.uniform(150, 500)),
            )
        else:
            risk = dict(
                risk_per_trade_pct=float(rng.uniform(1, 30)),
                daily_loss_pct=float(rng.uniform(5, 40)),
                weekly_loss_pct=float(rng.uniform(10, 60)),
                max_drawdown_pct=float(rng.uniform(20, 60)),
                max_positions=1,
                max_volume_lots=0.01,
                max_spread_pips=float(rng.uniform(10, 100)),
                max_orders_per_minute=int(rng.integers(1, 20)),
                min_reward_risk=float(rng.uniform(0.5, 2.0)),
                stop_atr_min=0.2,
                stop_atr_max=float(rng.uniform(0.5, 3.0)),
                cooldown_min=60,
                min_margin_level_pct=float(rng.uniform(150, 500)),
                min_equity_to_enter_usd=float(rng.uniform(0, 40)),
            )
        cfg = parse_config({"owner": {"profile": profile, "risk": risk}})
        equity = (
            float(rng.uniform(20, 200))
            if profile == "micro_live"
            else float(rng.uniform(500, 50_000))
        )
        peak = equity * float(rng.uniform(1.0, 1.5))
        acct = mk_acct(
            equity=equity,
            balance=equity,
            margin_free=equity * float(rng.uniform(0.0, 1.0)),
            margin_level=None if rng.random() < 0.5 else float(rng.uniform(100, 5000)),
            open_positions=int(rng.integers(0, 3)),
            day_pnl=float(rng.uniform(-0.5, 0.2) * equity),
            week_pnl=float(rng.uniform(-0.6, 0.3) * equity),
            equity_peak=peak,
            orders_last_minute=int(rng.integers(0, 25)),
            cooldown_until_ms=int(rng.choice([0, 2_000_000])),
            start_equity_day=equity,
            start_equity_week=equity,
            live_start_equity=peak,
        )
        clean = rng.random() < 0.6  # 60% mostly-valid scenarios so the approval path is exercised
        if clean:
            acct = mk_acct(
                equity=equity,
                balance=equity,
                margin_free=equity,
                margin_level=None,
                open_positions=0,
                day_pnl=0.0,
                week_pnl=0.0,
                equity_peak=equity,
                orders_last_minute=0,
                cooldown_until_ms=0,
                start_equity_day=equity,
                start_equity_week=equity,
                live_start_equity=equity,
            )
        bid = float(rng.uniform(3000, 5500))
        spread = float(rng.uniform(0.05, 0.4 if clean else 1.2))
        u = rng.random()
        atr = (
            float(rng.uniform(20, 120))
            if clean
            else (None if u < 0.05 else (0.0 if u < 0.10 else float(rng.uniform(5, 150))))
        )
        m = mk_market(
            bid=bid,
            ask=bid + spread,
            atr=atr,
            spread_pips=spread / 0.01,
            spread_regime="normal" if clean else str(rng.choice(regimes)),
            session=SessionState.OPEN if clean else sessions[int(rng.integers(0, len(sessions)))],
            entries_allowed=True if clean else bool(rng.random() < 0.8),
            tick_age_s=float(rng.uniform(0, 5)) if clean else float(rng.uniform(0, 20)),
            trade_mode=4 if clean else int(rng.choice([4, 4, 4, 3, 0, 1, 2])),
            stops_level_points=int(rng.choice([0, 0, 30])),
            margin_per_lot=None if rng.random() < 0.3 else float(rng.uniform(100, 2000)),
        )
        side = "long" if rng.random() < 0.5 else "short"
        entry = m.ask if side == "long" else m.bid
        if clean:
            dist = float(rng.uniform(risk["stop_atr_min"], risk["stop_atr_max"])) * atr
            tgt_dist = dist * float(rng.uniform(risk["min_reward_risk"], 3.0))
        else:
            dist = float(rng.uniform(0.1, 200))
            tgt_dist = float(rng.uniform(0.1, 300))
        stop = entry - dist if side == "long" else entry + dist
        target = (
            None
            if rng.random() < 0.2
            else (entry + tgt_dist if side == "long" else entry - tgt_dist)
        )
        vol = (
            None
            if (clean or rng.random() < 0.7)
            else float(rng.choice([0.01, 0.015, 0.02, 0.1, 1.0, 5.0]))
        )
        ctx = (
            CTX
            if clean
            else RiskContext(
                kill_switch=rng.random() < 0.05,
                paused=rng.random() < 0.05,
                owner_hash_ok=rng.random() > 0.03,
                spec_ok=rng.random() > 0.03,
                incident_open=rng.random() < 0.05,
                now_ms=1_000_000,
            )
        )
        v = check_entry(EntryRequest(side, stop, target, "b", "p", volume=vol), acct, m, cfg, ctx)
        if not v.ok:
            continue
        approved += 1
        r = cfg.owner.risk
        loss_at_stop = v.volume * v.stop_distance * m.contract_size
        assert loss_at_stop <= equity * r.risk_per_trade_pct / 100.0 + 1e-6, i
        assert v.volume <= r.max_volume_lots + 1e-12 and v.volume >= m.volume_min
        assert abs(v.volume / m.volume_step - round(v.volume / m.volume_step)) < 1e-6
        assert acct.open_positions < r.max_positions
        assert m.session is SessionState.OPEN and m.entries_allowed and m.spread_regime == "normal"
        assert m.spread_pips <= r.max_spread_pips and m.tick_age_s <= cfg.session.stale_tick_seconds
        assert r.stop_atr_min - 1e-9 <= v.stop_distance / m.atr <= r.stop_atr_max + 1e-9
        if side == "long":
            assert v.stop_price < m.bid and (target is None or target > m.ask)
        else:
            assert v.stop_price > m.ask and (target is None or target < m.bid)
        if v.reward_risk is not None:
            assert v.reward_risk >= r.min_reward_risk - 1e-9
        assert (
            not (ctx.kill_switch or ctx.paused or ctx.incident_open)
            and ctx.owner_hash_ok
            and ctx.spec_ok
        )
        assert acct.day_pnl > -equity * r.daily_loss_pct / 100.0
        assert acct.equity >= r.min_equity_to_enter_usd
        if m.margin_per_lot is not None:
            assert m.margin_per_lot * v.volume <= acct.margin_free
    assert approved > 2000, approved  # the generator must actually exercise the approval path

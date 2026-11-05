"""Shadow ledger (LEARNING_SYSTEM §3.3, D-047): fills mirror the backtester, filters mirror the live
path, rows are persisted and idempotent, and the module has no path to the broker."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from goldtrader.backtest.costs import CostModel
from goldtrader.backtest.engine import run_backtest
from goldtrader.backtest.strategies import TrendATR
from goldtrader.config.loader import parse_config
from goldtrader.data.features import add_features
from goldtrader.data.resample import frame_to_bars
from goldtrader.data.synthetic import generate_bars
from goldtrader.db import connect, migrate
from goldtrader.gateway.market import LiveFeatures
from goldtrader.session.state import SessionClock
from goldtrader.shadow import ShadowLedger
from goldtrader.types import Bar, SessionState, SymbolSpec

SPEC = SymbolSpec(
    name="XAUUSDm",
    digits=2,
    point=0.01,
    trade_tick_size=0.01,
    trade_tick_value=1.0,
    trade_contract_size=100.0,
    volume_min=0.01,
    volume_max=200.0,
    volume_step=0.01,
    volume_limit=0.0,
    trade_stops_level=0,
    trade_freeze_level=0,
    trade_mode=4,
    trade_exemode=2,
    filling_mode=3,
    swap_mode=1,
    swap_long=-30.0,
    swap_short=10.0,
    swap_rollover3days=3,
    margin_initial=0.0,
    margin_hedged=0.0,
    currency_base="XAU",
    currency_profit="USD",
    currency_margin="USD",
    spread=20,
    spread_float=True,
)
T0 = 1_768_262_400  # 2026-01-13 00:00 UTC (a Tuesday, winter schedule)
SLIP = 0.01  # CostModel default: 1 point
SPREAD = 0.20  # 20 pips
ATR = 2.0


def _cfg():
    return parse_config({"session": {"dst_mode": "winter"}})


def _db():
    conn = connect(None)
    migrate(conn)
    return conn


def hand_frame(rows: list[tuple], *, spread_regime: list[str] | None = None) -> pd.DataFrame:
    """Features-like frame from (bid_open, bid_high, bid_low, bid_close, trend_state) tuples."""
    n = len(rows)
    bo = np.array([r[0] for r in rows], float)
    bh = np.array([r[1] for r in rows], float)
    bl = np.array([r[2] for r in rows], float)
    bc = np.array([r[3] for r in rows], float)
    ts = np.array([r[4] for r in rows], int)
    t = T0 + 60 * np.arange(n)
    df = pd.DataFrame(
        {
            "time": t,
            "bid_open": bo,
            "bid_high": bh,
            "bid_low": bl,
            "bid_close": bc,
            "ask_open": bo + SPREAD,
            "ask_high": bh + SPREAD,
            "ask_low": bl + SPREAD,
            "ask_close": bc + SPREAD,
            "ticks": 10,
            "mid_close": bc + SPREAD / 2,
            "atr": ATR,
            "spread_pips": SPREAD / 0.01,
            "spread_regime": spread_regime or ["normal"] * n,
            "regime": "normal",
            "hour_utc": (t % 86400) // 3600,
            "trend_state": ts,
        }
    )
    df.attrs["bar_seconds"] = 60
    return df


class Feeder:
    """Feeds a frame row by row through ``on_bar`` with a prefix ``frame_provider``."""

    def __init__(self, df: pd.DataFrame, conn=None, cfg=None, **kw):
        self.df = df
        self.i = -1
        self.conn = conn or _db()
        self.ledger = ShadowLedger(
            self.conn,
            cfg or _cfg(),
            SPEC,
            lambda: 1_700_000_000_000,
            frame_provider=lambda: self.df.iloc[: self.i + 1],
            **kw,
        )

    def feed(self, k: int, state="OPEN", allowed=True, features=None):
        self.i = k
        return self.ledger.on_bar(self.df.iloc[k], features, state, allowed)

    def feed_all(self, upto: int | None = None, **kw):
        out = []
        for k in range(upto if upto is not None else len(self.df)):
            out += self.feed(k, **kw)
        return out

    def rows(self, status=None):
        q = "SELECT * FROM shadow_trades"
        if status:
            q += f" WHERE status='{status}'"
        return [dict(r) for r in self.conn.execute(q + " ORDER BY entry_ms")]


# bars 0-1 flat, bar 2 flips to +1 (signal), bar 3 fills at open, bar 4 is "the next bar"
LONG_SETUP = [
    (4000.0, 4000.5, 3999.5, 4000.2, 0),
    (4000.2, 4000.6, 3999.8, 4000.3, 0),
    (4000.3, 4000.8, 4000.0, 4000.6, 1),
    (4000.0, 4001.0, 3999.5, 4000.4, 1),  # fill bar: ask_open 4000.20 -> entry 4000.21
]


def test_long_entry_at_next_bar_ask_plus_slippage():
    f = Feeder(hand_frame(LONG_SETUP))
    f.ledger.register("exp", "b1", "trend_atr", {"stop_atr": 1.0, "target_atr": 1.5})
    ev = f.feed_all()
    kinds = [e["event"] for e in ev]
    assert kinds == ["signal", "opened"]
    (row,) = f.rows()
    assert row["status"] == "open" and row["side"] == "long" and row["volume"] == 0.01
    assert row["entry_price"] == pytest.approx(4000.0 + SPREAD + SLIP)
    assert row["entry_ms"] == (T0 + 3 * 60) * 1000 and row["signal_ms"] == (T0 + 2 * 60) * 1000
    assert row["sl"] == pytest.approx(round(4000.21 - 1.0 * ATR, 2))
    assert row["tp"] == pytest.approx(round(4000.21 + 1.5 * ATR, 2))
    # entry costs vs mid: half spread + slippage, at 0.01 lot x 100 oz
    assert row["spread_cost"] == pytest.approx((SPREAD / 2 + SLIP) * 0.01 * 100)
    assert row["slippage_cost"] == pytest.approx(SLIP * 0.01 * 100)
    assert f.ledger.open_positions()[0]["shadow_id"] == "b1:" + str(T0 + 3 * 60)


def test_short_entry_at_next_bar_bid_minus_slippage():
    rows = [(a, b, c, d, -s) for a, b, c, d, s in LONG_SETUP]
    f = Feeder(hand_frame(rows))
    f.ledger.register("exp", "b1", "trend_atr", {})
    f.feed_all()
    (row,) = f.rows()
    assert row["side"] == "short"
    assert row["entry_price"] == pytest.approx(4000.0 - SLIP)
    assert row["sl"] > row["entry_price"] > row["tp"]


def test_stop_first_on_ambiguous_bar_and_gap_fill():
    # bar 4: both the stop (3998.21) and the target (4003.21) are inside the bar's range
    rows = LONG_SETUP + [(4000.5, 4005.0, 3997.0, 4004.0, 1)]
    f = Feeder(hand_frame(rows))
    f.ledger.register("exp", "b1", "trend_atr", {})
    ev = f.feed_all()
    assert ev[-1]["event"] == "closed" and ev[-1]["reason"] == "stop"
    (row,) = f.rows("closed")
    sl = row["sl"]
    assert row["exit_price"] == pytest.approx(sl - SLIP)  # stop level, then slippage
    assert row["net_pnl"] == pytest.approx((sl - SLIP - row["entry_price"]) * 0.01 * 100)
    assert row["r_multiple"] == pytest.approx(row["net_pnl"] / (ATR * 0.01 * 100))
    assert row["slippage_cost"] == pytest.approx(2 * SLIP * 0.01 * 100)
    assert row["exit_ms"] == (T0 + 4 * 60) * 1000
    # gap through the stop: filled at the worse of level and open
    rows = LONG_SETUP + [(3990.0, 3991.0, 3989.0, 3990.5, 1)]
    f = Feeder(hand_frame(rows))
    f.ledger.register("exp", "b1", "trend_atr", {})
    f.feed_all()
    (row,) = f.rows("closed")
    assert row["exit_reason"] == "stop" and row["exit_price"] == pytest.approx(3990.0 - SLIP)


def test_target_fills_at_level():
    rows = LONG_SETUP + [(4001.0, 4004.0, 4000.5, 4003.5, 1)]
    f = Feeder(hand_frame(rows))
    f.ledger.register("exp", "b1", "trend_atr", {})
    f.feed_all()
    (row,) = f.rows("closed")
    assert row["exit_reason"] == "target" and row["exit_price"] == pytest.approx(row["tp"])
    assert row["net_pnl"] > 0 and row["r_multiple"] == pytest.approx(
        (row["tp"] - row["entry_price"]) / ATR
    )


@pytest.mark.parametrize("state", ["PRE_BREAK_BUFFER", "CLOSE_ONLY", "CLOSED", "STALE"])
def test_flatten_before_the_break(state):
    rows = LONG_SETUP + [(4000.8, 4001.2, 4000.4, 4001.0, 1)]
    f = Feeder(hand_frame(rows))
    f.ledger.register("exp", "b1", "trend_atr", {})
    f.feed_all(upto=4)
    ev = f.feed(4, state=state, allowed=False)
    assert ev[-1]["event"] == "closed" and ev[-1]["reason"] == "session_flatten"
    (row,) = f.rows("closed")
    assert row["exit_price"] == pytest.approx(4000.8 - SLIP)  # long exits at bid open - slip
    assert f.ledger.open_positions() == []


@pytest.mark.parametrize(
    "state,allowed,features,frame_regime,expected",
    [
        ("NEWS_WINDOW", False, None, "normal", "session_news_window"),
        ("FIX_WINDOW", False, None, "normal", "session_fix_window"),
        ("OPEN", False, None, "normal", "session_open"),  # trade_only_hours: no entries
        ("OPEN", True, None, "wide", "spread"),
        ("OPEN", True, None, "unknown", "spread"),
        ("OPEN", True, "wide", "normal", "spread_live"),
        ("OPEN", True, "pips", "normal", "spread_live"),
    ],
)
def test_no_entry_when_live_filters_block(state, allowed, features, frame_regime, expected):
    regimes = ["normal"] * 3 + [frame_regime]
    f = Feeder(hand_frame(LONG_SETUP, spread_regime=regimes))
    f.ledger.register("exp", "b1", "trend_atr", {})
    f.feed_all(upto=3)
    live = None
    if features == "wide":
        live = LiveFeatures(T0, ATR, 0.0005, 20.0, "wide", "normal", 1, None, None, 0)
    elif features == "pips":
        live = LiveFeatures(T0, ATR, 0.0005, 61.0, "normal", "normal", 1, None, None, 0)
    ev = f.feed(3, state=state, allowed=allowed, features=live)
    assert ev == [{"bundle_id": "b1", "event": "skipped", "reason": expected, "bar_s": T0 + 180}]
    assert f.rows() == [] and f.ledger.open_positions() == []


def test_spread_cap_from_owner_risk_blocks():
    df = hand_frame(LONG_SETUP)
    df.loc[3, "spread_pips"] = 61.0  # > max_spread_pips 60
    f = Feeder(df)
    f.ledger.register("exp", "b1", "trend_atr", {})
    ev = f.feed_all()
    assert ev[-1]["reason"] == "spread" and f.rows() == []


def test_skipped_signal_is_not_carried_to_a_later_bar():
    rows = LONG_SETUP + [(4000.4, 4000.9, 4000.1, 4000.6, 1)]
    f = Feeder(hand_frame(rows))
    f.ledger.register("exp", "b1", "trend_atr", {})
    f.feed_all(upto=3)
    f.feed(3, state="NEWS_WINDOW", allowed=False)
    ev = f.feed(4)  # OPEN again, but the flip signal was at bar 2 only
    assert ev == [] and f.rows() == []


def test_one_position_per_bundle_and_signal_ignored_while_open():
    # bar 4 flips to -1 while long is open: no second row, no reversal
    rows = LONG_SETUP + [(4000.4, 4000.9, 4000.1, 4000.6, -1), (4000.5, 4000.9, 4000.2, 4000.6, -1)]
    f = Feeder(hand_frame(rows))
    f.ledger.register("exp", "b1", "trend_atr", {})
    ev = f.feed_all()
    assert [e["event"] for e in ev] == ["signal", "opened"]
    assert len(f.rows()) == 1 and f.rows()[0]["status"] == "open"


def test_idempotent_on_replayed_and_older_bars():
    rows = LONG_SETUP + [(4000.5, 4005.0, 3997.0, 4004.0, 1)]
    f = Feeder(hand_frame(rows))
    f.ledger.register("exp", "b1", "trend_atr", {})
    f.feed_all()
    before = f.rows()
    assert len(before) == 1 and before[0]["status"] == "closed"
    # replay every bar again, then an older bar out of order
    assert f.feed_all() == []
    assert f.feed(2) == []
    assert f.rows() == before
    # a fresh ledger on the same database continues from the last bar it journaled
    g = ShadowLedger(f.conn, _cfg(), SPEC, lambda: 0, frame_provider=lambda: f.df.iloc[: f.i + 1])
    g.register("exp", "b1", "trend_atr", {})
    assert [g.on_bar(f.df.iloc[k], None, "OPEN", True) for k in range(len(rows))] == [[]] * 5
    assert f.rows() == before


def test_open_position_is_restored_after_restart():
    f = Feeder(hand_frame(LONG_SETUP + [(4000.5, 4005.0, 3997.0, 4004.0, 1)]))
    f.ledger.register("exp", "b1", "trend_atr", {})
    f.feed_all(upto=4)
    assert f.rows()[0]["status"] == "open"
    g = Feeder(f.df, conn=f.conn)
    g.ledger.register("exp", "b1", "trend_atr", {})
    assert g.ledger.summary("b1")["open"] is True
    ev = g.feed(4)
    assert ev[-1]["event"] == "closed" and ev[-1]["reason"] == "stop"
    (row,) = g.rows()
    assert row["status"] == "closed" and row["r_multiple"] == pytest.approx(-1.005, abs=0.01)


def test_register_is_idempotent_and_unregister_stops_simulation():
    f = Feeder(hand_frame(LONG_SETUP))
    f.ledger.register("exp", "b1", "trend_atr", {})
    f.ledger.register("exp", "b1", "trend_atr", {"stop_atr": 0.5})  # ignored: already registered
    f.ledger.register("exp", "b2", "flat", {})
    assert f.ledger.bundles == ["b1", "b2"]
    f.ledger.unregister("b2")
    f.feed_all()
    assert {r["bundle_id"] for r in f.rows()} == {"b1"}
    with pytest.raises(ValueError):
        f.ledger.register("exp", "b3", "martingale", {})


def test_accepts_bar_objects_dicts_and_series():
    df = hand_frame(LONG_SETUP)
    f = Feeder(df)
    f.ledger.register("exp", "b1", "trend_atr", {})
    f.i = 0
    f.ledger.on_bar(dict(df.iloc[0]), None, SessionState.OPEN, True)
    f.i = 1
    r = df.iloc[1]
    prices = {k: float(r[k]) for k in ("bid_open", "bid_high", "bid_low", "bid_close")}
    prices |= {k: float(r[k]) for k in ("ask_open", "ask_high", "ask_low", "ask_close")}
    f.ledger.on_bar(Bar(time=int(r["time"]), seconds=60, ticks=10, **prices), None, "OPEN", True)
    f.feed(2)
    f.feed(3)
    assert len(f.rows("open")) == 1
    # programmer errors: a provider frame whose last row is not the bar, or lacking columns
    f.ledger.register("exp", "fresh", "flat", {})
    f.i = 2
    with pytest.raises(ValueError, match="last row"):
        f.ledger.on_bar(df.iloc[3], None, "OPEN", True)
    f.ledger.frame_provider = lambda: df.drop(columns=["atr"]).iloc[:4]
    with pytest.raises(ValueError, match="missing columns"):
        f.ledger.on_bar(df.iloc[3], None, "OPEN", True)
    with pytest.raises(ValueError, match="'time'"):
        f.ledger.on_bar({"bid_open": 1.0}, None, "OPEN", True)


def test_spec_like_object_is_enough():
    spec = SimpleNamespace(point=0.01, digits=2, trade_contract_size=100.0, volume_min=0.01)
    f = Feeder(hand_frame(LONG_SETUP))
    f.ledger.spec = spec
    f.ledger.register("exp", "b1", "trend_atr", {})
    f.feed_all()
    assert f.rows()[0]["entry_price"] == pytest.approx(4000.21)


# --- against the backtester ----------------------------------------------------------------


@pytest.fixture(scope="module")
def synthetic():
    df = add_features(generate_bars(datetime(2026, 1, 12, tzinfo=UTC), 2, seed=7, seconds=60))
    cfg = _cfg()
    bt = run_backtest(df, TrendATR(), cfg, SPEC, equity0=5000.0)
    return df, cfg, bt


def _states(cfg, bars):
    clock = SessionClock(cfg.session)
    out = []
    for b in bars:
        d = clock.state_at(datetime.fromtimestamp(b.time, UTC))
        out.append((d.state, d.entries_allowed))
    return out


def test_pnl_matches_backtester_on_the_same_bars(synthetic):
    """Same frame, same fill model: the shadow ledger reproduces run_backtest trade for trade.

    Accepted difference: the engine force-closes an open position at the end of the data
    (``end_of_data``); the ledger keeps it open until a later bar closes it, so those rows are
    excluded. The default config sizes to the 0.01-lot cap, i.e. ``volume_min``, like the ledger.
    """
    df, cfg, bt = synthetic
    bars = frame_to_bars(df)
    f = Feeder(df, cfg=cfg)
    f.ledger.register("exp", "cand", "trend_atr", {})
    for k, (b, (state, allowed)) in enumerate(zip(bars, _states(cfg, bars))):
        f.i = k
        f.ledger.on_bar(b, None, state, allowed)
    shadow = pd.DataFrame(f.ledger.closed("cand"))
    engine = bt.trades[bt.trades.exit_reason != "end_of_data"].reset_index(drop=True)
    assert bt.n_trades > 20 and len(shadow) == len(engine)
    assert (bt.rejected.get("spread", 0) > 0) and bt.rejected.get("cooldown", 0) > 0  # filters hit
    np.testing.assert_allclose(shadow.entry_price, engine.entry_price, atol=1e-9)
    np.testing.assert_allclose(shadow.exit_price, engine.exit_price, atol=1e-9)
    np.testing.assert_allclose(shadow.net_pnl, engine.net_pnl, atol=1e-9)
    np.testing.assert_allclose(shadow.r_multiple, engine.r_multiple, atol=1e-9)
    assert list(shadow.exit_reason) == list(engine.exit_reason)
    assert list(shadow.entry_ms // 1000) == list(engine.entry_time)
    assert shadow.net_pnl.sum() == pytest.approx(engine.net_pnl.sum(), abs=1e-6)
    # signal-time to order-time drift is exactly one bar (LEARNING_SYSTEM §3.3)
    assert set(shadow.entry_ms - shadow.signal_ms) == {60_000}
    # the ledger's slippage equals the cost model's by construction -> ratio 1.0
    assert f.ledger.slippage_ratio("cand", CostModel().slippage_points) == pytest.approx(1.0)
    assert f.ledger.slippage_ratio("cand", 2.0) == pytest.approx(0.5)
    assert f.ledger.slippage_ratio("nobody", 1.0) is None
    s = f.ledger.summary("cand")
    assert s["n_closed"] == len(shadow) and s["trading_days"] >= 1


def test_own_trailing_frame_matches_frame_provider(synthetic):
    """Without a frame_provider the ledger recomputes features on its own bars; causal features
    make the prefix identical, so the trades match the provider mode on the same window."""
    df, cfg, bt = synthetic
    bars = frame_to_bars(df)
    states = _states(cfg, bars)
    # a window around one engine trade, starting flat (the previous trade exited earlier)
    idx = {int(t): i for i, t in enumerate(df["time"])}
    ent = [idx[int(t)] for t in bt.trades.entry_time]
    ex = [idx[int(t)] for t in bt.trades.exit_time]
    j = next(j for j in range(1, len(ent)) if ent[j] >= 600 and ex[j - 1] <= ent[j] - 4)
    start, n = ent[j] - 3, min(ex[j] - ent[j] + 6, 150)
    a = Feeder(df, cfg=cfg)
    a.ledger.register("exp", "cand", "trend_atr", {})
    conn_b = _db()
    own = ShadowLedger(conn_b, cfg, SPEC, lambda: 0, frame_bars=len(bars))
    own.preload(bars[:start])
    own.register("exp", "cand", "trend_atr", {})
    for k in range(start, start + n):
        a.i = k
        a.ledger.on_bar(bars[k], None, *states[k])
        own.on_bar(bars[k], None, *states[k])
    ra = [dict(r) for r in a.conn.execute("SELECT * FROM shadow_trades ORDER BY entry_ms")]
    rb = [dict(r) for r in conn_b.execute("SELECT * FROM shadow_trades ORDER BY entry_ms")]
    assert ra and ra == rb


# --- safety ----------------------------------------------------------------------------------


def test_shadow_module_has_no_path_to_the_broker():
    """D-047: the shadow stage never sends orders — no broker, execution or gateway imports."""
    src = Path(__file__).resolve().parents[1] / "src" / "goldtrader" / "shadow"
    forbidden = ("goldtrader.mt5", "goldtrader.execution", "goldtrader.gateway")
    for path in sorted(src.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)) and not _type_checking_only(
                tree, node
            ):
                if isinstance(node, ast.Import):
                    names.update(a.name for a in node.names)
                elif node.module:
                    names.add(node.module)
        bad = [m for m in names if m.startswith(forbidden)]
        assert not bad, f"{path.name} imports {bad}"
        text = path.read_text(encoding="utf-8")
        assert "order_send" not in text and "send_intent" not in text
    # and nothing reaches them transitively in a fresh interpreter
    code = (
        "import json, sys\n"
        "import goldtrader.shadow\n"
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('goldtrader'))))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(src.parents[1]),
    )
    loaded = json.loads(out.stdout.strip().splitlines()[-1])
    bad = [m for m in loaded if m.startswith(forbidden)]
    assert not bad, f"transitively imported: {bad}"
    assert "goldtrader.backtest.costs" in loaded  # the ledger really uses the backtester's costs


def _type_checking_only(tree: ast.Module, node: ast.AST) -> bool:
    """True when ``node`` sits inside an ``if TYPE_CHECKING:`` block."""
    for parent in ast.walk(tree):
        if (
            isinstance(parent, ast.If)
            and isinstance(parent.test, ast.Name)
            and parent.test.id == "TYPE_CHECKING"
            and any(node is child for child in ast.walk(parent))
        ):
            return True
    return False

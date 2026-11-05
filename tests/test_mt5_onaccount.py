"""The on-account suite (MT5-T00 … T16) must run end to end against FakeMT5 in CI (D-028)."""

from __future__ import annotations

from datetime import UTC, datetime

from goldtrader.mt5 import constants as K
from goldtrader.mt5.fake import FakeMT5
from goldtrader.mt5.onaccount import (
    READ_ONLY_TESTS,
    SUITE_MAGIC,
    TRADING_TESTS,
    TestResult,
    cleanup,
    render_markdown,
    run_suite,
    t00_constants,
    t04_filling_probe,
    t06_stops_probe,
    t07_partial_close,
    t15_failure_drills,
)
from goldtrader.types import OrderRequest

SYMBOL = "XAUUSDm"
T0 = 1_700_000_000_000 - (1_700_000_000_000 % 60_000)


def make_fake(**kw) -> FakeMT5:
    fake = FakeMT5(**kw)
    # a few minutes of ticks so T14 (history depth) has bars and ticks to find
    for i in range(0, 300, 5):
        fake.set_price(4000.0 + (i % 7) * 0.1, 4000.2 + (i % 7) * 0.1, time_msc=T0 + i * 1000)
    return fake


def by_id(results: list[TestResult]) -> dict[str, TestResult]:
    return {r.id: r for r in results}


def test_suite_runs_fully_against_fake_and_all_trading_tests_pass():
    fake = make_fake()
    now = datetime.fromtimestamp(fake.tick.time_msc / 1000, UTC)
    results = run_suite(
        fake,
        SYMBOL,
        allow_trading=True,
        now=now,
        n_slippage=5,
        n_latency=5,
        commission_per_lot_per_side=0.0,
    )
    r = by_id(results)
    assert len(results) == 17 and sorted(r) == [f"MT5-T{i:02d}" for i in range(17)]
    for tid in TRADING_TESTS:
        assert r[f"MT5-{tid}"].status == "pass", r[f"MT5-{tid}"]
    for tid in ("T01", "T02", "T03", "T05", "T14", "T15"):
        assert r[f"MT5-{tid}"].status == "pass", r[f"MT5-{tid}"]
    for tid in ("T11", "T13"):
        assert r[f"MT5-{tid}"].status == "info"
    assert r["MT5-T00"].status == "skip"  # MetaTrader5 not importable on Linux
    assert set(READ_ONLY_TESTS) | set(TRADING_TESTS) == {f"T{i:02d}" for i in range(17)}
    # everything the suite opened is closed again; the ledger shows only 0.01/0.02-lot deals
    assert fake.positions_get() == [] and fake.orders_get() == []
    assert all(d.volume <= 0.02 for d in fake.deals)
    entries = [
        req for req in fake.requests if req.action == K.TRADE_ACTION_DEAL and not req.position
    ]
    assert entries and all(
        req.volume <= 0.02 for req in entries
    )  # T07's over-close is 0.05 (10038)
    # a few concrete observations
    assert r["MT5-T04"].details["observed"] == {"FOK": 10009, "IOC": 10009, "RETURN": 10030}
    assert r["MT5-T06"].details["inside_spread_retcode"] == K.TRADE_RETCODE_INVALID_STOPS
    assert r["MT5-T07"].details["overclose_retcode"] == K.TRADE_RETCODE_INVALID_CLOSE_VOLUME
    assert r["MT5-T08"].details["positions_after"] == 0
    assert r["MT5-T09"].details["place_retcode"] == K.TRADE_RETCODE_PLACED
    assert r["MT5-T10"].details["entry_slippage_points"]["n"] == 5
    assert r["MT5-T12"].details["commission_sum"] == 0.0
    assert r["MT5-T14"].details["bar_counts"]["M1"] == 5
    assert r["MT5-T16"].details["n_sends"] == 10


def test_render_markdown_produces_the_table():
    fake = make_fake()
    now = datetime.fromtimestamp(fake.tick.time_msc / 1000, UTC)
    results = run_suite(fake, SYMBOL, allow_trading=True, now=now, n_slippage=2, n_latency=2)
    md = render_markdown(results)
    lines = md.splitlines()
    assert lines[0].startswith("### ") and "| ID | Test | Status |" in lines[2]
    rows = [ln for ln in lines if ln.startswith("| MT5-T")]
    assert len(rows) == 17 and all(row.count("|") == 6 for row in rows)
    assert "| MT5-T08 | hedge + close-by | PASS |" in md
    assert "Summary: pass" in lines[-1] and "skip 1" in lines[-1]
    row = render_markdown([TestResult("X", "a|b", "info", {"k": "v|w"}, "n|m")]).splitlines()[4]
    assert row.count("\\|") == 3 and row.replace("\\|", "").count("|") == 6


def test_real_account_refuses_trading_tests_without_allow_real():
    fake = make_fake()
    fake.set_account(trade_mode=K.ACCOUNT_TRADE_MODE_REAL)
    results = run_suite(fake, SYMBOL, allow_trading=True)
    r = by_id(results)
    for tid in TRADING_TESTS:
        res = r[f"MT5-{tid}"]
        assert res.status == "skip" and "REFUSED" in res.notes and "real" in res.notes
    # only order_check pre-checks (T05, T15) reached the fake: nothing was sent
    assert fake.results == [] and fake.deals == [] and fake.positions_get() == []
    assert r["MT5-T01"].status == "pass"  # read-only tests still run


def test_real_account_runs_with_allow_real():
    fake = make_fake()
    fake.set_account(trade_mode=K.ACCOUNT_TRADE_MODE_REAL)
    now = datetime.fromtimestamp(fake.tick.time_msc / 1000, UTC)
    results = run_suite(
        fake, SYMBOL, allow_trading=True, allow_real=True, now=now, n_slippage=1, n_latency=1
    )
    r = by_id(results)
    assert all(r[f"MT5-{t}"].status == "pass" for t in TRADING_TESTS)
    assert fake.positions_get() == []


def test_allow_trading_false_skips_trading_tests_and_sends_nothing():
    fake = make_fake()
    results = run_suite(fake, SYMBOL, allow_trading=False)
    r = by_id(results)
    assert all(r[f"MT5-{t}"].status == "skip" for t in TRADING_TESTS)
    assert "allow_trading=False" in r["MT5-T04"].notes
    assert fake.results == [] and fake.deals == []  # only order_check requests from T05/T15


def test_t00_skips_without_package():
    res = t00_constants()
    try:
        import MetaTrader5  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        assert res.status == "skip"
    else:  # pragma: no cover - Windows only
        assert res.status in ("pass", "fail") and res.details["checked"] > 50


def test_t15_detects_investor_password():
    fake = make_fake()
    fake.set_account(trade_allowed=False)
    res = t15_failure_drills(fake, SYMBOL, burst=5)
    assert res.status == "pass" and res.details["investor_password_detected"] is True
    assert res.details["burst_retcodes"] == [K.TRADE_RETCODE_TRADE_DISABLED]


def test_t04_reports_mismatch_when_server_disagrees_with_bitmask():
    fake = make_fake()
    # the symbol advertises IOC but the server (scripted) rejects it
    fake.reject_next(K.TRADE_RETCODE_INVALID_FILL, "Unsupported filling mode")  # FOK rejected
    res = t04_filling_probe(fake, SYMBOL)
    assert res.status == "fail" and "FOK" in str(res.details) and fake.positions_get() == []


def test_t06_and_t07_clean_up_after_themselves_even_on_failure():
    fake = make_fake()
    fake.max_positions = 1
    res7 = t07_partial_close(fake, SYMBOL)
    assert res7.status == "pass" and fake.positions_get() == []
    res6 = t06_stops_probe(fake, SYMBOL)
    assert res6.status == "pass" and fake.positions_get() == []
    assert t07_partial_close(fake, SYMBOL, volume=0.01).status == "skip"


def test_cleanup_closes_only_suite_positions_and_orders():
    fake = make_fake()
    fake.order_send(
        OrderRequest(
            action=K.TRADE_ACTION_DEAL, symbol=SYMBOL, volume=0.01, type=K.ORDER_TYPE_BUY, magic=1
        )
    )  # someone else's position
    fake.order_send(
        OrderRequest(
            action=K.TRADE_ACTION_DEAL,
            symbol=SYMBOL,
            volume=0.01,
            type=K.ORDER_TYPE_SELL,
            magic=SUITE_MAGIC,
        )
    )
    fake.order_send(
        OrderRequest(
            action=K.TRADE_ACTION_PENDING,
            symbol=SYMBOL,
            volume=0.01,
            type=K.ORDER_TYPE_SELL_LIMIT,
            price=4100.0,
            magic=SUITE_MAGIC,
        )
    )
    summary = cleanup(fake, SYMBOL)
    assert len(summary["closed"]) == 1 and len(summary["removed"]) == 1 and not summary["failed"]
    left = fake.positions_get()
    assert len(left) == 1 and left[0].magic == 1 and fake.orders_get() == []

"""Review bot: code attribution, copy verification, lesson dedup, fallback; daily brief."""

from __future__ import annotations

from goldtrader.bots.review import (
    Brief,
    BriefSection,
    attribution,
    check_brief,
    daily_brief_spec,
    dedupe_lessons,
    normalise_lesson,
    review_spec,
)
from goldtrader.contracts import PostMortem
from goldtrader.llm.fake import FakeProvider
from goldtrader.llm.provider import ProviderError
from goldtrader.llm.runtime import run_agent
from goldtrader.types import Bar
from tests.test_bots_helpers import NOW, cfg, ctx, db

T0 = NOW // 1000 - 600  # bars start 10 minutes before NOW


def _bar(t: int, mid: float, spread: float = 0.2) -> Bar:
    return Bar(t, 60, mid - spread / 2, mid, mid - spread, mid - spread / 2, mid + spread / 2, mid + spread, mid, mid + spread / 2, 10)  # fmt: skip


BARS = [_bar(T0 + 60 * i, 4000.0 + i) for i in range(10)]  # mid rises 1.0 per minute

TRADE = {
    "trade_id": "tr1",
    "side": "long",
    "volume": 0.01,
    "signal_ms": (T0 + 120) * 1000 + 5_000,  # inside bar 2 → mid 4002
    "entry_ms": (T0 + 125) * 1000,
    "entry_price": 4002.5,  # filled 0.5 above mid at signal
    "exit_ms": (T0 + 500) * 1000,  # inside bar 8 → mid 4008
    "exit_price": 4007.8,
    "spread_cost": 0.2,
    "slippage_cost": 0.05,
    "commission": 0.0,
    "swap": 0.0,
}


def test_attribution_mid_to_mid_and_timing():
    a = attribution(TRADE, BARS)
    # signal: (4008 - 4002) * 0.01 * 100 = 6.0 ; timing: (4002 - 4002.5) * 1 = -0.5
    assert a == {"signal_pnl": 6.0, "timing_pnl": -0.5, "slippage": 0.05, "costs": 0.2}
    short = attribution({**TRADE, "side": "short"}, BARS)
    assert short["signal_pnl"] == -6.0 and short["timing_pnl"] == 0.5
    # no bars: fall back to the trade's own prices
    bare = attribution(TRADE, [])
    assert bare["signal_pnl"] == round((4007.8 - 4002.5) * 1.0, 6) and bare["timing_pnl"] == 0.0
    # dict bars with ms timestamps work too
    dicts = [
        {"time": b.time * 1000, "bid_close": b.bid_close, "ask_close": b.ask_close} for b in BARS
    ]
    assert attribution(TRADE, dicts) == a


def test_lesson_normalisation_and_dedupe():
    assert normalise_lesson("  Don't chase the FIX!! ") == "don t chase the fix"
    new = ["Don't chase the fix", "don't CHASE the fix.", "Wait for spread to normalise", "", "x"]
    out = dedupe_lessons(new, ["wait for spread to normalise"])
    assert out == ["Don't chase the fix", "x"]
    assert len(dedupe_lessons([f"lesson {i}" for i in range(5)], [])) == 3


def _pm(attr: dict, **over) -> dict:
    base = dict(
        outcome_class="correct_win",
        signal_pnl=attr["signal_pnl"],
        timing_pnl=attr["timing_pnl"],
        slippage=attr["slippage"],
        costs=attr["costs"],
        lessons=["Entry chased 0.5 above mid; wait for a pullback", "Wait for spread to normalise"],
        confidence=0.7,
    )
    base.update(over)
    return base


def test_review_verifies_copied_numbers_then_dedupes_lessons():
    conn, store = db()
    fake = FakeProvider()
    attr = attribution(TRADE, BARS)
    c = ctx(trade=TRADE, attribution=attr, lessons=["wait for spread to normalise."])
    fake.script("standard", [_pm(attr, signal_pnl=5.9), _pm(attr)])  # wrong copy, then right
    run = run_agent(review_spec(cfg()), c, fake, store, now_ms=NOW)
    assert run.status == "ok" and run.turns == 2
    repair = fake.requests[1].messages[-1]["content"]
    assert "signal_pnl must be copied exactly" in repair and "6.0" in repair
    assert run.finalized.lessons == ["Entry chased 0.5 above mid; wait for a pullback"]
    text = fake.requests[0].messages[0]["content"]
    assert "- signal_pnl = 6.0" in text and "trade_id=tr1" in text


def test_review_fallback_unreviewed_with_code_numbers():
    conn, store = db()
    fake = FakeProvider()
    fake.script("standard", [ProviderError("down")])
    attr = attribution(TRADE, BARS)
    run = run_agent(review_spec(cfg()), ctx(trade=TRADE, attribution=attr), fake, store, now_ms=NOW)
    assert run.status == "degraded"
    assert isinstance(run.output, PostMortem) and run.output.outcome_class == "unreviewed"
    assert run.output.signal_pnl == 6.0 and run.output.lessons == []


def test_daily_brief_schema_checks_and_fallback():
    assert check_brief(Brief(sections=[BriefSection(key="a", text="x")])) == []
    dup = Brief(sections=[BriefSection(key="a", text="x"), BriefSection(key="A", text="y")])
    assert check_brief(dup) == ["section keys must be unique"]
    conn, store = db()
    fake = FakeProvider()
    spec = daily_brief_spec(cfg())
    assert spec.tools == [] and spec.tier == "standard"
    good = {
        "sections": [{"key": "pnl", "text": "Day P&L +12.30 USD <b>vs</b> limit -200."}],
        "questions": ["Keep the 0.01 lot size?"],
    }
    fake.script("standard", [good])
    run = run_agent(
        spec, ctx(brief_numbers={"day_pnl": 12.3, "trades": 2}), fake, store, now_ms=NOW
    )
    assert run.status == "ok"
    assert run.finalized.sections_dict() == {"pnl": "Day P&L +12.30 USD vs limit -200."}
    assert "day_pnl=12.3" in fake.requests[0].messages[0]["content"]
    fake.script("standard", [ProviderError("down")])
    run = run_agent(spec, ctx(brief_numbers={}), fake, store, now_ms=NOW)
    assert run.status == "degraded" and run.output.sections[0].text.startswith("degraded")

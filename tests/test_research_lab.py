"""Research lab (D-085): the rule language, the gates, the notebook and one session."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import numpy as np
import pytest

from goldtrader.backtest.engine import run_backtest
from goldtrader.config.loader import parse_config
from goldtrader.data.features import add_features
from goldtrader.data.synthetic import generate_bars
from goldtrader.db import connect, migrate
from goldtrader.research import lab, rules, runner
from tests.test_backtest_engine import SPEC

GOOD = {
    "entry": {"type": "pullback", "minutes": 5, "trend_minutes": 120},
    "filters": [
        {"type": "htf_trend", "minutes": 60, "fast": 8, "slow": 21, "mode": "with"},
        {"type": "hours", "from": 7, "to": 17},
    ],
    "exit": {"stop_atr": 4, "target_atr": 8, "trail_atr": 1.5, "trail_after_r": 2},
}


def test_rule_language_accepts_only_its_blocks_and_ranges():
    canon = rules.validate(GOOD)
    assert canon["filters"][0]["type"] == "hours"  # filters are put in one fixed order
    assert rules.key(canon) == rules.key(rules.validate(json.loads(json.dumps(canon))))
    assert rules.validate(GOOD)["exit"]["stop_atr"] == 4.0
    text = rules.describe(canon)
    assert "pullback on 5-minute bars" in text and "with the 60-minute trend" in text
    assert "trailing stop 1.5 x ATR after 2 R" in text

    def bad(change, match):
        spec = json.loads(json.dumps(GOOD))
        change(spec)
        with pytest.raises(rules.RuleError, match=match):
            rules.validate(spec, 6.0)

    bad(lambda s: s["entry"].update(type="buy_now"), "entry.type")
    bad(lambda s: s["entry"].update(code="import os"), "does not take|settings")
    bad(lambda s: s["entry"].update(minutes=1), "between")
    bad(lambda s: s["exit"].update(stop_atr=8), "owner's limit")
    bad(lambda s: s["exit"].update(target_atr=2), "at least the stop")
    bad(lambda s: s["exit"].update(leverage=10), "does not take")
    bad(
        lambda s: s.update(exit={"stop_atr": 4, "target_atr": 8, "use_target": False}),
        "trailing stop",
    )
    bad(lambda s: s["filters"].append({"type": "peek_ahead", "bars": 5}), "filter.type")
    bad(lambda s: s["filters"].append(dict(s["filters"][0])), "twice")
    bad(lambda s: s["filters"][1].update({"from": 20, "to": 8}), "below")
    bad(lambda s: s.update(filters=[{"type": "side", "only": "both"}]), "long or short")
    bad(lambda s: s.pop("exit"), "exit block")
    assert set(rules.grammar()["entry"]) == set(rules.ENTRIES)


def _frame(days: int = 16, seed: int = 3):
    start = datetime(2026, 9, 1, tzinfo=UTC)
    return add_features(generate_bars(start, days, seed=seed, seconds=60))


def test_filters_only_remove_signals_and_never_look_ahead():
    df = _frame()
    plain = rules.validate({"entry": {"type": "trend_flip"}, "filters": [], "exit": GOOD["exit"]})
    base = rules.Composed(plain).signals(df).side
    assert (base != 0).sum() > 20
    for f in (
        {"type": "side", "only": "short"},
        {"type": "hours", "from": 7, "to": 17},
        {"type": "vol", "min_pctile": 0.3, "max_pctile": 0.9},
        {"type": "weekday", "days": [1, 2]},
        {"type": "htf_trend", "minutes": 60, "fast": 3, "slow": 10, "mode": "with"},
        {"type": "htf_trend", "minutes": 60, "fast": 3, "slow": 10, "mode": "against"},
        {"type": "range_pos", "lookback_minutes": 240, "max": 0.8},
    ):
        spec = rules.validate({**plain, "filters": [f]})
        got = rules.Composed(spec).signals(rules.prepare(df, spec)).side
        changed = got != base
        assert (got[changed] == 0).all(), f  # a filter may only drop a signal
        assert 0 < (got != 0).sum() < (base != 0).sum(), f
        # causal: the signals up to bar k are the same whether or not later bars exist
        k = len(df) - 2000
        early = rules.Composed(spec).signals(rules.prepare(df.iloc[:k].copy(), spec)).side
        assert (early == got[:k]).all(), f
    short = rules.validate({**plain, "filters": [{"type": "side", "only": "short"}]})
    assert (rules.Composed(short).signals(df).side <= 0).all()
    w = rules.validate(
        {
            **plain,
            "filters": [
                {"type": "htf_trend", "minutes": 60, "fast": 3, "slow": 10, "mode": "with"}
            ],
        }
    )
    a = rules.validate(
        {
            **plain,
            "filters": [
                {"type": "htf_trend", "minutes": 60, "fast": 3, "slow": 10, "mode": "against"}
            ],
        }
    )
    sw, sa = (rules.Composed(x).signals(rules.prepare(df, x)).side for x in (w, a))
    assert not ((sw != 0) & (sa != 0)).any()  # with and against never keep the same signal


def test_compiled_rule_runs_in_the_backtester_with_its_exits():
    df = _frame()
    cfg = parse_config({"owner": {"risk": {"stop_atr_max": 6.0, "risk_per_trade_pct": 2.0}}})
    spec = rules.validate({"entry": {"type": "trend_flip"}, "filters": [], "exit": GOOD["exit"]})
    strat, opts = rules.compile_rule(spec)
    assert opts == {"trail_atr": 1.5, "trail_after_r": 2.0}
    res = run_backtest(df, strat, cfg, SPEC, equity0=100_000, **opts)
    assert res.n_trades > 5 and set(res.trades.exit_reason) <= {
        "stop", "target", "trail", "session_flatten", "end_of_data",
    }  # fmt: skip
    assert res.trades.stop_distance.div(res.trades.atr_at_entry).round(2).eq(4.0).all()


class _FakeLab(lab.Lab):
    """The gates with a scripted backtest, so each gate's rule can be checked exactly."""

    def __init__(self, script):
        self.script = script
        self.stop_max = 6.0
        self.full = _frame(4)
        t = self.full["time"]
        a, b = int(t.iloc[0]), int(t.iloc[-1]) + 1
        self.cuts = [a + (b - a) // 4, a + (b - a) // 2, a + 3 * (b - a) // 4, b - 600]
        self.calls = []
        self.discovery = (t >= self.cuts[0]) & (t < self.cuts[3])

    def _run(self, df, strategy, opts, spread=1.0):
        t0 = int(df["time"].iloc[0]) if len(df) else -1
        where = (
            "blind" if (t0 < self.cuts[0] or t0 >= self.cuts[3])
            else "disc" if int(df["time"].iloc[-1]) >= self.cuts[2] and t0 < self.cuts[1]
            else "ABC"[[self.cuts[0], self.cuts[1], self.cuts[2]].index(min(self.cuts[:3], key=lambda c: abs(c - t0)))]
        )  # fmt: skip
        kind = "random" if isinstance(strategy, lab.RandomEntries) else "rule"
        self.calls.append((where, kind, spread, getattr(strategy, "spec", {}).get("exit")))
        mean, n = self.script(where, kind, spread, getattr(strategy, "spec", {}).get("exit"))
        rng = np.random.default_rng(len(self.calls))
        rs = list(rng.normal(mean, 0.4, n)) if n else []
        rs = [r - (np.mean(rs) - mean) for r in rs] if rs else []
        return {
            "n": n,
            "mean_r": mean,
            "net": round(mean * n * 10, 2),
            "win": 0.4,
            "rs": rs,
            "longs": 0.5,
        }


SPEC_OK = rules.validate(
    {"entry": {"type": "trend_flip"}, "filters": [], "exit": {"stop_atr": 4, "target_atr": 8}}
)


def test_gates_stop_at_the_first_failure_in_order():
    def gate(script, **kw):
        return _FakeLab(script).evaluate(SPEC_OK, **kw)

    r = gate(lambda w, k, s, e: (0.2, 20))
    assert (r["gate"], r["failed_at"]) == (0, "trades") and "needs 100" in r["why"]
    r = gate(lambda w, k, s, e: (-0.05 if w == "B" else 0.2, 60))
    assert (r["gate"], r["failed_at"]) == (1, "periods") and "period B" in r["why"]
    r = gate(lambda w, k, s, e: (0.2, 60), stop_after=2)
    assert (r["gate"], r["status"]) == (2, "pending")
    r = gate(lambda w, k, s, e: (-0.1 if s > 1 else 0.2, 60))
    assert (r["gate"], r["failed_at"]) == (2, "robust") and "spread x1.5" in r["why"]
    r = gate(lambda w, k, s, e: (-0.1 if e and e["stop_atr"] > 4 else 0.2, 60))
    assert r["failed_at"] == "robust" and "stop x1.2" in r["why"]
    r = gate(lambda w, k, s, e: (0.3 if k == "random" else 0.2, 60))
    assert (r["gate"], r["failed_at"]) == (3, "luck") and "random entries" in r["why"]
    # a thin edge is not credible once thousands of rules have been tried, a fat one is
    thin = gate(lambda w, k, s, e: (0.0 if k == "random" else 0.04, 60), n_trials=5000)
    assert thin["failed_at"] == "luck" and "not credible" in thin["why"]
    fat = gate(
        lambda w, k, s, e: (0.0 if k == "random" else 0.5, 200 if w != "blind" else 5), n_trials=5
    )
    assert (fat["gate"], fat["status"]) == (4, "awaiting_blind") and fat["luck"][
        "beats_random"
    ] == 20
    lost = gate(
        lambda w, k, s, e: (-0.2 if w == "blind" else 0.0 if k == "random" else 0.5, 200),
        n_trials=5,
    )
    assert (lost["gate"], lost["failed_at"]) == (4, "blind")
    won = gate(lambda w, k, s, e: (0.0 if k == "random" else 0.5, 200), n_trials=5)
    assert (won["gate"], won["status"], won["failed_at"]) == (5, "passed", None)


def test_notebook_refuses_bad_and_repeated_rules_and_a_session_records_everything():
    conn = connect(None)
    migrate(conn)
    clock = [1000]

    def now():
        clock[0] += 1
        return clock[0]

    assert lab.propose(conn, {"entry": {"type": "nope"}}, source="agent", now_ms=1)[0] is None
    iid, why = lab.propose(conn, SPEC_OK, source="owner", rationale="x" * 900, now_ms=1)
    assert iid == 1 and why == ""
    assert lab.propose(conn, SPEC_OK, source="agent", now_ms=2) == (None, "already in the notebook")
    assert len(lab.notebook(conn)[0]["rationale"]) == 600

    seen = {}

    def propose_fn(ctx):
        seen.update(ctx)
        ideas = [
            {"rule": GOOD, "rationale": "trend days continue after a pause", "expectation": "works in trends"},
            {"rule": {"entry": {"type": "trend_flip"}, "exit": {"stop_atr": 99, "target_atr": 99}},
             "rationale": "too wide", "expectation": "none"},
        ]  # fmt: skip
        return ideas, [{"id": 1, "lesson": "not tested yet, so this must be ignored"}], 0.12

    # a scripted lab: the agent's rule holds everywhere, everything else loses in period B
    good_key = rules.key(rules.validate(GOOD, 6.0))

    class Scripted(_FakeLab):
        def evaluate(self, spec, **kw):
            self.script = (
                (lambda w, k, s, e: (0.0 if k == "random" else 0.5, 200))
                if rules.key(spec) == good_key
                else (lambda w, k, s, e: (-0.1 if w == "B" else 0.2, 60))
            )
            return super().evaluate(spec, **kw)

    L = Scripted(None)
    out = runner.run_session(conn, L, now_ms=now, n_ideas=6, propose_fn=propose_fn, seed=7)
    assert out["proposed"] == 6 and out["tested"] == 7  # 1 agent + 5 by code, plus the owner's
    assert out["passed_gate2"] == 1 and out["full_gates_run"] == 1 and out["passed_gate4"] == 1
    f = out["funnel"]
    assert f["proposed"] == 7 and f["tested"] == 7 and f["passed_blind"] == 1 and f["on_paper"] == 1
    assert "grammar" in seen and seen["stop_atr_max"] == 6.0 and "profile" in seen
    book = {n["id"]: n for n in lab.notebook(conn)}
    assert (
        book[1]["status"] == "failed"
        and book[1]["failed_at"] == "periods"
        and book[1]["lesson"] is None
    )
    winner = next(n for n in book.values() if n["status"] == "passed")
    assert winner["source"] == "agent" and winner["paper_bundle"].startswith("lab_")
    assert {n["source"] for n in book.values()} == {"owner", "agent", "optimiser"}
    paper = conn.execute("SELECT kind, params_json, active FROM paper_candidates").fetchall()
    assert len(paper) == 1 and paper[0][0] == "lab" and json.loads(paper[0][1]) == winner["spec"]
    assert conn.execute("SELECT COUNT(*) FROM alerts WHERE kind='lab_candidate'").fetchone()[0] == 1
    run = conn.execute("SELECT status, proposed, tested, cost_usd FROM research_runs").fetchone()
    assert tuple(run) == ("done", 6, 7, 0.12)

    # next session: the agent is shown the results and its lessons are stored
    def second(ctx):
        need = [n["id"] for n in ctx["notebook"] if n.get("needs_lesson")]
        assert 1 in need and all("periods_n_meanR" in n for n in ctx["notebook"] if n["id"] in need)
        return (
            [],
            [{"id": 1, "lesson": "Period B was a falling market and the rule kept buying."}],
            0.0,
        )

    runner.run_session(conn, L, now_ms=now, n_ideas=1, propose_fn=second, seed=8)
    assert "falling market" in {n["id"]: n for n in lab.notebook(conn)}[1]["lesson"]


def test_code_made_rules_are_always_valid():
    import random

    rng = random.Random(1)
    for _ in range(300):
        r = rules.validate(runner.random_rule(rng, 6.0), 6.0)
        rules.validate(runner.mutate(rng, r, 6.0), 6.0)
        rules.compile_rule(r)
        assert rules.describe(r).endswith(".")


def test_agent_output_becomes_a_plain_rule():
    from goldtrader.bots import lab_agent

    idea = lab_agent.LabIdea.model_validate(
        {
            "entry": {"type": "fade", "minutes": 15, "lookback": 20, "z_entry": 2.5},
            "filters": [{"type": "hours", "from_hour": 0, "to_hour": 7}],
            "exit": {"stop_atr": 3, "target_atr": 4.5, "use_target": True},
            "rationale": "Quiet Asian hours tend to revert after a stretched close.",
            "expectation": "Positive in all three periods; a trending Asia session disproves it.",
        }
    )
    rule = rules.validate(lab_agent.to_rule(idea), 6.0)
    assert (
        rule["filters"] == [{"type": "hours", "from": 0, "to": 7}]
        and "use_target" not in rule["exit"]
    )
    with pytest.raises(Exception):  # noqa: B017 - pydantic refuses fields outside the language
        lab_agent.LabIdea.model_validate({**idea.model_dump(), "code": "print(1)"})
    ctx = {
        "profile": {},
        "funnel": {},
        "notebook": [],
        "grammar": rules.grammar(),
        "stop_atr_max": 6.0,
    }
    assert "LabOut JSON" in lab_agent.messages(ctx)[0]["content"]


def test_dashboard_reads_the_notebook_and_paper_ledger_runs_a_lab_rule():
    from goldtrader.dashboard import data
    from goldtrader.shadow.ledger import LAB_KIND, ShadowLedger

    conn = connect(None)
    migrate(conn)
    conn.row_factory = __import__("sqlite3").Row
    assert data.lab(conn, 5)["funnel"]["proposed"] == 0
    iid, _ = lab.propose(conn, GOOD, source="agent", rationale="why", now_ms=1, stop_atr_max=6.0)
    res = {"gate": 1, "status": "failed", "failed_at": "periods", "why": "not positive in period B",
           "periods": [{"n": 40, "mean_r": 0.2, "net": 80.0, "win": 0.4}] * 3}  # fmt: skip
    lab.record(conn, iid, res, 2)
    conn.execute(
        "INSERT INTO research_runs(started_ms, status, proposed, tested) VALUES (1, 'done', 1, 1)"
    )
    d = data.lab(conn, 5)
    json.dumps(d)
    assert d["funnel"] == {"proposed": 1, "tested": 1, "awaiting_blind": 0, "on_paper": 0,
                           "trades": 1, "periods": 0, "robust": 0, "luck": 0, "blind": 0}  # fmt: skip
    assert d["fails"] == [("periods", 1)] and d["by_source"] == {
        "agent": {"n": 1, "past_periods": 0}
    }
    assert d["best"][0]["spec"]["entry"]["type"] == "pullback" and d["runs"][0]["status"] == "done"

    # the paper ledger accepts a lab rule, keeps its exit block and refuses what it cannot simulate
    cfg = parse_config({"owner": {"risk": {"stop_atr_max": 6.0}}})
    ledger = ShadowLedger(conn, cfg, SPEC, lambda: 0)
    canon = rules.validate(GOOD, 6.0)
    ledger.register("paper", "lab_x", LAB_KIND, canon)
    b = ledger._bundles["lab_x"]
    assert isinstance(b.strategy, rules.Composed) and b.exit["trail_atr"] == 1.5
    with pytest.raises(ValueError, match="opposite signal"):
        ledger.register(
            "paper", "lab_y", LAB_KIND, {**canon, "exit": {**canon["exit"], "on_opposite": "close"}}
        )
    with pytest.raises(rules.RuleError):
        ledger.register("paper", "lab_z", LAB_KIND, {"entry": {"type": "nope"}})

"""Learning loop (D-064): graded decisions, the agent's record, playbook, ideas, paper review."""

from __future__ import annotations

import json
import zlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from goldtrader.bots import nightly as nb
from goldtrader.bots.common import BotContext
from goldtrader.bots.strategy import build_strategy_messages
from goldtrader.cli import _default_spec
from goldtrader.config import load_config
from goldtrader.data.synthetic import generate_bars
from goldtrader.db import connect, migrate
from goldtrader.learning import loop

T0 = int(datetime(2026, 11, 17, 12, tzinfo=UTC).timestamp())


def _db():
    conn = connect(None)
    migrate(conn)
    return conn, load_config("config/demo.yaml")


def _falling_bars(conn, cfg, n=60):
    for i in range(1, n):  # price falls after the signal: a long is stopped, a short wins
        px = 4000.0 - 0.3 * i
        conn.execute(
            "INSERT INTO bars VALUES (?,60,?,?,?,?,?,?,?,?,?,1,'t')",
            (cfg.account.symbol, T0 + 60 * i, px, px + 0.2, px - 0.4, px, px + 0.2, px + 0.4)
            + (px - 0.2, px + 0.2),
        )


def _cycle(conn, cid, side, status, reason, action, conf, at_s=T0, hour=12):
    z = zlib.compress(json.dumps({"features": {"hour_utc": hour, "regime": "normal"}}).encode())
    conn.execute(
        "INSERT INTO decision_snapshots VALUES (?,?,?,?,?,?,?)",
        (cid, at_s * 1000, side, 4000.0, 4000.2, 1.0, z),
    )
    conn.execute(
        "INSERT INTO proposals(proposal_id, created_ms, bot, bundle_id, action, confidence, "
        "thesis, invalidation, agrees_with_signal, inputs_hash, raw_json, gate_status) "
        "VALUES (?,?,'strategy','b',?,?,'t','i',1,'h','{}','x')",
        ("p" + cid, at_s * 1000, action, conf),
    )
    conn.execute(
        "INSERT INTO brain_cycles VALUES (?,?,?,?,?,?,70,'[]')",
        (cid, at_s * 1000, at_s * 1000 + 5, status, "p" + cid, reason),
    )


def test_signal_decisions_are_graded_and_become_the_agents_own_record():
    conn, cfg = _db()
    strat = cfg.strategies[0]
    _falling_bars(conn, cfg)
    for i in range(6):
        _cycle(conn, f"e{i}", 1, "gated", "confidence_0.55", "enter", 0.55)
    for i in range(3):
        _cycle(conn, f"w{i}", 1, "wait", "wait", "wait", 0.8)
    _cycle(conn, "flat", 0, "wait", "wait", "wait", 0.9)  # no signal: nothing to grade
    _cycle(conn, "late", 1, "wait", "wait", "wait", 0.8, at_s=T0 + 58 * 60)  # not played out yet
    now = (T0 + 3600) * 1000
    assert loop.score_decisions(conn, cfg, strat, now) == 10
    rows = conn.execute("SELECT cycle_id, what, r, hour_utc FROM decision_scores").fetchall()
    assert len(rows) == 10 and all(r["what"] == "ai_veto" and r["hour_utc"] == 12 for r in rows)
    graded = [r for r in rows if r["r"] is not None]
    assert len(graded) == 9 and all(r["r"] == -1.0 for r in graded)
    assert loop.calibration_line(conn, now) == ""  # nine is too few to show
    assert loop.score_decisions(conn, cfg, strat, now) == 1  # only the unfinished one is retried
    _cycle(conn, "s1", -1, "gated", "confidence_0.30", "enter", 0.3)
    loop.score_decisions(conn, cfg, strat, now)
    line = loop.calibration_line(conn, now)
    assert "last 10 signals" in line
    assert "confidence 0.50-0.59: 6 signals, 0 would have won, mean -1.00 R" in line
    assert "confidence below 0.50: 1 signals, 1 would have won" in line
    assert "chose to wait: 3 signals, 0 would have won, mean -1.00 R" in line
    # the Strategy agent is shown its record and the playbook
    loop.replace_playbook(
        conn, "2026-11-17", [{"tag": "t", "text": "Asia signals: 40 played out, mean -0.2 R.", "evidence_n": 40}], now
    )  # fmt: skip
    ctx = BotContext(now_ms=now, cfg=cfg, calibration_line=line, lessons=loop.active_playbook(conn))
    text = build_strategy_messages(ctx, strat)[0]["content"]
    assert f"Calibration: {line}" in text and "Asia signals: 40 played out" in text
    ev = loop.evidence(conn, cfg, strat, now)
    json.dumps(ev)
    dec = ev["graded_decisions_last_30_days"]
    assert dec["signals_played_out"] == 10 and dec["by_what_happened"]["ai_veto"]["n"] == 10
    assert dec["by_session_utc"]["new_york"]["n"] == 10 and ev["minimum_cases_per_group"] == 8
    assert ev["live_rule"]["break_even_win_rate"] == 0.4


def test_playbook_keeps_only_lessons_that_cite_a_real_group():
    conn, _cfg = _db()
    items = [
        {"tag": "asia", "text": "Asia signals: 665 played out, mean -0.12 R; wait.", "evidence_n": 665},
        {"tag": "tiny", "text": "Three trades on Friday all won, so trust Fridays.", "evidence_n": 3},
        {"tag": "dup", "text": "asia signals: 665 played out, mean -0.12 R; wait.", "evidence_n": 665},
        {"tag": "x", "text": "too short", "evidence_n": 99},
    ]  # fmt: skip
    assert loop.replace_playbook(conn, "n1", items, 1) == 1
    assert loop.active_playbook(conn) == ["Asia signals: 665 played out, mean -0.12 R; wait."]
    many = [
        {"tag": f"t{i}", "text": f"Group number {i}: 50 played out, mean -0.1 R.", "evidence_n": 50}
        for i in range(12)
    ]
    assert loop.replace_playbook(conn, "n2", many, 2) == loop.MAX_PLAYBOOK
    book = loop.playbook_rows(conn)
    assert len(book) == 8 and all(r["night"] == "n2" for r in book)  # last night's is replaced
    assert conn.execute("SELECT COUNT(*) FROM playbook").fetchone()[0] == 9  # history is kept
    assert loop.replace_playbook(conn, "n3", [], 3) == 0 and loop.active_playbook(conn) == []


def test_ideas_outside_the_rule_library_or_the_owner_limits_are_refused():
    cfg = load_config("config/demo.yaml")
    ok = {"kind": "channel_break", "minutes": 15, "lookback": 30, "stop_atr": 4.0, "target_atr": 20}
    c, why = loop.validate_idea(ok, cfg)
    assert why == "" and c[0] == "channel_break" and c[1].startswith("idea_")
    assert c[2]["stop_atr"] == cfg.owner.risk.stop_atr_max  # clamped like the engine clamps it
    assert c[2] == {"stop_atr": 1.5, "target_atr": 20.0, "minutes": 15.0, "lookback": 30.0}
    same, _ = loop.validate_idea({**ok, "stop_atr": 9.0}, cfg)
    assert loop.idea_key(same[0], same[2]) == loop.idea_key(c[0], c[2])  # one idea, not two
    sess, _ = loop.validate_idea(
        {"kind": "breakout_session", "session": "london", "stop_atr": 1.0, "target_atr": 3}, cfg
    )
    assert sess[2]["range_start_hour"] == 7.0 and sess[2]["trade_end_hour"] == 17.0
    fade, _ = loop.validate_idea(
        {"kind": "range_fade", "minutes": 60, "z_entry": 2.5, "stop_atr": 1, "target_atr": 2}, cfg
    )
    assert fade[2]["lookback"] == 20.0 and fade[2]["z_entry"] == 2.5
    pull, _ = loop.validate_idea(
        {
            "kind": "pullback_htf",
            "minutes": 15,
            "trend_minutes": 120,
            "stop_atr": 1,
            "target_atr": 4,
        },
        cfg,
    )
    assert pull[2] == {"stop_atr": 1.0, "target_atr": 4.0, "minutes": 15.0, "trend_minutes": 120.0}
    assert loop.idea_from_params("pullback_htf", pull[2])["trend_minutes"] == 120
    for bad in (
        {
            "kind": "pullback_htf",
            "minutes": 60,
            "trend_minutes": 60,
            "stop_atr": 1,
            "target_atr": 2,
        },
        {
            "kind": "pullback_htf",
            "minutes": 15,
            "trend_minutes": 240,
            "stop_atr": 1,
            "target_atr": 2,
        },
        {"kind": "martingale", "stop_atr": 1, "target_atr": 2},
        {"kind": "trend_htf", "minutes": 7, "stop_atr": 1, "target_atr": 2},
        {"kind": "trend_htf", "minutes": 15, "stop_atr": 1, "target_atr": 500},
        {"kind": "trend_htf", "minutes": 15, "stop_atr": 2, "target_atr": 1},
        {"kind": "channel_break", "minutes": 240, "lookback": 60, "stop_atr": 1, "target_atr": 2},
        {"kind": "range_fade", "minutes": 15, "z_entry": 9, "stop_atr": 1, "target_atr": 2},
        {"kind": "breakout_session", "session": "tokyo", "stop_atr": 1, "target_atr": 2},
        {"kind": "trend_htf", "minutes": 15, "stop_atr": "x; DROP TABLE", "target_atr": 2},
    ):
        c, why = loop.validate_idea(bad, cfg)
        assert c is None and why, bad
    with pytest.raises(ValueError):
        nb.IdeasOut(ideas=[{"kind": "grid", "stop_atr": 1, "target_atr": 2, "rationale": "x" * 20}])


@pytest.fixture(scope="module")
def history(tmp_path_factory):
    cfg = load_config("config/demo.yaml")
    conn = connect(tmp_path_factory.mktemp("ll") / "t.sqlite")
    migrate(conn)
    df = generate_bars(datetime(2026, 9, 1, tzinfo=UTC), 12, 11)
    rows = [
        (cfg.account.symbol, 60, int(r.time), r.bid_open, r.bid_high, r.bid_low, r.bid_close)
        + (r.ask_open, r.ask_high, r.ask_low, r.ask_close, int(r.ticks), "synthetic")
        for r in df.itertuples(index=False)
    ]
    conn.executemany("INSERT INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    now_ms = (int(df["time"].iloc[-1]) + 3600) * 1000
    return conn, cfg, _default_spec(cfg.account.symbol), now_ms


def _fake_eval(good_kind="channel_break", held_pct=0.8):
    """A stand-in for the backtester: one kind is steadily profitable on every slice."""
    calls = {"n": 0}

    def evaluate(df, c, spread_mult=1.0):
        calls["n"] += 1
        if c[0] == good_kind:
            pct = held_pct if len(df) > 4000 else 0.5  # the blind third is the long slice
            return {"n_trades": 12, "net_return_pct": pct, "net_pnl": pct * 50}
        return {"n_trades": 12, "net_return_pct": -0.4, "net_pnl": -20.0}

    return evaluate, calls


def test_nightly_runs_once_per_trading_day_and_survives_a_failing_step(history):
    conn, cfg, spec, now = history
    strat = cfg.strategies[0]
    night = loop.night_of(now)
    assert loop.nightly_due(conn, now) == night
    seen: dict = {}

    def review(ev, book):
        seen["ev"], seen["book"] = ev, book
        n = ev["live_rule_signals_code_only"]["all"]["n"]
        return [{"tag": "all", "text": f"Live rule signals: {n} played out; be selective.", "evidence_n": n}]  # fmt: skip

    ideas = [
        {"kind": "channel_break", "minutes": 5, "lookback": 20, "stop_atr": 1.5, "target_atr": 4.5,
         "rationale": "Breakouts held on slower bars."},
        {"kind": "channel_break", "minutes": 5, "lookback": 20, "stop_atr": 3.0, "target_atr": 4.5,
         "rationale": "Same thing once the stop is clamped."},
        {"kind": "grid", "stop_atr": 1, "target_atr": 2, "rationale": "not in the library"},
        {"kind": "trend_htf", "minutes": 5, "stop_atr": 1, "target_atr": 2, "rationale": "a fourth"},
    ]  # fmt: skip
    evaluate, calls = _fake_eval(good_kind="none")
    out = loop.nightly(
        conn, cfg, strat, spec, now, review_fn=review, ideas_fn=lambda ev, tried: ideas,
        evaluate=evaluate,
    )  # fmt: skip
    assert out["review"] == {"lessons": 1} and out["evidence_groups"]["live_rule_signals"] > 8
    r = out["research"]
    assert r["trials"] == 24 and r["from_research_agent"] == 1  # the fourth idea is never tried
    assert [x["why"] for x in r["refused"]] == ["already tested", "kind not in the rule library"]
    assert r["best"] is None and r["to_paper"] == [] and r["profitable_across_folds"] == 0
    assert calls["n"] == 24 * 4  # four folds per trial; the blind period was never touched
    rows = conn.execute("SELECT status, rationale, in_sample_json FROM strategy_ideas").fetchall()
    assert len(rows) == 24 and {x["status"] for x in rows} == {"searched"}
    assert sum(1 for x in rows if x["rationale"] == "Breakouts held on slower bars.") == 1
    assert len(json.loads(rows[0]["in_sample_json"])["fold_returns"]) == 4
    run = conn.execute("SELECT * FROM learning_runs WHERE night=?", (night,)).fetchone()
    assert run["status"] == "done" and json.loads(run["summary_json"])["night"] == night
    assert loop.nightly_due(conn, now) is None  # not twice for the same trading day
    assert loop.nightly_due(conn, now + 86_400_000) == loop.night_of(now + 86_400_000)
    assert len(loop.search_history(conn)) == 24 and len(loop.tried_ideas(conn)) == 24
    # next night: nothing is tested twice, a broken review is recorded and the search still runs
    later = now + 86_400_000

    def boom(ev, book):
        raise RuntimeError("model down")

    out2 = loop.nightly(
        conn, cfg, strat, spec, later, review_fn=boom, ideas_fn=boom, evaluate=evaluate
    )
    assert out2["review"].startswith("error: RuntimeError")
    assert out2["research"]["trials"] == 24 and "research agent failed" in out2["research"]["note"]
    assert conn.execute("SELECT COUNT(DISTINCT key) FROM strategy_ideas").fetchone()[0] == 48
    assert conn.execute(
        "SELECT status FROM learning_runs WHERE night=?", (loop.night_of(later),)
    ).fetchone()[0] == "failed"  # fmt: skip
    assert len(loop.active_playbook(conn)) == 1  # the old playbook is kept when the review fails
    # without any model the review is skipped and the code search still runs
    out3 = loop.nightly(conn, cfg, strat, spec, later + 86_400_000, evaluate=evaluate)
    assert out3["review"] == "skipped: no model" and out3["research"]["trials"] == 24
    st = loop.status(conn, later)
    assert (
        st["ideas_total"] == 72 and st["last_run"]["status"] == "done" and len(st["playbook"]) == 1
    )
    assert len(st["ideas"]) == 12


def test_the_single_best_trial_is_blind_tested_and_only_then_paper_traded(history):
    conn, cfg, spec, now = history
    frame = __import__("goldtrader.backtest.timemachine", fromlist=["load_frame"]).load_frame(
        conn, cfg
    )
    conn.execute("DELETE FROM strategy_ideas")
    conn.execute("DELETE FROM paper_candidates")
    evaluate, calls = _fake_eval()
    seed = [{"kind": "channel_break", "minutes": 15, "lookback": 20, "stop_atr": 1.5, "target_atr": 6.0, "rationale": "seeded for the test"}]  # fmt: skip
    out = loop.search_ideas(conn, cfg, spec, seed, "n1", now, frame, evaluate=evaluate, trials=12)
    assert out["best"]["holds"] is True and out["profitable_across_folds"] >= 1
    assert out["pbo"] is not None and out["pbo"] <= 0.5 and "blind" in out["verdict"]
    assert calls["n"] == 12 * 4 + 2  # folds for every trial, the blind period twice for one
    holds = [t for t in out["tested"] if t["status"] == "holds"]
    assert len(holds) == 1 and holds[0]["kind"] == "channel_break"
    added = loop.auto_paper(conn, out["tested"], now)
    row = conn.execute("SELECT * FROM paper_candidates WHERE bundle_id=?", (added[0],)).fetchone()
    assert added[0].startswith("auto_") and row["active"] == 1 and row["kind"] == "channel_break"
    idea_row = conn.execute("SELECT status, paper_bundle_id, held_out_json FROM strategy_ideas WHERE status='holds'").fetchone()  # fmt: skip
    assert (
        idea_row["paper_bundle_id"] == added[0]
        and json.loads(idea_row["held_out_json"])["n_trades"] == 12
    )
    # a best trial that loses on the blind period is recorded as failed and not paper-traded
    conn.execute("DELETE FROM paper_candidates")
    bad_eval, _ = _fake_eval(good_kind="range_fade", held_pct=-0.3)
    seed2 = [{"kind": "range_fade", "minutes": 15, "lookback": 20, "z_entry": 2.0, "stop_atr": 1.0, "target_atr": 2.0, "rationale": "seeded for the test"}]  # fmt: skip
    out2 = loop.search_ideas(conn, cfg, spec, seed2, "n2", now, frame, evaluate=bad_eval, trials=12)
    assert out2["best"]["holds"] is False and loop.auto_paper(conn, out2["tested"], now) == []
    assert [t["status"] for t in out2["tested"]].count("fails") == 1
    # the number of paper candidates is capped
    for i in range(loop.MAX_ACTIVE_PAPER):
        conn.execute("INSERT INTO paper_candidates VALUES (?,?,?,?,?,1)", (f"x{i}", "flat", "{}", "x", 1))  # fmt: skip
    again = [{**holds[0], "key": "k2", "c": ("trend_htf", "idea_zz", {"minutes": 60.0})}]
    assert loop.auto_paper(conn, again, now) == []
    assert loop.idea_from_params("breakout_session", {"range_start_hour": 7.0, "stop_atr": 1.0, "target_atr": 3.0})["session"] == "london"  # fmt: skip


def test_paper_candidates_are_judged_at_100_trades_and_only_the_owner_promotes():
    conn, _cfg = _db()

    def cand(bid, n, r):
        conn.execute("INSERT INTO paper_candidates VALUES (?,?,?,?,?,1)", (bid, "flat", "{}", "Rule " + bid, 1))  # fmt: skip
        for i in range(n):
            conn.execute(
                "INSERT INTO shadow_trades(shadow_id, experiment_id, bundle_id, side, volume, "
                "signal_ms, entry_ms, exit_ms, entry_price, exit_price, sl, net_pnl, r_multiple, "
                "status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'closed')",
                (
                    f"{bid}{i}",
                    "paper",
                    bid,
                    "long",
                    0.01,
                    1,
                    1,
                    2,
                    4000.0,
                    4001.0,
                    3999.0,
                    r * 2,
                    r,
                ),
            )

    cand("good", 100, 0.2)
    cand("bad", 100, -0.2)
    cand("young", 99, -1.0)
    out = loop.review_paper(conn, 5)
    assert out == {"ready": ["good"], "retired": ["bad"]}
    active = dict(conn.execute("SELECT bundle_id, active FROM paper_candidates").fetchall())
    assert active == {"good": 1, "bad": 0, "young": 1}  # too few trades: left alone
    alerts = {r["kind"]: r["message"] for r in conn.execute("SELECT kind, message FROM alerts")}
    assert "only if you decide so" in alerts["paper_ready"] and "100 paper trades" in alerts["paper_ready"]  # fmt: skip
    assert "retired after losing" in alerts["paper_retired"]
    assert loop.review_paper(conn, 6) == {"ready": [], "retired": []}  # told once, not every time
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 2
    results = {p["id"]: p for p in loop.paper_results(conn)}
    assert results["good"]["closed_trades"] == 100 and results["good"]["total_r"] == 20.0


def test_nightly_agents_see_numbers_only_and_the_loop_cannot_reach_the_account():
    cfg = load_config("config/demo.yaml")
    for spec in (nb.nightly_review_spec(cfg), nb.strategy_ideas_spec(cfg)):
        assert spec.tools == [] and spec.fallback is None and spec.tier == "standard"
    msg = nb.review_messages({"evidence": {"a": 1}, "playbook": [{"text": "old"}]})[0]["content"]
    assert '"a": 1' in msg and "old" in msg
    msg = nb.ideas_messages({"evidence": {}, "tried": [{"kind": "x"}], "stop_atr_max": 1.5})
    assert "at most 1.5" in msg[0]["content"]
    assert "never" in nb.REVIEW_PROMPT.text.lower() and "do not trade" in nb.IDEAS_PROMPT.text
    root = Path(__file__).resolve().parents[1] / "src" / "goldtrader"
    for rel in ("learning/loop.py", "bots/nightly.py"):
        src = (root / rel).read_text(encoding="utf-8")
        for banned in ("goldtrader.mt5", "goldtrader.execution", "goldtrader.gateway", "Intent("):
            assert banned not in src, (rel, banned)
        assert "owner.risk." not in src.replace(
            "cfg.owner.risk.stop_atr", ""
        )  # reads, never writes
    # the evidence carries no channel wording: only owner-given names and measured numbers
    assert "signal_messages" not in (root / "learning" / "loop.py").read_text(encoding="utf-8")


def test_brain_starts_the_nightly_run_in_its_own_process_once(tmp_path):
    from goldtrader.brain.service import BrainService
    from goldtrader.db import Repo

    cfg = load_config("config/demo.yaml")
    conn = connect(tmp_path / "d.sqlite")
    migrate(conn)
    started: list[list[str]] = []
    now = {"ms": T0 * 1000}
    brain = BrainService(
        cfg, Repo(conn), gateway=None, provider=None, clock_ms=lambda: now["ms"],
        config_path="config/demo.yaml", spawn=started.append,
    )  # fmt: skip
    brain._learning_loop_job()
    brain._learning_loop_job()
    assert len(started) == 1
    cmd = started[0]
    assert "learn-nightly" in cmd and cmd[cmd.index("--provider") + 1] == "none"
    assert cmd[cmd.index("--night") + 1] == "2026-11-16"  # before 21:00 UTC: yesterday's session
    assert cmd[cmd.index("--db") + 1].endswith("d.sqlite")
    now["ms"] += 10 * 3_600_000  # after the daily break the next trading day is reviewed
    brain._learning_loop_job()
    assert len(started) == 2 and started[1][-1] == "2026-11-17"
    now["ms"] += 3 * 3_600_000  # a run that died is retried after two hours
    brain._learning_loop_job()
    assert len(started) == 3 and started[2][-1] == "2026-11-17"
    assert brain._lessons() == [] and brain._calibration() == ""
    no_cfg = BrainService(cfg, Repo(conn), gateway=None, provider=None, clock_ms=lambda: now["ms"] + 2 * 86_400_000, spawn=started.append)  # fmt: skip
    no_cfg._learning_loop_job()
    assert len(started) == 3  # without a config path nothing is started


def test_brain_looks_at_every_closed_bar_and_decides_at_once_on_a_signal(tmp_path):
    from goldtrader.brain.service import BrainService
    from goldtrader.db import Repo

    cfg = load_config("config/demo.yaml")
    conn = connect(tmp_path / "d.sqlite")
    migrate(conn)
    now = {"ms": T0 * 1000}
    brain = BrainService(
        cfg, Repo(conn), gateway=None, provider=None, clock_ms=lambda: now["ms"],
        spawn=lambda cmd: None,
    )  # fmt: skip
    assert brain.scheduler.jobs[0].name == "bar_watch"  # before the cycle, so it runs in one tick
    signal = {"v": 0}
    brain.feats.signal = lambda: signal["v"]
    cycle = next(j for j in brain.scheduler.jobs if j.name == "cycle")

    def bar(t):
        conn.execute(
            "INSERT INTO bars VALUES (?,60,?,4000,4001,3999,4000,4000.2,4001.2,3999.2,4000.2,1,'t')",
            (cfg.account.symbol, t),
        )

    brain._bar_watch_job()
    assert not cycle.pending_trigger  # no bars at all
    bar(T0 - 120)
    signal["v"] = 1
    brain._bar_watch_job()
    assert not cycle.pending_trigger  # the bar already there at start is not news
    bar(T0 - 60)  # closes exactly now
    signal["v"] = 0
    brain._bar_watch_job()
    assert not cycle.pending_trigger  # a new bar without a signal
    now["ms"] += 60_000
    bar(T0)
    signal["v"] = -1
    brain._bar_watch_job()
    assert cycle.pending_trigger  # a signal on the bar that just closed: decide now
    cycle.pending_trigger = False
    brain._bar_watch_job()
    assert not cycle.pending_trigger  # the same bar is not acted on twice
    bar(T0 + 60)  # a bar that arrives ten minutes late (refilled after an outage)
    now["ms"] += 11 * 60_000
    brain._bar_watch_job()
    assert not cycle.pending_trigger
    assert brain.feats.latest_bar_time() == T0 + 60

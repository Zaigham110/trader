"""One session of the research lab (D-085): gather ideas, test them, record what happened.

    sync      copy the newest stored prices into the research history
    propose   the research agent's rules (if a model is given), then rules made by code:
              one-step variations of the most promising entries and random rules from the language
    test      gates 1 and 2 for every new rule, in parallel; the full gates for the survivors
    record    every outcome in the notebook; rules past gate 4 go to paper trading

It reads prices and writes only the notebook, the session log and paper candidates. It cannot
place an order, change config or touch a limit, and nothing here raises a threshold or lowers one.
"""

from __future__ import annotations

import json
import random
import sqlite3
from collections.abc import Callable
from multiprocessing import Pool
from typing import Any

import numpy as np

from goldtrader.research import lab, rules

MAX_FULL_PER_SESSION = 4  # the full gates take about twenty minutes a rule
MAX_LAB_PAPER = 4
PAPER_PREFIX = "lab_"
_W: dict[str, Any] = {}


# ------------------------------------------------------------------ data
def sync_bars(hist: sqlite3.Connection, live_db: str) -> int:
    """Append bars the live database has and the research history lacks. Returns rows added."""
    last = hist.execute("SELECT COALESCE(MAX(time_s), 0) FROM bars WHERE seconds=60").fetchone()[0]
    src = sqlite3.connect(f"file:{live_db}?mode=ro", uri=True)
    try:
        cols = [r[1] for r in hist.execute("PRAGMA table_info(bars)")]
        rows = src.execute(
            f"SELECT {','.join(cols)} FROM bars WHERE seconds=60 AND time_s>? ORDER BY time_s",  # noqa: S608
            (last,),
        ).fetchall()
    finally:
        src.close()
    hist.executemany(
        f"INSERT OR IGNORE INTO bars({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",  # noqa: S608
        rows,
    )
    hist.commit()
    return len(rows)


def build_lab(hist_db: str, live_db: str, config_path: str) -> lab.Lab:
    from goldtrader.backtest.timemachine import _events, load_frame
    from goldtrader.cli import _default_spec
    from goldtrader.config import load_config
    from goldtrader.db import Repo, connect

    cfg = load_config(config_path)
    hconn = connect(hist_db)
    frame = load_frame(hconn, cfg)
    lconn = connect(live_db)
    spec = Repo(lconn).latest_spec(cfg.account.symbol) or _default_spec(cfg.account.symbol)
    t0, t1 = int(frame["time"].iloc[0]), int(frame["time"].iloc[-1])
    return lab.Lab(frame, cfg, spec, _events(lconn, t0, t1))  # the calendar lives in the live db


def market_profile(L: lab.Lab) -> dict[str, Any]:
    """A few undated facts about how the price moves, for the agent to reason from."""
    df = L.full[L.discovery]
    mid = df["mid_close"].to_numpy()
    out: dict[str, Any] = {"bars": int(len(df)), "days": round(len(df) / 1380, 1)}
    for n in (5, 15, 60, 240):
        r = np.diff(np.log(mid[::n]))
        if len(r) > 50:
            ac = float(np.corrcoef(r[:-1], r[1:])[0, 1])
            out[f"return_autocorrelation_{n}min"] = round(ac, 3)
    out["note_autocorrelation"] = (
        "above 0: moves tend to continue at that horizon; below 0: reverse"
    )
    by = df.groupby("hour_utc")["atr"].mean()
    sess = {
        "asia 00-06": (0, 7),
        "london 07-12": (7, 13),
        "overlap 13-17": (13, 18),
        "late 18-23": (18, 24),
    }
    base = float(df["atr"].mean())
    out["relative_range_by_session"] = {
        k: round(float(by[(by.index >= a) & (by.index < b)].mean()) / base, 2)
        for k, (a, b) in sess.items()
    }
    out["median_spread_in_atr"] = round(float((df["spread_close"] / df["atr"]).median()), 2)
    out["share_of_time_trending_1min"] = round(float((df["trend_state"] != 0).mean()), 2)
    a, b = float(mid[0]), float(mid[-1])
    out["net_drift_over_window_pct"] = round(100 * (b - a) / a, 1)
    out["drift_by_period_pct"] = [
        round(100 * (float(p["mid_close"].iloc[-1]) / float(p["mid_close"].iloc[0]) - 1), 1)
        for p in (L._part(L.full, L.cuts[i], L.cuts[i + 1]) for i in range(3))
    ]
    return out


# ------------------------------------------------------------------ rules made by code
def random_rule(rng: random.Random, stop_max: float) -> dict[str, Any]:
    etype = rng.choice(list(rules.ENTRIES))
    entry: dict[str, Any] = {"type": etype}
    if etype in ("trend_bars", "breakout", "pullback", "fade"):
        entry["minutes"] = rng.choice(
            [m for m in rules.BAR_SIZES if m <= (60 if etype == "pullback" else 120)]
        )
    if etype == "trend_bars":
        entry["on_cross"] = rng.choice([0, 1])
    if etype == "breakout":
        entry["lookback"] = rng.choice([20, 30, 55, 80])
    if etype == "pullback":
        entry["trend_minutes"] = rng.choice([60, 120, 240])
    if etype == "fade":
        entry["lookback"], entry["z_entry"] = rng.choice([20, 30, 50]), rng.choice([2.0, 2.5, 3.0])
    if etype == "session_range":
        entry.update(rng.choice([
            {"range_start_hour": 0, "range_end_hour": 7, "trade_start_hour": 7, "trade_end_hour": 12},
            {"range_start_hour": 7, "range_end_hour": 12, "trade_start_hour": 12, "trade_end_hour": 17},
        ]))  # fmt: skip
    stop = rng.choice([s for s in (2.0, 3.0, 4.0, 5.5, 6.0, 8.0) if s <= stop_max])
    ex: dict[str, Any] = {
        "stop_atr": stop,
        "target_atr": round(stop * rng.choice([1.5, 2.0, 3.0]), 2),
    }
    if rng.random() < 0.4:
        ex["trail_atr"], ex["trail_after_r"] = rng.choice([1.5, 3.0]), rng.choice([1.0, 2.0])
    filters: list[dict[str, Any]] = []
    if rng.random() < 0.35:
        filters.append(rng.choice([
            {"type": "htf_trend", "minutes": 60, "fast": 8, "slow": 21, "mode": "with"},
            {"type": "hours", "from": 7, "to": 17},
            {"type": "vol", "min_pctile": 0.3, "max_pctile": 1.0},
            {"type": "range_pos", "lookback_minutes": 240, "max": 0.8},
        ]))  # fmt: skip
    return {"entry": entry, "filters": filters, "exit": ex}


def mutate(rng: random.Random, spec: dict[str, Any], stop_max: float) -> dict[str, Any]:
    """One change to a rule that got somewhere: a neighbouring stop, target or bar size."""
    s = json.loads(json.dumps(spec))
    what = rng.choice(["stop", "target", "minutes", "trail", "drop_filter"])
    ex = s["exit"]
    if what == "stop":
        ex["stop_atr"] = round(min(stop_max, max(1.0, ex["stop_atr"] * rng.choice([0.7, 1.4]))), 2)
        ex["target_atr"] = max(ex["target_atr"], ex["stop_atr"])
    elif what == "target":
        ex["target_atr"] = round(max(ex["stop_atr"], ex["target_atr"] * rng.choice([0.6, 1.5])), 2)
    elif what == "minutes" and "minutes" in s["entry"]:
        cur = s["entry"]["minutes"]
        lo, hi, _i = rules.ENTRIES[s["entry"]["type"]][1]["minutes"]
        near = [m for m in rules.BAR_SIZES if m != cur and lo <= m <= hi]
        s["entry"]["minutes"] = min(near, key=lambda m: abs(m - cur * rng.choice([0.5, 2.0])))
    elif what == "trail":
        if "trail_atr" in ex:
            ex.pop("trail_atr"), ex.pop("trail_after_r", None), ex.pop("use_target", None)
        else:
            ex["trail_atr"], ex["trail_after_r"] = 1.5, 2.0
    elif s["filters"]:
        s["filters"].pop(rng.randrange(len(s["filters"])))
    return s


# ------------------------------------------------------------------ workers
def _init(hist_db: str, live_db: str, config_path: str) -> None:
    _W["lab"] = build_lab(hist_db, live_db, config_path)


def _quick(job: tuple[int, dict[str, Any]]) -> tuple[int, dict[str, Any]]:
    idea_id, spec = job
    try:
        return idea_id, _W["lab"].evaluate(spec, stop_after=2)
    except Exception as e:  # noqa: BLE001 - one bad rule must not end the session
        return idea_id, {
            "gate": 0,
            "status": "error",
            "failed_at": "error",
            "why": f"{type(e).__name__}: {e}"[:200],
        }


def _full(job: tuple[int, dict[str, Any], int, float | None]) -> tuple[int, dict[str, Any]]:
    idea_id, spec, n_trials, var = job
    try:
        return idea_id, _W["lab"].evaluate(spec, n_trials=n_trials, trial_sr_var=var)
    except Exception as e:  # noqa: BLE001
        return idea_id, {
            "gate": 0,
            "status": "error",
            "failed_at": "error",
            "why": f"{type(e).__name__}: {e}"[:200],
        }


# ------------------------------------------------------------------ the session
def funnel(conn: sqlite3.Connection) -> dict[str, int]:
    f = {"proposed": 0, "tested": 0, "passed_trades": 0, "passed_periods": 0, "passed_robust": 0,
         "passed_luck": 0, "passed_blind": 0, "awaiting_blind": 0, "on_paper": 0}  # fmt: skip
    for status, gate, paper, tested in conn.execute(
        "SELECT status, gate, paper_bundle, tested_ms FROM research_ideas"
    ):
        f["proposed"] += 1
        f["tested"] += tested is not None
        for i, k in enumerate(
            ("passed_trades", "passed_periods", "passed_robust", "passed_luck", "passed_blind"), 1
        ):
            f[k] += gate >= i
        f["awaiting_blind"] += status == "awaiting_blind"
        f["on_paper"] += paper is not None
    return f


def _brief(n: dict[str, Any]) -> dict[str, Any]:
    r = n["results"] or {}
    out = {"id": n["id"], "rule": n["what"], "spec": n["spec"], "source": n["source"],
           "outcome": n["status"] + (f" at gate {n['failed_at']}" if n["failed_at"] else ""),
           "why": r.get("why")}  # fmt: skip
    if r.get("periods"):
        out["periods_n_meanR"] = [[p["n"], p["mean_r"]] for p in r["periods"]]
    for k in ("robust", "luck", "blind"):
        if r.get(k):
            out[k] = r[k]
    if n["rationale"]:
        out["rationale"] = n["rationale"]
    if n["lesson"]:
        out["lesson"] = n["lesson"]
    elif n["status"] not in ("proposed",) and n["results"]:
        out["needs_lesson"] = True
    return out


def to_paper(conn: sqlite3.Connection, idea: dict[str, Any], now_ms: int) -> str | None:
    """Send a rule that passed gate 4 to paper trading (simulated only), a few at a time."""
    if idea["spec"]["exit"].get("on_opposite"):
        return None  # the paper ledger cannot close or reverse on an opposite signal
    n = conn.execute(
        "SELECT COUNT(*) FROM paper_candidates WHERE active=1 AND kind='lab'"
    ).fetchone()[0]
    if n >= MAX_LAB_PAPER:
        return None
    bid = f"{PAPER_PREFIX}{idea['key'][:10]}"
    conn.execute(
        "INSERT OR IGNORE INTO paper_candidates(bundle_id, kind, params_json, label, created_ms, "
        "active) VALUES (?, 'lab', ?, ?, ?, 1)",
        (bid, json.dumps(idea["spec"], sort_keys=True), idea["what"][:200], now_ms),
    )
    conn.execute("UPDATE research_ideas SET paper_bundle=? WHERE id=?", (bid, idea["id"]))
    return bid


def run_session(
    conn: sqlite3.Connection,
    L: lab.Lab,
    *,
    now_ms: Callable[[], int],
    n_ideas: int = 24,
    propose_fn: Callable[[dict[str, Any]], tuple[list, list, float]] | None = None,
    pool: Any = None,
    seed: int | None = None,
    log: Callable[[str], None] = lambda _m: None,
) -> dict[str, Any]:
    """One lab session. `pool` maps jobs over worker processes (None = run here, for tests)."""
    stop_max = L.stop_max
    start = now_ms()
    run_id = conn.execute(
        "INSERT INTO research_runs(started_ms, status) VALUES (?, 'running')", (start,)
    ).lastrowid
    rng = random.Random(seed if seed is not None else start)
    cost = 0.0
    proposed: list[int] = []

    # 1. the agent: lessons for what was tested last time, then new rules
    if propose_fn is not None:
        book = [_brief(n) for n in lab.notebook(conn, 60)]
        ctx = {"profile": market_profile(L), "funnel": funnel(conn), "notebook": book,
               "grammar": rules.grammar(), "stop_atr_max": stop_max, "max_ideas": min(8, n_ideas)}  # fmt: skip
        try:
            ideas, lessons, cost = propose_fn(ctx)
        except Exception as e:  # noqa: BLE001 - the session goes on with code-made rules
            ideas, lessons = [], []
            log(f"agent failed: {type(e).__name__}: {e}")
        for les in lessons:
            conn.execute(
                "UPDATE research_ideas SET lesson=? WHERE id=? AND lesson IS NULL AND tested_ms IS NOT NULL",
                (" ".join(str(les.get("lesson", "")).split())[:300], les.get("id")),
            )
        for i in ideas:
            iid, why = lab.propose(conn, i["rule"], source="agent", rationale=i["rationale"],
                                   expectation=i["expectation"], now_ms=now_ms(), stop_atr_max=stop_max)  # fmt: skip
            if iid is not None:
                proposed.append(iid)
            else:
                log(f"agent rule refused: {why}")

    # 2. rules made by code, to fill the session
    best = conn.execute(
        "SELECT spec_json FROM research_ideas WHERE gate >= 1 AND status != 'error' "
        "ORDER BY gate DESC, id DESC LIMIT 12"
    ).fetchall()
    tries = 0
    while len(proposed) < n_ideas and tries < n_ideas * 20:
        tries += 1
        if best and rng.random() < 0.5:
            cand = mutate(rng, json.loads(rng.choice(best)[0]), stop_max)
            why_text = "one-step variation of a rule that got past the first gate"
        else:
            cand = random_rule(rng, stop_max)
            why_text = "random rule from the language (baseline exploration)"
        iid, _ = lab.propose(
            conn,
            cand,
            source="optimiser",
            rationale=why_text,
            now_ms=now_ms(),
            stop_atr_max=stop_max,
        )
        if iid is not None:
            proposed.append(iid)

    # 3. gates 1 and 2 for everything untested (also what an interrupted session left behind)
    todo = [
        (r[0], json.loads(r[1]))
        for r in conn.execute(
            "SELECT id, spec_json FROM research_ideas WHERE tested_ms IS NULL ORDER BY id"
        )
    ]
    mapper = pool.imap_unordered if pool is not None else lambda f, jobs: map(f, jobs)
    if pool is None:
        _W["lab"] = L
    survivors: list[tuple[int, dict[str, Any], float]] = []
    for idea_id, res in mapper(_quick, todo):
        if res.get("gate", 0) >= 2 and res.get("status") == "pending":
            worst = min(p["mean_r"] for p in res["periods"])
            survivors.append((idea_id, res, worst))
            conn.execute(
                "UPDATE research_ideas SET gate=2, results_json=? WHERE id=?",
                (json.dumps(res), idea_id),
            )
        else:
            lab.record(conn, idea_id, res, now_ms())
        log(f"idea {idea_id}: gate {res.get('gate')} {res.get('failed_at') or ''}")

    # 4. the full gates for the best survivors, now and from earlier sessions
    waiting = conn.execute(
        "SELECT id, spec_json, results_json FROM research_ideas WHERE tested_ms IS NULL AND gate = 2"
    ).fetchall()
    ranked = sorted(waiting, key=lambda r: -min(p["mean_r"] for p in json.loads(r[2])["periods"]))
    n_trials, var = lab.trial_stats(conn)
    n_trials += len(ranked)
    jobs = [(r[0], json.loads(r[1]), max(1, n_trials), var) for r in ranked[:MAX_FULL_PER_SESSION]]
    passed4 = 0
    for idea_id, res in mapper(_full, jobs):
        lab.record(conn, idea_id, res, now_ms())
        log(f"idea {idea_id}: full gates -> {res.get('status')} (gate {res.get('gate')})")
        if res.get("gate", 0) >= 4:
            passed4 += 1
            row = conn.execute(
                "SELECT id, key, spec_json, what FROM research_ideas WHERE id=?", (idea_id,)
            ).fetchone()
            bid = to_paper(
                conn,
                {"id": row[0], "key": row[1], "spec": json.loads(row[2]), "what": row[3]},
                now_ms(),
            )
            if bid:
                conn.execute(
                    "INSERT INTO alerts(created_ms, severity, kind, message, details_json) VALUES (?,?,?,?,'{}')",
                    (now_ms(), "info", "lab_candidate",
                     f"Research lab: a rule passed four gates and is now on paper as {bid}: {row[3]}"),
                )  # fmt: skip

    summary = {"proposed": len(proposed), "tested": len(todo), "passed_gate2": len(survivors),
               "full_gates_run": len(jobs), "passed_gate4": passed4, "funnel": funnel(conn),
               "still_waiting_for_full_gates": max(0, len(ranked) - len(jobs))}  # fmt: skip
    conn.execute(
        "UPDATE research_runs SET finished_ms=?, status='done', proposed=?, tested=?, passed_gate2=?, "
        "passed_gate4=?, cost_usd=?, summary_json=? WHERE id=?",
        (
            now_ms(),
            len(proposed),
            len(todo),
            len(survivors),
            passed4,
            cost,
            json.dumps(summary),
            run_id,
        ),
    )
    return summary


def make_pool(hist_db: str, live_db: str, config_path: str, workers: int = 3) -> Any:
    return Pool(workers, initializer=_init, initargs=(hist_db, live_db, config_path))

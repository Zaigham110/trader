"""The learning loop (D-064): how the agents get better informed from everything the system does.

The model behind the agents is fixed; what grows is what they are shown. This module is the code
half of that, with no model call in it:

  * `score_decisions` grades every decision cycle in which the deterministic signal wanted a trade:
    what the Strategy agent did with it and how the signal played out on real prices;
  * `calibration_line` turns those grades into the Strategy agent's own track record;
  * `evidence` gathers the measured numbers the nightly review may draw lessons from: graded
    decisions, how the live rule's signals did by session and volatility, the paper candidates and
    the Telegram channels. Groups with fewer than `MIN_GROUP` cases are left out, so a lesson can
    never rest on a handful of trades;
  * `replace_playbook` / `active_playbook` hold the lessons the Strategy agent reads;
  * `validate_idea` / `try_ideas` take the research agent's strategy ideas, refuse anything
    outside the rule library or the owner's limits, and test the rest with the held-out search
    (`backtest.search`). An idea that holds goes to paper trading, never to the account;
  * `review_paper` tells the owner when a paper candidate has enough trades to judge, and retires
    the ones that lost;
  * `nightly` runs the whole thing once per trading day. The two model calls are injected.

Nothing here can place an order, change config or touch a risk limit.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import logging
import sqlite3
import zlib
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from goldtrader.config.schema import Config, StrategyConfig
from goldtrader.db.repo import Repo
from goldtrader.report import veto
from goldtrader.session.state import SessionClock

log = logging.getLogger(__name__)

MIN_GROUP = 8  # smaller groups are not shown to the agents at all
MIN_CALIBRATION = 10
SCORE_LOOKBACK_MS = 14 * 86_400_000
OUTCOME_DAYS = 30
NIGHT_STARTS_HOUR_UTC = 21  # the daily break: the trading day just ended is reviewed after it
STALE_RUN_MS = 2 * 3_600_000
MAX_PLAYBOOK = 8
MAX_IDEAS_PER_NIGHT = 3
MAX_ACTIVE_PAPER = 8
PAPER_MIN_TRADES = 100
SESSIONS = (("asia", 0, 7), ("london", 7, 12), ("new_york", 12, 17), ("late", 17, 24))
CONF_BANDS = ((0.0, 0.5, "below 0.50"), (0.5, 0.6, "0.50-0.59"), (0.6, 1.01, "0.60 or more"))
IDEA_KINDS = ("trend_htf", "range_fade", "channel_break", "breakout_session")
IDEA_MINUTES = (5, 15, 30, 60, 120, 240)

ReviewFn = Callable[[dict[str, Any], list[dict[str, Any]]], list[dict[str, Any]] | None]
IdeasFn = Callable[[dict[str, Any], list[dict[str, Any]]], list[dict[str, Any]]]


def session_of(hour: int | None) -> str:
    for name, a, b in SESSIONS:
        if hour is not None and a <= int(hour) < b:
            return name
    return "unknown"


def _stat(rs: list[float]) -> dict[str, Any]:
    n = len(rs)
    return {
        "n": n,
        "won": sum(1 for r in rs if r > 0),
        "mean_r": round(sum(rs) / n, 3) if n else None,
        "total_r": round(sum(rs), 2),
    }


def _groups(pairs: list[tuple[str, float]]) -> dict[str, dict[str, Any]]:
    by: dict[str, list[float]] = {}
    for k, r in pairs:
        by.setdefault(k, []).append(r)
    return {k: _stat(v) for k, v in sorted(by.items()) if len(v) >= MIN_GROUP}


def _table(conn: sqlite3.Connection, name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    )


# ------------------------------------------------------------------------------- grading
def _stops(cfg: Config, strat: StrategyConfig) -> tuple[float, float]:
    r = cfg.owner.risk
    stop = min(max(float(strat.params.get("stop_atr", 1.0)), r.stop_atr_min), r.stop_atr_max)
    return stop, max(float(strat.params.get("target_atr", 1.5)), stop * r.min_reward_risk)


def _calendar(conn: sqlite3.Connection, start_ms: int, end_ms: int) -> list:
    try:
        from goldtrader.feeds.calendar_store import CalendarStore

        return CalendarStore(conn).between(start_ms - 86_400_000, end_ms + 86_400_000)
    except Exception:  # noqa: BLE001 - no calendar: grade without news windows
        return []


def score_decisions(
    conn: sqlite3.Connection, cfg: Config, strat: StrategyConfig, now_ms: int
) -> int:
    """Grade signal cycles that are new or not yet played out. Returns how many rows were written."""
    rows = conn.execute(
        "SELECT s.cycle_id, s.at_ms, s.signal_side, s.atr, s.inputs_z, c.status, c.gate_reason, "
        "p.action, p.confidence FROM decision_snapshots s "
        "JOIN brain_cycles c ON c.cycle_id = s.cycle_id "
        "LEFT JOIN proposals p ON p.proposal_id = c.proposal_id "
        "LEFT JOIN decision_scores d ON d.cycle_id = s.cycle_id "
        "WHERE s.signal_side != 0 AND c.status != 'running' AND s.at_ms >= ? "
        "AND (d.cycle_id IS NULL OR d.r IS NULL) ORDER BY s.at_ms",
        (now_ms - SCORE_LOOKBACK_MS,),
    ).fetchall()
    if not rows:
        return 0
    stop_atr, target_atr = _stops(cfg, strat)
    start_s = int(rows[0]["at_ms"]) // 1000
    bars = Repo(conn).load_bars(cfg.account.symbol, 60, start_s, int(now_ms // 1000) + 60)
    times = [b.time for b in bars]
    events = _calendar(conn, start_s * 1000, now_ms)
    clock = SessionClock(cfg.session)
    n = 0
    for row in rows:
        at_s = int(row["at_ms"]) // 1000
        lo = bisect.bisect_right(times, at_s)
        hi = bisect.bisect_right(times, at_s + veto.MAX_HOLD_S)
        out = veto.simulate(
            int(row["signal_side"]),
            at_s,
            row["atr"],
            bars[lo:hi],
            stop_atr,
            target_atr,
            clock,
            events,
        )
        hour, regime = None, None
        try:
            feats = json.loads(zlib.decompress(row["inputs_z"])).get("features") or {}
            hour, regime = feats.get("hour_utc"), feats.get("regime")
        except Exception:  # noqa: BLE001 - an unreadable snapshot still gets a grade
            pass
        conn.execute(
            "INSERT OR REPLACE INTO decision_scores(cycle_id, at_ms, side, what, reason, action, "
            "confidence, hour_utc, regime, r, exit, scored_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["cycle_id"],
                int(row["at_ms"]),
                int(row["signal_side"]),
                veto.classify(row["status"], row["gate_reason"]),
                row["gate_reason"] or row["status"],
                row["action"],
                row["confidence"],
                hour,
                regime,
                out["r"] if out else None,
                out["exit"] if out else None,
                now_ms if out else None,
            ),
        )
        n += 1
    return n


def _graded(conn: sqlite3.Connection, now_ms: int, days: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM decision_scores WHERE r IS NOT NULL AND at_ms >= ? ORDER BY at_ms",
        (now_ms - days * 86_400_000,),
    ).fetchall()


def calibration_line(conn: sqlite3.Connection, now_ms: int, days: int = 30) -> str:
    """The Strategy agent's own record on signals that have played out, or '' while too few."""
    if not _table(conn, "decision_scores"):
        return ""
    rows = _graded(conn, now_ms, days)
    if len(rows) < MIN_CALIBRATION:
        return ""
    parts: list[str] = []
    entered = [r for r in rows if r["action"] == "enter" and r["confidence"] is not None]
    for lo, hi, label in CONF_BANDS:
        rs = [float(r["r"]) for r in entered if lo <= float(r["confidence"]) < hi]
        if rs:
            s = _stat(rs)
            parts.append(
                f"proposed entry at confidence {label}: {s['n']} signals, {s['won']} would have "
                f"won, mean {s['mean_r']:+.2f} R"
            )
    waited = [float(r["r"]) for r in rows if r["action"] == "wait"]
    if waited:
        s = _stat(waited)
        parts.append(
            f"chose to wait: {s['n']} signals, {s['won']} would have won, mean {s['mean_r']:+.2f} R"
        )
    if not parts:
        return ""
    return (
        f"your record on the last {len(rows)} signals that played out, as code-only trades (R = "
        "multiples of the amount risked; entries need confidence 0.60): " + "; ".join(parts) + "."
    )


# ------------------------------------------------------------------------------- evidence
def signal_outcomes(
    conn: sqlite3.Connection, cfg: Config, strat: StrategyConfig, now_ms: int, frame: Any = None
) -> dict[str, Any]:
    """How every signal of the live rule did over the last `OUTCOME_DAYS` days as a code-only
    trade, grouped by session, volatility and side. This is all its signals on every bar, not
    only the ones a decision cycle happened to see."""
    from goldtrader.backtest.strategies import build_strategy
    from goldtrader.backtest.timemachine import load_frame

    df = frame if frame is not None else load_frame(conn, cfg)
    since_s = int(now_ms // 1000) - OUTCOME_DAYS * 86_400
    df = df[df["time"] >= since_s - 86_400].reset_index(drop=True)
    if len(df) < 500:
        return {"n": 0}
    side = build_strategy(strat.kind, strat.id, dict(strat.params)).signals(df).side
    stop_atr, target_atr = _stops(cfg, strat)
    start_s = int(df["time"].iloc[0])
    bars = Repo(conn).load_bars(cfg.account.symbol, 60, start_s, int(now_ms // 1000) + 60)
    times = [b.time for b in bars]
    events = _calendar(conn, start_s * 1000, now_ms)
    clock = SessionClock(cfg.session)
    t, atr = df["time"].to_numpy(), df["atr"].to_numpy()
    pct = df["atr_pctile"].to_numpy() if "atr_pctile" in df else None
    by_session: list[tuple[str, float]] = []
    by_vol: list[tuple[str, float]] = []
    by_side: list[tuple[str, float]] = []
    for i in side.nonzero()[0]:
        at_s = int(t[i])
        if at_s < since_s:
            continue
        lo = bisect.bisect_right(times, at_s)
        hi = bisect.bisect_right(times, at_s + veto.MAX_HOLD_S)
        out = veto.simulate(
            int(side[i]), at_s, float(atr[i]), bars[lo:hi], stop_atr, target_atr, clock, events
        )
        if out is None:
            continue
        r = float(out["r"])
        by_session.append((session_of((at_s % 86_400) // 3600), r))
        by_side.append(("long" if side[i] > 0 else "short", r))
        if pct is not None and pct[i] == pct[i]:
            band = "quiet" if pct[i] < 0.33 else ("busy" if pct[i] > 0.66 else "normal")
            by_vol.append((f"volatility_{band}", r))
    return {
        "days": OUTCOME_DAYS,
        "n": len(by_side),
        "all": _stat([r for _, r in by_side]),
        "by_session_utc": _groups(by_session),
        "by_volatility": _groups(by_vol),
        "by_side": _groups(by_side),
    }


def paper_results(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not (_table(conn, "paper_candidates") and _table(conn, "shadow_trades")):
        return []
    rows = conn.execute(
        "SELECT c.bundle_id, c.label, c.active, "
        "SUM(CASE WHEN t.status='closed' THEN 1 ELSE 0 END) AS n, "
        "SUM(CASE WHEN t.status='closed' AND t.r_multiple > 0 THEN 1 ELSE 0 END) AS won, "
        "COALESCE(SUM(CASE WHEN t.status='closed' THEN t.r_multiple END),0) AS total_r, "
        "COALESCE(SUM(CASE WHEN t.status='closed' THEN t.net_pnl END),0) AS net "
        "FROM paper_candidates c LEFT JOIN shadow_trades t ON t.bundle_id=c.bundle_id "
        "GROUP BY c.bundle_id ORDER BY c.created_ms"
    ).fetchall()
    return [
        {
            "id": r["bundle_id"],
            "what": r["label"],
            "active": bool(r["active"]),
            "closed_trades": int(r["n"] or 0),
            "won": int(r["won"] or 0),
            "total_r": round(float(r["total_r"] or 0), 2),
            "net_usd": round(float(r["net"] or 0), 2),
        }
        for r in rows
    ]


def evidence(
    conn: sqlite3.Connection, cfg: Config, strat: StrategyConfig, now_ms: int, frame: Any = None
) -> dict[str, Any]:
    """Measured numbers only. Everything in here was computed by code from prices and our own
    records; no channel wording, news text or model output is included."""
    rows = _graded(conn, now_ms, 30)
    dec = {
        "signals_played_out": len(rows),
        "by_what_happened": _groups([(r["what"], float(r["r"])) for r in rows]),
        "by_session_utc": _groups([(session_of(r["hour_utc"]), float(r["r"])) for r in rows]),
        "agent_record": calibration_line(conn, now_ms),
    }
    try:
        outcomes = signal_outcomes(conn, cfg, strat, now_ms, frame)
    except Exception as e:  # noqa: BLE001 - too little history must not stop the review
        outcomes = {"n": 0, "note": f"unavailable: {type(e).__name__}"}
    channels: list[dict[str, Any]] = []
    try:
        from goldtrader.signals.report import scorecard

        card = scorecard(conn, now_ms) or {}
        for ch in card.get("channels", []):
            if int(ch.get("closed") or 0) >= MIN_GROUP:
                channels.append(
                    {k: ch.get(k) for k in ("name", "closed", "wins", "sum_r", "mean_r")}
                )
    except Exception:  # noqa: BLE001 - optional
        pass
    stop_atr, target_atr = _stops(cfg, strat)
    return {
        "live_rule": {
            "id": strat.id,
            "kind": strat.kind,
            "stop_atr": stop_atr,
            "target_atr": target_atr,
            "break_even_win_rate": round(stop_atr / (stop_atr + target_atr), 3),
        },
        "minimum_cases_per_group": MIN_GROUP,
        "graded_decisions_last_30_days": dec,
        "live_rule_signals_code_only": outcomes,
        "paper_candidates": [p for p in paper_results(conn) if p["closed_trades"] >= MIN_GROUP],
        "telegram_channels": channels,
    }


# ------------------------------------------------------------------------------- playbook
def active_playbook(conn: sqlite3.Connection, limit: int = MAX_PLAYBOOK) -> list[str]:
    if not _table(conn, "playbook"):
        return []
    rows = conn.execute(
        "SELECT text FROM playbook WHERE active=1 ORDER BY id LIMIT ?", (int(limit),)
    ).fetchall()
    return [r[0] for r in rows]


def playbook_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _table(conn, "playbook"):
        return []
    return [
        dict(r)
        for r in conn.execute(
            "SELECT id, night, tag, text, evidence_n FROM playbook WHERE active=1 ORDER BY id"
        )
    ]


def replace_playbook(
    conn: sqlite3.Connection, night: str, items: list[dict[str, Any]], now_ms: int
) -> int:
    """Tonight's playbook replaces the previous one. Lessons that cite fewer than `MIN_GROUP`
    cases are dropped: the model is only shown groups that large, so a smaller number is invented."""
    keep: list[tuple[str, str, int]] = []
    seen: set[str] = set()
    for it in items:
        text = " ".join(str(it.get("text", "")).split())[:240]
        n = int(it.get("evidence_n") or 0)
        if len(text) < 20 or n < MIN_GROUP or text.lower() in seen:
            continue
        seen.add(text.lower())
        keep.append((" ".join(str(it.get("tag", "general")).split())[:24] or "general", text, n))
    keep = keep[:MAX_PLAYBOOK]
    conn.execute("UPDATE playbook SET active=0 WHERE active=1")
    for tag, text, n in keep:
        conn.execute(
            "INSERT INTO playbook(night, created_ms, tag, text, evidence_n, active) "
            "VALUES (?,?,?,?,?,1)",
            (night, now_ms, tag, text, n),
        )
    return len(keep)


# ------------------------------------------------------------------------------- ideas
def idea_key(kind: str, params: dict[str, float]) -> str:
    return kind + "|" + json.dumps({k: float(v) for k, v in sorted(params.items())})


def validate_idea(idea: dict[str, Any], cfg: Config) -> tuple[tuple[str, str, dict] | None, str]:
    """A research-agent idea as a search candidate, or (None, why not). Only rule-library kinds,
    only their own parameters, every value inside fixed ranges; the stop is then clamped to the
    owner's limits exactly as the engine and the live gate clamp it."""
    from goldtrader.backtest import search as ss

    kind = str(idea.get("kind", ""))
    if kind not in IDEA_KINDS:
        return None, "kind not in the rule library"

    def num(key: str, default: float | None = None) -> float | None:
        v = idea.get(key, default)
        try:
            v = float(v) if v is not None else None
        except (TypeError, ValueError):
            return None
        return v if v is not None and v == v and abs(v) < 1e6 else None

    stop, target = num("stop_atr", 1.5), num("target_atr")
    if stop is None or target is None or not 0.1 <= stop <= 10 or not stop <= target <= 40:
        return None, "stop or target outside the allowed range"
    p: dict[str, float] = {"stop_atr": stop, "target_atr": target}
    if kind == "breakout_session":
        sess = ss.SESSIONS.get(str(idea.get("session", "")))
        if sess is None:
            return None, "session must be asia or london"
        p.update(
            zip(
                ("range_start_hour", "range_end_hour", "trade_start_hour", "trade_end_hour"),
                (float(x) for x in sess),
                strict=True,
            )
        )
    else:
        minutes = num("minutes")
        if minutes is None or int(minutes) not in IDEA_MINUTES:
            return None, f"minutes must be one of {IDEA_MINUTES}"
        p["minutes"] = float(int(minutes))
        if kind == "trend_htf":
            p["on_cross"] = 1.0 if idea.get("on_cross") else 0.0
        else:
            lookback = num("lookback", 20)
            if lookback is None or not 5 <= lookback <= 100:
                return None, "lookback must be 5 to 100 bars"
            # the trailing frame the paper ledger keeps is about 3 days of 1-minute bars
            if lookback * p["minutes"] > 3000:
                return None, "lookback x minutes is longer than the history the paper ledger keeps"
            p["lookback"] = float(int(lookback))
        if kind == "range_fade":
            z = num("z_entry", 2.0)
            if z is None or not 1.0 <= z <= 4.0:
                return None, "z_entry must be 1.0 to 4.0"
            p["z_entry"] = round(z, 2)
    name = "idea_" + hashlib.sha256(idea_key(kind, p).encode()).hexdigest()[:8]
    return ss.effective((kind, name, p), cfg), ""


def tried_ideas(conn: sqlite3.Connection, limit: int = 60) -> list[dict[str, Any]]:
    if not _table(conn, "strategy_ideas"):
        return []
    out = []
    for r in conn.execute(
        "SELECT kind, params_json, status, in_sample_json, held_out_json FROM strategy_ideas "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    ):
        ins, held = json.loads(r["in_sample_json"] or "{}"), json.loads(r["held_out_json"] or "{}")
        out.append(
            {
                "kind": r["kind"],
                **json.loads(r["params_json"]),
                "result": r["status"],
                "in_sample_pct": ins.get("net_return_pct"),
                "held_out_pct": held.get("net_return_pct"),
            }
        )
    return out


def try_ideas(
    conn: sqlite3.Connection,
    cfg: Config,
    spec: Any,
    ideas: list[dict[str, Any]],
    night: str,
    now_ms: int,
    frame: Any = None,
) -> list[dict[str, Any]]:
    """Validate, de-duplicate and test tonight's ideas; record every one. Returns the rows."""
    from goldtrader.backtest import search as ss

    out: list[dict[str, Any]] = []
    cands: list[tuple[str, str, dict]] = []
    why: dict[str, str] = {}
    for idea in ideas[:MAX_IDEAS_PER_NIGHT]:
        c, reason = validate_idea(idea, cfg)
        rationale = " ".join(str(idea.get("rationale", "")).split())[:300]
        if c is None:
            out.append({"kind": str(idea.get("kind"))[:30], "status": f"invalid: {reason}"})
            continue
        key = idea_key(c[0], c[2])
        if (
            key in why
            or conn.execute("SELECT 1 FROM strategy_ideas WHERE key=?", (key,)).fetchone()
        ):
            out.append({"kind": c[0], "what": ss.describe(c), "status": "already tested"})
            continue
        why[key] = rationale
        cands.append(c)
    if not cands:
        return out
    res = ss.run_search(conn, cfg, spec, candidates=cands, frame=frame)
    finalists = {r["candidate"][1]: r for r in res["finalists"]}
    for row in res["all"][1:]:
        c = next(x for x in cands if x[1] == row["id"])
        f = finalists.get(c[1])
        ins = row["in_sample"]
        if f is not None:
            status = "holds" if f["holds"] else "fails"
        elif (ins.get("n_trades") or 0) < ss.MIN_TRADES_IN_SAMPLE:
            status = "too_few_trades"
        else:
            status = "fails"
        key = idea_key(c[0], c[2])
        conn.execute(
            "INSERT OR IGNORE INTO strategy_ideas(night, created_ms, kind, params_json, key, what, "
            "rationale, status, in_sample_json, held_out_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                night,
                now_ms,
                c[0],
                json.dumps(c[2], sort_keys=True),
                key,
                row["what"],
                why[key],
                status,
                json.dumps(ins),
                json.dumps(f["held_out"]) if f is not None else None,
            ),
        )
        out.append({"kind": c[0], "what": row["what"], "status": status, "key": key, "c": c})
    return out


def auto_paper(conn: sqlite3.Connection, tested: list[dict[str, Any]], now_ms: int) -> list[str]:
    """Ideas that held go to paper trading (simulated, never the account), up to a fixed number."""
    added: list[str] = []
    for t in tested:
        if t.get("status") != "holds":
            continue
        active = conn.execute("SELECT COUNT(*) FROM paper_candidates WHERE active=1").fetchone()[0]
        if active >= MAX_ACTIVE_PAPER:
            break
        kind, name, params = t["c"]
        bid = "auto_" + name.removeprefix("idea_")
        conn.execute(
            "INSERT OR IGNORE INTO paper_candidates(bundle_id, kind, params_json, label, "
            "created_ms, active) VALUES (?,?,?,?,?,1)",
            (bid, kind, json.dumps(params, sort_keys=True), t["what"][:200], now_ms),
        )
        conn.execute("UPDATE strategy_ideas SET paper_bundle_id=? WHERE key=?", (bid, t["key"]))
        added.append(bid)
    return added


# ------------------------------------------------------------------------------- paper review
def _alert(conn: sqlite3.Connection, now_ms: int, kind: str, bundle: str, message: str) -> bool:
    """One alert per candidate and kind, delivered by the brain through the owner's bot."""
    details = json.dumps({"bundle_id": bundle})
    if conn.execute(
        "SELECT 1 FROM alerts WHERE kind=? AND details_json=?", (kind, details)
    ).fetchone():
        return False
    conn.execute(
        "INSERT INTO alerts(created_ms, severity, kind, message, details_json) "
        "VALUES (?,'info',?,?,?)",
        (now_ms, kind, message, details),
    )
    return True


def review_paper(conn: sqlite3.Connection, now_ms: int) -> dict[str, list[str]]:
    """With `PAPER_MIN_TRADES` closed paper trades a candidate can be judged: a profitable one is
    put in front of the owner (who alone decides about the demo account), a losing one is retired."""
    out: dict[str, list[str]] = {"ready": [], "retired": []}
    for p in paper_results(conn):
        if not p["active"] or p["closed_trades"] < PAPER_MIN_TRADES:
            continue
        record = (
            f"{p['closed_trades']} paper trades, {p['won']} won, {p['total_r']:+.1f} R in total, "
            f"{p['net_usd']:+.2f} USD at 0.01 lot"
        )
        if p["total_r"] > 0 and p["net_usd"] > 0:
            if _alert(
                conn,
                now_ms,
                "paper_ready",
                p["id"],
                f"Paper strategy ready for your review: {p['what']}. {record}. It keeps paper "
                "trading; it trades the demo account only if you decide so.",
            ):
                out["ready"].append(p["id"])
        else:
            conn.execute("UPDATE paper_candidates SET active=0 WHERE bundle_id=?", (p["id"],))
            _alert(
                conn,
                now_ms,
                "paper_retired",
                p["id"],
                f"Paper strategy retired after losing: {p['what']}. {record}.",
            )
            out["retired"].append(p["id"])
    return out


# ------------------------------------------------------------------------------- nightly
def night_of(now_ms: int) -> str:
    """UTC date of the trading day that the review at `now_ms` covers."""
    shifted = now_ms / 1000 - NIGHT_STARTS_HOUR_UTC * 3600
    return datetime.fromtimestamp(shifted, UTC).strftime("%Y-%m-%d")


def nightly_due(conn: sqlite3.Connection, now_ms: int) -> str | None:
    """The night to review now, or None. A day missed because the PC was off is caught up as soon
    as the brain runs again; a run that died is retried after two hours."""
    night = night_of(now_ms)
    row = conn.execute(
        "SELECT status, started_ms FROM learning_runs WHERE night=?", (night,)
    ).fetchone()
    if row is None:
        return night
    if row["status"] == "running" and now_ms - int(row["started_ms"]) > STALE_RUN_MS:
        return night
    return None


def mark_started(conn: sqlite3.Connection, night: str, now_ms: int) -> None:
    conn.execute(
        "INSERT INTO learning_runs(night, started_ms, status) VALUES (?,?,'running') "
        "ON CONFLICT(night) DO UPDATE SET started_ms=excluded.started_ms, status='running'",
        (night, now_ms),
    )


def nightly(
    conn: sqlite3.Connection,
    cfg: Config,
    strat: StrategyConfig,
    spec: Any,
    now_ms: int,
    *,
    review_fn: ReviewFn | None = None,
    ideas_fn: IdeasFn | None = None,
    frame: Any = None,
    night: str | None = None,
) -> dict[str, Any]:
    """One learning pass. Each step is isolated: a failure is recorded and the rest still runs."""
    night = night or night_of(now_ms)
    mark_started(conn, night, now_ms)
    summary: dict[str, Any] = {"night": night}

    def step(name: str, fn: Callable[[], Any]) -> Any:
        try:
            summary[name] = fn()
        except Exception as e:  # noqa: BLE001
            log.exception("learning step %s failed", name)
            summary[name] = f"error: {type(e).__name__}: {e}"[:200]
            return None
        return summary[name]

    step("scored", lambda: score_decisions(conn, cfg, strat, now_ms))
    step("paper", lambda: review_paper(conn, now_ms))
    if frame is None:
        try:
            from goldtrader.backtest.timemachine import load_frame

            frame = load_frame(conn, cfg)
        except Exception as e:  # noqa: BLE001
            summary["history"] = f"unavailable: {e}"[:200]
    ev = evidence(conn, cfg, strat, now_ms, frame)
    summary["evidence_groups"] = {
        "graded": ev["graded_decisions_last_30_days"]["signals_played_out"],
        "live_rule_signals": ev["live_rule_signals_code_only"].get("n", 0),
        "paper": len(ev["paper_candidates"]),
        "channels": len(ev["telegram_channels"]),
    }

    def review() -> Any:
        if review_fn is None:
            return "skipped: no model"
        items = review_fn(ev, playbook_rows(conn))
        if items is None:
            return "kept: the review did not answer"
        return {"lessons": replace_playbook(conn, night, items, now_ms)}

    def research() -> Any:
        if ideas_fn is None:
            return "skipped: no model"
        if frame is None:
            return "skipped: no history"
        tested = try_ideas(conn, cfg, spec, ideas_fn(ev, tried_ideas(conn)), night, now_ms, frame)
        added = auto_paper(conn, tested, now_ms)
        return {
            "tested": [{k: t.get(k) for k in ("kind", "what", "status")} for t in tested],
            "to_paper": added,
        }

    step("review", review)
    step("research", research)
    failed = any(isinstance(v, str) and v.startswith("error:") for v in summary.values())
    conn.execute(
        "UPDATE learning_runs SET finished_ms=?, status=?, summary_json=? WHERE night=?",
        (now_ms, "failed" if failed else "done", json.dumps(summary, default=str), night),
    )
    return summary


def status(conn: sqlite3.Connection, now_ms: int) -> dict[str, Any]:
    """Read-only view for `goldctl learn-status` and the dashboard."""
    if not _table(conn, "learning_runs"):
        return {}
    last = conn.execute("SELECT * FROM learning_runs ORDER BY night DESC LIMIT 1").fetchone()
    graded = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(r IS NOT NULL),0) FROM decision_scores"
    ).fetchone()
    ideas = [
        dict(r)
        for r in conn.execute(
            "SELECT night, what, status, paper_bundle_id FROM strategy_ideas ORDER BY id DESC "
            "LIMIT 12"
        )
    ]
    return {
        "last_run": (
            {
                "night": last["night"],
                "status": last["status"],
                "finished_ms": last["finished_ms"],
                "summary": json.loads(last["summary_json"] or "{}"),
            }
            if last
            else None
        ),
        "signals_seen": int(graded[0] or 0),
        "signals_graded": int(graded[1] or 0),
        "calibration": calibration_line(conn, now_ms),
        "playbook": playbook_rows(conn),
        "ideas": ideas,
        "ideas_total": conn.execute("SELECT COUNT(*) FROM strategy_ideas").fetchone()[0],
        "ideas_held": conn.execute(
            "SELECT COUNT(*) FROM strategy_ideas WHERE status='holds'"
        ).fetchone()[0],
    }

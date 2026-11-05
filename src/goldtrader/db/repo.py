"""Thin repository functions for Phase 3 tables. No ORM; explicit SQL."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from dataclasses import asdict
from typing import Any

from goldtrader.db.connect import now_ms
from goldtrader.types import AccountInfo, Bar, SymbolSpec


def _stable(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


class Repo:
    def __init__(self, conn: sqlite3.Connection):
        self.c = conn

    # --- config -------------------------------------------------------------------------
    def record_config(self, config_hash: str, owner_hash: str, source: str, body: dict) -> None:
        self.c.execute(
            "INSERT OR IGNORE INTO config_versions(config_hash, owner_hash, source, body_json, "
            "created_ms) VALUES (?,?,?,?,?)",
            (config_hash, owner_hash, source, _stable(body), now_ms()),
        )

    # --- specs --------------------------------------------------------------------------
    def latest_spec(self, symbol: str) -> SymbolSpec | None:
        row = self.c.execute(
            "SELECT spec_json FROM symbol_specs WHERE symbol=? ORDER BY read_ms DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        if row is None:
            return None
        d = json.loads(row["spec_json"])
        d.pop("CRITICAL", None)
        return SymbolSpec(**d)

    def record_spec(self, spec: SymbolSpec, changed: list[str], critical: bool) -> str:
        d = asdict(spec)
        d.pop("CRITICAL", None)
        h = hashlib.sha256(_stable(d).encode()).hexdigest()[:16]
        self.c.execute(
            "INSERT INTO symbol_specs(symbol, read_ms, spec_json, spec_hash, critical_change, "
            "changed_fields) VALUES (?,?,?,?,?,?)",
            (spec.name, now_ms(), _stable(d), h, int(critical), ",".join(changed) or None),
        )
        return h

    def record_account(self, a: AccountInfo) -> None:
        self.c.execute(
            "INSERT INTO account_snapshots(read_ms, login, server, company, currency, trade_mode, "
            "leverage, margin_mode, balance, equity, margin, margin_free, margin_level, profit, "
            "trade_allowed, trade_expert) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now_ms(), a.login, a.server, a.company, a.currency, a.trade_mode, a.leverage,
             a.margin_mode, a.balance, a.equity, a.margin, a.margin_free, a.margin_level, a.profit,
             int(a.trade_allowed), int(a.trade_expert)),
        )

    # --- bars ---------------------------------------------------------------------------
    def upsert_bars(self, symbol: str, bars: Iterable[Bar], source: str) -> int:
        rows = [
            (symbol, b.seconds, b.time, b.bid_open, b.bid_high, b.bid_low, b.bid_close,
             b.ask_open, b.ask_high, b.ask_low, b.ask_close, b.ticks, source)
            for b in bars
        ]
        with self.c:
            self.c.executemany(
                "INSERT OR REPLACE INTO bars(symbol, seconds, time_s, bid_open, bid_high, bid_low, "
                "bid_close, ask_open, ask_high, ask_low, ask_close, ticks, source) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        return len(rows)

    def load_bars(self, symbol: str, seconds: int, start_s: int | None = None,
                  end_s: int | None = None) -> list[Bar]:
        q = "SELECT * FROM bars WHERE symbol=? AND seconds=?"
        args: list[Any] = [symbol, seconds]
        if start_s is not None:
            q += " AND time_s>=?"
            args.append(start_s)
        if end_s is not None:
            q += " AND time_s<?"
            args.append(end_s)
        q += " ORDER BY time_s"
        return [
            Bar(time=r["time_s"], seconds=r["seconds"], bid_open=r["bid_open"],
                bid_high=r["bid_high"], bid_low=r["bid_low"], bid_close=r["bid_close"],
                ask_open=r["ask_open"], ask_high=r["ask_high"], ask_low=r["ask_low"],
                ask_close=r["ask_close"], ticks=r["ticks"])
            for r in self.c.execute(q, args)
        ]

    def bar_gaps(self, symbol: str, seconds: int, expected_open: Iterable[int]) -> list[int]:
        """Return expected bar times (epoch s) with no stored bar — the data soak check."""
        have = {r["time_s"] for r in self.c.execute(
            "SELECT time_s FROM bars WHERE symbol=? AND seconds=?", (symbol, seconds))}
        return [t for t in expected_open if t not in have]

    # --- snapshots, bundles, ledger -------------------------------------------------------
    def record_data_snapshot(self, snapshot_id: str, description: str, symbol: str, seconds: int,
                             start_s: int, end_s: int, n_bars: int) -> None:
        self.c.execute(
            "INSERT OR IGNORE INTO data_snapshots(snapshot_id, description, symbol, seconds, "
            "start_s, end_s, n_bars, created_ms) VALUES (?,?,?,?,?,?,?,?)",
            (snapshot_id, description, symbol, seconds, start_s, end_s, n_bars, now_ms()),
        )

    def record_bundle(self, bundle_id: str, strategy_id: str, strategy_kind: str, params: dict,
                      config_hash: str, parent: str | None = None, code_commit: str | None = None,
                      prompt_hashes: dict | None = None, model_ids: dict | None = None) -> None:
        self.c.execute(
            "INSERT OR IGNORE INTO bundles(bundle_id, parent_bundle_id, code_commit, strategy_id, "
            "strategy_kind, params_json, prompt_hashes_json, model_ids_json, config_hash, "
            "created_ms) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (bundle_id, parent, code_commit, strategy_id, strategy_kind, _stable(params),
             _stable(prompt_hashes or {}), _stable(model_ids or {}), config_hash, now_ms()),
        )

    def append_trial(self, **row: Any) -> int:
        """Append-only: there is deliberately no update or delete method (R-031)."""
        cols = ["created_ms", "bundle_id", "change_type", "description", "data_snapshot_id",
                "cost_model_json", "n_trades", "n_days", "sharpe", "sortino", "calmar",
                "net_return_pct", "max_drawdown_pct", "worst_day_pct", "skew", "kurt", "pbo", "dsr",
                "haircut_sr", "perm_p", "created_by", "status", "report_path"]
        row.setdefault("created_ms", now_ms())
        if isinstance(row.get("cost_model_json"), dict):
            row["cost_model_json"] = _stable(row["cost_model_json"])
        values = [row.get(c) for c in cols]
        cur = self.c.execute(
            f"INSERT INTO trial_ledger({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            values,
        )
        return int(cur.lastrowid)

    def trial_count(self, since_ms: int = 0) -> int:
        return int(self.c.execute(
            "SELECT COUNT(*) FROM trial_ledger WHERE created_ms>=?", (since_ms,)).fetchone()[0])

    def trial_sharpes(self, since_ms: int = 0) -> list[float]:
        return [r[0] for r in self.c.execute(
            "SELECT sharpe FROM trial_ledger WHERE created_ms>=? AND sharpe IS NOT NULL",
            (since_ms,))]

    def record_backtest_run(self, run_id: str, bundle_id: str, data_snapshot_id: str, profile: str,
                            params: dict, metrics: dict | None, trades_path: str | None,
                            equity_path: str | None, started_ms: int) -> None:
        self.c.execute(
            "INSERT OR REPLACE INTO backtest_runs(run_id, bundle_id, data_snapshot_id, profile, "
            "started_ms, finished_ms, params_json, metrics_json, trades_path, equity_path) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, bundle_id, data_snapshot_id, profile, started_ms, now_ms(), _stable(params),
             _stable(metrics) if metrics is not None else None, trades_path, equity_path),
        )

    # --- ops ----------------------------------------------------------------------------
    def heartbeat(self, process: str, status: str, details: dict | None = None) -> None:
        self.c.execute(
            "INSERT INTO heartbeats(process, last_ms, status, details_json) VALUES (?,?,?,?) "
            "ON CONFLICT(process) DO UPDATE SET last_ms=excluded.last_ms, status=excluded.status, "
            "details_json=excluded.details_json",
            (process, now_ms(), status, _stable(details or {})),
        )

    def alert(self, severity: str, kind: str, message: str, details: dict | None = None) -> int:
        cur = self.c.execute(
            "INSERT INTO alerts(created_ms, severity, kind, message, details_json) VALUES (?,?,?,?,?)",
            (now_ms(), severity, kind, message, _stable(details or {})),
        )
        return int(cur.lastrowid)

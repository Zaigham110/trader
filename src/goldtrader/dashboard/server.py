"""Loopback HTTP server for the dashboard (same shape as `gateway/api.py`, standard library only).

It answers GET for a handful of paths from a database opened with `mode=ro`, and refuses any
request whose `Host` header is not this PC (so another website cannot read the page through DNS
rebinding). It binds to loopback only.

The one thing it can write is a question for the Chat bot (D-057): `POST /api/chat` inserts a row
into `dashboard_chat_inbox` through a connection whose SQLite authorizer denies everything else.
The brain answers it; this process never calls a model, the gateway's write API or MT5.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from goldtrader import strategy_docs
from goldtrader.dashboard import data

LOOPBACK = ("127.0.0.1", "localhost", "::1")
STATIC = Path(__file__).parent / "static"
PAGE = STATIC / "index.html"
# The only files this server hands out, by exact request path (D-083): nothing in the request
# reaches the file system, so there is nothing to traverse. The chart library is a copy kept in
# the repository; the page loads nothing from outside this PC.
ASSETS: dict[str, tuple[str, str]] = {
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/legacy.js": ("legacy.js", "text/javascript; charset=utf-8"),
    "/pages.js": ("pages.js", "text/javascript; charset=utf-8"),
    "/vendor/lightweight-charts.js": (
        "vendor/lightweight-charts.standalone.production.js",
        "text/javascript; charset=utf-8",
    ),
}
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'"
    ),
}


def connect_readonly(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


INBOX = "dashboard_chat_inbox"
# Time machine parameters the page may ask for (the same lists as backtest/timemachine.py,
# repeated here because this process must not import the backtest or risk code).
TM_DAYS = (7, 14, 30, 60, 0)
TM_TIMINGS = ("every_bar", "live_5min")
TM_SPREADS = (1.0, 1.5, 2.0)
TM_ID = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")
MAX_QUESTION_CHARS = 2000
MAX_WAITING = 3


def connect_inbox(db_path: Path | str) -> sqlite3.Connection:
    """A connection that can INSERT into the chat inbox and read it back; nothing else."""
    conn = sqlite3.connect(str(Path(db_path).resolve()), timeout=5, isolation_level=None)

    def authorizer(action: int, a1: Any, a2: Any, _db: Any, _src: Any) -> int:
        if action in (sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SELECT):
            return sqlite3.SQLITE_OK
        if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_READ) and a1 == INBOX:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_UPDATE and a1 == "sqlite_sequence":  # AUTOINCREMENT counter
            return sqlite3.SQLITE_OK
        if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_READ) and a1 == "sqlite_sequence":
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    conn.set_authorizer(authorizer)
    return conn


def _origin_ok(header: str | None) -> bool:
    """A browser sends Origin on POST; it must be this page. No header = not a browser."""
    if header is None:
        return True
    parts = urlsplit(header)
    return parts.scheme == "http" and _host_ok(parts.netloc)


def _host_ok(header: str | None) -> bool:
    if not header:
        return False
    host = header.strip().lower()
    if host.startswith("["):  # [::1]:8760
        host = host[1:].split("]")[0]
    else:
        host = host.rsplit(":", 1)[0] if ":" in host else host
    return host in LOOPBACK


class DashboardServer:
    def __init__(
        self,
        db_path: Path | str,
        gateway: Any = None,
        *,
        symbol: str = "XAUUSDm",
        host: str = "127.0.0.1",
        port: int = 8760,
        clock_ms: Callable[[], int] | None = None,
        config_path: Path | str | None = None,
        strategies: tuple[str, ...] = (),
        spawn: Callable[[list[str]], Any] | None = None,
        strategy_configs: list[dict[str, Any]] | None = None,
        risk: dict[str, Any] | None = None,
        instance: str = "demo",
    ) -> None:
        if host not in LOOPBACK:
            raise ValueError("dashboard binds to loopback only")
        self.db_path = Path(db_path)
        self.gateway = gateway
        self.symbol = symbol
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.config_path = Path(config_path) if config_path is not None else None
        self.strategies = tuple(strategies)
        # shown on the Strategies and Risk pages: what is configured, never a secret
        self.strategy_configs = list(strategy_configs or [])
        self.risk = dict(risk or {})
        self.instance = instance
        self._spawn = spawn or _spawn_detached
        self._tm_proc: Any = None
        self._tm_lock = threading.Lock()
        srv = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "goldtrader-dashboard/0.1"

            def log_message(self, fmt, *args):  # quiet
                return

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in SECURITY_HEADERS.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, body: Any) -> None:
                self._send(code, json.dumps(body, default=str).encode(), "application/json")

            def do_GET(self):
                if not _host_ok(self.headers.get("Host")):
                    return self._json(403, {"error": "forbidden_host"})
                url = urlsplit(self.path)
                try:
                    if url.path in ("/", "/index.html"):
                        return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
                    if url.path in ASSETS:
                        name, ctype = ASSETS[url.path]
                        f = STATIC / name
                        if not f.is_file():
                            return self._json(404, {"error": "not_found"})
                        return self._send(200, f.read_bytes(), ctype)
                    if url.path == "/api/snapshot":
                        return self._json(200, srv.snapshot())
                    if url.path == "/api/live":
                        return self._json(200, srv.live())
                    if url.path == "/api/chat":
                        return self._json(200, srv.chat(_int(parse_qs(url.query), "after", 0)))
                    if url.path == "/api/timemachine":
                        q = parse_qs(url.query)
                        if "id" in q:
                            one = srv.timemachine_get(_int(q, "id", 0))
                            if one is None:
                                return self._json(404, {"error": "not_found"})
                            return self._json(200, one)
                        return self._json(200, srv.timemachine_list())
                    if url.path == "/api/meta":
                        return self._json(200, {"instance": srv.instance, "symbol": srv.symbol})
                    if url.path == "/api/strategies":
                        return self._json(200, srv.strategies_info())
                    if url.path == "/api/trades":
                        q = parse_qs(url.query)
                        return self._json(200, srv.read(data.trades_page, _str(q, "period", "all")))
                    if url.path == "/api/trade":
                        one = srv.read_symbol(
                            data.trade_detail, _str(parse_qs(url.query), "id", "")
                        )
                        if one is None:
                            return self._json(404, {"error": "not_found"})
                        return self._json(200, one)
                    if url.path == "/api/bars":
                        q = parse_qs(url.query)
                        return self._json(
                            200,
                            srv.bars(_int(q, "minutes", 1), _int(q, "limit", 240)),
                        )
                except sqlite3.Error as e:
                    return self._json(503, {"error": "database_unavailable", "detail": str(e)})
                return self._json(404, {"error": "not_found"})

            def _refuse(self):
                return self._json(405, {"error": "read_only"})

            def _drain(self) -> bytes | None:
                """Read the request body before answering, even when the answer is a refusal:
                closing the socket on an unread body resets the connection on Windows and the
                browser never sees the status. None = missing, unparseable or over-long length."""
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = -1
                if not 0 < n <= 4 * MAX_QUESTION_CHARS + 64:
                    self.close_connection = True
                    return None
                return self.rfile.read(n)

            def _refuse_write(self):
                self._drain()
                return self._refuse()

            def do_POST(self):
                raw = self._drain()
                path = urlsplit(self.path).path
                if path not in ("/api/chat", "/api/timemachine"):
                    return self._refuse()
                # Host + Origin + a header a plain HTML form cannot set: no other site can post here
                if not _host_ok(self.headers.get("Host")) or not _origin_ok(
                    self.headers.get("Origin")
                ):
                    return self._json(403, {"error": "forbidden_host"})
                if self.headers.get("X-Dashboard") != "1":
                    return self._json(403, {"error": "missing_header"})
                if raw is None:
                    return self._json(413, {"error": "bad_length"})
                try:
                    body = json.loads(raw.decode("utf-8"))
                    if not isinstance(body, dict):
                        raise TypeError
                    if path == "/api/timemachine":
                        code, out = srv.timemachine_start(body)
                        return self._json(code, out)
                    text = body["text"].strip() if isinstance(body["text"], str) else ""
                except (ValueError, KeyError, TypeError):
                    return self._json(400, {"error": "bad_json"})
                if not text or len(text) > MAX_QUESTION_CHARS:
                    return self._json(400, {"error": "bad_text"})
                try:
                    code, out = srv.ask(text)
                except sqlite3.Error as e:
                    return self._json(503, {"error": "database_unavailable", "detail": str(e)})
                return self._json(code, out)

            do_PUT = do_DELETE = do_PATCH = _refuse_write  # noqa: N815

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ data
    def snapshot(self) -> dict[str, Any]:
        conn = connect_readonly(self.db_path)
        try:
            return data.snapshot(conn, self.gateway, self.clock_ms(), self.symbol)
        finally:
            conn.close()

    def read(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run one `data` reader that takes (conn, now_ms, ...) on a read-only connection."""
        conn = connect_readonly(self.db_path)
        try:
            return fn(conn, self.clock_ms(), *args)
        finally:
            conn.close()

    def read_symbol(self, fn: Callable[..., Any], *args: Any) -> Any:
        """The same for a reader that takes (conn, symbol, ...)."""
        conn = connect_readonly(self.db_path)
        try:
            return fn(conn, self.symbol, *args)
        finally:
            conn.close()

    def strategies_info(self) -> dict[str, Any]:
        """The rules in plain words, what is configured, and the results so far."""
        conn = connect_readonly(self.db_path)
        try:
            return {
                "docs": strategy_docs.DOCS,
                "common": strategy_docs.COMMON,
                "configured": self.strategy_configs,
                "risk": self.risk,
                "instance": self.instance,
                "paper": data.paper(conn, 12),
                "results": data.results_by_bundle(conn),
            }
        finally:
            conn.close()

    def bars(self, minutes: int, limit: int) -> dict[str, Any]:
        conn = connect_readonly(self.db_path)
        try:
            return data.bars(conn, self.symbol, minutes, limit)
        finally:
            conn.close()

    def live(self) -> dict[str, Any]:
        conn = connect_readonly(self.db_path)
        try:
            return data.live(conn, self.gateway, self.clock_ms())
        finally:
            conn.close()

    def chat(self, after_id: int) -> dict[str, Any]:
        conn = connect_readonly(self.db_path)
        try:
            return data.chat(conn, after_id)
        finally:
            conn.close()

    def ask(self, text: str) -> tuple[int, dict[str, Any]]:
        """Queue one question for the brain's Chat bot. Returns (http status, body)."""
        ro = connect_readonly(self.db_path)
        try:
            state = data.chat(ro, 0, 1)
        finally:
            ro.close()
        if not state["ready"]:
            return 503, {"error": "brain_not_updated"}
        if len(state["waiting"]) >= MAX_WAITING:
            return 429, {"error": "too_many_waiting"}
        conn = connect_inbox(self.db_path)
        try:
            cur = conn.execute(
                f"INSERT INTO {INBOX}(created_ms, text) VALUES (?, ?)",  # noqa: S608 - constant
                (self.clock_ms(), text),
            )
            return 200, {"id": cur.lastrowid}
        finally:
            conn.close()

    # ------------------------------------------------------------------ time machine
    def timemachine_list(self) -> dict[str, Any]:
        conn = connect_readonly(self.db_path)
        try:
            runs = data.timemachine_list(conn, self.clock_ms())
        finally:
            conn.close()
        return {
            "runs": runs,
            "busy": self._tm_busy(),
            "available": self.config_path is not None,
            "strategies": list(self.strategies),
            "choices": {
                "days": list(TM_DAYS),
                "timing": list(TM_TIMINGS),
                "spread": list(TM_SPREADS),
            },
        }

    def timemachine_get(self, run_id: int) -> dict[str, Any] | None:
        conn = connect_readonly(self.db_path)
        try:
            return data.timemachine_get(conn, run_id)
        finally:
            conn.close()

    def _tm_busy(self) -> bool:
        p = self._tm_proc
        return p is not None and p.poll() is None

    def timemachine_start(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Start one replay as a separate `goldctl timemachine` process. Every parameter must be
        one of the listed choices, so nothing from the request reaches the command line freely."""
        if self.config_path is None:
            return 503, {"error": "not_available"}
        days, timing, spread = body.get("days"), body.get("timing"), body.get("spread_mult")
        strategy = body.get("strategy_id")
        if isinstance(days, bool) or days not in TM_DAYS or timing not in TM_TIMINGS:
            return 400, {"error": "bad_parameters"}
        if isinstance(spread, bool) or spread not in TM_SPREADS:
            return 400, {"error": "bad_parameters"}
        if strategy is not None and (
            not isinstance(strategy, str)
            or not TM_ID.match(strategy)
            or strategy not in self.strategies
        ):
            return 400, {"error": "bad_parameters"}
        cmd = [
            sys.executable,
            "-c",
            "from goldtrader.cli import app; app()",
            "timemachine",
            "--config",
            str(self.config_path),
            "--db",
            str(self.db_path),
            "--days",
            str(int(days)),
            "--timing",
            str(timing),
            "--spread-mult",
            str(float(spread)),
        ]
        if strategy is not None:
            cmd += ["--strategy-id", strategy]
        with self._tm_lock:
            if self._tm_busy():
                return 429, {"error": "already_running"}
            self._tm_proc = self._spawn(cmd)
        return 202, {"started": True}

    # ------------------------------------------------------------------ lifecycle
    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="dashboard"
        )
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _spawn_detached(cmd: list[str]) -> Any:
    """Run the replay outside this process: it imports the backtest and risk code, which the
    dashboard itself must never load."""
    return subprocess.Popen(  # noqa: S603 - fixed argv built from whitelisted values
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL
    )


def _int(q: dict[str, list[str]], key: str, default: int) -> int:
    try:
        return int(q[key][0])
    except (KeyError, IndexError, ValueError):
        return default


def _str(q: dict[str, list[str]], key: str, default: str) -> str:
    try:
        return q[key][0][:80]
    except (KeyError, IndexError):
        return default

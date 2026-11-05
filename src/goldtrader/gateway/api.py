"""Localhost intents API for the gateway (ARCHITECTURE §2.1 `api`, §6).

Standard-library HTTP server bound to 127.0.0.1 only; every request must carry the shared token in
the `X-Gateway-Token` header. Bodies are validated against the pydantic contracts before anything
touches the handler. Not exposed beyond the PC (D-026).
"""

from __future__ import annotations

import hmac
import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pydantic import ValidationError

from goldtrader.contracts import GatewayState, Intent, IntentResult

StateFn = Callable[[], GatewayState | None]
IntentFn = Callable[[Intent], IntentResult]
HealthFn = Callable[[], dict]


class GatewayAPI:
    def __init__(
        self,
        token: str,
        state_fn: StateFn,
        intent_fn: IntentFn,
        health_fn: HealthFn,
        host: str = "127.0.0.1",
        port: int = 8750,
    ) -> None:
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("gateway API binds to loopback only")
        if len(token) < 16:
            raise ValueError("gateway token must be at least 16 characters")
        self._token = token
        api = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "goldtrader-gateway/0.1"

            def log_message(self, fmt, *args):  # quiet; the gateway has its own logger
                return

            def _auth(self) -> bool:
                given = self.headers.get("X-Gateway-Token", "")
                return hmac.compare_digest(given, api._token)

            def _send(self, code: int, body: dict) -> None:
                data = json.dumps(body, default=str).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if not self._auth():
                    return self._send(401, {"error": "unauthorized"})
                if self.path == "/state":
                    st = state_fn()
                    return self._send(200, st.model_dump() if st else {"error": "no_state"})
                if self.path == "/health":
                    return self._send(200, health_fn())
                return self._send(404, {"error": "not_found"})

            def do_POST(self):
                if not self._auth():
                    return self._send(401, {"error": "unauthorized"})
                if self.path != "/intents":
                    return self._send(404, {"error": "not_found"})
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length > 64_000:
                    return self._send(413, {"error": "too_large"})
                raw = self.rfile.read(length) if length else b""
                try:
                    intent = Intent.model_validate_json(raw)
                except ValidationError as e:
                    return self._send(400, {"error": "invalid_intent", "detail": e.errors()})
                result = intent_fn(intent)
                return self._send(200, result.model_dump())

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="gateway-api"
        )
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class GatewayClient:
    """Brain-side client (stdlib only)."""

    def __init__(self, base_url: str, token: str, timeout_s: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_s = timeout_s

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        import urllib.error
        import urllib.request

        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"X-Gateway-Token": self.token, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return {"error": f"http_{e.code}", "detail": e.read().decode(errors="replace")}
        except (urllib.error.URLError, OSError, ValueError) as e:  # refused, timeout, bad JSON
            return {"error": "unreachable", "detail": str(e)}

    def state(self) -> GatewayState | None:
        d = self._call("GET", "/state")
        return None if "error" in d else GatewayState.model_validate(d)

    def health(self) -> dict:
        return self._call("GET", "/health")

    def send_intent(self, intent: Intent) -> IntentResult:
        d = self._call("POST", "/intents", intent.model_dump())
        if "error" in d:
            return IntentResult(intent_id=intent.intent_id, status="failed", detail=str(d))
        return IntentResult.model_validate(d)

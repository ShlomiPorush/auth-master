"""ASGI middleware: the request gate of database switches, and what works in each mode.

Every HTTP request except static files passes the gate (app.db_runtime.Gate): while a
switch holds it closed, the request waits up to GATE_WAIT_SEC and then gets 503.

In setup mode only first-time setup works: pages redirect to /setup, other API calls get
503 {"code": "setup_required"}.  In recovery mode only /health and the recovery page work:
pages redirect to /recovery, other calls get 503 with the failure code.
"""
from __future__ import annotations

import json
from typing import Any

from app import db_runtime

STATIC_PREFIXES = ("/css/", "/js/", "/fonts/", "/images/")
PAGES = frozenset({"/", "/login", "/login-mfa", "/setup", "/setup-mfa", "/recovery", "/docs", "/redoc"})

SETUP_ALLOWED = frozenset({"/health", "/setup", "/admin/api/setup/status"})
SETUP_PREFIXES = ("/admin/api/setup/database",)
RECOVERY_ALLOWED = frozenset({"/health", "/recovery"})
# The setup page follows its job into recovery mode (a switch that failed after saving).
RECOVERY_PREFIXES = ("/admin/api/recovery/", "/admin/api/setup/database/jobs/")


def _route_path(scope: dict[str, Any]) -> str:
    path = scope.get("path", "")
    root = scope.get("root_path", "")
    if root and (path == root or path.startswith(root + "/")):
        path = path[len(root):] or "/"
    return path


def _is_page(path: str) -> bool:
    return path in PAGES or (path.startswith("/admin") and not path.startswith("/admin/api"))


async def _send_json(send: Any, status: int, body: dict[str, Any], headers: list[tuple[bytes, bytes]] | None = None) -> None:
    data = json.dumps(body).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(data)).encode()),
            (b"cache-control", b"no-store"),
            *(headers or []),
        ],
    })
    await send({"type": "http.response.body", "body": data})


async def _redirect(send: Any, scope: dict[str, Any], target: str) -> None:
    location = (scope.get("root_path", "") + target).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": 302,
        "headers": [(b"location", location), (b"content-length", b"0"), (b"cache-control", b"no-store")],
    })
    await send({"type": "http.response.body", "body": b""})


class DatabaseGateMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = _route_path(scope)
        runtime: db_runtime.Runtime | None = getattr(getattr(scope.get("app"), "state", None), "runtime", None)
        if runtime is None or path.startswith(STATIC_PREFIXES):
            await self.app(scope, receive, send)
            return

        if not await runtime.gate.enter(db_runtime.GATE_WAIT_SEC):
            await _send_json(
                send, 503,
                {"detail": "The service is switching its database. Try again in a few seconds.", "code": "database_switching"},
                [(b"retry-after", b"5")],
            )
            return
        try:
            mode = runtime.mode
            if mode == db_runtime.SETUP and not (path in SETUP_ALLOWED or path.startswith(SETUP_PREFIXES)):
                if scope.get("method") == "GET" and _is_page(path):
                    await _redirect(send, scope, "/setup")
                else:
                    await _send_json(send, 503, {
                        "detail": "Setup is not complete. Open /setup to choose a database.",
                        "code": "setup_required",
                    })
                return
            if mode == db_runtime.RECOVERY and not (path in RECOVERY_ALLOWED or path.startswith(RECOVERY_PREFIXES)):
                if scope.get("method") == "GET" and _is_page(path):
                    await _redirect(send, scope, "/recovery")
                else:
                    await _send_json(send, 503, {
                        "detail": f"{runtime.failure_text} An administrator can fix the connection at /recovery.",
                        "code": runtime.failure_code or "database_unavailable",
                    })
                return
            await self.app(scope, receive, send)
        finally:
            runtime.gate.leave()

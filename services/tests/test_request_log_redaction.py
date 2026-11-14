"""uvicorn's request log never shows the query string of /validate.

GET /validate takes the token in the query string, and uvicorn logs the path with
its query string: HTTP requests on ``uvicorn.access`` and WebSocket handshakes on
``uvicorn.error``, both in the container output. A filter on these loggers replaces
the query of /validate with ``[redacted]``.

The end-to-end tests start the app the two ways it ships: the Dockerfile command
(``python -m uvicorn app.main:app``, which configures logging before importing the
app) and ``python -m app.main`` (which imports the app first, then configures
logging). Each server runs in its own process on a port the system picks, with an
environment built here from scratch: every secret is generated per run with
``secrets``, the database is a temporary SQLite file, and the working directory is
a temporary directory, so no local .env is read. Redis is not running: /validate
does not use it. Requires pytest.
"""
from __future__ import annotations

import base64
import http.client
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

SERVICES_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICES_DIR))

from app.config import KNOWN_PUBLIC_VALUES  # noqa: E402
from app.request_log import RedactValidateQuery  # noqa: E402

# uvicorn 0.54 logs HTTP requests and WebSocket handshakes with these formats.
_HTTP_FORMAT = '%s - "%s %s HTTP/%s" %d'
_WEBSOCKET_FORMAT = '%s - "WebSocket %s" 403'


def _record(method: str, path: str, status: int = 200) -> logging.LogRecord:
    args = ("127.0.0.1:50000", method, path, "1.1", status)
    return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, _HTTP_FORMAT, args, None)


def _filtered_message(record: logging.LogRecord) -> str:
    assert RedactValidateQuery().filter(record) is True
    return record.getMessage()


@pytest.mark.parametrize(
    ("method", "path", "status", "expected"),
    [
        ("GET", "/validate?token=tok_secret&area=orders&level=read", 200, "/validate?[redacted]"),
        ("HEAD", "/validate?token=tok_secret", 405, "/validate?[redacted]"),
        ("POST", "/validate?token=tok_secret", 422, "/validate?[redacted]"),
        ("GET", "/validate/?token=tok_secret", 307, "/validate/?[redacted]"),
        ("GET", "/validate//?token=tok_secret", 307, "/validate//?[redacted]"),
        ("GET", "/auth/validate?token=tok_secret", 200, "/auth/validate?[redacted]"),
    ],
)
def test_validate_query_is_redacted(method, path, status, expected):
    message = _filtered_message(_record(method, path, status))
    assert "tok_secret" not in message
    assert f'"{method} {expected} HTTP/1.1" {status}' in message


def test_websocket_handshake_query_is_redacted():
    args = ("127.0.0.1:50000", "/validate?token=tok_secret&area=orders&level=read")
    record = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, _WEBSOCKET_FORMAT, args, None)
    assert _filtered_message(record) == '127.0.0.1:50000 - "WebSocket /validate?[redacted]" 403'


@pytest.mark.parametrize(
    "path",
    [
        "/validate",
        "/validate/grants",
        "/admin/logs/access?token=0b8f6c1e-0000-4000-8000-000000000000",
        "/admin/api/tokens?limit=10",
        "/validated?token=x",
    ],
)
def test_other_lines_are_unchanged(path):
    record = _record("GET", path)
    before = record.getMessage()
    assert _filtered_message(record) == before


def test_records_without_a_tuple_of_arguments_are_unchanged():
    record = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, "Started server process [%(pid)d]", None, None)
    record.args = {"pid": 7}
    assert _filtered_message(record) == "Started server process [7]"


# --- End to end ------------------------------------------------------------------


def _generated(nbytes: int = 32) -> str:
    value = secrets.token_urlsafe(nbytes)
    if value in KNOWN_PUBLIC_VALUES:
        raise RuntimeError("generated a known public value")
    return value


def _server_env(tmp: str) -> dict[str, str]:
    # Built from scratch: nothing is taken from the caller's environment but what
    # starting Python needs.
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": tmp,
        "PYTHONPATH": str(SERVICES_DIR),
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PORT": "0",
        "DATABASE_URL": f"sqlite:///{tmp}/auth.db",
        "ADMIN_API_KEY": _generated(),
        "SESSION_SECRET": _generated(48),
        "APP_ENCRYPTION_KEY": secrets.token_hex(16),
        # Nothing listens there: /validate does not use Redis.
        "REDIS_URL": f"redis://:{secrets.token_hex(32)}@127.0.0.1:1/0",
    }
    if sys.platform == "win32":
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return env


def _request(port: int, method: str, path: str, body: dict | None = None, headers: dict | None = None) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        headers = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        resp.read()
        return resp.status
    finally:
        conn.close()


def _wait_for_port(proc: subprocess.Popen, log_path: Path) -> int:
    """The port uvicorn bound, from its start message; the app is then ready."""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        match = re.search(r"Uvicorn running on http://[^:\s]+:(\d+)", log_path.read_text(errors="replace"))
        if match:
            return int(match.group(1))
        if proc.poll() is not None:
            pytest.fail(f"server exited with {proc.returncode}:\n{log_path.read_text(errors='replace')}")
        time.sleep(0.2)
    pytest.fail(f"server did not start:\n{log_path.read_text(errors='replace')}")


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=15)


_ENTRYPOINTS = {
    # Dockerfile CMD, with the host changed to loopback and a port the system picks.
    "uvicorn-cli": [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "0"],
    # main.py's own entry point; it reads PORT.
    "python-main": [sys.executable, "-m", "app.main"],
}

_WEBSOCKET_HEADERS = {
    "Upgrade": "websocket",
    "Connection": "Upgrade",
    "Sec-WebSocket-Version": "13",
}


@pytest.mark.parametrize("entrypoint", sorted(_ENTRYPOINTS))
def test_server_output_never_contains_the_validate_token(entrypoint):
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        log_path = Path(tmp) / "server.log"
        tokens = [f"tok_{secrets.token_hex(24)}" for _ in range(4)]
        with open(log_path, "wb") as log:
            proc = subprocess.Popen(
                _ENTRYPOINTS[entrypoint],
                cwd=tmp,
                env=_server_env(tmp),
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                port = _wait_for_port(proc, log_path)
                query = "&area=orders&level=read"
                assert _request(port, "GET", f"/validate?token={tokens[0]}{query}") == 200
                assert _request(port, "GET", f"/validate/?token={tokens[1]}{query}") == 307
                assert _request(port, "GET", f"/validate//?token={tokens[2]}{query}") == 307
                ws_headers = {**_WEBSOCKET_HEADERS, "Sec-WebSocket-Key": base64.b64encode(secrets.token_bytes(16)).decode()}
                assert _request(port, "GET", f"/validate?token={tokens[3]}{query}", headers=ws_headers) == 403
                body = {"token": f"tok_{secrets.token_hex(24)}", "area": "orders", "level": "read"}
                assert _request(port, "POST", "/validate", body) == 200
            finally:
                _stop(proc)
        output = log_path.read_text(errors="replace")

    for token in tokens:
        assert token not in output, f"token written to the server output:\n{output}"
    assert '"GET /validate?[redacted] HTTP/1.1" 200' in output, output
    assert '"GET /validate/?[redacted] HTTP/1.1" 307' in output, output
    assert '"GET /validate//?[redacted] HTTP/1.1" 307' in output, output
    assert '"WebSocket /validate?[redacted]" 403' in output, output
    assert '"POST /validate HTTP/1.1" 200' in output, output

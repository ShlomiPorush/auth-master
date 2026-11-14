"""uvicorn's request log never shows the query string of /validate.

GET /validate takes the token in the query string, and uvicorn writes the path with
its query string to the ``uvicorn.access`` logger, which goes to the container
output. A filter on that logger replaces the query of /validate with ``[redacted]``.

The end-to-end tests start the app the two ways it ships: the Dockerfile command
(``python -m uvicorn app.main:app``, which configures logging before importing the
app) and ``python -m app.main`` (which imports the app first, then configures
logging). Each server runs in its own process with an environment built here from
scratch: every secret is generated per run with ``secrets``, the database is a
temporary SQLite file, and the working directory is a temporary directory, so no
local .env is read. Redis is not running: /validate does not use it, and readiness
is checked on /openapi.json, which does not need it. Requires pytest.
"""
from __future__ import annotations

import http.client
import json
import logging
import os
import secrets
import socket
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

# uvicorn 0.54 writes each request with this format and these arguments.
_UVICORN_FORMAT = '%s - "%s %s HTTP/%s" %d'


def _record(method: str, path: str, status: int = 200, args=None) -> logging.LogRecord:
    if args is None:
        args = ("127.0.0.1:50000", method, path, "1.1", status)
    return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, _UVICORN_FORMAT, args, None)


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


def test_records_of_another_shape_are_unchanged():
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "plain %s", ("/validate?token=x",), None)
    assert _filtered_message(record) == "plain /validate?token=x"


# --- End to end ------------------------------------------------------------------


def _generated(nbytes: int = 32) -> str:
    value = secrets.token_urlsafe(nbytes)
    if value in KNOWN_PUBLIC_VALUES:
        raise RuntimeError("generated a known public value")
    return value


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _server_env(tmp: str, port: int) -> dict[str, str]:
    # Built from scratch: nothing is taken from the caller's environment but what
    # starting Python needs.
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": tmp,
        "PYTHONPATH": str(SERVICES_DIR),
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PORT": str(port),
        "DATABASE_URL": f"sqlite:///{tmp}/auth.db",
        "ADMIN_API_KEY": _generated(),
        "SESSION_SECRET": _generated(48),
        "APP_ENCRYPTION_KEY": secrets.token_hex(16),
        # Nothing listens there: /validate and /openapi.json do not use Redis.
        "REDIS_URL": f"redis://:{secrets.token_hex(32)}@127.0.0.1:{_free_port()}/0",
    }
    if sys.platform == "win32":
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return env


def _request(port: int, method: str, path: str, body: dict | None = None) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        headers = {}
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


def _wait_ready(proc: subprocess.Popen, port: int, log_path: Path) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"server exited with {proc.returncode}:\n{log_path.read_text(errors='replace')}")
        try:
            if _request(port, "GET", "/openapi.json") == 200:
                return
        except OSError:
            pass
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
    # Dockerfile CMD, with the host changed to loopback.
    "uvicorn-cli": lambda port: [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
    # main.py's own entry point; it reads PORT.
    "python-main": lambda port: [sys.executable, "-m", "app.main"],
}


@pytest.mark.parametrize("entrypoint", sorted(_ENTRYPOINTS))
def test_server_output_never_contains_the_validate_token(entrypoint):
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        port = _free_port()
        log_path = Path(tmp) / "server.log"
        tokens = [f"tok_{secrets.token_hex(24)}" for _ in range(3)]
        with open(log_path, "wb") as log:
            proc = subprocess.Popen(
                _ENTRYPOINTS[entrypoint](port),
                cwd=tmp,
                env=_server_env(tmp, port),
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                _wait_ready(proc, port, log_path)
                query = "&area=orders&level=read"
                assert _request(port, "GET", f"/validate?token={tokens[0]}{query}") == 200
                assert _request(port, "GET", f"/validate/?token={tokens[1]}{query}") == 307
                assert _request(port, "GET", f"/validate//?token={tokens[2]}{query}") == 307
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
    assert '"POST /validate HTTP/1.1" 200' in output, output

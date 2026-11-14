"""Regression tests for the request body size cap.

Before the cap, every route read the whole request body and parsed it as JSON
before its own checks ran: an anonymous caller could send a body of any size to
``POST /admin/api/login``, ``POST /admin/api/bootstrap/register`` or a
rate-limited ``POST /validate`` and the service buffered and parsed all of it.
Starlette's ``RequestBodyLimitMiddleware`` now answers 413 when the
``Content-Length`` header is over the cap, and stops reading a body sent
without one as soon as the bytes received pass the cap.

Every secret the app reads is generated per run with ``secrets``.  The app is
imported inside the fixture, not at collection time, so whichever test module
imports it first fixes the cached settings; the fixture then refuses to run if
the effective settings hold an empty or placeholder secret.  The app always
opens this run's own temporary SQLite database, and Redis is replaced by an
in-memory fake; ``REDIS_URL`` carries a per-run random password because the app
requires one.

Requires pytest and httpx.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Importing the settings class does not read or cache any settings.
from app.config import Settings  # noqa: E402

_SECRET_FIELDS = ("admin_api_key", "session_secret", "app_encryption_key")
# Defaults in config.py, plus the example values in .env.example and docker-compose.yml.
_PLACEHOLDERS = {
    "",
    "generate-a-long-random-string",
    "at-least-32-characters-long-secret-key",
    "min-32-chars-secret-change-me!!",
} | {Settings.model_fields[f].default for f in _SECRET_FIELDS}

# Removed when the test process exits.
_TMP_DIR = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
_DATABASE_URL = f"sqlite:///{_TMP_DIR.name}/test.db"
_SECRETS = {
    "ADMIN_API_KEY": secrets.token_urlsafe(32),
    "SESSION_SECRET": secrets.token_urlsafe(48),
    "APP_ENCRYPTION_KEY": secrets.token_hex(16),
}
for _name, _value in _SECRETS.items():
    if _value in _PLACEHOLDERS:
        raise RuntimeError(f"{_name} is empty or a placeholder")
os.environ.update(_SECRETS)
os.environ["DATABASE_URL"] = _DATABASE_URL
os.environ["REDIS_URL"] = f"redis://:{secrets.token_hex(32)}@127.0.0.1:6379/0"
os.environ["RATE_LIMIT_MAX_REQ"] = "100000"

JSON = {"content-type": "application/json"}
# Over the 64 KiB cap, as in the audit's reproduction.
OVER_CAP = 70 * 1024


def _refuse_unsafe_settings(s: Settings) -> None:
    for field in _SECRET_FIELDS:
        if getattr(s, field) in _PLACEHOLDERS:
            raise RuntimeError(f"{field} is empty or a placeholder")


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, k):
        return self.data.get(k)

    async def setex(self, k, _ttl, v):
        self.data[k] = v

    async def delete(self, k):
        self.data.pop(k, None)

    async def ping(self):
        return True

    async def aclose(self):
        pass


@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient

    import app.main as main_mod
    from app import rate_limit
    from app.config import get_settings

    _refuse_unsafe_settings(get_settings())
    # Always this run's own database, whatever DATABASE_URL the cached settings
    # hold (another module may have imported the app first); the rows are wiped.
    create_database = main_mod.create_database
    monkeypatch.setattr(main_mod, "create_database", lambda _url: create_database(_DATABASE_URL))
    fake = FakeRedis()
    monkeypatch.setattr(main_mod.redis, "from_url", lambda *a, **k: fake)
    rate_limit._buckets.clear()
    with TestClient(main_mod.app, raise_server_exceptions=False) as c:
        c.app_module = main_mod
        c.db = main_mod.app.state.db
        # The key the app actually accepts (generated per run, checked above).
        c.api_auth = {"Authorization": f"Bearer {get_settings().admin_api_key}"}
        yield c
        c.portal.call(_wipe, c.db)
    rate_limit._buckets.clear()


async def _wipe(db):
    for table in ("tokens", "access_logs"):
        await db.execute(f"DELETE FROM {table}")


async def _count(db, table):
    return await db.fetchval(f"SELECT COUNT(*) FROM {table}")


def _padded(fields: dict, size: int) -> bytes:
    """A JSON object of exactly ``size`` bytes: ``fields`` plus a "pad"
    member, which the body models ignore, so the body is valid for the route."""
    base = json.dumps({**fields, "pad": ""}, separators=(",", ":")).encode()
    assert len(base) <= size
    return base[:-2] + b"A" * (size - len(base)) + b'"}'


def _login_body(size: int) -> bytes:
    return _padded({"username": "nobody", "password": "wrong"}, size)


def _chunks(body: bytes, size: int = 16384) -> list[bytes]:
    return [body[i:i + size] for i in range(0, len(body), size)]


def _call_streamed(client, path: str, chunks: list[bytes], headers=()):
    """Send ``chunks`` as separate ASGI body messages with no Content-Length,
    as a chunked upload arrives.  Returns (status, body, messages read)."""
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": path, "raw_path": path.encode(),
        "root_path": "", "query_string": b"",
        "headers": [(b"host", b"testserver"), (b"content-type", b"application/json"), *headers],
        "client": ("192.0.2.9", 1234), "server": ("testserver", 80),
    }
    queue = [
        {"type": "http.request", "body": c, "more_body": i < len(chunks) - 1}
        for i, c in enumerate(chunks)
    ]
    read = 0
    out: list[dict] = []

    async def receive():
        nonlocal read
        if queue:
            read += 1
            return queue.pop(0)
        await asyncio.sleep(3600)

    async def send(message):
        out.append(message)

    async def run():
        await client.app_module.app(scope, receive, send)

    client.portal.call(run)
    status = next(m["status"] for m in out if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in out if m["type"] == "http.response.body")
    return status, body, read


# ── Anonymous routes: rejected before any route check (accepted before the fix) ──


@pytest.mark.parametrize(
    "path",
    ["/admin/api/login", "/admin/api/bootstrap/register", "/validate"],
)
def test_body_over_cap_with_content_length_is_413(client, path):
    r = client.post(path, content=_login_body(OVER_CAP), headers=JSON)
    # The library answers in plain text when Content-Length gives the size.
    assert r.status_code == 413


def test_streamed_body_over_cap_is_413_and_reading_stops(client):
    chunks = _chunks(_login_body(1024 * 1024))
    status, body, read = _call_streamed(client, "/admin/api/login", chunks)
    assert status == 413
    assert json.loads(body) == {"detail": "Content Too Large"}
    # Reading stopped right after the cap: 64 KiB is four 16 KiB messages, the fifth passes it.
    assert read == 5 < len(chunks)


def test_body_at_cap_is_accepted(client):
    cap = client.app_module.MAX_REQUEST_BODY_BYTES
    assert cap == 64 * 1024
    r = client.post("/admin/api/login", content=_login_body(cap), headers=JSON)
    assert r.status_code == 401
    r = client.post("/admin/api/login", content=_login_body(cap + 1), headers=JSON)
    assert r.status_code == 413


# ── A rejected body changes nothing ──


def test_token_create_over_cap_creates_nothing(client):
    fields = {"name": "body-cap", "grants": [{"area": "orders", "level": "read"}]}
    big = _padded(fields, OVER_CAP)
    r = client.post("/tokens", content=big, headers={**JSON, **client.api_auth})
    assert r.status_code == 413
    auth = [(b"authorization", client.api_auth["Authorization"].encode())]
    status, _body, _read = _call_streamed(client, "/tokens", _chunks(big), auth)
    assert status == 413
    assert client.portal.call(_count, client.db, "tokens") == 0
    # Control: the same request under the cap creates the token.
    r = client.post("/tokens", content=_padded(fields, 1024), headers={**JSON, **client.api_auth})
    assert r.status_code == 201
    assert client.portal.call(_count, client.db, "tokens") == 1


def test_validate_over_cap_writes_no_access_log(client):
    fields = {"token": "tok_" + secrets.token_hex(8), "area": "orders", "level": "read"}
    r = client.post("/validate", content=_padded(fields, OVER_CAP), headers=JSON)
    assert r.status_code == 413
    status, _body, _read = _call_streamed(client, "/validate", _chunks(_padded(fields, OVER_CAP)))
    assert status == 413
    assert client.portal.call(_count, client.db, "access_logs") == 0
    # Control: the same request under the cap is answered and logged.
    r = client.post("/validate", content=_padded(fields, 1024), headers=JSON)
    assert r.status_code == 200
    assert r.json() == {"result": False}
    assert client.portal.call(_count, client.db, "access_logs") == 1


# ── Non-regression (passed before the fix too) ──


def test_streamed_small_body_is_unchanged(client):
    status, _body, _read = _call_streamed(client, "/admin/api/login", [b'{"username":"nobody",', b'"password":"wrong"}'])
    assert status == 401


def test_normal_login_is_unchanged(client):
    r = client.post("/admin/api/login", json={"username": "nobody", "password": "wrong"})
    assert r.status_code == 401
    assert r.json() == {"detail": "Invalid credentials"}

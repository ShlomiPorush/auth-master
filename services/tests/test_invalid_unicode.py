"""Regression tests for request text that is not valid UTF-8.

A JSON string escape such as ``"\\ud800"`` decodes to a lone surrogate: a valid
Python ``str`` with no UTF-8 encoding.

Bug 1: ``POST /validate`` answered 500 when ``token`` or ``area`` held one,
because hashing the token and writing ``area`` to the access log both encode the
text as UTF-8.

Bug 2: FastAPI's default validation error handler echoes the rejected input in
the 422 body, and rendering that body as UTF-8 failed, so any request with such
input that failed validation answered 500 instead of 422.

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

import json
import os
import secrets
import sys
import tempfile
import uuid
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
    # Report a server error as a 500 response, as a real client would see it.
    with TestClient(main_mod.app, raise_server_exceptions=False) as c:
        c.fake_redis = fake
        c.db = main_mod.app.state.db
        yield c
        c.portal.call(_wipe, c.db)
    rate_limit._buckets.clear()


async def _wipe(db):
    for table in ("tokens", "access_logs"):
        await db.execute(f"DELETE FROM {table}")


async def _insert_token(db, value, grants):
    from app.crypto_tokens import sha256_hex

    await db.execute(
        "INSERT INTO tokens (id, name, token_hash, grants) VALUES ($1, $2, $3, $4)",
        str(uuid.uuid4()), "unicode-test", sha256_hex(value), json.dumps(grants),
    )


async def _access_log_count(db):
    return await db.fetchval("SELECT COUNT(*) FROM access_logs")


# ── Bug 1: /validate with a lone surrogate (failed with 500 before the fix) ──


def test_validate_rejects_lone_surrogate_token(client):
    body = b'{"token": "\\ud800", "area": "orders", "level": "read"}'
    r = client.post("/validate", content=body, headers=JSON)
    assert r.status_code == 200
    assert r.json() == {"result": False}
    # Rejected before lookup, like an unknown level: no cache entry, no log row.
    assert client.fake_redis.data == {}
    assert client.portal.call(_access_log_count, client.db) == 0


def test_validate_rejects_lone_surrogate_area_with_422(client):
    # The area has a max_length, and Pydantic refuses text that is not valid Unicode
    # when it checks the length (error type string_unicode).
    body = b'{"token": "tok_unknown", "area": "\\ud800", "level": "read"}'
    r = client.post("/validate", content=body, headers=JSON)
    assert r.status_code == 422
    assert [e["type"] for e in r.json()["detail"]] == ["string_unicode"]
    assert client.fake_redis.data == {}
    assert client.portal.call(_access_log_count, client.db) == 0


# ── Bug 2: 422 echoing a lone surrogate (failed with 500 before the fix) ──


@pytest.mark.parametrize(
    ("body", "echoed"),
    [
        pytest.param(b'{"token": 5, "area": "\\ud800"}', {"token": 5, "area": "\ud800"}, id="value"),
        pytest.param(b'{"\\ud800": 1}', {"\ud800": 1}, id="key"),
        pytest.param(b'"\\ud800"', "\ud800", id="top-level-string"),
    ],
)
def test_validation_error_echoing_lone_surrogate_is_422(client, body, echoed):
    r = client.post("/validate", content=body, headers=JSON)
    assert r.status_code == 422
    detail = json.loads(r.content)["detail"]
    assert detail and all({"type", "loc", "msg", "input"} <= set(e) for e in detail)
    assert echoed in [e["input"] for e in detail]


# ── Non-regression (passed before the fix too) ──


def test_get_with_invalid_utf8_bytes_answers_false(client):
    # Starlette decodes the query string with replacement characters.
    r = client.get("/validate?token=%ED%A0%80&area=%ED%A0%80&level=read")
    assert r.status_code == 200
    assert r.json() == {"result": False}


def test_validation_error_for_ascii_input_is_unchanged(client):
    from starlette.responses import JSONResponse

    r = client.post("/validate", content=b'{"token": "x", "area": "orders"}', headers=JSON)
    assert r.status_code == 422
    assert r.headers["content-type"] == "application/json"
    assert r.json() == {
        "detail": [
            {
                "type": "missing",
                "loc": ["body", "level"],
                "msg": "Field required",
                "input": {"token": "x", "area": "orders"},
            }
        ]
    }
    # Byte for byte what FastAPI's default handler renders for this body.
    assert r.content == JSONResponse(content=r.json()).body


def test_validation_error_for_non_ascii_input_parses_the_same(client):
    body = {"token": "x", "area": "הזמנות"}
    r = client.post("/validate", content=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=JSON)
    assert r.status_code == 422
    assert r.json()["detail"][0]["input"] == body


def test_validate_grants_non_ascii_token_and_area(client):
    token = "טוקן-" + secrets.token_hex(8)
    client.portal.call(_insert_token, client.db, token, [{"area": "הזמנות", "level": "read"}])
    body = {"token": token, "area": "הזמנות", "level": "read"}
    r = client.post("/validate", content=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=JSON)
    assert r.status_code == 200
    assert r.json() == {"result": True}
    assert client.portal.call(_access_log_count, client.db) == 1

"""Regression tests: /validate must not store a caller-sized area in the access log.

``/validate`` is public.  Its ``area`` had no length bound, and every call wrote the
area to ``access_logs.area`` (TEXT, no bound), so one anonymous request could store a
string of any size (a 1,000,000-character area was stored in full).

Now ``area`` takes at most 255 characters (Unicode code points, not bytes) on both
POST and GET.  A longer area is answered 422 by FastAPI's request validation, before
any database work, so nothing is logged.  ``log_access`` also stores at most 255
characters of an area, whoever calls it.

Every secret the app reads is generated per run with ``secrets``.  The app is
imported inside the fixture, not at collection time, so whichever test module
imports it first fixes the cached settings; the fixture then refuses to run if
the effective settings hold an empty or placeholder secret.  The app always
opens this run's own temporary SQLite database, and Redis is replaced by an
in-memory fake; ``REDIS_URL`` carries a per-run random password because the app
requires one.  Token values are generated per test with ``secrets``.

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
_DATABASE_URL = f"sqlite:///{_TMP_DIR.name}/area-length.db"
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
LIMIT = 255
NON_BMP = "\U0001F600"  # one code point, four UTF-8 bytes


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
    monkeypatch.setattr(main_mod.redis, "from_url", lambda *a, **k: FakeRedis())
    rate_limit._buckets.clear()
    with TestClient(main_mod.app, raise_server_exceptions=False) as c:
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
        str(uuid.uuid4()), "area-length-test", sha256_hex(value), json.dumps(grants),
    )


async def _logged_areas(db):
    return [r["area"] for r in await db.fetch("SELECT area FROM access_logs")]


def _granted_token(client, area: str) -> str:
    token = secrets.token_urlsafe(32)
    client.portal.call(_insert_token, client.db, token, [{"area": area, "level": "read"}])
    return token


def _call(client, method: str, token: str, area: str):
    params = {"token": token, "area": area, "level": "read"}
    if method == "POST":
        return client.post("/validate", content=json.dumps(params), headers=JSON)
    return client.get("/validate", params=params)


METHODS = pytest.mark.parametrize("method", ["POST", "GET"])
OVER = pytest.mark.parametrize(
    "area",
    [pytest.param("o" * (LIMIT + 1), id="ascii"), pytest.param(NON_BMP * (LIMIT + 1), id="non-bmp")],
)
AT_LIMIT = pytest.mark.parametrize(
    "area",
    [pytest.param("o" * LIMIT, id="ascii"), pytest.param(NON_BMP * LIMIT, id="non-bmp")],
)


# ── Failed before the fix: the area was accepted and logged at any length ──


@METHODS
@OVER
def test_area_over_limit_unknown_token_is_422_and_not_logged(client, method, area):
    r = _call(client, method, secrets.token_urlsafe(32), area)
    assert r.status_code == 422
    assert [(e["type"], e["loc"][-1]) for e in r.json()["detail"]] == [("string_too_long", "area")]
    assert client.portal.call(_logged_areas, client.db) == []


@METHODS
@OVER
def test_area_over_limit_with_matching_grant_is_422_and_not_logged(client, method, area):
    token = _granted_token(client, area)
    r = _call(client, method, token, area)
    assert r.status_code == 422
    assert client.portal.call(_logged_areas, client.db) == []


def test_log_access_stores_at_most_limit_characters(client):
    from app.logger import log_access

    client.portal.call(log_access, client.db, None, None, "x" * 10_000, "read", False, "192.0.2.1")
    client.portal.call(log_access, client.db, None, None, NON_BMP * 300, "read", False, "192.0.2.1")
    stored = sorted(client.portal.call(_logged_areas, client.db))
    assert stored == sorted(["x" * LIMIT, NON_BMP * LIMIT])


# ── Passed before the fix too: areas within the limit work as before ──


@METHODS
@AT_LIMIT
def test_area_at_limit_is_validated_and_logged_in_full(client, method, area):
    token = _granted_token(client, area)
    r = _call(client, method, token, area)
    assert r.status_code == 200
    assert r.json() == {"result": True}
    assert client.portal.call(_logged_areas, client.db) == [area]


@METHODS
def test_longest_zone_name_with_surrounding_spaces_still_matches(client, method):
    zone = "z" * 63
    token = _granted_token(client, zone)
    r = _call(client, method, token, f"  {zone}  ")
    assert r.status_code == 200
    assert r.json() == {"result": True}


def test_repeated_get_area_uses_the_last_value(client):
    token = _granted_token(client, "orders")
    r = client.get(
        "/validate",
        params=[("token", token), ("area", "o" * (LIMIT + 100)), ("area", "orders"), ("level", "read")],
    )
    assert r.status_code == 200
    assert r.json() == {"result": True}
    assert client.portal.call(_logged_areas, client.db) == ["orders"]

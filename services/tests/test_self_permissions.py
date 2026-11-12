"""Tests for the self-permission endpoints.

- ``POST /validate/grants``: a token holder reads the grants of their token.
- ``GET /tokens/me``: an API key holder reads the scopes of their key.

Also covers ``/validate`` regressions around the shared token-record helper.

Every secret the app needs is generated per run with ``secrets`` and set in the
environment before the app is imported.  Requires pytest and httpx.
"""
from __future__ import annotations

import functools
import json
import os
import secrets
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Removed when the test process exits.
_TMP_DIR = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
_TMP = _TMP_DIR.name
_PLACEHOLDERS = {
    "",
    "change-me-in-production",
    "generate-a-long-random-string",
    "min-32-chars-secret-change-me-for-production-use",
    "min-32-chars-secret-change-me!!",
    "at-least-32-characters-long-secret-key",
    "0123456789abcdef0123456789abcdef",
}
MASTER_KEY = secrets.token_urlsafe(32)
_SECRETS = {
    "ADMIN_API_KEY": MASTER_KEY,
    "SESSION_SECRET": secrets.token_urlsafe(48),
    "APP_ENCRYPTION_KEY": secrets.token_hex(16),
}
for _name, _value in _SECRETS.items():
    if _value in _PLACEHOLDERS:
        raise RuntimeError(f"{_name} is empty or a placeholder")
os.environ.update(_SECRETS)
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["ALLOWED_AREAS"] = "orders,billing"
os.environ["RATE_LIMIT_MAX_REQ"] = "100000"
os.environ["TZ"] = "UTC"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main_mod  # noqa: E402
from app import rate_limit  # noqa: E402
from app.crypto_tokens import sha256_hex  # noqa: E402
from app.deps import ALL_SCOPES  # noqa: E402
from app.token_cache import cache_key  # noqa: E402


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


class BrokenRedis(FakeRedis):
    async def get(self, k):
        raise ConnectionError("redis down")

    async def setex(self, k, _ttl, v):
        raise ConnectionError("redis down")

    async def delete(self, k):
        raise ConnectionError("redis down")


@pytest.fixture()
def client(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(main_mod.redis, "from_url", lambda *a, **k: fake)

    async def _no_cleanup(*_a, **_k):
        return None

    # Keep expired test tokens in the DB until the assertions have run.
    monkeypatch.setattr(main_mod, "cleanup_expired_tokens", _no_cleanup)
    rate_limit._buckets.clear()
    with TestClient(main_mod.app) as c:
        c.fake_redis = fake
        yield c
        c.portal.call(_wipe, main_mod.app.state.db)
    rate_limit._buckets.clear()


async def _wipe(db):
    for table in ("tokens", "api_keys", "access_logs"):
        await db.execute(f"DELETE FROM {table}")


def _run(client, fn, *args, **kwargs):
    return client.portal.call(functools.partial(fn, main_mod.app.state.db, *args, **kwargs))


async def _insert_token(db, value, grants, *, active=True, expires_at=None):
    tid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO tokens (id, name, token_hash, grants, expires_at, is_active) VALUES ($1, $2, $3, $4, $5, $6)",
        tid, f"tok-{tid[:8]}", sha256_hex(value), json.dumps(grants), expires_at, active,
    )
    return tid


async def _insert_api_key(db, value, scopes):
    await db.execute(
        "INSERT INTO api_keys (id, name, key_hash, scopes) VALUES ($1, $2, $3, $4)",
        str(uuid.uuid4()), "test-key", sha256_hex(value), scopes,
    )


async def _access_logs(db):
    return await db.fetch("SELECT token_id, area, level, result FROM access_logs ORDER BY rowid")


async def _last_used(db, tid):
    return await db.fetchval("SELECT last_used_at FROM tokens WHERE id = $1", tid)


async def _set_inactive(db, tid):
    await db.execute("UPDATE tokens SET is_active = $1 WHERE id = $2", False, tid)


def _grants(client, token):
    return client.post("/validate/grants", json={"token": token})


def _future():
    return datetime.now(timezone.utc) + timedelta(days=30)


# ── POST /validate/grants ──────────────────────────────────────


def test_active_token_returns_grants_and_expiry(client):
    tok = secrets.token_urlsafe(24)
    exp = _future()
    _run(client, _insert_token, tok, [{"area": "orders", "level": "read"}, {"area": "billing", "level": "all"}],
         expires_at=exp)
    r = _grants(client, tok)
    assert r.status_code == 200
    body = r.json()
    assert body["active"] is True
    assert body["grants"] == [
        {"area": "orders", "level": "read"},
        {"area": "billing", "level": "read,write,delete"},
    ]
    assert datetime.fromisoformat(body["expiresAt"]) == exp
    assert set(body) == {"active", "grants", "expiresAt"}


def test_token_without_expiry(client):
    tok = secrets.token_urlsafe(24)
    _run(client, _insert_token, tok, [{"area": "orders", "level": "write"}])
    body = _grants(client, tok).json()
    assert body == {"active": True, "grants": [{"area": "orders", "level": "write"}], "expiresAt": None}


def test_legacy_readwrite_level_is_normalized(client):
    tok = "custom-value-" + secrets.token_hex(8)  # custom value, no tok_ prefix
    _run(client, _insert_token, tok, [{"area": "orders", "level": "readwrite"}])
    body = _grants(client, tok).json()
    assert body["grants"] == [{"area": "orders", "level": "read,write"}]


def test_active_token_with_no_grants(client):
    tok = secrets.token_urlsafe(24)
    _run(client, _insert_token, tok, [])
    assert _grants(client, tok).json() == {"active": True, "grants": [], "expiresAt": None}


def test_unknown_inactive_and_expired_are_identical(client):
    inactive, expired = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    g = [{"area": "orders", "level": "read"}]
    _run(client, _insert_token, inactive, g, active=False)
    _run(client, _insert_token, expired, g, expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    responses = [_grants(client, t) for t in (secrets.token_urlsafe(24), inactive, expired)]
    for r in responses:
        assert r.status_code == 200
        assert r.json() == {"active": False}
    assert len({r.content for r in responses}) == 1


def test_stale_cache_entry_does_not_report_revoked_token_active(client):
    tok = secrets.token_urlsafe(24)
    tid = _run(client, _insert_token, tok, [{"area": "orders", "level": "read"}])
    _run(client, _set_inactive, tid)
    # A stale active entry left in the validate cache after the revoke.
    client.fake_redis.data[cache_key(sha256_hex(tok))] = json.dumps(
        {"id": tid, "name": "x", "grants": [{"area": "orders", "level": "read"}], "expires_at": None, "is_active": True}
    )
    assert _grants(client, tok).json() == {"active": False}


def test_lookup_does_not_need_redis(client):
    tok = secrets.token_urlsafe(24)
    _run(client, _insert_token, tok, [{"area": "orders", "level": "read"}])
    main_mod.app.state.redis = BrokenRedis()
    try:
        assert _grants(client, tok).json()["active"] is True
    finally:
        main_mod.app.state.redis = client.fake_redis


def test_lookup_logs_access_and_keeps_last_used(client):
    tok = secrets.token_urlsafe(24)
    tid = _run(client, _insert_token, tok, [{"area": "orders", "level": "read"}])
    _grants(client, tok)
    _grants(client, "no-such-token")
    logs = _run(client, _access_logs)
    assert [(r["token_id"], r["area"], r["level"], bool(r["result"])) for r in logs] == [
        (tid, "*", "grants", True),
        (None, "*", "grants", False),
    ]
    assert _run(client, _last_used, tid) is None


def test_lookup_rate_limit_returns_429_and_is_shared(client, monkeypatch):
    tok = secrets.token_urlsafe(24)
    _run(client, _insert_token, tok, [{"area": "orders", "level": "read"}])
    monkeypatch.setattr(rate_limit, "MAX_REQ", 2)
    assert _grants(client, tok).status_code == 200
    assert _grants(client, tok).status_code == 200
    r = _grants(client, tok)
    assert r.status_code == 429
    assert r.json() == {"detail": "Too many requests"}
    # The budget is shared with /validate.
    v = client.post("/validate", json={"token": tok, "area": "orders", "level": "read"})
    assert v.json() == {"result": False}


def test_validate_budget_limits_lookup(client, monkeypatch):
    tok = secrets.token_urlsafe(24)
    _run(client, _insert_token, tok, [{"area": "orders", "level": "read"}])
    monkeypatch.setattr(rate_limit, "MAX_REQ", 1)
    assert client.post("/validate", json={"token": tok, "area": "orders", "level": "read"}).json() == {"result": True}
    assert _grants(client, tok).status_code == 429


def test_lookup_requires_token_field(client):
    assert client.post("/validate/grants", json={}).status_code == 422


def test_lookup_rejects_invalid_unicode(client):
    r = client.post(
        "/validate/grants",
        content=b'{"token": "\\ud800"}',
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422
    assert r.json() == {"detail": "Token must be valid UTF-8 text"}


def test_expiry_matches_token_list_format(client, monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Jerusalem")
    tok = secrets.token_urlsafe(24)
    # SQLite's own datetime form (UTC, no offset): the formatter must convert it to TZ.
    _run(client, _insert_token, tok, [{"area": "orders", "level": "read"}], expires_at="2031-01-01 00:00:00")
    listed = client.get("/tokens", headers={"Authorization": f"Bearer {MASTER_KEY}"}).json()
    assert listed[0]["expiresAt"] == "2031-01-01T02:00:00+02:00"
    assert _grants(client, tok).json()["expiresAt"] == listed[0]["expiresAt"]


# ── /validate regression ───────────────────────────────────────


@pytest.mark.parametrize("method", ["post", "get"])
def test_validate_unchanged(client, method):
    tok = secrets.token_urlsafe(24)
    tid = _run(client, _insert_token, tok, [{"area": "orders", "level": "readwrite"}])

    def call(area, level):
        params = {"token": tok, "area": area, "level": level}
        if method == "post":
            return client.post("/validate", json=params).json()
        return client.get("/validate", params=params).json()

    assert call("billing", "read") == {"result": False}
    assert _run(client, _last_used, tid) is None
    assert call("orders", "write") == {"result": True}
    assert _run(client, _last_used, tid) is not None
    assert call("orders", "delete") == {"result": False}
    # Second calls are served from the cache with the same answers.
    assert call("orders", "read") == {"result": True}
    assert cache_key(sha256_hex(tok)) in client.fake_redis.data
    logs = _run(client, _access_logs)
    assert [(r["area"], r["level"], bool(r["result"])) for r in logs] == [
        ("billing", "read", False),
        ("orders", "write", True),
        ("orders", "delete", False),
        ("orders", "read", True),
    ]


def test_validate_rejects_inactive_and_unknown(client):
    tok = secrets.token_urlsafe(24)
    _run(client, _insert_token, tok, [{"area": "orders", "level": "read"}], active=False)
    for t in (tok, "no-such-token"):
        assert client.post("/validate", json={"token": t, "area": "orders", "level": "read"}).json() == {"result": False}


# ── GET /tokens/me ─────────────────────────────────────────────


def _me(client, key):
    return client.get("/tokens/me", headers={"Authorization": f"Bearer {key}"})


def test_me_db_key_returns_clean_sorted_scopes(client):
    key = secrets.token_urlsafe(32)
    _run(client, _insert_api_key, key, " zones:read,validate,, zones:read ,tokens:read")
    r = _me(client, key)
    assert r.status_code == 200
    assert r.json() == {"scopes": ["tokens:read", "validate", "zones:read"]}


def test_me_master_key_returns_all_scopes(client):
    assert _me(client, MASTER_KEY).json() == {"scopes": sorted(ALL_SCOPES)}


def test_me_rejects_missing_invalid_and_token_values(client):
    tok = secrets.token_urlsafe(24)
    _run(client, _insert_token, tok, [{"area": "orders", "level": "read"}])
    assert client.get("/tokens/me").status_code == 401
    assert _me(client, secrets.token_urlsafe(32)).status_code == 401
    assert _me(client, tok).status_code == 401


def test_me_deleted_key_is_rejected(client):
    key = secrets.token_urlsafe(32)
    _run(client, _insert_api_key, key, "validate")
    assert _me(client, key).status_code == 200

    async def _delete(db):
        await db.execute("DELETE FROM api_keys WHERE key_hash = $1", sha256_hex(key))

    _run(client, _delete)
    assert _me(client, key).status_code == 401

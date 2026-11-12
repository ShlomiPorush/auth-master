"""GET /health checks the database and Redis separately.

It answers 200 with {"ok": true, "db": true, "redis": true} when both answer,
and 503 naming the failed dependency otherwise, so the Docker HEALTHCHECK fails
during an outage.

Every secret the app reads is generated per run with ``secrets``; the app opens
this run's own temporary SQLite database and Redis is an in-memory fake.
Requires pytest and httpx.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402

_SECRET_FIELDS = ("admin_api_key", "session_secret", "app_encryption_key")
_PLACEHOLDERS = {
    "",
    "generate-a-long-random-string",
    "at-least-32-characters-long-secret-key",
    "min-32-chars-secret-change-me!!",
    "change-me-in-production",
    "0123456789abcdef0123456789abcdef",
} | {Settings.model_fields[f].default for f in _SECRET_FIELDS}

# Removed when the test process exits.
_TMP_DIR = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
_DATABASE_URL = f"sqlite:///{_TMP_DIR.name}/health.db"
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


class FakeRedis:
    async def get(self, k):
        return None

    async def ping(self):
        return True

    async def aclose(self):
        pass


@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient

    import app.main as main_mod
    from app.config import get_settings

    s = get_settings()
    for field in _SECRET_FIELDS:
        if getattr(s, field) in _PLACEHOLDERS:
            raise RuntimeError(f"{field} is empty or a placeholder")
    create_database = main_mod.create_database
    monkeypatch.setattr(main_mod, "create_database", lambda _url: create_database(_DATABASE_URL))
    fake = FakeRedis()
    monkeypatch.setattr(main_mod.redis, "from_url", lambda *a, **k: fake)

    async def _no_cleanup(*_a, **_k):
        return None

    monkeypatch.setattr(main_mod, "cleanup_expired_tokens", _no_cleanup)
    with TestClient(main_mod.app) as c:
        c.fake_redis = fake
        c.db = main_mod.app.state.db
        yield c


def test_health_ok(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"ok": True, "db": True, "redis": True}


def test_health_database_down(client, monkeypatch):
    async def broken(*_a, **_k):
        raise OSError("database is down")

    monkeypatch.setattr(client.db, "fetchval", broken)
    res = client.get("/health")
    assert res.status_code == 503
    assert res.json() == {"ok": False, "db": False, "redis": True}


def test_health_redis_down(client, monkeypatch):
    async def broken():
        raise ConnectionError("redis is down")

    monkeypatch.setattr(client.fake_redis, "ping", broken)
    res = client.get("/health")
    assert res.status_code == 503
    assert res.json() == {"ok": False, "db": True, "redis": False}


def test_health_both_down(client, monkeypatch):
    async def broken(*_a, **_k):
        raise ConnectionError("down")

    monkeypatch.setattr(client.db, "fetchval", broken)
    monkeypatch.setattr(client.fake_redis, "ping", broken)
    res = client.get("/health")
    assert res.status_code == 503
    assert res.json() == {"ok": False, "db": False, "redis": False}


def test_health_dependency_that_hangs_counts_as_down(client, monkeypatch):
    from app.routers import health

    async def hang():
        await asyncio.sleep(30)

    monkeypatch.setattr(health, "CHECK_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(client.fake_redis, "ping", hang)
    res = client.get("/health")
    assert res.status_code == 503
    assert res.json() == {"ok": False, "db": True, "redis": False}

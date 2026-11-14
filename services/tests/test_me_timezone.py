"""GET /admin/api/me reports the time zone the server formats dates in.

``timeZone`` is the IANA name from TZ, or "UTC" when TZ is not a valid zone,
the same fallback the server's date formatting uses.

Every secret the app reads is generated per run with ``secrets``; the app opens
this run's own temporary SQLite database and Redis is an in-memory fake.
Requires pytest and httpx.
"""
from __future__ import annotations

import os
import secrets
import sys
import tempfile
import uuid
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
_DATABASE_URL = f"sqlite:///{_TMP_DIR.name}/me.db"
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
    from app.config import get_settings
    from app.sessions import COOKIE_NAME, session_record

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
        db = main_mod.app.state.db
        admin_id = str(uuid.uuid4())
        # The hash is never checked: the session below is created directly.
        c.portal.call(
            db.execute,
            "INSERT INTO admin_users (id, username, password_hash) VALUES ($1, $2, $3)",
            admin_id, "tz-admin", "unused",
        )
        sid = secrets.token_hex(32)
        key, value = session_record(sid, {"kind": "full", "adminId": admin_id, "csrf": secrets.token_hex(24)})
        fake.data[key] = value
        c.cookies.set(COOKIE_NAME, sid)
        try:
            yield c
        finally:
            c.portal.call(db.execute, "DELETE FROM admin_users WHERE id = $1", admin_id)


@pytest.mark.parametrize("tz", ["Asia/Jerusalem", "UTC", "Asia/Kolkata", "America/New_York"])
def test_me_reports_configured_zone(client, monkeypatch, tz):
    monkeypatch.setenv("TZ", tz)
    res = client.get("/admin/api/me")
    assert res.status_code == 200
    body = res.json()
    assert body["user"] == {"username": "tz-admin"}
    assert body["timeZone"] == tz


@pytest.mark.parametrize("tz", ["Not/AZone", "", "   ", "../../etc/passwd"])
def test_me_falls_back_to_utc_for_an_invalid_zone(client, monkeypatch, tz):
    monkeypatch.setenv("TZ", tz)
    res = client.get("/admin/api/me")
    assert res.status_code == 200
    assert res.json()["timeZone"] == "UTC"


def test_me_falls_back_to_utc_when_tz_is_unset(client, monkeypatch):
    monkeypatch.delenv("TZ", raising=False)
    assert client.get("/admin/api/me").json()["timeZone"] == "UTC"

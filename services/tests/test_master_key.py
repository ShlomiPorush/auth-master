"""ADMIN_API_KEY is optional: empty means there is no master key, and no request is ever
accepted as the master key then.  GET /admin/api/api-keys/master says whether one is set,
never what it is.

Every secret is generated per run with ``secrets``; Redis is an in-memory fake.
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

_TMP_DIR = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
os.environ.setdefault("SESSION_SECRET", secrets.token_urlsafe(48))
os.environ.setdefault("APP_ENCRYPTION_KEY", secrets.token_hex(16))
os.environ.setdefault("REDIS_URL", f"redis://:{secrets.token_hex(32)}@127.0.0.1:6379/0")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP_DIR.name}/master.db")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, k):
        return self.data.get(k)

    async def setex(self, k, _ttl, v):
        self.data[k] = v

    async def delete(self, *keys):
        for k in keys:
            self.data.pop(k, None)

    async def ping(self):
        return True

    async def aclose(self):
        pass


def use_master_key(monkeypatch, value: str | None) -> None:
    from app import deps
    from app.config import Settings
    from app.routers import admin_api_keys

    if value is None:
        monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ADMIN_API_KEY", value)
    s = Settings(_env_file=None)
    monkeypatch.setattr(deps, "get_settings", lambda: s)
    monkeypatch.setattr(admin_api_keys, "get_settings", lambda: s)


@pytest.fixture()
def client(monkeypatch, tmp_path):
    import app.main as main_mod
    from app.sessions import COOKIE_NAME, session_record

    fake = FakeRedis()
    monkeypatch.setattr(main_mod.redis, "from_url", lambda *a, **k: fake)
    url = f"sqlite:///{tmp_path}/master.db"
    create_database = main_mod.create_database
    monkeypatch.setattr(main_mod, "create_database", lambda _url: create_database(url))
    with TestClient(main_mod.app) as c:
        # A full session counts only for an existing admin with MFA enabled.
        admin_id = str(uuid.uuid4())
        c.portal.call(
            main_mod.app.state.db.execute,
            "INSERT INTO admin_users (id, username, password_hash, totp_enabled) VALUES ($1, $2, $3, $4)",
            admin_id, "master-key-admin", "unused", True,
        )
        sid = secrets.token_hex(32)
        key, value = session_record(sid, {"kind": "full", "adminId": admin_id, "csrf": secrets.token_hex(24)})
        fake.data[key] = value
        c.cookies.set(COOKIE_NAME, sid)
        yield c


@pytest.mark.parametrize("value", [None, "", "   "])
def test_no_master_key_when_unset_or_empty(client, monkeypatch, value):
    use_master_key(monkeypatch, value)
    assert client.get("/admin/api/api-keys/master").json() == {"active": False, "envName": "ADMIN_API_KEY"}
    for header in ("Bearer ", "Bearer    ", f"Bearer {value or ''}", "Bearer x"):
        assert client.get("/tokens/ping", headers={"Authorization": header}).status_code == 401, header


def test_a_set_master_key_works_and_is_never_shown(client, monkeypatch):
    key = secrets.token_urlsafe(32)
    use_master_key(monkeypatch, key)
    res = client.get("/admin/api/api-keys/master")
    assert res.json() == {"active": True, "envName": "ADMIN_API_KEY"}
    assert key not in res.text
    assert client.get("/tokens/me", headers={"Authorization": f"Bearer {key}"}).status_code == 200
    assert client.get("/tokens/ping", headers={"Authorization": f"Bearer {key}x"}).status_code == 401


def test_master_key_status_needs_an_admin_session(client):
    client.cookies.clear()
    assert client.get("/admin/api/api-keys/master").status_code == 401

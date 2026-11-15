"""The name authenticator apps show for the first admin (the TOTP issuer), set in the
setup wizard: GET /admin/api/bootstrap/mfa-enroll and POST /admin/api/bootstrap/issuer.

The name is kept only in the caller's setup session; TOTP_ISSUER locks it.  The endpoint
needs a setup session and its CSRF token, a pending admin without MFA and its pending
secret, and is refused once setup is complete.

Every secret and password is generated per run with ``secrets``; Redis is an in-memory
fake.  Requires pytest and httpx.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

_TMP_DIR = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
os.environ.setdefault("SESSION_SECRET", secrets.token_urlsafe(48))
os.environ.setdefault("APP_ENCRYPTION_KEY", secrets.token_hex(16))
os.environ.setdefault("REDIS_URL", f"redis://:{secrets.token_hex(32)}@127.0.0.1:6379/0")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP_DIR.name}/issuer.db")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, k):
        return self.data.get(k)

    async def setex(self, k, _ttl, v):
        self.data[k] = v

    async def set(self, k, v, nx=False, ex=None):
        # SET NX, as the used-code record of mfa-verify needs; expiry is not modelled.
        if nx and k in self.data:
            return None
        self.data[k] = str(v)
        return True

    async def delete(self, *keys):
        for k in keys:
            self.data.pop(k, None)

    async def ping(self):
        return True

    async def aclose(self):
        pass


def use_issuer(monkeypatch, value: str | None) -> None:
    from app.config import Settings
    from app.routers import admin_bootstrap

    if value is None:
        monkeypatch.delenv("TOTP_ISSUER", raising=False)
    else:
        monkeypatch.setenv("TOTP_ISSUER", value)
    s = Settings(_env_file=None)
    monkeypatch.setattr(admin_bootstrap, "get_settings", lambda: s)


@pytest.fixture()
def client(monkeypatch, tmp_path):
    import app.main as main_mod

    use_issuer(monkeypatch, None)
    fake = FakeRedis()
    monkeypatch.setattr(main_mod.redis, "from_url", lambda *a, **k: fake)
    url = f"sqlite:///{tmp_path}/issuer.db"
    create_database = main_mod.create_database
    monkeypatch.setattr(main_mod, "create_database", lambda _url: create_database(url))

    async def _no_cleanup(*_a, **_k):
        return None

    monkeypatch.setattr(main_mod, "cleanup_expired_tokens", _no_cleanup)
    with TestClient(main_mod.app) as c:
        c.fake = fake
        c.db = main_mod.app.state.db
        yield c


def _register(c) -> str:
    res = c.post("/admin/api/bootstrap/register", json={"username": "first-admin", "password": secrets.token_urlsafe(16)})
    assert res.status_code == 200, res.text
    return res.json()["csrf"]


def _issuer(c, csrf, issuer):
    # ASCII JSON, so a lone surrogate travels as its \u escape.
    return c.post(
        "/admin/api/bootstrap/issuer",
        content=json.dumps({"issuer": issuer}).encode("ascii"),
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
    )


def _uri_issuer(url: str) -> str:
    return parse_qs(urlsplit(url).query)["issuer"][0]


def test_enrollment_shows_the_default_and_its_limits(client):
    _register(client)
    body = client.get("/admin/api/bootstrap/mfa-enroll").json()
    assert body["issuer"] == "AuthService" and _uri_issuer(body["otpauthUrl"]) == "AuthService"
    assert (body["issuerLocked"], body["issuerEnvName"], body["issuerMaxLength"]) == (False, "TOTP_ISSUER", 64)
    assert body["accountName"] == "first-admin" and body["secretBase32"]


def test_issuer_is_kept_in_the_setup_session_only(client):
    csrf = _register(client)
    secret = client.get("/admin/api/bootstrap/mfa-enroll").json()["secretBase32"]
    res = _issuer(client, csrf, "  Acme Corp  ")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["issuer"] == "Acme Corp" and _uri_issuer(body["otpauthUrl"]) == "Acme Corp"
    assert body["secretBase32"] == secret and body["issuerLocked"] is False
    assert client.get("/admin/api/bootstrap/mfa-enroll").json()["issuer"] == "Acme Corp"
    # Nothing global: a new setup starts from the default again.
    client.cookies.clear()
    _register(client)
    assert client.get("/admin/api/bootstrap/mfa-enroll").json()["issuer"] == "AuthService"


def test_a_stale_setup_session_cannot_change_anything(client):
    from app.sessions import COOKIE_NAME

    csrf = _register(client)
    old_sid = client.cookies.get(COOKIE_NAME)
    client.cookies.clear()
    _register(client)  # a new setup replaces the pending admin of the first one
    new_sid = client.cookies.get(COOKIE_NAME)
    client.cookies.clear()
    client.cookies.set(COOKIE_NAME, old_sid)
    res = _issuer(client, csrf, "Other")
    assert res.status_code == 400
    client.cookies.clear()
    client.cookies.set(COOKIE_NAME, new_sid)
    assert client.get("/admin/api/bootstrap/mfa-enroll").json()["issuer"] == "AuthService"


def test_needs_a_setup_session_and_its_csrf_token(client):
    from app.sessions import COOKIE_NAME, session_record

    assert _issuer(client, "x", "Acme").status_code == 401
    csrf = _register(client)
    assert _issuer(client, "wrong", "Acme").status_code == 403
    assert client.post("/admin/api/bootstrap/issuer", json={"issuer": "Acme"}).status_code == 403
    # A full admin session is not a setup session.
    sid = secrets.token_hex(32)
    key, value = session_record(sid, {"kind": "full", "adminId": str(uuid.uuid4()), "csrf": csrf})
    client.fake.data[key] = value
    client.cookies.clear()
    client.cookies.set(COOKIE_NAME, sid)
    assert _issuer(client, csrf, "Acme").status_code == 401


def test_expired_enrollment_is_refused(client):
    from app.sessions import PENDING_TOTP_PREFIX

    csrf = _register(client)
    for key in [k for k in client.fake.data if k.startswith(PENDING_TOTP_PREFIX)]:
        del client.fake.data[key]
    assert _issuer(client, csrf, "Acme").status_code == 400


@pytest.mark.parametrize("issuer", ["", "   ", "x" * 65, "Acme\nCorp", "Acme\x00", "\ud800"])
def test_invalid_names_are_refused(client, issuer):
    csrf = _register(client)
    res = _issuer(client, csrf, issuer)
    assert res.status_code == 422
    assert res.json()["code"] == "invalid" and res.json()["field"] == "issuer"


def test_longest_name_is_accepted(client):
    csrf = _register(client)
    assert _issuer(client, csrf, "é" * 64).status_code == 200


def test_environment_locks_the_name(client, monkeypatch):
    use_issuer(monkeypatch, "Corp From Env")
    csrf = _register(client)
    body = client.get("/admin/api/bootstrap/mfa-enroll").json()
    assert body["issuer"] == "Corp From Env" and body["issuerLocked"] is True
    res = _issuer(client, csrf, "Other")
    assert res.status_code == 409 and res.json()["code"] == "locked"


def test_refused_after_setup_is_complete(client):
    import pyotp

    from app.sessions import COOKIE_NAME, save_pending_totp, session_record

    csrf = _register(client)
    secret = client.get("/admin/api/bootstrap/mfa-enroll").json()["secretBase32"]
    res = client.post("/admin/api/bootstrap/mfa-verify", json={"code": pyotp.TOTP(secret).now()}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200, res.text
    # A leftover setup session for another pending admin.
    pending_id = str(uuid.uuid4())
    client.portal.call(
        client.db.execute,
        "INSERT INTO admin_users (id, username, password_hash, totp_enabled) VALUES ($1, $2, $3, $4)",
        pending_id, "late", "unused", False,
    )
    sid, csrf2 = secrets.token_hex(32), secrets.token_hex(24)
    key, value = session_record(sid, {"kind": "setup", "adminId": pending_id, "csrf": csrf2})
    client.fake.data[key] = value
    client.portal.call(save_pending_totp, client.fake, pending_id, pyotp.random_base32(), 600)
    client.cookies.clear()
    client.cookies.set(COOKIE_NAME, sid)
    assert _issuer(client, csrf2, "Acme").status_code == 403

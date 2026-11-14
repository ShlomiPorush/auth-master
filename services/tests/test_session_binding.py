"""Admin sessions and pending MFA enrollments in Redis are bound to SESSION_SECRET.

A record's key name is an HMAC of the session ID (or admin ID) and its value is
AES-GCM encrypted with the key name as associated data, both with keys derived
from SESSION_SECRET. So:

- an installation with another SESSION_SECRET that shares the Redis database does
  not accept this installation's session cookie, even when it has an MFA-enabled
  admin with the same ID;
- a Redis client without the secret cannot forge a session, move a session's value
  onto another session, or read or plant a pending MFA secret;
- a damaged or foreign value, an old-format record and an unusual cookie count as
  no session (401), never 500; Redis connection errors are not hidden.

Every secret and password is generated per run with ``secrets``; each installation
opens its own temporary SQLite database and Redis is an in-memory fake shared by
both. Requires pytest and httpx.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

import pyotp
import pytest
import redis.exceptions

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
_SECRETS = {
    "ADMIN_API_KEY": secrets.token_urlsafe(32),
    "SESSION_SECRET": secrets.token_urlsafe(48),
    "APP_ENCRYPTION_KEY": secrets.token_hex(16),
}
for _name, _value in _SECRETS.items():
    if _value in _PLACEHOLDERS:
        raise RuntimeError(f"{_name} is empty or a placeholder")
# Used only when this module is the first to load the settings.
for _name, _value in _SECRETS.items():
    os.environ.setdefault(_name, _value)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP_DIR.name}/unused.db")
os.environ.setdefault("REDIS_URL", f"redis://:{secrets.token_hex(32)}@127.0.0.1:6379/0")

REGISTER = "/admin/api/bootstrap/register"
ENROLL = "/admin/api/bootstrap/mfa-enroll"
VERIFY = "/admin/api/bootstrap/mfa-verify"
LOGIN = "/admin/api/login"
LOGOUT = "/admin/api/logout"
ME = "/admin/api/me"
FULL_ONLY = "/admin/api/rate-limit"  # any endpoint guarded by FullSession


class FakeRedis:
    """One Redis logical database, shared by every installation in a test.

    The app's client decodes replies as UTF-8 (decode_responses=True); a stored
    bytes value is decoded the same way, so bytes that are not UTF-8 raise
    UnicodeDecodeError from get(), as redis-py does."""

    def __init__(self) -> None:
        self.data: dict[str, str | bytes] = {}
        self.ttl: dict[str, int] = {}
        self.fail_with: Exception | None = None

    async def get(self, k):
        if self.fail_with:
            raise self.fail_with
        v = self.data.get(k)
        return v.decode("utf-8") if isinstance(v, bytes) else v

    async def setex(self, k, ttl, v):
        self.data[k] = v
        self.ttl[k] = ttl

    async def delete(self, *ks):
        for k in ks:
            self.data.pop(k, None)
            self.ttl.pop(k, None)

    async def ping(self):
        return True

    async def aclose(self):
        pass


def _fresh_secret() -> str:
    value = secrets.token_urlsafe(48)
    if value in _PLACEHOLDERS:
        raise RuntimeError("generated a placeholder")
    return value


@contextmanager
def installation(monkeypatch, fake: FakeRedis, db_name: str, session_secret: str):
    """The app with its own SQLite database and SESSION_SECRET, on the shared Redis."""
    from fastapi.testclient import TestClient

    import app.main as main_mod
    import app.sessions as sessions_mod
    from app.config import get_settings
    from app.db import create_database

    s = get_settings()
    for field in _SECRET_FIELDS:
        if getattr(s, field) in _PLACEHOLDERS:
            raise RuntimeError(f"{field} is empty or a placeholder")
    settings = s.model_copy(update={"session_secret": session_secret})
    monkeypatch.setattr(sessions_mod, "get_settings", lambda: settings)
    url = f"sqlite:///{_TMP_DIR.name}/{db_name}.db"
    monkeypatch.setattr(main_mod, "create_database", lambda _url: create_database(url))
    monkeypatch.setattr(main_mod.redis, "from_url", lambda *a, **k: fake)

    async def _no_cleanup(*_a, **_k):
        return None

    monkeypatch.setattr(main_mod, "cleanup_expired_tokens", _no_cleanup)
    with TestClient(main_mod.app) as c:
        yield c, main_mod.app.state.db


def _cookie_name() -> str:
    from app.sessions import COOKIE_NAME

    return COOKIE_NAME


def _record(sid: str, payload: dict) -> tuple[str, str]:
    from app.sessions import session_record

    return session_record(sid, payload)


def _register(c) -> tuple[str, str]:
    """Start the first-admin setup. Returns (password, csrf)."""
    from app.config import get_settings

    password = secrets.token_urlsafe(16)
    headers = {}
    if get_settings().bootstrap_token:
        headers["X-Bootstrap-Token"] = get_settings().bootstrap_token
    res = c.post(REGISTER, json={"username": "admin", "password": password}, headers=headers)
    assert res.status_code == 200, res.text
    return password, res.json()["csrf"]


def _bootstrap(c) -> dict:
    """Complete setup and MFA; the client keeps the full session cookie."""
    password, csrf = _register(c)
    seed = c.get(ENROLL).json()["secretBase32"]
    res = c.post(VERIFY, json={"code": pyotp.TOTP(seed).now()}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200, res.text
    return {"password": password, "seed": seed, "sid": c.cookies.get(_cookie_name())}


def _admin_id(c, db) -> str:
    return str(c.portal.call(db.fetchval, "SELECT id FROM admin_users WHERE username = $1", "admin"))


def _use(c, sid: str) -> None:
    c.cookies.clear()
    c.cookies.set(_cookie_name(), sid)


def _signed_out(c) -> None:
    assert c.get(FULL_ONLY).status_code == 401
    res = c.get(ME)
    assert res.status_code == 200 and res.json()["user"] is None


# ---------------------------------------------------------------------------
# Two installations, one Redis
# ---------------------------------------------------------------------------


def test_cookie_of_one_installation_is_refused_by_another_on_the_same_redis(monkeypatch):
    fake = FakeRedis()
    secret_a, secret_b = _fresh_secret(), _fresh_secret()
    db_a, db_b = f"a-{uuid.uuid4().hex}", f"b-{uuid.uuid4().hex}"

    with installation(monkeypatch, fake, db_a, secret_a) as (c, db):
        sid = _bootstrap(c)["sid"]
        admin_id = _admin_id(c, db)
        assert c.get(FULL_ONLY).status_code == 200

    # B holds an MFA-enabled admin with the same ID (as after a copied database), so
    # only the session binding can tell the installations apart.
    with installation(monkeypatch, fake, db_b, secret_a) as (c, db):
        c.portal.call(
            db.execute,
            "INSERT INTO admin_users (id, username, password_hash, totp_secret_enc, totp_enabled)"
            " VALUES ($1, $2, $3, $4, $5)",
            admin_id, "admin", "unused", "unused", True,
        )
        # Control: with A's secret, B accepts the cookie.
        _use(c, sid)
        assert c.get(FULL_ONLY).status_code == 200
        assert c.get(ME).json()["user"] == {"username": "admin"}

    with installation(monkeypatch, fake, db_b, secret_b) as (c, _db):
        _use(c, sid)
        _signed_out(c)


# ---------------------------------------------------------------------------
# A Redis client without the secret
# ---------------------------------------------------------------------------


def test_forged_plain_session_is_refused(monkeypatch):
    fake = FakeRedis()
    with installation(monkeypatch, fake, f"f-{uuid.uuid4().hex}", _fresh_secret()) as (c, db):
        _bootstrap(c)
        admin_id = _admin_id(c, db)
        sid = secrets.token_hex(32)
        forged = json.dumps({"kind": "full", "adminId": admin_id, "csrf": secrets.token_hex(24)})
        # The old key layout, and the stored name of the attacker's own session
        # written with a value the attacker made without the secret.
        fake.data["auth:sess:" + sid] = forged
        fake.data[_record(sid, {})[0]] = forged
        _use(c, sid)
        _signed_out(c)


def test_value_moved_from_a_full_session_onto_a_partial_session_is_refused(monkeypatch):
    fake = FakeRedis()
    with installation(monkeypatch, fake, f"m-{uuid.uuid4().hex}", _fresh_secret()) as (c, _db):
        admin = _bootstrap(c)
        full_key = _record(admin["sid"], {})[0]
        assert full_key in fake.data

        # Password only: a partial session.
        c.cookies.clear()
        res = c.post(LOGIN, json={"username": "admin", "password": admin["password"]})
        assert res.status_code == 200 and res.json()["mfaRequired"] is True
        partial_sid = c.cookies.get(_cookie_name())
        partial_key = _record(partial_sid, {})[0]
        assert partial_key in fake.data
        assert c.get(FULL_ONLY).status_code == 401

        fake.data[partial_key] = fake.data[full_key]
        _use(c, partial_sid)
        _signed_out(c)
        # The full session itself still works.
        _use(c, admin["sid"])
        assert c.get(FULL_ONLY).status_code == 200


def test_pending_mfa_secret_is_neither_readable_nor_plantable(monkeypatch):
    fake = FakeRedis()
    with installation(monkeypatch, fake, f"p-{uuid.uuid4().hex}", _fresh_secret()) as (c, _db):
        _password, csrf = _register(c)
        seed = c.get(ENROLL).json()["secretBase32"]
        assert not any(seed in str(v) for v in fake.data.values())

        planted = pyotp.random_base32()
        pending = [k for k in fake.data if k.startswith("auth:totp_pending:")]
        assert len(pending) == 1
        fake.data[pending[0]] = planted

        res = c.get(ENROLL)
        assert res.status_code == 400
        assert planted not in res.text
        res = c.post(VERIFY, json={"code": pyotp.TOTP(planted).now()}, headers={"X-CSRF-Token": csrf})
        assert res.status_code == 400
        assert c.get(FULL_ONLY).status_code == 401


# ---------------------------------------------------------------------------
# The normal flow
# ---------------------------------------------------------------------------


def test_setup_login_and_logout_work_and_keep_their_lifetimes(monkeypatch):
    from app.sessions import SESSION_TTL, SETUP_TTL

    fake = FakeRedis()
    with installation(monkeypatch, fake, f"n-{uuid.uuid4().hex}", _fresh_secret()) as (c, _db):
        _password, csrf = _register(c)
        setup_sid = c.cookies.get(_cookie_name())
        assert fake.ttl[_record(setup_sid, {})[0]] == SETUP_TTL
        pending = [k for k in fake.data if k.startswith("auth:totp_pending:")]
        assert len(pending) == 1 and fake.ttl[pending[0]] == SETUP_TTL

        seed = c.get(ENROLL).json()["secretBase32"]
        res = c.post(VERIFY, json={"code": pyotp.TOTP(seed).now()}, headers={"X-CSRF-Token": csrf})
        assert res.status_code == 200
        sid = c.cookies.get(_cookie_name())
        assert fake.ttl[_record(sid, {})[0]] == SESSION_TTL
        assert not any(k.startswith("auth:totp_pending:") for k in fake.data)
        assert _record(setup_sid, {})[0] not in fake.data

        assert c.get(FULL_ONLY).status_code == 200
        me = c.get(ME).json()
        assert me["user"] == {"username": "admin"} and me["csrf"] == res.json()["csrf"]

        assert c.post(LOGOUT).status_code == 200
        assert not any(k.startswith("auth:sess:") for k in fake.data)
        _use(c, sid)
        _signed_out(c)


# ---------------------------------------------------------------------------
# Damaged or foreign values and unusual cookies
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "damage",
    ["not-utf8", "truncated", "not-base64", "empty-envelope", "other-session-name"],
)
def test_damaged_value_counts_as_no_session(monkeypatch, damage):
    fake = FakeRedis()
    with installation(monkeypatch, fake, f"d-{uuid.uuid4().hex}", _fresh_secret()) as (c, _db):
        sid = _bootstrap(c)["sid"]
        key = _record(sid, {})[0]
        value = fake.data[key]
        fake.data[key] = {
            "not-utf8": b"\xff\xfe" + value.encode("ascii"),
            "truncated": value[:20],
            "not-base64": "%%%" + value,
            "empty-envelope": "",
            "other-session-name": _record(secrets.token_hex(32), {"kind": "full"})[1],
        }[damage]
        _use(c, sid)
        _signed_out(c)


@pytest.mark.parametrize(
    "cookie",
    [
        "א".encode("utf-8") * 2048,  # 4 KB, not ASCII
        b"\xff" * 64,
        b"x",
    ],
)
def test_unusual_cookie_counts_as_no_session(monkeypatch, cookie):
    fake = FakeRedis()
    with installation(monkeypatch, fake, f"c-{uuid.uuid4().hex}", _fresh_secret()) as (c, _db):
        _bootstrap(c)
        c.cookies.clear()
        header = {"Cookie": _cookie_name().encode("ascii") + b"=" + cookie}
        assert c.get(FULL_ONLY, headers=header).status_code == 401
        res = c.get(ME, headers=header)
        assert res.status_code == 200 and res.json()["user"] is None


def test_redis_connection_errors_are_not_hidden(monkeypatch):
    fake = FakeRedis()
    with installation(monkeypatch, fake, f"e-{uuid.uuid4().hex}", _fresh_secret()) as (c, _db):
        _bootstrap(c)
        fake.fail_with = redis.exceptions.ConnectionError("connection refused")
        with pytest.raises(redis.exceptions.ConnectionError):
            c.get(FULL_ONLY)

"""Admin passwords and bcrypt's 72-byte limit.

bcrypt uses only the first 72 bytes of a password. bcrypt 4 dropped the rest
silently; bcrypt 5 raises ValueError instead, which made registration and login
with a longer password answer 500. Registration now refuses such a password with
a clear message, and login checks the first 72 bytes, so a password stored by
bcrypt 4 keeps working, even when byte 72 falls inside a multibyte character.

A JSON escape such as "\\ud800" decodes to a lone surrogate, which has no UTF-8
encoding. Registration refuses it with 400 and login answers 401, never 500.

Every secret and password is generated per run with ``secrets``; the app opens
this run's own temporary SQLite database and Redis is an in-memory fake.
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

import bcrypt
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
_DATABASE_URL = f"sqlite:///{_TMP_DIR.name}/password-length.db"
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

REGISTER = "/admin/api/bootstrap/register"
LOGIN = "/admin/api/login"
TOO_LONG = "Password: use at most 72 bytes"
_HEBREW_LETTERS = [chr(c) for c in range(0x05D0, 0x05EB)]  # two bytes each in UTF-8


def _ascii(n: int) -> str:
    """n random ASCII characters, one byte each."""
    return secrets.token_hex(n)[:n]


def _hebrew(n: int) -> str:
    """n random Hebrew letters, two bytes each."""
    return "".join(secrets.choice(_HEBREW_LETTERS) for _ in range(n))


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
def app_ctx(monkeypatch):
    from fastapi.testclient import TestClient

    import app.main as main_mod
    from app.config import get_settings

    s = get_settings()
    for field in _SECRET_FIELDS:
        if getattr(s, field) in _PLACEHOLDERS:
            raise RuntimeError(f"{field} is empty or a placeholder")
    if s.bootstrap_token:
        raise RuntimeError("these tests expect BOOTSTRAP_TOKEN to be unset")
    create_database = main_mod.create_database
    monkeypatch.setattr(main_mod, "create_database", lambda _url: create_database(_DATABASE_URL))
    fake = FakeRedis()
    monkeypatch.setattr(main_mod.redis, "from_url", lambda *a, **k: fake)

    async def _no_cleanup(*_a, **_k):
        return None

    monkeypatch.setattr(main_mod, "cleanup_expired_tokens", _no_cleanup)
    with TestClient(main_mod.app) as c:
        db = main_mod.app.state.db
        try:
            yield c, db, fake
        finally:
            c.portal.call(db.execute, "DELETE FROM admin_users")


def _rows(c, db):
    return c.portal.call(db.fetch, "SELECT id, username, password_hash FROM admin_users")


def _post_raw(c, path, raw_json: str):
    return c.post(path, content=raw_json.encode("ascii"), headers={"Content-Type": "application/json"})


def _add_user(c, db, password_bytes: bytes) -> str:
    """Store a hash the way bcrypt 4 did: from the first 72 bytes."""
    username = "admin-" + secrets.token_hex(4)
    pw_hash = bcrypt.hashpw(password_bytes[:72], bcrypt.gensalt(rounds=4)).decode("ascii")
    c.portal.call(
        db.execute,
        "INSERT INTO admin_users (id, username, password_hash, totp_enabled) VALUES ($1, $2, $3, $4)",
        str(uuid.uuid4()), username, pw_hash, False,
    )
    return username


def _login(c, username: str, password_bytes: bytes):
    raw = json.dumps({"username": username, "password": password_bytes.decode("utf-8")})
    return _post_raw(c, LOGIN, raw)


# ── Registration ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "password",
    [
        lambda: _ascii(73),
        lambda: _hebrew(37),  # 74 bytes, 37 characters
        lambda: _ascii(71) + _hebrew(1),  # 73 bytes, 72 characters
    ],
    ids=["ascii-73", "hebrew-74", "ends-in-multibyte-73"],
)
def test_register_refuses_password_over_72_bytes(app_ctx, password):
    c, db, _fake = app_ctx
    res = c.post(REGISTER, json={"username": "admin", "password": password()})
    assert res.status_code == 400
    assert res.json()["detail"].startswith(TOO_LONG)
    assert _rows(c, db) == []


def test_refused_register_keeps_a_pending_account(app_ctx):
    c, db, fake = app_ctx
    first = c.post(REGISTER, json={"username": "pending", "password": secrets.token_hex(8)})
    assert first.status_code == 200
    rows_before = [dict(r) for r in _rows(c, db)]
    redis_before = dict(fake.data)
    assert len(rows_before) == 1 and redis_before

    res = c.post(REGISTER, json={"username": "other", "password": _ascii(73)})
    assert res.status_code == 400
    assert [dict(r) for r in _rows(c, db)] == rows_before
    assert fake.data == redis_before


@pytest.mark.parametrize(
    "password",
    [lambda: _ascii(72), lambda: _hebrew(36)],
    ids=["ascii-72", "hebrew-72"],
)
def test_register_accepts_exactly_72_bytes(app_ctx, password):
    c, db, _fake = app_ctx
    pw = password()
    assert len(pw.encode("utf-8")) == 72
    res = c.post(REGISTER, json={"username": "admin", "password": pw})
    assert res.status_code == 200
    (row,) = _rows(c, db)
    assert bcrypt.checkpw(pw.encode("utf-8"), row["password_hash"].encode("ascii"))


def test_register_refuses_a_lone_surrogate(app_ctx):
    c, db, _fake = app_ctx
    res = _post_raw(c, REGISTER, '{"username": "admin", "password": "' + _ascii(8) + '\\ud800"}')
    assert res.status_code == 400
    assert _rows(c, db) == []


# ── Login ──────────────────────────────────────────────────────


def _long_password() -> bytes:
    return secrets.token_hex(50).encode("ascii")  # 100 bytes


def test_login_accepts_a_long_password_stored_by_bcrypt_4(app_ctx):
    c, db, _fake = app_ctx
    pw = _long_password()
    user = _add_user(c, db, pw)
    res = _login(c, user, pw)
    # The password matched; this user has no MFA yet.
    assert res.status_code == 403
    assert res.json()["detail"] == "MFA not configured"


def test_login_ignores_bytes_after_72_like_bcrypt_4(app_ctx):
    c, db, _fake = app_ctx
    pw = _long_password()
    user = _add_user(c, db, pw)
    other_tail = pw[:72] + secrets.token_hex(20).encode("ascii")
    assert _login(c, user, other_tail).status_code == 403


def test_login_refuses_a_long_password_that_differs_in_the_first_72_bytes(app_ctx):
    c, db, _fake = app_ctx
    pw = _long_password()
    user = _add_user(c, db, pw)
    wrong = pw[:40] + (b"x" if pw[40:41] != b"x" else b"y") + pw[41:]
    res = _login(c, user, wrong)
    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid credentials"


def test_login_accepts_a_password_whose_72nd_byte_splits_a_character(app_ctx):
    c, db, _fake = app_ctx
    # Byte 72 is the first byte of a two-byte letter, so the first 72 bytes are not
    # valid UTF-8 on their own. bcrypt 4 hashed exactly those bytes.
    pw = (_ascii(71) + _hebrew(4)).encode("utf-8")
    assert len(pw) == 79
    user = _add_user(c, db, pw)
    assert _login(c, user, pw).status_code == 403


def test_login_refuses_a_lone_surrogate(app_ctx):
    c, db, _fake = app_ctx
    user = _add_user(c, db, secrets.token_hex(8).encode("ascii"))
    res = _post_raw(c, LOGIN, json.dumps({"username": user, "password": _ascii(8)})[:-2] + '\\ud800"}')
    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid credentials"

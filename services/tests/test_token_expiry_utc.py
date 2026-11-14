"""A token's expiresAt without a UTC offset means UTC on every backend and every write path.

The four write paths (POST and PATCH on /tokens and on /admin/api/tokens) used to bind the
parsed value as given. A value without an offset is a naive datetime; SQLite stores it as
UTC, but the PostgreSQL driver reads it in the process zone (TZ). With TZ west of UTC a
token kept validating for up to the zone offset after the time it was given to expire.

The tests run with TZ=America/New_York on a temporary SQLite database, and also on
PostgreSQL when EXPIRY_TEST_DATABASE_URL names a throwaway database whose name starts with
expiry_test (for example in a container with a password generated for that run); they
delete every token and zone in it. The PostgreSQL case needs time.tzset (not on Windows).
Every secret the app reads is generated per run with ``secrets``; Redis is an in-memory
fake. Requires pytest and httpx.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

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
_PG_ENV = "EXPIRY_TEST_DATABASE_URL"
_PG_URL = os.environ.get(_PG_ENV, "")
if _PG_URL:
    from app.config import _postgres_url_problem  # noqa: E402

    # Every test deletes all tokens and zones: only a database made for these tests.
    if not urlsplit(_PG_URL).path.lstrip("/").startswith("expiry_test"):
        raise RuntimeError(f"{_PG_ENV} must name a throwaway database whose name starts with expiry_test")
    if not urlsplit(_PG_URL).password or _postgres_url_problem(_PG_URL):
        raise RuntimeError(f"{_PG_ENV} needs a password generated for this run")

# Fresh for this run; the app reads them from the settings of the first test module loaded.
_SECRETS = {
    "ADMIN_API_KEY": secrets.token_urlsafe(32),
    "SESSION_SECRET": secrets.token_urlsafe(48),
    "APP_ENCRYPTION_KEY": secrets.token_hex(16),
    "REDIS_URL": f"redis://:{secrets.token_hex(32)}@127.0.0.1:6379/0",
}
os.environ.update(_SECRETS)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP_DIR.name}/expiry-default.db")

_ZONE = "expiryzone"
_SERVER_TZ = "America/New_York"
PATHS = ("api_create", "api_patch", "admin_create", "admin_patch")


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


class Env:
    def __init__(self, client, db, api_auth, csrf) -> None:
        self.c = client
        self.db = db
        self.api = api_auth
        self.admin = {"X-CSRF-Token": csrf}

    def run(self, fn, *args):
        return self.c.portal.call(fn, *args)

    def set_expiry(self, path: str, expires_at: str) -> tuple[str, str]:
        """Give a new token this expiresAt through one write path; return its id and value."""
        raw = "exp-" + secrets.token_hex(16)
        body = {"name": f"exp-{secrets.token_hex(6)}", "grants": [{"area": _ZONE, "level": "read"}], "token": raw}
        create_url, headers = ("/tokens", self.api) if path.startswith("api") else ("/admin/api/tokens", self.admin)
        if path.endswith("create"):
            body["expiresAt"] = expires_at
        r = self.c.post(create_url, headers=headers, json=body)
        assert r.status_code == 201, r.text
        tid = r.json()["id"]
        if path.endswith("patch"):
            r = self.c.patch(f"{create_url}/{tid}", headers=headers, json={"expiresAt": expires_at})
            assert r.status_code == 200, r.text
        return tid, raw

    def listed(self, tid: str) -> dict:
        r = self.c.get("/tokens", headers=self.api)
        assert r.status_code == 200, r.text
        return next(t for t in r.json() if t["id"] == tid)

    def validates(self, raw: str) -> bool:
        r = self.c.post("/validate", json={"token": raw, "area": _ZONE, "level": "read"})
        assert r.status_code == 200, r.text
        return r.json()["result"]


def _backends():
    yield pytest.param(f"sqlite:///{_TMP_DIR.name}/expiry.db", id="sqlite")
    yield pytest.param(
        _PG_URL,
        id="postgres",
        marks=[
            pytest.mark.skipif(not _PG_URL, reason=f"{_PG_ENV} is not set"),
            pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset is needed to change the process zone"),
        ],
    )


@pytest.fixture(params=list(_backends()))
def env(request, monkeypatch):
    from fastapi.testclient import TestClient

    import app.main as main_mod
    from app import rate_limit
    from app.config import get_settings
    from app.sessions import COOKIE_NAME, PREFIX

    s = get_settings()
    for field in _SECRET_FIELDS:
        if getattr(s, field) in _PLACEHOLDERS:
            raise RuntimeError(f"{field} is empty or a placeholder")

    # The server runs west of UTC, where the driver would read a naive value as a later instant.
    monkeypatch.setenv("TZ", _SERVER_TZ)
    if hasattr(time, "tzset"):
        time.tzset()

    url = request.param
    create_database = main_mod.create_database
    monkeypatch.setattr(main_mod, "create_database", lambda _url: create_database(url))
    fake = FakeRedis()
    monkeypatch.setattr(main_mod.redis, "from_url", lambda *a, **k: fake)

    async def _no_cleanup(*_a, **_k):
        return None

    monkeypatch.setattr(main_mod, "cleanup_expired_tokens", _no_cleanup)
    rate_limit._buckets.clear()
    try:
        with TestClient(main_mod.app) as c:
            db = main_mod.app.state.db
            assert db.is_pg == url.startswith("postgres"), "test ran on the wrong database backend"
            sid, csrf = secrets.token_hex(32), secrets.token_hex(24)
            fake.data[PREFIX + sid] = json.dumps({"kind": "full", "adminId": str(uuid.uuid4()), "csrf": csrf})
            c.cookies.set(COOKIE_NAME, sid)
            e = Env(c, db, {"Authorization": f"Bearer {s.admin_api_key}"}, csrf)
            e.run(db.execute, "DELETE FROM tokens")
            e.run(db.execute, "DELETE FROM zones")
            r = c.post("/tokens/zones", headers=e.api, json={"name": _ZONE})
            assert r.status_code == 201, r.text
            try:
                yield e
            finally:
                e.run(db.execute, "DELETE FROM tokens")
                e.run(db.execute, "DELETE FROM zones")
    finally:
        monkeypatch.undo()
        if hasattr(time, "tzset"):
            time.tzset()


def _instant(text: str) -> datetime:
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@pytest.mark.parametrize("path", PATHS)
def test_naive_expiry_in_the_past_is_expired(env, path):
    past = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=2)
    tid, raw = env.set_expiry(path, past.replace(tzinfo=None).isoformat())

    row = env.listed(tid)
    assert _instant(row["expiresAt"]) == past
    assert row["isExpired"] is True
    assert env.validates(raw) is False


@pytest.mark.parametrize("path", PATHS)
def test_naive_expiry_is_stored_as_the_same_utc_instant(env, path):
    future = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=2)
    tid, raw = env.set_expiry(path, future.replace(tzinfo=None).isoformat())

    row = env.listed(tid)
    assert _instant(row["expiresAt"]) == future
    assert row["isExpired"] is False
    assert env.validates(raw) is True


class _RecordTokenWrites:
    """Wraps the database and records every datetime bound in a write to the tokens table."""

    def __init__(self, db) -> None:
        self._db = db
        self.bound: list[datetime] = []

    def __getattr__(self, name):
        return getattr(self._db, name)

    def _record(self, sql, args):
        if "tokens" in sql and ("INSERT" in sql or "UPDATE" in sql):
            self.bound += [a for a in args if isinstance(a, datetime)]

    async def execute(self, sql, *args):
        self._record(sql, args)
        return await self._db.execute(sql, *args)

    async def fetchval(self, sql, *args):
        self._record(sql, args)
        return await self._db.fetchval(sql, *args)


@pytest.mark.parametrize("path", PATHS)
def test_every_write_path_binds_an_aware_utc_expiry(env, path):
    """Both backends get the same instant only when the bound value carries its offset."""
    import app.main as main_mod

    rec = _RecordTokenWrites(main_mod.app.state.db)
    main_mod.app.state.db = rec
    try:
        env.set_expiry(path, (datetime.now(timezone.utc) + timedelta(hours=2)).replace(tzinfo=None).isoformat())
    finally:
        main_mod.app.state.db = rec._db
    assert rec.bound, "no expiry was bound"
    for dt in rec.bound:
        assert dt.tzinfo is not None and dt.utcoffset() == timedelta(0), dt


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("given", ["{t}Z", "{t}+00:00", "{t_plus_2}+02:00"])
def test_expiry_with_an_offset_keeps_its_instant(env, path, given):
    future = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=2)
    text = given.format(
        t=future.replace(tzinfo=None).isoformat(),
        t_plus_2=(future + timedelta(hours=2)).replace(tzinfo=None).isoformat(),
    )
    tid, _raw = env.set_expiry(path, text)
    assert _instant(env.listed(tid)["expiresAt"]) == future


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize(
    ("given", "instant"),
    [
        ("2099-01-01", datetime(2099, 1, 1, tzinfo=timezone.utc)),
        ("9999-12-31T23:59:59", datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)),
    ],
)
def test_naive_date_only_and_last_instant_mean_utc(env, path, given, instant):
    tid, raw = env.set_expiry(path, given)
    row = env.listed(tid)
    assert _instant(row["expiresAt"]) == instant
    assert row["isExpired"] is False
    assert env.validates(raw) is True


@pytest.mark.parametrize("given", ["9999-12-31T23:59:59", "9999-12-31T23:59:59Z"])
def test_last_instant_is_listed_east_of_utc(env, monkeypatch, given):
    """In a zone east of UTC the last UTC instant falls in year 10000 local time."""
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    if hasattr(time, "tzset"):
        time.tzset()
    tid, raw = env.set_expiry("api_create", given)
    row = env.listed(tid)
    assert _instant(row["expiresAt"]) == datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    assert env.c.get("/admin/api/tokens").status_code == 200
    r = env.c.post("/validate/grants", json={"token": raw})
    assert r.status_code == 200, r.text


def test_old_naive_sqlite_row_at_the_last_instant_is_listed_east_of_utc(env, monkeypatch):
    """Rows written before the fix hold a naive UTC string on SQLite."""
    if not env.db.is_sqlite:
        pytest.skip("naive text rows exist only on SQLite")
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    if hasattr(time, "tzset"):
        time.tzset()
    tid = str(uuid.uuid4())
    env.run(
        env.db.execute,
        "INSERT INTO tokens (id, name, token_hash, grants, expires_at) VALUES ($1, $2, $3, $4, $5)",
        tid, "old-row", secrets.token_hex(32), json.dumps([{"area": _ZONE, "level": "read"}]), "9999-12-31 23:59:59",
    )
    assert _instant(env.listed(tid)["expiresAt"]) == datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    assert env.c.get("/admin/api/tokens").status_code == 200


@pytest.mark.parametrize("path", PATHS)
def test_invalid_expiry_is_rejected(env, path):
    url = "/tokens" if path.startswith("api") else "/admin/api/tokens"
    headers = env.api if path.startswith("api") else env.admin
    body = {"name": f"bad-{secrets.token_hex(6)}", "grants": [{"area": _ZONE, "level": "read"}]}
    if path.endswith("create"):
        r = env.c.post(url, headers=headers, json={**body, "expiresAt": "not-a-date"})
    else:
        r = env.c.post(url, headers=headers, json=body)
        assert r.status_code == 201, r.text
        r = env.c.patch(f"{url}/{r.json()['id']}", headers=headers, json={"expiresAt": "not-a-date"})
    assert r.status_code == 400, r.text

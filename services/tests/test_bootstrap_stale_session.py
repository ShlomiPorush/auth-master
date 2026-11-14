"""Regression tests: only one admin completes bootstrap, and only a live admin holds a full session.

1. A setup session whose admin row a later register deleted must not complete MFA after
   another admin finished bootstrap: mfa-verify answers 403 and issues no full session.
2. A full session whose admin row is missing, or whose admin has MFA disabled, is not an
   admin: /me shows no user, and protected routes answer 401 and delete the session.
3. Two pending admins that verify at the same time: exactly one wins, one row is enabled.
4. SQLite: the enabled row is committed when mfa-verify answers, and mfa-verify writes on
   its own connection, never on the shared request connection, whose open statements and
   transactions belong to other requests.
5. Startup stops with a clear message when the database already holds two MFA-enabled admins.

The tests drive the real HTTP routes.  Redis is an in-memory fake with the get/setex/delete
commands the routes use.  Every secret the app reads is generated per run with ``secrets``.

Each test DELETES EVERY ROW of admin_users in its database, before and after it runs, because
the bootstrap routes only work while no admin has MFA enabled.  The database is a temporary
SQLite file, or, when ``TEST_DATABASE_URL`` is set, that PostgreSQL database: point it only at
a throwaway database used by one test run at a time.

Requires pytest and httpx.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Removed when the test process exits.
_TMP_DIR = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
_DB_URL = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{_TMP_DIR.name}/bootstrap.db"
_EXPECT_PG = _DB_URL.startswith("postgres")

# Fresh values for this run, never ones inherited from the environment.
os.environ.update({
    "ADMIN_API_KEY": secrets.token_urlsafe(32),
    "SESSION_SECRET": secrets.token_urlsafe(48),
    "APP_ENCRYPTION_KEY": secrets.token_hex(16),
})
os.environ.setdefault("REDIS_URL", f"redis://:{secrets.token_hex(32)}@127.0.0.1:6379/0")
os.environ.setdefault("DATABASE_URL", _DB_URL)


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
    def __init__(self, app, db, redis):
        self.app, self.db, self.redis = app, db, redis
        self.clients: list = []

    def client(self):
        import httpx

        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")
        self.clients.append(c)
        return c


@contextlib.asynccontextmanager
async def _env():
    """App wired to this run's database, emptied of admins, and a fake Redis."""
    from app import rate_limit
    from app.config import KNOWN_PUBLIC_VALUES, get_settings
    from app.db import create_database
    from app.main import app
    from app.schema import ensure_database_schema

    s = get_settings()
    for value in (s.admin_api_key, s.session_secret, s.app_encryption_key):
        if not value.strip() or value in KNOWN_PUBLIC_VALUES:
            raise RuntimeError("a secret setting is empty or a placeholder")
    if s.bootstrap_token:
        pytest.skip("BOOTSTRAP_TOKEN is set in this environment")
    saved_state = {n: getattr(app.state, n, None) for n in ("db", "redis")}
    rate_limit._buckets.clear()
    db = await create_database(_DB_URL)
    e = None
    try:
        assert db.is_pg == _EXPECT_PG, "test ran on the wrong database backend"
        await ensure_database_schema(db)
        await db.execute("DELETE FROM admin_users")
        redis = FakeRedis()
        app.state.db, app.state.redis = db, redis
        e = Env(app, db, redis)
        yield e
    finally:
        app.state.db = db
        with contextlib.suppress(Exception):
            await db.execute("DELETE FROM admin_users")
        if e is not None:
            for c in e.clients:
                await c.aclose()
        await db.close()
        for n, v in saved_state.items():
            setattr(app.state, n, v)
        rate_limit._buckets.clear()


async def _register(c, username: str) -> str:
    resp = await c.post(
        "/admin/api/bootstrap/register",
        json={"username": username, "password": secrets.token_urlsafe(18)},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["csrf"]


async def _enroll_secret(c) -> str:
    resp = await c.get("/admin/api/bootstrap/mfa-enroll")
    assert resp.status_code == 200, resp.text
    return resp.json()["secretBase32"]


async def _verify(c, csrf: str, secret: str):
    import pyotp

    return await c.post(
        "/admin/api/bootstrap/mfa-verify",
        json={"code": pyotp.TOTP(secret).now()},
        headers={"X-CSRF-Token": csrf},
    )


async def _enabled_ids(db) -> set[str]:
    return {str(r["id"]) for r in await db.fetch("SELECT id FROM admin_users WHERE totp_enabled = $1", True)}


async def _plant_pending_admin(e: Env, username: str):
    """A pending admin row with its setup session and secret, as register leaves them."""
    import pyotp

    from app.routers.admin_bootstrap import pending_key
    from app.sessions import COOKIE_NAME, SETUP_TTL, new_csrf, new_sid, save_session

    admin_id = str(uuid.uuid4())
    await e.db.execute(
        "INSERT INTO admin_users (id, username, password_hash, totp_enabled) VALUES ($1, $2, $3, $4)",
        admin_id, username, "unused", False,
    )
    secret = pyotp.random_base32()
    await e.redis.setex(pending_key(admin_id), SETUP_TTL, secret)
    sid, csrf = new_sid(), new_csrf()
    await save_session(e.redis, sid, {"kind": "setup", "adminId": admin_id, "csrf": csrf}, SETUP_TTL)
    c = e.client()
    c.cookies.set(COOKIE_NAME, sid)
    return c, csrf, secret


def test_stale_setup_session_cannot_complete_after_bootstrap():
    async def run():
        from app.routers.admin_bootstrap import pending_key
        from app.sessions import COOKIE_NAME, get_session

        async with _env() as e:
            attacker, legit = e.client(), e.client()
            a_csrf = await _register(attacker, "stale-attacker")
            a_secret = await _enroll_secret(attacker)
            a_sid = attacker.cookies.get(COOKIE_NAME)
            a_id = (await get_session(e.redis, a_sid))["adminId"]

            l_csrf = await _register(legit, "legit-admin")  # deletes the attacker's row
            l_secret = await _enroll_secret(legit)
            resp = await _verify(legit, l_csrf, l_secret)
            assert resp.status_code == 200, resp.text
            assert (await legit.get("/admin/api/setup/status")).json() == {"needsSetup": False}

            resp = await _verify(attacker, a_csrf, a_secret)
            assert resp.status_code == 403, f"stale setup session completed MFA: {resp.status_code} {resp.text}"
            assert attacker.cookies.get(COOKIE_NAME) == a_sid, "a new session cookie was issued"
            assert await get_session(e.redis, a_sid) is None, "the stale setup session was kept"
            assert await e.redis.get(pending_key(a_id)) is None, "the stale pending secret was kept"
            enabled = await _enabled_ids(e.db)
            assert len(enabled) == 1
            sessions = [json.loads(v) for k, v in e.redis.data.items() if k.startswith("auth:sess:")]
            ghosts = [s for s in sessions if s.get("kind") == "full" and s.get("adminId") not in enabled]
            assert ghosts == [], "a full session exists for an admin that is not enabled"
            assert (await attacker.get("/admin/api/tokens")).status_code == 401
            assert (await legit.get("/admin/api/tokens")).status_code == 200

    asyncio.run(run())


@pytest.mark.parametrize("admin_state", ["missing", "mfa_disabled"])
def test_full_session_without_an_enabled_admin_is_rejected_and_deleted(admin_state):
    async def run():
        from app.sessions import COOKIE_NAME, PREFIX, SESSION_TTL, new_csrf, new_sid, save_session

        async with _env() as e:
            admin_id = str(uuid.uuid4())
            if admin_state == "mfa_disabled":
                await e.db.execute(
                    "INSERT INTO admin_users (id, username, password_hash, totp_enabled) VALUES ($1, $2, $3, $4)",
                    admin_id, "not-enabled", "unused", False,
                )
            sid = new_sid()
            await save_session(e.redis, sid, {"kind": "full", "adminId": admin_id, "csrf": new_csrf()}, SESSION_TTL)
            c = e.client()
            c.cookies.set(COOKIE_NAME, sid)
            assert (await c.get("/admin/api/me")).json() == {"user": None}
            assert (await c.get("/admin/api/tokens")).status_code == 401
            assert PREFIX + sid not in e.redis.data, "the session was kept"

    asyncio.run(run())


@pytest.mark.parametrize("admin_id", ["not-a-uuid", None, 42])
def test_full_session_with_a_malformed_admin_id_is_rejected(admin_id):
    async def run():
        from app.sessions import COOKIE_NAME, PREFIX, SESSION_TTL, new_csrf, new_sid, save_session

        async with _env() as e:
            sid = new_sid()
            await save_session(e.redis, sid, {"kind": "full", "adminId": admin_id, "csrf": new_csrf()}, SESSION_TTL)
            c = e.client()
            c.cookies.set(COOKIE_NAME, sid)
            assert (await c.get("/admin/api/tokens")).status_code == 401
            assert PREFIX + sid not in e.redis.data, "the session was kept"

    asyncio.run(run())


def test_concurrent_verifies_for_two_pending_admins_enable_exactly_one():
    async def run():
        async with _env() as e:
            c1, csrf1, s1 = await _plant_pending_admin(e, "pending-one")
            c2, csrf2, s2 = await _plant_pending_admin(e, "pending-two")
            r1, r2 = await asyncio.gather(_verify(c1, csrf1, s1), _verify(c2, csrf2, s2))
            codes = sorted([r1.status_code, r2.status_code])
            assert codes == [200, 403], f"expected one 200 and one 403, got {codes}: {r1.text} {r2.text}"
            assert len(await _enabled_ids(e.db)) == 1
            winner, loser = (c1, c2) if r1.status_code == 200 else (c2, c1)
            assert (await loser.get("/admin/api/tokens")).status_code == 401
            assert (await winner.get("/admin/api/tokens")).status_code == 200
            if e.db.is_sqlite:
                assert not e.db._conn.in_transaction, "a transaction was left open on the shared connection"

    asyncio.run(run())


class _RecordingConnection:
    """Passes every call to the shared aiosqlite connection, recording SQL and commits."""

    def __init__(self, conn):
        self._conn = conn
        self.recording = False
        self.statements: list[str] = []
        self.commits = 0

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def execute(self, sql, *args, **kwargs):
        if self.recording:
            self.statements.append(sql)
        return await self._conn.execute(sql, *args, **kwargs)

    async def commit(self):
        if self.recording:
            self.commits += 1
        return await self._conn.commit()


@pytest.mark.skipif(_EXPECT_PG, reason="SQLite connection handling")
def test_sqlite_verify_commits_on_its_own_connection():
    async def run():
        import aiosqlite

        async with _env() as e:
            c = e.client()
            csrf = await _register(c, "committed-admin")
            secret = await _enroll_secret(c)
            shared = e.db._conn
            recorder = _RecordingConnection(shared)
            e.db._conn = recorder
            try:
                recorder.recording = True
                resp = await _verify(c, csrf, secret)
                recorder.recording = False
            finally:
                e.db._conn = shared
            assert resp.status_code == 200, resp.text
            touched = [s for s in recorder.statements if "UPDATE admin_users" in s]
            assert touched == [], f"mfa-verify wrote on the shared connection: {touched}"
            assert recorder.commits == 0, "mfa-verify committed the shared connection"
            assert not shared.in_transaction, "mfa-verify left a transaction open on the shared connection"

            other = await aiosqlite.connect(e.db.path)
            try:
                cur = await other.execute("SELECT COUNT(*) FROM admin_users WHERE totp_enabled = 1")
                assert (await cur.fetchone())[0] == 1, "enabled admin not visible to a second connection"
            finally:
                await other.close()

    asyncio.run(run())


def test_startup_refuses_two_enabled_admins():
    async def run():
        from app.db import UniqueViolationError
        from app.schema import ensure_database_schema

        async with _env() as e:
            await e.db.execute("DROP INDEX IF EXISTS admin_users_one_enabled")
            try:
                for name in ("enabled-one", "enabled-two"):
                    await e.db.execute(
                        "INSERT INTO admin_users (id, username, password_hash, totp_enabled) VALUES ($1, $2, $3, $4)",
                        str(uuid.uuid4()), name, "unused", True,
                    )
                with pytest.raises(RuntimeError, match="more than one admin with MFA enabled"):
                    await ensure_database_schema(e.db)
            finally:
                # Never leave the database without the index.
                await e.db.execute("DELETE FROM admin_users")
                await ensure_database_schema(e.db)
            with pytest.raises(UniqueViolationError):  # the index is back
                for name in ("enabled-three", "enabled-four"):
                    await e.db.execute(
                        "INSERT INTO admin_users (id, username, password_hash, totp_enabled) VALUES ($1, $2, $3, $4)",
                        str(uuid.uuid4()), name, "unused", True,
                    )

    asyncio.run(run())

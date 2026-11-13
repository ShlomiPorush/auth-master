"""Regression tests: a token change must be seen by every /validate request that starts after it.

Two routes used to leave a changed token valid until the validate cache entry expired:
1. A /validate request reads the active row, pauses, an admin change completes, and the
   paused request then stores the old row in the cache.
2. The admin change is saved in the database, but the cache delete never happens
   (Redis error or process death).

The tests drive the real HTTP routes and depend only on behaviour: they pause a request
right after its token row is read from the database, and make every Redis delete fail.
Redis is an in-memory fake with the same get/setex/delete commands, which is enough for
both routes; the tests do not import any cache module.

Every secret the app reads is generated per run with ``secrets``.  The app is imported
inside the tests, so whichever test module imports it first fixes the cached settings;
the API key is read from those settings, and a placeholder secret is refused.  Each test
opens this run's own database: a temporary SQLite file, or the PostgreSQL database in
``TEST_DATABASE_URL`` when that is set.

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
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Removed when the test process exits.
_TMP_DIR = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
_DB_URL = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{_TMP_DIR.name}/race.db"
_EXPECT_PG = _DB_URL.startswith("postgres")

# Used only when this module is the first to load the settings.
os.environ.setdefault("ADMIN_API_KEY", secrets.token_urlsafe(32))
os.environ.setdefault("SESSION_SECRET", secrets.token_urlsafe(48))
os.environ.setdefault("APP_ENCRYPTION_KEY", secrets.token_hex(16))
os.environ.setdefault("REDIS_URL", f"redis://:{secrets.token_hex(32)}@127.0.0.1:6379/0")
os.environ.setdefault("DATABASE_URL", _DB_URL)

_NARROWED = [{"area": "orders", "level": "write"}]
_TOKEN_READ = "FROM tokens WHERE token_hash"
CHANGES = ("revoke", "rotate", "narrow_grants", "delete")


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


class DeleteLostRedis(FakeRedis):
    """Every DEL fails, as if the process died before it reached Redis."""

    async def delete(self, *keys):
        raise ConnectionError("simulated: cache delete never reached Redis")


class _PauseAfterTokenRead:
    """Wraps the database so the next token lookup pauses after it returned its row."""

    def __init__(self, db):
        self._db = db
        self.armed = True
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    def __getattr__(self, name):
        return getattr(self._db, name)

    async def fetchrow(self, sql, *args):
        rec = await self._db.fetchrow(sql, *args)
        if self.armed and _TOKEN_READ in sql:
            self.armed = False
            self.reached.set()
            await self.release.wait()
        return rec


class Env:
    def __init__(self, app, db, redis, client, auth):
        self.app, self.db, self.redis, self.client, self.auth = app, db, redis, client, auth
        self.token_id = None
        self.raw = None


@contextlib.asynccontextmanager
async def _env():
    """App wired to this run's database and a fake Redis, plus one fresh token."""
    import httpx

    from app import rate_limit
    from app.config import KNOWN_PUBLIC_VALUES, get_settings
    from app.db import create_database
    from app.main import app
    from app.schema import ensure_database_schema

    key = get_settings().admin_api_key
    if not key.strip() or key in KNOWN_PUBLIC_VALUES:
        raise RuntimeError("ADMIN_API_KEY is empty or a placeholder")

    saved_state = {n: getattr(app.state, n, None) for n in ("db", "redis")}
    rate_limit._buckets.clear()
    db = await create_database(_DB_URL)
    client = None
    e = None
    try:
        assert db.is_pg == _EXPECT_PG, "test ran on the wrong database backend"
        await ensure_database_schema(db)
        redis = FakeRedis()
        app.state.db, app.state.redis = db, redis
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        e = Env(app, db, redis, client, {"Authorization": f"Bearer {key}"})
        resp = await client.post(
            "/tokens",
            headers=e.auth,
            json={"name": f"race-{secrets.token_hex(6)}", "grants": [{"area": "orders", "level": "read"}]},
        )
        assert resp.status_code == 201, resp.text
        e.token_id, e.raw = resp.json()["id"], resp.json()["token"]
        yield e
    finally:
        app.state.db = db
        if e is not None and e.token_id is not None:
            with contextlib.suppress(Exception):
                await db.execute("DELETE FROM tokens WHERE id = $1", e.token_id)
        if client is not None:
            await client.aclose()
        await db.close()
        for n, v in saved_state.items():
            setattr(app.state, n, v)
        rate_limit._buckets.clear()


async def _validate(e: Env, method: str = "GET") -> bool:
    payload = {"token": e.raw, "area": "orders", "level": "read"}
    if method == "GET":
        resp = await e.client.get("/validate", params=payload)
    else:
        resp = await e.client.post("/validate", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]


async def _apply_change(e: Env, change: str):
    if change == "delete":
        return await e.client.delete(f"/tokens/{e.token_id}", headers=e.auth)
    body = {
        "revoke": {"isActive": False},
        "rotate": {"token": secrets.token_hex(24)},
        "narrow_grants": {"grants": _NARROWED},
    }[change]
    return await e.client.patch(f"/tokens/{e.token_id}", headers=e.auth, json=body)


async def _assert_change_committed(e: Env, change: str) -> None:
    from app.crypto_tokens import sha256_hex

    rec = await e.db.fetchrow("SELECT is_active, token_hash, grants FROM tokens WHERE id = $1", e.token_id)
    if change == "delete":
        assert rec is None, "precondition: token row deleted"
        return
    assert rec is not None, "precondition: token row still exists"
    if change == "revoke":
        assert not rec["is_active"], "precondition: token revoked"
    elif change == "rotate":
        assert rec["token_hash"] != sha256_hex(e.raw), "precondition: token secret rotated"
        assert rec["is_active"], "precondition: rotated token still active"
    elif change == "narrow_grants":
        g = rec["grants"]
        g = json.loads(g) if isinstance(g, (str, bytes)) else g
        assert g == _NARROWED, f"precondition: grants narrowed, got {g!r}"
        assert rec["is_active"], "precondition: narrowed token still active"


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("change", CHANGES)
def test_change_during_concurrent_validate_is_seen_by_later_requests(change, method):
    async def run():
        async with _env() as e:
            pause = _PauseAfterTokenRead(e.db)
            e.app.state.db = pause
            reader = asyncio.create_task(_validate(e, method))
            try:
                await asyncio.wait_for(pause.reached.wait(), 5)
                resp = await _apply_change(e, change)
                assert resp.status_code == 200, resp.text
                pause.release.set()
                await asyncio.wait_for(reader, 5)  # it started before the change; either answer is allowed
            finally:
                pause.release.set()
                if not reader.done():
                    reader.cancel()
                with contextlib.suppress(BaseException):
                    await reader
                e.app.state.db = e.db
            await _assert_change_committed(e, change)
            assert await _validate(e, method) is False, f"token still validates after {change}"

    asyncio.run(run())


@pytest.mark.parametrize("change", CHANGES)
def test_change_with_lost_cache_delete_is_seen_by_later_requests(change):
    async def run():
        async with _env() as e:
            assert await _validate(e) is True
            lost = DeleteLostRedis()
            lost.data = e.redis.data
            e.app.state.redis = lost
            try:
                resp = await _apply_change(e, change)
                assert resp.status_code == 200, resp.text
            except ConnectionError:
                pass  # the route failed after its database change; the change itself is asserted below
            finally:
                e.app.state.redis = e.redis
            await _assert_change_committed(e, change)
            assert await _validate(e) is False, f"token still validates after {change} with a lost cache delete"

    asyncio.run(run())


def test_control_unchanged_token_keeps_validating():
    """Control: the setup works and an untouched token stays valid, repeatedly."""

    async def run():
        async with _env() as e:
            for method in ("GET", "POST", "GET"):
                assert await _validate(e, method) is True

    asyncio.run(run())

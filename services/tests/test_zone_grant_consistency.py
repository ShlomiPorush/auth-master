"""Regression tests: zone changes and token grant writes must not leave a token authorized
for an area that is not a zone, or undo a grant change.

Three routes used to do that:
1. A zone delete or rename on SQLite rewrote every token's grants from one earlier read, so
   a grant change saved in between was overwritten (a revoked area came back).
2. A token grant write checked the zones and then wrote in a separate step, so a zone
   deleted or renamed in between left a grant on an area that no longer exists, and
   /validate kept answering true for it.
3. A zone delete or rename on SQLite committed the zone change before the grant rewrite,
   so a failure in between left grants on the old zone name with no zone to retry from.

The interleaving tests wrap the request's database handle and run the competing request
after each database call the first request makes, one trial per call, so every point
between two of its statements is tried; they depend only on the HTTP behaviour and the
stored rows.  The failure tests make the grant rewrite fail with a database trigger.

Every secret the app reads is generated per run with ``secrets``; a placeholder is refused.
Redis is an in-memory fake.  Each test opens this run's own database: a temporary SQLite
file, or the PostgreSQL database in ``TEST_DATABASE_URL`` when that is set.  The tests
create and delete only their own zones and tokens (names starting with ``zgc-``).

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
_DB_URL = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{_TMP_DIR.name}/zones.db"
# As create_database() decides: anything that is not a sqlite URL goes to asyncpg.
_EXPECT_PG = not _DB_URL.startswith("sqlite")

# Generated for this run, never taken from the environment the tests run in.  They take
# effect when this module is the first to load the settings; the tests read the API key
# from the loaded settings and refuse a placeholder.
os.environ.update(
    {
        "ADMIN_API_KEY": secrets.token_urlsafe(32),
        "SESSION_SECRET": secrets.token_urlsafe(48),
        "APP_ENCRYPTION_KEY": secrets.token_hex(16),
        # Redis is an in-memory fake; this URL is never connected to.
        "REDIS_URL": f"redis://:{secrets.token_hex(32)}@127.0.0.1:6379/0",
    }
)
os.environ.setdefault("DATABASE_URL", _DB_URL)

_MAX_STEPS = 16  # more database calls than any of the routes under test makes
_TRIGGER = "zgc_fail_grant_rewrite"


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, k):
        return self.data.get(k)

    async def setex(self, k, _ttl, v):
        self.data[k] = v

    async def set(self, k, v, *a, **kw):
        self.data[k] = v

    async def delete(self, *keys):
        for k in keys:
            self.data.pop(k, None)

    async def ping(self):
        return True

    async def aclose(self):
        pass


class _InjectAfterCall:
    """Database wrapper that runs ``inject`` once, right after its ``step``-th call returned."""

    def __init__(self, db, step: int, inject):
        self._db = db
        self._step = step
        self._inject = inject
        self.calls = 0
        self.fired = False
        self.armed = True

    def __getattr__(self, name):
        return getattr(self._db, name)

    async def _after(self) -> None:
        if not self.armed:
            return
        self.calls += 1
        if self.calls == self._step:
            self.armed = False
            self.fired = True
            await self._inject()

    async def execute(self, sql, *args):
        r = await self._db.execute(sql, *args)
        await self._after()
        return r

    async def fetch(self, sql, *args):
        r = await self._db.fetch(sql, *args)
        await self._after()
        return r

    async def fetchrow(self, sql, *args):
        r = await self._db.fetchrow(sql, *args)
        await self._after()
        return r

    async def fetchval(self, sql, *args):
        r = await self._db.fetchval(sql, *args)
        await self._after()
        return r


class Env:
    def __init__(self, app, db, client, api, admin):
        self.app, self.db, self.client, self.api, self.admin = app, db, client, api, admin
        self.prefix = f"zgc-{secrets.token_hex(4)}"

    async def zone(self, suffix: str) -> dict:
        resp = await self.client.post("/admin/api/zones", headers=self.admin, json={"name": f"{self.prefix}-{suffix}"})
        assert resp.status_code == 201, resp.text
        return resp.json()

    async def token(self, suffix: str, grants: list[dict]) -> dict:
        body = {"name": f"{self.prefix}-{suffix}", "grants": grants, "token": secrets.token_hex(24)}
        resp = await self.client.post("/tokens", headers=self.api, json=body)
        assert resp.status_code == 201, resp.text
        out = resp.json()
        out["raw"] = body["token"]
        return out

    async def grants_of(self, token_id: str) -> list[dict] | None:
        rec = await self.db.fetchrow("SELECT grants FROM tokens WHERE id = $1", token_id)
        if rec is None:
            return None
        g = rec["grants"]
        return json.loads(g) if isinstance(g, (str, bytes)) else g

    async def token_id_by_name(self, name: str) -> str | None:
        return await self.db.fetchval("SELECT id FROM tokens WHERE name = $1", name)

    async def zone_names(self) -> set[str]:
        return {r["name"] for r in await self.db.fetch("SELECT name FROM zones")}

    async def validate(self, raw: str, area: str, level: str = "read") -> bool:
        resp = await self.client.post("/validate", json={"token": raw, "area": area, "level": level})
        assert resp.status_code == 200, resp.text
        return resp.json()["result"]

    def area(self, suffix: str) -> str:
        return f"{self.prefix}-{suffix}"


def _refuse_public_database_password() -> None:
    """The tests connect to _DB_URL directly, so validate it as the app validates DATABASE_URL
    (the URL's password and PGPASSWORD, which asyncpg uses when the URL has none)."""
    from pydantic import ValidationError

    from app.config import Settings, _describe

    try:
        Settings(DATABASE_URL=_DB_URL)
    except ValidationError as error:
        raise RuntimeError(f"TEST_DATABASE_URL: {_describe(error)}") from None


@contextlib.asynccontextmanager
async def _env():
    """App wired to this run's database, a fake Redis, a full admin session and the API key."""
    import httpx

    from app import rate_limit
    from app.config import KNOWN_PUBLIC_VALUES, get_settings
    from app.db import create_database
    from app.main import app
    from app.schema import ensure_database_schema
    from app.sessions import COOKIE_NAME, PREFIX

    s = get_settings()
    for field in ("admin_api_key", "session_secret", "app_encryption_key"):
        value = getattr(s, field)
        if not value.strip() or value.strip() in KNOWN_PUBLIC_VALUES:
            raise RuntimeError(f"{field} is empty or a placeholder")
    key = s.admin_api_key
    _refuse_public_database_password()

    saved_state = {n: getattr(app.state, n, None) for n in ("db", "redis")}
    rate_limit._buckets.clear()
    db = await create_database(_DB_URL)
    client = None
    e = None
    try:
        assert db.is_pg == _EXPECT_PG, "test ran on the wrong database backend"
        await ensure_database_schema(db)
        redis = FakeRedis()
        sid, csrf = secrets.token_hex(32), secrets.token_hex(24)
        redis.data[PREFIX + sid] = json.dumps({"kind": "full", "adminId": str(uuid.uuid4()), "csrf": csrf})
        app.state.db, app.state.redis = db, redis
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        )
        client.cookies.set(COOKIE_NAME, sid)
        e = Env(app, db, client, {"Authorization": f"Bearer {key}"}, {"X-CSRF-Token": csrf})
        yield e
    finally:
        app.state.db = db
        if e is not None:
            with contextlib.suppress(Exception):
                await _drop_failure_trigger(db)
            with contextlib.suppress(Exception):
                await db.execute("DELETE FROM tokens WHERE name LIKE $1", e.prefix + "-%")
            with contextlib.suppress(Exception):
                await db.execute("DELETE FROM zones WHERE name LIKE $1", e.prefix + "-%")
        if client is not None:
            await client.aclose()
        await db.close()
        for n, v in saved_state.items():
            setattr(app.state, n, v)
        rate_limit._buckets.clear()


async def _run_with_injection(e: Env, step: int, request, inject):
    """Run ``request()`` with ``inject()`` run after its ``step``-th database call."""
    wrapped = _InjectAfterCall(e.db, step, inject)
    e.app.state.db = wrapped
    try:
        resp = await asyncio.wait_for(request(), 30)
    finally:
        e.app.state.db = e.db
    return resp, wrapped.fired


async def _zone_op(e: Env, op: str, zone: dict, new_suffix: str):
    if op == "delete":
        return await e.client.delete(f"/admin/api/zones/{zone['id']}", headers=e.admin)
    return await e.client.patch(
        f"/admin/api/zones/{zone['id']}", headers=e.admin, json={"name": e.area(new_suffix)}
    )


async def _grant_write(e: Env, route: str, token: dict | None, name: str, grants: list[dict]):
    if route == "api_create":
        return await e.client.post("/tokens", headers=e.api, json={"name": name, "grants": grants})
    if route == "admin_create":
        return await e.client.post("/admin/api/tokens", headers=e.admin, json={"name": name, "grants": grants})
    if route == "api_patch":
        return await e.client.patch(f"/tokens/{token['id']}", headers=e.api, json={"grants": grants})
    return await e.client.patch(f"/admin/api/tokens/{token['id']}", headers=e.admin, json={"grants": grants})


GRANT_ROUTES = ("api_create", "admin_create", "api_patch", "admin_patch")
ZONE_OPS = ("delete", "rename")


# ---------------------------------------------------------------------------
# 1. A grant change during a zone delete or rename is kept
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("revoke_route", ("api_patch", "admin_patch"))
@pytest.mark.parametrize("op", ZONE_OPS)
def test_grant_revocation_during_zone_change_is_kept(op, revoke_route):
    async def run():
        async with _env() as e:
            await e.zone("public")
            await e.zone("secret")
            for step in range(1, _MAX_STEPS + 1):
                old = await e.zone(f"old{step}")
                # Tokens that the zone change rewrites, created before the target token.
                for i in range(3):
                    await e.token(f"f{step}-{i}", [{"area": old["name"], "level": "read"}])
                target = await e.token(
                    f"t{step}",
                    [{"area": old["name"], "level": "read"}, {"area": e.area("secret"), "level": "all"}],
                )
                narrowed = [{"area": e.area("public"), "level": "read"}]

                async def revoke():
                    resp = await _grant_write(e, revoke_route, target, "", narrowed)
                    assert resp.status_code == 200, resp.text

                resp, fired = await _run_with_injection(
                    e, step, lambda: _zone_op(e, op, old, f"new{step}"), revoke
                )
                assert resp.status_code == 200, resp.text
                if not fired:
                    break
                assert await e.grants_of(target["id"]) == narrowed, (
                    f"grant revocation saved after database call {step} of the zone {op} was overwritten"
                )
                assert await e.validate(target["raw"], e.area("secret"), "delete") is False
            else:
                pytest.fail("the zone change made more database calls than the test covers")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 2. A grant written during a zone delete or rename never names a missing zone
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("route", GRANT_ROUTES)
@pytest.mark.parametrize("op", ZONE_OPS)
def test_grant_write_during_zone_change_leaves_no_grant_on_a_missing_zone(op, route):
    async def run():
        async with _env() as e:
            base = await e.zone("base")
            for step in range(1, _MAX_STEPS + 1):
                zone = await e.zone(f"z{step}")
                token = None
                if route.endswith("_patch"):
                    token = await e.token(f"t{step}", [{"area": base["name"], "level": "read"}])
                name = f"{e.prefix}-n{step}"

                async def change_zone():
                    resp = await _zone_op(e, op, zone, f"r{step}")
                    assert resp.status_code == 200, resp.text

                resp, fired = await _run_with_injection(
                    e, step,
                    lambda: _grant_write(e, route, token, name, [{"area": zone["name"], "level": "read"}]),
                    change_zone,
                )
                assert resp.status_code in (200, 201, 400), resp.text
                if not fired:
                    break
                assert zone["name"] not in await e.zone_names(), "precondition: zone deleted or renamed"
                tid = token["id"] if token else await e.token_id_by_name(name)
                if tid is not None:
                    stored = await e.grants_of(tid) or []
                    zones = await e.zone_names()
                    assert all(g["area"] in zones for g in stored), (
                        f"{route} with the zone {op} after its database call {step} stored a grant "
                        f"on a missing zone: {stored}"
                    )
                if resp.status_code == 400:
                    assert "Area not allowed" in resp.text, resp.text
                else:
                    raw = resp.json().get("token") if route.endswith("_create") else token["raw"]
                    assert await e.validate(raw, zone["name"]) is False
            else:
                pytest.fail("the grant write made more database calls than the test covers")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 3. A zone delete or rename that fails changes nothing, and a retry works
# ---------------------------------------------------------------------------

async def _create_failure_trigger(db, token_name: str) -> None:
    # The token name is a test-made value of hex characters; never user input.
    assert all(c.isalnum() or c == "-" for c in token_name)
    if db.is_pg:
        await db.execute(
            f"CREATE OR REPLACE FUNCTION {_TRIGGER}() RETURNS trigger AS $f$ "
            "BEGIN RAISE EXCEPTION 'injected failure of the grant rewrite'; END $f$ LANGUAGE plpgsql"
        )
        await db.execute(
            f"CREATE TRIGGER {_TRIGGER} BEFORE UPDATE OF grants ON tokens FOR EACH ROW "
            f"WHEN (NEW.name = '{token_name}') EXECUTE FUNCTION {_TRIGGER}()"
        )
    else:
        await db.execute(
            f"CREATE TRIGGER {_TRIGGER} BEFORE UPDATE OF grants ON tokens "
            f"WHEN NEW.name = '{token_name}' BEGIN SELECT RAISE(ABORT, 'injected failure of the grant rewrite'); END"
        )


async def _drop_failure_trigger(db) -> None:
    if db.is_pg:
        await db.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER} ON tokens")
        await db.execute(f"DROP FUNCTION IF EXISTS {_TRIGGER}()")
    else:
        await db.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER}")


@pytest.mark.parametrize("op", ZONE_OPS)
def test_failed_zone_change_changes_nothing_and_can_be_retried(op):
    async def run():
        async with _env() as e:
            zone = await e.zone("doomed")
            other = await e.zone("other")
            third = await e.zone("third")
            grants = [
                {"area": other["name"], "level": "read"},
                {"area": zone["name"], "level": "read,write"},
                {"area": third["name"], "level": "read,write,delete"},
            ]
            token = await e.token("t", grants)
            only = await e.token("only", [{"area": zone["name"], "level": "read"}])
            untouched = await e.token("untouched", [{"area": third["name"], "level": "read"}])
            await _create_failure_trigger(e.db, token["name"])

            resp = await _zone_op(e, op, zone, "renamed")
            assert resp.status_code == 500, resp.text
            assert zone["name"] in await e.zone_names(), f"zone {op} was saved although the grant rewrite failed"
            assert await e.grants_of(token["id"]) == grants
            assert await e.grants_of(only["id"]) == [{"area": zone["name"], "level": "read"}]

            await _drop_failure_trigger(e.db)
            resp = await _zone_op(e, op, zone, "renamed")
            assert resp.status_code == 200, resp.text
            assert zone["name"] not in await e.zone_names()
            if op == "rename":
                renamed = e.area("renamed")
                expected = [grants[0], {"area": renamed, "level": "read,write"}, grants[2]]
                expected_only = [{"area": renamed, "level": "read"}]
            else:
                expected = [grants[0], grants[2]]
                expected_only = []
            # Order and levels are kept; a token whose only grant was the zone keeps [].
            assert await e.grants_of(token["id"]) == expected
            assert await e.grants_of(only["id"]) == expected_only
            assert await e.grants_of(untouched["id"]) == [{"area": third["name"], "level": "read"}]
            assert await e.validate(token["raw"], zone["name"]) is False

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 4. ALLOWED_AREAS fallback: a grant write that started while there were no zones
#    is refused when a zone is created before it writes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("route", GRANT_ROUTES)
def test_fallback_grant_write_is_refused_after_the_first_zone_is_created(route, monkeypatch):
    from app.config import get_settings

    async def run():
        async with _env() as e:
            if await e.zone_names():
                pytest.skip("the zones table of this database is not empty")
            area = e.area("env")
            monkeypatch.setattr(get_settings(), "allowed_areas", area)
            token = None
            if route.endswith("_patch"):
                token = await e.token("t", [{"area": area, "level": "read"}])
            seen_before: dict[int, bool] = {}
            for step in range(1, _MAX_STEPS + 1):
                name = f"{e.prefix}-n{step}"
                level = "write" if step % 2 else "delete"  # each trial writes a different grant
                wanted = [{"area": area, "level": level}]

                async def create_zone():
                    tid = token["id"] if token else await e.token_id_by_name(name)
                    seen_before[step] = tid is not None and (await e.grants_of(tid)) == wanted
                    z = await e.zone(f"z{step}")
                    created.append(z)

                created: list[dict] = []
                resp, fired = await _run_with_injection(
                    e, step, lambda: _grant_write(e, route, token, name, wanted), create_zone
                )
                for z in created:
                    r = await e.client.delete(f"/admin/api/zones/{z['id']}", headers=e.admin)
                    assert r.status_code == 200, r.text
                if not fired:
                    break
                if seen_before[step]:
                    continue  # written before the zone existed: kept by design
                tid = token["id"] if token else await e.token_id_by_name(name)
                stored = (await e.grants_of(tid)) if tid else None
                assert stored != wanted, (
                    f"{route} wrote an ALLOWED_AREAS grant after a zone was created at its database call {step}"
                )
                assert resp.status_code == 400, resp.text
            else:
                pytest.fail("the grant write made more database calls than the test covers")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 5. A grant write that runs while a zone change is not yet committed on another
#    connection waits for it, and is refused when the change removed the zone
# ---------------------------------------------------------------------------

@contextlib.asynccontextmanager
async def _uncommitted(statements: list[tuple[str, tuple]]):
    """Run ``statements`` in a transaction on a second connection; commit when the block ends."""
    if _EXPECT_PG:
        import asyncpg

        conn = await asyncpg.connect(_DB_URL)
        try:
            tr = conn.transaction()
            await tr.start()
            for sql, args in statements:
                await conn.execute(sql, *args)
            yield
            await tr.commit()
        finally:
            await conn.close()
    else:
        import aiosqlite

        from app.db import _pg_to_sqlite

        conn = await aiosqlite.connect(_DB_URL.split("///", 1)[1], isolation_level=None)
        try:
            await conn.execute("BEGIN IMMEDIATE")
            for sql, args in statements:
                await conn.execute(*_pg_to_sqlite(sql, args))
            yield
            await conn.execute("COMMIT")
        finally:
            await conn.close()


async def _until_waiting_or_done(e: Env, task: asyncio.Task, query: str) -> None:
    """Return when ``task`` finished or (PostgreSQL) its statement ``query`` waits for a lock.

    ``query`` is a LIKE pattern for the statement the request under test runs; the test's
    own sessions never run it.  SQLite shows no lock wait, so there it waits 1 s, well
    inside the 5 s busy timeout; a request that has not reached the database by then
    makes the test pass without exercising the wait, never fail wrongly.
    """
    if not _EXPECT_PG:
        await asyncio.wait({task}, timeout=1.0)
        return
    for _ in range(400):
        if task.done():
            return
        waiting = await e.db.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            "AND wait_event_type = 'Lock' AND query LIKE $1 AND pid <> pg_backend_pid()",
            query,
        )
        if waiting:
            return
        await asyncio.sleep(0.025)
    raise AssertionError("the request neither finished nor waited for a lock within 10 s")


async def _grant_write_across_commit(e: Env, statements, request, before_commit=None):
    """Start ``request()``; once it waits for a lock or finished, commit ``statements``.

    Returns the response and the result of ``before_commit()``, run just before the commit.
    """
    async with _uncommitted(statements):
        task = asyncio.create_task(request())
        try:
            await _until_waiting_or_done(e, task, "LOCK TABLE zones IN SHARE MODE%")
            seen = await before_commit() if before_commit else None
        except BaseException:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            raise
    return await asyncio.wait_for(task, 30), seen


def _zone_change_sql(op: str, zone: dict, new_name: str) -> list[tuple[str, tuple]]:
    zid = uuid.UUID(zone["id"]) if _EXPECT_PG else zone["id"]
    if op == "delete":
        return [("DELETE FROM zones WHERE id = $1", (zid,))]
    return [("UPDATE zones SET name = $1 WHERE id = $2", (new_name, zid))]


@pytest.mark.parametrize("route", GRANT_ROUTES)
@pytest.mark.parametrize("op", ZONE_OPS)
def test_grant_write_waits_for_an_uncommitted_zone_change(op, route):
    async def run():
        async with _env() as e:
            base = await e.zone("base")
            zone = await e.zone("z")
            token = await e.token("t", [{"area": base["name"], "level": "read"}]) if route.endswith("_patch") else None
            name = f"{e.prefix}-n"
            resp, _ = await _grant_write_across_commit(
                e,
                _zone_change_sql(op, zone, e.area("r")),
                lambda: _grant_write(e, route, token, name, [{"area": zone["name"], "level": "read"}]),
            )
            assert zone["name"] not in await e.zone_names(), "precondition: zone change committed"
            tid = token["id"] if token else await e.token_id_by_name(name)
            stored = (await e.grants_of(tid) or []) if tid else []
            assert all(g["area"] != zone["name"] for g in stored), (
                f"{route} stored a grant on a zone that a concurrent {op} removed: {stored}"
            )
            assert resp.status_code == 400, resp.text
            assert "Area not allowed" in resp.text, resp.text

    asyncio.run(run())


@pytest.mark.parametrize("route", GRANT_ROUTES)
def test_fallback_grant_write_waits_for_an_uncommitted_first_zone(route, monkeypatch):
    from app.config import get_settings

    async def run():
        async with _env() as e:
            if await e.zone_names():
                pytest.skip("the zones table of this database is not empty")
            area = e.area("env")
            monkeypatch.setattr(get_settings(), "allowed_areas", area)
            token = await e.token("t", [{"area": area, "level": "read"}]) if route.endswith("_patch") else None
            name = f"{e.prefix}-n"
            wanted = [{"area": area, "level": "write"}]
            zid = uuid.uuid4()

            async def stored():
                tid = token["id"] if token else await e.token_id_by_name(name)
                return (await e.grants_of(tid)) if tid else None

            async def written_before_commit():
                return await stored() == wanted

            resp, written_first = await _grant_write_across_commit(
                e,
                [("INSERT INTO zones (id, name) VALUES ($1, $2)", (zid if _EXPECT_PG else str(zid), e.area("first")))],
                lambda: _grant_write(e, route, token, name, wanted),
                written_before_commit,
            )
            if written_first:
                # Saved while there was no zone yet: kept by design.
                assert resp.status_code in (200, 201), resp.text
                return
            assert await stored() != wanted, f"{route} saved an ALLOWED_AREAS grant after the first zone was committed"
            assert resp.status_code == 400, resp.text

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 6. Unique values still answer 409
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("route", GRANT_ROUTES)
def test_duplicate_token_value_answers_409(route):
    async def run():
        async with _env() as e:
            zone = await e.zone("z")
            grants = [{"area": zone["name"], "level": "read"}]
            existing = await e.token("existing", grants)
            other = await e.token("other", grants)
            body = {"token": existing["raw"], "grants": grants}
            if route.endswith("_create"):
                body["name"] = f"{e.prefix}-dup"
                path = "/tokens" if route == "api_create" else "/admin/api/tokens"
                resp = await e.client.post(path, headers=e.api if route == "api_create" else e.admin, json=body)
            else:
                path = f"/tokens/{other['id']}" if route == "api_patch" else f"/admin/api/tokens/{other['id']}"
                resp = await e.client.patch(path, headers=e.api if route == "api_patch" else e.admin, json=body)
            assert resp.status_code == 409, resp.text
            assert await e.grants_of(other["id"]) == grants

    asyncio.run(run())


def test_zone_rename_to_an_existing_name_answers_409():
    async def run():
        async with _env() as e:
            a = await e.zone("a")
            b = await e.zone("b")
            token = await e.token("t", [{"area": a["name"], "level": "read"}])
            resp = await e.client.patch(f"/admin/api/zones/{a['id']}", headers=e.admin, json={"name": b["name"]})
            assert resp.status_code == 409, resp.text
            assert await e.zone_names() >= {a["name"], b["name"]}
            assert await e.grants_of(token["id"]) == [{"area": a["name"], "level": "read"}]

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 7. Reverse order: a zone change that starts while a grant write is inside its
#    transaction waits for it, and then rewrites the grant it saved
# ---------------------------------------------------------------------------

class _PauseAfterTokenWrite:
    """Pauses the transaction statement that writes the token carrying ``marker``, after it returned.

    Wraps the methods of the transaction handle classes (app.db._SqliteConn, _PgConn), so
    the pause is inside the grant write's transaction, before its commit.
    """

    def __init__(self, monkeypatch, marker: str):
        from app import db as db_mod

        self.marker = marker
        self.reached = asyncio.Event()
        self.release = asyncio.Event()
        self.armed = True
        for cls in (db_mod._SqliteConn, db_mod._PgConn):
            for name in ("execute", "fetchval"):
                monkeypatch.setattr(cls, name, self._wrap(getattr(cls, name)))

    def _wrap(self, method):
        async def wrapped(conn, sql, *args):
            result = await method(conn, sql, *args)
            if self.armed and sql.lstrip().startswith(("INSERT INTO tokens", "UPDATE tokens SET")) and self.marker in args:
                self.armed = False
                self.reached.set()
                await self.release.wait()
            return result

        return wrapped


async def _repeatable_read_database():
    """A PostgreSQL handle whose sessions default to REPEATABLE READ."""
    import asyncpg

    from app.db import PgDatabase

    pool = await asyncpg.create_pool(
        _DB_URL, min_size=1, max_size=10, server_settings={"default_transaction_isolation": "repeatable read"}
    )
    return PgDatabase(pool)


@pytest.mark.parametrize("isolation", ("server_default", "repeatable_read"))
@pytest.mark.parametrize("route", GRANT_ROUTES)
@pytest.mark.parametrize("op", ZONE_OPS)
def test_zone_change_during_grant_write_rewrites_the_new_grant(op, route, isolation, monkeypatch):
    if isolation == "repeatable_read" and not _EXPECT_PG:
        pytest.skip("PostgreSQL only")

    async def run():
        async with _env() as e:
            base = await e.zone("base")
            zone = await e.zone("z")
            token = await e.token("t", [{"area": base["name"], "level": "read"}]) if route.endswith("_patch") else None
            name = f"{e.prefix}-n"
            pause = _PauseAfterTokenWrite(monkeypatch, token["id"] if token else name)
            rr_db = await _repeatable_read_database() if isolation == "repeatable_read" else None
            if rr_db is not None:
                e.app.state.db = rr_db
            write = asyncio.create_task(_grant_write(e, route, token, name, [{"area": zone["name"], "level": "read"}]))
            change = None
            try:
                await asyncio.wait_for(pause.reached.wait(), 10)
                change = asyncio.create_task(_zone_op(e, op, zone, "r"))
                await _until_waiting_or_done(e, change, "DELETE FROM zones%" if op == "delete" else "UPDATE zones SET%")
                assert not change.done(), f"the zone {op} did not wait for the open grant write: {change.result().text}"
                pause.release.set()
                resp = await asyncio.wait_for(write, 30)
                zresp = await asyncio.wait_for(change, 30)
            finally:
                pause.release.set()
                for t in (write, change):
                    if t is not None and not t.done():
                        t.cancel()
                    if t is not None:
                        with contextlib.suppress(BaseException):
                            await t
                e.app.state.db = e.db
                if rr_db is not None:
                    await rr_db.close()
            assert resp.status_code in (200, 201), resp.text
            assert zresp.status_code == 200, zresp.text
            tid = token["id"] if token else await e.token_id_by_name(name)
            stored = await e.grants_of(tid)
            zones = await e.zone_names()
            assert all(g["area"] in zones for g in stored), f"grant on a missing zone after the {op}: {stored}"
            if op == "rename":
                assert {"area": e.area("r"), "level": "read"} in stored, stored
            raw = resp.json()["token"] if route.endswith("_create") else token["raw"]
            assert await e.validate(raw, zone["name"]) is False

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 8. The last zone is deleted while ALLOWED_AREAS is empty: a grant write refuses
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("how", ("uncommitted", "between_calls"))
@pytest.mark.parametrize("route", GRANT_ROUTES)
def test_grant_write_refused_when_the_last_zone_is_deleted(route, how, monkeypatch):
    from app.config import get_settings

    async def run():
        async with _env() as e:
            if await e.zone_names():
                pytest.skip("the zones table of this database is not empty")
            monkeypatch.setattr(get_settings(), "allowed_areas", "")
            steps = range(1, 2) if how == "uncommitted" else range(1, _MAX_STEPS + 1)
            for step in steps:
                zone = await e.zone(f"only{step}")
                token = await e.token(f"t{step}", [{"area": zone["name"], "level": "read"}]) if route.endswith("_patch") else None
                name = f"{e.prefix}-n{step}"
                wanted = [{"area": zone["name"], "level": "write"}]

                def request():
                    return _grant_write(e, route, token, name, wanted)

                async def written() -> bool:
                    tid = token["id"] if token else await e.token_id_by_name(name)
                    return tid is not None and wanted[0] in (await e.grants_of(tid) or [])

                if how == "uncommitted":
                    # The test's own delete does not rewrite grants, so only the new grant is checked.
                    resp, written_first = await _grant_write_across_commit(
                        e, _zone_change_sql("delete", zone, ""), request, written
                    )
                    assert not written_first, "the grant write did not wait for the uncommitted zone delete"
                    assert not await written(), f"{route} stored a grant on the deleted last zone: {wanted}"
                else:
                    seen: list[bool] = []

                    async def delete_zone():
                        seen.append(await written())
                        r = await _zone_op(e, "delete", zone, "")
                        assert r.status_code == 200, r.text

                    resp, fired = await _run_with_injection(e, step, request, delete_zone)
                    if not fired:
                        r = await _zone_op(e, "delete", zone, "")
                        assert r.status_code == 200, r.text
                        break
                    written_first = seen[0]
                    tid = token["id"] if token else await e.token_id_by_name(name)
                    stored = (await e.grants_of(tid) or []) if tid else []
                    assert all(g["area"] != zone["name"] for g in stored), (
                        f"{route} kept a grant on the deleted last zone (step {step}): {stored}"
                    )
                assert not await e.zone_names(), "precondition: the last zone was deleted"
                if written_first:
                    assert resp.status_code in (200, 201), resp.text  # saved before the delete
                else:
                    assert resp.status_code == 400, resp.text
                    assert "Define at least one zone" in resp.text or "Area not allowed" in resp.text, resp.text
            else:
                if how == "between_calls":
                    pytest.fail("the grant write made more database calls than the test covers")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 9. SQLite: a failed write on the shared connection does not hold the write lock
# ---------------------------------------------------------------------------

def test_write_after_a_409_on_the_shared_connection_does_not_wait():
    if _EXPECT_PG:
        pytest.skip("SQLite only")

    async def run():
        async with _env() as e:
            a = await e.zone("a")
            b = await e.zone("b")
            token = await e.token("t", [{"area": a["name"], "level": "read"}])
            # A UNIQUE violation on the shared request connection, and no other write after it.
            resp = await e.client.post("/admin/api/zones", headers=e.admin, json={"name": a["name"]})
            assert resp.status_code == 409, resp.text
            # Each must get the write lock well before the 5 s busy timeout runs out.
            resp = await asyncio.wait_for(
                e.client.patch(f"/tokens/{token['id']}", headers=e.api, json={"grants": [{"area": b["name"], "level": "read"}]}),
                3,
            )
            assert resp.status_code == 200, resp.text
            resp = await e.client.post("/admin/api/zones", headers=e.admin, json={"name": b["name"]})
            assert resp.status_code == 409, resp.text
            resp = await asyncio.wait_for(e.client.delete(f"/admin/api/zones/{a['id']}", headers=e.admin), 3)
            assert resp.status_code == 200, resp.text

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 10. A zone change reads stored grants the way /validate reads them
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("op", ZONE_OPS)
def test_zone_change_reads_stored_grants_like_validate(op):
    """Rows not written by the current API: a string element and a repeated "area" key."""
    from app.crypto_tokens import sha256_hex

    async def run():
        async with _env() as e:
            old = await e.zone("old")
            keep = await e.zone("keep")
            ghost = e.area("ghost")  # not a zone

            async def raw_token(suffix: str, grants_text: str) -> str:
                raw = secrets.token_hex(24)
                await e.db.execute(
                    "INSERT INTO tokens (id, name, token_hash, grants) VALUES ($1::uuid, $2, $3, $4::jsonb)",
                    str(uuid.uuid4()), f"{e.prefix}-{suffix}", sha256_hex(raw), grants_text,
                )
                return raw

            # A string whose text is a grant object: /validate ignores it, it is no grant.
            nested = json.dumps({"area": ghost, "level": "all"})
            string_elem = await raw_token("str", json.dumps([{"area": old["name"], "level": "read"}, nested]))
            # A repeated key: /validate (json.loads) reads the last value, here the zone being changed.
            repeated = await raw_token(
                "dup", '[{"area": "%s", "area": "%s", "level": "read"}]' % (keep["name"], old["name"])
            )
            assert await e.validate(string_elem, ghost, "delete") is False, "precondition"
            assert await e.validate(repeated, old["name"]) is True, "precondition"

            resp = await _zone_op(e, op, old, "renamed")
            assert resp.status_code == 200, resp.text

            assert await e.validate(string_elem, ghost, "delete") is False, "a string element became a grant"
            assert await e.validate(repeated, old["name"]) is False, "a grant on the changed zone was kept"
            if op == "rename":
                assert await e.validate(repeated, e.area("renamed")) is True

    asyncio.run(run())


def test_zone_rename_does_not_turn_a_non_string_area_into_a_grant():
    """A stored area that is a number or a boolean is no grant (/validate needs a string),
    also after the zone with the same text is renamed."""
    from app.crypto_tokens import sha256_hex

    async def run():
        async with _env() as e:
            number = str(10**11 + secrets.randbelow(10**11))
            cases = [(number, int(number)), ("true", True)]
            if "true" in await e.zone_names():
                pytest.skip("this database already has a zone named true")
            created = []
            try:
                for text, stored_area in cases:
                    resp = await e.client.post("/admin/api/zones", headers=e.admin, json={"name": text})
                    assert resp.status_code == 201, resp.text
                    created.append(resp.json())
                    raw = secrets.token_hex(24)
                    await e.db.execute(
                        "INSERT INTO tokens (id, name, token_hash, grants) VALUES ($1::uuid, $2, $3, $4::jsonb)",
                        str(uuid.uuid4()), f"{e.prefix}-{text}", sha256_hex(raw),
                        json.dumps([{"area": stored_area, "level": "all"}]),
                    )
                    new_name = e.area(f"from-{text}")
                    resp = await e.client.patch(
                        f"/admin/api/zones/{created[-1]['id']}", headers=e.admin, json={"name": new_name}
                    )
                    assert resp.status_code == 200, resp.text
                    assert await e.validate(raw, new_name, "delete") is False, (
                        f"renaming zone {text!r} turned the stored area {stored_area!r} into a grant"
                    )
            finally:
                for z in created:
                    with contextlib.suppress(Exception):
                        await e.db.execute("DELETE FROM zones WHERE id = $1", uuid.UUID(z["id"]) if _EXPECT_PG else z["id"])

    asyncio.run(run())

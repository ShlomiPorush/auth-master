"""Regression tests: zone identity follows /validate, which compares areas regardless of case.

/validate matches an area to a grant with ``strip().lower()``.  Zone names used to be
unique only in their exact spelling, so zones ``billing`` and ``BILLING`` could both
exist and a token for one passed /validate for the other.  Zone names are now unique
regardless of case, grants are stored with the zone's spelling, and a zone rename or
delete reaches every grant /validate would match to the zone (with the exact spelling
only while zones that differ only in case remain from earlier versions).

Every secret the app reads is generated per run with ``secrets``; a placeholder is
refused.  The admin session ID and CSRF value come from app.sessions per test.  Redis is
an in-memory fake.  Each test opens this run's own database: a temporary SQLite file, or
the PostgreSQL database in ``TEST_DATABASE_URL`` when that is set (a throwaway database:
the tests drop and recreate the zone name index).  The tests create and delete only
their own zones and tokens (names starting with ``znc-``).

Requires pytest and httpx.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
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
_EXPECT_PG = not _DB_URL.startswith("sqlite")


# Generated for this run, never taken from the environment the tests run in.  They take
# effect when this module is the first to load the settings; the tests read the values
# they need from the loaded settings and refuse a placeholder.
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

_INDEX = "idx_zones_name_fold"
_LEVELS = ("read", "write", "delete", "readwrite", "all")
_ZONE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,62}$")


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


class Env:
    def __init__(self, app, db, client, api, admin):
        self.app, self.db, self.client, self.api, self.admin = app, db, client, api, admin
        self.prefix = f"znc-{secrets.token_hex(4)}"
        self.extra_zones: list[str] = []

    def area(self, suffix: str) -> str:
        return f"{self.prefix}-{suffix}"

    async def zone(self, suffix: str, path: str = "admin"):
        if path == "admin":
            return await self.client.post("/admin/api/zones", headers=self.admin, json={"name": self.area(suffix)})
        return await self.client.post("/tokens/zones", headers=self.api, json={"name": self.area(suffix)})

    async def zone_ok(self, suffix: str) -> dict:
        resp = await self.zone(suffix)
        assert resp.status_code == 201, resp.text
        return resp.json()

    async def raw_zone(self, name: str) -> str:
        """Insert a zone directly, bypassing the checks (a zone from an earlier version)."""
        if self.db.is_pg:
            return str(await self.db.fetchval("INSERT INTO zones (name) VALUES ($1) RETURNING id", name))
        zid = str(uuid.uuid4())
        await self.db.execute("INSERT INTO zones (id, name) VALUES ($1, $2)", zid, name)
        return zid

    async def raw_token(self, suffix: str, grants) -> tuple[str, str]:
        """Insert a token with stored grants as given (a list, or raw JSON text)."""
        from app.crypto_tokens import sha256_hex

        raw = secrets.token_hex(24)
        text = grants if isinstance(grants, str) else json.dumps(grants)
        if self.db.is_pg:
            tid = await self.db.fetchval(
                "INSERT INTO tokens (name, token_hash, grants) VALUES ($1, $2, $3::jsonb) RETURNING id",
                self.area(suffix), sha256_hex(raw), text,
            )
        else:
            tid = str(uuid.uuid4())
            await self.db.execute(
                "INSERT INTO tokens (id, name, token_hash, grants) VALUES ($1, $2, $3, $4)",
                tid, self.area(suffix), sha256_hex(raw), text,
            )
        return str(tid), raw

    async def api_token(self, suffix: str, grants: list[dict]):
        body = {"name": self.area(suffix), "grants": grants, "token": secrets.token_hex(24)}
        resp = await self.client.post("/tokens", headers=self.api, json=body)
        return resp, body["token"]

    async def stored_text(self, token_id: str):
        return await self.db.fetchval("SELECT grants FROM tokens WHERE id = $1", token_id)

    async def grants_of(self, token_id: str):
        g = await self.stored_text(token_id)
        return json.loads(g) if isinstance(g, (str, bytes)) else g

    async def zone_rows(self) -> dict[str, str]:
        rows = await self.db.fetch("SELECT id, name FROM zones")
        return {r["name"]: str(r["id"]) for r in rows}

    async def rename(self, zone_id: str, name: str):
        return await self.client.patch(f"/admin/api/zones/{zone_id}", headers=self.admin, json={"name": name})

    async def delete(self, zone_id: str):
        return await self.client.delete(f"/admin/api/zones/{zone_id}", headers=self.admin)

    async def validate(self, raw: str, area: str, level: str = "read") -> bool:
        resp = await self.client.post("/validate", json={"token": raw, "area": area, "level": level})
        assert resp.status_code == 200, resp.text
        return resp.json()["result"]

    async def drop_index(self) -> None:
        await self.db.execute(f"DROP INDEX IF EXISTS {_INDEX}")

    async def has_index(self) -> bool:
        if self.db.is_pg:
            sql = "SELECT 1 FROM pg_indexes WHERE indexname = $1"
        else:
            sql = "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = $1"
        return await self.db.fetchval(sql, _INDEX) is not None


def _refuse_public_database_password() -> None:
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
    from app.sessions import COOKIE_NAME, new_csrf, new_sid, session_record

    s = get_settings()
    for field in ("admin_api_key", "session_secret", "app_encryption_key"):
        value = getattr(s, field)
        if not value.strip() or value.strip() in KNOWN_PUBLIC_VALUES:
            raise RuntimeError(f"{field} is empty or a placeholder")
    _refuse_public_database_password()

    saved_state = {n: getattr(app.state, n, None) for n in ("db", "redis")}
    rate_limit._buckets.clear()
    db = await create_database(_DB_URL)
    client = None
    e = None
    own_admin = None
    try:
        assert db.is_pg == _EXPECT_PG, "test ran on the wrong database backend"
        await ensure_database_schema(db)
        # A full session counts only for an existing admin with MFA enabled, and at most one
        # such admin may exist: use the one this database has, or add one for this test.
        admin_id = await db.fetchval("SELECT id FROM admin_users WHERE totp_enabled = $1", True)
        if admin_id is None:
            own_admin = admin_id = str(uuid.uuid4())
            await db.execute(
                "INSERT INTO admin_users (id, username, password_hash, totp_enabled) VALUES ($1, $2, $3, $4)",
                admin_id, "znc-admin-" + secrets.token_hex(4), "unused", True,
            )
        redis = FakeRedis()
        sid, csrf = new_sid(), new_csrf()
        rkey, rvalue = session_record(sid, {"kind": "full", "adminId": str(admin_id), "csrf": csrf})
        redis.data[rkey] = rvalue
        app.state.db, app.state.redis = db, redis
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        )
        client.cookies.set(COOKIE_NAME, sid)
        e = Env(app, db, client, {"Authorization": f"Bearer {s.admin_api_key}"}, {"X-CSRF-Token": csrf})
        yield e
    finally:
        app.state.db = db
        if e is not None:
            with contextlib.suppress(Exception):
                await db.execute("DELETE FROM tokens WHERE name LIKE $1", e.prefix + "-%")
            with contextlib.suppress(Exception):
                await db.execute("DELETE FROM zones WHERE lower(name) LIKE $1", e.prefix + "-%")
            for name in e.extra_zones:
                with contextlib.suppress(Exception):
                    await db.execute("DELETE FROM zones WHERE name = $1", name)
            with contextlib.suppress(Exception):
                await ensure_database_schema(db)  # the index again, for the next test
        if own_admin is not None:
            with contextlib.suppress(Exception):
                await db.execute("DELETE FROM admin_users WHERE id = $1", own_admin)
        if client is not None:
            await client.aclose()
        await db.close()
        for n, v in saved_state.items():
            setattr(app.state, n, v)
        rate_limit._buckets.clear()


async def _blocked_or_done(e: Env, task: asyncio.Task, query: str) -> None:
    """Return when ``task`` finished or (PostgreSQL) its statement ``query`` waits for a lock.

    SQLite shows no lock wait; there it waits 1 s, well inside the 5 s busy timeout.
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


class _PauseFirstRewrite:
    """Holds the first zone change inside its transaction, right after its grant rewrite."""

    def __init__(self, monkeypatch):
        from app import zones

        self.reached = asyncio.Event()
        self.release = asyncio.Event()
        self.armed = True
        original = zones.rewrite_zone_grants

        async def paused(*args, **kwargs):
            await original(*args, **kwargs)
            if self.armed:
                self.armed = False
                self.reached.set()
                await self.release.wait()

        monkeypatch.setattr(zones, "rewrite_zone_grants", paused)


async def _settle(*tasks):
    for t in tasks:
        if t is not None and not t.done():
            t.cancel()
        if t is not None:
            with contextlib.suppress(BaseException):
                await t


def _run(coro_fn):
    asyncio.run(coro_fn())


# ---------------------------------------------------------------------------
# 1. Zone names are unique regardless of case: 409 on create and rename
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ("admin", "api"))
def test_create_refuses_a_name_that_differs_only_in_case(path):
    async def run():
        async with _env() as e:
            await e.zone_ok("billing")
            for suffix in ("billing", "BILLING", "Billing"):
                resp = await e.zone(suffix, path)
                assert resp.status_code == 409, (suffix, resp.status_code, resp.text)
                assert resp.json()["detail"] == (
                    "A zone with this name already exists. Zone names are not case-sensitive."
                )
            assert [n for n in await e.zone_rows() if n.lower() == e.area("billing")] == [e.area("billing")]

    _run(run)


def test_rename_refuses_a_name_that_differs_only_in_case():
    async def run():
        async with _env() as e:
            billing = await e.zone_ok("billing")
            orders = await e.zone_ok("orders")
            tid, raw = await e.raw_token("t", [{"area": e.area("orders"), "level": "read"}])
            for target in (e.area("billing"), e.area("BILLING")):
                resp = await e.rename(orders["id"], target)
                assert resp.status_code == 409, resp.text
            assert set(await e.zone_rows()) >= {e.area("billing"), e.area("orders")}
            assert await e.grants_of(tid) == [{"area": e.area("orders"), "level": "read"}]
            # A zone may change the case of its own name; /validate already folds, so its
            # grants stay as they are and keep working.
            resp = await e.rename(billing["id"], e.area("BILLING"))
            assert resp.status_code == 200, resp.text
            resp = await e.rename(orders["id"], e.area("Orders"))
            assert resp.status_code == 200, resp.text
            assert await e.grants_of(tid) == [{"area": e.area("orders"), "level": "read"}]
            assert await e.validate(raw, e.area("Orders")) is True

    _run(run)


def test_pg_locale_does_not_change_the_comparison():
    if not _EXPECT_PG:
        pytest.skip("PostgreSQL only")

    async def run():
        async with _env() as e:
            if await e.db.fetchval("SELECT 1 FROM pg_collation WHERE collname = 'tr-x-icu'") is None:
                pytest.skip("no ICU collations in this PostgreSQL")
            await e.db.execute('ALTER TABLE zones ALTER COLUMN name TYPE TEXT COLLATE "tr-x-icu"')
            try:
                # Under a Turkish collation lower('I') is a dotless i, so a locale-dependent
                # comparison would let BILLING in next to billing.
                assert await e.db.fetchval("SELECT lower('BILLING' COLLATE \"tr-x-icu\") <> 'billing'")
                await e.zone_ok("billing")
                assert (await e.zone("BILLING")).status_code == 409
                orders = await e.zone_ok("orders")
                assert (await e.rename(orders["id"], e.area("BILLING"))).status_code == 409
            finally:
                await e.db.execute('ALTER TABLE zones ALTER COLUMN name TYPE TEXT COLLATE "default"')

    _run(run)


def test_concurrent_creates_of_case_variants_without_the_index():
    async def run():
        async with _env() as e:
            await e.drop_index()
            results = await asyncio.gather(*(e.zone(s) for s in ("x", "X", "x", "X")))
            codes = sorted(r.status_code for r in results)
            assert codes == [201, 409, 409, 409], [(r.status_code, r.text) for r in results]

    _run(run)


# ---------------------------------------------------------------------------
# 2. Grants written through the API take the zone's spelling
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("route", ("api_create", "admin_create", "api_patch", "admin_patch"))
def test_grant_write_stores_the_zone_spelling(route):
    async def run():
        async with _env() as e:
            await e.zone_ok("billing")
            await e.zone_ok("orders")
            want = [{"area": e.area("billing"), "level": "read"}]
            sent = [{"area": e.area("BILLING"), "level": "read"}]
            name = e.area("t")
            if route == "api_create":
                resp = await e.client.post("/tokens", headers=e.api, json={"name": name, "grants": sent})
            elif route == "admin_create":
                resp = await e.client.post("/admin/api/tokens", headers=e.admin, json={"name": name, "grants": sent})
            else:
                tid, _ = await e.raw_token("t", [{"area": e.area("orders"), "level": "read"}])
                base = "/tokens" if route == "api_patch" else "/admin/api/tokens"
                headers = e.api if route == "api_patch" else e.admin
                resp = await e.client.patch(f"{base}/{tid}", headers=headers, json={"grants": sent})
            assert resp.status_code in (200, 201), resp.text
            tid = await e.db.fetchval("SELECT id FROM tokens WHERE name = $1", name)
            assert await e.grants_of(str(tid)) == want

    _run(run)


def test_grant_write_refusals():
    async def run():
        async with _env() as e:
            await e.zone_ok("billing")
            resp, _ = await e.api_token("twice", [
                {"area": e.area("billing"), "level": "read"}, {"area": e.area("BILLING"), "level": "write"},
            ])
            assert resp.status_code == 400 and "listed twice" in resp.text, resp.text
            # Zones that differ only in case, from an earlier version.
            await e.drop_index()
            await e.raw_zone(e.area("BILLING"))
            resp, _ = await e.api_token("exact", [{"area": e.area("BILLING"), "level": "read"}])
            assert resp.status_code == 201, resp.text
            tid = await e.db.fetchval("SELECT id FROM tokens WHERE name = $1", e.area("exact"))
            assert await e.grants_of(str(tid)) == [{"area": e.area("BILLING"), "level": "read"}]
            resp, _ = await e.api_token("ambiguous", [{"area": e.area("Billing"), "level": "read"}])
            assert resp.status_code == 400 and "more than one zone" in resp.text, resp.text

    _run(run)


# ---------------------------------------------------------------------------
# 3. Rename and delete reach every grant /validate matches to the zone
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("op", ("delete", "rename"))
def test_grants_in_another_spelling_follow_the_zone(op):
    """Spellings /validate folds to the zone: case, Unicode whitespace, the Kelvin sign."""
    async def run():
        async with _env() as e:
            zone = await e.zone_ok("key")
            spellings = [
                e.area("KEY"),
                f" {e.area('key')} ",
                f" {e.area('key')}　",
                f"{e.prefix}-Key",  # KELVIN SIGN: lower() gives "k"
            ]
            tokens = [await e.raw_token(f"t{i}", [{"area": s, "level": "read"}]) for i, s in enumerate(spellings)]
            for _, raw in tokens:
                assert await e.validate(raw, e.area("key")) is True
            if op == "delete":
                assert (await e.delete(zone["id"])).status_code == 200
                for tid, raw in tokens:
                    assert await e.grants_of(tid) == []
                    assert await e.validate(raw, e.area("key")) is False
            else:
                assert (await e.rename(zone["id"], e.area("lock"))).status_code == 200
                for tid, raw in tokens:
                    assert await e.grants_of(tid) == [{"area": e.area("lock"), "level": "read"}]
                    assert await e.validate(raw, e.area("lock")) is True
                    assert await e.validate(raw, e.area("key")) is False

    _run(run)


def test_legacy_case_variants_use_the_exact_spelling_until_one_is_left():
    async def run():
        async with _env() as e:
            await e.drop_index()
            lower_id = await e.raw_zone(e.area("billing"))
            upper_id = await e.raw_zone(e.area("BILLING"))
            t_lower, _ = await e.raw_token("lower", [{"area": e.area("billing"), "level": "read"}])
            t_upper, _ = await e.raw_token("upper", [{"area": e.area("BILLING"), "level": "read"}])
            t_mixed, raw_mixed = await e.raw_token("mixed", [{"area": e.area("Billing"), "level": "read"}])

            assert (await e.delete(upper_id)).status_code == 200
            assert await e.grants_of(t_upper) == []
            # billing still exists: its own grants, and the ambiguous one, stay.
            assert await e.grants_of(t_lower) == [{"area": e.area("billing"), "level": "read"}]
            assert await e.grants_of(t_mixed) == [{"area": e.area("Billing"), "level": "read"}]

            assert (await e.delete(lower_id)).status_code == 200
            assert await e.grants_of(t_lower) == []
            assert await e.grants_of(t_mixed) == []
            assert await e.validate(raw_mixed, e.area("billing")) is False

    _run(run)


def test_legacy_case_variant_resolved_by_rename():
    async def run():
        async with _env() as e:
            await e.drop_index()
            lower_id = await e.raw_zone(e.area("billing"))
            upper_id = await e.raw_zone(e.area("BILLING"))
            t_mixed, raw = await e.raw_token("mixed", [{"area": e.area("Billing"), "level": "read"}])
            assert (await e.rename(upper_id, e.area("other"))).status_code == 200
            assert await e.grants_of(t_mixed) == [{"area": e.area("Billing"), "level": "read"}]
            assert (await e.rename(lower_id, e.area("payments"))).status_code == 200
            assert await e.grants_of(t_mixed) == [{"area": e.area("payments"), "level": "read"}]
            assert await e.validate(raw, e.area("payments")) is True

    _run(run)


@pytest.mark.parametrize("first", ("delete", "rename"))
def test_concurrent_changes_of_a_legacy_pair_leave_no_grant_behind(first, monkeypatch):
    """Without one-at-a-time zone changes each change sees the other zone, matches only its
    exact spelling, and the ambiguous grant outlives both zones."""
    async def run():
        async with _env() as e:
            await e.drop_index()
            lower_id = await e.raw_zone(e.area("billing"))
            upper_id = await e.raw_zone(e.area("BILLING"))
            t_mixed, raw = await e.raw_token("mixed", [{"area": e.area("Billing"), "level": "read"}])
            pause = _PauseFirstRewrite(monkeypatch)
            one = asyncio.create_task(
                e.delete(lower_id) if first == "delete" else e.rename(lower_id, e.area("payments"))
            )
            two = None
            try:
                await asyncio.wait_for(pause.reached.wait(), 10)
                two = asyncio.create_task(e.delete(upper_id))
                await _blocked_or_done(e, two, "LOCK TABLE zones IN SHARE ROW EXCLUSIVE MODE%")
                pause.release.set()
                r1 = await asyncio.wait_for(one, 30)
                r2 = await asyncio.wait_for(two, 30)
            finally:
                pause.release.set()
                await _settle(one, two)
            assert r1.status_code == 200, r1.text
            assert r2.status_code == 200, r2.text
            left = await e.grants_of(t_mixed)
            names = await e.zone_rows()
            assert all(g["area"] in names for g in left), f"a grant outlived its zone: {left}"
            if first == "delete":
                assert await e.validate(raw, e.area("billing")) is False

    _run(run)


def test_rename_refused_when_a_token_would_hold_the_zone_twice():
    async def run():
        async with _env() as e:
            billing = await e.zone_ok("billing")
            # From an ALLOWED_AREAS list with both spellings: one grant each.
            t_two, raw = await e.raw_token("two", [
                {"area": e.area("billing"), "level": "read"}, {"area": e.area("BILLING"), "level": "write"},
            ])
            # Holds the target name twice but no grant of the renamed zone: not changed.
            t_other, _ = await e.raw_token("other", [
                {"area": e.area("orders"), "level": "read"}, {"area": e.area("ORDERS"), "level": "write"},
            ])
            before = {lv: await e.validate(raw, e.area("billing"), lv) for lv in _LEVELS}
            resp = await e.rename(billing["id"], e.area("orders"))
            assert resp.status_code == 409, resp.text
            assert e.area("two") in resp.json()["detail"] and e.area("other") not in resp.json()["detail"]
            assert e.area("billing") in await e.zone_rows()
            assert await e.grants_of(t_two) == [
                {"area": e.area("billing"), "level": "read"}, {"area": e.area("BILLING"), "level": "write"},
            ]
            assert {lv: await e.validate(raw, e.area("billing"), lv) for lv in _LEVELS} == before

            # The admin edits the token, then the rename goes through.
            resp = await e.client.patch(f"/tokens/{t_two}", headers=e.api, json={
                "grants": [{"area": e.area("billing"), "level": "read,write"}],
            })
            assert resp.status_code == 200, resp.text
            resp = await e.rename(billing["id"], e.area("orders"))
            assert resp.status_code == 200, resp.text
            assert await e.grants_of(t_two) == [{"area": e.area("orders"), "level": "read,write"}]
            assert await e.grants_of(t_other) == [
                {"area": e.area("orders"), "level": "read"}, {"area": e.area("ORDERS"), "level": "write"},
            ]

    _run(run)


@pytest.mark.parametrize("op", ("delete", "rename"))
def test_elements_that_are_not_grants_are_kept(op):
    async def run():
        async with _env() as e:
            zone = await e.zone_ok("billing")
            others = [
                {"level": "read"},
                {"area": 5, "level": "read"},
                {"area": None, "level": "read"},
                {"area": [e.area("billing")], "level": "read"},
                "billing",
                7,
            ]
            mixed, _ = await e.raw_token("mixed", [{"area": e.area("billing"), "level": "read"}, *others])
            untouched = []
            if not _EXPECT_PG:
                untouched.append(await e.raw_token("broken", "[{\"area\": \"x\""))
            untouched.append(await e.raw_token("object", {"area": e.area("billing"), "level": "read"}))
            texts = {tid: await e.stored_text(tid) for tid, _ in untouched}
            if op == "delete":
                assert (await e.delete(zone["id"])).status_code == 200
                assert await e.grants_of(mixed) == others
            else:
                assert (await e.rename(zone["id"], e.area("payments"))).status_code == 200
                assert await e.grants_of(mixed) == [{"area": e.area("payments"), "level": "read"}, *others]
            for tid, text in texts.items():
                assert await e.stored_text(tid) == text

    _run(run)


# ---------------------------------------------------------------------------
# 4. A token write and a rename at the same time
# ---------------------------------------------------------------------------

class _PauseGrantWrite:
    """Holds the next token grant write inside its transaction, after its write."""

    def __init__(self, monkeypatch):
        from app.routers import tokens as tokens_router

        self.reached = asyncio.Event()
        self.release = asyncio.Event()
        original = tokens_router.grant_write_transaction

        @contextlib.asynccontextmanager
        async def paused(db):
            async with original(db) as tx:
                yield tx
                self.reached.set()
                await self.release.wait()

        monkeypatch.setattr(tokens_router, "grant_write_transaction", paused)


def test_token_write_then_rename(monkeypatch):
    async def run():
        async with _env() as e:
            zone = await e.zone_ok("billing")
            pause = _PauseGrantWrite(monkeypatch)
            write = asyncio.create_task(e.api_token("t", [{"area": e.area("BILLING"), "level": "read"}]))
            change = None
            try:
                await asyncio.wait_for(pause.reached.wait(), 10)
                change = asyncio.create_task(e.rename(zone["id"], e.area("payments")))
                await _blocked_or_done(e, change, "LOCK TABLE zones IN SHARE ROW EXCLUSIVE MODE%")
                if _EXPECT_PG:
                    assert not change.done(), "the rename did not wait for the open grant write"
                pause.release.set()
                resp, raw = await asyncio.wait_for(write, 30)
                zresp = await asyncio.wait_for(change, 30)
            finally:
                pause.release.set()
                await _settle(write, change)
            assert resp.status_code == 201, resp.text
            assert zresp.status_code == 200, zresp.text
            tid = str(await e.db.fetchval("SELECT id FROM tokens WHERE name = $1", e.area("t")))
            assert await e.grants_of(tid) == [{"area": e.area("payments"), "level": "read"}]
            payments = (await e.zone_rows())[e.area("payments")]
            assert (await e.delete(payments)).status_code == 200
            assert await e.grants_of(tid) == []
            assert await e.validate(raw, e.area("payments")) is False

    _run(run)


def test_rename_then_token_write(monkeypatch):
    async def run():
        async with _env() as e:
            zone = await e.zone_ok("billing")
            pause = _PauseFirstRewrite(monkeypatch)
            change = asyncio.create_task(e.rename(zone["id"], e.area("payments")))
            write = None
            try:
                await asyncio.wait_for(pause.reached.wait(), 10)
                # The early check still sees the old name; the check inside the write's
                # transaction runs after the rename committed.
                write = asyncio.create_task(e.api_token("t", [{"area": e.area("BILLING"), "level": "read"}]))
                await _blocked_or_done(e, write, "LOCK TABLE zones IN SHARE MODE%")
                pause.release.set()
                zresp = await asyncio.wait_for(change, 30)
                resp, _ = await asyncio.wait_for(write, 30)
            finally:
                pause.release.set()
                await _settle(change, write)
            assert zresp.status_code == 200, zresp.text
            assert resp.status_code == 400, resp.text
            assert await e.db.fetchval("SELECT id FROM tokens WHERE name = $1", e.area("t")) is None

    _run(run)


# ---------------------------------------------------------------------------
# 5. Startup with zones that differ only in case
# ---------------------------------------------------------------------------

def test_startup_with_legacy_case_variants(caplog):
    async def run():
        from app.schema import ensure_database_schema

        async with _env() as e:
            assert await e.has_index()
            await e.drop_index()
            lower_id = await e.raw_zone(e.area("billing"))
            await e.raw_zone(e.area("BILLING"))
            tid, raw = await e.raw_token("t", [{"area": e.area("billing"), "level": "read"}])
            before = {(a, lv): await e.validate(raw, a, lv) for a in (e.area("billing"), e.area("BILLING")) for lv in _LEVELS}
            stored = await e.stored_text(tid)
            with caplog.at_level(logging.WARNING, logger="app.zones"):
                await ensure_database_schema(e.db)
            assert not await e.has_index()
            assert any(e.area("billing") in r.getMessage() and e.area("BILLING") in r.getMessage() for r in caplog.records)
            assert await e.stored_text(tid) == stored
            after = {(a, lv): await e.validate(raw, a, lv) for a in (e.area("billing"), e.area("BILLING")) for lv in _LEVELS}
            assert after == before
            # Creating a third spelling is refused also without the index.
            assert (await e.zone("Billing")).status_code == 409
            assert (await e.delete(lower_id)).status_code == 200
            caplog.clear()
            with caplog.at_level(logging.WARNING, logger="app.zones"):
                await ensure_database_schema(e.db)
            assert await e.has_index()
            assert not caplog.records

    _run(run)


def test_startup_survives_a_duplicate_added_during_the_scan(caplog, monkeypatch):
    async def run():
        from app import zones
        from app.schema import ensure_database_schema

        async with _env() as e:
            await e.drop_index()
            await e.raw_zone(e.area("billing"))
            await e.raw_zone(e.area("BILLING"))
            real = zones.duplicate_zone_groups
            calls = {"n": 0}

            async def first_scan_misses(db):
                calls["n"] += 1
                return [] if calls["n"] == 1 else await real(db)

            monkeypatch.setattr(zones, "duplicate_zone_groups", first_scan_misses)
            with caplog.at_level(logging.WARNING, logger="app.zones"):
                await ensure_database_schema(e.db)
            assert not await e.has_index()
            assert any(e.area("BILLING") in r.getMessage() for r in caplog.records)

    _run(run)


# ---------------------------------------------------------------------------
# 6. The helpers
# ---------------------------------------------------------------------------

def test_area_matches_zone():
    from app.grants import area_matches_zone, fold_area

    assert fold_area(" Key　") == "key"
    assert area_matches_zone("billing", "billing", [])
    assert area_matches_zone(" billing ", "billing", ["BILLING"])
    assert area_matches_zone("BILLING", "billing", ["orders"])
    assert not area_matches_zone("Billing", "billing", ["BILLING"])
    assert not area_matches_zone("orders", "billing", [])


def test_canonical_grants():
    from app.grants import canonical_grants

    allowed = ["billing", "orders", "PAY", "pay"]
    g = lambda a, lv="read": {"area": a, "level": lv}  # noqa: E731
    assert canonical_grants([g("BILLING"), g("Orders", "write")], allowed) == [g("billing"), g("orders", "write")]
    assert canonical_grants([g("pay"), g("PAY", "write")], allowed) == [g("pay"), g("PAY", "write")]
    assert canonical_grants([g("x")], []) == [g("x")]
    for grants, message in (
        ([g("nope")], "Area not allowed: nope"),
        ([g("Pay")], "Area matches more than one zone (PAY, pay). Use the exact name."),
        ([g("billing"), g("BILLING")], "Area listed twice: billing"),
    ):
        with pytest.raises(ValueError) as err:
            canonical_grants(grants, allowed)
        assert str(err.value) == message

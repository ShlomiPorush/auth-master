"""Rate limit settings on the admin Settings page.

- Validation: limits in range, whole seconds, single IP addresses only, IPv4-mapped IPv6
  stored as IPv4, no zones or networks, at most 100 entries.
- Storage: one row per version, a second save of the same version loses, a failed or
  conflicting save leaves no transaction open, and nothing on the shared request
  connection (a rolled-back transaction, a failing write) can undo a saved version.
- Runtime: apply() never goes back, reloads pick up rows written elsewhere, invalid
  stored settings stop the start and are kept out by reloads, exempt addresses are
  never limited or counted.
- HTTP: full admin session and CSRF required, environment-set fields locked, version
  conflicts answered 409, saving your own address needs a confirmation, every save
  is recorded.

Every secret is generated per run with ``secrets``; Redis is faked.  The app opens
only this module's own database file.  PostgreSQL cases run when TEST_DATABASE_URL is
set.  Requires pytest and httpx.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

_TMP_DIR = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
_DATABASE_URL = f"sqlite:///{_TMP_DIR.name}/settings.db"

# Used only when this module is the first to load the settings.
os.environ.setdefault("ADMIN_API_KEY", secrets.token_urlsafe(32))
os.environ.setdefault("SESSION_SECRET", secrets.token_urlsafe(48))
os.environ.setdefault("APP_ENCRYPTION_KEY", secrets.token_hex(16))
os.environ.setdefault("REDIS_URL", f"redis://:{secrets.token_hex(32)}@127.0.0.1:6379/0")
os.environ.setdefault("DATABASE_URL", _DATABASE_URL)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

PG_URL = os.environ.get("TEST_DATABASE_URL")
BACKENDS = ["sqlite", pytest.param("postgres", marks=pytest.mark.skipif(not PG_URL, reason="TEST_DATABASE_URL is not set"))]


def _mods():
    """Imported on first use: whichever module imports the app first fixes its settings."""
    import app.main as main_mod
    from app import db as db_mod, rate_limit, rate_limit_settings as rls

    return main_mod, db_mod, rate_limit, rls


@pytest.fixture(autouse=True)
def _restore_limits():
    _, _, rate_limit, rls = _mods()
    rate_limit.reset_state()
    rls.status.last_ok_ms = None
    rls.status.last_error = None
    yield
    rate_limit.reset_state()


# ── Validation ──────────────────────────────────────────────────

def test_exempt_addresses_are_normalized():
    _, _, _, rls = _mods()
    got = rls.check_exempt([" 10.0.0.2 ", "::ffff:10.0.0.1", "10.0.0.1", "", "2001:DB8::1", "2001:db8:0::1"])
    assert got == ("10.0.0.1", "10.0.0.2", "2001:db8::1")


@pytest.mark.parametrize("entry", [
    "10.0.0.5/24", "fe80::1%eth0", "fe80::/64%eth0", "localhost", "10.0.0", "10.0.0.0/33", "1" * 50, "10.0.0.1, 10.0.0.2",
])
def test_exempt_refuses_anything_but_an_address_or_network(entry):
    _, _, _, rls = _mods()
    with pytest.raises(rls.InvalidSettings) as e:
        rls.check_exempt([entry])
    assert e.value.field == "exempt"


def test_exempt_networks_are_normalized():
    _, _, _, rls = _mods()
    got = rls.check_exempt([
        "172.18.0.0/16", "10.0.0.7/32", "::ffff:10.0.0.9/128", "2001:DB8::/32", "0.0.0.0/0", "172.18.0.0/16",
    ])
    assert got == ("0.0.0.0/0", "10.0.0.7", "10.0.0.9", "172.18.0.0/16", "2001:db8::/32")


def test_network_with_host_bits_says_how_to_write_it():
    _, _, _, rls = _mods()
    with pytest.raises(rls.InvalidSettings, match="Write it as 172.18.0.0/16"):
        rls.check_exempt(["172.18.0.5/16"])


def test_exempt_count_is_capped():
    _, _, _, rls = _mods()
    rls.check_exempt([f"10.0.0.{i}" for i in range(1, 101)])
    with pytest.raises(rls.InvalidSettings):
        rls.check_exempt([f"10.0.1.{i}" for i in range(1, 102)])


@pytest.mark.parametrize("value", [0, -1, 1_000_001, True, 1.5, "10", None])
def test_max_req_bounds(value):
    _, _, _, rls = _mods()
    with pytest.raises(rls.InvalidSettings):
        rls.check_max_req(value)


@pytest.mark.parametrize("value", [0, 999, 1500, 60_001, 3_601_000, True])
def test_window_must_be_whole_seconds_in_range(value):
    _, _, _, rls = _mods()
    with pytest.raises(rls.InvalidSettings):
        rls.check_window_ms(value)


@pytest.mark.parametrize("text", [
    "not json", "[]", '{"format": 2, "maxReq": 5, "windowMs": 60000, "exempt": []}',
    '{"format": 1, "maxReq": 5, "windowMs": 60000}',
    '{"format": 1, "maxReq": 5, "windowMs": 60000, "exempt": ["10.0.0.2", "10.0.0.1"]}',
    '{"format": 1, "maxReq": 0, "windowMs": 60000, "exempt": []}',
    '{"format": true, "maxReq": 5, "windowMs": 60000, "exempt": []}',
    '{"format": 1.0, "maxReq": 5, "windowMs": 60000, "exempt": []}',
])
def test_stored_value_is_checked(text):
    _, _, _, rls = _mods()
    with pytest.raises(rls.InvalidSettings):
        rls.parse_value(text)


def test_environment_locks_the_fields_it_sets(monkeypatch):
    _, _, _, rls = _mods()
    from app.config import Settings

    monkeypatch.setenv("RATE_LIMIT_MAX_REQ", "500")
    monkeypatch.delenv("RATE_LIMIT_WINDOW_MS", raising=False)
    s = Settings(_env_file=None)
    monkeypatch.setattr(rls, "get_settings", lambda: s)
    assert rls.locked_fields() == {"maxReq": True, "windowMs": False}
    eff = rls.effective(rls.Stored(3, 7, 5000, ("10.0.0.1",)))
    assert (eff.version, eff.max_req, eff.window_ms, eff.exempt) == (3, 500, 5000, frozenset({"10.0.0.1"}))


# ── Runtime limits ──────────────────────────────────────────────

def test_apply_only_moves_forward():
    _, _, rate_limit, _ = _mods()
    L = rate_limit.Limits
    assert rate_limit.apply(L(2, 10, 60_000))
    assert not rate_limit.apply(L(1, 99, 60_000))
    assert not rate_limit.apply(L(2, 99, 60_000))
    assert rate_limit.current().max_req == 10


def test_address_inside_an_exempt_network_is_never_limited():
    _, _, rate_limit, _ = _mods()
    rate_limit.reset_state(rate_limit.Limits(1, 1, 60_000, frozenset({"172.18.0.0/16", "2001:db8::/32"})))
    for key in ("172.18.3.4", "::ffff:172.18.0.50", "2001:db8::7"):
        assert all(rate_limit.allow_validate(key) for _ in range(3)), key
    assert rate_limit.allow_validate("172.19.0.1")
    assert not rate_limit.allow_validate("172.19.0.1")
    assert rate_limit.rejection_stats()["rejectedTotal"] == 1


def test_exempt_address_is_never_limited_or_counted():
    _, _, rate_limit, _ = _mods()
    rate_limit.reset_state(rate_limit.Limits(1, 1, 60_000, frozenset({"10.0.0.1"})))
    assert all(rate_limit.allow_validate("10.0.0.1") for _ in range(5))
    assert all(rate_limit.allow_validate("::ffff:10.0.0.1") for _ in range(5))
    assert "10.0.0.1" not in rate_limit._buckets
    assert rate_limit.allow_validate("10.0.0.2")
    assert not rate_limit.allow_validate("10.0.0.2")
    for key in ("unknown", "testclient", ""):
        assert rate_limit.allow_validate(key)
        assert not rate_limit.allow_validate(key)
    assert rate_limit.rejection_stats()["rejectedTotal"] == 4


# ── Storage ─────────────────────────────────────────────────────

def _run(coro):
    return asyncio.run(coro)


async def _open(url):
    _, db_mod, _, _ = _mods()
    from app.schema import ensure_database_schema

    main = await db_mod.create_database(url)
    await ensure_database_schema(main)
    if main.is_pg:
        await main.execute("DELETE FROM rate_limit_settings")
    settings_db, own = await db_mod.open_settings_database(main)
    return main, settings_db, own


async def _close(main, settings_db, own):
    if own:
        await settings_db.close()
    await main.close()


def _url(backend, tmp_path):
    return f"sqlite:///{tmp_path}/store.db" if backend == "sqlite" else PG_URL


def _value(rls, max_req=5, exempt=()):
    return rls.dump_value(max_req, None, tuple(exempt))


@pytest.mark.parametrize("backend", BACKENDS)
def test_one_winner_per_version(backend, tmp_path):
    _, _, _, rls = _mods()

    async def scenario():
        main, sdb, own = await _open(_url(backend, tmp_path))
        try:
            assert await rls.read_current(sdb) == rls.EMPTY
            results = await asyncio.gather(*(rls.insert_version(sdb, 1, _value(rls, n), f"admin{n}", None) for n in (5, 6)))
            assert sorted(results) == [False, True]
            cur = await rls.read_current(sdb)
            assert cur.version == 1 and cur.max_req == (5 if results[0] else 6)
            # A conflict leaves nothing open: other connections can write at once.
            assert not await rls.insert_version(sdb, 1, _value(rls, 9), "late", None)
            await main.execute("INSERT INTO zones (name) VALUES ($1)", f"z{secrets.token_hex(4)}")
            assert await rls.insert_version(sdb, 2, _value(rls, 9), "next", "10.0.0.9")
            cur = await rls.read_current(sdb)
            assert (cur.version, cur.max_req, cur.actor, cur.ip_address) == (2, 9, "next", "10.0.0.9")
        finally:
            await _close(main, sdb, own)

    _run(scenario())


def test_shared_connection_cannot_undo_a_saved_version(tmp_path):
    _, _, _, rls = _mods()

    async def scenario():
        main, sdb, own = await _open(_url("sqlite", tmp_path))
        try:
            assert await rls.insert_version(sdb, 1, _value(rls, 5), "admin", None)
            # A transaction on the shared connection that rolls back.
            with pytest.raises(RuntimeError):
                async with main.transaction() as conn:
                    await conn.execute("INSERT INTO zones (name) VALUES ($1)", "rolled-back")
                    raise RuntimeError("simulated failure inside a zones transaction")
            # A failing write on the shared connection.
            await main.execute("INSERT INTO zones (name) VALUES ($1)", "dup")
            with pytest.raises(Exception):
                await main.execute("INSERT INTO zones (name) VALUES ($1)", "dup")
            assert (await rls.read_current(sdb)).version == 1
            assert await main.fetchval("SELECT COUNT(*) FROM rate_limit_settings") == 1
            assert await main.fetchval("SELECT COUNT(*) FROM zones WHERE name = 'rolled-back'") == 0
        finally:
            await _close(main, sdb, own)

    _run(scenario())


def test_busy_database_fails_the_save_and_leaves_nothing_open(tmp_path, monkeypatch):
    _, db_mod, _, rls = _mods()
    monkeypatch.setattr(db_mod, "SETTINGS_LOCK_TIMEOUT_SEC", 0.2)

    async def scenario():
        main, sdb, own = await _open(_url("sqlite", tmp_path))
        try:
            async with main.transaction() as conn:  # holds the write lock
                await conn.execute("INSERT INTO zones (name) VALUES ($1)", "held")
                with pytest.raises(sqlite3.OperationalError):
                    await rls.insert_version(sdb, 1, _value(rls), "admin", None)
            assert await rls.insert_version(sdb, 1, _value(rls), "admin", None)
        finally:
            await _close(main, sdb, own)

    _run(scenario())


def test_in_memory_sqlite_is_refused():
    _, db_mod, _, _ = _mods()

    async def scenario():
        main = await db_mod.create_database("sqlite:///:memory:")
        try:
            with pytest.raises(RuntimeError, match="in-memory"):
                await db_mod.open_settings_database(main)
        finally:
            await main.close()

    _run(scenario())


def test_reload_applies_newer_rows_and_keeps_last_good_on_bad_data(tmp_path):
    _, _, rate_limit, rls = _mods()

    async def scenario():
        main, sdb, own = await _open(_url("sqlite", tmp_path))
        try:
            await rls.load(sdb)
            assert rate_limit.current().version == 0
            # Written by "another process": the main connection.
            await main.execute(
                "INSERT INTO rate_limit_settings (version, value, actor) VALUES ($1, $2, $3)",
                1, rls.dump_value(None, 2000, ("10.0.0.1",)), "other",
            )
            await rls.reload_once(sdb)
            assert rate_limit.current().version == 1
            assert rate_limit.current().window_ms == 2000
            assert rate_limit.current().exempt == frozenset({"10.0.0.1"})
            assert rls.status.last_error is None

            await main.execute(
                "INSERT INTO rate_limit_settings (version, value, actor) VALUES ($1, $2, $3)", 2, '{"format": 9}', "broken",
            )
            await rls.reload_once(sdb)
            assert rate_limit.current().version == 1
            assert "version 2" in rls.status.last_error and "README" in rls.status.last_error
            with pytest.raises(RuntimeError, match="Repairing stored rate limit settings"):
                await rls.load(sdb)

            # The documented repair: a higher version, nothing deleted.
            await main.execute(
                "INSERT INTO rate_limit_settings (version, value, actor) SELECT COALESCE(MAX(version), 0) + 1, "
                "'{\"exempt\": [], \"format\": 1, \"maxReq\": null, \"windowMs\": null}', 'manual repair' FROM rate_limit_settings"
            )
            await rls.reload_once(sdb)
            assert rate_limit.current().version == 3 and rate_limit.current().exempt == frozenset()
        finally:
            await _close(main, sdb, own)

    _run(scenario())


def test_cancelled_save_is_all_or_nothing_and_reload_applies_it(tmp_path):
    _, _, rate_limit, rls = _mods()

    async def scenario():
        main, sdb, own = await _open(_url("sqlite", tmp_path))
        try:
            await rls.load(sdb)
            for ticks in range(4):
                version = rate_limit.current().version
                stored_before = (await rls.read_current(sdb)).version
                task = asyncio.create_task(rls.insert_version(sdb, stored_before + 1, _value(rls, 10 + ticks), "admin", None))
                for _ in range(ticks):
                    await asyncio.sleep(0)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await asyncio.sleep(0.05)  # an operation already handed to the SQLite thread finishes
                after = await rls.read_current(sdb)
                assert after.version in (stored_before, stored_before + 1)
                assert rate_limit.current().version == version  # not applied by the cancelled request
                await rls.reload_once(sdb)
                assert rate_limit.current().version == after.version
        finally:
            await _close(main, sdb, own)

    _run(scenario())


# ── HTTP ────────────────────────────────────────────────────────

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


class Admin:
    """A TestClient behind uvicorn's ProxyHeadersMiddleware, so each request can come
    from a chosen address through X-Forwarded-For, the way a trusted proxy sends it."""

    def __init__(self, client: TestClient, fake: FakeRedis) -> None:
        self.client = client
        self.fake = fake
        self.csrf = ""

    def login(self, kind: str = "full") -> None:
        from app.sessions import COOKIE_NAME, PREFIX

        sid = secrets.token_hex(32)
        self.csrf = secrets.token_hex(24)
        self.fake.data[PREFIX + sid] = json.dumps({"kind": kind, "adminId": "00000000-0000-0000-0000-000000000001", "csrf": self.csrf})
        self.client.cookies.set(COOKIE_NAME, sid)

    def get(self, frm: str = "10.9.9.9"):
        return self.client.get("/admin/api/settings/rate-limit", headers={"X-Forwarded-For": frm})

    def put(self, body: dict, frm: str = "10.9.9.9", csrf: bool = True):
        headers = {"X-Forwarded-For": frm}
        if csrf:
            headers["X-CSRF-Token"] = self.csrf
        return self.client.put("/admin/api/settings/rate-limit", json=body, headers=headers)


@pytest.fixture()
def admin(monkeypatch, tmp_path):
    main_mod, db_mod, _, rls = _mods()
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
    from app.config import Settings

    # Other test modules set RATE_LIMIT_MAX_REQ; here no limit comes from the environment.
    monkeypatch.delenv("RATE_LIMIT_MAX_REQ", raising=False)
    monkeypatch.delenv("RATE_LIMIT_WINDOW_MS", raising=False)
    unlocked = Settings(_env_file=None)
    monkeypatch.setattr(rls, "get_settings", lambda: unlocked)

    fake = FakeRedis()
    monkeypatch.setattr(main_mod.redis, "from_url", lambda *a, **k: fake)
    url = f"sqlite:///{tmp_path}/http.db"
    create_database = db_mod.create_database
    monkeypatch.setattr(main_mod, "create_database", lambda _url: create_database(url))
    with TestClient(ProxyHeadersMiddleware(main_mod.app, trusted_hosts="*")) as c:
        a = Admin(c, fake)
        a.url = url
        yield a


@pytest.mark.parametrize("kind", [None, "setup", "partial"])
def test_needs_a_full_session(admin, kind):
    if kind:
        admin.login(kind)
    assert admin.get().status_code == 401
    assert admin.put({"baseVersion": 0, "maxReq": 5}).status_code == 401


def test_needs_csrf(admin):
    admin.login()
    assert admin.put({"baseVersion": 0, "maxReq": 5}, csrf=False).status_code == 403


def _db(admin, sql, *args):
    main_mod, _, _, _ = _mods()
    return admin.client.portal.call(main_mod.app.state.db.fetch, sql, *args)


def test_save_applies_records_and_exempts(admin):
    _, _, rate_limit, _ = _mods()
    admin.login()
    view = admin.get().json()
    assert view["version"] == 0
    assert view["fields"]["exempt"]["value"] == []
    assert view["yourAddress"] == "10.9.9.9"

    res = admin.put({"baseVersion": 0, "maxReq": 2, "windowSeconds": 30, "exempt": ["::ffff:10.0.0.7"]})
    assert res.status_code == 200, res.text
    view = res.json()
    assert view["version"] == 1
    assert view["fields"]["exempt"]["value"] == ["10.0.0.7"]
    assert view["applied"]["version"] == 1
    assert rate_limit.current().exempt == frozenset({"10.0.0.7"})
    assert rate_limit.current().window_ms == 30_000

    rows = _db(admin, "SELECT version, actor, ip_address FROM rate_limit_settings")
    assert [(r["version"], r["ip_address"]) for r in rows] == [(1, "10.9.9.9")]
    acts = _db(admin, "SELECT actor, details FROM activity_logs WHERE action = 'settings.update'")
    assert len(acts) == 1 and json.loads(acts[0]["details"])["to"]["exempt"] == ["10.0.0.7"]

    # The exempt address is never limited; another one is.
    body = {"token": "x" * 40, "area": "orders", "level": "read"}
    assert all(admin.client.post("/validate", json=body, headers={"X-Forwarded-For": "10.0.0.7"}).status_code == 200 for _ in range(4))
    assert rate_limit.rejection_stats()["rejectedTotal"] == 0
    for _ in range(3):
        admin.client.post("/validate", json=body, headers={"X-Forwarded-For": "10.0.0.8"})
    assert rate_limit.rejection_stats()["rejectedTotal"] == 1

    # Saving the same values again writes nothing.
    assert admin.put({"baseVersion": 1, "maxReq": 2}).json()["version"] == 1


def test_stale_version_is_a_conflict(admin):
    admin.login()
    assert admin.put({"baseVersion": 0, "maxReq": 7}).status_code == 200
    res = admin.put({"baseVersion": 0, "exempt": []})
    assert res.status_code == 409
    assert res.json()["code"] == "conflict"
    assert res.json()["current"]["version"] == 1
    assert res.json()["current"]["fields"]["maxReq"]["value"] == 7


def test_invalid_input_is_refused(admin):
    admin.login()
    for body, field in [
        ({"baseVersion": 0, "maxReq": 0}, "maxReq"),
        ({"baseVersion": 0, "windowSeconds": 3601}, "windowMs"),
        ({"baseVersion": 0, "exempt": ["10.0.0.5/8"]}, "exempt"),
        ({"baseVersion": 0, "maxReq": None}, "maxReq"),
    ]:
        res = admin.put(body)
        assert res.status_code == 422, body
        assert res.json()["field"] == field
    assert admin.put({"baseVersion": 0, "maxReq": "5"}).status_code == 422
    # A lone surrogate (JSON "\ud800") is refused with a readable message, not a server error.
    res = admin.client.put(
        "/admin/api/settings/rate-limit",
        content=b'{"baseVersion": 0, "exempt": ["\\ud800"]}',
        headers={"Content-Type": "application/json", "X-CSRF-Token": admin.csrf, "X-Forwarded-For": "10.9.9.9"},
    )
    assert res.status_code == 422 and res.json()["field"] == "exempt"
    assert admin.put({"baseVersion": 0, "other": 1}).status_code == 422
    assert admin.get().json()["version"] == 0


def test_locked_field_cannot_be_sent_but_others_can_change(admin, monkeypatch):
    _, _, _, rls = _mods()
    from app.config import Settings

    monkeypatch.setenv("RATE_LIMIT_MAX_REQ", "500")
    s = Settings(_env_file=None)
    monkeypatch.setattr(rls, "get_settings", lambda: s)
    admin.login()
    view = admin.get().json()
    assert view["fields"]["maxReq"] == {**view["fields"]["maxReq"], "value": 500, "source": "env", "locked": True}
    res = admin.put({"baseVersion": 0, "maxReq": 5})
    assert res.status_code == 409 and res.json()["code"] == "locked"
    assert admin.put({"baseVersion": 0, "windowSeconds": 10}).status_code == 200


@pytest.mark.parametrize("frm", ["172.18.0.50", "::ffff:172.18.0.50"])
def test_own_address_needs_confirmation(admin, frm):
    admin.login()
    res = admin.put({"baseVersion": 0, "exempt": ["172.18.0.50"]}, frm=frm)
    assert res.status_code == 409 and res.json()["code"] == "own_address"
    assert admin.get().json()["version"] == 0
    res = admin.put({"baseVersion": 0, "exempt": ["172.18.0.50"], "confirmOwnAddress": True}, frm=frm)
    assert res.status_code == 200
    # Already exempt: changing something else does not ask again.
    assert admin.put({"baseVersion": 1, "maxReq": 9}, frm=frm).status_code == 200


def test_own_address_inside_a_new_network_needs_confirmation_once(admin):
    admin.login()
    res = admin.put({"baseVersion": 0, "exempt": ["172.18.0.0/16"]}, frm="172.18.1.7")
    assert res.status_code == 409 and res.json()["code"] == "own_address"
    assert admin.put({"baseVersion": 0, "exempt": ["172.18.0.0/16"], "confirmOwnAddress": True}, frm="172.18.1.7").status_code == 200
    # Narrowing to a smaller network that still holds the address does not exempt anything new.
    assert admin.put({"baseVersion": 1, "exempt": ["172.18.1.0/24"]}, frm="172.18.1.7").status_code == 200


def test_proxy_network_is_shown(admin, monkeypatch):
    admin.login()
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    assert admin.get().json()["proxyNetwork"] == {"envName": "FORWARDED_ALLOW_IPS", "value": None}
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "172.18.0.0/16")
    assert admin.get().json()["proxyNetwork"] == {"envName": "FORWARDED_ALLOW_IPS", "value": "172.18.0.0/16"}
    # Empty is not the same as unset: uvicorn then trusts no address at all.
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "")
    assert admin.get().json()["proxyNetwork"] == {"envName": "FORWARDED_ALLOW_IPS", "value": ""}


def test_activity_copy_failure_still_saves(admin, monkeypatch):
    from app.routers import admin_settings

    async def broken(*_a, **_k):
        raise RuntimeError("simulated")

    monkeypatch.setattr(admin_settings, "log_activity", broken)
    admin.login()
    res = admin.put({"baseVersion": 0, "maxReq": 3})
    assert res.status_code == 200
    assert res.json()["warning"] == "Saved, but the activity log entry could not be written."
    assert res.json()["version"] == 1


def test_invalid_stored_settings_stop_the_start(monkeypatch, tmp_path):
    main_mod, db_mod, _, _ = _mods()
    from app.schema import ensure_database_schema

    url = f"sqlite:///{tmp_path}/broken.db"

    async def prepare():
        db = await db_mod.create_database(url)
        await ensure_database_schema(db)
        await db.execute("INSERT INTO rate_limit_settings (version, value, actor) VALUES (1, 'nope', 'x')")
        await db.close()

    _run(prepare())
    create_database = db_mod.create_database
    monkeypatch.setattr(main_mod, "create_database", lambda _url: create_database(url))
    monkeypatch.setattr(main_mod.redis, "from_url", lambda *a, **k: FakeRedis())
    with pytest.raises(RuntimeError, match="version 1 in the table rate_limit_settings"):
        with TestClient(main_mod.app):
            pass

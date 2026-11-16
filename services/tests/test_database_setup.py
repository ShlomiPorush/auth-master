"""The database connection managed by the service: data/database.json, setup without a
database, recovery mode, the Settings page connection change, the request gate and the
process registry (app.db_config, app.db_runtime, app.db_operations, app.db_gate).

The app runs with its full lifespan in a temporary working and data directory, with an
in-memory fake Redis.  PostgreSQL cases need DBUI_TEST_ADMIN_URL: a superuser connection
(to the maintenance database) of a throwaway server, used to create a new database and
user per test, which are dropped afterwards.  Point it only at a server you can throw
away.  The CI PostgreSQL job refuses to run without it, so these cases never skip there.

Every secret, including the database passwords, is generated per run with ``secrets``.
Requires pytest and httpx.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import stat
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_SECRETS = {
    "ADMIN_API_KEY": secrets.token_urlsafe(32),
    "SESSION_SECRET": secrets.token_urlsafe(48),
    "APP_ENCRYPTION_KEY": secrets.token_hex(16),
}
for _name, _value in _SECRETS.items():
    os.environ.setdefault(_name, _value)
os.environ.setdefault("REDIS_URL", f"redis://:{secrets.token_hex(32)}@127.0.0.1:6379/0")

import pyotp  # noqa: E402

ADMIN_URL = os.environ.get("DBUI_TEST_ADMIN_URL")
needs_pg = pytest.mark.skipif(not ADMIN_URL, reason="DBUI_TEST_ADMIN_URL is not set")



# ── Fake Redis (sessions, TOTP counters) ────────────────────────

class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, k):
        return self.data.get(k)

    async def setex(self, k, _ttl, v):
        self.data[k] = v

    async def set(self, k, v, nx=False, ex=None):
        return self._set(k, v, nx=nx, ex=ex)

    def _set(self, k, v, ex=None, nx=False):
        if nx and k in self.data:
            return None
        self.data[k] = str(v)
        return True

    def _incr(self, k):
        n = int(self.data.get(k, "0")) + 1
        self.data[k] = str(n)
        return n

    def _ttl(self, k):
        return 900 if k in self.data else -2

    async def delete(self, *keys):
        for k in keys:
            self.data.pop(k, None)

    async def expire(self, k, _ttl):
        return k in self.data

    async def ping(self):
        return True

    async def aclose(self):
        return None

    def pipeline(self, transaction=True):
        return _Pipe(self)


class _Pipe:
    def __init__(self, fake):
        self.fake, self.queue = fake, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __getattr__(self, name):
        def add(*a, **k):
            self.queue.append((name, a, k))
            return self
        return add

    async def execute(self, raise_on_error=True):
        out = [getattr(self.fake, "_" + n)(*a, **k) for n, a, k in self.queue]
        self.queue = []
        return out


# ── The service in a temporary directory ────────────────────────

class Service:
    def __init__(self, app, redis, data_dir: Path, master_key: str):
        self.app, self.redis, self.data_dir, self.master_key = app, redis, data_dir, master_key
        self.clients = []

    @property
    def runtime(self):
        return self.app.state.runtime

    def client(self, **headers):
        import httpx

        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test",
                              headers=headers, follow_redirects=False)
        self.clients.append(c)
        return c

    def recovery_client(self):
        return self.client(Authorization=f"Bearer {self.master_key}")

    async def add_admin(self) -> tuple[str, str]:
        from app.crypto_totp import encrypt_totp_secret

        admin_id, totp = str(uuid.uuid4()), pyotp.random_base32()
        await self.app.state.db.execute(
            "INSERT INTO admin_users (id, username, password_hash, totp_secret_enc, totp_enabled) VALUES ($1, $2, $3, $4, $5)",
            admin_id, "admin", "unused", encrypt_totp_secret(totp), True,
        )
        return admin_id, totp

    async def signed_in(self, admin_id: str):
        from app.sessions import COOKIE_NAME, SESSION_TTL, new_csrf, new_sid, save_session

        sid, csrf = new_sid(), new_csrf()
        await save_session(self.redis, sid, {"kind": "full", "adminId": admin_id, "csrf": csrf}, SESSION_TTL)
        c = self.client(**{"X-CSRF-Token": csrf})
        c.cookies.set(COOKIE_NAME, sid)
        return c

    def config_text(self) -> str:
        return (self.data_dir / "database.json").read_text(encoding="utf-8")

    def config(self) -> dict:
        return json.loads(self.config_text())


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    from app import db_config
    from app.config import get_settings

    # The settings object is shared with other test modules: patched per test, restored after.
    s = get_settings()
    monkeypatch.setattr(s, "database_url", None)
    if not s.admin_api_key.strip():
        monkeypatch.setattr(s, "admin_api_key", secrets.token_urlsafe(32))
    if not s.env_value("redis_url"):
        monkeypatch.setattr(s, "redis_url", f"redis://:{secrets.token_hex(32)}@127.0.0.1:6379/0")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(db_config, "DATA_DIR", tmp_path / "data")
    return tmp_path


@contextlib.asynccontextmanager
async def service(monkeypatch, redis: FakeRedis | None = None):
    import redis.asyncio as redis_asyncio

    import app.main as main_mod
    from app import db_config, general_settings, rate_limit
    from app.config import get_settings

    fake = redis or FakeRedis()
    monkeypatch.setattr(redis_asyncio, "from_url", lambda *a, **k: fake)

    async def no_cleanup(*_a, **_k):
        return None

    monkeypatch.setattr(main_mod, "cleanup_expired_tokens", no_cleanup)
    app = main_mod.app
    svc = Service(app, fake, db_config.data_dir(), get_settings().admin_api_key)
    try:
        async with app.router.lifespan_context(app):
            yield svc
            for c in svc.clients:
                await c.aclose()
    finally:
        rate_limit.reset_state()
        general_settings.reset_state()


def run(coro):
    return asyncio.run(coro)


async def wait_job(client, url: str, timeout: float = 30.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        res = await client.get(url)
        assert res.status_code == 200, res.text
        job = res.json()["job"]
        if job["state"] != "running":
            return job
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"job still running: {job}")
        await asyncio.sleep(0.05)


async def setup_sqlite(svc: Service) -> dict:
    c = svc.client()
    res = await c.post("/admin/api/setup/database", json={"backend": "sqlite"})
    assert res.status_code == 202, res.text
    job = await wait_job(c, f"/admin/api/setup/database/jobs/{res.json()['job']['id']}")
    assert job["state"] == "done", job
    return job


async def build_older_sqlite(path: Path) -> None:
    """A database file as an earlier version leaves it: the service's tables, no installation."""
    from app.db import create_database
    from app.schema import ensure_database_schema

    path.parent.mkdir(parents=True, exist_ok=True)
    db = await create_database(f"sqlite:///{path}")
    await ensure_database_schema(db)
    await db.execute("DROP TABLE installation")
    await db.execute("INSERT INTO zones (id, name) VALUES ($1, $2)", str(uuid.uuid4()), "older-zone")
    await db.close()


def older_sqlite_file(path: Path) -> None:
    run(build_older_sqlite(path))


# ── Unit: the configuration file ────────────────────────────────

def test_config_round_trip_keeps_the_password_encrypted(workdir):
    from app import db_config

    marker = "mk" + secrets.token_hex(16)
    cfg = db_config.Config(db_config.POSTGRES, "iid-1", pg=db_config.PgParams("db.example", 5432, "auth", "svc", marker))
    db_config.publish_config(cfg)
    text = db_config.config_path().read_text(encoding="utf-8")
    assert marker not in text
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(db_config.config_path()).st_mode) == 0o600
    again = db_config.read_config()
    assert again.pg.password == marker and again.installation_id == "iid-1" and again.pg.sslmode == "prefer"
    assert not list(db_config.data_dir().glob(".database.json.*"))


def test_publish_that_fails_keeps_the_old_file(workdir, monkeypatch):
    from app import db_config

    db_config.publish_config(db_config.Config(db_config.SQLITE, "old", sqlite_path="data/auth.db"))

    def broken(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(db_config.os, "replace", broken)
    with pytest.raises(OSError):
        db_config.publish_config(db_config.Config(db_config.SQLITE, "new", sqlite_path="data/auth.db"))
    assert db_config.read_config().installation_id == "old"
    assert not list(db_config.data_dir().glob(".database.json.*"))


def test_config_with_another_key_is_unreadable_not_invalid(workdir, monkeypatch):
    from app import db_config
    from app.config import get_settings

    db_config.publish_config(db_config.Config(db_config.POSTGRES, "iid", pg=db_config.PgParams("h", 5432, "d", "u", secrets.token_hex(16))))
    monkeypatch.setattr(get_settings(), "app_encryption_key", secrets.token_hex(16))
    with pytest.raises(db_config.ConfigError) as e:
        db_config.read_config()
    assert e.value.code == "config_unreadable"


@pytest.mark.parametrize("change, field", [
    ({"password": ""}, "password"),
    ({"password": "changeme"}, "password"),
    ({"port": 0}, "port"),
    ({"host": "bad host"}, "host"),
    ({"sslmode": "allow"}, "sslmode"),
    ({"ca_pem": "not a certificate", "sslmode": "verify-full"}, "caPem"),
    ({"ca_pem": "x", "sslmode": "prefer"}, "caPem"),
])
def test_connection_fields_are_checked(change, field):
    from dataclasses import replace

    from app import db_config

    base = db_config.PgParams("db.example", 5432, "auth", "svc", secrets.token_hex(16))
    assert db_config.params_problem(base) is None
    assert db_config.params_problem(replace(base, **change))[0] == field


def test_tls_argument_follows_the_choice():
    import ssl

    from app import db_config

    base = db_config.PgParams("db.example", 5432, "auth", "svc", "x")
    for mode in ("prefer", "disable", "require"):
        assert db_config.ssl_argument(db_config.PgParams("h", 1, "d", "u", "p", mode)) == mode
    ctx = db_config.ssl_argument(db_config.PgParams("h", 1, "d", "u", "p", "verify-full"))
    assert isinstance(ctx, ssl.SSLContext) and ctx.check_hostname and ctx.verify_mode == ssl.CERT_REQUIRED
    assert base.sslmode == "prefer"


def test_service_tables_list_matches_the_schema(tmp_path):
    from app import db_inspect
    from app.schema import INSTALLATION_TABLE, SERVICE_TABLES

    async def go():
        from app.db import create_database
        from app.schema import ensure_database_schema

        db = await create_database(f"sqlite:///{tmp_path / 'drift.db'}")
        try:
            await ensure_database_schema(db)
            names = set(await db_inspect.relations(db))
        finally:
            await db.close()
        assert names == set(SERVICE_TABLES) | {INSTALLATION_TABLE}

    run(go())


# ── Unit: registry, gate, master key ────────────────────────────

def test_registry_refuses_while_another_process_holds_its_file(tmp_path):
    from app.db_runtime import OperationError, Registry

    first, second = Registry(), Registry()
    assert first.register(tmp_path) and second.register(tmp_path)
    with pytest.raises(OperationError) as e:
        with first.exclusive():
            pass
    assert e.value.code == "other_process"
    second.close()
    # A file whose process ended (no lock) is removed, not counted.
    (tmp_path / ".proc" / "ended.lock").write_text("")
    with first.exclusive():
        pass
    assert not (tmp_path / ".proc" / "ended.lock").exists()
    first.close()


def test_gate_holds_new_requests_and_drains_running_ones():
    from app.db_runtime import Gate

    async def go():
        gate = Gate()
        assert await gate.enter(1)
        closing = asyncio.create_task(gate.close_and_drain(2))
        await asyncio.sleep(0.05)
        assert not closing.done()
        waiting = asyncio.create_task(gate.enter(2))
        await asyncio.sleep(0.05)
        assert not waiting.done()
        gate.leave()
        assert await closing
        assert not waiting.done()
        gate.reopen()
        assert await waiting
        gate.leave()
        # A drain that does not finish in time reopens the gate.
        assert await gate.enter(1)
        assert not await gate.close_and_drain(0.05)
        assert gate.is_open
        gate.leave()

    run(go())


def test_master_key_comparison(monkeypatch):
    from app import db_runtime
    from app.config import get_settings

    key = secrets.token_urlsafe(32)
    monkeypatch.setattr(get_settings(), "admin_api_key", key)
    assert db_runtime.master_key_matches(f"Bearer {key}")
    assert not db_runtime.master_key_matches(f"Bearer {key}x")
    assert not db_runtime.master_key_matches(None)
    monkeypatch.setattr(get_settings(), "admin_api_key", "")
    assert not db_runtime.master_key_matches("Bearer ")


# ── SQLite: setup, restart, adoption, recovery ──────────────────

def test_without_a_database_only_setup_works(workdir, monkeypatch):
    async def go():
        async with service(monkeypatch) as svc:
            c = svc.client()
            res = await c.get("/health")
            assert res.status_code == 200 and res.json()["status"] == "setup_required"
            assert (await c.get("/admin/api/setup/status")).json() == {"needsSetup": True, "needsDatabase": True}
            res = await c.get("/admin/api/tokens")
            assert res.status_code == 503 and res.json()["code"] == "setup_required"
            for page in ("/login", "/admin/tokens", "/recovery"):
                res = await c.get(page)
                assert res.status_code == 302 and res.headers["location"] == "/setup", page
            assert (await c.get("/setup")).status_code == 200
            assert not (svc.data_dir / "auth.db").exists()

    run(go())


def test_setup_with_sqlite_saves_the_choice_and_survives_a_restart(workdir, monkeypatch):
    async def go():
        async with service(monkeypatch) as svc:
            job = await setup_sqlite(svc)
            assert job["result"]["next"] == "register"
            cfg = svc.config()
            assert cfg["backend"] == "sqlite" and cfg["sqlite"]["path"].endswith("auth.db")
            if os.name == "posix":
                assert stat.S_IMODE(os.stat(svc.data_dir / "database.json").st_mode) == 0o600
            row = await svc.app.state.db.fetchrow("SELECT id, status FROM installation")
            assert row["id"] == cfg["installationId"] and row["status"] == "active"
            assert (await svc.app.state.db.fetchval("PRAGMA journal_mode")) == "wal"
            c = svc.client()
            assert (await c.get("/admin/api/setup/status")).json() == {"needsSetup": True}
            assert (await c.get("/health")).json() == {"ok": True, "db": True, "redis": True}
            res = await c.post("/admin/api/setup/database", json={"backend": "sqlite"})
            assert res.status_code == 409 and res.json()["code"] == "already_configured"
            iid = cfg["installationId"]
        async with service(monkeypatch) as svc:
            assert svc.runtime.mode == "ready" and svc.runtime.source == "file"
            assert (await svc.app.state.db.fetchval("SELECT id FROM installation")) == iid
            assert not list(svc.data_dir.glob("auth.db.setup-*"))

    run(go())


def test_an_earlier_version_auth_db_is_adopted_at_start(workdir, monkeypatch):
    from app import db_config

    older_sqlite_file(db_config.data_dir() / "auth.db")

    async def go():
        async with service(monkeypatch) as svc:
            assert svc.runtime.mode == "ready"
            cfg = svc.config()
            assert (await svc.app.state.db.fetchval("SELECT id FROM installation")) == cfg["installationId"]
            assert (await svc.app.state.db.fetchval("SELECT name FROM zones WHERE name = 'older-zone'")) == "older-zone"

    run(go())


def test_a_file_without_the_service_tables_is_not_adopted(workdir, monkeypatch):
    import sqlite3

    from app import db_config

    db_config.data_dir().mkdir(parents=True)
    sqlite3.connect(db_config.data_dir() / "auth.db").execute("CREATE TABLE other (x)").connection.commit()

    async def go():
        async with service(monkeypatch) as svc:
            assert svc.runtime.mode == "recovery" and svc.runtime.failure_code == "legacy_unrecognised"
            assert not (svc.data_dir / "database.json").exists()

    run(go())


def test_a_missing_database_file_leads_to_recovery_not_setup(workdir, monkeypatch):
    async def go():
        async with service(monkeypatch) as svc:
            await setup_sqlite(svc)
        (svc.data_dir / "auth.db").unlink()
        for extra in svc.data_dir.glob("auth.db-*"):
            extra.unlink()
        async with service(monkeypatch) as svc:
            assert svc.runtime.mode == "recovery" and svc.runtime.failure_code == "sqlite_missing"
            c = svc.client()
            res = await c.get("/health")
            assert res.status_code == 503 and res.json()["status"] == "database_unavailable"
            assert (await c.get("/admin/tokens")).headers["location"] == "/recovery"
            res = await c.post("/admin/api/setup/database", json={"backend": "sqlite"})
            assert res.status_code == 503 and res.json()["code"] == "sqlite_missing"
            assert not (svc.data_dir / "auth.db").exists()

    run(go())


def test_recovery_needs_the_master_key_and_limits_wrong_keys(workdir, monkeypatch):
    from app import db_config

    db_config.publish_config(db_config.Config(db_config.SQLITE, "iid", sqlite_path=str(db_config.data_dir() / "auth.db")))

    async def go():
        async with service(monkeypatch) as svc:
            c = svc.client()
            res = await c.get("/admin/api/recovery/status")
            assert res.json() == {"mode": "recovery", "masterKeyConfigured": True}
            bad = svc.client(Authorization="Bearer wrong")
            for _ in range(10):
                assert (await bad.post("/admin/api/recovery/check", json={"backend": "sqlite"})).status_code == 401
            res = await bad.post("/admin/api/recovery/check", json={"backend": "sqlite"})
            assert res.status_code == 429 and int(res.headers["retry-after"]) > 0
            good = svc.recovery_client()
            assert (await good.get("/admin/api/recovery/status")).status_code == 429  # same address
            svc.runtime.attempts.reset()
            res = await good.get("/admin/api/recovery/status")
            assert res.status_code == 200 and res.json()["code"] == "sqlite_missing"

    run(go())


def test_recovery_adopts_an_older_backup_of_the_sqlite_file(workdir, monkeypatch):
    from app import db_config

    async def go():
        async with service(monkeypatch) as svc:
            await setup_sqlite(svc)
            iid = svc.config()["installationId"]
        for f in svc.data_dir.glob("auth.db*"):
            f.unlink()
        await build_older_sqlite(svc.data_dir / "auth.db")
        async with service(monkeypatch) as svc:
            assert svc.runtime.failure_code == "older_installation"
            c = svc.recovery_client()
            res = await c.post("/admin/api/recovery/check", json={"backend": "sqlite"})
            assert res.json()["state"] == "older"
            res = await c.post("/admin/api/recovery/save", json={"backend": "sqlite"})
            job = await wait_job(c, f"/admin/api/recovery/jobs/{res.json()['job']['id']}")
            assert job["code"] == "confirm_recovery"
            res = await c.post("/admin/api/recovery/save", json={"backend": "sqlite", "confirm": True})
            job = await wait_job(c, f"/admin/api/recovery/jobs/{res.json()['job']['id']}")
            assert job["state"] == "done", job
            assert svc.runtime.mode == "ready"
            # The id in database.json is kept, so the file matches it.
            assert (await svc.app.state.db.fetchval("SELECT id FROM installation")) == iid
            assert db_config.read_config().installation_id == iid
            logged = await svc.app.state.db.fetchval(
                "SELECT actor FROM activity_logs WHERE action = 'database.adopted'"
            )
            assert logged == "master_api_key"

    run(go())


def test_a_second_operation_while_one_runs_gets_409(workdir, monkeypatch):
    from app import db_operations

    async def go():
        started = asyncio.Event()
        release = asyncio.Event()
        original = db_operations.check_sqlite

        async def slow(path):
            started.set()
            await release.wait()
            return await original(path)

        monkeypatch.setattr(db_operations, "check_sqlite", slow)
        async with service(monkeypatch) as svc:
            c = svc.client()
            first = await c.post("/admin/api/setup/database", json={"backend": "sqlite"})
            assert first.status_code == 202
            await started.wait()
            second = await c.post("/admin/api/setup/database", json={"backend": "sqlite"})
            assert second.status_code == 409 and second.json()["job"]["id"] == first.json()["job"]["id"]
            release.set()
            job = await wait_job(c, f"/admin/api/setup/database/jobs/{first.json()['job']['id']}")
            assert job["state"] == "done"

    run(go())


def test_setup_refuses_while_another_process_uses_the_data_directory(workdir, monkeypatch):
    from app import db_config
    from app.db_runtime import Registry

    other = Registry()
    db_config.data_dir().mkdir(parents=True, exist_ok=True)
    assert other.register(db_config.data_dir())

    async def go():
        async with service(monkeypatch) as svc:
            c = svc.client()
            res = await c.post("/admin/api/setup/database", json={"backend": "sqlite"})
            job = await wait_job(c, f"/admin/api/setup/database/jobs/{res.json()['job']['id']}")
            assert job["code"] == "other_process"
            assert svc.runtime.mode == "setup"
            assert not (svc.data_dir / "database.json").exists()
            # Nothing was created before the check either.
            assert not (svc.data_dir / "auth.db").exists()
            assert not list(svc.data_dir.glob("auth.db.setup-*"))

    try:
        run(go())
    finally:
        other.close()


def test_validation_errors_do_not_echo_the_input(workdir, monkeypatch):
    marker = "mk" + secrets.token_hex(16)

    async def go():
        async with service(monkeypatch) as svc:
            c = svc.client()
            res = await c.post("/admin/api/setup/database/check", json={
                "backend": "postgres", "postgres": {"host": "h", "port": "x", "database": "d", "user": "u", "password": marker},
            })
            assert res.status_code == 422 and res.json()["field"] == "port"
            assert marker not in res.text
            res = await c.post("/admin/api/setup/database/check", json={"backend": "postgres", marker: 1})
            assert res.status_code == 422 and marker not in res.text

    run(go())


# ── PostgreSQL ──────────────────────────────────────────────────

class Pg:
    """A database and a user of their own on the throwaway server, dropped afterwards."""

    def __init__(self) -> None:
        parts = urlsplit(ADMIN_URL)
        self.host, self.port = parts.hostname, parts.port or 5432
        self.name = "dbui_" + secrets.token_hex(6)
        self.user = self.name + "_u"
        self.password = "mk" + secrets.token_hex(16)
        self.extra_dbs: list[str] = []
        self.extra_roles: list[str] = []

    async def admin(self, database: str | None = None):
        import asyncpg

        if database is None:
            return await asyncpg.connect(ADMIN_URL)
        return await asyncpg.connect(ADMIN_URL, database=database)

    async def create(self) -> "Pg":
        conn = await self.admin()
        try:
            await conn.execute(f"CREATE ROLE {self.user} LOGIN PASSWORD '{self.password}'")
            await conn.execute(f"CREATE DATABASE {self.name} OWNER {self.user}")
        finally:
            await conn.close()
        conn = await self.admin(self.name)
        try:
            # PostgreSQL 15 and later: only the owner may create in public by default.
            await conn.execute(f"ALTER SCHEMA public OWNER TO {self.user}")
        finally:
            await conn.close()
        return self

    async def add_database(self, owner: str | None = None) -> str:
        name = "dbui_" + secrets.token_hex(6)
        conn = await self.admin()
        try:
            await conn.execute(f"CREATE DATABASE {name} OWNER {owner or self.user}")
        finally:
            await conn.close()
        conn = await self.admin(name)
        try:
            await conn.execute(f"ALTER SCHEMA public OWNER TO {owner or self.user}")
        finally:
            await conn.close()
        self.extra_dbs.append(name)
        return name

    async def add_role(self, password: str) -> str:
        name = "dbui_" + secrets.token_hex(6) + "_r"
        conn = await self.admin()
        try:
            await conn.execute(f"CREATE ROLE {name} LOGIN PASSWORD '{password}'")
        finally:
            await conn.close()
        self.extra_roles.append(name)
        return name

    async def sql(self, statement: str, database: str | None = None):
        conn = await self.admin(database or self.name)
        try:
            return await conn.execute(statement)
        finally:
            await conn.close()

    async def drop(self) -> None:
        conn = await self.admin()
        try:
            for db in [self.name, *self.extra_dbs]:
                await conn.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1 AND pid <> pg_backend_pid()", db
                )
                await conn.execute(f"DROP DATABASE IF EXISTS {db}")
            for role in [*self.extra_roles, self.user]:
                await conn.execute(f"DROP ROLE IF EXISTS {role}")
        finally:
            await conn.close()

    def body(self, **change) -> dict:
        b = {"host": self.host, "port": self.port, "database": self.name, "user": self.user,
             "password": self.password, "sslmode": "prefer"}
        b.update(change)
        return b


@contextlib.asynccontextmanager
async def pg_database():
    pg = await Pg().create()
    try:
        yield pg
    finally:
        await pg.drop()


async def setup_postgres(svc: Service, pg: Pg, **extra) -> dict:
    c = svc.client()
    res = await c.post("/admin/api/setup/database", json={"backend": "postgres", "postgres": pg.body(), **extra})
    assert res.status_code == 202, res.text
    return await wait_job(c, f"/admin/api/setup/database/jobs/{res.json()['job']['id']}")


@needs_pg
def test_setup_with_an_empty_postgres_database_and_restart(workdir, monkeypatch, capsys):
    async def go():
        async with pg_database() as pg:
            async with service(monkeypatch) as svc:
                c = svc.client()
                res = await c.post("/admin/api/setup/database/check", json={"backend": "postgres", "postgres": pg.body()})
                assert res.json()["ok"] and res.json()["state"] == "empty" and res.json()["problems"] == []
                job = await setup_postgres(svc, pg)
                assert job["state"] == "done" and job["result"]["next"] == "register", job
                assert svc.runtime.mode == "ready" and svc.app.state.db.is_pg
                text = svc.config_text()
                assert pg.password not in text
                assert json.loads(text)["postgres"]["sslmode"] == "prefer"
                assert pg.password not in res.text + json.dumps(job)
                iid = svc.config()["installationId"]
            async with service(monkeypatch) as svc:
                assert svc.runtime.mode == "ready"
                assert (await svc.app.state.db.fetchval("SELECT id FROM installation")) == iid
        assert pg.password not in capsys.readouterr().out

    run(go())


@needs_pg
def test_setup_finds_an_existing_installation_and_leads_to_sign_in(workdir, monkeypatch):
    async def go():
        async with pg_database() as pg:
            async with service(monkeypatch) as svc:
                await setup_postgres(svc, pg)
                await svc.add_admin()
                iid = svc.config()["installationId"]
            (svc.data_dir / "database.json").unlink()
            async with service(monkeypatch) as svc:
                assert svc.runtime.mode == "setup"
                c = svc.client()
                res = await c.post("/admin/api/setup/database/check", json={"backend": "postgres", "postgres": pg.body()})
                assert res.json()["state"] == "installation" and res.json()["hasAdmin"]
                job = await setup_postgres(svc, pg)
                assert job["result"]["next"] == "login"
                assert svc.config()["installationId"] == iid
                assert (await svc.app.state.db.fetchval("SELECT count(*) FROM admin_users")) == 1

    run(go())


@needs_pg
def test_setup_adopts_an_older_postgres_installation_only_when_confirmed(workdir, monkeypatch):
    async def go():
        async with pg_database() as pg:
            from app.db import create_database
            from app.schema import ensure_database_schema

            db = await create_database(f"postgres://{pg.user}:{pg.password}@{pg.host}:{pg.port}/{pg.name}")
            await ensure_database_schema(db)
            await db.execute("DROP TABLE installation")
            await db.close()
            async with service(monkeypatch) as svc:
                c = svc.client()
                res = await c.post("/admin/api/setup/database/check", json={"backend": "postgres", "postgres": pg.body()})
                assert res.json()["state"] == "older"
                job = await setup_postgres(svc, pg)
                assert job["code"] == "confirm_adopt" and svc.runtime.mode == "setup"
                job = await setup_postgres(svc, pg, adopt=True)
                assert job["state"] == "done" and job["result"]["action"] == "database.adopted"
                assert svc.runtime.mode == "ready"

    run(go())


@needs_pg
def test_setup_refuses_a_database_with_other_tables(workdir, monkeypatch):
    async def go():
        async with pg_database() as pg:
            await pg.sql(f"CREATE TABLE customers (id int); ALTER TABLE customers OWNER TO {pg.user}")
            async with service(monkeypatch) as svc:
                job = await setup_postgres(svc, pg)
                assert job["code"] == "foreign_database" and "customers" in job["error"]
                assert svc.runtime.mode == "setup" and not (svc.data_dir / "database.json").exists()
                assert (await pg.sql("SELECT 1", pg.name)) is not None

    run(go())


@needs_pg
def test_tls_verify_full_against_a_server_without_tls_fails_with_a_code(workdir, monkeypatch):
    async def go():
        async with pg_database() as pg:
            async with service(monkeypatch) as svc:
                c = svc.client()
                res = await c.post("/admin/api/setup/database/check",
                                   json={"backend": "postgres", "postgres": pg.body(sslmode="verify-full")})
                assert res.json()["ok"] is False and res.json()["code"] == "tls_failed"
                assert pg.password not in res.text

    run(go())


_codes_used: dict[str, int] = {}


def fresh_code(totp: str) -> str:
    """A code not used yet: each code works once (app.totp_guard), so a test that saves
    twice takes the next time step's code, which the check accepts one step early."""
    import time

    step = _codes_used.get(totp, -1) + 1
    _codes_used[totp] = step
    return pyotp.TOTP(totp).at(int(time.time()) + 30 * step)


async def _edit(svc: Service, c, body: dict, totp: str, **extra) -> dict:
    res = await c.put("/admin/api/settings/database", json={"postgres": body, "code": fresh_code(totp), **extra})
    assert res.status_code == 202, res.text
    return await wait_job(c, f"/admin/api/settings/database/jobs/{res.json()['job']['id']}")


@needs_pg
def test_user_change_from_settings_under_load(workdir, monkeypatch, capsys):
    """Requests keep running during the switch: none fails, the admin session continues,
    and a restart starts with the new user."""
    async def go():
        async with pg_database() as pg:
            async with service(monkeypatch) as svc:
                await setup_postgres(svc, pg)
                admin_id, totp = await svc.add_admin()
                c = await svc.signed_in(admin_id)
                view = (await c.get("/admin/api/settings/database")).json()
                assert view["editable"] and view["connection"]["user"] == pg.user and "password" not in json.dumps(view)

                password = "mk" + secrets.token_hex(16)
                role = await pg.add_role(password)
                await pg.sql(f"GRANT {pg.user} TO {role}")
                body = pg.body(user=role, password=password)
                res = await c.post("/admin/api/settings/database/check", json={"postgres": body})
                assert res.json()["ok"] and res.json()["identity"] == "same" and res.json()["problems"] == [], res.text

                statuses: list[int] = []
                stop = asyncio.Event()

                async def hammer(client, path):
                    while not stop.is_set():
                        statuses.append((await client.get(path)).status_code)
                        await asyncio.sleep(0)

                workers = [
                    asyncio.create_task(hammer(svc.client(), "/health")),
                    asyncio.create_task(hammer(await svc.signed_in(admin_id), "/admin/api/settings/database")),
                ]
                try:
                    job = await _edit(svc, c, body, totp)
                    await asyncio.sleep(0.2)  # requests on the new connection too
                finally:
                    stop.set()
                    await asyncio.gather(*workers)
                assert job["state"] == "done" and job["result"]["action"] == "database.connection_changed", job
                assert len(statuses) > 10 and set(statuses) == {200}, set(statuses)
                assert password not in svc.config_text() and pg.password not in svc.config_text()
                assert await svc.app.state.db.fetchval("SELECT current_user") == role
                details = await svc.app.state.db.fetchval(
                    "SELECT details FROM activity_logs WHERE action = 'database.connection_changed'"
                )
                assert role in details and password not in details and pg.password not in details
                # The same admin session works on the new connection.
                assert (await c.get("/admin/api/settings/database")).status_code == 200
            async with service(monkeypatch) as svc:
                assert svc.runtime.mode == "ready"
                assert await svc.app.state.db.fetchval("SELECT current_user") == role
        out = capsys.readouterr().out
        assert pg.password not in out and password not in out

    run(go())


@needs_pg
def test_password_rotation_from_settings(workdir, monkeypatch, capsys):
    async def go():
        async with pg_database() as pg:
            async with service(monkeypatch) as svc:
                await setup_postgres(svc, pg)
                admin_id, totp = await svc.add_admin()
                c = await svc.signed_in(admin_id)
                new_password = "mk" + secrets.token_hex(16)
                await pg.sql(f"ALTER ROLE {pg.user} PASSWORD '{new_password}'")
                job = await _edit(svc, c, pg.body(password=new_password), totp)
                assert job["state"] == "done" and job["result"]["action"] == "database.connection_changed", job
                from app import db_config

                assert db_config.read_config().pg.password == new_password
                assert new_password not in svc.config_text()
            async with service(monkeypatch) as svc:
                assert svc.runtime.mode == "ready"
        out = capsys.readouterr().out
        assert pg.password not in out and new_password not in out

    run(go())


@needs_pg
def test_settings_save_needs_the_second_factor(workdir, monkeypatch):
    async def go():
        async with pg_database() as pg:
            async with service(monkeypatch) as svc:
                await setup_postgres(svc, pg)
                admin_id, _totp = await svc.add_admin()
                c = await svc.signed_in(admin_id)
                res = await c.put("/admin/api/settings/database", json={"postgres": pg.body(), "code": "000000"})
                assert res.status_code in (400, 401, 403)
                assert svc.runtime.jobs.current.kind == "setup"  # no edit job started

    run(go())


@needs_pg
def test_a_restored_copy_needs_confirmation(workdir, monkeypatch):
    async def go():
        async with pg_database() as pg:
            async with service(monkeypatch) as svc:
                await setup_postgres(svc, pg)
                admin_id, totp = await svc.add_admin()
                iid = svc.config()["installationId"]
                # A "restored backup": another database holding the same installation.
                copy = await pg.add_database()
                from app.db import create_database
                from app.schema import ensure_database_schema
                from app.crypto_totp import encrypt_totp_secret

                db = await create_database(f"postgres://{pg.user}:{pg.password}@{pg.host}:{pg.port}/{copy}")
                await ensure_database_schema(db)
                await db.execute("INSERT INTO installation (id, status) VALUES ($1, 'active')", iid)
                await db.execute(
                    "INSERT INTO admin_users (id, username, password_hash, totp_secret_enc, totp_enabled) VALUES ($1, $2, $3, $4, $5)",
                    admin_id, "admin", "unused", encrypt_totp_secret(totp), True,
                )
                await db.execute("INSERT INTO zones (name) VALUES ('from-the-copy')")
                await db.close()

                c = await svc.signed_in(admin_id)
                res = await c.post("/admin/api/settings/database/check", json={"postgres": pg.body(database=copy)})
                assert res.json()["identity"] == "different_copy"
                job = await _edit(svc, c, pg.body(database=copy), totp)
                assert job["code"] == "confirm_copy" and svc.config()["postgres"]["database"] == pg.name
                job = await _edit(svc, c, pg.body(database=copy), totp, confirmCopy=True)
                assert job["state"] == "done" and job["result"]["action"] == "database.restored_copy_selected", job
                assert svc.config()["postgres"]["database"] == copy
                assert await svc.app.state.db.fetchval("SELECT name FROM zones WHERE name = 'from-the-copy'")

    run(go())


@needs_pg
def test_another_installation_is_refused_in_settings(workdir, monkeypatch):
    async def go():
        async with pg_database() as pg:
            async with service(monkeypatch) as svc:
                await setup_postgres(svc, pg)
                admin_id, totp = await svc.add_admin()
                other = await pg.add_database()
                from app.db import create_database
                from app.schema import ensure_database_schema

                db = await create_database(f"postgres://{pg.user}:{pg.password}@{pg.host}:{pg.port}/{other}")
                await ensure_database_schema(db)
                await db.execute("INSERT INTO installation (id, status) VALUES ($1, 'active')", str(uuid.uuid4()))
                await db.close()
                c = await svc.signed_in(admin_id)
                job = await _edit(svc, c, pg.body(database=other), totp, confirmCopy=True)
                assert job["code"] == "other_installation"
                assert svc.config()["postgres"]["database"] == pg.name

    run(go())


@needs_pg
def test_missing_privileges_are_named_and_nothing_changes(workdir, monkeypatch):
    async def go():
        async with pg_database() as pg:
            async with service(monkeypatch) as svc:
                await setup_postgres(svc, pg)
                admin_id, totp = await svc.add_admin()
                c = await svc.signed_in(admin_id)
                password = "mk" + secrets.token_hex(16)
                role = await pg.add_role(password)
                # DML on every table, but not the owner and no CREATE on the schema.
                await pg.sql(f"GRANT USAGE ON SCHEMA public TO {role}; "
                             f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}")
                res = await c.post("/admin/api/settings/database/check",
                                   json={"postgres": pg.body(user=role, password=password)})
                problems = " ".join(res.json()["problems"])
                assert "CREATE privilege" in problems and "owner role" in problems, res.text
                job = await _edit(svc, c, pg.body(user=role, password=password), totp)
                assert job["code"] == "permission_denied"
                # Read-only sessions and row-level security are named too.
                await pg.sql(f"GRANT {pg.user} TO {role}; ALTER ROLE {role} SET default_transaction_read_only = on")
                res = await c.post("/admin/api/settings/database/check",
                                   json={"postgres": pg.body(user=role, password=password)})
                assert res.json()["ok"] is False or "read-only" in " ".join(res.json()["problems"]), res.text
                await pg.sql(f"ALTER ROLE {role} RESET default_transaction_read_only; "
                             "ALTER TABLE tokens ENABLE ROW LEVEL SECURITY; ALTER TABLE tokens FORCE ROW LEVEL SECURITY")
                res = await c.post("/admin/api/settings/database/check",
                                   json={"postgres": pg.body(user=role, password=password)})
                assert "Row-level security" in " ".join(res.json()["problems"]), res.text
                assert svc.config()["postgres"]["user"] == pg.user

    run(go())


@needs_pg
def test_recovery_after_the_password_changed(workdir, monkeypatch, capsys):
    async def go():
        async with pg_database() as pg:
            async with service(monkeypatch) as svc:
                await setup_postgres(svc, pg)
            new_password = "mk" + secrets.token_hex(16)
            await pg.sql(f"ALTER ROLE {pg.user} PASSWORD '{new_password}'")
            async with service(monkeypatch) as svc:
                assert svc.runtime.mode == "recovery" and svc.runtime.failure_code == "auth_failed"
                c = svc.recovery_client()
                status = (await c.get("/admin/api/recovery/status")).json()
                assert status["configured"]["user"] == pg.user and pg.password not in json.dumps(status)
                res = await c.post("/admin/api/recovery/check",
                                   json={"backend": "postgres", "postgres": pg.body(password=new_password)})
                assert res.json()["state"] == "installation"
                res = await c.post("/admin/api/recovery/save",
                                   json={"backend": "postgres", "postgres": pg.body(password=new_password), "confirm": True})
                job = await wait_job(c, f"/admin/api/recovery/jobs/{res.json()['job']['id']}")
                assert job["state"] == "done", job
                assert svc.runtime.mode == "ready"
                assert (await svc.client().get("/health")).status_code == 200
        out = capsys.readouterr().out
        assert pg.password not in out and new_password not in out

    run(go())


@needs_pg
def test_an_emptied_postgres_database_goes_to_recovery(workdir, monkeypatch):
    async def go():
        async with pg_database() as pg:
            async with service(monkeypatch) as svc:
                await setup_postgres(svc, pg)
            await pg.sql(f"DROP SCHEMA public CASCADE; CREATE SCHEMA public AUTHORIZATION {pg.user}")
            async with service(monkeypatch) as svc:
                assert svc.runtime.mode == "recovery" and svc.runtime.failure_code == "database_empty"
                # Nothing was created in the empty database.
                assert await pg.sql("SELECT 1") is not None
                conn = await pg.admin(pg.name)
                try:
                    assert await conn.fetchval("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'") == 0
                finally:
                    await conn.close()

    run(go())


# ── The states an interrupted operation leaves (crash table of the plan) ──

def test_a_setup_interrupted_before_publishing_leaves_nothing_behind(workdir, monkeypatch):
    from app import db_config

    db_config.data_dir().mkdir(parents=True)
    leftover = db_config.data_dir() / "auth.db.setup-0123abcd"
    leftover.write_bytes(b"partial")

    async def go():
        async with service(monkeypatch) as svc:
            assert svc.runtime.mode == "setup"
            assert not leftover.exists() and not (svc.data_dir / "auth.db").exists()

    run(go())


def test_an_adoption_interrupted_after_its_row_keeps_that_id(workdir, monkeypatch):
    from app import db_config

    path = db_config.data_dir() / "auth.db"
    older_sqlite_file(path)
    iid = str(uuid.uuid4())

    async def mark():
        from app import installation
        from app.db import create_database

        db = await create_database(f"sqlite:///{path}")
        await installation.create(db, iid)
        await db.close()

    run(mark())

    async def go():
        async with service(monkeypatch) as svc:
            assert svc.runtime.mode == "ready"
            assert svc.config()["installationId"] == iid

    run(go())


@needs_pg
def test_a_setup_interrupted_after_the_schema_continues_with_the_first_admin(workdir, monkeypatch):
    async def go():
        async with pg_database() as pg:
            from app import installation
            from app.db import create_database
            from app.schema import ensure_database_schema

            db = await create_database(f"postgres://{pg.user}:{pg.password}@{pg.host}:{pg.port}/{pg.name}")
            await ensure_database_schema(db)
            iid = str(uuid.uuid4())
            await installation.create(db, iid)
            await db.close()
            async with service(monkeypatch) as svc:
                job = await setup_postgres(svc, pg)
                assert job["state"] == "done" and job["result"]["next"] == "register"
                assert svc.config()["installationId"] == iid

    run(go())


# ── Review fixes: commit point, start-up lock, retries, background work ──

def test_a_switch_the_disk_did_not_confirm_goes_to_recovery(workdir, monkeypatch):
    from app import db_config

    def broken(_path):
        raise OSError("flush failed")

    monkeypatch.setattr(db_config, "_fsync_dir", broken)
    with pytest.raises(db_config.PublishUncertain):
        db_config.publish_config(db_config.Config(db_config.SQLITE, "new", sqlite_path="data/auth.db"))
    assert db_config.read_config().installation_id == "new"

    async def go():
        async with service(monkeypatch) as svc:
            c = svc.client()
            res = await c.post("/admin/api/setup/database", json={"backend": "sqlite"})
            job = await wait_job(c, f"/admin/api/setup/database/jobs/{res.json()['job']['id']}")
            # The file was replaced but not confirmed: no write is acknowledged on it.
            assert job["state"] == "failed"
            assert svc.runtime.mode == "recovery" and svc.runtime.failure_code == "config_not_durable"
            assert (await c.get("/admin/api/tokens")).status_code == 503
            monkeypatch.undo()
            monkeypatch.setattr(db_config, "DATA_DIR", svc.data_dir)
            from app.config import get_settings

            monkeypatch.setattr(get_settings(), "database_url", None)
            import redis.asyncio as redis_asyncio

            monkeypatch.setattr(redis_asyncio, "from_url", lambda *a, **k: svc.redis)
            r = svc.recovery_client()
            res = await r.post("/admin/api/recovery/save", json={"backend": "sqlite", "confirm": True})
            job = await wait_job(r, f"/admin/api/recovery/jobs/{res.json()['job']['id']}")
            assert job["state"] == "done", job
            assert svc.runtime.mode == "ready"

    (db_config.data_dir() / "database.json").unlink()
    run(go())


def test_an_installation_without_its_admin_table_is_never_recreated(workdir, monkeypatch):
    async def go():
        async with service(monkeypatch) as svc:
            await setup_sqlite(svc)
            await svc.app.state.db.execute("DROP TABLE passkeys")
            await svc.app.state.db.execute("DROP TABLE admin_users")
        async with service(monkeypatch) as svc:
            assert svc.runtime.mode == "recovery" and svc.runtime.failure_code == "incomplete_installation"
            assert (await svc.client().get("/admin/tokens")).headers["location"] == "/recovery"
            c = svc.recovery_client()
            res = await c.post("/admin/api/recovery/save", json={"backend": "sqlite", "confirm": True})
            job = await wait_job(c, f"/admin/api/recovery/jobs/{res.json()['job']['id']}")
            assert job["code"] == "incomplete_installation" and "admin_users" in job["error"]

    run(go())


def test_a_registry_file_that_cannot_be_opened_counts_as_a_live_process(tmp_path, monkeypatch):
    from app import db_runtime
    from app.db_runtime import OperationError, Registry

    mine = Registry()
    assert mine.register(tmp_path)
    (tmp_path / ".proc" / "other.lock").write_text("")
    real_open = db_runtime.os.open

    def guarded(path, *a, **k):
        if str(path).endswith("other.lock"):
            raise PermissionError("not allowed")
        return real_open(path, *a, **k)

    monkeypatch.setattr(db_runtime.os, "open", guarded)
    with pytest.raises(OperationError) as e:
        with mine.exclusive():
            pass
    assert e.value.code == "other_process"
    monkeypatch.setattr(db_runtime.os, "open", real_open)
    mine.close()


def test_a_starting_process_waits_for_a_switch_in_progress(tmp_path, monkeypatch):
    from app import db_runtime
    from app.db_runtime import Registry

    switcher, starter = Registry(), Registry()
    assert switcher.register(tmp_path)

    async def go():
        with switcher.exclusive():
            monkeypatch.setattr(db_runtime, "STARTUP_LOCK_WAIT_SEC", 0.3)
            with pytest.raises(RuntimeError):
                async with starter.starting(tmp_path):
                    pass
            assert not starter.active
        async with starter.starting(tmp_path):
            assert starter.active
        # Registered now: the next switch sees it.
        with pytest.raises(db_runtime.OperationError):
            with switcher.exclusive():
                pass

    try:
        run(go())
    finally:
        switcher.close()
        starter.close()


def test_background_work_restarts_once_after_a_slow_stop(monkeypatch):
    from app import db_runtime

    async def go():
        release = asyncio.Event()
        started: list[int] = []

        async def stubborn():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await release.wait()  # like a statement that must finish first
                raise

        def start_background(_app, _db, _settings_db):
            started.append(1)
            return [asyncio.create_task(stubborn())]

        rt = db_runtime.Runtime(start_background=start_background)
        rt.db, rt.settings_db, rt.mode = object(), object(), db_runtime.READY
        rt.tasks = start_background(None, rt.db, rt.settings_db)
        await asyncio.sleep(0.01)  # running, not just created
        monkeypatch.setattr(db_runtime, "STOP_BACKGROUND_SEC", 0.05)
        with pytest.raises(db_runtime.OperationError) as e:
            await rt._stop_background()
        assert e.value.code == "background_busy"
        rt._resume_background(None)
        await asyncio.sleep(0.05)
        assert len(started) == 1 and rt.tasks == []  # not twice while the old task runs
        release.set()
        await asyncio.sleep(0.05)
        assert len(started) == 2 and len(rt.tasks) == 1
        rt.tasks[0].cancel()
        await asyncio.gather(*rt.tasks, return_exceptions=True)

    run(go())


@needs_pg
def test_the_automatic_retry_does_not_undo_a_recovery_save(workdir, monkeypatch):
    from app import db_config, db_runtime

    async def go():
        async with pg_database() as pg:
            async with service(monkeypatch) as svc:
                await setup_postgres(svc, pg)
                iid = svc.config()["installationId"]
            # The saved server is gone (unreachable): recovery mode with retries.
            cfg = db_config.read_config()
            db_config.publish_config(db_config.Config(db_config.POSTGRES, iid, pg=db_config.PgParams(
                "127.0.0.1", 1, cfg.pg.database, cfg.pg.user, cfg.pg.password)))
            monkeypatch.setattr(db_runtime, "RECOVERY_RETRY_SEC", 0.05)
            async with service(monkeypatch) as svc:
                assert svc.runtime.failure_code == "unreachable"
                c = svc.recovery_client()
                for _ in range(50):
                    res = await c.post("/admin/api/recovery/save",
                                       json={"backend": "postgres", "postgres": pg.body(), "confirm": True})
                    if res.status_code == 202:
                        break
                    assert res.status_code == 409  # a retry holds the job; try again
                    await asyncio.sleep(0.02)
                job = await wait_job(c, f"/admin/api/recovery/jobs/{res.json()['job']['id']}")
                assert job["state"] == "done", job
                await asyncio.sleep(0.3)  # several retry periods
                assert svc.runtime.mode == "ready"
                assert svc.runtime.config.pg.port == pg.port
                assert db_config.read_config().pg.port == pg.port

    run(go())


@needs_pg
def test_concurrent_checks_both_see_the_same_database(workdir, monkeypatch):
    async def go():
        async with pg_database() as pg:
            async with service(monkeypatch) as svc:
                await setup_postgres(svc, pg)
                admin_id, _ = await svc.add_admin()
                c = await svc.signed_in(admin_id)
                answers = await asyncio.gather(*[
                    c.post("/admin/api/settings/database/check", json={"postgres": pg.body()}) for _ in range(4)
                ])
                assert [a.json()["identity"] for a in answers] == ["same"] * 4

    run(go())


def test_a_process_that_cannot_register_does_not_start(tmp_path, monkeypatch):
    from app.db_runtime import Registry

    monkeypatch.setattr(Registry, "register", lambda self, directory: False)

    async def go():
        with pytest.raises(RuntimeError) as e:
            async with Registry().starting(tmp_path):
                pass
        assert "not writable" in str(e.value)

    run(go())


def test_a_configuration_file_that_is_not_utf8_leads_to_recovery(workdir, monkeypatch):
    from app import db_config

    db_config.data_dir().mkdir(parents=True)
    db_config.config_path().write_bytes(b'{"format": 1, "installationId": "\xff\xfe"}')

    async def go():
        async with service(monkeypatch) as svc:
            assert svc.runtime.mode == "recovery" and svc.runtime.failure_code == "config_invalid"

    run(go())


def test_an_installation_missing_any_table_is_incomplete(workdir, monkeypatch):
    async def go():
        async with service(monkeypatch) as svc:
            await setup_sqlite(svc)
            await svc.app.state.db.execute("DROP TABLE api_keys")
        async with service(monkeypatch) as svc:
            assert svc.runtime.failure_code == "incomplete_installation"

    run(go())


@needs_pg
def test_the_saved_password_is_never_sent_to_another_server(workdir, monkeypatch):
    async def go():
        async with pg_database() as pg:
            async with service(monkeypatch) as svc:
                await setup_postgres(svc, pg)
                admin_id, totp = await svc.add_admin()
                c = await svc.signed_in(admin_id)
                body = pg.body(host="attacker.example", password="")
                del body["password"]
                res = await c.post("/admin/api/settings/database/check", json={"postgres": body})
                assert res.status_code == 422 and res.json()["field"] == "password"
                res = await c.post("/admin/api/settings/database/check", json={"postgres": {**body, "port": 5433, "host": pg.host}})
                assert res.status_code == 422 and res.json()["field"] == "password"
                # Same host and port: the saved password is kept.
                same = pg.body()
                del same["password"]
                res = await c.post("/admin/api/settings/database/check", json={"postgres": same})
                assert res.status_code == 200 and res.json()["identity"] == "same", res.text

    run(go())


# ── Round 3: slow or cancelled commit, restart after an unconfirmed write, DATABASE_URL ──

def test_a_write_the_disk_does_not_finish_in_time_ends_in_recovery(workdir, monkeypatch):
    import threading

    from app import db_config, db_runtime

    release = threading.Event()
    real = db_config.publish_config

    def slow(cfg):
        release.wait(5)
        real(cfg)

    monkeypatch.setattr(db_config, "publish_config", slow)
    monkeypatch.setattr(db_runtime, "SWITCH_BUDGET_SEC", 0.5)

    async def go():
        async with service(monkeypatch) as svc:
            c = svc.client()
            res = await c.post("/admin/api/setup/database", json={"backend": "sqlite"})
            job_id = res.json()["job"]["id"]
            for _ in range(100):
                if svc.runtime.mode == "recovery":
                    break
                await asyncio.sleep(0.02)
            # Requests are not held while the write is unconfirmed: they get 503 at once.
            assert svc.runtime.failure_code == "config_not_durable"
            assert (await c.get("/admin/api/tokens")).status_code == 503
            # The job keeps the operation lock until the write ends.
            assert svc.runtime.jobs.get(job_id).state == "running"
            release.set()
            job = await wait_job(c, f"/admin/api/setup/database/jobs/{job_id}")
            assert job["code"] == "attach_failed" and svc.runtime.mode == "recovery"

    try:
        run(go())
    finally:
        release.set()


def test_a_cancelled_switch_finishes_its_commit_first(workdir, monkeypatch):
    import time as time_mod

    from app import db_config

    real = db_config.publish_config

    def slow(cfg):
        time_mod.sleep(0.3)
        real(cfg)

    monkeypatch.setattr(db_config, "publish_config", slow)

    async def go():
        async with service(monkeypatch) as svc:
            c = svc.client()
            res = await c.post("/admin/api/setup/database", json={"backend": "sqlite"})
            job = svc.runtime.jobs.get(res.json()["job"]["id"])
            for _ in range(200):
                if job.step == "saving the configuration":
                    break
                await asyncio.sleep(0.005)
            assert job.step == "saving the configuration"
            tasks = list(svc.runtime.jobs._tasks)
            for _ in range(3):  # cancelled again and again, as at shutdown
                for t in tasks:
                    t.cancel()
                await asyncio.sleep(0.02)
            await asyncio.gather(*tasks, return_exceptions=True)
            # The switch went through: the service uses what the file names, and it is logged.
            assert svc.runtime.mode == "ready"
            assert svc.config()["installationId"] == await svc.app.state.db.fetchval("SELECT id FROM installation")
            assert await svc.app.state.db.fetchval(
                "SELECT count(*) FROM activity_logs WHERE action = 'database.created'"
            ) == 1

    run(go())


def test_after_a_restart_an_unconfirmed_file_must_be_confirmed(workdir, monkeypatch):
    from app import db_config

    async def go():
        async with service(monkeypatch) as svc:
            await setup_sqlite(svc)

        def refuse():
            raise OSError("disk refuses")

        monkeypatch.setattr(db_config, "confirm_durable", refuse)
        async with service(monkeypatch) as svc:
            assert svc.runtime.mode == "recovery" and svc.runtime.failure_code == "config_not_durable"
            assert svc.runtime.config is not None

    run(go())


def test_a_database_url_process_is_visible_to_switches(workdir, monkeypatch):
    from app import db_config
    from app.config import get_settings
    from app.db_runtime import OperationError, Registry

    monkeypatch.setattr(get_settings(), "database_url", f"sqlite:///{workdir / 'env.db'}")

    async def go():
        async with service(monkeypatch):
            other = Registry()
            assert other.register(db_config.data_dir())
            try:
                with pytest.raises(OperationError) as e:
                    with other.exclusive():
                        pass
                assert e.value.code == "other_process"
            finally:
                other.close()

    run(go())


def test_a_database_url_process_that_cannot_register_does_not_start(workdir, monkeypatch):
    from app.config import get_settings
    from app.db_runtime import NotRegistered, Registry

    monkeypatch.setattr(get_settings(), "database_url", f"sqlite:///{workdir / 'env.db'}")
    monkeypatch.setattr(Registry, "register", lambda self, directory: False)

    async def go():
        with pytest.raises(NotRegistered):
            async with service(monkeypatch):
                pass

    run(go())


def test_closing_a_connection_that_hangs_is_bounded(monkeypatch):
    from app import db_runtime

    class Hanging:
        terminated = False

        async def close(self):
            await asyncio.sleep(3600)

        def terminate(self):
            self.terminated = True

    async def go():
        monkeypatch.setattr(db_runtime, "CLOSE_TIMEOUT_SEC", 0.05)
        handle = Hanging()
        await db_runtime.Runtime._close_handles(handle, None, False)
        assert handle.terminated

    run(go())


def test_a_failed_attach_does_not_wait_for_a_stuck_settings_connection(workdir, monkeypatch):
    """After the commit point, recovery mode and the open gate come before closing a
    settings connection that hangs (round 5 of the review)."""
    from app import db_runtime, rate_limit_settings

    async def go():
        async with service(monkeypatch) as svc:
            hang = asyncio.Event()
            real_open = db_runtime.open_settings_database

            class StuckClose:
                is_sqlite, is_pg = True, False

                def __init__(self, inner):
                    self.inner = inner

                def __getattr__(self, name):
                    return getattr(self.inner, name)

                async def close(self):
                    # Like aiosqlite waiting for its worker thread: a cancellation does not end it.
                    while not hang.is_set():
                        try:
                            await hang.wait()
                        except asyncio.CancelledError:
                            continue

            async def stuck_open(db):
                inner, _close = await real_open(db)
                return StuckClose(inner), True

            async def broken_load(*_a, **_k):
                raise RuntimeError("settings unreadable")

            monkeypatch.setattr(db_runtime, "open_settings_database", stuck_open)
            monkeypatch.setattr(rate_limit_settings, "load", broken_load)
            monkeypatch.setattr(db_runtime, "CLOSE_TIMEOUT_SEC", 30)
            c = svc.client()
            try:
                res = await c.post("/admin/api/setup/database", json={"backend": "sqlite"})
                job = await asyncio.wait_for(
                    wait_job(c, f"/admin/api/setup/database/jobs/{res.json()['job']['id']}"), 5
                )
                assert job["code"] == "attach_failed" and svc.runtime.mode == "recovery"
            finally:
                hang.set()

    run(go())


# ── The pending mark of a connection change (a lost answer) ─────

class _HeldChange:
    """A connection change on a SQLite service with the PostgreSQL parts replaced: the
    second-factor check waits until `release` (and fails when `refuse` is set), and the
    job waits until `finish`.  The request's own logic around them is the real one."""

    def __init__(self, monkeypatch):
        from fastapi import HTTPException

        from app import db_operations
        from app.routers import admin_database

        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.finish = asyncio.Event()
        self.refuse = False
        self.checks = 0

        async def held_step_up(*_a, **_k):
            self.checks += 1
            self.entered.set()
            await self.release.wait()
            if self.refuse:
                raise HTTPException(401, "Invalid code")
            return "code"

        async def held_edit(_rt, _app, job, *_a, **_k):
            job.step = "testing"
            await self.finish.wait()
            return {"action": "database.connection_changed"}

        monkeypatch.setattr(admin_database, "_editable", lambda _rt: None)
        monkeypatch.setattr(admin_database, "_params", lambda _body, _saved=None: object())
        monkeypatch.setattr(admin_database, "verify_step_up", held_step_up)
        monkeypatch.setattr(db_operations, "run_edit", held_edit)

    BODY = {"postgres": {"host": "db.example", "database": "auth", "user": "auth"}, "code": "123456"}


async def _signed_in_service(svc: Service):
    await setup_sqlite(svc)
    admin_id, _totp = await svc.add_admin()
    return await svc.signed_in(admin_id)


async def _job_view(c) -> dict | None:
    res = await c.get("/admin/api/settings/database")
    assert res.status_code == 200, res.text
    return res.json()["job"]


def test_a_change_reads_pending_until_its_job_starts(workdir, monkeypatch):
    """The answer of the request is lost while it waits for the second factor: the read
    the page makes then says pending, a second change is refused without clearing the
    first one's mark, and the first one goes on to its running job."""
    held = _HeldChange(monkeypatch)

    async def go():
        async with service(monkeypatch) as svc:
            c = await _signed_in_service(svc)
            first = asyncio.create_task(c.put("/admin/api/settings/database", json=held.BODY))
            await held.entered.wait()
            assert await _job_view(c) == {"state": "pending"}

            second = await c.put("/admin/api/settings/database", json=held.BODY)
            assert second.status_code == 409 and second.json()["code"] == "busy"
            assert second.json()["job"] == {"state": "pending"}
            assert held.checks == 1  # refused before its own second-factor check
            assert await _job_view(c) == {"state": "pending"}

            held.release.set()
            res = await first
            assert res.status_code == 202, res.text
            job = res.json()["job"]
            assert job["state"] == "running" and job["kind"] == "edit"
            assert (await _job_view(c))["id"] == job["id"]

            held.finish.set()
            done = await wait_job(c, f"/admin/api/settings/database/jobs/{job['id']}")
            assert done["state"] == "done"
            assert await _job_view(c) is None

    run(go())


def test_a_change_refused_by_the_second_factor_clears_its_mark(workdir, monkeypatch):
    held = _HeldChange(monkeypatch)

    async def go():
        async with service(monkeypatch) as svc:
            c = await _signed_in_service(svc)
            held.refuse = True
            first = asyncio.create_task(c.put("/admin/api/settings/database", json=held.BODY))
            await held.entered.wait()
            assert await _job_view(c) == {"state": "pending"}
            held.release.set()
            res = await first
            assert res.status_code == 401
            assert await _job_view(c) is None
            # Nothing is left behind: the next change goes through.
            held.refuse = False
            res = await c.put("/admin/api/settings/database", json=held.BODY)
            assert res.status_code == 202, res.text
            held.finish.set()
            await wait_job(c, f"/admin/api/settings/database/jobs/{res.json()['job']['id']}")

    run(go())


@pytest.mark.parametrize("failure", ["busy", "error"])
def test_a_change_that_fails_to_start_its_job_clears_its_mark(workdir, monkeypatch, failure):
    from app.db_runtime import Busy, Job

    held = _HeldChange(monkeypatch)
    held.release.set()

    async def go():
        async with service(monkeypatch) as svc:
            c = await _signed_in_service(svc)

            def broken_start(*_a, **_k):
                if failure == "busy":
                    raise Busy(Job("other", "reconnect"))
                raise RuntimeError("start failed")

            monkeypatch.setattr(svc.runtime.jobs, "start", broken_start)
            if failure == "busy":
                res = await c.put("/admin/api/settings/database", json=held.BODY)
                assert res.status_code == 409 and res.json()["code"] == "busy"
            else:
                with pytest.raises(RuntimeError):
                    await c.put("/admin/api/settings/database", json=held.BODY)
            assert svc.runtime.jobs.busy() is None
            assert await _job_view(c) is None

    run(go())


def test_a_cancelled_change_clears_its_mark(workdir, monkeypatch):
    held = _HeldChange(monkeypatch)

    async def go():
        async with service(monkeypatch) as svc:
            c = await _signed_in_service(svc)
            first = asyncio.create_task(c.put("/admin/api/settings/database", json=held.BODY))
            await held.entered.wait()
            assert svc.runtime.jobs.busy() is not None
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert svc.runtime.jobs.busy() is None
            assert await _job_view(c) is None

    run(go())


def test_only_the_request_that_set_the_mark_clears_it():
    from app.db_runtime import Busy, Jobs

    jobs = Jobs()
    mark = jobs.mark_pending()
    with pytest.raises(Busy) as refused:
        jobs.mark_pending()
    assert refused.value.job.view() == {"state": "pending"}
    other = type(mark)()
    jobs.clear_pending(other)
    assert jobs.busy() is mark
    jobs.clear_pending(mark)
    assert jobs.busy() is None

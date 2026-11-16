"""Date Format and Time Format of the admin pages (app.display_settings,
/admin/api/settings/display, /admin/api/me): one key in the existing table app_state.

- Validation, environment locks (an unknown value locks the field and gives the default).
- Storage: no new table (database inspection still sees an installation), a missing key is
  the defaults, a round trip, compare-and-set saves where only one of two wins, hostile and
  invalid stored values that never stop the start, a write the database refuses.
- Loading on attach: the database now in use decides, also with an older or no version.
- HTTP: full session and CSRF, save, conflict, locked, invalid, /me fields, and the
  addressed Settings sections served as the dashboard page.

Every secret is generated per run with ``secrets``; Redis is faked.  PostgreSQL cases run
when TEST_DATABASE_URL is set (they delete the rows of the tables they use there).
Requires pytest and httpx.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

_TMP_DIR = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)

# Used only when this module is the first to load the settings.
os.environ.setdefault("SESSION_SECRET", secrets.token_urlsafe(48))
os.environ.setdefault("APP_ENCRYPTION_KEY", secrets.token_hex(16))
os.environ.setdefault("REDIS_URL", f"redis://:{secrets.token_hex(32)}@127.0.0.1:6379/0")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP_DIR.name}/display.db")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

PG_URL = os.environ.get("TEST_DATABASE_URL")
BACKENDS = ["sqlite", pytest.param("postgres", marks=pytest.mark.skipif(not PG_URL, reason="TEST_DATABASE_URL is not set"))]
ENV_NAMES = ("DATE_FORMAT", "TIME_FORMAT")


def _ds():
    from app import display_settings

    return display_settings


def use_env(monkeypatch, **env: str) -> None:
    """The display settings see exactly these of ENV_NAMES set (the rest unset)."""
    from app.config import Settings

    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    s = Settings(_env_file=None)
    monkeypatch.setattr(_ds(), "get_settings", lambda: s)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    use_env(monkeypatch)
    _ds().reset_state()
    yield
    _ds().reset_state()


def _run(coro):
    return asyncio.run(coro)


# ── Validation and locks ────────────────────────────────────────

@pytest.mark.parametrize("name,value", [
    ("dateFormat", "dmy"), ("dateFormat", "dd/mm/yyyy"), ("dateFormat", ""), ("dateFormat", None), ("dateFormat", 1),
    ("timeFormat", "24"), ("timeFormat", "12H"), ("timeFormat", " 24h"), ("timeFormat", True),
])
def test_invalid_values_are_refused(name, value):
    ds = _ds()
    with pytest.raises(ds.InvalidSettings) as e:
        ds.check(name, value)
    assert e.value.field == name


def test_every_choice_is_accepted_and_the_defaults_are_browser_and_24_hours():
    ds = _ds()
    for name in ds.FIELDS:
        for value in ds.OPTIONS[name]:
            assert ds.check(name, value) == value
    eff = ds.effective(ds.EMPTY)
    assert (eff.version, eff.date_format, eff.time_format) == (0, "browser", "24h")
    assert ds.locked_fields() == {"dateFormat": False, "timeFormat": False}


def test_environment_locks_and_wins(monkeypatch):
    ds = _ds()
    use_env(monkeypatch, DATE_FORMAT="YYYY-MM-DD")
    stored = ds.Stored(3, {"dateFormat": "DD.MM.YYYY", "timeFormat": "12h"})
    eff = ds.effective(stored)
    assert ds.locked_fields() == {"dateFormat": True, "timeFormat": False}
    assert (eff.version, eff.date_format, eff.time_format) == (3, "YYYY-MM-DD", "12h")


@pytest.mark.parametrize("value", ["dmy", "12-hour", "browser order", "<script>"])
def test_an_unknown_environment_value_locks_the_field_and_gives_the_default(monkeypatch, value):
    ds = _ds()
    use_env(monkeypatch, DATE_FORMAT=value, TIME_FORMAT=value)
    stored = ds.Stored(2, {"dateFormat": "DD.MM.YYYY", "timeFormat": "12h"})
    eff = ds.effective(stored)
    assert ds.locked_fields() == {"dateFormat": True, "timeFormat": True}
    assert ds.env_invalid("dateFormat") and ds.env_invalid("timeFormat")
    assert (eff.date_format, eff.time_format) == ("browser", "24h")


@pytest.mark.parametrize("value", ["", "   "])
def test_an_empty_environment_value_is_not_set(monkeypatch, value):
    ds = _ds()
    use_env(monkeypatch, DATE_FORMAT=value, TIME_FORMAT=value)
    assert ds.locked_fields() == {"dateFormat": False, "timeFormat": False}


def test_apply_never_goes_back():
    ds = _ds()
    assert ds.apply(ds.Effective(2, "DD/MM/YYYY", "12h"))
    assert not ds.apply(ds.Effective(1, "YYYY-MM-DD", "24h"))
    assert ds.current().date_format == "DD/MM/YYYY"


# ── Storage ─────────────────────────────────────────────────────

async def _open(url):
    from app import db as db_mod
    from app.schema import ensure_database_schema

    main = await db_mod.create_database(url)
    await ensure_database_schema(main)
    if main.is_pg:
        for table in ("app_state", "activity_logs"):
            await main.execute(f"DELETE FROM {table}")
    settings_db, own = await db_mod.open_settings_database(main)
    return main, settings_db, own


async def _close(main, settings_db, own):
    if own:
        await settings_db.close()
    await main.close()


def _url(backend, tmp_path, name="store"):
    return f"sqlite:///{tmp_path}/{name}.db" if backend == "sqlite" else PG_URL


async def _put_raw(main, text):
    await main.execute("DELETE FROM app_state WHERE key = $1", _ds().KEY)
    await main.execute("INSERT INTO app_state (key, value) VALUES ($1, $2)", _ds().KEY, text)


async def _events(main):
    rows = await main.fetch(
        "SELECT actor, details FROM activity_logs WHERE action = 'settings.update' AND entity_name = 'display' ORDER BY created_at"
    )
    return [(r["actor"], json.loads(r["details"])) for r in rows]


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_missing_key_gives_the_defaults_and_a_save_round_trips(backend, tmp_path):
    ds = _ds()

    async def scenario():
        main, sdb, own = await _open(_url(backend, tmp_path))
        try:
            stored = await ds.read_current(sdb)
            assert stored == ds.EMPTY
            value = ds.dump_value(1, {"dateFormat": "DD.MM.YYYY", "timeFormat": "12h"}, "admin", "2026-11-16T10:00:00+00:00")
            assert await ds.save(main, stored, value)
            again = await ds.read_current(sdb)
            assert (again.version, again.values, again.actor, again.saved_at) == (
                1, {"dateFormat": "DD.MM.YYYY", "timeFormat": "12h"}, "admin", "2026-11-16T10:00:00+00:00")
            # The same base cannot be saved over twice.
            assert not await ds.save(main, stored, value)
            later = ds.dump_value(2, {"dateFormat": "YYYY-MM-DD", "timeFormat": "24h"}, "admin", "2026-11-16T11:00:00+00:00")
            assert await ds.save(main, again, later)
            assert not await ds.save(main, again, later)
            assert (await ds.read_current(sdb)).version == 2
        finally:
            await _close(main, sdb, own)

    _run(scenario())


@pytest.mark.parametrize("backend", BACKENDS)
def test_two_saves_from_one_version_one_conflict(backend, tmp_path):
    ds = _ds()

    async def scenario():
        main, sdb, own = await _open(_url(backend, tmp_path))
        try:
            for base in (ds.EMPTY, None):
                if base is None:
                    base = await ds.read_current(sdb)
                v = base.version + 1
                results = await asyncio.gather(
                    ds.save(main, base, ds.dump_value(v, {"dateFormat": "DD/MM/YYYY", "timeFormat": "24h"}, "a", "x")),
                    ds.save(main, base, ds.dump_value(v, {"dateFormat": "MM/DD/YYYY", "timeFormat": "12h"}, "b", "y")),
                )
                assert sorted(results) == [False, True]
        finally:
            await _close(main, sdb, own)

    _run(scenario())


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_key_does_not_change_what_the_database_is(backend, tmp_path):
    """No new table: a database holding the key is still this service's installation."""
    from app import db_inspect, installation

    ds = _ds()

    async def scenario():
        main, sdb, own = await _open(_url(backend, tmp_path))
        try:
            if main.is_pg:
                await main.execute("DELETE FROM installation")
            await installation.ensure_row(main)
            value = ds.dump_value(1, {"dateFormat": "DD/MM/YYYY", "timeFormat": "12h"}, "admin", "x")
            assert await ds.save(main, ds.EMPTY, value)
            state = await db_inspect.inspect(main)
            assert state.kind == db_inspect.INSTALLATION, state
        finally:
            await _close(main, sdb, own)

    _run(scenario())


HOSTILE = [
    "nope", "", "[]", "null", '{"format": 2, "version": 1, "dateFormat": "browser", "timeFormat": "24h"}',
    '{"format": 1, "version": 0, "dateFormat": "browser", "timeFormat": "24h"}',
    '{"format": 1, "version": "1", "dateFormat": "browser", "timeFormat": "24h"}',
    '{"format": true, "version": 1, "dateFormat": "browser", "timeFormat": "24h"}',
    '{"format": 1, "version": 1, "dateFormat": "<img src=x onerror=alert(1)>", "timeFormat": "24h"}',
    '{"format": 1, "version": 1, "dateFormat": "browser"}',
    '{"format": 1, "version": 1, "dateFormat": "browser", "timeFormat": ["24h"]}',
    '{"format": 1, "version": 1e400, "dateFormat": "browser", "timeFormat": "24h"}',
]


@pytest.mark.parametrize("text", HOSTILE)
def test_an_unusable_stored_value_gives_the_defaults_and_never_stops_the_start(text, tmp_path, capsys):
    ds = _ds()

    async def scenario():
        main, sdb, own = await _open(_url("sqlite", tmp_path))
        try:
            await _put_raw(main, text)
            stored = await ds.read_current(sdb)
            assert stored.invalid and stored.version == 0 and stored.raw == text
            await ds.load(sdb, main)
            assert (ds.current().date_format, ds.current().time_format) == ("browser", "24h")
            assert "cannot be used" in capsys.readouterr().out
            # A save replaces it.
            value = ds.dump_value(1, {"dateFormat": "DD/MM/YYYY", "timeFormat": "12h"}, "admin", "x")
            assert await ds.save(main, stored, value)
            assert (await ds.read_current(sdb)).values == {"dateFormat": "DD/MM/YYYY", "timeFormat": "12h"}
        finally:
            await _close(main, sdb, own)

    _run(scenario())


def test_a_refused_write_is_a_failed_save_never_bad_stored_data(tmp_path):
    ds = _ds()

    class ReadOnly:
        is_pg, is_sqlite = False, True

        async def fetchval_write(self, *_a):
            import sqlite3

            raise sqlite3.OperationalError("attempt to write a readonly database")

    class Locked(ReadOnly):
        async def fetchval_write(self, *_a):
            import sqlite3

            raise sqlite3.OperationalError("database is locked")

    class NoPrivilege(ReadOnly):
        async def fetchval_write(self, *_a):
            import asyncpg

            raise asyncpg.exceptions.InsufficientPrivilegeError("permission denied for table app_state")

    class Dropped(ReadOnly):
        # The connection failed after the statement was sent: it may have been committed
        async def fetchval_write(self, *_a):
            raise ConnectionError("connection was closed in the middle of operation")

    class PgDropped(ReadOnly):
        async def fetchval_write(self, *_a):
            import asyncpg

            raise asyncpg.exceptions.ConnectionDoesNotExistError("connection was closed")

    async def scenario():
        for db, busy in ((ReadOnly(), False), (Locked(), True), (NoPrivilege(), False)):
            with pytest.raises(ds.WriteRefused) as e:
                await ds.save(db, ds.EMPTY, "{}")
            assert e.value.busy is busy
        for db in (Dropped(), PgDropped()):
            with pytest.raises(ds.WriteUnknown):
                await ds.save(db, ds.EMPTY, "{}")

    _run(scenario())


@pytest.mark.parametrize("backend", BACKENDS)
def test_startup_copies_environment_values_once(backend, tmp_path, monkeypatch):
    ds = _ds()
    use_env(monkeypatch, TIME_FORMAT="12h", DATE_FORMAT="not-a-format")

    async def scenario():
        main, sdb, own = await _open(_url(backend, tmp_path))
        try:
            await ds.load(sdb, main)
            stored = await ds.read_current(sdb)
            # The unknown DATE_FORMAT is not copied.
            assert stored.version == 1 and stored.actor == "environment"
            assert stored.values == {"dateFormat": "browser", "timeFormat": "12h"}
            events = await _events(main)
            assert len(events) == 1 and events[0][0] == "environment" and events[0][1]["to"]["timeFormat"] == "12h"
            assert (ds.current().date_format, ds.current().time_format) == ("browser", "12h")
            await ds.load(sdb, main)
            assert (await ds.read_current(sdb)).version == 1
            # Removed later, the last value stays.
            use_env(monkeypatch)
            await ds.load(sdb, main)
            assert ds.current().time_format == "12h" and (await ds.read_current(sdb)).version == 1
        finally:
            await _close(main, sdb, own)

    _run(scenario())


def test_the_database_now_in_use_decides_after_a_switch(tmp_path):
    """Attaching another database reads its value, also an older version or none."""
    ds = _ds()

    async def scenario():
        a = await _open(_url("sqlite", tmp_path, "a"))
        b = await _open(_url("sqlite", tmp_path, "b"))
        try:
            await _put_raw(a[0], ds.dump_value(5, {"dateFormat": "DD.MM.YYYY", "timeFormat": "12h"}, "admin", "x"))
            await ds.load(a[1], a[0])
            assert (ds.current().version, ds.current().date_format) == (5, "DD.MM.YYYY")
            await ds.load(b[1], b[0])  # no key at all
            assert (ds.current().version, ds.current().date_format, ds.current().time_format) == (0, "browser", "24h")
            await _put_raw(b[0], ds.dump_value(1, {"dateFormat": "MM/DD/YYYY", "timeFormat": "browser"}, "admin", "y"))
            await ds.load(a[1], a[0])
            await ds.load(b[1], b[0])  # an older version than the one in use
            assert (ds.current().version, ds.current().date_format, ds.current().time_format) == (1, "MM/DD/YYYY", "browser")
        finally:
            await _close(*a)
            await _close(*b)

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
    def __init__(self, client, fake) -> None:
        self.client = client
        self.fake = fake
        self.csrf = ""

    def login(self) -> None:
        import app.main as main_mod
        from app.sessions import COOKIE_NAME, session_record

        admin_id = self.client.portal.call(main_mod.app.state.db.fetchval, "SELECT id FROM admin_users WHERE totp_enabled = $1", True)
        if admin_id is None:
            admin_id = str(uuid.uuid4())
            self.client.portal.call(
                main_mod.app.state.db.execute,
                "INSERT INTO admin_users (id, username, password_hash, totp_enabled) VALUES ($1, $2, $3, $4)",
                admin_id, "display-admin", "unused", True,
            )
        sid = secrets.token_hex(32)
        self.csrf = secrets.token_hex(24)
        key, value = session_record(sid, {"kind": "full", "adminId": str(admin_id), "csrf": self.csrf})
        self.fake.data[key] = value
        self.client.cookies.set(COOKIE_NAME, sid)

    def get(self):
        return self.client.get("/admin/api/settings/display")

    def put(self, body, csrf=True):
        headers = {"X-CSRF-Token": self.csrf} if csrf else {}
        return self.client.put("/admin/api/settings/display", json=body, headers=headers)

    def me(self):
        return self.client.get("/admin/api/me").json()

    def call(self, fn, *args):
        return self.client.portal.call(fn, *args)


@pytest.fixture()
def admin(monkeypatch, tmp_path):
    import app.main as main_mod
    from app import db as db_mod

    fake = FakeRedis()
    monkeypatch.setattr(main_mod.redis, "from_url", lambda *a, **k: fake)
    url = f"sqlite:///{tmp_path}/http.db"
    create_database = db_mod.create_database
    monkeypatch.setattr(main_mod, "create_database", lambda _url: create_database(url))

    async def _no_cleanup(*_a, **_k):
        return None

    monkeypatch.setattr(main_mod, "cleanup_expired_tokens", _no_cleanup)
    with TestClient(main_mod.app) as c:
        yield Admin(c, fake)


def test_needs_a_full_session_and_csrf(admin):
    assert admin.get().status_code == 401
    assert admin.put({"baseVersion": 0, "dateFormat": "DD/MM/YYYY"}).status_code == 401
    admin.login()
    assert admin.put({"baseVersion": 0, "dateFormat": "DD/MM/YYYY"}, csrf=False).status_code == 403
    assert admin.get().json()["version"] == 0


def test_view_save_and_me(admin):
    import app.main as main_mod

    ds = _ds()
    admin.login()
    view = admin.get().json()
    assert view["version"] == 0 and view["savedBy"] is None and view["storedInvalid"] is False
    assert view["fields"]["dateFormat"] == {
        "value": "browser", "source": "default", "locked": False, "envName": "DATE_FORMAT", "envInvalid": False,
        "default": "browser", "options": list(ds.DATE_FORMATS),
    }
    assert view["fields"]["timeFormat"]["value"] == "24h" and view["fields"]["timeFormat"]["options"] == ["24h", "12h", "browser"]
    me = admin.me()
    assert (me["dateFormat"], me["timeFormat"]) == ("browser", "24h")

    res = admin.put({"baseVersion": 0, "dateFormat": "DD.MM.YYYY"})
    assert res.status_code == 200, res.text
    view = res.json()
    assert view["version"] == 1 and view["savedBy"] == "display-admin" and view["savedAt"]
    assert view["fields"]["dateFormat"]["value"] == "DD.MM.YYYY" and view["fields"]["dateFormat"]["source"] == "stored"
    assert ds.current().version == 1
    me = admin.me()
    assert (me["dateFormat"], me["timeFormat"]) == ("DD.MM.YYYY", "24h")

    rows = admin.call(main_mod.app.state.db.fetch,
                      "SELECT details FROM activity_logs WHERE action = 'settings.update' AND entity_name = 'display'")
    assert len(rows) == 1
    details = json.loads(rows[0]["details"])
    assert details["from"] == {"dateFormat": "browser", "timeFormat": "24h"}
    assert details["to"] == {"dateFormat": "DD.MM.YYYY", "timeFormat": "24h"}

    # The same values again write nothing.
    assert admin.put({"baseVersion": 1, "dateFormat": "DD.MM.YYYY"}).json()["version"] == 1


def test_a_stale_version_and_a_lost_race_are_conflicts(admin):
    import app.main as main_mod

    ds = _ds()
    admin.login()
    assert admin.put({"baseVersion": 0, "timeFormat": "12h"}).status_code == 200
    res = admin.put({"baseVersion": 0, "dateFormat": "YYYY-MM-DD"})
    assert res.status_code == 409 and res.json()["code"] == "conflict"
    assert res.json()["current"]["fields"]["timeFormat"]["value"] == "12h"

    # Another process saves between this request's read and its write.
    real_save = ds.save

    async def raced(write_db, base, value):
        other = ds.dump_value(base.version + 1, {"dateFormat": "MM/DD/YYYY", "timeFormat": "24h"}, "other", "x")
        assert await real_save(write_db, base, other)
        return await real_save(write_db, base, value)

    ds.save = raced
    try:
        res = admin.put({"baseVersion": 1, "dateFormat": "YYYY-MM-DD"})
    finally:
        ds.save = real_save
    assert res.status_code == 409 and res.json()["code"] == "conflict"
    assert res.json()["current"]["fields"]["dateFormat"]["value"] == "MM/DD/YYYY"
    stored = admin.call(ds.read_current, main_mod.app.state.settings_db)
    assert stored.values["dateFormat"] == "MM/DD/YYYY"


def test_invalid_input_and_locked_fields_are_refused(admin, monkeypatch):
    admin.login()
    for body, field in [
        ({"baseVersion": 0, "dateFormat": "dmy"}, "dateFormat"),
        ({"baseVersion": 0, "timeFormat": "24"}, "timeFormat"),
        ({"baseVersion": 0, "dateFormat": None}, "dateFormat"),
    ]:
        res = admin.put(body)
        assert res.status_code == 422 and res.json()["field"] == field, body
    for body in ({"baseVersion": 0, "dateFormat": 1}, {"baseVersion": 0, "x": 1}, {}):
        assert admin.put(body).status_code == 422, body

    use_env(monkeypatch, DATE_FORMAT="YYYY-MM-DD", TIME_FORMAT="13h")
    view = admin.get().json()
    assert view["fields"]["dateFormat"]["locked"] and view["fields"]["dateFormat"]["value"] == "YYYY-MM-DD"
    assert view["fields"]["timeFormat"]["locked"] and view["fields"]["timeFormat"]["envInvalid"]
    assert view["fields"]["timeFormat"]["value"] == "24h"
    res = admin.put({"baseVersion": 0, "dateFormat": "DD/MM/YYYY"})
    assert res.status_code == 409 and res.json()["code"] == "locked" and res.json()["field"] == "dateFormat"
    assert admin.get().json()["version"] == 0


def test_a_write_the_database_refuses_is_reported_and_saves_nothing(admin, monkeypatch):
    ds = _ds()
    admin.login()

    async def refused(*_a, **_k):
        raise ds.WriteRefused(busy=False)

    real_save = ds.save
    ds.save = refused
    try:
        res = admin.put({"baseVersion": 0, "dateFormat": "DD/MM/YYYY"})
    finally:
        ds.save = real_save
    assert res.status_code == 503 and res.json()["code"] == "write_refused"
    assert admin.get().json()["version"] == 0
    assert ds.current().date_format == "browser"


def test_a_write_the_database_did_not_confirm_is_reported_as_unknown(admin, monkeypatch):
    ds = _ds()
    admin.login()

    async def unconfirmed(*_a, **_k):
        raise ds.WriteUnknown()

    real_save = ds.save
    ds.save = unconfirmed
    try:
        res = admin.put({"baseVersion": 0, "dateFormat": "DD/MM/YYYY"})
    finally:
        ds.save = real_save
    assert res.status_code == 503 and res.json()["code"] == "write_unknown"


def test_the_start_reads_the_stored_value(monkeypatch, tmp_path):
    """The service loads the formats of its database when it attaches it."""
    import app.main as main_mod
    from app import db as db_mod

    ds = _ds()

    async def prepare():
        main, sdb, own = await _open(f"sqlite:///{tmp_path}/start.db")
        await _put_raw(main, ds.dump_value(4, {"dateFormat": "YYYY-MM-DD", "timeFormat": "12h"}, "admin", "x"))
        await _close(main, sdb, own)

    _run(prepare())
    fake = FakeRedis()
    monkeypatch.setattr(main_mod.redis, "from_url", lambda *a, **k: fake)
    create_database = db_mod.create_database
    monkeypatch.setattr(main_mod, "create_database", lambda _url: create_database(f"sqlite:///{tmp_path}/start.db"))

    async def _no_cleanup(*_a, **_k):
        return None

    monkeypatch.setattr(main_mod, "cleanup_expired_tokens", _no_cleanup)
    with TestClient(main_mod.app) as c:
        a = Admin(c, fake)
        a.login()
        me = a.me()
        assert (me["dateFormat"], me["timeFormat"]) == ("YYYY-MM-DD", "12h")


@pytest.mark.parametrize("section", ["general", "rate-limit", "database", "redis", "passkeys", "unknown_one"])
def test_settings_sections_are_the_dashboard_page(admin, section):
    res = admin.client.get(f"/admin/settings/{section}", follow_redirects=False)
    assert res.status_code == 200 and "text/html" in res.headers["content-type"]
    assert res.text == admin.client.get("/admin/settings").text


@pytest.mark.parametrize("section", ["General", "a.b", "x" * 65, "a b"])
def test_other_settings_addresses_are_not_found(admin, section):
    assert admin.client.get(f"/admin/settings/{section}", follow_redirects=False).status_code == 404

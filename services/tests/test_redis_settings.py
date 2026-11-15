"""The Redis connection on the admin Settings page (app.redis_settings,
/admin/api/settings/redis).

- Masking: the password never shows, in the user info, the query string, percent-encoded,
  or in a unix:// URL.
- Precedence at startup: REDIS_URL > saved URL > REDIS_DEFAULT_URL > refusing to start;
  REDIS_URL overrides a saved value that cannot be read; the client is built once.
- The check: PING, then SETEX, GET and DEL on each key prefix the service uses, bounded
  in time whatever the URL says; failures are reported by category only.
- HTTP: session and CSRF before anything (the check never runs without them), locked,
  confirmation, conflict, nothing saved when the check fails, and the password never in a
  response body (422 bodies included), the activity log, or the process output.

Tests marked "real Redis" run against the Redis server in TEST_REDIS_URL (an admin URL
with a password generated for the run, of a throwaway server); they create ACL users with
generated passwords and delete them afterwards.  Every other secret is generated per run
with ``secrets``.  Requires pytest and httpx.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit

import pytest

_TMP_DIR = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
os.environ.setdefault("SESSION_SECRET", secrets.token_urlsafe(48))
os.environ.setdefault("APP_ENCRYPTION_KEY", secrets.token_hex(16))
os.environ.setdefault("REDIS_URL", f"redis://:{secrets.token_hex(32)}@127.0.0.1:6379/0")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP_DIR.name}/redis.db")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

REDIS_URL = os.environ.get("TEST_REDIS_URL")
real_redis = pytest.mark.skipif(not REDIS_URL, reason="TEST_REDIS_URL is not set")


def _rs():
    from app import redis_settings

    return redis_settings


def use_env(monkeypatch, url: str | None = None, default: str | None = None) -> None:
    """The Redis settings see exactly these REDIS_URL and REDIS_DEFAULT_URL values."""
    from app.config import Settings

    for name, value in (("REDIS_URL", url), ("REDIS_DEFAULT_URL", default)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    s = Settings(_env_file=None)
    monkeypatch.setattr(_rs(), "get_settings", lambda: s)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    _rs().reset_state()
    yield
    _rs().reset_state()


def _pw() -> str:
    return secrets.token_hex(32)


def _encodings(pw: str) -> list[str]:
    return [pw, quote(pw, safe=""), quote(quote(pw, safe=""), safe="")]


# ── Masking ─────────────────────────────────────────────────────

@pytest.mark.parametrize("template,expected", [
    ("redis://app:{p}@redis.local:6380/2", "redis://app:****@redis.local:6380/2"),
    ("redis://:{p}@redis", "redis://:****@redis:6379/0"),
    ("redis://redis:6379/0?password={p}", "redis://:****@redis:6379/0"),
    ("redis://redis/3?socket_timeout=5&password={p}", "redis://:****@redis:6379/3"),
    ("rediss://app:{p}@[::1]:6380", "rediss://app:****@[::1]:6380/0"),
    ("unix:///run/redis.sock?password={p}&db=3", "unix://:****@/run/redis.sock?db=3"),
    ("unix://app:{p}@/run/redis.sock", "unix://app:****@/run/redis.sock"),
])
def test_masked_urls_never_hold_the_password(template, expected):
    pw = _pw()
    masked = _rs().mask_url(template.format(p=pw))
    assert masked == expected
    assert pw not in masked


def test_percent_encoded_password_is_masked():
    pw = "p@ss/" + _pw() + ":x"
    for url in (f"redis://app:{quote(pw, safe='')}@redis:6379/0", f"redis://redis?password={quote(pw, safe='')}"):
        masked = _rs().mask_url(url)
        assert "****" in masked and "@redis:6379/0" in masked
        _no_secret(pw, masked)
        assert "p@ss" not in masked and "p%40ss" not in masked


def test_unreadable_url_is_not_echoed():
    pw = _pw()
    assert _rs().mask_url(f"http://:{pw}@redis") == "(not a valid Redis URL)"
    assert _rs().mask_url(None) is None


# ── Precedence and startup ──────────────────────────────────────

def _stored(url: str | None, version: int = 1):
    rs = _rs()
    return rs.Stored(version, None if url is None else rs.encrypt_url(url))


def test_environment_wins_over_saved_and_default(monkeypatch):
    rs = _rs()
    env, saved, default = (f"redis://:{_pw()}@{h}:6379/0" for h in ("env", "saved", "default"))
    use_env(monkeypatch, url=env, default=default)
    assert rs.resolve(_stored(saved)) == rs.Connection(env, "env")
    use_env(monkeypatch, default=default)
    assert rs.resolve(_stored(saved)) == rs.Connection(saved, "stored")
    assert rs.resolve(rs.EMPTY) == rs.Connection(default, "default")
    assert rs.resolve(_stored(None)) == rs.Connection(default, "default")


@pytest.mark.parametrize("value", [None, "", "   "])
def test_no_url_anywhere_stops_the_start(monkeypatch, value):
    rs = _rs()
    if value is None:
        use_env(monkeypatch)
    else:
        use_env(monkeypatch, url=value, default=value)
    with pytest.raises(RuntimeError, match="REDIS_URL is not set"):
        rs.resolve(rs.EMPTY)


def test_unreadable_saved_value_stops_the_start_unless_redis_url_is_set(monkeypatch):
    rs = _rs()
    pw = _pw()
    bad = rs.Stored(4, secrets.token_hex(40))  # not encrypted with this deployment's keys
    use_env(monkeypatch, default=f"redis://:{pw}@default:6379")
    with pytest.raises(RuntimeError) as e:
        rs.resolve(bad)
    assert "cannot be read" in str(e.value) and "REDIS_URL" in str(e.value) and e.value.__cause__ is None
    assert rs.saved_url(bad) is None
    garbled = rs.Stored(5, None, invalid=True)
    with pytest.raises(RuntimeError, match="not in format 1"):
        rs.resolve(garbled)
    env = f"redis://:{_pw()}@env:6379"
    use_env(monkeypatch, url=env)
    assert rs.resolve(bad).url == env and rs.resolve(garbled).url == env


@pytest.mark.parametrize("template", ["redis://:changeme@redis:6379", "redis://redis:6379", "http://:{p}@redis"])
def test_saved_url_passes_the_same_checks(monkeypatch, template):
    rs = _rs()
    pw = _pw()
    use_env(monkeypatch)
    with pytest.raises(RuntimeError) as e:
        rs.resolve(_stored(template.format(p=pw)))
    assert "saved on the Settings page" in str(e.value) and pw not in str(e.value)


def test_category_comes_from_the_exception_class():
    from redis import exceptions as rex

    rs = _rs()
    assert rs._category(rex.AuthenticationError("x")) == "auth"
    assert rs._category(rex.NoPermissionError("x")) == "no_write"
    assert rs._category(rex.ReadOnlyError("x")) == "no_write"
    assert rs._category(rex.ConnectionError("x")) == "unreachable"
    assert rs._category(rex.TimeoutError("x")) == "unreachable"
    assert rs._category(TimeoutError()) == "unreachable"
    assert rs._category(ConnectionRefusedError()) == "unreachable"
    assert rs._category(rex.ResponseError("x")) == "other"
    assert rs._category(ValueError("x")) == "other"


def test_unreachable_server():
    assert asyncio.run(_rs().check(f"redis://:{_pw()}@127.0.0.1:1/0")) == "unreachable"


def test_check_is_bounded_even_when_the_url_asks_for_longer():
    rs = _rs()

    async def scenario():
        held = []

        async def never_answer(reader, writer):
            held.append(writer)
            await asyncio.Event().wait()

        server = await asyncio.start_server(never_answer, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            started = time.monotonic()
            category = await rs.check(f"redis://:{_pw()}@127.0.0.1:{port}/0?socket_timeout=600&socket_connect_timeout=600")
            return category, time.monotonic() - started
        finally:
            for w in held:
                w.close()
            server.close()

    category, elapsed = asyncio.run(scenario())
    assert category == "unreachable"
    assert elapsed < rs.CHECK_TIMEOUT_SEC + 1.5


# ── HTTP with a fake Redis (no server needed) ───────────────────

class _FakePipeline:
    """Queues commands and runs them in order on execute(), like a MULTI/EXEC pipeline."""

    def __init__(self, redis) -> None:
        self.redis = redis
        self.ops: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def __getattr__(self, name):
        return lambda *a, **k: self.ops.append((name, a, k))

    async def execute(self, raise_on_error=True):
        return [await getattr(self.redis, name)(*a, **k) for name, a, k in self.ops]


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def get(self, k):
        return self.data.get(k)

    async def setex(self, k, ttl, v):
        self.data[k] = v
        self.ttls[k] = ttl

    async def set(self, k, v, nx=False, ex=None):
        if nx and k in self.data:
            return None
        self.data[k] = str(v)
        if ex is not None:
            self.ttls[k] = ex
        return True

    async def incr(self, k):
        self.data[k] = str(int(self.data.get(k, 0)) + 1)
        return int(self.data[k])

    async def ttl(self, k):
        return self.ttls.get(k, -1) if k in self.data else -2

    async def expire(self, k, seconds):
        self.ttls[k] = seconds
        return k in self.data

    def pipeline(self, transaction=True):
        return _FakePipeline(self)

    async def delete(self, *keys):
        for k in keys:
            self.data.pop(k, None)
            self.ttls.pop(k, None)

    async def ping(self):
        return True

    async def aclose(self):
        pass


class Admin:
    def __init__(self, client, put_redis) -> None:
        self.client = client
        self._put_redis = put_redis
        self.csrf = ""

    def login(self) -> None:
        import app.main as main_mod
        from app.sessions import COOKIE_NAME, session_record

        # A full session counts only for an existing admin with MFA enabled (one per database).
        admin_id = self.client.portal.call(main_mod.app.state.db.fetchval, "SELECT id FROM admin_users WHERE totp_enabled = $1", True)
        if admin_id is None:
            admin_id = str(uuid.uuid4())
            self.client.portal.call(
                main_mod.app.state.db.execute,
                "INSERT INTO admin_users (id, username, password_hash, totp_enabled) VALUES ($1, $2, $3, $4)",
                admin_id, "settings-admin", "unused", True,
            )
        sid = secrets.token_hex(32)
        self.csrf = secrets.token_hex(24)
        self._put_redis(*session_record(sid, {"kind": "full", "adminId": str(admin_id), "csrf": self.csrf}))
        self.client.cookies.set(COOKIE_NAME, sid)

    def get(self):
        return self.client.get("/admin/api/settings/redis")

    def put(self, body, csrf=True, raw: bytes | None = None):
        headers = {"X-Forwarded-For": "10.9.9.9"}
        if csrf:
            headers["X-CSRF-Token"] = self.csrf
        if raw is not None:
            return self.client.put("/admin/api/settings/redis", content=raw, headers={**headers, "Content-Type": "application/json"})
        return self.client.put("/admin/api/settings/redis", json=body, headers=headers)

    def db(self, sql, *args):
        import app.main as main_mod

        return self.client.portal.call(main_mod.app.state.db.fetch, sql, *args)

    def call(self, fn, *args):
        return self.client.portal.call(fn, *args)


class App:
    """Starts and stops the app on one database, as separate processes would."""

    def __init__(self, monkeypatch, tmp_path, fake: FakeRedis | None) -> None:
        import app.main as main_mod

        self.main = main_mod
        url = f"sqlite:///{tmp_path}/app.db"
        create_database = main_mod.create_database
        monkeypatch.setattr(main_mod, "create_database", lambda _url: create_database(url))

        async def _no_cleanup(*_a, **_k):
            return None

        monkeypatch.setattr(main_mod, "cleanup_expired_tokens", _no_cleanup)
        if fake is not None:
            monkeypatch.setattr(main_mod.redis, "from_url", lambda *a, **k: fake)
        self.fake = fake
        self.tc = None

    def start(self) -> Admin:
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        tc = TestClient(ProxyHeadersMiddleware(self.main.app, trusted_hosts="*"))
        c = tc.__enter__()
        self.tc = tc

        def put_redis(key, value):
            if self.fake is not None:
                self.fake.data[key] = value
            else:
                c.portal.call(self.main.app.state.redis.setex, key, 600, value)

        return Admin(c, put_redis)

    def stop(self) -> None:
        if self.tc is not None:
            self.tc.__exit__(None, None, None)
            self.tc = None


@pytest.fixture()
def fake_app(monkeypatch, tmp_path):
    a = App(monkeypatch, tmp_path, FakeRedis())
    yield a
    a.stop()


def _no_secret(pw: str, *texts: str) -> None:
    for text in texts:
        for e in _encodings(pw):
            assert e not in text


def test_check_never_runs_without_a_session_and_csrf(fake_app, monkeypatch):
    rs = _rs()
    use_env(monkeypatch, default=f"redis://:{_pw()}@default:6379")
    calls = []

    async def counting(url):
        calls.append(url)
        return None

    monkeypatch.setattr(rs, "check", counting)
    admin = fake_app.start()
    body = {"baseVersion": 0, "url": f"redis://:{_pw()}@other:6379", "confirmRestart": True}
    assert admin.put(body).status_code == 401
    assert admin.get().status_code == 401
    admin.login()
    assert admin.put(body, csrf=False).status_code == 403
    assert calls == []
    assert admin.put(body).status_code == 200 and len(calls) == 1


def test_save_flow_with_confirmation_and_masks(fake_app, monkeypatch, capsys, caplog):
    rs = _rs()
    caplog.set_level(logging.DEBUG)
    default_pw, new_pw = _pw(), _pw()
    default = f"redis://:{default_pw}@default:6379/0"
    new = f"redis://app:{new_pw}@other:6380/1"
    use_env(monkeypatch, default=default)
    admin = fake_app.start()
    admin.login()

    view = admin.get().json()
    assert view["version"] == 0 and view["restartNeeded"] is False
    assert view["url"] == {
        "connected": "redis://:****@default:6379/0", "saved": None, "source": "default", "locked": False,
        "envName": "REDIS_URL", "syncPending": False,
    }
    res = admin.put({"baseVersion": 0, "url": new})
    assert res.status_code == 409 and res.json()["code"] == "confirm_restart"
    assert "restart" in res.json()["detail"] and "signs in again" in res.json()["detail"]
    assert admin.get().json()["version"] == 0

    res = admin.put({"baseVersion": 0, "url": new, "confirmRestart": True})
    assert res.status_code == 200, res.text
    view = res.json()
    assert view["version"] == 1 and view["savedFrom"] == "10.9.9.9"
    assert view["url"]["saved"] == "redis://app:****@other:6380/1" and view["url"]["source"] == "stored"
    assert view["url"]["connected"] == "redis://:****@default:6379/0" and view["restartNeeded"] is True

    # The same URL again saves nothing and asks nothing.
    assert admin.put({"baseVersion": 1, "url": new}).json()["version"] == 1
    # A stale version is a conflict.
    res = admin.put({"baseVersion": 0, "url": default, "confirmRestart": True})
    assert res.status_code == 409 and res.json()["code"] == "conflict"
    assert res.json()["current"]["url"]["saved"] == "redis://app:****@other:6380/1"

    rows = admin.db("SELECT value FROM redis_settings")
    events = admin.db("SELECT details FROM activity_logs WHERE action = 'settings.update' AND entity_name = 'redis'")
    assert len(events) == 1
    assert json.loads(events[0]["details"]) == {"version": 1, "from": None, "to": "redis://app:****@other:6380/1"}
    out = capsys.readouterr()
    for pw in (new_pw, default_pw):
        _no_secret(pw, admin.get().text, res.text, json.dumps(rows), json.dumps(events), out.out, out.err, caplog.text)
    assert rs.decrypt(rs.Stored(1, json.loads(rows[0]["value"])["urlEnc"])) == new


@pytest.mark.parametrize("make", [
    lambda pw: {"baseVersion": 0},
    lambda pw: {"url": f"redis://:{pw}@x:6379"},
    lambda pw: {"baseVersion": "0", "url": f"redis://:{pw}@x:6379"},
    lambda pw: {"baseVersion": 0, "url": 5, "confirmRestart": True},
    lambda pw: {"baseVersion": 0, "url": f"redis://:{pw}@x:6379", "confirmRestart": "yes"},
    lambda pw: {"baseVersion": 0, "url": f"redis://:{pw}@x:6379", f"redis://:{pw}@x:6379": 1},
    lambda pw: {"baseVersion": 0, "url": f"redis://:{pw}@x:6379", "extra": f"redis://:{pw}@x:6379"},
    lambda pw: [{"baseVersion": 0, "url": f"redis://:{pw}@x:6379"}],
    lambda pw: f"redis://:{pw}@x:6379",
    lambda pw: {"baseVersion": -1, "url": f"redis://:{pw}@x:6379"},
    lambda pw: {"baseVersion": 0, "url": f"http://:{pw}@x:6379"},
    lambda pw: {"baseVersion": 0, "url": f"redis://{pw}@x:6379"},
    lambda pw: {"baseVersion": 0, "url": "redis://:changeme@x:6379"},
    lambda pw: {"baseVersion": 0, "url": "x" * 5000 + pw},
])
def test_invalid_bodies_never_echo_the_password(fake_app, monkeypatch, make):
    use_env(monkeypatch, default=f"redis://:{_pw()}@default:6379")
    admin = fake_app.start()
    admin.login()
    pw = _pw()
    res = admin.put(make(pw))
    assert res.status_code == 422, res.text
    assert res.json()["code"] == "invalid" and res.json()["field"] in ("baseVersion", "url", "confirmRestart", "body")
    _no_secret(pw, res.text)
    assert admin.get().json()["version"] == 0


@pytest.mark.parametrize("raw", [b'{"baseVersion": 0, "url": "redis://:SECRET@x:6379"', b"not json SECRET", b'{"baseVersion": 0, "url": "redis://:SECRET\\ud800@x"}'])
def test_unreadable_bodies_never_echo_the_password(fake_app, monkeypatch, raw):
    use_env(monkeypatch, default=f"redis://:{_pw()}@default:6379")
    admin = fake_app.start()
    admin.login()
    pw = _pw()
    res = admin.put(None, raw=raw.replace(b"SECRET", pw.encode()))
    assert res.status_code == 422 and res.json()["code"] == "invalid"
    _no_secret(pw, res.text)


@pytest.mark.parametrize("streamed", [False, True])
def test_oversized_body_is_refused_before_the_check(fake_app, monkeypatch, streamed):
    """This route reads its body itself, and the 64 KiB request body limit (main.py) still
    applies: 413 with or without Content-Length, no check, nothing saved, nothing echoed."""
    rs = _rs()
    use_env(monkeypatch, default=f"redis://:{_pw()}@default:6379")
    calls = []

    async def counting(url):
        calls.append(url)
        return None

    monkeypatch.setattr(rs, "check", counting)
    admin = fake_app.start()
    admin.login()
    pw = _pw()
    raw = json.dumps({"baseVersion": 0, "url": f"redis://:{pw}@x:6379", "confirmRestart": True, "pad": "x" * 65536}).encode()
    headers = {"X-CSRF-Token": admin.csrf, "Content-Type": "application/json"}
    content = iter([raw[:32768], raw[32768:]]) if streamed else raw
    res = admin.client.put("/admin/api/settings/redis", content=content, headers=headers)
    assert ("content-length" in res.request.headers) is not streamed
    assert res.status_code == 413
    assert calls == []
    assert admin.get().json()["version"] == 0
    _no_secret(pw, res.text)


def test_environment_locks_the_field_and_is_masked(fake_app, monkeypatch):
    env_pw = _pw()
    use_env(monkeypatch, url=f"redis://app:{env_pw}@env:6379/0")
    admin = fake_app.start()
    admin.login()
    res = admin.get()
    view = res.json()
    assert view["url"]["locked"] is True and view["url"]["source"] == "env"
    assert view["url"]["connected"] == "redis://app:****@env:6379/0"
    # The startup copy saved it (its check passed against the fake Redis).
    assert view["url"]["saved"] == "redis://app:****@env:6379/0" and view["url"]["syncPending"] is False
    put = admin.put({"baseVersion": view["version"], "url": f"redis://:{_pw()}@other:6379", "confirmRestart": True})
    assert put.status_code == 409 and put.json()["code"] == "locked" and put.json()["field"] == "url"
    _no_secret(env_pw, res.text, put.text)


def test_environment_overrides_an_unreadable_saved_value(fake_app, monkeypatch, capsys):
    rs = _rs()
    default_pw, env_pw = _pw(), _pw()
    use_env(monkeypatch, default=f"redis://:{default_pw}@default:6379/0")
    admin = fake_app.start()
    admin.login()
    # Written by a process with other keys: this one cannot read it.
    import app.main as main_mod

    admin.call(main_mod.app.state.settings_db.execute,
               "INSERT INTO redis_settings (version, value, actor) VALUES (1, $1, 'other')", rs.dump_value(secrets.token_hex(40)))
    view = admin.get().json()
    assert view["url"]["saved"] is None
    res = admin.put({"baseVersion": 0, "url": f"redis://:{_pw()}@other:6379", "confirmRestart": True})
    assert res.status_code == 409 and res.json()["code"] == "conflict" and res.json()["current"]["url"]["saved"] is None
    fake_app.stop()

    # Without REDIS_URL the start stops and says how to recover; with it, it starts.
    with pytest.raises(RuntimeError, match="Set REDIS_URL"):
        fake_app.start()
    fake_app.stop()
    use_env(monkeypatch, url=f"redis://:{env_pw}@env:6379/0")
    admin = fake_app.start()
    admin.login()
    view = admin.get().json()
    assert view["url"]["connected"] == "redis://:****@env:6379/0" and view["url"]["source"] == "env"
    # The environment value was copied over the unreadable one.
    assert view["version"] == 2 and view["url"]["saved"] == "redis://:****@env:6379/0"
    events = admin.db("SELECT details FROM activity_logs WHERE action = 'settings.update' AND entity_name = 'redis'")
    assert json.loads(events[-1]["details"]) == {"version": 2, "from": None, "to": "redis://:****@env:6379/0"}
    out = capsys.readouterr()
    _no_secret(env_pw, json.dumps(view), json.dumps(events), out.out, out.err)
    _no_secret(default_pw, json.dumps(view), out.out, out.err)


def test_startup_copy_waits_while_the_check_fails(fake_app, monkeypatch):
    rs = _rs()
    use_env(monkeypatch, url=f"redis://:{_pw()}@env:6379/0")
    results = ["unreachable"]

    async def failing_until_up(url):
        return results[-1]

    monkeypatch.setattr(rs, "check", failing_until_up)
    admin = fake_app.start()
    admin.login()
    view = admin.get().json()
    assert view["url"]["syncPending"] is True and view["url"]["saved"] is None and view["version"] == 0
    import app.main as main_mod

    sdb = main_mod.app.state.settings_db
    admin.call(rs.retry_sync, sdb, main_mod.app.state.db)
    assert admin.get().json()["url"]["syncPending"] is True
    results.append(None)  # Redis is up
    admin.call(rs.retry_sync, sdb, main_mod.app.state.db)
    view = admin.get().json()
    assert view["url"]["syncPending"] is False and view["version"] == 1 and view["url"]["saved"] == "redis://:****@env:6379/0"


def test_failed_check_saves_nothing(fake_app, monkeypatch):
    rs = _rs()
    use_env(monkeypatch, default=f"redis://:{_pw()}@default:6379/0")

    async def denied(url):
        return "no_write"

    monkeypatch.setattr(rs, "check", denied)
    admin = fake_app.start()
    admin.login()
    res = admin.put({"baseVersion": 0, "url": f"redis://:{_pw()}@other:6379", "confirmRestart": True})
    assert res.status_code == 422 and res.json()["code"] == "redis_check_failed" and res.json()["category"] == "no_write"
    assert admin.get().json()["version"] == 0


def test_two_saves_from_one_version_one_conflict(fake_app, monkeypatch):
    rs = _rs()
    use_env(monkeypatch, default=f"redis://:{_pw()}@default:6379/0")
    admin = fake_app.start()
    import app.main as main_mod

    sdb = main_mod.app.state.settings_db

    async def both():
        return await asyncio.gather(*(
            rs.insert_version(sdb, 1, rs.dump_value(rs.encrypt_url(f"redis://:{_pw()}@h{n}:6379")), "a", None) for n in (1, 2)
        ))

    assert sorted(admin.call(both)) == [False, True]


# ── Real Redis ──────────────────────────────────────────────────

def _admin_client():
    import redis as redis_sync

    return redis_sync.Redis.from_url(REDIS_URL)


def _user_url(user: str, pw: str) -> str:
    parts = urlsplit(REDIS_URL)
    return f"redis://{user}:{pw}@{parts.hostname}:{parts.port or 6379}/0"


# The key prefixes and commands the service uses (README "Use an external Redis").
APP_KEYS = ("~auth:sess:*", "~auth:totp_pending:*", "~auth:login:addr:*", "~auth:mfa-fail:*", "~auth:totp-used:*", "~auth:webauthn-used:*")
APP_COMMANDS = ("+ping", "+setex", "+get", "+del", "+set", "+incrby", "+ttl", "+expire", "+multi", "+exec")
# The check fails either way; redis-py does not report a refused MULTI or EXEC as a
# permission error, so the page shows the general message.
TRANSACTION_DENIED = "other"


# Each prefix with only the commands the service sends for it; PING, MULTI and EXEC take no key.
EXACT_PER_PREFIX = (
    "+ping", "+multi", "+exec",
    "(~auth:sess:* +setex +get +del)",
    "(~auth:totp_pending:* +setex +get +del)",
    "(~auth:login:addr:* +set +incrby +ttl +expire)",
    "(~auth:mfa-fail:* +set +incrby +ttl +expire +del)",
    "(~auth:totp-used:* +set)",
    "(~auth:webauthn-used:* +set)",
)


def _without(command):
    return tuple(c for c in APP_COMMANDS if c != command)


@pytest.fixture()
def acl():
    """acl(*rules) creates an ACL user with a generated password and returns its URL."""
    created = []
    client = _admin_client()

    def make(*rules, passwords=None):
        user = f"t{secrets.token_hex(6)}"
        pws = passwords or [_pw()]
        client.execute_command("ACL", "SETUSER", user, "reset", "on", *(f">{p}" for p in pws), *rules)
        created.append(user)
        return user, pws

    yield make
    for user in created:
        client.execute_command("ACL", "DELUSER", user)
    client.close()


@real_redis
def test_real_check_passes_and_leaves_no_probe_keys():
    rs = _rs()
    client = _admin_client()
    try:
        before = set(client.scan_iter(match="auth:*"))
        assert asyncio.run(rs.check(REDIS_URL)) is None
        # Like the service, the check lets the address counter and the used-code marker expire.
        for key in set(client.scan_iter(match="auth:*")) - before:
            assert key.startswith((b"auth:login:addr:", b"auth:totp-used:", b"auth:webauthn-used:")), key
            assert 0 < client.ttl(key) <= rs.PROBE_TTL_SEC
    finally:
        client.close()


@real_redis
def test_real_wrong_password_is_auth():
    parts = urlsplit(REDIS_URL)
    assert asyncio.run(_rs().check(f"redis://:{_pw()}@{parts.hostname}:{parts.port or 6379}/0")) == "auth"


@real_redis
@pytest.mark.parametrize("rules,expected", [
    (("~*", "+@read", "+ping"), "no_write"),  # read-only user
    (("~*", "+ping", "+set", "+get", "+del"), "no_write"),  # no SETEX and no counters
    ((*APP_KEYS, *_without("+setex")), "no_write"),
    ((*APP_KEYS, *_without("+incrby")), "no_write"),
    ((*APP_KEYS, *_without("+ttl")), "no_write"),
    ((*APP_KEYS, *_without("+expire")), "no_write"),
    ((*APP_KEYS, *_without("+multi")), TRANSACTION_DENIED),
    ((*APP_KEYS, *_without("+exec")), TRANSACTION_DENIED),
    (("~auth:sess:*", "+@all"), "no_write"),  # one of the prefixes only
    ((*(k for k in APP_KEYS if k != "~auth:login:addr:*"), "+@all"), "no_write"),
    ((*(k for k in APP_KEYS if k != "~auth:totp-used:*"), "+@all"), "no_write"),
    ((*(k for k in APP_KEYS if k != "~auth:webauthn-used:*"), "+@all"), "no_write"),
    ((*APP_KEYS, *APP_COMMANDS), None),  # exactly what the app uses
    # Exactly the commands the app sends for each key prefix (Redis 7 ACL selectors).
    (EXACT_PER_PREFIX, None),
    (("~auth:*", *APP_COMMANDS), None),  # the rule README gives
    # Keys in the shape the app writes (hex record names) pass ...
    (("~auth:sess:[0-9a-f]*", "~auth:totp_pending:[0-9a-f]*", *APP_KEYS[2:], *APP_COMMANDS), None),
    # ... and keys of another shape under the same prefixes do not stand in for them.
    (("~auth:sess:probe:*", "~auth:totp_pending:probe:*", *APP_KEYS[2:], "+@all"), "no_write"),
])
def test_real_acl_users(acl, rules, expected):
    user, (pw,) = acl(*rules)
    assert asyncio.run(_rs().check(_user_url(user, pw))) == expected


@pytest.fixture()
def real_app(monkeypatch, tmp_path):
    a = App(monkeypatch, tmp_path, None)
    yield a
    a.stop()


@real_redis
def test_real_read_only_user_saves_nothing(real_app, acl, monkeypatch, capsys):
    user, (pw,) = acl("~*", "+@read", "+ping")
    use_env(monkeypatch, default=REDIS_URL)
    admin = real_app.start()
    admin.login()
    res = admin.put({"baseVersion": 0, "url": _user_url(user, pw), "confirmRestart": True})
    assert res.status_code == 422 and res.json()["category"] == "no_write"
    assert admin.get().json()["version"] == 0
    assert admin.db("SELECT COUNT(*) AS n FROM redis_settings")[0]["n"] == 0
    out = capsys.readouterr()
    _no_secret(pw, res.text, out.out, out.err)


@real_redis
def test_real_save_then_restart_uses_the_new_url(real_app, acl, monkeypatch, capsys, caplog):
    """A process started after a save uses the new URL; the running one keeps the old."""
    caplog.set_level(logging.DEBUG)
    user, (pw,) = acl(*APP_KEYS, "+@all")
    use_env(monkeypatch, default=REDIS_URL)
    admin = real_app.start()
    admin.login()
    res = admin.put({"baseVersion": 0, "url": _user_url(user, pw), "confirmRestart": True})
    assert res.status_code == 200, res.text
    view = admin.get().json()
    assert view["restartNeeded"] is True and user not in view["url"]["connected"] and user in view["url"]["saved"]
    real_app.stop()

    admin = real_app.start()
    admin.login()  # sessions are written through the new connection
    view = admin.get().json()
    assert view["url"]["source"] == "stored" and user in view["url"]["connected"] and view["restartNeeded"] is False
    out = capsys.readouterr()
    rows = admin.db("SELECT * FROM activity_logs")
    _no_secret(pw, json.dumps(view), json.dumps(rows, default=str), out.out, out.err, caplog.text)


@real_redis
def test_real_password_only_change_needs_a_restart(real_app, acl, monkeypatch):
    user, (p1, p2) = acl(*APP_KEYS, "+@all", passwords=[_pw(), _pw()])
    use_env(monkeypatch, default=REDIS_URL)
    admin = real_app.start()
    admin.login()
    assert admin.put({"baseVersion": 0, "url": _user_url(user, p1), "confirmRestart": True}).status_code == 200
    real_app.stop()
    admin = real_app.start()
    admin.login()
    assert admin.get().json()["restartNeeded"] is False
    res = admin.put({"baseVersion": 1, "url": _user_url(user, p2), "confirmRestart": True})
    assert res.status_code == 200
    view = res.json()
    # The masked text is the same; only the password changed.
    assert view["url"]["saved"] == view["url"]["connected"] and view["restartNeeded"] is True


@real_redis
def test_real_copy_waits_until_redis_accepts_the_url(real_app, monkeypatch):
    rs = _rs()
    import app.main as main_mod

    user, pw = f"t{secrets.token_hex(6)}", _pw()
    use_env(monkeypatch, url=_user_url(user, pw))  # this user does not exist yet
    admin = real_app.start()
    # Sessions cannot be written for this user yet: read the state directly.
    assert rs.state.sync_pending is True
    sdb = main_mod.app.state.settings_db
    assert admin.call(rs.read_current, sdb).version == 0
    client = _admin_client()
    try:
        client.execute_command("ACL", "SETUSER", user, "reset", "on", f">{pw}", *APP_KEYS, "+@all")
        admin.call(rs.retry_sync, sdb, main_mod.app.state.db)  # what the reload loop runs every 15 seconds
        assert rs.state.sync_pending is False
        assert rs.saved_url(admin.call(rs.read_current, sdb)) == _user_url(user, pw)
    finally:
        client.execute_command("ACL", "DELUSER", user)
        client.close()


@real_redis
def test_real_changing_the_password_of_a_saved_url(real_app, acl, monkeypatch):
    """README "Changing secrets": with a saved URL, set the new password in REDIS_URL,
    restart, and remove REDIS_URL once the page shows it saved."""
    rs = _rs()
    user, (p1,) = acl(*APP_KEYS, "+@all")
    use_env(monkeypatch, default=REDIS_URL)
    admin = real_app.start()
    admin.login()
    assert admin.put({"baseVersion": 0, "url": _user_url(user, p1), "confirmRestart": True}).status_code == 200
    real_app.stop()

    p2 = _pw()
    client = _admin_client()
    try:
        client.execute_command("ACL", "SETUSER", user, "resetpass", f">{p2}")
    finally:
        client.close()
    use_env(monkeypatch, url=_user_url(user, p2), default=REDIS_URL)
    admin = real_app.start()
    admin.login()
    view = admin.get().json()
    assert view["url"]["source"] == "env" and view["url"]["syncPending"] is False and view["version"] == 2
    real_app.stop()

    use_env(monkeypatch, default=REDIS_URL)
    admin = real_app.start()
    admin.login()
    view = admin.get().json()
    assert view["url"]["source"] == "stored" and view["restartNeeded"] is False
    assert rs.state.connection.url == _user_url(user, p2)


@real_redis
def test_real_user_limited_to_the_checked_commands_runs_the_whole_app(real_app, acl, monkeypatch):
    """What the check probes is what the service needs: first-admin setup (issuer
    included), sign-out, sign-in with its attempt limits, and the health check work for
    such a user."""
    import time

    import pyotp

    user, (pw,) = acl(*EXACT_PER_PREFIX)
    url = _user_url(user, pw)
    assert asyncio.run(_rs().check(url)) is None
    use_env(monkeypatch, default=url)
    admin = real_app.start()
    c = admin.client
    password = secrets.token_urlsafe(16)
    res = c.post("/admin/api/bootstrap/register", json={"username": "first", "password": password})
    assert res.status_code == 200, res.text
    csrf = res.json()["csrf"]
    res = c.post("/admin/api/bootstrap/issuer", json={"issuer": "Acme"}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200, res.text
    secret = c.get("/admin/api/bootstrap/mfa-enroll").json()["secretBase32"]
    res = c.post("/admin/api/bootstrap/mfa-verify", json={"code": pyotp.TOTP(secret).now()}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200, res.text
    assert c.get("/admin/api/me").json()["user"] == {"username": "first"}
    assert c.post("/admin/api/logout").status_code == 200
    res = c.post("/admin/api/login", json={"username": "first", "password": password})
    assert res.status_code == 200, res.text
    # The code of the next time step: the current one was used to finish the setup.
    code = pyotp.TOTP(secret).at(time.time() + 30)
    res = c.post("/admin/api/login/mfa", json={"code": code}, headers={"X-CSRF-Token": res.json()["csrf"]})
    assert res.status_code == 200, res.text
    assert c.get("/admin/api/me").json()["user"] == {"username": "first"}
    assert c.get("/health").json()["redis"] is True

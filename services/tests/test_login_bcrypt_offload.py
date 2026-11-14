"""Admin sign-in: bcrypt off the event loop, equal work for unknown usernames, and the
per-address attempt limit (app.login_limit).

bcrypt used to run on the event loop, so a few concurrent sign-ins held up every other
request; an unknown username answered at once, which told which usernames exist; and
nothing limited the number of attempts.

Every secret and password is generated per run with ``secrets``; the app opens this
run's own temporary SQLite database and Redis is an in-memory fake (the no-retry test
uses a local TCP listener). Requires pytest and httpx.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

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
_DATABASE_URL = f"sqlite:///{_TMP_DIR.name}/login-bcrypt.db"
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


@pytest.fixture()
def login_limit():
    # Imported by the tests, not at collection: importing the app reads the settings,
    # which other test modules set in the environment when they are collected.
    from app import login_limit as module

    return module


LOGIN = "/admin/api/login"
REGISTER = "/admin/api/bootstrap/register"
WAIT = 5  # seconds a test waits for something to happen before it fails


class FakeRedis:
    """In-memory Redis: values, expiries (seconds, never counting down) and transactions."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.expiry: dict[str, int] = {}
        self.fail_next: dict[str, Exception] = {}  # command name -> error inside the next EXEC

    async def get(self, k):
        return self.data.get(k)

    async def setex(self, k, ttl, v):
        self.data[k] = v
        self.expiry[k] = ttl

    async def delete(self, k):
        self.data.pop(k, None)
        self.expiry.pop(k, None)

    async def expire(self, k, ttl):
        if k not in self.data:
            return False
        self.expiry[k] = ttl
        return True

    async def ping(self):
        return True

    async def aclose(self):
        pass

    def pipeline(self, transaction=True):
        return _FakePipeline(self)

    # Commands as a transaction runs them.
    def _set(self, k, v, ex=None, nx=False):
        if nx and k in self.data:
            return None
        self.data[k] = str(v)
        if ex is not None:
            self.expiry[k] = ex
        else:
            self.expiry.pop(k, None)
        return True

    def _incr(self, k):
        from redis.exceptions import ResponseError

        try:
            n = int(self.data.get(k, "0")) + 1
        except ValueError:
            raise ResponseError("value is not an integer or out of range") from None
        self.data[k] = str(n)
        return n

    def _ttl(self, k):
        if k not in self.data:
            return -2
        return self.expiry.get(k, -1)


class _FakePipeline:
    def __init__(self, fake: FakeRedis) -> None:
        self.fake = fake
        self.queue: list[tuple[str, tuple, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def set(self, *a, **k):
        self.queue.append(("set", a, k))
        return self

    def incr(self, *a, **k):
        self.queue.append(("incr", a, k))
        return self

    def ttl(self, *a, **k):
        self.queue.append(("ttl", a, k))
        return self

    async def execute(self, raise_on_error=True):
        results = []
        for name, a, k in self.queue:
            err = self.fake.fail_next.pop(name, None)
            if err is not None:
                results.append(err)
                continue
            try:
                results.append(getattr(self.fake, "_" + name)(*a, **k))
            except Exception as e:  # noqa: BLE001 - mirrors redis-py: errors become results
                results.append(e)
        self.queue = []
        if raise_on_error:
            for r in results:
                if isinstance(r, Exception):
                    raise r
        return results


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
    clients: list[dict] = []

    def from_url(*a, **k):
        clients.append(k)
        return fake

    monkeypatch.setattr(main_mod.redis, "from_url", from_url)

    async def _no_cleanup(*_a, **_k):
        return None

    monkeypatch.setattr(main_mod, "cleanup_expired_tokens", _no_cleanup)
    with TestClient(main_mod.app) as c:
        db = main_mod.app.state.db
        try:
            yield SimpleNamespace(c=c, db=db, fake=fake, clients=clients)
        finally:
            c.portal.call(db.execute, "DELETE FROM admin_users")


def _add_user(ctx, password: str) -> str:
    username = "admin-" + secrets.token_hex(4)
    pw_hash = bcrypt.hashpw(password.encode("ascii"), bcrypt.gensalt(rounds=4)).decode("ascii")
    ctx.c.portal.call(
        ctx.db.execute,
        "INSERT INTO admin_users (id, username, password_hash, totp_enabled) VALUES ($1, $2, $3, $4)",
        str(uuid.uuid4()), username, pw_hash, False,
    )
    return username


def _login(ctx, username: str, password: str):
    return ctx.c.post(LOGIN, json={"username": username, "password": password})


def _wrong() -> str:
    return "wrong-" + secrets.token_hex(8)


def _on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class Spy:
    """Wraps a bcrypt function: records each call, can hold calls until released."""

    def __init__(self, real, hold: bool = False, sleep: float = 0.0) -> None:
        self.real = real
        self.calls: list[tuple] = []
        self.on_loop: list[bool] = []
        self.active = 0
        self.peak = 0
        self.lock = threading.Lock()
        self.release = threading.Event()
        if not hold:
            self.release.set()
        self.sleep = sleep

    def __call__(self, *args):
        with self.lock:
            self.calls.append(args)
            self.on_loop.append(_on_event_loop())
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            # Bounded, so code that blocks the event loop cannot hang the test.
            self.release.wait(WAIT)
            if self.sleep:
                time.sleep(self.sleep)
            return self.real(*args)
        finally:
            with self.lock:
                self.active -= 1

    def wait_active(self, n: int) -> bool:
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            if self.active >= n:
                return True
            time.sleep(0.01)
        return False


def _in_threads(fn, n: int) -> tuple[list, list[threading.Thread]]:
    results: list = []
    threads = [threading.Thread(target=lambda: results.append(fn())) for _ in range(n)]
    for t in threads:
        t.start()
    return results, threads


def _join(threads) -> None:
    for t in threads:
        t.join(WAIT * 3)
        assert not t.is_alive()


# ── bcrypt runs off the event loop, at most BCRYPT_SLOTS at once ──


def test_login_checks_the_password_off_the_event_loop(login_limit, app_ctx, monkeypatch):
    spy = Spy(bcrypt.checkpw)
    monkeypatch.setattr(bcrypt, "checkpw", spy)
    user = _add_user(app_ctx, secrets.token_hex(8))
    assert _login(app_ctx, user, _wrong()).status_code == 401
    assert spy.on_loop == [False]


def test_register_hashes_the_password_off_the_event_loop(login_limit, app_ctx, monkeypatch):
    spy = Spy(bcrypt.hashpw)
    monkeypatch.setattr(bcrypt, "hashpw", spy)
    res = app_ctx.c.post(REGISTER, json={"username": "admin", "password": secrets.token_hex(8)})
    assert res.status_code == 200
    assert spy.on_loop == [False]
    assert spy.calls[0][1].startswith(b"$2b$12$")


def test_concurrent_logins_overlap_up_to_the_slot_count(login_limit, app_ctx, monkeypatch):
    user = _add_user(app_ctx, secrets.token_hex(8))
    spy = Spy(bcrypt.checkpw, hold=True)
    monkeypatch.setattr(bcrypt, "checkpw", spy)
    results, threads = _in_threads(lambda: _login(app_ctx, user, _wrong()).status_code, 5)
    try:
        spy.wait_active(login_limit.BCRYPT_SLOTS)
        time.sleep(0.3)  # room for a third call to start, if anything allowed it
        peak = spy.peak
    finally:
        spy.release.set()
        _join(threads)
    assert peak == login_limit.BCRYPT_SLOTS
    assert results == [401] * 5


def test_register_shares_the_slots_with_login(login_limit, app_ctx, monkeypatch):
    user = _add_user(app_ctx, secrets.token_hex(8))
    check = Spy(bcrypt.checkpw, hold=True)
    hashpw = Spy(bcrypt.hashpw)
    monkeypatch.setattr(bcrypt, "checkpw", check)
    monkeypatch.setattr(bcrypt, "hashpw", hashpw)
    logins, threads = _in_threads(lambda: _login(app_ctx, user, _wrong()).status_code, 2)
    reg: list = []
    try:
        assert check.wait_active(2)
        reg, reg_threads = _in_threads(
            lambda: app_ctx.c.post(REGISTER, json={"username": "admin", "password": secrets.token_hex(8)}).status_code,
            1,
        )
        threads += reg_threads
        time.sleep(0.3)
        waited = hashpw.calls == []  # waits for a slot
    finally:
        check.release.set()
        _join(threads)
    assert waited
    assert logins == [401, 401] and reg == [200]
    assert len(hashpw.calls) == 1


def test_a_full_slot_queue_answers_503_after_the_wait(login_limit, app_ctx, monkeypatch):
    monkeypatch.setattr(login_limit, "SLOT_WAIT_SECONDS", 0.3)
    user = _add_user(app_ctx, secrets.token_hex(8))
    spy = Spy(bcrypt.checkpw, hold=True)
    monkeypatch.setattr(bcrypt, "checkpw", spy)
    held, threads = _in_threads(lambda: _login(app_ctx, user, _wrong()).status_code, 2)
    try:
        assert spy.wait_active(2)
        res = _login(app_ctx, user, _wrong())
    finally:
        spy.release.set()
        _join(threads)
    assert res.status_code == 503
    assert res.headers["Retry-After"]
    assert len(spy.calls) == 2  # the third request never reached bcrypt
    assert held == [401, 401]


def test_a_started_check_longer_than_the_wait_still_answers(login_limit, app_ctx, monkeypatch):
    monkeypatch.setattr(login_limit, "SLOT_WAIT_SECONDS", 0.2)
    spy = Spy(bcrypt.checkpw, sleep=0.5)
    monkeypatch.setattr(bcrypt, "checkpw", spy)
    user = _add_user(app_ctx, secrets.token_hex(8))
    assert _login(app_ctx, user, _wrong()).status_code == 401


# ── Unknown usernames take the same path ──────────────────────


def test_unknown_username_is_checked_against_a_cost_12_dummy_hash(login_limit, app_ctx, monkeypatch):
    check = Spy(bcrypt.checkpw)
    hashpw = Spy(bcrypt.hashpw)
    monkeypatch.setattr(bcrypt, "checkpw", check)
    monkeypatch.setattr(bcrypt, "hashpw", hashpw)
    res = _login(app_ctx, "nobody-" + secrets.token_hex(4), _wrong())
    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid credentials"
    assert len(check.calls) == 1
    assert check.calls[0][1] == login_limit.DUMMY_HASH
    assert login_limit.DUMMY_HASH.startswith(b"$2b$12$")
    assert hashpw.calls == []  # made at import, never during a request


def test_dummy_hash_never_matches_a_password_on_its_own(login_limit, app_ctx):
    assert not bcrypt.checkpw(b"", login_limit.DUMMY_HASH)


@pytest.mark.parametrize("known", [True, False], ids=["known-user", "unknown-user"])
def test_a_lone_surrogate_is_refused_before_lookup_and_bcrypt(login_limit, app_ctx, monkeypatch, known):
    check = Spy(bcrypt.checkpw)
    monkeypatch.setattr(bcrypt, "checkpw", check)
    lookups = []
    real_fetchrow = app_ctx.db.fetchrow

    async def fetchrow(*a, **k):
        lookups.append(a)
        return await real_fetchrow(*a, **k)

    monkeypatch.setattr(app_ctx.db, "fetchrow", fetchrow)
    user = _add_user(app_ctx, secrets.token_hex(8)) if known else "nobody-" + secrets.token_hex(4)
    raw = '{"username": "' + user + '", "password": "' + secrets.token_hex(4) + '\\ud800"}'
    res = app_ctx.c.post(LOGIN, content=raw.encode("ascii"), headers={"Content-Type": "application/json"})
    assert res.status_code == 401
    assert check.calls == [] and lookups == []


def test_a_correct_password_still_reaches_the_next_step(login_limit, app_ctx):
    pw = secrets.token_hex(8)
    user = _add_user(app_ctx, pw)
    res = _login(app_ctx, user, pw)
    # The password matched; this user has no MFA yet.
    assert res.status_code == 403
    assert res.json()["detail"] == "MFA not configured"


# ── Attempt limit per address ─────────────────────────────────


def test_the_eleventh_attempt_from_an_address_gets_429_without_bcrypt(login_limit, app_ctx, monkeypatch):
    check = Spy(bcrypt.checkpw)
    monkeypatch.setattr(bcrypt, "checkpw", check)
    user = _add_user(app_ctx, secrets.token_hex(8))
    for _ in range(login_limit.MAX_ATTEMPTS):
        assert _login(app_ctx, user, _wrong()).status_code == 401
    res = _login(app_ctx, user, _wrong())
    assert res.status_code == 429
    assert int(res.headers["Retry-After"]) == login_limit.WINDOW_SECONDS
    assert res.json()["detail"] == "Too many sign-in attempts from this address. Try again in 15 minutes."
    assert len(check.calls) == login_limit.MAX_ATTEMPTS
    # Unknown usernames count too.
    assert _login(app_ctx, "nobody-" + secrets.token_hex(4), _wrong()).status_code == 429


def test_a_burst_reaches_bcrypt_at_most_the_limit(login_limit, app_ctx, monkeypatch):
    check = Spy(bcrypt.checkpw)
    monkeypatch.setattr(bcrypt, "checkpw", check)
    user = _add_user(app_ctx, secrets.token_hex(8))
    results, threads = _in_threads(lambda: _login(app_ctx, user, _wrong()).status_code, 30)
    _join(threads)
    assert sorted(results) == [401] * login_limit.MAX_ATTEMPTS + [429] * (30 - login_limit.MAX_ATTEMPTS)
    assert len(check.calls) == login_limit.MAX_ATTEMPTS


def test_a_correct_password_does_not_reset_the_count(login_limit, app_ctx):
    pw = secrets.token_hex(8)
    user = _add_user(app_ctx, pw)
    for _ in range(login_limit.MAX_ATTEMPTS - 1):
        assert _login(app_ctx, user, _wrong()).status_code == 401
    assert _login(app_ctx, user, pw).status_code == 403
    assert _login(app_ctx, user, pw).status_code == 429


def test_the_counter_key_expires_with_its_window(login_limit, app_ctx):
    user = _add_user(app_ctx, secrets.token_hex(8))
    _login(app_ctx, user, _wrong())
    (key,) = [k for k in app_ctx.fake.data if k.startswith(login_limit.COUNTER_PREFIX)]
    assert app_ctx.fake.expiry[key] == login_limit.WINDOW_SECONDS


def test_a_counter_without_expiry_gets_one(login_limit, app_ctx):
    key = login_limit.COUNTER_PREFIX + "unknown"  # TestClient's address is not an IP
    app_ctx.fake.data[key] = str(login_limit.MAX_ATTEMPTS + 5)
    res = _login(app_ctx, "nobody", _wrong())
    assert res.status_code == 429
    assert app_ctx.fake.expiry[key] == login_limit.WINDOW_SECONDS


def test_a_failed_command_in_the_transaction_still_leaves_an_expiry(login_limit, app_ctx):
    from redis.exceptions import ResponseError

    fake = app_ctx.fake
    fake.fail_next["set"] = ResponseError("NOPERM")
    key = secrets.token_hex(4)
    with pytest.raises(ResponseError):
        app_ctx.c.portal.call(login_limit.count_attempt, fake, key)
    name = login_limit.COUNTER_PREFIX + key
    assert fake.data[name] == "1"
    assert fake.expiry[name] == login_limit.WINDOW_SECONDS


def test_a_redis_error_fails_the_login_closed(login_limit, app_ctx, monkeypatch):
    from redis.exceptions import ResponseError

    check = Spy(bcrypt.checkpw)
    monkeypatch.setattr(bcrypt, "checkpw", check)
    app_ctx.fake.fail_next["incr"] = ResponseError("NOPERM")
    with pytest.raises(ResponseError):
        _login(app_ctx, "nobody", _wrong())
    assert check.calls == []


def _request(host):
    return SimpleNamespace(client=None if host is None else SimpleNamespace(host=host))


@pytest.mark.parametrize(
    "host, key",
    [
        ("192.0.2.7", "192.0.2.7"),
        ("::ffff:192.0.2.7", "192.0.2.7"),
        ("2001:db8:1:2:aaaa::1", "2001:db8:1:2::/64"),
        ("2001:db8:1:2:bbbb::9", "2001:db8:1:2::/64"),
        ("2001:db8:1:3::1", "2001:db8:1:3::/64"),
        ("testclient", "unknown"),
        (None, "unknown"),
    ],
)
def test_client_key(login_limit, host, key):
    assert login_limit.client_key(_request(host)) == key


def test_addresses_are_counted_apart(login_limit, app_ctx):
    fake = app_ctx.fake
    for _ in range(3):
        app_ctx.c.portal.call(login_limit.count_attempt, fake, "192.0.2.1")
    count, _ttl = app_ctx.c.portal.call(login_limit.count_attempt, fake, "192.0.2.2")
    assert count == 1


# ── The counter's Redis client never resends ──────────────────


def test_the_app_gives_the_counter_a_client_without_retries(login_limit, app_ctx):
    assert login_limit.COUNTER_CLIENT_OPTIONS["retry"] in [k.get("retry") for k in app_ctx.clients]


def test_a_lost_reply_does_not_send_the_transaction_twice(login_limit):
    """A Redis that drops the connection when the transaction arrives, before any reply."""
    import redis.asyncio as redis
    from redis.exceptions import ConnectionError as RedisConnectionError

    async def run() -> int:
        multis = 0

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            nonlocal multis
            while True:
                line = await reader.readline()
                if not line:
                    break
                if not line.startswith(b"*"):
                    continue
                args = []
                for _ in range(int(line[1:])):
                    size = int((await reader.readline())[1:])
                    args.append((await reader.readexactly(size + 2))[:-2].upper())
                if args[0] == b"MULTI":
                    multis += 1
                    writer.close()  # the reply is lost
                    return
                # HELLO (RESP3 handshake) wants a map; anything else before MULTI gets OK.
                writer.write(b"%1\r\n+proto\r\n:3\r\n" if args[0] == b"HELLO" else b"+OK\r\n")
                await writer.drain()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = redis.from_url(
            f"redis://127.0.0.1:{port}/0?retry_on_timeout=true",
            decode_responses=True,
            **login_limit.COUNTER_CLIENT_OPTIONS,
        )
        try:
            with pytest.raises(RedisConnectionError):
                await login_limit.count_attempt(client, "192.0.2.1")
        finally:
            await client.aclose()
            server.close()
            await server.wait_closed()
        return multis

    assert asyncio.run(run()) == 1

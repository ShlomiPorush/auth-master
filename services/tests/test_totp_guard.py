"""TOTP attempt limit and single-use codes.

- login/mfa, token reveal and API key reveal share a per-admin attempt limit;
- an accepted code is not accepted again, also not by bootstrap mfa-verify;
- the partial session between password and code lasts 5 minutes.

Every secret the app needs, the admin password and the TOTP seed are generated per
run with ``secrets`` and ``pyotp.random_base32()``; codes are derived from that seed at
a controlled clock.  Redis is replaced by a fake with expiry on the same clock.
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

# Removed when the test process exits.
_TMP_DIR = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
_TMP = _TMP_DIR.name
_PLACEHOLDERS = {
    "",
    "change-me-in-production",
    "generate-a-long-random-string",
    "min-32-chars-secret-change-me-for-production-use",
    "min-32-chars-secret-change-me!!",
    "at-least-32-characters-long-secret-key",
    "0123456789abcdef0123456789abcdef",
}
_SECRETS = {
    "ADMIN_API_KEY": secrets.token_urlsafe(32),
    "SESSION_SECRET": secrets.token_urlsafe(48),
    "APP_ENCRYPTION_KEY": secrets.token_hex(16),
}
for _name, _value in _SECRETS.items():
    if _value in _PLACEHOLDERS:
        raise RuntimeError(f"{_name} is empty or a placeholder")
os.environ.update(_SECRETS)
# The app requires a Redis password; the tests replace Redis with FakeRedis below.
os.environ["REDIS_URL"] = f"redis://:{secrets.token_hex(32)}@127.0.0.1:6379"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["ALLOWED_AREAS"] = "orders,billing"
os.environ["TZ"] = "UTC"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bcrypt  # noqa: E402
import pyotp  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.main as main_mod  # noqa: E402
from app import totp_guard  # noqa: E402
from app.crypto_tokens import encrypt_token_value, sha256_hex  # noqa: E402
from app.crypto_totp import encrypt_totp_secret  # noqa: E402

# 5 s into a 30 s step, so a request never straddles a step boundary by accident.
T0 = 1_900_000_005.0


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> float:
        return self.t


class FakeRedis:
    """What the app uses of redis-py, with expiry on the test clock.

    Each async command yields once before it runs, so concurrent checks interleave;
    a pipeline runs its queued commands together, like MULTI/EXEC.
    """

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.data: dict[str, str] = {}
        self.exp: dict[str, float] = {}

    def _alive(self, k):
        if k in self.exp and self.exp[k] <= self.clock():
            self.data.pop(k, None)
            self.exp.pop(k, None)
        return k in self.data

    def _set(self, k, v, nx=False, ex=None):
        if nx and self._alive(k):
            return None
        self.data[k] = str(v)
        self.exp.pop(k, None)
        if ex is not None:
            self.exp[k] = self.clock() + ex
        return True

    def _incr(self, k):
        n = int(self.data[k]) + 1 if self._alive(k) else 1
        self.data[k] = str(n)
        return n

    def _ttl(self, k):
        if not self._alive(k):
            return -2
        if k not in self.exp:
            return -1
        return max(0, round(self.exp[k] - self.clock()))

    async def get(self, k):
        await asyncio.sleep(0)
        return self.data.get(k) if self._alive(k) else None

    async def set(self, k, v, nx=False, ex=None):
        await asyncio.sleep(0)
        return self._set(k, v, nx=nx, ex=ex)

    async def setex(self, k, ttl, v):
        await asyncio.sleep(0)
        self._set(k, v, ex=ttl)

    async def delete(self, *keys):
        await asyncio.sleep(0)
        for k in keys:
            self.data.pop(k, None)
            self.exp.pop(k, None)

    async def expire(self, k, seconds):
        await asyncio.sleep(0)
        if self._alive(k):
            self.exp[k] = self.clock() + seconds

    async def ttl(self, k):
        await asyncio.sleep(0)
        return self._ttl(k)

    def pipeline(self, transaction=True):
        assert transaction
        return FakePipeline(self)

    async def ping(self):
        return True

    async def aclose(self):
        pass


class FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.ops: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.ops.clear()

    def set(self, k, v, nx=False, ex=None):
        self.ops.append(lambda: self.redis._set(k, v, nx=nx, ex=ex))
        return self

    def incr(self, k):
        self.ops.append(lambda: self.redis._incr(k))
        return self

    def ttl(self, k):
        self.ops.append(lambda: self.redis._ttl(k))
        return self

    async def execute(self):
        await asyncio.sleep(0)
        out = [op() for op in self.ops]
        self.ops.clear()
        return out


@pytest.fixture()
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(totp_guard, "_now", c)
    return c


@pytest.fixture()
def ctx(monkeypatch, clock):
    fake = FakeRedis(clock)
    monkeypatch.setattr(main_mod.redis, "from_url", lambda *a, **k: fake)

    async def _no_cleanup(*_a, **_k):
        return None

    monkeypatch.setattr(main_mod, "cleanup_expired_tokens", _no_cleanup)
    with TestClient(main_mod.app) as c:
        c.portal.call(_wipe, main_mod.app.state.db)
        c.fake_redis = fake
        c.clock = clock
        yield c
        c.portal.call(_wipe, main_mod.app.state.db)


async def _wipe(db):
    for table in ("admin_users", "tokens", "api_keys", "activity_logs"):
        await db.execute(f"DELETE FROM {table}")


def _db(c, fn, *args):
    return c.portal.call(fn, main_mod.app.state.db, *args)


class Admin:
    def __init__(self, admin_id, username, password, totp):
        self.id, self.username, self.password, self.totp = admin_id, username, password, totp


def _distinct_seed() -> str:
    # Six digits can repeat between steps; some tests need different codes around T0.
    while True:
        seed = pyotp.random_base32()
        totp = pyotp.TOTP(seed)
        if len({totp.at(int(T0) + d) for d in (-30, 0, 30, 60)}) == 4:
            return seed


def _add_admin(c, seed: str | None = None) -> Admin:
    seed = seed or pyotp.random_base32()
    password = secrets.token_urlsafe(16)
    username = "adm" + secrets.token_hex(4)
    admin_id = str(uuid.uuid4())

    async def _insert(db):
        await db.execute(
            "INSERT INTO admin_users (id, username, password_hash, totp_secret_enc, totp_enabled) "
            "VALUES ($1, $2, $3, $4, $5)",
            admin_id, username, bcrypt.hashpw(password.encode(), bcrypt.gensalt(4)).decode(),
            encrypt_totp_secret(seed), True,
        )

    _db(c, _insert)
    return Admin(admin_id, username, password, pyotp.TOTP(seed))


def _add_token(c) -> tuple[str, str]:
    value = secrets.token_urlsafe(24)
    tid = str(uuid.uuid4())

    async def _insert(db):
        await db.execute(
            "INSERT INTO tokens (id, name, token_hash, token_enc, grants) VALUES ($1, $2, $3, $4, $5)",
            tid, "tok-" + tid[:8], sha256_hex(value), encrypt_token_value(value), json.dumps([]),
        )

    _db(c, _insert)
    return tid, value


def _add_api_key(c, *, revealable=True) -> tuple[str, str]:
    value = secrets.token_urlsafe(24)
    kid = str(uuid.uuid4())

    async def _insert(db):
        await db.execute(
            "INSERT INTO api_keys (id, name, key_hash, key_enc, scopes) VALUES ($1, $2, $3, $4, $5)",
            kid, "key-" + kid[:8], sha256_hex(value), encrypt_token_value(value) if revealable else None, "validate",
        )

    _db(c, _insert)
    return kid, value


def _code(admin: Admin, c, steps: int = 0) -> str:
    return admin.totp.at(int(c.clock() + steps * 30))


def _wrong(admin: Admin, c) -> str:
    good = {admin.totp.at(int(c.clock() + d)) for d in (-60, -30, 0, 30, 60)}
    while True:
        guess = "%06d" % secrets.randbelow(10**6)
        if guess not in good:
            return guess


def _use(c, sid):
    c.cookies.clear()
    if sid:
        c.cookies.set("auth_sid", sid)


def _login(c, admin: Admin):
    _use(c, None)
    r = c.post("/admin/api/login", json={"username": admin.username, "password": admin.password})
    assert r.status_code == 200, r.text
    return r.cookies.get("auth_sid"), r.json()["csrf"], r


def _mfa(c, sid, csrf, code):
    _use(c, sid)
    return c.post("/admin/api/login/mfa", json={"code": code}, headers={"X-CSRF-Token": csrf})


def _full(c, admin: Admin):
    sid, csrf, _ = _login(c, admin)
    r = _mfa(c, sid, csrf, _code(admin, c))
    assert r.status_code == 200, r.text
    return r.cookies.get("auth_sid"), r.json()["csrf"]


def _reveal_token(c, full, tid, code):
    _use(c, full[0])
    return c.post(f"/admin/api/tokens/{tid}/reveal", json={"code": code}, headers={"X-CSRF-Token": full[1]})


def _reveal_key(c, full, kid, code):
    _use(c, full[0])
    return c.post(f"/admin/api/api-keys/{kid}/reveal", json={"code": code}, headers={"X-CSRF-Token": full[1]})


def _actions(c, admin: Admin):
    async def _q(db):
        rows = await db.fetch(
            "SELECT action, details FROM activity_logs WHERE entity_id = $1 ORDER BY rowid", admin.id
        )
        return [(r["action"], json.loads(r["details"]) if r["details"] else None) for r in rows]

    return _db(c, _q)


def _fail_count(c, admin: Admin):
    v = c.fake_redis.data.get(totp_guard.FAIL_PREFIX + admin.id)
    return int(v) if v is not None and c.fake_redis._alive(totp_guard.FAIL_PREFIX + admin.id) else 0


# ── Single use ──────────────────────────────────────────────────


def test_code_accepted_at_login_is_refused_on_another_session(ctx):
    admin = _add_admin(ctx)
    a = _login(ctx, admin)[:2]
    b = _login(ctx, admin)[:2]
    code = _code(admin, ctx)
    assert _mfa(ctx, *a, code).status_code == 200
    r = _mfa(ctx, *b, code)
    assert r.status_code == 400
    assert r.json()["detail"] == totp_guard.REUSED_TEXT
    # The next code completes the second sign-in.
    ctx.clock.t += 30
    assert _mfa(ctx, *b, _code(admin, ctx)).status_code == 200


def test_login_code_and_reveal_codes_are_single_use(ctx):
    admin = _add_admin(ctx)
    tid, token_value = _add_token(ctx)
    kid, key_value = _add_api_key(ctx)
    login_code = _code(admin, ctx)
    sid, csrf, _ = _login(ctx, admin)
    r = _mfa(ctx, sid, csrf, login_code)
    full = (r.cookies.get("auth_sid"), r.json()["csrf"])

    # The login code does not also reveal.
    assert _reveal_token(ctx, full, tid, login_code).status_code == 400
    assert _reveal_key(ctx, full, kid, login_code).status_code == 400

    # The next code reveals once.
    ctx.clock.t += 30
    code = _code(admin, ctx)
    r = _reveal_token(ctx, full, tid, code)
    assert r.status_code == 200 and r.json() == {"token": token_value}
    assert _reveal_token(ctx, full, tid, code).status_code == 400
    assert _reveal_key(ctx, full, kid, code).status_code == 400

    ctx.clock.t += 30
    code = _code(admin, ctx)
    r = _reveal_key(ctx, full, kid, code)
    assert r.status_code == 200 and r.json() == {"key": key_value}
    r = _reveal_key(ctx, full, kid, code)
    assert r.status_code == 400 and r.json()["detail"] == totp_guard.REUSED_TEXT


def test_code_from_the_window_edge_is_refused_on_reuse_in_the_next_step(ctx):
    admin = _add_admin(ctx, _distinct_seed())
    a = _login(ctx, admin)[:2]
    b = _login(ctx, admin)[:2]
    # The next step's code is accepted early (valid_window=1) ...
    early = _code(admin, ctx, steps=1)
    assert _mfa(ctx, *a, early).status_code == 200
    # ... and not again once its own step has come.
    ctx.clock.t += 30
    assert _mfa(ctx, *b, early).status_code == 400


def test_bootstrap_code_cannot_sign_in(ctx):
    _use(ctx, None)
    password = secrets.token_urlsafe(16)
    username = "boot" + secrets.token_hex(4)
    r = ctx.post("/admin/api/bootstrap/register", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    setup = (r.cookies.get("auth_sid"), r.json()["csrf"])
    _use(ctx, setup[0])
    totp = pyotp.TOTP(ctx.get("/admin/api/bootstrap/mfa-enroll").json()["secretBase32"])
    code = totp.at(int(ctx.clock()))
    r = ctx.post("/admin/api/bootstrap/mfa-verify", json={"code": code}, headers={"X-CSRF-Token": setup[1]})
    assert r.status_code == 200, r.text

    _use(ctx, None)
    r = ctx.post("/admin/api/login", json={"username": username, "password": password})
    sid, csrf = r.cookies.get("auth_sid"), r.json()["csrf"]
    assert _mfa(ctx, sid, csrf, code).status_code == 400


# ── Attempt limit ───────────────────────────────────────────────


def test_lockout_after_five_wrong_codes(ctx):
    admin = _add_admin(ctx)
    sid, csrf, _ = _login(ctx, admin)
    for _ in range(totp_guard.MAX_ATTEMPTS - 1):
        r = _mfa(ctx, sid, csrf, _wrong(admin, ctx))
        assert r.status_code == 400 and r.json()["detail"] == "Invalid code"

    r = _mfa(ctx, sid, csrf, _wrong(admin, ctx))
    assert r.status_code == 429
    assert r.headers["Retry-After"] == str(totp_guard.ATTEMPT_WINDOW)
    assert r.json()["detail"] == "Too many incorrect codes. Sign in again in 15 minutes."
    cleared = [h for h in r.headers.get_list("set-cookie") if h.startswith("auth_sid=")]
    assert cleared and ("Max-Age=0" in cleared[0] or 'auth_sid=""' in cleared[0])
    assert ctx.fake_redis.data.get("auth:sess:" + sid) is None

    # The ended sign-in is gone.
    assert _mfa(ctx, sid, csrf, _code(admin, ctx)).status_code == 401
    # A new sign-in is locked too, even with the correct code.
    ctx.clock.t += 60
    sid2, csrf2, _ = _login(ctx, admin)
    r = _mfa(ctx, sid2, csrf2, _code(admin, ctx))
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) == totp_guard.ATTEMPT_WINDOW - 60

    # Refused attempts do not extend the window; after it a fresh code works.
    ctx.clock.t += totp_guard.ATTEMPT_WINDOW - 60
    sid3, csrf3, _ = _login(ctx, admin)
    assert _mfa(ctx, sid3, csrf3, _code(admin, ctx)).status_code == 200


def test_code_that_is_not_valid_unicode_counts_as_wrong(ctx):
    admin = _add_admin(ctx)
    sid, csrf, _ = _login(ctx, admin)
    for _ in range(totp_guard.MAX_ATTEMPTS - 1):
        assert _mfa(ctx, sid, csrf, _wrong(admin, ctx)).status_code == 400
    _use(ctx, sid)
    # A lone surrogate as the fifth try still ends in the lockout.
    r = ctx.post(
        "/admin/api/login/mfa",
        content=b'{"code": "\\ud800"}',
        headers={"Content-Type": "application/json", "X-CSRF-Token": csrf},
    )
    assert r.status_code == 429
    assert ctx.fake_redis.data.get("auth:sess:" + sid) is None
    assert _actions(ctx, admin)[-2:] == [
        ("mfa_failed", {"where": "login", "reason": "invalid"}),
        ("mfa_locked", {"where": "login", "seconds": totp_guard.ATTEMPT_WINDOW}),
    ]


def test_lockout_ends_the_sign_in_even_if_the_log_write_fails(ctx, monkeypatch):
    admin = _add_admin(ctx)
    sid, csrf, _ = _login(ctx, admin)
    for _ in range(totp_guard.MAX_ATTEMPTS - 1):
        assert _mfa(ctx, sid, csrf, _wrong(admin, ctx)).status_code == 400

    async def broken_log(*_a, **_k):
        raise RuntimeError("activity log unavailable")

    monkeypatch.setattr(totp_guard, "log_activity", broken_log)
    r = _mfa(ctx, sid, csrf, _wrong(admin, ctx))
    assert r.status_code == 429
    assert r.headers["Retry-After"] == str(totp_guard.ATTEMPT_WINDOW)
    cleared = [h for h in r.headers.get_list("set-cookie") if h.startswith("auth_sid=")]
    assert cleared and ("Max-Age=0" in cleared[0] or 'auth_sid=""' in cleared[0])
    assert ctx.fake_redis.data.get("auth:sess:" + sid) is None


def test_counter_is_shared_by_login_and_reveals_per_admin(ctx):
    admin = _add_admin(ctx)
    other = _add_admin(ctx)
    tid, _ = _add_token(ctx)
    kid, _ = _add_api_key(ctx)
    full = _full(ctx, admin)

    assert _reveal_token(ctx, full, tid, _wrong(admin, ctx)).status_code == 400
    assert _reveal_key(ctx, full, kid, _wrong(admin, ctx)).status_code == 400
    sid, csrf, _ = _login(ctx, admin)
    assert _mfa(ctx, sid, csrf, _wrong(admin, ctx)).status_code == 400
    assert _fail_count(ctx, admin) == 3

    # Another admin is not affected.
    assert _fail_count(ctx, other) == 0
    other_full = _full(ctx, other)
    assert other_full[0]

    # A success resets the counter.
    ctx.clock.t += 30
    assert _reveal_token(ctx, full, tid, _code(admin, ctx)).status_code == 200
    assert _fail_count(ctx, admin) == 0

    # Lockout on a reveal: 429 with "Try again", the full session stays.
    for _ in range(totp_guard.MAX_ATTEMPTS - 1):
        assert _reveal_key(ctx, full, kid, _wrong(admin, ctx)).status_code == 400
    r = _reveal_key(ctx, full, kid, _wrong(admin, ctx))
    assert r.status_code == 429
    assert r.json()["detail"] == "Too many incorrect codes. Try again in 15 minutes."
    assert ctx.fake_redis.data.get("auth:sess:" + full[0]) is not None
    _use(ctx, full[0])
    assert ctx.get("/admin/api/me").json()["user"]["username"] == admin.username
    # ... and login for that admin is locked as well.
    sid, csrf, _ = _login(ctx, admin)
    assert _mfa(ctx, sid, csrf, _code(admin, ctx, steps=1)).status_code == 429


def test_activity_log_rows(ctx):
    admin = _add_admin(ctx)
    a = _login(ctx, admin)[:2]
    b = _login(ctx, admin)[:2]
    code = _code(admin, ctx)
    assert _mfa(ctx, *a, code).status_code == 200
    assert _mfa(ctx, *b, code).status_code == 400  # reused
    for _ in range(totp_guard.MAX_ATTEMPTS - 1):
        _mfa(ctx, *b, _wrong(admin, ctx))
    # Refused while locked: no row.
    sid, csrf, _ = _login(ctx, admin)
    assert _mfa(ctx, sid, csrf, _wrong(admin, ctx)).status_code == 429

    rows = _actions(ctx, admin)
    assert rows[0] == ("login", None)
    assert rows[1] == ("mfa_failed", {"where": "login", "reason": "reused"})
    assert rows[2:6] == [("mfa_failed", {"where": "login", "reason": "invalid"})] * 4
    assert rows[6] == ("mfa_locked", {"where": "login", "seconds": totp_guard.ATTEMPT_WINDOW})
    assert len(rows) == 7


def test_partial_session_lasts_five_minutes(ctx):
    admin = _add_admin(ctx)
    sid, _, r = _login(ctx, admin)
    cookie = [h for h in r.headers.get_list("set-cookie") if h.startswith("auth_sid=")][0]
    assert "Max-Age=300" in cookie
    assert ctx.fake_redis.exp["auth:sess:" + sid] == ctx.clock() + 300


def test_refused_requests_do_not_use_attempts_or_codes(ctx):
    admin = _add_admin(ctx)
    full = _full(ctx, admin)
    ctx.clock.t += 30
    code = _code(admin, ctx)

    # Wrong CSRF at login/mfa
    sid, _, _ = _login(ctx, admin)
    assert _mfa(ctx, sid, "wrong", _wrong(admin, ctx)).status_code == 403
    # API key that does not exist, and one that cannot be revealed
    assert _reveal_key(ctx, full, str(uuid.uuid4()), code).status_code == 404
    kid_old, _ = _add_api_key(ctx, revealable=False)
    assert _reveal_key(ctx, full, kid_old, code).status_code == 409
    assert _fail_count(ctx, admin) == 0

    # The code is still unused.
    kid, value = _add_api_key(ctx)
    r = _reveal_key(ctx, full, kid, code)
    assert r.status_code == 200 and r.json() == {"key": value}


# ── Helper ─────────────────────────────────────────────────────


def _helper_admin():
    return str(uuid.uuid4()), pyotp.random_base32()


def test_concurrent_checks_with_the_same_code_have_one_winner(clock):
    fake = FakeRedis(clock)
    admin_id, seed = _helper_admin()
    code = pyotp.TOTP(seed).at(int(clock()))

    async def run():
        return await asyncio.gather(
            *(totp_guard.check_totp_code(fake, admin_id, seed, code, count_attempt=False) for _ in range(8))
        )

    results = asyncio.run(run())
    assert sum(r.ok for r in results) == 1
    assert {r.reason for r in results if not r.ok} == {"reused"}


def test_concurrent_wrong_codes_get_at_most_five_checks(clock, monkeypatch):
    fake = FakeRedis(clock)
    admin_id, seed = _helper_admin()
    totp = pyotp.TOTP(seed)
    good = {totp.at(int(clock() + d)) for d in (-30, 0, 30)}
    guesses = []
    while len(guesses) < 20:
        g = "%06d" % secrets.randbelow(10**6)
        if g not in good:
            guesses.append(g)
    checked = []
    real = totp_guard._claim_code

    async def counting(*args):
        checked.append(1)
        return await real(*args)

    monkeypatch.setattr(totp_guard, "_claim_code", counting)

    async def run():
        return await asyncio.gather(*(totp_guard.check_totp_code(fake, admin_id, seed, g) for g in guesses))

    results = asyncio.run(run())
    assert len(checked) == totp_guard.MAX_ATTEMPTS
    assert sum(r.reason == "locked" for r in results) == len(guesses) - totp_guard.MAX_ATTEMPTS
    assert sum(r.locked_now for r in results) == 1


def test_stalled_claim_after_the_window_is_refused(clock, monkeypatch):
    fake = FakeRedis(clock)
    admin_id, seed = _helper_admin()
    code = pyotp.TOTP(seed).at(int(clock()))
    real_set = fake.set

    async def stalled_set(*a, **k):
        # The request stalls between the code check and the claim, past the marker TTL.
        clock.t += totp_guard.USED_STEP_TTL + 30
        return await real_set(*a, **k)

    monkeypatch.setattr(fake, "set", stalled_set)
    result = asyncio.run(totp_guard.check_totp_code(fake, admin_id, seed, code, count_attempt=False))
    assert not result.ok and result.reason == "invalid"
    # The marker stays.
    assert any(k.startswith(totp_guard.USED_PREFIX + admin_id) for k in fake.data)


def test_counter_without_ttl_gets_one(clock, monkeypatch):
    fake = FakeRedis(clock)
    admin_id, seed = _helper_admin()
    key = totp_guard.FAIL_PREFIX + admin_id
    real_execute = FakePipeline.execute

    async def execute_after_expiry(self):
        out = await real_execute(self)
        # As if the key expired between SET and INCR and INCR recreated it.
        fake.exp.pop(key, None)
        out[2] = -1
        return out

    monkeypatch.setattr(FakePipeline, "execute", execute_after_expiry)
    result = asyncio.run(totp_guard.check_totp_code(fake, admin_id, seed, "000000x"))
    assert not result.ok
    assert fake._ttl(key) == totp_guard.ATTEMPT_WINDOW


def test_retry_after_is_at_least_one_second(clock):
    fake = FakeRedis(clock)
    admin_id, seed = _helper_admin()
    key = totp_guard.FAIL_PREFIX + admin_id
    fake._set(key, totp_guard.MAX_ATTEMPTS, ex=totp_guard.ATTEMPT_WINDOW)
    clock.t += totp_guard.ATTEMPT_WINDOW - 0.4
    result = asyncio.run(totp_guard.check_totp_code(fake, admin_id, seed, "123456"))
    assert result.reason == "locked" and result.retry_after == 1
    err = totp_guard.totp_error(result, at_login=False)
    assert err.headers["Retry-After"] == "1"
    assert err.detail == "Too many incorrect codes. Try again in 1 minute."

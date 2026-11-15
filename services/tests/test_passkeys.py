"""Passkeys: registration, sign-in, step-up for token reveal, and the passkey address.

The tests drive the real HTTP routes with a software authenticator built here: an EC P-256
key, "none" attestation, and assertions signed over the bytes WebAuthn defines.  The
server side is py_webauthn, so these tests check how the service uses it: ceremonies,
single use, user verification, user handles, the address and its changes, and the stored
passkey state.

Redis is an in-memory fake with the commands the routes use (SET NX with expiry, and the
transaction of the sign-in limit).  Every secret the app reads is generated per run with
``secrets``; every key and TOTP secret is generated per test.

The database is a temporary SQLite file, or, when ``TEST_DATABASE_URL`` is set, that
PostgreSQL database: point it only at a throwaway database used by one test run at a time.
Each test DELETES EVERY ROW of admin_users, passkeys and passkey_settings in it.

Requires pytest and httpx.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import secrets
import struct
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_TMP_DIR = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
_DB_URL = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{_TMP_DIR.name}/passkeys.db"

os.environ.update({
    "ADMIN_API_KEY": secrets.token_urlsafe(32),
    "SESSION_SECRET": secrets.token_urlsafe(48),
    "APP_ENCRYPTION_KEY": secrets.token_hex(16),
})
os.environ.setdefault("REDIS_URL", f"redis://:{secrets.token_hex(32)}@127.0.0.1:6379/0")
os.environ.setdefault("DATABASE_URL", _DB_URL)
os.environ.pop("PASSKEY_ORIGIN", None)

import cbor2  # noqa: E402
import pyotp  # noqa: E402
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

ORIGIN = "https://auth.example.com"
OTHER_ORIGIN = "https://login.example.org"


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ── Software authenticator ──────────────────────────────────────

class Authenticator:
    """One discoverable passkey.  count_step 0: a synced passkey that always reports 0."""

    def __init__(self, count_step: int = 0) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = secrets.token_bytes(32)
        self.count = 0
        self.count_step = count_step
        self.user_handle: bytes | None = None

    def _cose(self) -> bytes:
        n = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})

    @staticmethod
    def _flags(uv: bool, attested: bool = False) -> int:
        return 0x01 | (0x04 if uv else 0) | 0x08 | 0x10 | (0x40 if attested else 0)

    def create(self, public_key: dict, origin: str = ORIGIN, uv: bool = True, cose: bytes | None = None) -> dict:
        self.user_handle = unb64u(public_key["user"]["id"])
        rp_id = public_key["rp"]["id"]
        client = json.dumps({"type": "webauthn.create", "challenge": public_key["challenge"], "origin": origin}).encode()
        auth = (
            hashlib.sha256(rp_id.encode()).digest()
            + bytes([self._flags(uv, attested=True)])
            + struct.pack(">I", self.count)
            + bytes(16)
            + struct.pack(">H", len(self.cred_id))
            + self.cred_id
            + (cose if cose is not None else self._cose())
        )
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth})
        return {
            "id": b64u(self.cred_id), "rawId": b64u(self.cred_id), "type": "public-key",
            "response": {"clientDataJSON": b64u(client), "attestationObject": b64u(att), "transports": ["internal"]},
            "clientExtensionResults": {},
        }

    def get(self, public_key: dict, origin: str = ORIGIN, uv: bool = True, handle: bytes | None | bool = True) -> dict:
        rp_id = public_key["rpId"]
        self.count += self.count_step
        client = json.dumps({"type": "webauthn.get", "challenge": public_key["challenge"], "origin": origin}).encode()
        auth = hashlib.sha256(rp_id.encode()).digest() + bytes([self._flags(uv)]) + struct.pack(">I", self.count)
        sig = self.key.sign(auth + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        user_handle = self.user_handle if handle is True else (None if handle in (False, None) else handle)
        response = {"clientDataJSON": b64u(client), "authenticatorData": b64u(auth), "signature": b64u(sig)}
        if user_handle is not None:
            response["userHandle"] = b64u(user_handle)
        return {"id": b64u(self.cred_id), "rawId": b64u(self.cred_id), "type": "public-key", "response": response,
                "clientExtensionResults": {}}


# ── Fake Redis ──────────────────────────────────────────────────

class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.on_set = None

    async def get(self, k):
        return self.data.get(k)

    async def setex(self, k, _ttl, v):
        self.data[k] = v

    async def set(self, k, v, nx=False, ex=None):
        if self.on_set:
            self.on_set(k)
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


# ── Environment ─────────────────────────────────────────────────

class Env:
    def __init__(self, app, db, redis):
        self.app, self.db, self.redis = app, db, redis
        self.clients = []
        self.admin_id = ""
        self.totp = ""
        self.csrf = ""

    def client(self):
        import httpx

        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")
        self.clients.append(c)
        return c

    async def signed_in(self):
        """A client with a full session of the admin."""
        from app.sessions import COOKIE_NAME, SESSION_TTL, new_csrf, new_sid, save_session

        sid, csrf = new_sid(), new_csrf()
        await save_session(self.redis, sid, {"kind": "full", "adminId": self.admin_id, "csrf": csrf}, SESSION_TTL)
        c = self.client()
        c.cookies.set(COOKIE_NAME, sid)
        c.headers["X-CSRF-Token"] = csrf
        return c

    def code(self):
        return pyotp.TOTP(self.totp).now()


async def _wipe(db):
    for table in ("passkeys", "passkey_settings", "activity_logs", "admin_users"):
        await db.execute(f"DELETE FROM {table}")


@contextlib.asynccontextmanager
async def _env(monkeypatch=None, env_origin: str | None = None):
    from app.config import get_settings
    from app.crypto_totp import encrypt_totp_secret
    from app.db import create_database
    from app.main import app
    from app.schema import ensure_database_schema

    # The settings object is shared with other test modules: change one field, then restore it.
    settings = get_settings()
    saved_origin = settings.passkey_origin
    settings.passkey_origin = env_origin or ""
    saved = {n: getattr(app.state, n, None) for n in ("db", "redis", "login_redis")}
    db = await create_database(_DB_URL)
    e = None
    try:
        await ensure_database_schema(db)
        await _wipe(db)
        redis = FakeRedis()
        app.state.db, app.state.redis, app.state.login_redis = db, redis, redis
        e = Env(app, db, redis)
        e.admin_id = str(uuid.uuid4())
        e.totp = pyotp.random_base32()
        await db.execute(
            "INSERT INTO admin_users (id, username, password_hash, totp_secret_enc, totp_enabled) VALUES ($1, $2, $3, $4, $5)",
            e.admin_id, "admin", "unused", encrypt_totp_secret(e.totp), True,
        )
        yield e
    finally:
        with contextlib.suppress(Exception):
            await _wipe(db)
        if e is not None:
            for c in e.clients:
                await c.aclose()
        await db.close()
        for n, v in saved.items():
            setattr(app.state, n, v)
        settings.passkey_origin = saved_origin


def run(coro):
    return asyncio.run(coro)


async def register(e: Env, c, auth: Authenticator, *, step_up: dict | None = None, origin=ORIGIN, name="Laptop"):
    body = step_up if step_up is not None else {"code": e.code(), "proposedOrigin": origin}
    res = await c.post("/admin/api/passkeys/options", json=body)
    assert res.status_code == 200, res.text
    opts = res.json()
    res = await c.post("/admin/api/passkeys", json={
        "name": name, "credential": auth.create(opts["publicKey"], origin=origin), "ceremony": opts["ceremony"],
    })
    return res


async def login_options(c):
    res = await c.post("/admin/api/login/passkey/options")
    assert res.status_code == 200, res.text
    return res.json()


async def login(c, auth: Authenticator, opts=None, **kw):
    opts = opts or await login_options(c)
    return await c.post("/admin/api/login/passkey", json={
        "credential": auth.get(opts["publicKey"], **kw), "ceremony": opts["ceremony"],
    })


async def stepup(c, auth: Authenticator, **kw) -> dict:
    res = await c.post("/admin/api/passkeys/stepup/options")
    assert res.status_code == 200, res.text
    opts = res.json()
    return {"passkey": auth.get(opts["publicKey"], **kw), "ceremony": opts["ceremony"]}


async def setup_passkey(e: Env, count_step: int = 0) -> tuple[Authenticator, object]:
    c = await e.signed_in()
    auth = Authenticator(count_step)
    res = await register(e, c, auth)
    assert res.status_code == 200, res.text
    return auth, c


async def actions(db) -> list[tuple[str, dict]]:
    rows = await db.fetch("SELECT action, details FROM activity_logs ORDER BY created_at, id")
    return [(r["action"], json.loads(r["details"]) if r["details"] else {}) for r in rows]


# ── Address validation ──────────────────────────────────────────

@pytest.mark.parametrize("value, expected", [
    ("https://Auth.Example.com", "https://auth.example.com"),
    ("https://auth.example.com/", "https://auth.example.com"),
    ("https://auth.example.com:443", "https://auth.example.com"),
    ("https://auth.example.com:8443", "https://auth.example.com:8443"),
    ("http://localhost:8080", "http://localhost:8080"),
    ("  https://xn--bcher-kva.example  ", "https://xn--bcher-kva.example"),
])
def test_address_canonical_form(value, expected):
    from app.passkey_origin import canonical_origin

    assert canonical_origin(value) == expected


@pytest.mark.parametrize("value", [
    "", "auth.example.com", "ftp://auth.example.com", "http://auth.example.com", "https://192.0.2.1",
    "https://[2001:db8::1]", "https://10.1", "https://user:pw@auth.example.com", "https://auth.example.com/auth",
    "https://auth.example.com?x=1", "https://auth.example.com#x", "https://auth..example.com",
    "https://-auth.example.com", "https://auth.example.com:0", "https://auth.example.com:99999",
    "https://auth.example.com.", "https://bücher.example", "https://faß.de", "https://" + "a" * 300 + ".com",
])
def test_address_refused(value):
    from app.passkey_origin import InvalidOrigin, canonical_origin

    with pytest.raises(InvalidOrigin):
        canonical_origin(value)


# ── Registration and sign-in ────────────────────────────────────

def test_first_passkey_saves_the_address_and_signs_in():
    async def scenario():
        async with _env() as e:
            auth, _ = await setup_passkey(e)
            row = await e.db.fetchrow("SELECT version, origin, actor FROM passkey_settings")
            assert (row["version"], row["origin"], row["actor"]) == (1, ORIGIN, "admin")

            c = e.client()
            res = await login(c, auth)
            assert res.status_code == 200, res.text
            me = (await c.get("/admin/api/me")).json()
            assert me["user"] == {"username": "admin"}
            got = await actions(e.db)
            assert ("passkey_added", {"rpId": "auth.example.com"}) in got
            assert ("login", {"method": "passkey", "passkey": "Laptop"}) in got
            listing = (await (await e.signed_in()).get("/admin/api/passkeys")).json()
            assert listing["address"]["origin"] == ORIGIN
            [pk] = listing["passkeys"]
            assert pk["name"] == "Laptop" and pk["usable"] and pk["lastUsedAt"]
    run(scenario())


def test_sign_in_options_are_the_same_for_every_caller_and_need_an_address():
    async def scenario():
        async with _env() as e:
            res = await e.client().post("/admin/api/login/passkey/options")
            assert res.status_code == 409
            await setup_passkey(e)
            before = dict(e.redis.data)
            a, b = await login_options(e.client()), await login_options(e.client())
            assert e.redis.data == before
            assert set(a) == set(b) == {"publicKey", "ceremony"}
            assert a["publicKey"]["allowCredentials"] == [] == b["publicKey"]["allowCredentials"]
            assert a["publicKey"]["userVerification"] == "required"
            assert a["publicKey"]["rpId"] == "auth.example.com"
            assert a["publicKey"]["challenge"] != b["publicKey"]["challenge"]
    run(scenario())


def test_sign_in_without_user_verification_is_refused():
    async def scenario():
        async with _env() as e:
            auth, _ = await setup_passkey(e)
            res = await login(e.client(), auth, uv=False)
            assert res.status_code == 401
            assert ("passkey_failed", {"where": "login", "reason": "invalid"}) in await actions(e.db)
    run(scenario())


def test_registration_without_user_verification_is_refused():
    async def scenario():
        async with _env() as e:
            c = await e.signed_in()
            res = await c.post("/admin/api/passkeys/options", json={"code": e.code(), "proposedOrigin": ORIGIN})
            opts = res.json()
            res = await c.post("/admin/api/passkeys", json={
                "name": "x", "credential": Authenticator().create(opts["publicKey"], uv=False), "ceremony": opts["ceremony"],
            })
            assert res.status_code == 400
            assert await e.db.fetchval("SELECT COUNT(*) FROM passkeys") == 0
            assert await e.db.fetchval("SELECT COUNT(*) FROM passkey_settings") == 0
    run(scenario())


def test_wrong_origin_is_refused():
    async def scenario():
        async with _env() as e:
            auth, _ = await setup_passkey(e)
            res = await login(e.client(), auth, origin="https://evil.example.com")
            assert res.status_code == 401
    run(scenario())


def test_a_ceremony_is_used_once_even_concurrently():
    async def scenario():
        async with _env() as e:
            auth, _ = await setup_passkey(e)
            c = e.client()
            opts = await login_options(c)
            body = {"credential": auth.get(opts["publicKey"]), "ceremony": opts["ceremony"]}
            results = await asyncio.gather(*(e.client().post("/admin/api/login/passkey", json=body) for _ in range(4)))
            assert sorted(r.status_code for r in results) == [200, 400, 400, 400]
            again = await e.client().post("/admin/api/login/passkey", json=body)
            assert again.status_code == 400
    run(scenario())


def test_two_ceremonies_in_parallel_both_work():
    async def scenario():
        async with _env() as e:
            auth, _ = await setup_passkey(e)
            a, b = e.client(), e.client()
            oa, ob = await login_options(a), await login_options(b)
            ra, rb = await asyncio.gather(login(a, auth, oa), login(b, auth, ob))
            assert (ra.status_code, rb.status_code) == (200, 200)
    run(scenario())


def test_a_request_stalled_past_the_ceremony_is_refused(monkeypatch):
    from app import passkeys

    async def scenario():
        async with _env() as e:
            auth, _ = await setup_passkey(e)
            c = e.client()
            opts = await login_options(c)
            real_now = passkeys._now
            # The request stalls on its way to Redis until the ceremony (and the marker an
            # earlier claim would have left) is over.
            e.redis.on_set = lambda k: monkeypatch.setattr(
                passkeys, "_now", lambda: real_now() + passkeys.CEREMONY_TTL + passkeys.USED_MARKER_GRACE + 1
            ) if k.startswith("auth:webauthn-used:") else None
            res = await login(c, auth, opts)
            assert res.status_code == 400
            assert (await c.get("/admin/api/me")).json()["user"] is None
    run(scenario())


def test_expired_ceremony_is_refused(monkeypatch):
    from app import passkeys

    async def scenario():
        async with _env() as e:
            auth, _ = await setup_passkey(e)
            opts = await login_options(e.client())
            real_now = passkeys._now
            monkeypatch.setattr(passkeys, "_now", lambda: real_now() + passkeys.CEREMONY_TTL + 1)
            assert (await login(e.client(), auth, opts)).status_code == 400
    run(scenario())


def test_sign_in_needs_the_user_handle_of_the_passkeys_admin():
    async def scenario():
        async with _env() as e:
            auth, _ = await setup_passkey(e)
            assert (await login(e.client(), auth, handle=False)).status_code == 401
            assert (await login(e.client(), auth, handle=uuid.uuid4().bytes)).status_code == 401
            assert (await login(e.client(), auth)).status_code == 200
    run(scenario())


def test_unknown_passkey_is_404():
    async def scenario():
        async with _env() as e:
            await setup_passkey(e)
            res = await login(e.client(), Authenticator())
            assert res.status_code == 404
    run(scenario())


def test_counting_passkey_cannot_be_replayed_and_its_count_is_stored():
    async def scenario():
        async with _env() as e:
            auth, _ = await setup_passkey(e, count_step=1)
            assert (await login(e.client(), auth)).status_code == 200
            assert await e.db.fetchval("SELECT sign_count FROM passkeys") == 1
            auth.count -= 1  # a clone that reports an old count
            assert (await login(e.client(), auth)).status_code == 401
            assert await e.db.fetchval("SELECT sign_count FROM passkeys") == 1
    run(scenario())


def test_passkey_deleted_during_sign_in_does_not_sign_in(monkeypatch):
    from app import passkeys

    async def scenario():
        async with _env() as e:
            auth, _ = await setup_passkey(e)
            real_tx = passkeys.passkey_settings.passkey_transaction

            @contextlib.asynccontextmanager
            async def tx_after_delete(db):
                # Another request deletes the passkey after it was verified.
                await e.db.execute("DELETE FROM passkeys")
                async with real_tx(db) as tx:
                    yield tx

            monkeypatch.setattr(passkeys.passkey_settings, "passkey_transaction", tx_after_delete)
            c = e.client()
            res = await login(c, auth)
            assert res.status_code == 409
            assert (await c.get("/admin/api/me")).json()["user"] is None
    run(scenario())


def test_admin_without_mfa_cannot_sign_in_with_a_passkey():
    async def scenario():
        async with _env() as e:
            auth, _ = await setup_passkey(e)
            await e.db.execute("UPDATE admin_users SET totp_enabled = $1", False)
            c = e.client()
            assert (await login(c, auth)).status_code == 409
            assert (await c.get("/admin/api/me")).json()["user"] is None
    run(scenario())


def test_sign_in_attempts_are_limited():
    async def scenario():
        async with _env() as e:
            await setup_passkey(e)
            c = e.client()
            codes = []
            for _ in range(11):
                res = await c.post("/admin/api/login/passkey", json={"credential": {}, "ceremony": "x"})
                codes.append(res.status_code)
            assert codes[:10] == [400] * 10 and codes[10] == 429
    run(scenario())


@pytest.mark.parametrize("body", [
    {"credential": {"id": "x"}, "ceremony": "x"},
    {"credential": {"id": "!!", "rawId": "!!", "type": "public-key", "response": {}}, "ceremony": "a"},
    {"credential": "text", "ceremony": "x"},
    {"ceremony": "x"},
])
def test_malformed_sign_in_is_400_or_422(body):
    async def scenario():
        async with _env() as e:
            await setup_passkey(e)
            res = await e.client().post("/admin/api/login/passkey", json=body)
            assert res.status_code in (400, 422)
    run(scenario())


def test_malformed_response_with_a_valid_ceremony_is_400():
    async def scenario():
        async with _env() as e:
            auth, _ = await setup_passkey(e)
            c = e.client()
            opts = await login_options(c)
            good = auth.get(opts["publicKey"])
            good["response"]["clientDataJSON"] = "bm90IGpzb24"
            good["response"]["authenticatorData"] = "AA"
            res = await c.post("/admin/api/login/passkey", json={"credential": good, "ceremony": opts["ceremony"]})
            assert res.status_code in (400, 401)
            assert (await c.get("/admin/api/me")).json()["user"] is None
    run(scenario())


# ── Ceremony binding ────────────────────────────────────────────

def test_ceremonies_do_not_cross_purposes_or_sessions():
    async def scenario():
        async with _env() as e:
            auth, c = await setup_passkey(e)
            # A step-up ceremony cannot sign in.
            su = await stepup(c, auth)
            res = await e.client().post("/admin/api/login/passkey", json={"credential": su["passkey"], "ceremony": su["ceremony"]})
            assert res.status_code == 400
            # A step-up ceremony of one session does not work in another session of the same admin.
            su = await stepup(c, auth)
            other = await e.signed_in()
            token_id = await _token(e)
            res = await other.post(f"/admin/api/tokens/{token_id}/reveal", json=su)
            assert res.status_code == 400
            # A sign-in ceremony cannot confirm a reveal.
            opts = await login_options(e.client())
            res = await c.post(f"/admin/api/tokens/{token_id}/reveal",
                               json={"passkey": auth.get(opts["publicKey"]), "ceremony": opts["ceremony"]})
            assert res.status_code == 400
    run(scenario())


async def _token(e: Env) -> str:
    c = await e.signed_in()
    res = await c.post("/admin/api/tokens", json={"name": "t", "grants": [{"area": "orders", "level": "read"}]})
    assert res.status_code in (200, 201), res.text
    data = res.json()
    return data.get("id") or data["token"]["id"]


# ── Step-up ─────────────────────────────────────────────────────

def test_reveal_with_a_passkey_and_with_a_code():
    async def scenario():
        async with _env() as e:
            auth, c = await setup_passkey(e)
            token_id = await _token(e)
            res = await c.post(f"/admin/api/tokens/{token_id}/reveal", json=await stepup(c, auth, handle=False))
            assert res.status_code == 200, res.text
            assert res.json()["token"]
            assert ("token_reveal", {"method": "passkey"}) in await actions(e.db)
            # The code that confirmed the registration was used; wait for a fresh one is not
            # possible here, so a wrong code shows the code path still answers.
            res = await c.post(f"/admin/api/tokens/{token_id}/reveal", json={"code": "000000"})
            assert res.status_code in (400, 429)
            res = await c.post(f"/admin/api/tokens/{token_id}/reveal", json={})
            assert res.status_code == 400
    run(scenario())


def test_api_key_copy_with_a_passkey_and_with_a_code():
    async def scenario():
        async with _env() as e:
            auth, c = await setup_passkey(e)
            res = await c.post("/admin/api/api-keys", json={"name": "ci", "scopes": ["tokens:read"]})
            assert res.status_code == 201, res.text
            key_id, raw = res.json()["id"], res.json()["key"]
            res = await c.post(f"/admin/api/api-keys/{key_id}/reveal", json=await stepup(c, auth))
            assert res.status_code == 200, res.text
            assert res.json()["key"] == raw
            # A step-up ceremony is used once.
            body = await stepup(c, auth)
            assert (await c.post(f"/admin/api/api-keys/{key_id}/reveal", json=body)).status_code == 200
            assert (await c.post(f"/admin/api/api-keys/{key_id}/reveal", json=body)).status_code == 400
            # The code path: a code of the next time step (the one before was used to add the passkey).
            later = pyotp.TOTP(e.totp).at(int(__import__("time").time()) + 30)
            res = await c.post(f"/admin/api/api-keys/{key_id}/reveal", json={"code": later})
            assert res.status_code == 200, res.text
            assert res.json()["key"] == raw
            got = [d for a, d in await actions(e.db) if a == "api_key_reveal"]
            assert sorted(d["method"] for d in got) == ["code", "passkey", "passkey"]
    run(scenario())


def test_step_up_refuses_a_wrong_user_handle_and_another_admins_passkey():
    async def scenario():
        async with _env() as e:
            auth, c = await setup_passkey(e)
            token_id = await _token(e)
            res = await c.post(f"/admin/api/tokens/{token_id}/reveal", json=await stepup(c, auth, handle=uuid.uuid4().bytes))
            assert res.status_code == 400
            # A passkey row of another admin is never accepted for this admin.
            other_admin = str(uuid.uuid4())
            await e.db.execute(
                "INSERT INTO admin_users (id, username, password_hash, totp_enabled) VALUES ($1, $2, $3, $4)",
                other_admin, "other", "unused", False,
            )
            await e.db.execute("UPDATE passkeys SET admin_id = $1", other_admin)
            res = await c.post("/admin/api/passkeys/stepup/options")
            assert res.status_code == 409
    run(scenario())


def test_adding_a_passkey_needs_a_step_up():
    async def scenario():
        async with _env() as e:
            c = await e.signed_in()
            res = await c.post("/admin/api/passkeys/options", json={"proposedOrigin": ORIGIN})
            assert res.status_code == 400
            res = await c.post("/admin/api/passkeys/options", json={"code": "000000", "proposedOrigin": ORIGIN})
            assert res.status_code == 400
            # A second passkey can be confirmed with the first.
            res = await c.post("/admin/api/passkeys/options", json={"code": e.code(), "proposedOrigin": ORIGIN})
            opts = res.json()
            first = Authenticator()
            res = await c.post("/admin/api/passkeys", json={"name": "a", "credential": first.create(opts["publicKey"]), "ceremony": opts["ceremony"]})
            assert res.status_code == 200, res.text
            second = Authenticator()
            res = await register(e, c, second, step_up=await stepup(c, first), name="b")
            assert res.status_code == 200, res.text
            # The same authenticator cannot be registered twice.
            res = await register(e, c, second, step_up=await stepup(c, first), name="c")
            assert res.status_code == 409
    run(scenario())


def test_passkey_routes_need_a_full_session_and_csrf():
    async def scenario():
        async with _env() as e:
            from app.sessions import COOKIE_NAME, PARTIAL_TTL, new_sid, save_session

            anon = e.client()
            for method, path in (("get", "/admin/api/passkeys"), ("post", "/admin/api/passkeys/options"),
                                 ("post", "/admin/api/passkeys/stepup/options"), ("get", "/admin/api/settings/passkeys")):
                assert (await getattr(anon, method)(path)).status_code == 401
            sid = new_sid()
            await save_session(e.redis, sid, {"kind": "partial", "adminId": e.admin_id, "csrf": "c"}, PARTIAL_TTL)
            partial = e.client()
            partial.cookies.set(COOKIE_NAME, sid)
            res = await partial.post("/admin/api/passkeys/options", json={"code": e.code()}, headers={"X-CSRF-Token": "c"})
            assert res.status_code == 401
            full = await e.signed_in()
            res = await full.post("/admin/api/passkeys/options", json={"code": e.code()}, headers={"X-CSRF-Token": "wrong"})
            assert res.status_code == 403
    run(scenario())


def test_rename_and_delete():
    async def scenario():
        async with _env() as e:
            auth, c = await setup_passkey(e)
            [pk] = (await c.get("/admin/api/passkeys")).json()["passkeys"]
            res = await c.patch(f"/admin/api/passkeys/{pk['id']}", json={"name": "  Work   phone "})
            assert res.status_code == 200 and res.json()["name"] == "Work phone"
            assert (await c.patch(f"/admin/api/passkeys/{pk['id']}", json={"name": " "})).status_code == 422
            assert (await c.patch(f"/admin/api/passkeys/{pk['id']}", json={"name": "x" * 65})).status_code == 422
            assert (await c.delete(f"/admin/api/passkeys/{uuid.uuid4()}")).status_code == 404
            assert (await c.delete("/admin/api/passkeys/not-a-uuid")).status_code == 404
            assert (await c.delete(f"/admin/api/passkeys/{pk['id']}")).status_code == 200
            assert (await login(e.client(), auth)).status_code == 404
            got = [a for a, _ in await actions(e.db)]
            assert "passkey_renamed" in got and "passkey_deleted" in got
    run(scenario())


# ── The address ─────────────────────────────────────────────────

def test_address_change_during_a_ceremony_ends_it():
    async def scenario():
        async with _env() as e:
            auth, c = await setup_passkey(e)
            opts = await login_options(e.client())
            res = await c.put("/admin/api/settings/passkeys", json={"baseVersion": 1, "origin": "https://auth.example.com:8443"})
            assert res.status_code == 200, res.text
            res = await e.client().post("/admin/api/login/passkey", json={
                "credential": auth.get(opts["publicKey"]), "ceremony": opts["ceremony"]})
            assert res.status_code == 409
            # Same host, so the passkey still works at the new address.
            assert (await login(e.client(), auth, origin="https://auth.example.com:8443")).status_code == 200
    run(scenario())


def test_address_change_committed_after_verification_is_refused(monkeypatch):
    from app import passkey_settings

    async def scenario():
        async with _env() as e:
            auth, _ = await setup_passkey(e)
            real_tx = passkey_settings.passkey_transaction

            @contextlib.asynccontextmanager
            async def change_then_tx(db):
                # The address changes after the early checks and the verification, just
                # before the outcome takes its lock.
                await e.db.execute(
                    "INSERT INTO passkey_settings (version, origin, actor) VALUES ($1, $2, $3)",
                    2, OTHER_ORIGIN, "someone",
                )
                async with real_tx(db) as t:
                    yield t

            monkeypatch.setattr(passkey_settings, "passkey_transaction", change_then_tx)
            c2 = e.client()
            res = await login(c2, auth)
            assert res.status_code == 409
            assert (await c2.get("/admin/api/me")).json()["user"] is None
            assert await e.db.fetchval("SELECT last_used_at FROM passkeys") is None
    run(scenario())


def test_postgres_address_change_waits_for_an_outcome_in_progress():
    """The sequence that motivated the table lock: an outcome holds SHARE and waits on a
    passkey row that another transaction locked; an address change issued meanwhile waits
    for the outcome, so a read after the change already sees the outcome."""
    if not _DB_URL.startswith("postgres"):
        pytest.skip("PostgreSQL only: SQLite runs one writer at a time")

    async def scenario():
        async with _env() as e:
            auth, c = await setup_passkey(e)
            async with e.db._pool.acquire() as holder:
                row_lock = holder.transaction()
                await row_lock.start()
                await holder.execute("SELECT id FROM passkeys FOR UPDATE")
                sign_in = asyncio.create_task(login(e.client(), auth))
                await asyncio.sleep(0.5)
                change = asyncio.create_task(c.put(
                    "/admin/api/settings/passkeys", json={"baseVersion": 1, "origin": OTHER_ORIGIN}))
                await asyncio.sleep(0.5)
                assert not sign_in.done() and not change.done()
                await row_lock.commit()
            res_in, res_change = await asyncio.wait_for(asyncio.gather(sign_in, change), 10)
            assert res_in.status_code == 200, res_in.text
            assert res_change.status_code == 200, res_change.text
            assert await e.db.fetchval("SELECT last_used_at FROM passkeys") is not None
            # And afterwards the old address is refused.
            assert (await login(e.client(), auth)).status_code in (404, 409)
    run(scenario())


def test_first_registration_refused_when_the_address_was_set_meanwhile():
    async def scenario():
        async with _env() as e:
            c = await e.signed_in()
            res = await c.post("/admin/api/passkeys/options", json={"code": e.code(), "proposedOrigin": ORIGIN})
            opts = res.json()
            assert opts["origin"] == ORIGIN
            res = await c.put("/admin/api/settings/passkeys", json={"baseVersion": 0, "origin": OTHER_ORIGIN})
            assert res.status_code == 200, res.text
            res = await c.post("/admin/api/passkeys", json={
                "name": "x", "credential": Authenticator().create(opts["publicKey"]), "ceremony": opts["ceremony"]})
            assert res.status_code == 409
            assert await e.db.fetchval("SELECT COUNT(*) FROM passkeys") == 0
            assert await e.db.fetchval("SELECT MAX(version) FROM passkey_settings") == 1
    run(scenario())


def test_passkey_of_another_address_is_not_offered_or_accepted():
    async def scenario():
        async with _env() as e:
            auth, c = await setup_passkey(e)
            res = await c.put("/admin/api/settings/passkeys", json={"baseVersion": 1, "origin": OTHER_ORIGIN})
            assert res.status_code == 200
            [pk] = (await c.get("/admin/api/passkeys")).json()["passkeys"]
            assert pk["usable"] is False
            assert (await c.post("/admin/api/passkeys/stepup/options")).status_code == 409
            assert (await login(e.client(), auth, origin=OTHER_ORIGIN)).status_code == 404
    run(scenario())


def test_address_settings_page():
    async def scenario():
        async with _env() as e:
            c = await e.signed_in()
            view = (await c.get("/admin/api/settings/passkeys")).json()
            assert view["origin"] is None and view["locked"] is False and view["version"] == 0
            res = await c.put("/admin/api/settings/passkeys", json={"baseVersion": 0, "origin": "https://192.0.2.1"})
            assert res.status_code == 422
            res = await c.put("/admin/api/settings/passkeys", json={"baseVersion": 0, "origin": "HTTPS://Auth.Example.com:443/"})
            assert res.status_code == 200 and res.json()["origin"] == ORIGIN
            res = await c.put("/admin/api/settings/passkeys", json={"baseVersion": 0, "origin": OTHER_ORIGIN})
            assert res.status_code == 409
            res = await c.put("/admin/api/settings/passkeys", json={"baseVersion": 1, "origin": ""})
            assert res.status_code == 200 and res.json()["origin"] is None
            assert (await e.client().post("/admin/api/login/passkey/options")).status_code == 409
            rows = await e.db.fetch("SELECT version, origin FROM passkey_settings ORDER BY version")
            assert [(r["version"], r["origin"]) for r in rows] == [(1, ORIGIN), (2, None)]
            got = sorted((d["version"], d["to"]["origin"]) for a, d in await actions(e.db) if a == "settings.update")
            assert got == [(1, ORIGIN), (2, None)]
    run(scenario())


def test_environment_address_locks_the_field_and_wins():
    async def scenario():
        async with _env(env_origin="https://Auth.Example.com/") as e:
            c = await e.signed_in()
            view = (await c.get("/admin/api/settings/passkeys")).json()
            assert view["origin"] == ORIGIN and view["locked"] is True
            res = await c.put("/admin/api/settings/passkeys", json={"baseVersion": 0, "origin": OTHER_ORIGIN})
            assert res.status_code == 409
            # A proposed address is ignored: the environment decides.
            auth = Authenticator()
            res = await register(e, c, auth, step_up={"code": e.code(), "proposedOrigin": OTHER_ORIGIN})
            assert res.status_code == 200, res.text
            assert await e.db.fetchval("SELECT COUNT(*) FROM passkey_settings") == 0
            assert (await login(e.client(), auth)).status_code == 200
    run(scenario())


def test_empty_environment_address_is_no_override():
    async def scenario():
        async with _env(env_origin="   ") as e:
            c = await e.signed_in()
            view = (await c.get("/admin/api/settings/passkeys")).json()
            assert view["locked"] is False and view["origin"] is None
            res = await c.put("/admin/api/settings/passkeys", json={"baseVersion": 0, "origin": ORIGIN})
            assert res.status_code == 200
    run(scenario())


def test_invalid_environment_address_stops_the_start(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("PASSKEY_ORIGIN", "http://192.0.2.1:8080")
    with pytest.raises(Exception) as info:
        Settings()
    assert "PASSKEY_ORIGIN" in str(info.value)


def test_proposed_address_is_validated():
    async def scenario():
        async with _env() as e:
            c = await e.signed_in()
            for bad in ("http://auth.example.com", "https://10.0.0.5", None):
                res = await c.post("/admin/api/passkeys/options", json={"code": e.code(), "proposedOrigin": bad})
                assert res.status_code == 422
    run(scenario())


# ── Hostile or broken registrations ─────────────────────────────

def _off_curve_cose() -> bytes:
    # x and y that are not a point of P-256.
    return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: (1).to_bytes(32, "big"), -3: (2).to_bytes(32, "big")})


@pytest.mark.parametrize("cose", [cbor2.dumps({1: 2}), _off_curve_cose()], ids=["incomplete-key", "off-curve-key"])
def test_unusable_public_key_is_refused_at_registration(cose):
    async def scenario():
        async with _env() as e:
            c = await e.signed_in()
            opts = (await c.post("/admin/api/passkeys/options", json={"code": e.code(), "proposedOrigin": ORIGIN})).json()
            res = await c.post("/admin/api/passkeys", json={
                "name": "x", "credential": Authenticator().create(opts["publicKey"], cose=cose), "ceremony": opts["ceremony"]})
            assert res.status_code == 400
            assert await e.db.fetchval("SELECT COUNT(*) FROM passkeys") == 0
            assert await e.db.fetchval("SELECT COUNT(*) FROM passkey_settings") == 0
    run(scenario())


def test_stored_unusable_key_fails_sign_in_without_500():
    async def scenario():
        async with _env() as e:
            auth, _ = await setup_passkey(e)
            # A key the library cannot load, as an older version or a manual edit could leave.
            await e.db.execute("UPDATE passkeys SET public_key = $1", b64u(_off_curve_cose()))
            res = await login(e.client(), auth)
            assert res.status_code == 401
    run(scenario())


def test_a_passkey_is_never_saved_without_its_audit_entry(monkeypatch):
    from app.routers import admin_passkeys

    real = admin_passkeys.log_activity

    async def failing(db, **kw):
        if kw.get("action") == "passkey_added":
            raise RuntimeError("audit write failed")
        return await real(db, **kw)

    monkeypatch.setattr(admin_passkeys, "log_activity", failing)

    async def scenario():
        async with _env() as e:
            c = await e.signed_in()
            opts = (await c.post("/admin/api/passkeys/options", json={"code": e.code(), "proposedOrigin": ORIGIN})).json()
            auth = Authenticator()
            with pytest.raises(RuntimeError):
                await c.post("/admin/api/passkeys", json={
                    "name": "x", "credential": auth.create(opts["publicKey"]), "ceremony": opts["ceremony"]})
            assert await e.db.fetchval("SELECT COUNT(*) FROM passkeys") == 0
            assert await e.db.fetchval("SELECT COUNT(*) FROM passkey_settings") == 0
            assert await e.db.fetchval("SELECT COUNT(*) FROM activity_logs WHERE action = $1", "settings.update") == 0
    run(scenario())


def test_control_characters_in_a_name_are_refused_before_the_ceremony_is_used():
    async def scenario():
        async with _env() as e:
            c = await e.signed_in()
            opts = (await c.post("/admin/api/passkeys/options", json={"code": e.code(), "proposedOrigin": ORIGIN})).json()
            auth = Authenticator()
            credential = auth.create(opts["publicKey"])
            res = await c.post("/admin/api/passkeys", json={"name": "Key\u0000name", "credential": credential, "ceremony": opts["ceremony"]})
            assert res.status_code == 422
            res = await c.post("/admin/api/passkeys", json={"name": "Key name", "credential": credential, "ceremony": opts["ceremony"]})
            assert res.status_code == 200, res.text
            [pk] = (await c.get("/admin/api/passkeys")).json()["passkeys"]
            assert (await c.patch(f"/admin/api/passkeys/{pk['id']}", json={"name": "a\u0007b"})).status_code == 422
    run(scenario())

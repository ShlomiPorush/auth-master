"""The Redis connection (admin sessions and first-admin setup state), saved on the admin
Settings page.

Stored in the append-only table redis_settings, like app.rate_limit_settings: one row per
saved version.  Value format 1: {"format": 1, "urlEnc": str | null}, the URL encrypted with
app.crypto_tokens (AES-GCM under SESSION_SECRET and APP_ENCRYPTION_KEY).

Effective URL, in order: REDIS_URL in the environment (the field is then locked), the saved
URL, REDIS_DEFAULT_URL (set by the bundled compose file), else the service does not start.
Every value passes config._redis_url_problem.  The URL is applied when the process starts
only: the client is built once, and a saved change needs a restart of every instance.

A URL is saved only after check() passes against it.  The password never leaves the
server: views, errors and the activity log show mask_url(), and exception text (which can
echo the URL) is never returned or logged.
"""
from __future__ import annotations

import asyncio
import json
import ipaddress
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import redis.asyncio as redis_asyncio
from redis import exceptions as redis_errors
from redis.connection import parse_url

from app import login_limit, sessions, totp_guard
from app.config import GENERATE_HINT, _redis_url_problem, get_settings
from app.crypto_tokens import decrypt_token_value, encrypt_token_value
from app.db import Database, UniqueViolationError
from app.rate_limit import bucket_key

FORMAT = 1
ENV_NAME = "REDIS_URL"
DEFAULT_ENV_NAME = "REDIS_DEFAULT_URL"
ENVIRONMENT_ACTOR = "environment"
SYNC_ATTEMPTS = 5
SOCKET_TIMEOUT_SEC = 3
CHECK_TIMEOUT_SEC = 8
PROBE_TTL_SEC = 10
# Every key the service writes, in its real shape and with the commands the service sends
# for it, so an ACL that allows those keys and commands passes and one that does not fails.
# app.sessions records: the prefix + an HMAC-SHA256 hex digest; SETEX, GET, DEL.
PROBE_KEYS = (
    lambda: sessions.PREFIX + secrets.token_hex(32),
    lambda: sessions.PENDING_TOTP_PREFIX + secrets.token_hex(32),
)
# Attempt counters: MULTI, SET NX EX, INCRBY, TTL, EXEC, then EXPIRE; the service deletes
# only the MFA counter, so only that probe key is deleted (the other expires).
# app.login_limit counts per address or IPv6 /64 network; the probe uses a random network
# of 2001:db8::/32, which is reserved for documentation, so no client has it.
# app.totp_guard counts per admin id.  (make_key, delete_after)
PROBE_COUNTER_KEYS = (
    (lambda: login_limit.COUNTER_PREFIX + bucket_key(str(ipaddress.IPv6Address(
        (0x20010DB8 << 96) | (secrets.randbits(32) << 64)
    ))), False),
    (lambda: totp_guard.FAIL_PREFIX + str(uuid.uuid4()), True),
)


def _probe_marker_key() -> str:
    """The used-code marker of app.totp_guard: an admin id and a time step; SET NX EX only
    (the service lets it expire, so the probe does too)."""
    return f"{totp_guard.USED_PREFIX}{uuid.uuid4()}:{int(time.time()) // 30}"


MAX_URL_LENGTH = 4096
SAVED_NAME = "The Redis URL saved on the Settings page"


class InvalidStored(ValueError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Stored:
    version: int
    url_enc: str | None
    actor: str | None = None
    ip_address: str | None = None
    created_at: Any = None
    # The row's value is not in format 1 (it is then never used).
    invalid: bool = False


EMPTY = Stored(0, None)


@dataclass(frozen=True)
class Connection:
    url: str
    source: str  # "env" | "stored" | "default"


# ── Values ──────────────────────────────────────────────────────

def parse_value(text: str) -> str | None:
    data = json.loads(text)
    if (
        not isinstance(data, dict)
        or type(data.get("format")) is not int
        or data["format"] != FORMAT
        or set(data) != {"format", "urlEnc"}
        or not (data["urlEnc"] is None or isinstance(data["urlEnc"], str))
    ):
        raise ValueError("not in format 1")
    return data["urlEnc"]


def dump_value(url_enc: str | None) -> str:
    return json.dumps({"format": FORMAT, "urlEnc": url_enc}, sort_keys=True)


def encrypt_url(url: str) -> str:
    return encrypt_token_value(url)


def decrypt(stored: Stored) -> str | None:
    """The saved URL (None when none is saved).  InvalidStored when it cannot be read."""
    if stored.invalid:
        raise InvalidStored(
            f"{SAVED_NAME} (version {stored.version} in the table redis_settings) is not in format {FORMAT}. "
            f"Set {ENV_NAME} in the environment, which overrides it, start the service, and save the URL again."
        )
    if stored.url_enc is None:
        return None
    try:
        return decrypt_token_value(stored.url_enc)
    except Exception:
        raise InvalidStored(
            f"{SAVED_NAME} (version {stored.version} in the table redis_settings) cannot be read: "
            "SESSION_SECRET or APP_ENCRYPTION_KEY changed since it was saved. "
            f"Set {ENV_NAME} in the environment, which overrides it, start the service, and save the URL again."
        ) from None


def saved_url(stored: Stored) -> str | None:
    """The saved URL, or None when none is saved or it cannot be read."""
    try:
        return decrypt(stored)
    except InvalidStored:
        return None


def env_url() -> str | None:
    return get_settings().env_value("redis_url")


def default_url() -> str | None:
    return get_settings().env_value("redis_default_url")


def mask_url(url: str | None) -> str | None:
    """scheme://[user:****@]host:port/db (unix://[user:****@]/path[?db=n]): the URL without
    its password and without any query parameter, read the way redis-py reads it."""
    if url is None:
        return None
    try:
        parts = parse_url(url)
    except Exception:
        return "(not a valid Redis URL)"
    scheme = url.split("://", 1)[0].lower()
    user = parts.get("username")
    auth = quote(user, safe="") if user else ""
    if parts.get("password"):
        auth += ":****"
    if auth:
        auth += "@"
    if "path" in parts:
        db = parts.get("db")
        return f"unix://{auth}{parts['path']}" + (f"?db={db}" if db else "")
    host = parts.get("host") or "localhost"
    if ":" in host:
        host = f"[{host}]"
    return f"{scheme}://{auth}{host}:{parts.get('port') or 6379}/{parts.get('db') or 0}"


# ── Database ────────────────────────────────────────────────────

async def read_current(db: Database) -> Stored:
    """The newest saved version.  A value that is not in format 1 is marked, not raised:
    while REDIS_URL is set it must not stop the start."""
    row = await db.fetchrow(
        "SELECT version, value, actor, ip_address, created_at FROM redis_settings ORDER BY version DESC LIMIT 1"
    )
    if row is None:
        return EMPTY
    try:
        url_enc, invalid = parse_value(row["value"]), False
    except (TypeError, ValueError):
        url_enc, invalid = None, True
    return Stored(int(row["version"]), url_enc, row["actor"], row["ip_address"], row["created_at"], invalid)


async def insert_version(db: Database, version: int, value: str, actor: str, ip_address: str | None) -> bool:
    """Save `value` as `version`.  False when that version already exists (someone saved first)."""
    try:
        await db.execute(
            "INSERT INTO redis_settings (version, value, actor, ip_address) VALUES ($1, $2, $3, $4)",
            version, value, actor, ip_address,
        )
    except UniqueViolationError:
        return False
    return True


async def log_change(db: Database, actor: str, version: int, before: str | None, after: str, ip_address: str | None) -> None:
    from app.logger import log_activity

    await log_activity(
        db,
        actor=actor,
        action="settings.update",
        entity_type="settings",
        entity_name="redis",
        details={"version": version, "from": mask_url(before), "to": mask_url(after)},
        ip_address=ip_address,
    )


# ── The connection this process uses ────────────────────────────

def resolve(stored: Stored) -> Connection:
    """The effective URL at startup.  RuntimeError (never containing the URL) when there
    is none, or when the one to use cannot be read or is not acceptable."""
    env = env_url()
    if env is not None:
        # Already checked with the other settings (config.Settings); the stored value is not read.
        return Connection(env, "env")
    try:
        url = decrypt(stored)
    except InvalidStored as e:
        raise RuntimeError(e.message) from None
    if url is not None:
        if problem := _redis_url_problem(url, SAVED_NAME):
            raise RuntimeError(f"{problem} Or set {ENV_NAME} in the environment, which overrides it.")
        return Connection(url, "stored")
    default = default_url()
    if default is not None:
        return Connection(default, "default")
    raise RuntimeError(
        "Configuration error. The service will not start until these are fixed:\n"
        f"  - {ENV_NAME} is not set, and no Redis URL is saved on the Settings page.\n{GENERATE_HINT}"
    )


@dataclass
class State:
    connection: Connection | None = None
    # REDIS_URL is set but not saved yet: its check failed (or saves kept conflicting).
    sync_pending: bool = False


state = State()


def reset_state() -> None:
    """As before startup (tests)."""
    state.connection = None
    state.sync_pending = False


def current_choice(stored: Stored) -> Connection | None:
    """The URL a process starting now would use (None if it would not start)."""
    env = env_url()
    if env is not None:
        return Connection(env, "env")
    url = saved_url(stored)
    if url is not None:
        return Connection(url, "stored")
    default = default_url()
    return Connection(default, "default") if default is not None else None


# ── The check ───────────────────────────────────────────────────

def _category(error: BaseException) -> str:
    # AuthenticationError is a ConnectionError in redis-py: test it first.
    if isinstance(error, redis_errors.AuthenticationError):
        return "auth"
    if isinstance(error, (redis_errors.NoPermissionError, redis_errors.ReadOnlyError)):
        return "no_write"
    if isinstance(error, (redis_errors.ConnectionError, redis_errors.TimeoutError, TimeoutError, OSError)):
        return "unreachable"
    return "other"


class _Mismatch(Exception):
    pass


async def _probe(url: str) -> None:
    client = redis_asyncio.from_url(
        url, socket_connect_timeout=SOCKET_TIMEOUT_SEC, socket_timeout=SOCKET_TIMEOUT_SEC, decode_responses=True
    )
    try:
        await client.ping()
        for make_key in PROBE_KEYS:
            key = make_key()
            value = secrets.token_hex(16)
            await client.setex(key, PROBE_TTL_SEC, value)
            if await client.get(key) != value:
                raise _Mismatch()
            await client.delete(key)
        for make_key, delete_after in PROBE_COUNTER_KEYS:
            key = make_key()
            async with client.pipeline(transaction=True) as pipe:
                pipe.set(key, 0, nx=True, ex=PROBE_TTL_SEC)
                pipe.incr(key)
                pipe.ttl(key)
                _set, count, ttl = await pipe.execute()
            if count != 1 or not 0 < ttl <= PROBE_TTL_SEC:
                raise _Mismatch()
            await client.expire(key, PROBE_TTL_SEC)
            if delete_after:
                await client.delete(key)
        if not await client.set(_probe_marker_key(), 1, nx=True, ex=PROBE_TTL_SEC):
            raise _Mismatch()
    finally:
        await client.aclose()


async def check(url: str) -> str | None:
    """Run the commands the service uses, on its key prefixes, against `url`.  None when
    they all work, else a category: "unreachable", "auth", "no_write" or "other"."""
    try:
        # URL query parameters can override the client's timeouts: this bounds everything.
        async with asyncio.timeout(CHECK_TIMEOUT_SEC):
            await _probe(url)
    except Exception as e:  # only the class is used: the text can contain the URL
        return _category(e)
    return None


# ── Copying REDIS_URL into the saved setting ────────────────────

async def sync_from_environment(db: Database, log_db: Database) -> None:
    """While REDIS_URL is set, save it once its check passes, so removing the variable
    later keeps it.  Until then sync_pending is set and the reload loop calls this again."""
    env = env_url()
    if env is None:
        state.sync_pending = False
        return
    checked = False
    for attempt in range(SYNC_ATTEMPTS + 1):
        stored = await read_current(db)
        before = saved_url(stored)
        if before == env:
            state.sync_pending = False
            return
        if attempt == SYNC_ATTEMPTS:
            break
        if not checked:
            if await check(env) is not None:
                break
            checked = True
        try:
            saved = await insert_version(db, stored.version + 1, dump_value(encrypt_url(env)), ENVIRONMENT_ACTOR, None)
        except sqlite3.OperationalError:
            saved = False
        if saved:
            try:
                await log_change(log_db, ENVIRONMENT_ACTOR, stored.version + 1, before, env, None)
            except Exception as e:
                print(f"Redis URL version {stored.version + 1} saved from the environment, "
                      f"but the activity log entry failed ({type(e).__name__})")
        else:
            await asyncio.sleep(0.05 * (attempt + 1))
    state.sync_pending = True


async def retry_sync(db: Database, log_db: Database) -> None:
    """From the reload loop: retry a pending copy of REDIS_URL."""
    if not state.sync_pending:
        return
    try:
        await sync_from_environment(db, log_db)
    except Exception as e:
        print(f"Could not save REDIS_URL to the settings yet ({type(e).__name__}); retrying.")

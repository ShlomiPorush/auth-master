import base64
import hashlib
import hmac
import json
import os
import secrets
from functools import lru_cache
from typing import Any, Literal

import redis.asyncio as redis
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import Response

from app.config import get_settings

COOKIE_NAME = "auth_sid"
PREFIX = "auth:sess:"
PENDING_TOTP_PREFIX = "auth:totp_pending:"
SETUP_TTL = 15 * 60
SESSION_TTL = 12 * 60 * 60

SessionKind = Literal["setup", "partial", "full"]

# Records in Redis are bound to this installation's SESSION_SECRET: the key name is
# an HMAC of the session ID (or admin ID), and the value is AES-GCM encrypted with
# the key name as associated data. Another installation, or another Redis client
# without the secret, can neither find, read, forge nor move a record.


def new_sid() -> str:
    return secrets.token_hex(32)


def new_csrf() -> str:
    return secrets.token_hex(24)


@lru_cache(maxsize=4)
def _derived_keys(secret: str) -> tuple[bytes, bytes]:
    def derive(info: bytes) -> bytes:
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(secret.encode("utf-8"))

    return derive(b"auth-master redis key names v1"), derive(b"auth-master redis values v1")


def _keys() -> tuple[bytes, bytes]:
    return _derived_keys(get_settings().session_secret)


def _name(prefix: str, purpose: bytes, ident: str) -> str:
    # surrogatepass: any str a cookie can carry maps to a name without an error.
    msg = purpose + b"\0" + ident.encode("utf-8", "surrogatepass")
    return prefix + hmac.new(_keys()[0], msg, hashlib.sha256).hexdigest()


def _seal(name: str, plaintext: str) -> str:
    nonce = os.urandom(12)
    ct = AESGCM(_keys()[1]).encrypt(nonce, plaintext.encode("utf-8"), name.encode("ascii"))
    return base64.b64encode(nonce + ct).decode("ascii")


def _open(name: str, value: str) -> str | None:
    try:
        raw = base64.b64decode(value, validate=True)
        return AESGCM(_keys()[1]).decrypt(raw[:12], raw[12:], name.encode("ascii")).decode("utf-8")
    except (ValueError, InvalidTag):
        return None


async def _get(r: redis.Redis, name: str) -> str | None:
    try:
        value = await r.get(name)
    except UnicodeDecodeError:
        # The client decodes replies as UTF-8; another client stored other bytes here.
        return None
    if not value:
        return None
    return _open(name, value)


def _key(sid: str) -> str:
    return _name(PREFIX, b"sess", sid)


def session_record(sid: str, payload: dict[str, Any]) -> tuple[str, str]:
    """The Redis key and value that store this session."""
    name = _key(sid)
    return name, _seal(name, json.dumps(payload))


async def save_session(r: redis.Redis, sid: str, payload: dict[str, Any], ttl: int) -> None:
    name, value = session_record(sid, payload)
    await r.setex(name, ttl, value)


async def get_session(r: redis.Redis, sid: str) -> dict[str, Any] | None:
    raw = await _get(r, _key(sid))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


async def delete_session(r: redis.Redis, sid: str) -> None:
    await r.delete(_key(sid))


def _pending_totp_key(admin_id: str) -> str:
    return _name(PENDING_TOTP_PREFIX, b"totp_pending", admin_id)


async def save_pending_totp(r: redis.Redis, admin_id: str, secret: str, ttl: int) -> None:
    name = _pending_totp_key(admin_id)
    await r.setex(name, ttl, _seal(name, secret))


async def get_pending_totp(r: redis.Redis, admin_id: str) -> str | None:
    return await _get(r, _pending_totp_key(admin_id))


async def delete_pending_totp(r: redis.Redis, admin_id: str) -> None:
    await r.delete(_pending_totp_key(admin_id))


def cookie_params(max_age_sec: int) -> dict[str, Any]:
    s = get_settings()
    return {
        "key": COOKIE_NAME,
        "httponly": True,
        "secure": s.cookie_secure,
        "samesite": "lax",
        "max_age": max_age_sec,
        "path": "/",
    }


def set_sid_cookie(response: Response, sid: str, max_age_sec: int) -> None:
    response.set_cookie(value=sid, **cookie_params(max_age_sec))


def clear_sid_cookie(response: Response) -> None:
    s = get_settings()
    response.delete_cookie(COOKIE_NAME, path="/", secure=s.cookie_secure, samesite="lax")

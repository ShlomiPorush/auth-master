import json
import secrets
from typing import Any, Literal

import redis.asyncio as redis
from fastapi import Response

from app.config import get_settings

COOKIE_NAME = "auth_sid"
PREFIX = "auth:sess:"
SETUP_TTL = 15 * 60
SESSION_TTL = 12 * 60 * 60

SessionKind = Literal["setup", "partial", "full"]


def new_sid() -> str:
    return secrets.token_hex(32)


def new_csrf() -> str:
    return secrets.token_hex(24)


def _key(sid: str) -> str:
    return PREFIX + sid


async def save_session(r: redis.Redis, sid: str, payload: dict[str, Any], ttl: int) -> None:
    await r.setex(_key(sid), ttl, json.dumps(payload))


async def get_session(r: redis.Redis, sid: str) -> dict[str, Any] | None:
    raw = await r.get(_key(sid))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


async def delete_session(r: redis.Redis, sid: str) -> None:
    await r.delete(_key(sid))


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

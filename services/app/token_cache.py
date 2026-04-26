import json
from typing import Any

import redis.asyncio as redis

from app.config import get_settings


def cache_key(token_hash_hex: str) -> str:
    return f"validate:{token_hash_hex}"


async def cache_get(r: redis.Redis, token_hash_hex: str) -> dict[str, Any] | None:
    raw = await r.get(cache_key(token_hash_hex))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


async def cache_set(r: redis.Redis, token_hash_hex: str, row: dict[str, Any]) -> None:
    ttl = get_settings().validate_cache_ttl_sec
    await r.setex(cache_key(token_hash_hex), ttl, json.dumps(row))


async def cache_delete(r: redis.Redis, token_hash_hex: str) -> None:
    await r.delete(cache_key(token_hash_hex))

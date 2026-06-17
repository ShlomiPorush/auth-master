import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from redis.asyncio import Redis

from app.crypto_tokens import sha256_hex
from app.datetime_utils import is_expired
from app.grants import grant_covers, parse_grants
from app.rate_limit import allow_validate
from app.token_cache import cache_delete, cache_get, cache_set
from app.logger import log_access


router = APIRouter(tags=["validate"])


class ValidateBody(BaseModel):
    token: str
    area: str
    level: str


@router.post("/validate")
async def validate_token(request: Request, body: ValidateBody):
    return await _validate_token_values(request, body.token, body.area, body.level)


@router.get("/validate")
async def validate_token_get(
    request: Request,
    token: str = Query(...),
    area: str = Query(...),
    level: str = Query(...),
):
    return await _validate_token_values(request, token, area, level)


async def _validate_token_values(request: Request, token: str, area: str, level: str):
    ip = request.client.host if request.client else "unknown"
    if not allow_validate(ip):
        return JSONResponse(content={"result": False}, media_type="application/json")

    if level not in ("read", "readwrite", "write", "delete", "all"):
        return JSONResponse(content={"result": False}, media_type="application/json")

    h = sha256_hex(token)
    db = request.app.state.db
    r: Redis = request.app.state.redis

    row = await cache_get(r, h)
    if row is None:
        rec = await db.fetchrow(
            "SELECT id, name, grants, expires_at, is_active FROM tokens WHERE token_hash = $1",
            h,
        )
        if not rec:
            await log_access(db, None, None, area, level, False, ip)
            return JSONResponse(content={"result": False}, media_type="application/json")
        g = rec["grants"]
        if isinstance(g, str):
            g = json.loads(g)
        elif isinstance(g, (bytes, memoryview)):
            g = json.loads(bytes(g).decode())
        grants = parse_grants(g)
        exp = rec["expires_at"]
        if exp and not isinstance(exp, str):
            exp = exp.isoformat()
        row = {"id": str(rec["id"]), "name": rec["name"], "grants": grants, "expires_at": exp, "is_active": rec["is_active"]}
        await cache_set(r, h, row)

    # SQLite stores booleans as 0/1
    is_active = row.get("is_active")
    if isinstance(is_active, int):
        is_active = bool(is_active)
    if not is_active or is_expired(row.get("expires_at")):
        await log_access(db, row.get("id"), row.get("name"), area, level, False, ip)
        return JSONResponse(content={"result": False}, media_type="application/json")

    ok = grant_covers(row["grants"], area.strip(), level)  # type: ignore[arg-type]
    if ok:
        await db.execute("UPDATE tokens SET last_used_at = now() WHERE id = $1", row["id"])

    await log_access(db, row.get("id"), row.get("name"), area, level, ok, ip)

    return JSONResponse(content={"result": ok}, media_type="application/json")

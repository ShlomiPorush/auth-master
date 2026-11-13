import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.crypto_tokens import sha256_hex
from app.datetime_utils import fmt_datetime, is_expired
from app.grants import grant_covers, parse_grants
from app.rate_limit import allow_validate, retry_after_seconds
from app.logger import log_access


router = APIRouter(tags=["validate"])


def _too_many_requests(ip: str) -> HTTPException:
    """429 for a caller over the rate limit, with the seconds until its window ends."""
    return HTTPException(
        status_code=429,
        detail="Too many requests",
        headers={"Retry-After": str(retry_after_seconds(ip))},
    )


class ValidateBody(BaseModel):
    token: str
    area: str
    level: str


class GrantsBody(BaseModel):
    token: str


_TOKEN_SELECT = "SELECT id, name, grants, expires_at, is_active FROM tokens WHERE token_hash = $1"


def _grants_from_record(rec: Any) -> list[dict[str, str]]:
    """Parse the stored grants column (JSONB, or JSON text/bytes on SQLite)."""
    g = rec["grants"]
    if isinstance(g, str):
        g = json.loads(g)
    elif isinstance(g, (bytes, memoryview)):
        g = json.loads(bytes(g).decode())
    return parse_grants(g)


def _is_active(rec: Any) -> bool:
    """True when the token is enabled and not expired.  SQLite stores booleans as 0/1."""
    return bool(rec["is_active"]) and not is_expired(rec["expires_at"])


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
        raise _too_many_requests(ip)

    if level not in ("read", "readwrite", "write", "delete", "all"):
        return JSONResponse(content={"result": False}, media_type="application/json")

    # A lone surrogate (e.g. from a JSON "\ud800" escape) is a valid Python str but
    # not UTF-8 text: it matches no stored token and cannot be written to the access
    # log.  Answer like any other rejected input, before database or logging.
    try:
        area.encode("utf-8")
        h = sha256_hex(token)
    except UnicodeEncodeError:
        return JSONResponse(content={"result": False}, media_type="application/json")
    db = request.app.state.db

    # Always read the token from the database: a revoke, rotate, grant change or delete
    # takes effect for the next request, with no cache entry that could outlive it.
    rec = await db.fetchrow(_TOKEN_SELECT, h)
    if not rec:
        await log_access(db, None, None, area, level, False, ip)
        return JSONResponse(content={"result": False}, media_type="application/json")
    grants = _grants_from_record(rec)
    exp = rec["expires_at"]
    if exp and not isinstance(exp, str):
        exp = exp.isoformat()
    row = {"id": str(rec["id"]), "name": rec["name"], "grants": grants, "expires_at": exp, "is_active": rec["is_active"]}

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


@router.post("/validate/grants")
async def token_grants(request: Request, body: GrantsBody):
    """Return the grants of the presented token to its holder.

    Reads the database on every request, like ``/validate``, so a revoked, deleted
    or rotated token is reported inactive immediately.  Unknown, inactive and expired
    tokens get the same answer.  Shares the per-IP rate limit with ``/validate``.
    """
    ip = request.client.host if request.client else "unknown"
    if not allow_validate(ip):
        raise _too_many_requests(ip)

    try:
        token_hash = sha256_hex(body.token)
    except UnicodeEncodeError as e:  # e.g. a lone surrogate from a JSON \uD800 escape
        raise HTTPException(status_code=422, detail="Token must be valid UTF-8 text") from e

    db = request.app.state.db
    rec = await db.fetchrow(_TOKEN_SELECT, token_hash)
    active = rec is not None and _is_active(rec)
    await log_access(
        db,
        str(rec["id"]) if rec else None,
        rec["name"] if rec else None,
        "*",
        "grants",
        active,
        ip,
    )
    if not active:
        return {"active": False}
    return {
        "active": True,
        "grants": _grants_from_record(rec),
        "expiresAt": fmt_datetime(rec["expires_at"]),
    }

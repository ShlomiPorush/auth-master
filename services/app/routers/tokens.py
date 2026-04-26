import json
import re
import uuid as uuid_mod
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from redis.asyncio import Redis

from app.crypto_tokens import encrypt_token_value, generate_raw_token, sha256_hex
from app.db import UniqueViolationError
from app.deps import require_admin_api_key, require_scope
from app.grants import assert_grants_allowed, parse_grants
from app.token_cache import cache_delete
from app.zones import allowed_area_names

router = APIRouter(prefix="/tokens", tags=["tokens"], dependencies=[Depends(require_admin_api_key)])

_ZONE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:\-]{0,62}$")


class TokenCreate(BaseModel):
    name: str
    grants: list[dict[str, Any]]
    expiresAt: str | None = None
    token: str | None = None


class TokenPatch(BaseModel):
    name: str | None = None
    token: str | None = None
    grants: list[dict[str, Any]] | None = None
    isActive: bool | None = None
    expiresAt: str | None = None


async def _normalize_grants(request: Request, raw: list[dict[str, Any]]) -> list[dict[str, str]]:
    db = request.app.state.db
    allowed = await allowed_area_names(db)
    grants = parse_grants(raw)
    if not grants:
        raise HTTPException(400, "grants required")
    if not allowed:
        raise HTTPException(400, "Define at least one zone (or set ALLOWED_AREAS in env when zones table is empty)")
    try:
        assert_grants_allowed(grants, allowed)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return grants



@router.get("/ping")
async def ping():
    """Simple connectivity check — validates the API key without requiring any scope."""
    return {"ok": True}


@router.get("/zones", dependencies=[require_scope("zones:read")])
async def list_zones(request: Request):
    """Lightweight zone list for API consumers (e.g. n8n node)."""
    db = request.app.state.db
    names = await allowed_area_names(db)
    return [{"name": n} for n in names]


class ZoneCreateApi(BaseModel):
    name: str
    description: str = ""


@router.post("/zones", status_code=201, dependencies=[require_scope("zones:write")])
async def create_zone_api(request: Request, body: ZoneCreateApi):
    """Create a zone via API key (requires zones:write scope)."""
    name = body.name.strip()
    if not name or not _ZONE_NAME_RE.match(name):
        raise HTTPException(400, "Invalid zone name")
    db = request.app.state.db
    new_id = str(uuid_mod.uuid4())
    desc = (body.description or "").strip()
    try:
        if db.is_sqlite:
            await db.execute(
                "INSERT INTO zones (id, name, description) VALUES ($1, $2, $3)",
                new_id, name, desc,
            )
        else:
            new_id = await db.fetchval(
                "INSERT INTO zones (name, description) VALUES ($1, $2) RETURNING id",
                name, desc,
            )
    except UniqueViolationError as e:
        raise HTTPException(409, "Zone already exists") from e
    return {"id": str(new_id), "name": name, "description": desc}


@router.get("", dependencies=[require_scope("tokens:read")])
async def list_tokens(request: Request):
    db = request.app.state.db
    rows = await db.fetch(
        "SELECT id, name, grants, expires_at, is_active, created_at, last_used_at FROM tokens ORDER BY created_at DESC"
    )
    out = []
    for row in rows:
        g = row["grants"]
        if isinstance(g, str):
            g = json.loads(g)
        out.append(
            {
                "id": str(row["id"]),
                "name": row["name"],
                "grants": g,
                "expiresAt": row["expires_at"] if isinstance(row["expires_at"], str) else (row["expires_at"].isoformat() if row["expires_at"] else None),
                "isActive": bool(row["is_active"]),
                "createdAt": row["created_at"] if isinstance(row["created_at"], str) else (row["created_at"].isoformat() if row["created_at"] else None),
                "lastUsedAt": row["last_used_at"] if isinstance(row["last_used_at"], str) else (row["last_used_at"].isoformat() if row["last_used_at"] else None),
            }
        )
    return out


@router.post("", status_code=201, dependencies=[require_scope("tokens:write")])
async def create_token(request: Request, body: TokenCreate):
    db = request.app.state.db
    grants = await _normalize_grants(request, body.grants)
    raw = body.token.strip() if body.token and body.token.strip() else generate_raw_token()
    th = sha256_hex(raw)
    exp = None
    if body.expiresAt:
        try:
            exp = datetime.fromisoformat(body.expiresAt.replace("Z", "+00:00"))
        except ValueError as e:
            raise HTTPException(400, "invalid expiresAt") from e
    new_id = str(uuid_mod.uuid4())
    try:
        if db.is_sqlite:
            await db.execute(
                "INSERT INTO tokens (id, name, token_hash, token_enc, grants, expires_at) VALUES ($1, $2, $3, $4, $5, $6)",
                new_id, body.name.strip(), th, encrypt_token_value(raw), json.dumps(grants), exp,
            )
            rid = new_id
        else:
            rid = await db.fetchval(
                "INSERT INTO tokens (name, token_hash, token_enc, grants, expires_at) VALUES ($1, $2, $3, $4::jsonb, $5) RETURNING id",
                body.name.strip(), th, encrypt_token_value(raw), json.dumps(grants), exp,
            )
    except UniqueViolationError as e:
        raise HTTPException(409, "Token already exists") from e
    return {
        "id": str(rid),
        "token": raw,
        "name": body.name.strip(),
        "grants": grants,
        "expiresAt": exp.isoformat() if exp else None,
    }


@router.patch("/{token_id}", dependencies=[require_scope("tokens:write")])
async def patch_token(request: Request, token_id: str, body: TokenPatch):
    db = request.app.state.db
    r: Redis = request.app.state.redis
    row = await db.fetchrow("SELECT token_hash FROM tokens WHERE id = $1", token_id)
    if not row:
        raise HTTPException(404, "Not found")
    th_old = row["token_hash"]
    parts: list[str] = []
    args: list[Any] = []
    n = 1
    if body.name is not None:
        nm = body.name.strip()
        if not nm:
            raise HTTPException(400, "name required")
        parts.append(f"name = ${n}")
        args.append(nm)
        n += 1
    if body.token is not None:
        tv = body.token.strip()
        if not tv:
            raise HTTPException(400, "token required")
        parts.append(f"token_hash = ${n}")
        args.append(sha256_hex(tv))
        n += 1
        parts.append(f"token_enc = ${n}")
        args.append(encrypt_token_value(tv))
        n += 1
    if body.grants is not None:
        grants_json = json.dumps(await _normalize_grants(request, body.grants))
        if db.is_pg:
            parts.append(f"grants = ${n}::jsonb")
        else:
            parts.append(f"grants = ${n}")
        args.append(grants_json)
        n += 1
    if body.isActive is not None:
        parts.append(f"is_active = ${n}")
        args.append(bool(body.isActive))
        n += 1
    if body.expiresAt is not None:
        if body.expiresAt == "":
            parts.append("expires_at = NULL")
        else:
            try:
                dt = datetime.fromisoformat(body.expiresAt.replace("Z", "+00:00"))
            except ValueError as e:
                raise HTTPException(400, "invalid expiresAt") from e
            parts.append(f"expires_at = ${n}")
            args.append(dt)
            n += 1
    if not parts:
        raise HTTPException(400, "no updates")
    args.append(token_id)
    q = f"UPDATE tokens SET {', '.join(parts)} WHERE id = ${n}"
    try:
        await db.execute(q, *args)
    except UniqueViolationError as e:
        raise HTTPException(409, "Token already exists") from e
    await cache_delete(r, th_old)
    if body.token is not None:
        await cache_delete(r, sha256_hex(body.token.strip()))
    return {"ok": True}


@router.delete("/{token_id}", dependencies=[require_scope("tokens:write")])
async def delete_token(request: Request, token_id: str):
    db = request.app.state.db
    r: Redis = request.app.state.redis
    row = await db.fetchrow("SELECT token_hash FROM tokens WHERE id = $1", token_id)
    if not row:
        raise HTTPException(404, "Not found")
    th = row["token_hash"]
    await db.execute("DELETE FROM tokens WHERE id = $1", token_id)
    await cache_delete(r, th)
    return {"ok": True}

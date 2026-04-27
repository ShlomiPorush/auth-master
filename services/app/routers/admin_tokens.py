import json
import uuid as uuid_mod
from datetime import datetime
from typing import Any

from app.datetime_utils import fmt_datetime

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel
import pyotp
from redis.asyncio import Redis

from app.admin_deps import FullSession, csrf_check
from app.crypto_tokens import decrypt_token_value, encrypt_token_value, generate_raw_token, sha256_hex
from app.crypto_totp import decrypt_totp_secret
from app.db import UniqueViolationError
from app.grants import assert_grants_allowed, parse_grants
from app.token_cache import cache_delete
from app.zones import allowed_area_names

router = APIRouter(prefix="/admin/api", tags=["admin-tokens"])


class TokenCreate(BaseModel):
    name: str
    grants: list[dict[str, Any]]
    description: str = ""
    expiresAt: str | None = None
    token: str | None = None


class TokenPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    token: str | None = None
    grants: list[dict[str, Any]] | None = None
    isActive: bool | None = None
    expiresAt: str | None = None


class RevealBody(BaseModel):
    code: str


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



@router.get("/tokens")
async def admin_list_tokens(request: Request, _sess: FullSession):
    db = request.app.state.db
    rows = await db.fetch(
        "SELECT id, name, description, token_enc, grants, expires_at, is_active, created_at, last_used_at FROM tokens ORDER BY created_at DESC"
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
                "description": row["description"] or "",
                "grants": g,
                "expiresAt": fmt_datetime(row["expires_at"]),
                "isActive": bool(row["is_active"]),
                "canReveal": bool(row["token_enc"]),
                "createdAt": fmt_datetime(row["created_at"]),
                "lastUsedAt": fmt_datetime(row["last_used_at"]),
            }
        )
    return out


@router.post("/tokens", status_code=201)
async def admin_create_token(
    request: Request,
    sess: FullSession,
    body: TokenCreate,
    x_csrf_token: str | None = Header(None, alias="X-CSRF-Token"),
):
    csrf_check(sess, x_csrf_token)
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
                "INSERT INTO tokens (id, name, description, token_hash, token_enc, grants, expires_at) VALUES ($1, $2, $3, $4, $5, $6, $7)",
                new_id, body.name.strip(), (body.description or "").strip(), th, encrypt_token_value(raw), json.dumps(grants), exp,
            )
            rid = new_id
        else:
            rid = await db.fetchval(
                "INSERT INTO tokens (name, description, token_hash, token_enc, grants, expires_at) VALUES ($1, $2, $3, $4, $5::jsonb, $6) RETURNING id",
                body.name.strip(), (body.description or "").strip(), th, encrypt_token_value(raw), json.dumps(grants), exp,
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


@router.patch("/tokens/{token_id}")
async def admin_patch_token(
    request: Request,
    sess: FullSession,
    token_id: str,
    body: TokenPatch,
    x_csrf_token: str | None = Header(None, alias="X-CSRF-Token"),
):
    csrf_check(sess, x_csrf_token)
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
    if body.description is not None:
        parts.append(f"description = ${n}")
        args.append(body.description.strip())
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


@router.delete("/tokens/{token_id}")
async def admin_delete_token(
    request: Request,
    sess: FullSession,
    token_id: str,
    x_csrf_token: str | None = Header(None, alias="X-CSRF-Token"),
):
    csrf_check(sess, x_csrf_token)
    db = request.app.state.db
    r: Redis = request.app.state.redis
    row = await db.fetchrow("SELECT token_hash FROM tokens WHERE id = $1", token_id)
    if not row:
        raise HTTPException(404, "Not found")
    th = row["token_hash"]
    await db.execute("DELETE FROM tokens WHERE id = $1", token_id)
    await cache_delete(r, th)
    return {"ok": True}


@router.post("/tokens/{token_id}/reveal")
async def admin_reveal_token(
    request: Request,
    sess: FullSession,
    token_id: str,
    body: RevealBody,
    x_csrf_token: str | None = Header(None, alias="X-CSRF-Token"),
):
    csrf_check(sess, x_csrf_token)
    db = request.app.state.db
    try:
        token_uuid = uuid_mod.UUID(token_id)
    except ValueError as e:
        raise HTTPException(400, "Invalid id") from e
    admin_id = sess.get("adminId")
    if not admin_id:
        raise HTTPException(401, "Unauthorized")
    admin_row = await db.fetchrow(
        "SELECT totp_secret_enc, totp_enabled FROM admin_users WHERE id = $1",
        str(admin_id),
    )
    token_row = await db.fetchrow("SELECT token_enc FROM tokens WHERE id = $1", str(token_uuid))
    if not admin_row or not admin_row["totp_enabled"] or not admin_row["totp_secret_enc"]:
        raise HTTPException(403, "MFA not configured")
    if not token_row:
        raise HTTPException(404, "Not found")
    token_enc = token_row["token_enc"]
    if not token_enc:
        raise HTTPException(409, "Token cannot be revealed (created before encrypted storage support)")
    try:
        secret = decrypt_totp_secret(admin_row["totp_secret_enc"])
    except Exception as e:
        raise HTTPException(500, "decrypt failed") from e
    totp = pyotp.TOTP(secret)
    if not totp.verify(body.code.replace(" ", ""), valid_window=1):
        raise HTTPException(400, "Invalid code")
    try:
        token_value = decrypt_token_value(token_enc)
    except Exception as e:
        raise HTTPException(500, "Failed to decrypt token") from e
    return {"token": token_value}

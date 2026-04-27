"""Admin API Key management (requires admin session)."""
from __future__ import annotations

import secrets
import uuid as uuid_mod

import pyotp
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from app.crypto_tokens import decrypt_token_value, encrypt_token_value, sha256_hex
from app.crypto_totp import decrypt_totp_secret
from app.datetime_utils import fmt_datetime
from app.db import UniqueViolationError
from app.deps import ALL_SCOPES
from app.admin_deps import FullSession, csrf_check

router = APIRouter(prefix="/admin/api/api-keys", tags=["admin-api-keys"])


class ApiKeyCreate(BaseModel):
    name: str
    scopes: list[str]


class ApiKeyPatch(BaseModel):
    name: str | None = None
    scopes: list[str] | None = None


class RevealBody(BaseModel):
    code: str




@router.get("")
async def list_api_keys(request: Request, _sess: FullSession):
    db = request.app.state.db
    rows = await db.fetch("SELECT id, name, key_enc, scopes, created_at FROM api_keys ORDER BY created_at DESC")
    return [
        {
            "id": str(r["id"]),
            "name": r["name"],
            "scopes": r["scopes"],
            "canReveal": bool(r["key_enc"]),
            "createdAt": fmt_datetime(r["created_at"]),
        }
        for r in rows
    ]


@router.post("", status_code=201)
async def create_api_key(
    request: Request,
    sess: FullSession,
    body: ApiKeyCreate,
    x_csrf_token: str | None = Header(None, alias="X-CSRF-Token"),
):
    csrf_check(sess, x_csrf_token)
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "name required")

    # Validate scopes
    valid_scopes = [s.strip() for s in body.scopes if s.strip() in ALL_SCOPES]
    if not valid_scopes:
        raise HTTPException(400, "At least one valid scope required")

    # Generate a secure random key
    raw_key = f"ask_{secrets.token_urlsafe(32)}"
    key_hash = sha256_hex(raw_key)
    key_enc = encrypt_token_value(raw_key)
    scopes_str = ",".join(sorted(valid_scopes))

    new_id = str(uuid_mod.uuid4())
    db = request.app.state.db
    try:
        if db.is_sqlite:
            await db.execute(
                "INSERT INTO api_keys (id, name, key_hash, key_enc, scopes) VALUES ($1, $2, $3, $4, $5)",
                new_id, name, key_hash, key_enc, scopes_str,
            )
        else:
            new_id = await db.fetchval(
                "INSERT INTO api_keys (name, key_hash, key_enc, scopes) VALUES ($1, $2, $3, $4) RETURNING id",
                name, key_hash, key_enc, scopes_str,
            )
    except UniqueViolationError as e:
        raise HTTPException(409, "Key already exists") from e

    return {
        "id": str(new_id),
        "name": name,
        "key": raw_key,  # shown ONCE in the reveal modal
        "scopes": scopes_str,
    }


@router.patch("/{key_id}")
async def patch_api_key(
    request: Request,
    key_id: str,
    sess: FullSession,
    body: ApiKeyPatch,
    x_csrf_token: str | None = Header(None, alias="X-CSRF-Token"),
):
    csrf_check(sess, x_csrf_token)
    db = request.app.state.db
    row = await db.fetchrow("SELECT id FROM api_keys WHERE id = $1", key_id)
    if not row:
        raise HTTPException(404, "Not found")

    parts: list[str] = []
    args: list = []
    n = 1

    if body.name is not None:
        nm = body.name.strip()
        if not nm:
            raise HTTPException(400, "name required")
        parts.append(f"name = ${n}")
        args.append(nm)
        n += 1

    if body.scopes is not None:
        valid_scopes = [s.strip() for s in body.scopes if s.strip() in ALL_SCOPES]
        if not valid_scopes:
            raise HTTPException(400, "At least one valid scope required")
        parts.append(f"scopes = ${n}")
        args.append(",".join(sorted(valid_scopes)))
        n += 1

    if not parts:
        raise HTTPException(400, "no updates")

    args.append(key_id)
    q = f"UPDATE api_keys SET {', '.join(parts)} WHERE id = ${n}"
    await db.execute(q, *args)
    return {"ok": True}


@router.post("/{key_id}/reveal")
async def reveal_api_key(
    request: Request,
    key_id: str,
    sess: FullSession,
    body: RevealBody,
    x_csrf_token: str | None = Header(None, alias="X-CSRF-Token"),
):
    csrf_check(sess, x_csrf_token)
    db = request.app.state.db

    # Verify MFA code (same as token reveal)
    admin_id = sess.get("adminId")
    if not admin_id:
        raise HTTPException(401, "Unauthorized")
    admin_row = await db.fetchrow(
        "SELECT totp_secret_enc, totp_enabled FROM admin_users WHERE id = $1",
        str(admin_id),
    )
    if not admin_row or not admin_row["totp_enabled"] or not admin_row["totp_secret_enc"]:
        raise HTTPException(403, "MFA not configured")
    try:
        secret = decrypt_totp_secret(admin_row["totp_secret_enc"])
    except Exception as e:
        raise HTTPException(500, "decrypt failed") from e
    totp = pyotp.TOTP(secret)
    if not totp.verify(body.code.replace(" ", ""), valid_window=1):
        raise HTTPException(400, "Invalid code")

    row = await db.fetchrow("SELECT key_enc FROM api_keys WHERE id = $1", key_id)
    if not row:
        raise HTTPException(404, "Not found")
    if not row["key_enc"]:
        raise HTTPException(409, "Key cannot be revealed (created before encrypted storage support)")
    try:
        raw_key = decrypt_token_value(row["key_enc"])
    except Exception as e:
        raise HTTPException(500, "Decrypt failed") from e
    return {"key": raw_key}


@router.delete("/{key_id}")
async def delete_api_key(
    request: Request,
    key_id: str,
    sess: FullSession,
    x_csrf_token: str | None = Header(None, alias="X-CSRF-Token"),
):
    csrf_check(sess, x_csrf_token)
    db = request.app.state.db
    row = await db.fetchrow("SELECT id FROM api_keys WHERE id = $1", key_id)
    if not row:
        raise HTTPException(404, "Not found")
    await db.execute("DELETE FROM api_keys WHERE id = $1", key_id)
    return {"ok": True}

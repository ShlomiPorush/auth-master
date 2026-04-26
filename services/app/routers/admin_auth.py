import uuid as uuid_mod

import bcrypt
import pyotp
from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel
from redis.asyncio import Redis

from app.crypto_totp import decrypt_totp_secret
from app.sessions import (
    COOKIE_NAME,
    SESSION_TTL,
    clear_sid_cookie,
    delete_session,
    get_session,
    new_csrf,
    new_sid,
    save_session,
    set_sid_cookie,
)
from app.zones import allowed_area_names

router = APIRouter(prefix="/admin/api", tags=["admin-auth"])


class LoginBody(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(request: Request, response: Response, body: LoginBody):
    db = request.app.state.db
    r: Redis = request.app.state.redis
    row = await db.fetchrow(
        "SELECT id, password_hash, totp_enabled, totp_secret_enc FROM admin_users WHERE username = $1",
        body.username.strip(),
    )
    if not row:
        raise HTTPException(401, "Invalid credentials")
    stored = row["password_hash"]
    if isinstance(stored, str):
        stored = stored.encode("utf-8")
    if not bcrypt.checkpw(body.password.encode("utf-8"), stored):
        raise HTTPException(401, "Invalid credentials")
    if not row["totp_enabled"] or not row["totp_secret_enc"]:
        raise HTTPException(403, "MFA not configured")
    sid = new_sid()
    csrf = new_csrf()
    await save_session(r, sid, {"kind": "partial", "adminId": str(row["id"]), "csrf": csrf}, SESSION_TTL)
    set_sid_cookie(response, sid, SESSION_TTL)
    return {"mfaRequired": True, "csrf": csrf}


class MfaBody(BaseModel):
    code: str


@router.post("/login/mfa")
async def login_mfa(request: Request, response: Response, body: MfaBody, x_csrf_token: str | None = Header(None, alias="X-CSRF-Token")):
    db = request.app.state.db
    r: Redis = request.app.state.redis
    sid = request.cookies.get(COOKIE_NAME)
    if not sid:
        raise HTTPException(401, "No session")
    sess = await get_session(r, sid)
    if not sess or sess.get("kind") != "partial":
        raise HTTPException(401, "Invalid session")
    if not x_csrf_token or x_csrf_token != sess.get("csrf"):
        raise HTTPException(403, "CSRF")
    row = await db.fetchrow(
        "SELECT totp_secret_enc FROM admin_users WHERE id = $1 AND totp_enabled = $2",
        sess["adminId"], True,
    )
    if not row or not row["totp_secret_enc"]:
        raise HTTPException(401, "Invalid")
    try:
        secret = decrypt_totp_secret(row["totp_secret_enc"])
    except Exception as e:
        raise HTTPException(500, "decrypt failed") from e
    totp = pyotp.TOTP(secret)
    if not totp.verify(body.code.replace(" ", ""), valid_window=1):
        raise HTTPException(400, "Invalid code")
    await delete_session(r, sid)
    new_sid_v = new_sid()
    csrf = new_csrf()
    await save_session(r, new_sid_v, {"kind": "full", "adminId": sess["adminId"], "csrf": csrf}, SESSION_TTL)
    set_sid_cookie(response, new_sid_v, SESSION_TTL)
    return {"ok": True, "csrf": csrf}


@router.post("/logout")
async def logout(request: Request, response: Response):
    r: Redis = request.app.state.redis
    sid = request.cookies.get(COOKIE_NAME)
    if sid:
        await delete_session(r, sid)
    clear_sid_cookie(response)
    return {"ok": True}


@router.get("/me")
async def me(request: Request):
    db = request.app.state.db
    r: Redis = request.app.state.redis
    sid = request.cookies.get(COOKIE_NAME)
    if not sid:
        return {"user": None}
    sess = await get_session(r, sid)
    if not sess or sess.get("kind") != "full":
        return {"user": None}
    username = await db.fetchval("SELECT username FROM admin_users WHERE id = $1", sess["adminId"])
    if not username:
        return {"user": None}
    areas = await allowed_area_names(db)
    return {
        "user": {"username": username},
        "csrf": sess["csrf"],
        "allowedAreas": areas,
    }

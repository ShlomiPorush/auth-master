import uuid as uuid_mod

import bcrypt
import pyotp
from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel
from redis.asyncio import Redis

from app.config import get_settings
from app.crypto_totp import encrypt_totp_secret
from app.db import UniqueViolationError
from app.sessions import (
    COOKIE_NAME,
    SETUP_TTL,
    SESSION_TTL,
    clear_sid_cookie,
    delete_session,
    get_session,
    new_csrf,
    new_sid,
    save_session,
    set_sid_cookie,
)

router = APIRouter(prefix="/admin/api", tags=["admin-bootstrap"])

PENDING_PREFIX = "auth:totp_pending:"


def pending_key(admin_id: str) -> str:
    return PENDING_PREFIX + admin_id


async def has_completed_admin(db) -> bool:
    v = await db.fetchval("SELECT 1 FROM admin_users WHERE totp_enabled = $1 LIMIT 1", True)
    return v is not None


def check_bootstrap_header(x_bootstrap_token: str | None) -> None:
    s = get_settings().bootstrap_token
    if not s:
        return
    if x_bootstrap_token != s:
        raise HTTPException(status_code=403, detail="Invalid bootstrap token")


@router.get("/setup/status")
async def setup_status(request: Request):
    db = request.app.state.db
    needs = not await has_completed_admin(db)
    return {"needsSetup": needs}


class RegisterBody(BaseModel):
    username: str
    password: str


@router.post("/bootstrap/register")
async def bootstrap_register(
    request: Request,
    response: Response,
    body: RegisterBody,
    x_bootstrap_token: str | None = Header(None, alias="X-Bootstrap-Token"),
):
    check_bootstrap_header(x_bootstrap_token)
    db = request.app.state.db
    r: Redis = request.app.state.redis
    if await has_completed_admin(db):
        raise HTTPException(403, "Bootstrap already completed")
    if len(body.username.strip()) < 2 or len(body.password) < 8:
        raise HTTPException(400, "username (min 2) and password (min 8) required")

    pw_hash = bcrypt.hashpw(body.password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")
    secret = pyotp.random_base32()
    await db.execute("DELETE FROM admin_users WHERE totp_enabled = $1", False)
    new_id = str(uuid_mod.uuid4())
    if db.is_sqlite:
        await db.execute(
            "INSERT INTO admin_users (id, username, password_hash, totp_enabled) VALUES ($1, $2, $3, $4)",
            new_id, body.username.strip(), pw_hash, False,
        )
        admin_id = new_id
    else:
        admin_id = await db.fetchval(
            "INSERT INTO admin_users (username, password_hash, totp_enabled) VALUES ($1, $2, false) RETURNING id",
            body.username.strip(), pw_hash,
        )
    aid = str(admin_id)
    await r.setex(pending_key(aid), SETUP_TTL, secret)

    sid = new_sid()
    csrf = new_csrf()
    await save_session(r, sid, {"kind": "setup", "adminId": aid, "csrf": csrf}, SETUP_TTL)
    set_sid_cookie(response, sid, SETUP_TTL)
    return {"ok": True, "csrf": csrf}


@router.get("/bootstrap/mfa-enroll")
async def mfa_enroll(request: Request):
    db = request.app.state.db
    r: Redis = request.app.state.redis
    sid = request.cookies.get(COOKIE_NAME)
    if not sid:
        raise HTTPException(401, "No session")
    sess = await get_session(r, sid)
    if not sess or sess.get("kind") != "setup":
        raise HTTPException(401, "Invalid session")
    secret = await r.get(pending_key(sess["adminId"]))
    if not secret:
        raise HTTPException(400, "Enrollment expired; restart bootstrap")
    username = await db.fetchval("SELECT username FROM admin_users WHERE id = $1", sess["adminId"])
    if not username:
        raise HTTPException(400, "User missing")
    issuer = get_settings().totp_issuer
    totp = pyotp.TOTP(secret)
    otpauth_url = totp.provisioning_uri(name=username, issuer_name=issuer)
    return {
        "otpauthUrl": otpauth_url,
        "secretBase32": secret,
        "issuer": issuer,
        "accountName": username,
    }


class MfaVerifyBody(BaseModel):
    code: str


@router.post("/bootstrap/mfa-verify")
async def mfa_verify(request: Request, response: Response, body: MfaVerifyBody, x_csrf_token: str | None = Header(None, alias="X-CSRF-Token")):
    db = request.app.state.db
    r: Redis = request.app.state.redis
    sid = request.cookies.get(COOKIE_NAME)
    if not sid:
        raise HTTPException(401, "No session")
    sess = await get_session(r, sid)
    if not sess or sess.get("kind") != "setup":
        raise HTTPException(401, "Invalid session")
    if not x_csrf_token or x_csrf_token != sess.get("csrf"):
        raise HTTPException(403, "CSRF")
    secret = await r.get(pending_key(sess["adminId"]))
    if not secret:
        raise HTTPException(400, "Enrollment expired")
    totp = pyotp.TOTP(secret)
    if not totp.verify(body.code.replace(" ", ""), valid_window=1):
        raise HTTPException(400, "Invalid code")
    enc = encrypt_totp_secret(secret)
    await db.execute(
        "UPDATE admin_users SET totp_secret_enc = $1, totp_enabled = $2 WHERE id = $3",
        enc, True, sess["adminId"],
    )
    await r.delete(pending_key(sess["adminId"]))
    await delete_session(r, sid)
    new_sid_v = new_sid()
    csrf = new_csrf()
    await save_session(r, new_sid_v, {"kind": "full", "adminId": sess["adminId"], "csrf": csrf}, SESSION_TTL)
    set_sid_cookie(response, new_sid_v, SESSION_TTL)
    return {"ok": True, "csrf": csrf}

import unicodedata
import uuid as uuid_mod

import bcrypt
import pyotp
from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictStr
from redis.asyncio import Redis

from app import login_limit
from app.config import get_settings
from app.crypto_totp import encrypt_totp_secret
from app.db import UniqueViolationError
from app.totp_guard import check_totp_code, totp_error
from app.sessions import (
    COOKIE_NAME,
    SETUP_TTL,
    SESSION_TTL,
    clear_sid_cookie,
    delete_pending_totp,
    delete_session,
    get_pending_totp,
    get_session,
    new_csrf,
    new_sid,
    save_pending_totp,
    save_session,
    set_sid_cookie,
)

router = APIRouter(prefix="/admin/api", tags=["admin-bootstrap"])

PASSWORD_TOO_LONG = (
    "Password: use at most 72 bytes. Letters outside English, such as Hebrew, "
    "and emoji take 2 to 4 bytes each"
)

# The name authenticator apps show for the account.  TOTP_ISSUER sets it for every setup;
# otherwise the setup wizard may change it, for its own setup session only.
ISSUER_ENV_NAME = "TOTP_ISSUER"
ISSUER_MAX_LENGTH = 64


async def has_completed_admin(db) -> bool:
    v = await db.fetchval("SELECT 1 FROM admin_users WHERE totp_enabled = $1 LIMIT 1", True)
    return v is not None


@router.get("/setup/status")
async def setup_status(request: Request):
    db = request.app.state.db
    if db is None:
        # No database configured yet: setup starts with choosing one (app.db_runtime).
        return {"needsSetup": True, "needsDatabase": True}
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
):
    db = request.app.state.db
    r: Redis = request.app.state.redis
    if await has_completed_admin(db):
        raise HTTPException(403, "Bootstrap already completed")
    if len(body.username.strip()) < 2 or len(body.password) < 8:
        raise HTTPException(400, "username (min 2) and password (min 8) required")
    try:
        password = body.password.encode("utf-8")
    except UnicodeEncodeError:
        # A lone surrogate (e.g. from a JSON "\ud800" escape) has no UTF-8 encoding.
        raise HTTPException(400, "Password: contains text that is not valid Unicode") from None
    if len(password) > 72:
        # bcrypt uses only the first 72 bytes, and bcrypt 5 refuses longer input.
        raise HTTPException(400, PASSWORD_TOO_LONG)

    salt = bcrypt.gensalt(rounds=login_limit.BCRYPT_COST)
    pw_hash = (await login_limit.run_bcrypt(bcrypt.hashpw, password, salt)).decode("ascii")
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
    await save_pending_totp(r, aid, secret, SETUP_TTL)

    sid = new_sid()
    csrf = new_csrf()
    await save_session(r, sid, {"kind": "setup", "adminId": aid, "csrf": csrf}, SETUP_TTL)
    set_sid_cookie(response, sid, SETUP_TTL)
    return {"ok": True, "csrf": csrf}


def _issuer_locked() -> bool:
    return "totp_issuer" in get_settings().model_fields_set


def _issuer(sess: dict) -> str:
    if _issuer_locked():
        return get_settings().totp_issuer
    return sess.get("issuer") or get_settings().totp_issuer


def _enrollment(secret: str, username: str, issuer: str) -> dict:
    return {
        "otpauthUrl": pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer),
        "secretBase32": secret,
        "issuer": issuer,
        "accountName": username,
        "issuerLocked": _issuer_locked(),
        "issuerEnvName": ISSUER_ENV_NAME,
        "issuerMaxLength": ISSUER_MAX_LENGTH,
    }


async def _setup_session(request: Request) -> tuple[str, dict]:
    sid = request.cookies.get(COOKIE_NAME)
    if not sid:
        raise HTTPException(401, "No session")
    sess = await get_session(request.app.state.redis, sid)
    if not sess or sess.get("kind") != "setup":
        raise HTTPException(401, "Invalid session")
    return sid, sess


@router.get("/bootstrap/mfa-enroll")
async def mfa_enroll(request: Request):
    db = request.app.state.db
    r: Redis = request.app.state.redis
    _sid, sess = await _setup_session(request)
    secret = await get_pending_totp(r, sess["adminId"])
    if not secret:
        raise HTTPException(400, "Enrollment expired; restart bootstrap")
    username = await db.fetchval("SELECT username FROM admin_users WHERE id = $1", sess["adminId"])
    if not username:
        raise HTTPException(400, "User missing")
    return _enrollment(secret, username, _issuer(sess))


class IssuerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issuer: StrictStr


def _issuer_problem(issuer: str) -> str | None:
    if not issuer:
        return "Enter a name."
    if len(issuer) > ISSUER_MAX_LENGTH:
        return f"Use at most {ISSUER_MAX_LENGTH} characters."
    if any(unicodedata.category(ch) in ("Cc", "Cs") for ch in issuer):
        return "Use letters, digits, spaces and punctuation only."
    return None


@router.post("/bootstrap/issuer")
async def set_issuer(request: Request, body: IssuerBody, x_csrf_token: str | None = Header(None, alias="X-CSRF-Token")):
    """Change the name authenticator apps show, for this setup session only (nothing is
    stored outside it), and answer with the new enrollment."""
    db = request.app.state.db
    r: Redis = request.app.state.redis
    sid, sess = await _setup_session(request)
    if not x_csrf_token or x_csrf_token != sess.get("csrf"):
        raise HTTPException(403, "CSRF")
    if await has_completed_admin(db):
        raise HTTPException(403, "Bootstrap already completed")
    if _issuer_locked():
        return JSONResponse(
            status_code=409,
            content={"detail": f"The {ISSUER_ENV_NAME} environment variable sets this name, so it cannot be changed here.", "code": "locked"},
        )
    issuer = body.issuer.strip()
    if problem := _issuer_problem(issuer):
        return JSONResponse(status_code=422, content={"detail": problem, "code": "invalid", "field": "issuer"})
    username = await db.fetchval(
        "SELECT username FROM admin_users WHERE id = $1 AND totp_enabled = $2", sess["adminId"], False
    )
    if not username:
        raise HTTPException(400, "User missing")
    secret = await get_pending_totp(r, sess["adminId"])
    if not secret:
        raise HTTPException(400, "Enrollment expired; restart bootstrap")
    # Written like every session record (the Redis check on the Settings page covers the
    # commands it uses).  The pending secret keeps its own expiry, so enrollment does not last longer.
    await save_session(r, sid, {**sess, "issuer": issuer}, SETUP_TTL)
    return _enrollment(secret, username, issuer)


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
    secret = await get_pending_totp(r, sess["adminId"])
    if not secret:
        raise HTTPException(400, "Enrollment expired")
    # Recorded as used, so the enrollment code cannot also sign in or reveal
    check = await check_totp_code(r, sess["adminId"], secret, body.code.replace(" ", ""), count_attempt=False)
    if not check.ok:
        raise totp_error(check, at_login=False)
    enc = encrypt_totp_secret(secret)
    # Completes only an admin that still exists and has not enrolled yet; the
    # admin_users_one_enabled index refuses a second enabled admin.  The UPDATE's own
    # result decides, so a stale setup session (its row deleted by a later register) or
    # a concurrent enrollment cannot get a full session.
    try:
        done = await db.fetchval_write(
            "UPDATE admin_users SET totp_secret_enc = $1, totp_enabled = $2 "
            "WHERE id = $3 AND totp_enabled = $4 RETURNING id",
            enc, True, sess["adminId"], False,
        )
    except UniqueViolationError:
        done = None
    if done is None:
        await delete_pending_totp(r, sess["adminId"])
        await delete_session(r, sid)
        raise HTTPException(403, "Bootstrap already completed")
    await delete_pending_totp(r, sess["adminId"])
    await delete_session(r, sid)
    new_sid_v = new_sid()
    csrf = new_csrf()
    await save_session(r, new_sid_v, {"kind": "full", "adminId": sess["adminId"], "csrf": csrf}, SESSION_TTL)
    set_sid_cookie(response, new_sid_v, SESSION_TTL)
    return {"ok": True, "csrf": csrf}

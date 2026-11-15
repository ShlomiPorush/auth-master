"""Passkeys: sign-in without password and code, the admin's passkey list, and the passkey address."""
from __future__ import annotations

import json
import uuid as uuid_mod
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr
from webauthn import generate_registration_options, verify_registration_response
from webauthn.helpers import (
    bytes_to_base64url,
    decode_credential_public_key,
    decoded_public_key_to_cryptography,
    options_to_json_dict,
    parse_registration_credential_json,
)
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app import login_limit, passkey_settings, passkeys
from app.admin_deps import FullSession, csrf_check
from app.config import get_settings
from app.datetime_utils import fmt_datetime
from app.db import UniqueViolationError, write_transaction
from app.logger import get_actor, log_activity
from app.passkey_origin import InvalidOrigin, canonical_origin
from app.sessions import (
    COOKIE_NAME,
    SESSION_TTL,
    delete_session,
    new_csrf,
    new_sid,
    save_session,
    set_sid_cookie,
)
from app.step_up import StepUpBody, verify_step_up

router = APIRouter(prefix="/admin/api", tags=["admin-passkeys"])

NOT_CONFIGURED = "Passkeys are not set up on this service yet. Sign in with your password and authenticator code."


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def _address(request: Request) -> passkey_settings.Address:
    try:
        return await passkey_settings.read_current(request.app.state.db)
    except passkey_settings.InvalidStoredAddress as e:
        raise HTTPException(500, str(e)) from None


# ── Sign-in ─────────────────────────────────────────────────────

@router.post("/login/passkey/options")
async def login_options(request: Request):
    """Options for a discoverable passkey: the same for every caller, and nothing is stored."""
    address = await _address(request)
    if address.origin is None:
        raise HTTPException(409, NOT_CONFIGURED)
    c, token = passkeys.new_ceremony("login", address)
    return {"publicKey": passkeys.request_options(c), "ceremony": token}


class LoginBody(BaseModel):
    credential: dict[str, Any]
    ceremony: str


@router.post("/login/passkey")
async def login(request: Request, response: Response, body: LoginBody):
    await login_limit.check_attempt(request)
    c = passkeys.open_token(body.ceremony, "login")
    credential = passkeys.parse_assertion(body.credential)
    await passkeys.claim(request.app.state.redis, c)
    used = await passkeys.use_passkey(request, c, credential, session_admin=None, where="login")

    db = request.app.state.db
    r = request.app.state.redis
    username = await db.fetchval("SELECT username FROM admin_users WHERE id = $1", used.admin_id)
    await log_activity(
        db,
        actor=username or f"admin:{used.admin_id}",
        action="login",
        entity_type="admin_user",
        entity_id=used.admin_id,
        entity_name=username,
        details={"method": "passkey", "passkey": used.name},
        ip_address=_ip(request),
    )
    old_sid = request.cookies.get(COOKIE_NAME)
    if old_sid:
        await delete_session(r, old_sid)
    sid = new_sid()
    csrf = new_csrf()
    await save_session(r, sid, {"kind": "full", "adminId": used.admin_id, "csrf": csrf}, SESSION_TTL)
    set_sid_cookie(response, sid, SESSION_TTL)
    return {"ok": True, "csrf": csrf}


# ── The signed-in admin's passkeys ──────────────────────────────

def _admin_id(sess: dict) -> str:
    return str(sess["adminId"])


def _address_view(address: passkey_settings.Address) -> dict[str, Any]:
    return {
        "origin": address.origin,
        "rpId": address.rp_id,
        "version": address.version,
        "locked": address.locked,
        "envName": "PASSKEY_ORIGIN",
        "savedBy": None if address.locked else address.actor,
        "savedAt": None if address.locked else fmt_datetime(address.created_at),
    }


@router.get("/passkeys")
async def list_passkeys(request: Request, sess: FullSession):
    address = await _address(request)
    rows = await request.app.state.db.fetch(
        "SELECT id, name, rp_id, backed_up, created_at, last_used_at FROM passkeys "
        "WHERE admin_id = $1 ORDER BY created_at, id",
        _admin_id(sess),
    )
    return {
        "address": _address_view(address),
        "passkeys": [
            {
                "id": str(r["id"]),
                "name": r["name"],
                "createdAt": fmt_datetime(r["created_at"]),
                "lastUsedAt": fmt_datetime(r["last_used_at"]),
                "backedUp": bool(r["backed_up"]),
                "usable": address.rp_id is not None and r["rp_id"] == address.rp_id,
                "rpId": r["rp_id"],
            }
            for r in rows
        ],
    }


async def _usable_rows(request: Request, admin_id: str, rp_id: str) -> list[Any]:
    return await request.app.state.db.fetch(
        "SELECT credential_id, transports FROM passkeys WHERE admin_id = $1 AND rp_id = $2",
        admin_id, rp_id,
    )


@router.post("/passkeys/stepup/options")
async def stepup_options(
    request: Request, sess: FullSession, x_csrf_token: str | None = Header(None, alias="X-CSRF-Token")
):
    """Options to confirm a sensitive action with one of the admin's own passkeys."""
    csrf_check(sess, x_csrf_token)
    address = await _address(request)
    if address.origin is None:
        raise HTTPException(409, NOT_CONFIGURED)
    rows = await _usable_rows(request, _admin_id(sess), address.rp_id or "")
    if not rows:
        raise HTTPException(409, "You have no passkey for this address. Use your authenticator code.")
    c, token = passkeys.new_ceremony(
        "stepup", address, admin_id=_admin_id(sess), sid=request.cookies.get(COOKIE_NAME)
    )
    return {"publicKey": passkeys.request_options(c, rows), "ceremony": token}


class RegisterOptionsBody(StepUpBody):
    proposedOrigin: str | None = None


@router.post("/passkeys/options")
async def register_options(
    request: Request,
    sess: FullSession,
    body: RegisterOptionsBody,
    x_csrf_token: str | None = Header(None, alias="X-CSRF-Token"),
):
    """Options to create a passkey, after an authenticator code or another passkey."""
    csrf_check(sess, x_csrf_token)
    db = request.app.state.db
    admin_id = _admin_id(sess)
    address = await _address(request)
    origin = None
    if address.origin is None:
        try:
            origin = canonical_origin(body.proposedOrigin or "")
        except InvalidOrigin as e:
            raise HTTPException(422, f"Passkey address: {e}") from None
    admin_row = await db.fetchrow(
        "SELECT username, totp_secret_enc, totp_enabled FROM admin_users WHERE id = $1", admin_id
    )
    if not admin_row or not admin_row["totp_enabled"] or not admin_row["totp_secret_enc"]:
        raise HTTPException(403, "MFA not configured")
    await verify_step_up(request, admin_id, admin_row, body, "passkey_add")

    c, token = passkeys.new_ceremony(
        "register", address, origin=origin, admin_id=admin_id, sid=request.cookies.get(COOKIE_NAME)
    )
    existing = await _usable_rows(request, admin_id, c.rp_id)
    opts = generate_registration_options(
        rp_id=c.rp_id,
        rp_name=get_settings().totp_issuer,
        user_name=admin_row["username"],
        user_id=passkeys.admin_handle(admin_id),
        user_display_name=admin_row["username"],
        challenge=c.challenge,
        timeout=passkeys.PROMPT_TIMEOUT_MS,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=passkeys.descriptors(existing),
    )
    return {"publicKey": options_to_json_dict(opts), "ceremony": token, "origin": c.origin}


class RegisterBody(BaseModel):
    name: Any = None
    credential: dict[str, Any]
    ceremony: str


@router.post("/passkeys")
async def register(
    request: Request,
    sess: FullSession,
    body: RegisterBody,
    x_csrf_token: str | None = Header(None, alias="X-CSRF-Token"),
):
    csrf_check(sess, x_csrf_token)
    db = request.app.state.db
    admin_id = _admin_id(sess)
    name = passkeys.check_name(body.name)
    c = passkeys.open_token(body.ceremony, "register", admin_id=admin_id, sid=request.cookies.get(COOKIE_NAME))
    try:
        credential = parse_registration_credential_json(body.credential)
    except (WebAuthnException, ValueError, TypeError, KeyError):
        raise HTTPException(400, "The passkey response is missing or not valid.") from None
    await passkeys.claim(request.app.state.redis, c)
    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=c.challenge,
            expected_rp_id=c.rp_id,
            expected_origin=c.origin,
            require_user_verification=True,
        )
        # The library stores keys it cannot use later (such as an EC point off its curve);
        # its own helpers turn the key into a usable one here, or fail.
        decoded_public_key_to_cryptography(decode_credential_public_key(verified.credential_public_key))
    except (WebAuthnException, ValueError, KeyError, TypeError):
        # py_webauthn raises more than WebAuthnException on some malformed data or keys.
        raise HTTPException(400, "The passkey could not be created. Try again.") from None

    actor = await get_actor(request, sess)
    passkey_id = str(uuid_mod.uuid4())
    transports = [t.value for t in (credential.response.transports or [])]
    transaction = (
        passkey_settings.address_change_transaction if c.first else passkey_settings.passkey_transaction
    )
    try:
        async with transaction(db) as tx:
            current = await passkey_settings.read_current(tx)
            passkeys.check_address(c, current)
            if c.first:
                await passkey_settings.insert_version(tx, current.version + 1, c.origin, actor, _ip(request))
            if await tx.fetchval(
                "SELECT 1 FROM admin_users WHERE id = $1 AND totp_enabled = $2", admin_id, True
            ) is None:
                raise HTTPException(401, "Unauthorized")
            await tx.execute(
                "INSERT INTO passkeys (id, admin_id, credential_id, public_key, sign_count, transports, "
                "backed_up, rp_id, name) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
                passkey_id, admin_id, bytes_to_base64url(verified.credential_id),
                bytes_to_base64url(verified.credential_public_key), verified.sign_count, json.dumps(transports),
                bool(verified.credential_backed_up), c.rp_id, name,
            )
            # In the same transaction: a passkey is never usable without its audit entry.
            if c.first:
                await log_activity(
                    tx, actor=actor, action="settings.update", entity_type="settings", entity_name="passkeys",
                    details={"version": c.version + 1, "from": {"origin": None}, "to": {"origin": c.origin}},
                    ip_address=_ip(request),
                )
            await log_activity(
                tx, actor=actor, action="passkey_added", entity_type="passkey", entity_id=passkey_id,
                entity_name=name, details={"rpId": c.rp_id}, ip_address=_ip(request),
            )
    except UniqueViolationError:
        raise HTTPException(409, "This passkey is already registered.") from None
    except passkey_settings.InvalidStoredAddress as e:
        raise HTTPException(500, str(e)) from None

    return {"id": passkey_id, "name": name}


class RenameBody(BaseModel):
    name: Any = None


def _passkey_uuid(passkey_id: str) -> str:
    try:
        return str(uuid_mod.UUID(passkey_id))
    except ValueError:
        raise HTTPException(404, "Not found") from None


@router.patch("/passkeys/{passkey_id}")
async def rename_passkey(
    request: Request,
    passkey_id: str,
    sess: FullSession,
    body: RenameBody,
    x_csrf_token: str | None = Header(None, alias="X-CSRF-Token"),
):
    csrf_check(sess, x_csrf_token)
    db = request.app.state.db
    pid = _passkey_uuid(passkey_id)
    name = passkeys.check_name(body.name)
    actor = await get_actor(request, sess)
    # The change and its audit entry commit together, or neither does.
    async with write_transaction(db) as tx:
        old = await tx.fetchval("SELECT name FROM passkeys WHERE id = $1 AND admin_id = $2", pid, _admin_id(sess))
        if old is None:
            raise HTTPException(404, "Not found")
        # The row may have been deleted after the read: only a row that is still there counts.
        if await tx.fetchval(
            "UPDATE passkeys SET name = $1 WHERE id = $2 AND admin_id = $3 RETURNING id", name, pid, _admin_id(sess)
        ) is None:
            raise HTTPException(404, "Not found")
        await log_activity(
            tx, actor=actor, action="passkey_renamed", entity_type="passkey", entity_id=pid,
            entity_name=name, details={"from": old}, ip_address=_ip(request),
        )
    return {"id": pid, "name": name}


@router.delete("/passkeys/{passkey_id}")
async def delete_passkey(
    request: Request,
    passkey_id: str,
    sess: FullSession,
    x_csrf_token: str | None = Header(None, alias="X-CSRF-Token"),
):
    csrf_check(sess, x_csrf_token)
    db = request.app.state.db
    pid = _passkey_uuid(passkey_id)
    actor = await get_actor(request, sess)
    # The deletion and its audit entry commit together, or neither does.
    async with write_transaction(db) as tx:
        name = await tx.fetchval(
            "DELETE FROM passkeys WHERE id = $1 AND admin_id = $2 RETURNING name", pid, _admin_id(sess)
        )
        if name is None:
            raise HTTPException(404, "Not found")
        await log_activity(
            tx, actor=actor, action="passkey_deleted", entity_type="passkey", entity_id=pid,
            entity_name=name, ip_address=_ip(request),
        )
    return {"ok": True}


# ── The passkey address on the Settings page ────────────────────

@router.get("/settings/passkeys")
async def get_address(request: Request, _sess: FullSession):
    return _address_view(await _address(request))


class AddressBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    baseVersion: StrictInt = Field(ge=0)
    # Empty removes the address: passkeys then cannot be used until one is set again.
    origin: StrictStr


@router.put("/settings/passkeys")
async def put_address(
    request: Request,
    sess: FullSession,
    body: AddressBody,
    x_csrf_token: str | None = Header(None, alias="X-CSRF-Token"),
):
    csrf_check(sess, x_csrf_token)
    if get_settings().passkey_origin_env is not None:
        raise HTTPException(409, "The PASSKEY_ORIGIN environment variable sets this value, so it cannot be changed here.")
    origin = None
    if body.origin.strip():
        try:
            origin = canonical_origin(body.origin)
        except InvalidOrigin as e:
            raise HTTPException(422, str(e)) from None
    actor = await get_actor(request, sess)
    db = request.app.state.db
    async with passkey_settings.address_change_transaction(db) as tx:
        try:
            current = await passkey_settings.read_current(tx)
        except passkey_settings.InvalidStoredAddress:
            # A broken newest version is replaced by this save.
            current = passkey_settings.Address(None, int(await tx.fetchval(
                "SELECT COALESCE(MAX(version), 0) FROM passkey_settings"
            )), False)
        if current.version != body.baseVersion:
            raise HTTPException(409, "Someone else changed the passkey address after you opened the page. Reload and try again.")
        if current.origin == origin:
            return _address_view(current)
        await passkey_settings.insert_version(tx, current.version + 1, origin, actor, _ip(request))
        await log_activity(
            tx, actor=actor, action="settings.update", entity_type="settings", entity_name="passkeys",
            details={"version": current.version + 1, "from": {"origin": current.origin}, "to": {"origin": origin}},
            ip_address=_ip(request),
        )
    return _address_view(await _address(request))

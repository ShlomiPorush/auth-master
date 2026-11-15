"""Passkey (WebAuthn) ceremonies on top of py_webauthn.

py_webauthn builds the options and verifies the browser's responses.  This module keeps
what the library leaves to the application:

- the ceremony state: a sealed token (app.sessions.seal_ceremony) that the client sends
  back, holding the challenge, its purpose, its expiry, the passkey address and settings
  version it was made for, and, for a signed-in admin, the admin and session it belongs to;
- single use: a ceremony is claimed in Redis with SET NX before its response is verified,
  and its expiry is checked again after the claim, so a request that stalled until the
  claim marker expired is refused as well;
- the stored passkey: its sign count, backup state and last use, written in the same
  transaction that checks the passkey address (app.passkey_settings).

Passkeys are created and used with user verification required: a passkey replaces both
the password and the authenticator code, so the device must verify the person.
"""
from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import HTTPException, Request
from redis.asyncio import Redis
from webauthn import generate_authentication_options, verify_authentication_response
from webauthn.helpers import (
    base64url_to_bytes,
    bytes_to_base64url,
    generate_challenge,
    options_to_json_dict,
    parse_authentication_credential_json,
)
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticationCredential,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    UserVerificationRequirement,
)

from app import passkey_settings
from app.logger import log_activity
from app.passkey_settings import Address
from app.sessions import open_ceremony, seal_ceremony, sid_tag, webauthn_used_key

Purpose = Literal["login", "register", "stepup"]

CEREMONY_TTL = 5 * 60
# The browser prompt may stay open this long; the ceremony outlives it.
PROMPT_TIMEOUT_MS = 4 * 60 * 1000
# Kept after the ceremony's expiry, so a claim made just before it still blocks a replay.
USED_MARKER_GRACE = 60
MAX_NAME_LEN = 64

START_AGAIN = "This passkey request has expired or was already used. Start again."
ADDRESS_CHANGED = "The passkey address changed. Start again."

# The clock; tests replace it.
_now = time.time


@dataclass(frozen=True)
class Ceremony:
    purpose: Purpose
    challenge: bytes
    origin: str
    rp_id: str
    version: int
    # The address was not set when the ceremony started: a registration saves `origin`.
    first: bool
    exp: float
    admin_id: str | None = None
    sid: str | None = None


def new_ceremony(
    purpose: Purpose,
    address: Address,
    *,
    origin: str | None = None,
    admin_id: str | None = None,
    sid: str | None = None,
) -> tuple[Ceremony, str]:
    """A new ceremony and its token.  `origin` is the proposed address when none is set."""
    first = address.origin is None
    chosen = origin if first else address.origin
    if chosen is None:
        raise ValueError("no passkey address")
    c = Ceremony(
        purpose=purpose,
        challenge=generate_challenge(),
        origin=chosen,
        rp_id=passkey_settings.rp_id_of(chosen),
        version=address.version,
        first=first,
        exp=_now() + CEREMONY_TTL,
        admin_id=admin_id,
        sid=sid_tag(sid) if sid else None,
    )
    token = seal_ceremony({
        "p": c.purpose,
        "c": bytes_to_base64url(c.challenge),
        "o": c.origin,
        "v": c.version,
        "f": c.first,
        "e": c.exp,
        "a": c.admin_id,
        "s": c.sid,
    })
    return c, token


def open_token(token: Any, purpose: Purpose, *, admin_id: str | None = None, sid: str | None = None) -> Ceremony:
    """The ceremony in `token`, checked for purpose, expiry, admin and session."""
    data = open_ceremony(token) if isinstance(token, str) else None
    if not data or data.get("p") != purpose:
        raise HTTPException(400, START_AGAIN)
    try:
        c = Ceremony(
            purpose=purpose,
            challenge=base64url_to_bytes(data["c"]),
            origin=str(data["o"]),
            rp_id=passkey_settings.rp_id_of(str(data["o"])),
            version=int(data["v"]),
            first=bool(data["f"]),
            exp=float(data["e"]),
            admin_id=data.get("a"),
            sid=data.get("s"),
        )
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, START_AGAIN) from None
    if _now() >= c.exp:
        raise HTTPException(400, START_AGAIN)
    if purpose != "login":
        if c.admin_id is None or c.admin_id != admin_id or not sid or c.sid != sid_tag(sid):
            raise HTTPException(400, START_AGAIN)
    return c


async def claim(r: Redis, c: Ceremony) -> None:
    """Use the ceremony once.  Redis errors propagate: never read as success."""
    ttl = max(1, math.ceil(c.exp - _now())) + USED_MARKER_GRACE
    won = await r.set(webauthn_used_key(bytes_to_base64url(c.challenge)), 1, nx=True, ex=ttl)
    if not won or _now() >= c.exp:
        raise HTTPException(400, START_AGAIN)


def check_address(c: Ceremony, current: Address) -> None:
    """The ceremony was made for the current address (a first registration: for no address)."""
    if current.version != c.version:
        raise HTTPException(409, ADDRESS_CHANGED)
    if c.first:
        if current.origin is not None:
            raise HTTPException(409, ADDRESS_CHANGED)
    elif current.origin != c.origin:
        raise HTTPException(409, ADDRESS_CHANGED)


def check_name(name: Any) -> str:
    if not isinstance(name, str):
        raise HTTPException(422, "Give the passkey a name.")
    text = " ".join(name.split())
    if not text:
        raise HTTPException(422, "Give the passkey a name.")
    if len(text) > MAX_NAME_LEN:
        raise HTTPException(422, f"The name can have at most {MAX_NAME_LEN} characters.")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise HTTPException(422, "The name contains text that is not valid Unicode.") from None
    return text


def descriptors(rows: list[Any]) -> list[PublicKeyCredentialDescriptor]:
    out = []
    for row in rows:
        transports = []
        try:
            for t in json.loads(row["transports"] or "[]"):
                try:
                    transports.append(AuthenticatorTransport(t))
                except ValueError:
                    pass
        except (TypeError, ValueError):
            pass
        out.append(PublicKeyCredentialDescriptor(id=base64url_to_bytes(row["credential_id"]), transports=transports or None))
    return out


def request_options(c: Ceremony, allow: list[Any] | None = None) -> dict[str, Any]:
    opts = generate_authentication_options(
        rp_id=c.rp_id,
        challenge=c.challenge,
        timeout=PROMPT_TIMEOUT_MS,
        allow_credentials=descriptors(allow or []),
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    return options_to_json_dict(opts)


def parse_assertion(credential: Any) -> AuthenticationCredential:
    if not isinstance(credential, dict):
        raise HTTPException(400, "The passkey response is missing or not valid.")
    try:
        return parse_authentication_credential_json(credential)
    except (WebAuthnException, ValueError, TypeError, KeyError):
        raise HTTPException(400, "The passkey response is missing or not valid.") from None


def admin_handle(admin_id: str) -> bytes:
    """The WebAuthn user handle of an admin: the 16 bytes of its ID (no personal data)."""
    return uuid.UUID(str(admin_id)).bytes


async def find_passkey(db: Any, credential: AuthenticationCredential, rp_id: str) -> Any:
    return await db.fetchrow(
        "SELECT id, admin_id, credential_id, public_key, sign_count, name FROM passkeys "
        "WHERE credential_id = $1 AND rp_id = $2",
        bytes_to_base64url(credential.raw_id), rp_id,
    )


@dataclass(frozen=True)
class Used:
    passkey_id: str
    admin_id: str
    name: str


async def use_passkey(
    request: Request,
    c: Ceremony,
    credential: AuthenticationCredential,
    *,
    session_admin: str | None,
    where: str,
) -> Used:
    """Verify an assertion for a claimed ceremony and record the use.

    Sign-in (session_admin None): the user handle is required and names the passkey's
    admin.  Step-up: the passkey must belong to the signed-in admin; a user handle, which
    the browser may leave out here, must name that admin when present.
    """
    db = request.app.state.db
    row = await find_passkey(db, credential, c.rp_id)
    if row is None:
        raise HTTPException(404 if session_admin is None else 400, "This passkey is not registered here. Use another passkey or your authenticator code.")
    owner = str(row["admin_id"])
    handle = credential.response.user_handle
    if session_admin is None:
        if handle is None or handle != admin_handle(owner):
            await _log_failure(request, owner, where, "user_handle")
            raise HTTPException(401, "The passkey could not be verified.")
    else:
        if owner != str(session_admin) or (handle is not None and handle != admin_handle(owner)):
            raise HTTPException(400, "This passkey is not one of yours.")
    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=c.challenge,
            expected_rp_id=c.rp_id,
            expected_origin=c.origin,
            credential_public_key=base64url_to_bytes(row["public_key"]),
            credential_current_sign_count=int(row["sign_count"]),
            require_user_verification=True,
        )
    except WebAuthnException:
        await _log_failure(request, owner, where, "invalid")
        raise HTTPException(401 if session_admin is None else 400, "The passkey could not be verified.") from None

    async with passkey_settings.passkey_transaction(db) as tx:
        check_address(c, await passkey_settings.read_current(tx))
        # The admin must still be enabled; the old count must still be the stored one, so
        # concurrent uses cannot move the counter back and a deleted passkey is not used.
        done = await tx.fetchval(
            "UPDATE passkeys SET sign_count = $1, backed_up = $2, last_used_at = now() "
            "WHERE id = $3 AND sign_count = $4 "
            "AND admin_id IN (SELECT id FROM admin_users WHERE totp_enabled = $5) RETURNING id",
            verified.new_sign_count, bool(verified.credential_backed_up), str(row["id"]), int(row["sign_count"]), True,
        )
    if done is None:
        raise HTTPException(409, "The passkey changed while you were using it. Try again.")
    return Used(str(row["id"]), owner, row["name"])


async def _log_failure(request: Request, admin_id: str, where: str, reason: str) -> None:
    db = request.app.state.db
    username = await db.fetchval("SELECT username FROM admin_users WHERE id = $1", admin_id)
    await log_activity(
        db,
        actor=username or f"admin:{admin_id}",
        action="passkey_failed",
        entity_type="admin_user",
        entity_id=admin_id,
        entity_name=username,
        details={"where": where, "reason": reason},
        ip_address=request.client.host if request.client else "unknown",
    )

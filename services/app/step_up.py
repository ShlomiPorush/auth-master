"""The second check before a secret is shown or a passkey is added: an authenticator code or a passkey."""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel

from app import passkeys
from app.crypto_totp import decrypt_totp_secret
from app.sessions import COOKIE_NAME
from app.totp_guard import check_totp_code, log_totp_failure, totp_error


class StepUpBody(BaseModel):
    """An authenticator code, or a passkey response with the ceremony token of its options."""

    code: str | None = None
    passkey: dict[str, Any] | None = None
    ceremony: str | None = None


async def verify_step_up(request: Request, admin_id: str, admin_row: Any, body: StepUpBody, where: str) -> str:
    """Check the code or the passkey of the signed-in admin; the method used, or HTTPException."""
    if body.passkey is not None:
        if body.code:
            raise HTTPException(400, "Use either a passkey or a code, not both.")
        c = passkeys.open_token(body.ceremony, "stepup", admin_id=str(admin_id), sid=request.cookies.get(COOKIE_NAME))
        credential = passkeys.parse_assertion(body.passkey)
        await passkeys.claim(request.app.state.redis, c)
        await passkeys.use_passkey(request, c, credential, session_admin=str(admin_id), where=where)
        return "passkey"
    if not body.code:
        raise HTTPException(400, "Enter the code from your authenticator app, or use a passkey.")
    try:
        secret = decrypt_totp_secret(admin_row["totp_secret_enc"])
    except Exception as e:
        raise HTTPException(500, "decrypt failed") from e
    check = await check_totp_code(request.app.state.redis, admin_id, secret, body.code.replace(" ", ""))
    if not check.ok:
        await log_totp_failure(request, admin_id, where, check)
        raise totp_error(check, at_login=False)
    return "code"

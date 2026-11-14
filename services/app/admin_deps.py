import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from app.sessions import COOKIE_NAME, delete_session, get_session


def _admin_uuid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


async def full_session(request: Request) -> dict:
    r = request.app.state.redis
    sid = request.cookies.get(COOKIE_NAME)
    if not sid:
        raise HTTPException(401, "Unauthorized")
    sess = await get_session(r, sid)
    if not sess or sess.get("kind") != "full":
        raise HTTPException(401, "Unauthorized")
    # The admin must still exist and have MFA enabled, whatever the session says.
    admin_id = _admin_uuid(sess.get("adminId"))
    enabled = admin_id is not None and await request.app.state.db.fetchval(
        "SELECT 1 FROM admin_users WHERE id = $1 AND totp_enabled = $2", admin_id, True
    )
    if not enabled:
        await delete_session(r, sid)
        raise HTTPException(401, "Unauthorized")
    return sess


def csrf_check(sess: dict, x_csrf_token: str | None) -> None:
    if not x_csrf_token or x_csrf_token != sess.get("csrf"):
        raise HTTPException(403, "CSRF")


FullSession = Annotated[dict, Depends(full_session)]

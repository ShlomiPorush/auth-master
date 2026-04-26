from typing import Annotated

from fastapi import Depends, HTTPException, Request

from app.sessions import COOKIE_NAME, get_session


async def full_session(request: Request) -> dict:
    r = request.app.state.redis
    sid = request.cookies.get(COOKIE_NAME)
    if not sid:
        raise HTTPException(401, "Unauthorized")
    sess = await get_session(r, sid)
    if not sess or sess.get("kind") != "full":
        raise HTTPException(401, "Unauthorized")
    return sess


def csrf_check(sess: dict, x_csrf_token: str | None) -> None:
    if not x_csrf_token or x_csrf_token != sess.get("csrf"):
        raise HTTPException(403, "CSRF")


FullSession = Annotated[dict, Depends(full_session)]

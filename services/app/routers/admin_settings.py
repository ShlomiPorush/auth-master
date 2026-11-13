"""Admin Settings page: the /validate rate limit and the addresses exempt from it."""
from __future__ import annotations

import os
import sqlite3
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from app import rate_limit, rate_limit_settings as rls
from app.admin_deps import FullSession, csrf_check
from app.datetime_utils import fmt_datetime
from app.logger import get_actor, log_activity

router = APIRouter(prefix="/admin/api/settings", tags=["admin-settings"])


def _own_address(request: Request) -> str | None:
    return rate_limit.address_key(request.client.host if request.client else None)


def _view(stored: rls.Stored, request: Request) -> dict[str, Any]:
    eff = rls.effective(stored)
    locked = rls.locked_fields()

    def source(field: str, stored_value: Any) -> str:
        if locked[field]:
            return "env"
        return "stored" if stored_value is not None else "default"

    return {
        "version": stored.version,
        "savedBy": stored.actor,
        "savedAt": fmt_datetime(stored.created_at),
        "savedFrom": stored.ip_address,
        "fields": {
            "maxReq": {
                "value": eff.max_req, "source": source("maxReq", stored.max_req), "locked": locked["maxReq"],
                "envName": rls.ENV_FIELDS["maxReq"][1], "min": 1, "max": rls.MAX_REQ_LIMIT,
            },
            "windowMs": {
                "value": eff.window_ms, "source": source("windowMs", stored.window_ms), "locked": locked["windowMs"],
                "envName": rls.ENV_FIELDS["windowMs"][1], "minSeconds": 1, "maxSeconds": rls.WINDOW_SECONDS_LIMIT,
            },
            "exempt": {
                "value": list(stored.exempt), "source": "stored" if stored.exempt else "default", "locked": False,
                "max": rls.MAX_EXEMPT,
            },
        },
        "applied": rls.status_view(),
        "yourAddress": _own_address(request),
        # uvicorn's own setting, read when the server starts; shown here, never changed here.
        "proxyNetwork": {"envName": "FORWARDED_ALLOW_IPS", "value": os.environ.get("FORWARDED_ALLOW_IPS") or None},
    }


def _error(status: int, code: str, detail: str, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail, "code": code, **extra})


async def _read(request: Request) -> rls.Stored | JSONResponse:
    try:
        return await rls.read_current(request.app.state.settings_db)
    except rls.InvalidSettings as e:
        return _error(500, "invalid_stored", e.message)


@router.get("/rate-limit")
async def get_rate_limit_settings(request: Request, _sess: FullSession):
    stored = await _read(request)
    if isinstance(stored, JSONResponse):
        return stored
    return _view(stored, request)


class RateLimitSettingsBody(BaseModel):
    """Only the fields being changed; a field the environment sets must be left out."""

    model_config = ConfigDict(extra="forbid")

    baseVersion: StrictInt = Field(ge=0, description="The settings version the form was loaded from.")
    maxReq: StrictInt | None = None
    windowSeconds: StrictInt | None = None
    exempt: list[StrictStr] | None = Field(default=None, max_length=1000)
    confirmOwnAddress: StrictBool = False


@router.put("/rate-limit")
async def put_rate_limit_settings(
    request: Request,
    body: RateLimitSettingsBody,
    sess: FullSession,
    x_csrf_token: str | None = Header(None, alias="X-CSRF-Token"),
):
    csrf_check(sess, x_csrf_token)
    sent = body.model_fields_set
    locked = rls.locked_fields()
    for field, name in (("maxReq", "maxReq"), ("windowMs", "windowSeconds")):
        if name in sent and locked[field]:
            env = rls.ENV_FIELDS[field][1]
            return _error(409, "locked", f"The {env} environment variable sets this value, so it cannot be changed here.", field=field)
    for name in ("maxReq", "windowSeconds", "exempt"):
        if name in sent and getattr(body, name) is None:
            return _error(422, "invalid", "Leave a field out to keep its value; it cannot be empty.", field=name)

    stored = await _read(request)
    if isinstance(stored, JSONResponse):
        return stored
    if stored.version != body.baseVersion:
        return _error(409, "conflict", "Someone else changed these settings after you opened the page.", current=_view(stored, request))

    try:
        max_req = rls.check_max_req(body.maxReq) if "maxReq" in sent else stored.max_req
        window_ms = rls.check_window_ms(body.windowSeconds * 1000) if "windowSeconds" in sent else stored.window_ms
        exempt = rls.check_exempt(body.exempt) if "exempt" in sent else stored.exempt
    except rls.InvalidSettings as e:
        return _error(422, "invalid", e.message, field=e.field)

    own = _own_address(request)
    newly_exempt = (
        own is not None
        and rate_limit.Limits(0, 1, 1000, frozenset(exempt)).is_exempt(own)
        and not rate_limit.Limits(0, 1, 1000, frozenset(stored.exempt)).is_exempt(own)
    )
    if newly_exempt and not body.confirmOwnAddress:
        return _error(
            409, "own_address",
            f"This exempts {own}, the address your own request came from. Behind a reverse proxy that "
            "FORWARDED_ALLOW_IPS does not cover, it is the proxy's address, and exempting it removes the limit for every caller.",
            field="exempt", address=own,
        )

    if (max_req, window_ms, exempt) == (stored.max_req, stored.window_ms, stored.exempt):
        return _view(stored, request)

    actor = await get_actor(request, sess)
    value = rls.dump_value(max_req, window_ms, exempt)
    try:
        saved = await rls.insert_version(request.app.state.settings_db, stored.version + 1, value, actor, own)
    except sqlite3.OperationalError:
        # Another connection held the write lock for longer than the busy timeout.
        return _error(503, "busy", "The database is busy. Try again in a moment.")
    if not saved:
        current = await _read(request)
        view = None if isinstance(current, JSONResponse) else _view(current, request)
        return _error(409, "conflict", "Someone else changed these settings after you opened the page.", current=view)

    new = rls.Stored(stored.version + 1, max_req, window_ms, exempt, actor, own, None)
    rate_limit.apply(rls.effective(new))
    # The settings row is the durable record; this copy shows the change on the Activity Log page.
    warning = None
    try:
        await log_activity(
            request.app.state.db,
            actor=actor,
            action="settings.update",
            entity_type="settings",
            entity_name="rate_limit",
            details={
                "version": new.version,
                "from": {"maxReq": stored.max_req, "windowMs": stored.window_ms, "exempt": list(stored.exempt)},
                "to": {"maxReq": max_req, "windowMs": window_ms, "exempt": list(exempt)},
            },
            ip_address=request.client.host if request.client else None,
        )
    except Exception as e:
        print(f"Rate limit settings version {new.version} saved, but the activity log entry failed ({type(e).__name__})")
        warning = "Saved, but the activity log entry could not be written."

    current = await _read(request)
    view = _view(new if isinstance(current, JSONResponse) else current, request)
    if warning:
        view["warning"] = warning
    return view

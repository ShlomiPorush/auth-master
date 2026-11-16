"""Admin Settings page: the /validate rate limit and the addresses exempt from it, the
general settings (time zone, Secure session cookie, log retention), the Redis connection, and
how dates and times are written (display)."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import display_settings as ds, general_settings as gs, rate_limit, rate_limit_settings as rls, redis_settings as rs
from app.admin_deps import FullSession, csrf_check
from app.config import _redis_url_problem, get_settings
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
                "envName": rls.ENV_FIELDS["maxReq"][1], "min": 1, "max": rls.MAX_REQ_LIMIT, "default": rls.DEFAULT_MAX_REQ,
            },
            "windowMs": {
                "value": eff.window_ms, "source": source("windowMs", stored.window_ms), "locked": locked["windowMs"],
                "envName": rls.ENV_FIELDS["windowMs"][1], "minSeconds": 1, "maxSeconds": rls.WINDOW_SECONDS_LIMIT,
                "default": rls.DEFAULT_WINDOW_MS,
            },
            "exempt": {
                "value": list(stored.exempt), "source": "stored" if stored.exempt else "default", "locked": False,
                "max": rls.MAX_EXEMPT,
            },
        },
        "applied": rls.status_view(),
        "yourAddress": _own_address(request),
        # uvicorn's own setting, read when the server starts; shown here, never changed here.
        "proxyNetwork": {"envName": "FORWARDED_ALLOW_IPS", "value": os.environ.get("FORWARDED_ALLOW_IPS")},
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


# ── General settings ────────────────────────────────────────────

_CONFLICT = "Someone else changed these settings after you opened the page."
_BUSY = "The database is busy. Try again in a moment."
_ACTIVITY_WARNING = "Saved, but the activity log entry could not be written."

COOKIE_SECURE_OVER_HTTP = (
    "Browsers send a Secure cookie only over HTTPS. Signing in over plain HTTP will stop working; "
    "sessions already open keep working until they end. To undo it without the page, set COOKIE_SECURE=false."
)


def _general_view(stored: gs.Stored) -> dict[str, Any]:
    eff = gs.effective(stored)
    locked = gs.locked_fields()

    def item(name: str) -> dict[str, Any]:
        if locked[name]:
            source = "env"
        else:
            source = "stored" if stored.values[name] is not None else "default"
        return {"value": eff.value(name), "source": source, "locked": locked[name], "envName": gs.ENV_FIELDS[name][1],
                "default": gs.DEFAULTS[name]}

    retention = {"min": gs.RETENTION_MIN, "max": gs.RETENTION_MAX}
    s = get_settings()
    return {
        "version": stored.version,
        "savedBy": stored.actor,
        "savedAt": fmt_datetime(stored.created_at),
        "savedFrom": stored.ip_address,
        "fields": {
            "timeZone": {**item("timeZone"), "envInvalid": gs.env_time_zone_invalid()},
            "cookieSecure": item("cookieSecure"),
            "activityLogRetentionDays": {**item("activityLogRetentionDays"), **retention},
            "accessLogRetentionDays": {**item("accessLogRetentionDays"), **retention},
        },
        "timeZones": sorted(gs.time_zones()),
        "applied": gs.status_view(),
        "purge": gs.purge_view(),
        # Read when the process starts and must match the reverse proxy; shown, never changed here.
        "rootPath": {"envName": "ROOT_PATH", "value": s.root_path if "root_path" in s.model_fields_set else None},
    }


async def _read_general(request: Request) -> gs.Stored | JSONResponse:
    try:
        return await gs.read_current(request.app.state.settings_db)
    except gs.InvalidSettings as e:
        return _error(500, "invalid_stored", e.message)


@router.get("/general")
async def get_general_settings(request: Request, _sess: FullSession):
    stored = await _read_general(request)
    if isinstance(stored, JSONResponse):
        return stored
    return _general_view(stored)


class ConfirmPurge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    activityLogRetentionDays: StrictInt
    accessLogRetentionDays: StrictInt


class GeneralSettingsBody(BaseModel):
    """Only the fields being changed; a field the environment sets must be left out."""

    model_config = ConfigDict(extra="forbid")

    baseVersion: StrictInt = Field(ge=0, description="The settings version the form was loaded from.")
    timeZone: StrictStr | None = None
    cookieSecure: StrictBool | None = None
    activityLogRetentionDays: StrictInt | None = None
    accessLogRetentionDays: StrictInt | None = None
    confirmPurge: ConfirmPurge | None = None
    confirmCookieSecure: StrictBool = False


async def _count_older(request: Request, table: str, days: int) -> int | None:
    db = request.app.state.db
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        return int(await db.fetchval(
            f"SELECT COUNT(*) FROM {table} WHERE created_at < $1",
            cutoff.strftime("%Y-%m-%d %H:%M:%S") if db.is_sqlite else cutoff,
        ))
    except Exception:
        return None


@router.put("/general")
async def put_general_settings(
    request: Request,
    body: GeneralSettingsBody,
    sess: FullSession,
    x_csrf_token: str | None = Header(None, alias="X-CSRF-Token"),
):
    csrf_check(sess, x_csrf_token)
    sent = body.model_fields_set & set(gs.FIELDS)
    locked = gs.locked_fields()
    for name in gs.FIELDS:
        if name in sent and locked[name]:
            env = gs.ENV_FIELDS[name][1]
            return _error(409, "locked", f"The {env} environment variable sets this value, so it cannot be changed here.", field=name)
    for name in gs.FIELDS:
        if name in sent and getattr(body, name) is None:
            return _error(422, "invalid", "Leave a field out to keep its value; it cannot be empty.", field=name)

    stored = await _read_general(request)
    if isinstance(stored, JSONResponse):
        return stored
    if stored.version != body.baseVersion:
        return _error(409, "conflict", _CONFLICT, current=_general_view(stored))

    values = dict(stored.values)
    try:
        for name in gs.FIELDS:
            if name in sent:
                values[name] = gs.check(name, getattr(body, name))
    except gs.InvalidSettings as e:
        return _error(422, "invalid", e.message, field=e.field)

    before = gs.effective(stored)
    new = gs.Stored(stored.version + 1, values)
    after = gs.effective(new)

    lowered = [
        name for name in ("activityLogRetentionDays", "accessLogRetentionDays")
        if after.value(name) < before.value(name)
    ]
    if lowered:
        confirm = {name: after.value(name) for name in ("activityLogRetentionDays", "accessLogRetentionDays")}
        given = body.confirmPurge.model_dump() if body.confirmPurge is not None else None
        if given != confirm:
            estimate = {
                "activityLogs": await _count_older(request, "activity_logs", after.activity_log_retention_days)
                if "activityLogRetentionDays" in lowered else None,
                "accessLogs": await _count_older(request, "access_logs", after.access_log_retention_days)
                if "accessLogRetentionDays" in lowered else None,
            }
            return _error(
                409, "confirm_purge",
                "A shorter retention deletes the older log entries within about a minute, and they cannot be restored.",
                estimate=estimate, confirm=confirm,
            )

    if after.cookie_secure and not before.cookie_secure and request.url.scheme == "http" and not body.confirmCookieSecure:
        return _error(409, "confirm_cookie_secure", COOKIE_SECURE_OVER_HTTP)

    if values == stored.values:
        return _general_view(stored)

    actor = await get_actor(request, sess)
    own = _own_address(request)
    try:
        saved = await gs.insert_version(request.app.state.settings_db, new.version, gs.dump_value(values), actor, own)
    except sqlite3.OperationalError:
        return _error(503, "busy", _BUSY)
    if not saved:
        current = await _read_general(request)
        view = None if isinstance(current, JSONResponse) else _general_view(current)
        return _error(409, "conflict", _CONFLICT, current=view)

    gs.apply(after)
    warning = None
    try:
        await gs.log_change(
            request.app.state.db, actor, new.version, stored.values, values,
            request.client.host if request.client else None,
        )
    except Exception as e:
        print(f"General settings version {new.version} saved, but the activity log entry failed ({type(e).__name__})")
        warning = _ACTIVITY_WARNING

    current = await _read_general(request)
    view = _general_view(gs.Stored(new.version, values, actor, own) if isinstance(current, JSONResponse) else current)
    if warning:
        view["warning"] = warning
    return view


# ── Redis connection ────────────────────────────────────────────

CONFIRM_RESTART = (
    "Takes effect when every instance of the service restarts. Then every admin signs in again, because "
    "sessions live in Redis, and a first-admin setup in progress starts over."
)

CHECK_FAILED = {
    "unreachable": "Could not reach this Redis server in time. Check the host, port and network. Nothing was saved.",
    "auth": "The Redis server refused the user name or password. Nothing was saved.",
    "no_write": (
        "This Redis user cannot run the commands the service uses on its auth:* keys, or the server is a "
        "read-only replica. README.md lists the commands and key prefixes. Nothing was saved."
    ),
    "other": "The Redis server did not pass the check. Nothing was saved.",
}

_REDIS_BODY_FIELDS = ("baseVersion", "url", "confirmRestart")


def _redis_view(stored: rs.Stored) -> dict[str, Any]:
    saved = rs.saved_url(stored)
    choice = rs.current_choice(stored)
    connected = rs.state.connection
    return {
        "version": stored.version,
        "savedBy": stored.actor,
        "savedAt": fmt_datetime(stored.created_at),
        "savedFrom": stored.ip_address,
        "url": {
            "connected": rs.mask_url(connected.url) if connected else None,
            "saved": rs.mask_url(saved),
            "source": choice.source if choice else "default",
            "locked": rs.env_url() is not None,
            "envName": rs.ENV_NAME,
            "syncPending": rs.state.sync_pending,
        },
        "restartNeeded": connected is not None and (choice is None or choice.url != connected.url),
    }


@router.get("/redis")
async def get_redis_settings(request: Request, _sess: FullSession):
    return _redis_view(await rs.read_current(request.app.state.settings_db))


class RedisSettingsBody(BaseModel):
    # Validated by the handler, not declared as the route's body: FastAPI's validation
    # errors echo the input, which here holds a password.
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    baseVersion: StrictInt = Field(ge=0)
    url: StrictStr = Field(min_length=1, max_length=rs.MAX_URL_LENGTH)
    confirmRestart: StrictBool = False


@router.put("/redis")
async def put_redis_settings(
    request: Request,
    sess: FullSession,
    x_csrf_token: str | None = Header(None, alias="X-CSRF-Token"),
):
    csrf_check(sess, x_csrf_token)
    invalid_body = "Send a JSON object with baseVersion (a whole number), url (text) and optionally confirmRestart (true or false)."
    try:
        data = await request.json()
    except StarletteHTTPException:
        # The request body limit (64 KiB, main.py) answers 413 for an oversized body.
        raise
    except Exception:
        return _error(422, "invalid", invalid_body, field="body")
    try:
        body = RedisSettingsBody.model_validate(data)
    except ValidationError as e:
        loc = e.errors(include_input=False, include_url=False)[0]["loc"]
        # A key of the body is client text: name only the fields this endpoint has.
        name = loc[0] if loc and loc[0] in _REDIS_BODY_FIELDS else "body"
        return _error(422, "invalid", invalid_body, field=name)

    if rs.env_url() is not None:
        return _error(409, "locked", f"The {rs.ENV_NAME} environment variable sets this value, so it cannot be changed here.", field="url")
    url = body.url.strip()
    try:
        url.encode("utf-8")
    except UnicodeEncodeError:
        return _error(422, "invalid", "The Redis URL contains text that is not valid Unicode.", field="url")
    if problem := _redis_url_problem(url, "The Redis URL"):
        return _error(422, "invalid", problem, field="url")

    settings_db = request.app.state.settings_db
    stored = await rs.read_current(settings_db)
    if stored.version != body.baseVersion:
        return _error(409, "conflict", _CONFLICT, current=_redis_view(stored))
    before = rs.saved_url(stored)
    if url == before:
        return _redis_view(stored)
    if not body.confirmRestart:
        return _error(409, "confirm_restart", CONFIRM_RESTART)

    category = await rs.check(url)
    if category is not None:
        return _error(422, "redis_check_failed", CHECK_FAILED[category], category=category)

    actor = await get_actor(request, sess)
    own = _own_address(request)
    try:
        saved = await rs.insert_version(settings_db, stored.version + 1, rs.dump_value(rs.encrypt_url(url)), actor, own)
    except sqlite3.OperationalError:
        return _error(503, "busy", _BUSY)
    if not saved:
        return _error(409, "conflict", _CONFLICT, current=_redis_view(await rs.read_current(settings_db)))

    warning = None
    try:
        await rs.log_change(
            request.app.state.db, actor, stored.version + 1, before, url,
            request.client.host if request.client else None,
        )
    except Exception as e:
        print(f"Redis URL version {stored.version + 1} saved, but the activity log entry failed ({type(e).__name__})")
        warning = _ACTIVITY_WARNING
    view = _redis_view(await rs.read_current(settings_db))
    if warning:
        view["warning"] = warning
    return view


# ── Display: how dates and times are written ────────────────────

def _display_view(stored: ds.Stored) -> dict[str, Any]:
    eff = ds.effective(stored)
    locked = ds.locked_fields()

    def item(name: str) -> dict[str, Any]:
        if locked[name]:
            source = "env"
        else:
            source = "stored" if stored.values[name] is not None else "default"
        return {
            "value": eff.value(name), "source": source, "locked": locked[name], "envName": ds.ENV_FIELDS[name][1],
            "envInvalid": ds.env_invalid(name), "default": ds.DEFAULTS[name], "options": list(ds.OPTIONS[name]),
        }

    return {
        "version": stored.version,
        "savedBy": stored.actor,
        "savedAt": stored.saved_at,
        "fields": {name: item(name) for name in ds.FIELDS},
        # The stored value could not be read: the defaults apply until the next save.
        "storedInvalid": stored.invalid is not None,
        "applied": ds.status_view(),
    }


async def _read_display(request: Request) -> ds.Stored:
    return await ds.read_current(request.app.state.settings_db)


@router.get("/display")
async def get_display_settings(request: Request, _sess: FullSession):
    return _display_view(await _read_display(request))


class DisplaySettingsBody(BaseModel):
    """Only the fields being changed; a field the environment sets must be left out."""

    model_config = ConfigDict(extra="forbid")

    baseVersion: StrictInt = Field(ge=0, description="The settings version the form was loaded from.")
    dateFormat: StrictStr | None = None
    timeFormat: StrictStr | None = None


@router.put("/display")
async def put_display_settings(
    request: Request,
    body: DisplaySettingsBody,
    sess: FullSession,
    x_csrf_token: str | None = Header(None, alias="X-CSRF-Token"),
):
    csrf_check(sess, x_csrf_token)
    sent = body.model_fields_set & set(ds.FIELDS)
    locked = ds.locked_fields()
    for name in ds.FIELDS:
        if name in sent and locked[name]:
            env = ds.ENV_FIELDS[name][1]
            return _error(409, "locked", f"The {env} environment variable sets this value, so it cannot be changed here.", field=name)
    for name in ds.FIELDS:
        if name in sent and getattr(body, name) is None:
            return _error(422, "invalid", "Leave a field out to keep its value; it cannot be empty.", field=name)

    stored = await _read_display(request)
    if stored.version != body.baseVersion:
        return _error(409, "conflict", _CONFLICT, current=_display_view(stored))

    values = dict(stored.values)
    try:
        for name in ds.FIELDS:
            if name in sent:
                values[name] = ds.check(name, getattr(body, name))
    except ds.InvalidSettings as e:
        return _error(422, "invalid", e.message, field=e.field)

    if values == stored.values and not stored.invalid:
        return _display_view(stored)

    before = {name: ds.effective(stored).value(name) for name in ds.FIELDS}
    filled = {name: values[name] if values[name] is not None else ds.DEFAULTS[name] for name in ds.FIELDS}
    actor = await get_actor(request, sess)
    version = stored.version + 1
    value = ds.dump_value(version, filled, actor, ds.now_iso())
    try:
        saved = await ds.save(request.app.state.db, stored, value)
    except ds.WriteRefused as e:
        if e.busy:
            return _error(503, "busy", _BUSY)
        return _error(503, "write_refused", "The database did not accept the change. Nothing was saved.")
    except ds.WriteUnknown:
        return _error(503, "write_unknown", "The database did not confirm the change. It may have been saved; read the settings again.")
    if not saved:
        return _error(409, "conflict", _CONFLICT, current=_display_view(await _read_display(request)))

    new = ds.Stored(version, filled, actor, None, raw=value)
    ds.apply(ds.effective(new))
    warning = None
    try:
        await ds.log_change(request.app.state.db, actor, version, before, filled,
                            request.client.host if request.client else None)
    except Exception as e:
        print(f"Display settings version {version} saved, but the activity log entry failed ({type(e).__name__})")
        warning = _ACTIVITY_WARNING
    view = _display_view(await _read_display(request))
    if warning:
        view["warning"] = warning
    return view

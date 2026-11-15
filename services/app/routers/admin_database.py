"""The database connection: first-time setup, the Settings page and the recovery page.

Setup routes work only while no database is configured (open to whoever reaches the
service first, like the rest of first-time setup).  Settings routes need a full session
and the CSRF header; saving also needs an authenticator code or a passkey.  Recovery
routes need the master key (ADMIN_API_KEY) on every call, compared in constant time, with
failed attempts limited per address.

Request bodies are parsed here, not declared on the routes: FastAPI's validation errors
echo the input, which holds a password.  No response, log line or activity row contains
the password; errors carry fixed codes and short texts.
"""
from __future__ import annotations

from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import db_config, db_operations as ops, db_runtime
from app.admin_deps import FullSession, csrf_check
from app.db_runtime import Busy, OperationError, Runtime
from app.logger import get_actor
from app.step_up import StepUpBody, verify_step_up

router = APIRouter(prefix="/admin/api", tags=["admin-database"])


class PostgresBody(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    host: StrictStr = Field(max_length=db_config.MAX_HOST)
    port: StrictInt = 5432
    database: StrictStr = Field(max_length=256)
    user: StrictStr = Field(max_length=256)
    # Settings page: empty keeps the saved password.
    password: StrictStr | None = Field(default=None, max_length=db_config.MAX_PASSWORD)
    sslmode: StrictStr = db_config.DEFAULT_SSLMODE
    caPem: StrictStr | None = Field(default=None, max_length=db_config.MAX_CA_PEM)
    # Settings page: keep the saved CA certificate when caPem is not sent.
    keepCa: StrictBool = False


class SetupBody(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    backend: Literal["sqlite", "postgres"]
    postgres: PostgresBody | None = None
    adopt: StrictBool = False


class EditBody(StepUpBody):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    postgres: PostgresBody
    confirmCopy: StrictBool = False


class RecoveryBody(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    backend: Literal["sqlite", "postgres"]
    postgres: PostgresBody | None = None
    confirm: StrictBool = False


_BODY_FIELDS = {"backend", "postgres", "adopt", "confirmCopy", "confirm", "code", "passkey", "ceremony"}
_PG_FIELDS = set(PostgresBody.model_fields)


def _error(status: int, code: str, detail: str, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail, "code": code, **extra},
                        headers={"Cache-Control": "no-store"})


def _op_error(e: OperationError, status: int = 422) -> JSONResponse:
    return _error(status, e.code, e.text, **e.extra)


async def _parse(request: Request, model: type[BaseModel]) -> BaseModel | JSONResponse:
    invalid = "Send a JSON object with the connection fields."
    try:
        data = await request.json()
    except StarletteHTTPException:
        raise  # 413 from the request body limit
    except Exception:
        return _error(422, "invalid", invalid, field="body")
    try:
        return model.model_validate(data)
    except ValidationError as e:
        loc = e.errors(include_input=False, include_url=False)[0]["loc"]
        # Keys of the body are client text: name only fields this endpoint has.
        name = "body"
        if loc and loc[0] == "postgres" and len(loc) > 1 and loc[1] in _PG_FIELDS:
            name = str(loc[1])
        elif loc and loc[0] in _BODY_FIELDS:
            name = str(loc[0])
        return _error(422, "invalid", f"Check the field {name}.", field=name)


def _params(body: PostgresBody, saved: db_config.PgParams | None = None) -> db_config.PgParams | JSONResponse:
    password = body.password
    if not password and saved is not None:
        # The saved password goes only to the server it was saved for: another host or port
        # could be a server that asks for the password in clear text and keeps it.
        if (body.host.strip(), body.port) != (saved.host, saved.port):
            return _error(422, "invalid", "Enter the password: the host or port changed.", field="password")
        password = saved.password
    ca = body.caPem.strip() + "\n" if body.caPem and body.caPem.strip() else None
    if ca is None and body.keepCa and saved is not None and body.sslmode == "verify-full":
        ca = saved.ca_pem
    p = db_config.PgParams(
        host=body.host.strip(),
        port=body.port,
        database=body.database,
        user=body.user,
        password=password or "",
        sslmode=body.sslmode,
        ca_pem=ca,
    )
    if problem := db_config.params_problem(p):
        field, text = problem
        return _error(422, "invalid", text, field=field)
    return p


def _runtime(request: Request) -> Runtime:
    return request.app.state.runtime


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _start(rt: Runtime, kind: str, fn: Any) -> JSONResponse:
    try:
        job = rt.jobs.start(kind, fn)
    except Busy as b:
        return _error(409, "busy", "Another database operation is running.", job=b.job.view())
    return JSONResponse(status_code=202, content={"job": job.view()}, headers={"Cache-Control": "no-store"})


def _job(rt: Runtime, job_id: str, kinds: set[str]) -> JSONResponse:
    job = rt.jobs.get(job_id)
    if job is None or job.kind not in kinds:
        return _error(404, "not_found", "No such operation.")
    return JSONResponse(content={"job": job.view(), "mode": rt.mode}, headers={"Cache-Control": "no-store"})


async def _check(run: Any) -> JSONResponse:
    try:
        result = await run()
    except OperationError as e:
        return JSONResponse(content={"ok": False, "code": e.code, "detail": e.text}, headers={"Cache-Control": "no-store"})
    return JSONResponse(content={"ok": True, **result.view()}, headers={"Cache-Control": "no-store"})


# ── First-time setup ────────────────────────────────────────────

def _setup_only(rt: Runtime) -> JSONResponse | None:
    if rt.mode != db_runtime.SETUP:
        return _error(409, "already_configured", "The database is already set up.")
    return None


@router.post("/setup/database/check")
async def setup_database_check(request: Request):
    rt = _runtime(request)
    if (refused := _setup_only(rt)) is not None:
        return refused
    body = await _parse(request, SetupBody)
    if isinstance(body, JSONResponse):
        return body
    if body.backend == db_config.SQLITE:
        return await _check(lambda: ops.check_sqlite(db_config.default_sqlite_path()))
    if body.postgres is None:
        return _error(422, "invalid", "Enter the PostgreSQL connection.", field="postgres")
    params = _params(body.postgres)
    if isinstance(params, JSONResponse):
        return params
    return await _check(lambda: ops.check_postgres(params))


@router.post("/setup/database")
async def setup_database(request: Request):
    rt = _runtime(request)
    if (refused := _setup_only(rt)) is not None:
        return refused
    body = await _parse(request, SetupBody)
    if isinstance(body, JSONResponse):
        return body
    params = None
    if body.backend == db_config.POSTGRES:
        if body.postgres is None:
            return _error(422, "invalid", "Enter the PostgreSQL connection.", field="postgres")
        params = _params(body.postgres)
        if isinstance(params, JSONResponse):
            return params
    ip = _ip(request)
    app = request.app
    return _start(rt, "setup", lambda job: ops.run_setup(rt, app, job, body.backend, params, body.adopt, ip))


@router.get("/setup/database/jobs/{job_id}")
async def setup_database_job(request: Request, job_id: str):
    return _job(_runtime(request), job_id, {"setup"})


# ── Settings page ───────────────────────────────────────────────

def _env_view(url: str) -> dict[str, Any]:
    if url.startswith("sqlite"):
        return {"backend": "sqlite", "path": url.split("///", 1)[-1] if "///" in url else None}
    try:
        parts = urlsplit(url)
        return {
            "backend": "postgres",
            "host": parts.hostname,
            "port": parts.port or 5432,
            "database": unquote(parts.path.lstrip("/")) or None,
            "user": unquote(parts.username) if parts.username else None,
            "sslmode": None,
        }
    except ValueError:
        return {"backend": "postgres"}


def settings_view(rt: Runtime) -> dict[str, Any]:
    from app.config import get_settings

    env = get_settings().env_value("database_url")
    current = rt.jobs.current
    view: dict[str, Any] = {
        "source": rt.source,
        "locked": env is not None,
        "envName": "DATABASE_URL",
        "job": current.view() if current is not None and current.state == "running" else None,
        "sslmodes": list(db_config.SSLMODES),
    }
    if env is not None:
        view["connection"] = _env_view(env)
    elif rt.config is not None:
        cfg = rt.config
        if cfg.backend == db_config.SQLITE:
            view["connection"] = {"backend": "sqlite", "path": cfg.sqlite_path}
        else:
            assert cfg.pg is not None
            view["connection"] = {"backend": "postgres", **cfg.pg.public()}
    view["editable"] = (env is None and rt.config is not None and rt.config.backend == db_config.POSTGRES)
    return view


@router.get("/settings/database")
async def get_database_settings(request: Request, _sess: FullSession):
    return JSONResponse(content=settings_view(_runtime(request)), headers={"Cache-Control": "no-store"})


def _editable(rt: Runtime) -> JSONResponse | None:
    if not settings_view(rt)["editable"]:
        return _error(409, "locked", "This connection cannot be changed here.")
    return None


@router.post("/settings/database/check")
async def check_database_settings(
    request: Request, sess: FullSession, x_csrf_token: str | None = Header(None, alias="X-CSRF-Token"),
):
    csrf_check(sess, x_csrf_token)
    rt = _runtime(request)
    if (refused := _editable(rt)) is not None:
        return refused
    body = await _parse(request, EditBody)
    if isinstance(body, JSONResponse):
        return body
    params = _params(body.postgres, rt.config.pg)
    if isinstance(params, JSONResponse):
        return params
    current = rt.db
    response = await _check(lambda: ops.check_postgres(params, current_db=current))
    return response


@router.put("/settings/database")
async def put_database_settings(
    request: Request, sess: FullSession, x_csrf_token: str | None = Header(None, alias="X-CSRF-Token"),
):
    csrf_check(sess, x_csrf_token)
    rt = _runtime(request)
    if (refused := _editable(rt)) is not None:
        return refused
    body = await _parse(request, EditBody)
    if isinstance(body, JSONResponse):
        return body
    params = _params(body.postgres, rt.config.pg)
    if isinstance(params, JSONResponse):
        return params
    db = request.app.state.db
    admin_id = str(sess.get("adminId"))
    admin_row = await db.fetchrow("SELECT totp_secret_enc, totp_enabled FROM admin_users WHERE id = $1", admin_id)
    if not admin_row or not admin_row["totp_enabled"] or not admin_row["totp_secret_enc"]:
        return _error(403, "mfa_required", "MFA not configured")
    if rt.jobs.current is not None and rt.jobs.current.state == "running":
        return _error(409, "busy", "Another database operation is running.", job=rt.jobs.current.view())
    await verify_step_up(request, admin_id, admin_row, body, "database_change")
    actor = await get_actor(request, sess)
    ip = _ip(request)
    app = request.app
    return _start(rt, "edit", lambda job: ops.run_edit(rt, app, job, params, body.confirmCopy, actor, ip))


@router.get("/settings/database/jobs/{job_id}")
async def database_settings_job(request: Request, job_id: str, _sess: FullSession):
    return _job(_runtime(request), job_id, {"edit"})


# ── Recovery page ───────────────────────────────────────────────

def _key_problem(request: Request, authorization: str | None) -> JSONResponse | None:
    from app.config import get_settings

    rt = _runtime(request)
    if not get_settings().admin_api_key.strip():
        return _error(
            403, "no_master_key",
            "The recovery page needs the master key. Set ADMIN_API_KEY in the environment and restart the service.",
        )
    key = _ip(request) or "unknown"
    wait = rt.attempts.blocked(key)
    if wait:
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many wrong keys. Try again later.", "code": "rate_limited", "retryAfter": wait},
            headers={"Retry-After": str(wait), "Cache-Control": "no-store"},
        )
    if not db_runtime.master_key_matches(authorization):
        rt.attempts.fail(key)
        return _error(401, "unauthorized", "The master key is not correct.")
    return None


@router.get("/recovery/status")
async def recovery_status(request: Request, authorization: str | None = Header(None)):
    """Without the key: whether the page is usable.  With it: the failure and the saved
    connection (never the password)."""
    from app.config import get_settings

    rt = _runtime(request)
    base = {"mode": rt.mode, "masterKeyConfigured": bool(get_settings().admin_api_key.strip())}
    if authorization is None:
        return JSONResponse(content=base, headers={"Cache-Control": "no-store"})
    if (refused := _key_problem(request, authorization)) is not None:
        return refused
    current = rt.jobs.current
    return JSONResponse(
        content={**base, **ops.failure_view(rt), "sslmodes": list(db_config.SSLMODES),
                 "job": current.view() if current is not None and current.state == "running" else None},
        headers={"Cache-Control": "no-store"},
    )


def _recovery_only(rt: Runtime) -> JSONResponse | None:
    if rt.mode != db_runtime.RECOVERY:
        return _error(409, "not_in_recovery", "The service is not in recovery mode.")
    return None


async def _recovery_params(rt: Runtime, body: RecoveryBody) -> db_config.PgParams | None | JSONResponse:
    if body.backend == db_config.SQLITE:
        return None
    if body.postgres is None:
        return _error(422, "invalid", "Enter the PostgreSQL connection.", field="postgres")
    saved = rt.config.pg if rt.config is not None and rt.config.backend == db_config.POSTGRES else None
    return _params(body.postgres, saved)


@router.post("/recovery/check")
async def recovery_check(request: Request, authorization: str | None = Header(None)):
    if (refused := _key_problem(request, authorization)) is not None:
        return refused
    rt = _runtime(request)
    if (refused := _recovery_only(rt)) is not None:
        return refused
    body = await _parse(request, RecoveryBody)
    if isinstance(body, JSONResponse):
        return body
    params = await _recovery_params(rt, body)
    if isinstance(params, JSONResponse):
        return params
    if params is None:
        cfg = rt.config
        from pathlib import Path

        path = Path(cfg.sqlite_path) if cfg is not None and cfg.backend == db_config.SQLITE and cfg.sqlite_path else db_config.default_sqlite_path()
        if not path.is_file():
            return JSONResponse(content={"ok": False, "code": "sqlite_missing", "detail": db_runtime.message("sqlite_missing")})
        response = await _check(lambda: ops.check_sqlite(path))
    else:
        response = await _check(lambda: ops.check_postgres(params))
    return response


@router.post("/recovery/save")
async def recovery_save(request: Request, authorization: str | None = Header(None)):
    if (refused := _key_problem(request, authorization)) is not None:
        return refused
    rt = _runtime(request)
    if (refused := _recovery_only(rt)) is not None:
        return refused
    body = await _parse(request, RecoveryBody)
    if isinstance(body, JSONResponse):
        return body
    params = await _recovery_params(rt, body)
    if isinstance(params, JSONResponse):
        return params
    ip = _ip(request)
    app = request.app
    return _start(rt, "recovery", lambda job: ops.run_recovery(rt, app, job, body.backend, params, body.confirm, ip))


@router.get("/recovery/jobs/{job_id}")
async def recovery_job(request: Request, job_id: str, authorization: str | None = Header(None)):
    if (refused := _key_problem(request, authorization)) is not None:
        return refused
    return _job(_runtime(request), job_id, {"recovery", "reconnect"})

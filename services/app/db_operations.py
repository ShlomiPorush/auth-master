"""The database operations of setup, the Settings page and the recovery page.

Each check opens the target, reports what it holds and what the service would do, and
changes nothing.  Each operation runs as a job (app.db_runtime.Jobs) and ends with a switch
(Runtime.switch); every refusal happens before the commit point and changes nothing.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app import db_config, db_inspect, installation
from app.db_config import Config, PgParams
from app.db_runtime import READY, RECOVERY, SETUP, Job, OperationError, Runtime, message
from app.schema import ensure_database_schema

SAME = "same"
COPY = "different_copy"


@dataclass
class CheckResult:
    state: str  # "new" (no SQLite file yet) or a db_inspect kind
    has_admin: bool = False
    problems: list[str] = field(default_factory=list)
    others: list[str] = field(default_factory=list)
    installation_id: str | None = None
    pending: bool = False
    # Settings page: "same" (the live database) or "different_copy"; None when not checked.
    identity: str | None = None

    def view(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "hasAdmin": self.has_admin,
            "problems": self.problems,
            "others": self.others,
            "pending": self.pending,
            "identity": self.identity,
        }


async def _open_postgres(p: PgParams) -> Any:
    try:
        return await db_config.open_postgres(p, min_size=1, max_size=2)
    except Exception as e:
        raise OperationError(db_config.classify(e)) from None


async def _close(db: Any) -> None:
    try:
        await db.close()
    except Exception:
        pass


async def _inspect(db: Any) -> CheckResult:
    state = await db_inspect.inspect(db)
    result = CheckResult(state.kind, has_admin=state.has_admin, others=list(state.others))
    if state.installation is not None:
        result.installation_id = state.installation.id
        result.pending = state.installation.status != installation.ACTIVE
    if db.is_pg:
        tables_exist = state.kind in (db_inspect.INSTALLATION, db_inspect.OLDER)
        result.problems = await db_inspect.privilege_problems(db, tables_exist=tables_exist)
    return result


# ── Checks (read-only) ──────────────────────────────────────────

async def check_postgres(p: PgParams, current_db: Any = None) -> CheckResult:
    """Open `p` and report.  With `current_db` (the Settings page), also whether `p`
    reaches the same live database (same_live_database writes a random value to each)."""
    db = await _open_postgres(p)
    try:
        result = await _inspect(db)
        if current_db is not None and result.state == db_inspect.INSTALLATION and not result.pending:
            current = await installation.read(current_db)
            if current is not None and current.id == result.installation_id:
                same = await db_inspect.same_live_database(current_db, db)
                result.identity = SAME if same else COPY
        return result
    except OperationError:
        raise
    except Exception as e:
        raise OperationError(db_config.classify(e)) from None
    finally:
        await _close(db)


async def check_sqlite(path: Path) -> CheckResult:
    if not path.exists():
        return CheckResult("new")
    db = await db_config.open_sqlite(str(path))
    try:
        return await _inspect(db)
    finally:
        await _close(db)


# ── Shared steps ────────────────────────────────────────────────

def _refuse_state(result: CheckResult, *, expected_id: str | None) -> None:
    if result.state == db_inspect.FOREIGN:
        raise OperationError(
            "foreign_database",
            "The database holds tables that are not this service's"
            + (f" ({', '.join(result.others)})" if result.others else "")
            + ". Use an empty database or one that holds this service's data.",
            others=result.others,
        )
    if result.state == db_inspect.INCOMPLETE:
        raise OperationError(
            "incomplete_installation",
            "The database holds this service's installation id, but these tables are missing: "
            + ", ".join(result.others) + ". Restore the whole database, then try again.",
        )
    if result.state == db_inspect.INSTALLATION and result.pending:
        raise OperationError("installation_pending")
    if result.state == db_inspect.INSTALLATION and expected_id is not None and result.installation_id != expected_id:
        raise OperationError("other_installation")


def _refuse_problems(result: CheckResult) -> None:
    if result.problems:
        raise OperationError(
            "permission_denied",
            "The database user cannot be used: " + " ".join(result.problems),
            problems=result.problems,
        )


async def _create_postgres_installation(db: Any, installation_id: str) -> None:
    """Schema and installation row in one transaction (an empty database, or adoption of
    an older installation, whose missing tables and columns are added)."""
    async with db.transaction() as tx:
        await ensure_database_schema(tx)
        if await installation.read(tx) is None:
            await installation.create(tx, installation_id)


async def _adopt_sqlite(db: Any, installation_id: str) -> None:
    await installation.adopt_sqlite(db, installation_id)


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


async def _create_sqlite(path: Path, installation_id: str) -> None:
    """A new SQLite database at `path`: built in a temporary file without WAL (so every
    committed page is in the main file), flushed, then published with a hard link, which
    fails if `path` exists (no overwrite)."""
    import aiosqlite  # type: ignore[import-untyped]

    from app.db import SqliteDatabase

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.setup-{uuid.uuid4().hex}")
    conn = await aiosqlite.connect(str(tmp), isolation_level=None)
    conn.row_factory = None
    try:
        await conn.execute("PRAGMA journal_mode=DELETE")
        await conn.execute("PRAGMA foreign_keys=ON")
        db = SqliteDatabase(conn)
        db.path = str(tmp)
        await ensure_database_schema(db)
        await installation.create(db, installation_id)
    finally:
        await conn.close()
    try:
        _fsync(tmp)
        try:
            os.link(tmp, path)
        except FileExistsError:
            raise OperationError(
                "sqlite_exists",
                "data/auth.db was created by something else meanwhile. Check the connection again.",
            ) from None
        if os.name == "posix":
            _fsync(path.parent)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


async def _log(db: Any, actor: str, action: str, details: dict[str, Any], ip: str | None) -> str | None:
    """Activity row in the database now in use; a failure is reported, not raised."""
    from app.logger import log_activity

    try:
        # Bounded: it runs inside the switch, which holds the operation lock.
        await asyncio.wait_for(log_activity(db, actor=actor, action=action, entity_type="settings",
                                            entity_name="database", details=details, ip_address=ip), 5)
        return None
    except Exception as e:
        print(f"Database change done, but the activity log entry failed ({type(e).__name__})")
        return "The change is done, but the activity log entry could not be written."


def _public(cfg: Config) -> dict[str, Any]:
    if cfg.backend == db_config.SQLITE:
        return {"backend": "sqlite", "path": cfg.sqlite_path}
    assert cfg.pg is not None
    p = cfg.pg.public()
    return {"backend": "postgres", "host": p["host"], "port": p["port"], "database": p["database"],
            "user": p["user"], "sslmode": p["sslmode"]}


# ── Setup ───────────────────────────────────────────────────────

async def run_setup(rt: Runtime, app: Any, job: Job, backend: str, params: PgParams | None, adopt: bool,
                    ip: str | None) -> dict[str, Any]:
    if rt.mode != SETUP:
        raise OperationError("already_configured", "The database is already set up.")
    job.step = "checking the database"
    if backend == db_config.SQLITE:
        path = db_config.default_sqlite_path()
        result = await check_sqlite(path)
        _refuse_state(result, expected_id=None)
        if result.state == "new":
            iid = installation.new_id()
            job.step = "creating the database"
            await _create_sqlite(path, iid)
            action = "database.created"
        elif result.state == db_inspect.EMPTY:
            raise OperationError(
                "sqlite_exists",
                "data/auth.db exists but is empty. Move it out of the data directory, then try again.",
            )
        elif result.state == db_inspect.OLDER:
            if not adopt:
                raise OperationError("confirm_adopt", "Confirm that the service should adopt this database.")
            db = await db_config.open_sqlite(str(path))
            try:
                iid = installation.new_id()
                await _adopt_sqlite(db, iid)
            finally:
                await _close(db)
            action = "database.adopted"
        else:
            iid = result.installation_id
            action = "database.existing_installation"
        new_db = await db_config.open_sqlite(str(path))
        new_cfg = Config(db_config.SQLITE, iid, sqlite_path=str(path))
    else:
        assert params is not None
        new_db = await _open_postgres(params)
        try:
            result = await _inspect(new_db)
            _refuse_state(result, expected_id=None)
            _refuse_problems(result)
            if result.state == db_inspect.EMPTY:
                iid = installation.new_id()
                job.step = "creating the tables"
                await _create_postgres_installation(new_db, iid)
                action = "database.created"
            elif result.state == db_inspect.OLDER:
                if not adopt:
                    raise OperationError("confirm_adopt", "Confirm that the service should adopt this database.")
                iid = installation.new_id()
                await _create_postgres_installation(new_db, iid)
                action = "database.adopted"
            else:
                iid = result.installation_id
                action = "database.existing_installation"
        except BaseException:
            await _close(new_db)
            raise
        new_cfg = Config(db_config.POSTGRES, iid, pg=params)

    logged: dict[str, Any] = {}

    async def after() -> None:
        logged["warning"] = await _log(rt.db, "setup", action, _public(new_cfg), ip)

    await _switch(rt, app, job, new_db, new_cfg, after=after)
    has_admin = await db_inspect.has_enabled_admin(rt.db)
    return {"next": "login" if has_admin else "register", "action": action, "warning": logged.get("warning")}


async def _switch(rt: Runtime, app: Any, job: Job, new_db: Any, new_cfg: Config, verify: Any = None,
                  after: Any = None) -> None:
    """`after` writes the activity log entry inside the switch, so a switch that happened is
    logged even when the job is cancelled at that moment."""
    try:
        await rt.switch(app, job, new_db=new_db, new_config=new_cfg, verify=verify, after=after)
    except BaseException as e:
        # Closed here only while it is still the caller's: once attached (even when this job
        # was cancelled after the commit point) it is the runtime's, and after a failed
        # commit the switch closed it.
        if rt.db is not new_db and not (isinstance(e, OperationError) and e.code == "attach_failed"):
            await _close(new_db)
        raise


# ── Settings page: a new connection to the same installation ────

async def run_edit(rt: Runtime, app: Any, job: Job, params: PgParams, confirm_copy: bool, actor: str,
                   ip: str | None) -> dict[str, Any]:
    cfg = rt.config
    if rt.mode != READY or rt.source != "file" or cfg is None or cfg.backend != db_config.POSTGRES:
        raise OperationError("not_editable", "This connection cannot be changed here.")
    job.step = "connecting"
    new_db = await _open_postgres(params)
    try:
        result = await _inspect(new_db)
        _refuse_state(result, expected_id=cfg.installation_id)
        if result.state == db_inspect.EMPTY:
            raise OperationError("database_empty", "The database is empty. Choose the database that holds this service's data.")
        _refuse_problems(result)
        verify = None
        if result.state == db_inspect.OLDER:
            if not confirm_copy:
                raise OperationError("confirm_copy", COPY_TEXT_OLDER, identity="older")
            await _create_postgres_installation(new_db, cfg.installation_id)
            action = "database.adopted"
        else:
            same = await db_inspect.same_live_database(rt.db, new_db)
            if same:
                action = "database.connection_changed"

                async def verify() -> None:
                    if not await db_inspect.same_live_database(rt.db, new_db):
                        raise OperationError("different_copy", COPY_TEXT)
            else:
                if not confirm_copy:
                    raise OperationError("confirm_copy", COPY_TEXT, identity=COPY)
                action = "database.restored_copy_selected"
    except BaseException:
        await _close(new_db)
        raise
    new_cfg = Config(db_config.POSTGRES, cfg.installation_id, pg=params)
    before = _public(cfg)
    logged: dict[str, Any] = {}

    async def after() -> None:
        logged["warning"] = await _log(rt.db, actor, action, {"from": before, "to": _public(new_cfg)}, ip)

    await _switch(rt, app, job, new_db, new_cfg, verify, after=after)
    return {"action": action, "warning": logged.get("warning")}


COPY_TEXT = (
    "This is a different copy of this service's database, for example a restored backup. "
    "Changes made after the copy was taken will be lost. Confirm to use it."
)
COPY_TEXT_OLDER = (
    "This database holds this service's data from an older version or a backup. Changes made "
    "after it was taken will be lost. Confirm to adopt it."
)


# ── Recovery page ───────────────────────────────────────────────

async def run_recovery(rt: Runtime, app: Any, job: Job, backend: str, params: PgParams | None, confirm: bool,
                       ip: str | None) -> dict[str, Any]:
    if rt.mode != RECOVERY:
        raise OperationError("not_in_recovery", "The service is not in recovery mode.")
    expected = rt.known_installation_id
    job.step = "connecting"
    if backend == db_config.SQLITE:
        cfg = rt.config
        path = Path(cfg.sqlite_path) if cfg is not None and cfg.backend == db_config.SQLITE and cfg.sqlite_path else db_config.default_sqlite_path()
        if not path.is_file():
            raise OperationError("sqlite_missing")
        new_db = await db_config.open_sqlite(str(path))
    else:
        assert params is not None
        path = None
        new_db = await _open_postgres(params)
    try:
        result = await _inspect(new_db)
        _refuse_state(result, expected_id=expected)
        if result.state == db_inspect.EMPTY:
            raise OperationError("database_empty", "The database is empty. Choose the database that holds this service's data.")
        _refuse_problems(result)
        if not confirm:
            raise OperationError(
                "confirm_recovery",
                "The service cannot tell whether this is the latest copy of its database. If it is "
                "a restored backup or a replica, changes made after it was taken are lost. Confirm to use it.",
                state=result.state,
            )
        if result.state == db_inspect.OLDER:
            iid = expected or installation.new_id()
            if new_db.is_pg:
                await _create_postgres_installation(new_db, iid)
            else:
                await _adopt_sqlite(new_db, iid)
            action = "database.adopted"
        else:
            iid = result.installation_id
            action = "database.connection_recovered"
    except BaseException:
        await _close(new_db)
        raise
    if backend == db_config.SQLITE:
        new_cfg = Config(db_config.SQLITE, iid, sqlite_path=str(path))
    else:
        new_cfg = Config(db_config.POSTGRES, iid, pg=params)
    logged: dict[str, Any] = {}

    async def after() -> None:
        rt.attempts.reset()
        logged["warning"] = await _log(rt.db, "master_api_key", action, _public(new_cfg), ip)

    await _switch(rt, app, job, new_db, new_cfg, after=after)
    return {"action": action, "warning": logged.get("warning")}


def failure_view(rt: Runtime) -> dict[str, Any]:
    cfg = rt.config
    return {
        "mode": rt.mode,
        "code": rt.failure_code,
        "message": rt.failure_text,
        "configured": _public(cfg) if cfg is not None else None,
        "installationId": rt.known_installation_id,
        "sqliteFile": db_config.default_sqlite_path().is_file(),
    }


__all__ = [
    "CheckResult", "check_postgres", "check_sqlite", "run_setup", "run_edit", "run_recovery",
    "failure_view", "message",
]

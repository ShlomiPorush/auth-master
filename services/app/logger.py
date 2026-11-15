import json
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any
from fastapi import Request
from app.db import Database
from app.usage_rollup import PURGED_BEFORE_KEY, PurgeMark, purge_access_logs

# Longest area, in characters, that /validate accepts and the access log stores.
# Zone names are at most 63 characters; the rest leaves room for surrounding spaces.
MAX_AREA_LEN = 255


async def get_actor(request: Request, sess: dict | None = None) -> str:
    """Resolve the current actor's name from session or request state."""
    if sess and "adminId" in sess:
        db = request.app.state.db
        username = await db.fetchval("SELECT username FROM admin_users WHERE id = $1", sess["adminId"])
        return username or f"admin:{sess['adminId']}"
    
    if hasattr(request.state, "actor"):
        return request.state.actor
        
    return "system"


async def log_activity(
    db: Database,
    actor: str,
    action: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    entity_name: str | None = None,
    details: dict | None = None,
    ip_address: str | None = None,
) -> None:
    """Insert a new record into activity_logs."""
    details_str = json.dumps(details) if details is not None else None
    log_id = str(uuid.uuid4())
    
    if db.is_sqlite:
        await db.execute(
            "INSERT INTO activity_logs (id, actor, action, entity_type, entity_id, entity_name, details, ip_address) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            log_id, actor, action, entity_type, entity_id, entity_name, details_str, ip_address
        )
    else:
        await db.execute(
            "INSERT INTO activity_logs (id, actor, action, entity_type, entity_id, entity_name, details, ip_address) VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8)",
            log_id, actor, action, entity_type, entity_id, entity_name, details_str, ip_address
        )


async def log_access(
    db: Database,
    token_id: str | None,
    token_name: str | None,
    area: str,
    level: str,
    result: bool,
    ip_address: str | None,
) -> None:
    """Insert a new record into access_logs, with at most MAX_AREA_LEN characters of area."""
    log_id = str(uuid.uuid4())
    area = area[:MAX_AREA_LEN]

    if db.is_sqlite:
        await db.execute(
            "INSERT INTO access_logs (id, token_id, token_name, area, level, result, ip_address) VALUES ($1, $2, $3, $4, $5, $6, $7)",
            log_id, token_id, token_name, area, level, int(result), ip_address
        )
    else:
        await db.execute(
            "INSERT INTO access_logs (id, token_id, token_name, area, level, result, ip_address) VALUES ($1::uuid, $2, $3, $4, $5, $6, $7)",
            log_id, token_id, token_name, area, level, result, ip_address
        )


_LOGS = (
    ("activity", "activity_logs", "activityLogRetentionDays"),
    ("access", "access_logs", "accessLogRetentionDays"),
)

# Only while no settings version was saved since the purge read the settings.
_VERSION_UNCHANGED = "(SELECT COALESCE(MAX(version), 0) FROM app_settings) = $2"


def _bind_time(db: Database, value: datetime):
    # SQLite stores created_at as "YYYY-MM-DD HH:MM:SS" in UTC.
    return value.strftime("%Y-%m-%d %H:%M:%S") if db.is_sqlite else value


async def init_purged_before(db: Database, retention_days: int) -> None:
    """Once per database: installs that ran an older version kept only the access log rows
    of the last `retention_days` days, so data before that start is not claimed."""
    start = datetime.now(timezone.utc) - timedelta(days=retention_days)
    await db.execute(
        "INSERT INTO app_state (key, value) VALUES ($1, $2) ON CONFLICT (key) DO NOTHING",
        PURGED_BEFORE_KEY, PurgeMark(start).encode(),
    )


async def read_purged_before(db: Any) -> PurgeMark | None:
    value = await db.fetchval("SELECT value FROM app_state WHERE key = $1", PURGED_BEFORE_KEY)
    try:
        return PurgeMark.decode(value)
    except (TypeError, ValueError, OverflowError):
        return None


async def purge_old_logs(db: Database, settings_db: Database) -> None:
    """Delete log rows older than their retention, only while this process uses the
    retention that is saved (app.general_settings.purge_mismatch).

    Each DELETE also requires that no settings version was saved since the read, so a save
    in between stops it until the next round.  Access log rows go in chunks, each with the
    Usage summary state and the purge boundary in its transaction (app.usage_rollup)."""
    from app import general_settings as gs

    try:
        stored = await gs.read_current(settings_db)
    except Exception as e:
        reason = e.message if isinstance(e, gs.InvalidSettings) else f"Could not read the saved settings ({type(e).__name__})."
        for kind, _table, _name in _LOGS:
            gs.record_purge(kind, "skipped", reason)
        return

    eff = gs.current()
    now = datetime.now(timezone.utc)
    for kind, table, name in _LOGS:
        days = eff.value(name)
        problem = gs.purge_mismatch(name, days, stored)
        if problem:
            gs.record_purge(kind, "skipped", problem)
            continue
        cutoff = now - timedelta(days=days)
        try:
            if kind == "access":
                await purge_access_logs(db, cutoff, settings_version=stored.version)
            else:
                await db.execute(
                    f"DELETE FROM {table} WHERE created_at < $1 AND {_VERSION_UNCHANGED}",
                    _bind_time(db, cutoff), stored.version,
                )
        except Exception as e:
            gs.record_purge(kind, "skipped", f"The purge failed ({type(e).__name__}); it runs again within a minute.")
            continue
        gs.record_purge(kind, "ok", None)

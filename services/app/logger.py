import json
import uuid
from datetime import datetime, timezone, timedelta
from fastapi import Request
from app.db import Database


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
    """Insert a new record into access_logs."""
    log_id = str(uuid.uuid4())
    
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


async def purge_old_logs(db: Database, activity_days: int, access_days: int) -> None:
    """Delete logs older than retention settings."""
    now_utc = datetime.now(timezone.utc)
    activity_cutoff = now_utc - timedelta(days=activity_days)
    access_cutoff = now_utc - timedelta(days=access_days)
    
    if db.is_sqlite:
        # SQLite uses "YYYY-MM-DD HH:MM:SS" (UTC) format
        act_str = activity_cutoff.strftime("%Y-%m-%d %H:%M:%S")
        acc_str = access_cutoff.strftime("%Y-%m-%d %H:%M:%S")
        await db.execute("DELETE FROM activity_logs WHERE created_at < $1", act_str)
        await db.execute("DELETE FROM access_logs WHERE created_at < $1", acc_str)
    else:
        await db.execute("DELETE FROM activity_logs WHERE created_at < $1", activity_cutoff)
        await db.execute("DELETE FROM access_logs WHERE created_at < $1", access_cutoff)

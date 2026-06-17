from __future__ import annotations

import json
from typing import Any
from fastapi import APIRouter, Header, HTTPException, Request, Query
from app.admin_deps import FullSession
from app.datetime_utils import fmt_datetime

router = APIRouter(prefix="/admin/api/logs", tags=["admin-logs"])


@router.get("/activity")
async def get_activity_logs(
    request: Request,
    _sess: FullSession,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=100),
    action: str | None = Query(default=None),
    search: str | None = Query(default=None),
):
    db = request.app.state.db
    limit = max(1, min(100, limit))
    offset = (page - 1) * limit

    where_parts = []
    args = []
    n = 1

    if action:
        where_parts.append(f"action = ${n}")
        args.append(action)
        n += 1

    if search:
        where_parts.append(f"(LOWER(actor) LIKE ${n} OR LOWER(entity_name) LIKE ${n} OR LOWER(action) LIKE ${n})")
        args.append(f"%{search.strip().lower()}%")
        n += 1

    where_clause = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

    # Fetch total count
    count_q = f"SELECT COUNT(*) FROM activity_logs {where_clause}"
    total = await db.fetchval(count_q, *args)

    # Fetch rows
    args.extend([limit, offset])
    q = f"SELECT id, actor, action, entity_type, entity_id, entity_name, details, ip_address, created_at FROM activity_logs {where_clause} ORDER BY created_at DESC LIMIT ${n} OFFSET ${n+1}"
    rows = await db.fetch(q, *args)

    logs = []
    for r in rows:
        details_val = None
        if r["details"]:
            try:
                details_val = json.loads(r["details"])
            except Exception:
                details_val = r["details"]

        logs.append({
            "id": str(r["id"]),
            "actor": r["actor"],
            "action": r["action"],
            "entityType": r["entity_type"],
            "entityId": r["entity_id"],
            "entityName": r["entity_name"],
            "details": details_val,
            "ipAddress": r["ip_address"] or "unknown",
            "createdAt": fmt_datetime(r["created_at"]),
        })

    return {
        "logs": logs,
        "total": total or 0,
        "page": page,
        "limit": limit
    }


@router.get("/access")
async def get_access_logs(
    request: Request,
    _sess: FullSession,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=100),
    result: bool | None = Query(default=None),
    search: str | None = Query(default=None),
    token_id: str | None = Query(default=None),
):
    db = request.app.state.db
    limit = max(1, min(100, limit))
    offset = (page - 1) * limit

    where_parts = []
    args = []
    n = 1

    if result is not None:
        if db.is_sqlite:
            where_parts.append(f"result = ${n}")
            args.append(int(result))
        else:
            where_parts.append(f"result = ${n}")
            args.append(result)
        n += 1

    if search:
        where_parts.append(f"(LOWER(token_name) LIKE ${n} OR LOWER(area) LIKE ${n})")
        args.append(f"%{search.strip().lower()}%")
        n += 1

    if token_id:
        where_parts.append(f"token_id = ${n}")
        args.append(token_id)
        n += 1

    where_clause = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

    # Fetch total count
    count_q = f"SELECT COUNT(*) FROM access_logs {where_clause}"
    total = await db.fetchval(count_q, *args)

    # Fetch rows
    args.extend([limit, offset])
    q = f"""
        SELECT 
            al.id, 
            al.token_id, 
            COALESCE(al.token_name, t.name) AS token_name, 
            al.area, 
            al.level, 
            al.result, 
            al.ip_address, 
            al.created_at 
        FROM access_logs al
        LEFT JOIN tokens t ON al.token_id = t.id
        {where_clause} 
        ORDER BY al.created_at DESC 
        LIMIT ${n} OFFSET ${n+1}
    """
    rows = await db.fetch(q, *args)

    logs = []
    for r in rows:
        logs.append({
            "id": str(r["id"]),
            "tokenId": r["token_id"],
            "tokenName": r["token_name"] or "Unknown / Deleted",
            "area": r["area"],
            "level": r["level"],
            "result": bool(r["result"]),
            "ipAddress": r["ip_address"] or "unknown",
            "createdAt": fmt_datetime(r["created_at"]),
        })

    return {
        "logs": logs,
        "total": total or 0,
        "page": page,
        "limit": limit
    }

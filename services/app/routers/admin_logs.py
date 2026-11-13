from __future__ import annotations

import base64
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any
from fastapi import APIRouter, Header, HTTPException, Request, Query
from app.admin_deps import FullSession
from app.datetime_utils import fmt_datetime

router = APIRouter(prefix="/admin/api/logs", tags=["admin-logs"])

# Cursors
#
# Rows are listed by (created_at DESC, id DESC). A cursor is the base64url
# (unpadded) form of the compact JSON {"t": <created_at as stored>, "i": <id>}
# of one row: on SQLite "t" is the stored text "YYYY-MM-DD HH:MM:SS" (UTC), on
# PostgreSQL the ISO 8601 form of the timestamptz value.
#
# before=<cursor>: rows strictly after that row in the order.
# since=<cursor>:  rows with created_at >= t - SINCE_LOOKBACK. On PostgreSQL
#                  created_at is now() at transaction start, so a row can commit
#                  after a later one; log writes are single autocommit INSERTs,
#                  so the gap is milliseconds and the lookback covers it.
SINCE_LOOKBACK = timedelta(seconds=60)
DEFAULT_MAX_LIMIT = 100
SINCE_MAX_LIMIT = 500
_SQLITE_TS = "%Y-%m-%d %H:%M:%S"
_CURSOR_MAX_LEN = 512
_B64URL = re.compile(r"[A-Za-z0-9_-]+")


def _encode_cursor(created_at: Any, row_id: Any) -> str:
    t = created_at.isoformat() if isinstance(created_at, datetime) else str(created_at)
    raw = json.dumps({"t": t, "i": str(row_id)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(db, value: str) -> tuple[datetime, Any, str]:
    """Return (instant, value to bind for created_at, id) or raise HTTP 400."""
    try:
        if len(value) > _CURSOR_MAX_LEN or not _B64URL.fullmatch(value):
            raise ValueError("bad alphabet")
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        data = json.loads(decoded.decode("utf-8"))
        if not isinstance(data, dict) or set(data) != {"t", "i"}:
            raise ValueError("bad shape")
        t, i = data["t"], data["i"]
        if not isinstance(t, str) or not isinstance(i, str):
            raise ValueError("bad types")
        # Canonical lowercase form only: SQLite compares ids as text.
        if str(uuid.UUID(i)) != i:
            raise ValueError("bad id")
        if db.is_sqlite:
            instant = datetime.strptime(t, _SQLITE_TS).replace(tzinfo=timezone.utc)
            # strptime also accepts unpadded fields; only the stored form compares correctly.
            if instant.strftime(_SQLITE_TS) != t:
                raise ValueError("bad timestamp")
            bound: Any = t
        else:
            instant = datetime.fromisoformat(t)
            if instant.tzinfo is None:
                raise ValueError("bad timestamp")
            instant = instant.astimezone(timezone.utc)
            bound = instant
        # Fail here, not in the driver, for instants at the edge of the range.
        instant - SINCE_LOOKBACK
    except (ValueError, TypeError, OverflowError) as e:
        raise HTTPException(400, "Invalid cursor") from e
    return instant, bound, i


def _cursor_conditions(db, before: str | None, since: str | None, n: int, prefix: str = "") -> tuple[list[str], list[Any], int]:
    """SQL conditions and bind values for the before/since cursors (never interpolated)."""
    parts: list[str] = []
    args: list[Any] = []
    if before is not None:
        _instant, bound, row_id = _decode_cursor(db, before)
        parts.append(
            f"({prefix}created_at < ${n}::timestamptz OR "
            f"({prefix}created_at = ${n}::timestamptz AND {prefix}id < ${n + 1}::uuid))"
        )
        args.extend([bound, row_id])
        n += 2
    if since is not None:
        instant, _bound, _row_id = _decode_cursor(db, since)
        start = instant - SINCE_LOOKBACK
        parts.append(f"{prefix}created_at >= ${n}::timestamptz")
        args.append(start.strftime(_SQLITE_TS) if db.is_sqlite else start)
        n += 1
    return parts, args, n


def _cursors(rows: list) -> dict[str, str | None]:
    if not rows:
        return {"nextCursor": None, "headCursor": None}
    return {
        "nextCursor": _encode_cursor(rows[-1]["created_at"], rows[-1]["id"]),
        "headCursor": _encode_cursor(rows[0]["created_at"], rows[0]["id"]),
    }


def _checked_limit(limit: int, since: str | None) -> int:
    """Up to 100 rows per request, as before; up to 500 only for a since refresh."""
    if since is None and limit > DEFAULT_MAX_LIMIT:
        raise HTTPException(422, f"limit must be at most {DEFAULT_MAX_LIMIT} without since")
    return limit


@router.get("/activity")
async def get_activity_logs(
    request: Request,
    _sess: FullSession,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=SINCE_MAX_LIMIT, description="At most 100, or at most 500 together with since."),
    action: str | None = Query(default=None),
    search: str | None = Query(default=None),
    before: Annotated[str | None, Query()] = None,
    since: Annotated[str | None, Query()] = None,
):
    db = request.app.state.db
    limit = _checked_limit(limit, since)
    # A cursor gives the position; the page number is ignored.
    offset = 0 if before is not None or since is not None else (page - 1) * limit

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

    cursor_parts, cursor_args, n = _cursor_conditions(db, before, since, n)

    where_clause = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

    # Fetch total count (filters only, cursors ignored)
    count_q = f"SELECT COUNT(*) FROM activity_logs {where_clause}"
    total = await db.fetchval(count_q, *args)

    # Fetch rows
    row_parts = where_parts + cursor_parts
    row_where = f"WHERE {' AND '.join(row_parts)}" if row_parts else ""
    args.extend(cursor_args)
    args.extend([limit, offset])
    q = f"SELECT id, actor, action, entity_type, entity_id, entity_name, details, ip_address, created_at FROM activity_logs {row_where} ORDER BY created_at DESC, id DESC LIMIT ${n} OFFSET ${n+1}"
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
        "limit": limit,
        **_cursors(rows),
    }


@router.get("/access")
async def get_access_logs(
    request: Request,
    _sess: FullSession,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=SINCE_MAX_LIMIT, description="At most 100, or at most 500 together with since."),
    result: bool | None = Query(default=None),
    search: str | None = Query(default=None),
    token_id: str | None = Query(default=None),
    before: Annotated[str | None, Query()] = None,
    since: Annotated[str | None, Query()] = None,
):
    db = request.app.state.db
    limit = _checked_limit(limit, since)
    # A cursor gives the position; the page number is ignored.
    offset = 0 if before is not None or since is not None else (page - 1) * limit

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

    # tokens also has id and created_at, so the cursor columns are qualified.
    cursor_parts, cursor_args, n = _cursor_conditions(db, before, since, n, prefix="al.")

    where_clause = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

    # Fetch total count (filters only, cursors ignored)
    count_q = f"SELECT COUNT(*) FROM access_logs {where_clause}"
    total = await db.fetchval(count_q, *args)

    # Fetch rows
    row_parts = where_parts + cursor_parts
    row_where = f"WHERE {' AND '.join(row_parts)}" if row_parts else ""
    args.extend(cursor_args)
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
        LEFT JOIN tokens t ON al.token_id = CAST(t.id AS TEXT)
        {row_where}
        ORDER BY al.created_at DESC, al.id DESC
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
        "limit": limit,
        **_cursors(rows),
    }

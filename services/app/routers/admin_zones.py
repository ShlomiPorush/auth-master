import json
import re
import uuid as uuid_mod
from typing import Any

from app.datetime_utils import fmt_datetime

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from app.admin_deps import FullSession, csrf_check
from app.db import UniqueViolationError, write_transaction
from app.logger import log_activity, get_actor

router = APIRouter(prefix="/admin/api", tags=["admin-zones"])

_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,62}$")


class ZoneCreate(BaseModel):
    name: str
    description: str = ""


class ZonePatch(BaseModel):
    name: str | None = None
    description: str | None = None




@router.get("/zones")
async def list_zones(request: Request, _sess: FullSession):
    db = request.app.state.db
    rows = await db.fetch("SELECT id, name, description, created_at FROM zones ORDER BY name")
    return [
        {
            "id": str(r["id"]),
            "name": r["name"],
            "description": r["description"] or "",
            "createdAt": fmt_datetime(r["created_at"]),
        }
        for r in rows
    ]


@router.post("/zones", status_code=201)
async def create_zone(
    request: Request,
    sess: FullSession,
    body: ZoneCreate,
    x_csrf_token: str | None = Header(None, alias="X-CSRF-Token"),
):
    csrf_check(sess, x_csrf_token)
    name = body.name.strip()
    if not name or not _NAME_RE.match(name):
        raise HTTPException(400, "Invalid zone name (1-63 chars, start alphanumeric; allowed: . _ : -)")
    db = request.app.state.db
    new_id = str(uuid_mod.uuid4())
    try:
        if db.is_sqlite:
            await db.execute(
                "INSERT INTO zones (id, name, description) VALUES ($1, $2, $3)",
                new_id, name, (body.description or "").strip(),
            )
            zid = new_id
        else:
            zid = await db.fetchval(
                "INSERT INTO zones (name, description) VALUES ($1, $2) RETURNING id",
                name, (body.description or "").strip(),
            )
    except UniqueViolationError as e:
        raise HTTPException(409, "Zone already exists") from e

    actor = await get_actor(request, sess)
    ip = request.client.host if request.client else "unknown"
    await log_activity(
        db,
        actor=actor,
        action="zone_create",
        entity_type="zone",
        entity_id=str(zid),
        entity_name=name,
        details={"description": (body.description or "").strip()},
        ip_address=ip,
    )

    return {"id": str(zid), "name": name, "description": (body.description or "").strip()}


@router.patch("/zones/{zone_id}")
async def patch_zone(
    request: Request,
    sess: FullSession,
    zone_id: str,
    body: ZonePatch,
    x_csrf_token: str | None = Header(None, alias="X-CSRF-Token"),
):
    csrf_check(sess, x_csrf_token)
    db = request.app.state.db
    try:
        uid = uuid_mod.UUID(zone_id)
    except ValueError as e:
        raise HTTPException(400, "Invalid id") from e

    row = await db.fetchrow("SELECT name FROM zones WHERE id = $1", str(uid))
    if row is None:
        raise HTTPException(404, "Not found")
    old_name = row["name"]

    parts: list[str] = []
    args: list[Any] = []
    n = 1

    if body.name is not None:
        nm = body.name.strip()
        if not nm or not _NAME_RE.match(nm):
            raise HTTPException(400, "Invalid zone name")
        parts.append(f"name = ${n}")
        args.append(nm)
        n += 1
    if body.description is not None:
        parts.append(f"description = ${n}")
        args.append(body.description.strip())
        n += 1
    if not parts:
        raise HTTPException(400, "No updates")

    if db.is_pg:
        await _patch_zone_pg(db, uid, body, parts, args, n)
    else:
        await _patch_zone_sqlite(db, uid, body, parts, args, n)

    actor = await get_actor(request, sess)
    ip = request.client.host if request.client else "unknown"
    await log_activity(
        db,
        actor=actor,
        action="zone_patch",
        entity_type="zone",
        entity_id=zone_id,
        entity_name=body.name.strip() if body.name is not None else old_name,
        details={
            "updates": {
                k: v for k, v in {"name": body.name, "description": body.description}.items() if v is not None
            }
        },
        ip_address=ip,
    )

    return {"ok": True}


async def _patch_zone_pg(db: Any, uid: uuid_mod.UUID, body: ZonePatch, parts: list[str], args: list[Any], n: int) -> None:
    """PostgreSQL implementation using jsonb functions inside a transaction."""
    async with db.transaction() as conn:
        row = await conn.fetchrow("SELECT name FROM zones WHERE id = $1::uuid FOR UPDATE", uid)
        if row is None:
            raise HTTPException(404, "Not found")
        old_name = row["name"]
        args.append(uid)
        q = f"UPDATE zones SET {', '.join(parts)} WHERE id = ${n}::uuid"
        try:
            await conn.execute(q, *args)
        except UniqueViolationError as e:
            raise HTTPException(409, "Zone name already exists") from e

        if body.name is not None and body.name.strip() != old_name:
            new_name = body.name.strip()
            await conn.execute(
                """
                UPDATE tokens t
                SET grants = (
                    SELECT jsonb_agg(
                        CASE WHEN elem->'area' = to_jsonb($1::text)
                             THEN jsonb_set(elem, '{area}', to_jsonb($2::text))
                             ELSE elem
                        END
                    )
                    FROM jsonb_array_elements(t.grants) AS elem
                )
                WHERE EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(t.grants) AS elem
                    WHERE elem->'area' = to_jsonb($1::text)
                )
                """,
                old_name,
                new_name,
            )


async def _rewrite_grants_sqlite(tx: Any, area: str, new_area: str | None) -> None:
    """Rename (``new_area``) or remove (``None``) every grant on ``area``.

    Runs on the zone change's write transaction, which holds SQLite's write lock from
    before this read until its commit, so no grant change can land between the read and
    the rewrite, and the zone change and the rewrite are saved together or not at all.
    The grants are decoded as /validate decodes them (json.loads: a repeated key counts
    with its last value; only objects are grants); other elements are kept unchanged.
    """
    for row in await tx.fetch("SELECT id, grants FROM tokens"):
        grants = json.loads(row["grants"])
        if not isinstance(grants, list):
            continue
        kept: list[Any] = []
        changed = False
        for g in grants:
            if isinstance(g, dict) and g.get("area") == area:
                changed = True
                if new_area is None:
                    continue
                g = {**g, "area": new_area}
            kept.append(g)
        if changed:
            await tx.execute("UPDATE tokens SET grants = $1 WHERE id = $2", json.dumps(kept), row["id"])


async def _patch_zone_sqlite(db: Any, uid: uuid_mod.UUID, body: ZonePatch, parts: list[str], args: list[Any], n: int) -> None:
    """SQLite implementation: the zone update and the grant rewrite in one write transaction."""
    async with write_transaction(db) as tx:
        row = await tx.fetchrow("SELECT name FROM zones WHERE id = $1", str(uid))
        if row is None:
            raise HTTPException(404, "Not found")
        old_name = row["name"]
        args.append(str(uid))
        q = f"UPDATE zones SET {', '.join(parts)} WHERE id = ${n}"
        try:
            await tx.execute(q, *args)
        except UniqueViolationError as e:
            raise HTTPException(409, "Zone name already exists") from e

        if body.name is not None and body.name.strip() != old_name:
            await _rewrite_grants_sqlite(tx, old_name, body.name.strip())


@router.delete("/zones/{zone_id}")
async def delete_zone(
    request: Request,
    sess: FullSession,
    zone_id: str,
    x_csrf_token: str | None = Header(None, alias="X-CSRF-Token"),
):
    csrf_check(sess, x_csrf_token)
    db = request.app.state.db
    try:
        uid = uuid_mod.UUID(zone_id)
    except ValueError as e:
        raise HTTPException(400, "Invalid id") from e

    row = await db.fetchrow("SELECT name FROM zones WHERE id = $1", str(uid))
    if row is None:
        raise HTTPException(404, "Not found")
    zone_name = row["name"]

    if db.is_pg:
        await _delete_zone_pg(db, uid)
    else:
        await _delete_zone_sqlite(db, uid)

    actor = await get_actor(request, sess)
    ip = request.client.host if request.client else "unknown"
    await log_activity(
        db,
        actor=actor,
        action="zone_delete",
        entity_type="zone",
        entity_id=zone_id,
        entity_name=zone_name,
        ip_address=ip,
    )

    return {"ok": True}


async def _delete_zone_pg(db: Any, uid: uuid_mod.UUID) -> None:
    async with db.transaction() as conn:
        row = await conn.fetchrow("DELETE FROM zones WHERE id = $1::uuid RETURNING id, name", uid)
        if row is None:
            raise HTTPException(404, "Not found")
        await conn.execute(
            """
            UPDATE tokens t
            SET grants = COALESCE(
                (
                    SELECT jsonb_agg(elem)
                    FROM jsonb_array_elements(t.grants) AS elem
                    WHERE elem->>'area' <> $1
                ),
                '[]'::jsonb
            )
            WHERE EXISTS (
                SELECT 1
                FROM jsonb_array_elements(t.grants) AS elem
                WHERE elem->>'area' = $1
            )
            """,
            row["name"],
        )


async def _delete_zone_sqlite(db: Any, uid: uuid_mod.UUID) -> None:
    """SQLite implementation: the zone delete and the grant rewrite in one write transaction."""
    async with write_transaction(db) as tx:
        row = await tx.fetchrow("SELECT id, name FROM zones WHERE id = $1", str(uid))
        if row is None:
            raise HTTPException(404, "Not found")
        await tx.execute("DELETE FROM zones WHERE id = $1", str(uid))
        await _rewrite_grants_sqlite(tx, row["name"], None)

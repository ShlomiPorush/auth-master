import re
import uuid as uuid_mod
from typing import Any

from app.datetime_utils import fmt_datetime

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from app.admin_deps import FullSession, csrf_check
from app import zones
from app.db import UniqueViolationError
from app.grants import fold_area
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
    try:
        zid = await zones.create_zone(db, name, (body.description or "").strip())
    except zones.ZoneNameTaken as e:
        raise HTTPException(409, zones.ZONE_NAME_TAKEN) from e

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

    await _patch_zone(db, uid, body, parts, args, n)

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


async def _patch_zone(db: Any, uid: uuid_mod.UUID, body: ZonePatch, parts: list[str], args: list[Any], n: int) -> None:
    """The zone update and the rewrite of its grants in one transaction (both backends)."""
    async with zones.zone_change_transaction(db) as tx:
        lock = " FOR UPDATE" if tx.is_pg else ""
        row = await tx.fetchrow(f"SELECT name FROM zones WHERE id = $1::uuid{lock}", uid)
        if row is None:
            raise HTTPException(404, "Not found")
        old_name = row["name"]
        new_name = body.name.strip() if body.name is not None else None
        if new_name is not None and await zones.zone_name_taken(tx, new_name, exclude_id=uid):
            raise HTTPException(409, zones.ZONE_NAME_TAKEN)
        args.append(uid)
        q = f"UPDATE zones SET {', '.join(parts)} WHERE id = ${n}::uuid"
        try:
            await tx.execute(q, *args)
        except UniqueViolationError as e:
            raise HTTPException(409, zones.ZONE_NAME_TAKEN) from e

        # A change of case only needs no rewrite: /validate compares areas regardless of
        # case, and rewriting could turn two spellings into one name held twice.
        if new_name is not None and fold_area(new_name) != fold_area(old_name):
            try:
                await zones.rewrite_zone_grants(tx, uid, old_name, new_name)
            except zones.ZoneRenameConflict as e:
                raise HTTPException(
                    409,
                    "Cannot rename: these tokens hold this zone more than once (in different spellings): "
                    f"{', '.join(e.token_names)}. Edit them first.",
                ) from e


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

    await _delete_zone(db, uid)

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


async def _delete_zone(db: Any, uid: uuid_mod.UUID) -> None:
    """The zone delete and the removal of its grants in one transaction (both backends)."""
    async with zones.zone_change_transaction(db) as tx:
        row = await tx.fetchrow("SELECT id, name FROM zones WHERE id = $1::uuid", uid)
        if row is None:
            raise HTTPException(404, "Not found")
        await tx.execute("DELETE FROM zones WHERE id = $1::uuid", uid)
        await zones.rewrite_zone_grants(tx, uid, row["name"], None)

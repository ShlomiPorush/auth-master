import json
import re
import uuid as uuid_mod
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel
from redis.asyncio import Redis

from app.admin_deps import FullSession, csrf_check
from app.db import UniqueViolationError
from app.token_cache import cache_delete

router = APIRouter(prefix="/admin/api", tags=["admin-zones"])

_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,62}$")


class ZoneCreate(BaseModel):
    name: str
    description: str = ""


class ZonePatch(BaseModel):
    name: str | None = None
    description: str | None = None


def _iso_or_str(val: Any) -> str | None:
    if val is None:
        return None
    if isinstance(val, str):
        return val
    return val.isoformat()


@router.get("/zones")
async def list_zones(request: Request, _sess: FullSession):
    db = request.app.state.db
    rows = await db.fetch("SELECT id, name, description, created_at FROM zones ORDER BY name")
    return [
        {
            "id": str(r["id"]),
            "name": r["name"],
            "description": r["description"] or "",
            "createdAt": _iso_or_str(r["created_at"]),
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
    r: Redis = request.app.state.redis
    try:
        uid = uuid_mod.UUID(zone_id)
    except ValueError as e:
        raise HTTPException(400, "Invalid id") from e

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
        await _patch_zone_pg(db, r, uid, body, parts, args, n)
    else:
        await _patch_zone_sqlite(db, r, uid, body, parts, args, n)

    return {"ok": True}


async def _patch_zone_pg(db: Any, r: Redis, uid: uuid_mod.UUID, body: ZonePatch, parts: list[str], args: list[Any], n: int) -> None:
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
            updated = await conn.fetch(
                """
                UPDATE tokens t
                SET grants = (
                    SELECT jsonb_agg(
                        CASE WHEN elem->>'area' = $1
                             THEN jsonb_set(elem, '{area}', to_jsonb($2::text))
                             ELSE elem
                        END
                    )
                    FROM jsonb_array_elements(t.grants) AS elem
                )
                WHERE EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(t.grants) AS elem
                    WHERE elem->>'area' = $1
                )
                RETURNING token_hash
                """,
                old_name,
                new_name,
            )
            for u in updated:
                await cache_delete(r, u["token_hash"])


async def _patch_zone_sqlite(db: Any, r: Redis, uid: uuid_mod.UUID, body: ZonePatch, parts: list[str], args: list[Any], n: int) -> None:
    """SQLite implementation — update grants JSON in Python."""
    row = await db.fetchrow("SELECT name FROM zones WHERE id = $1", str(uid))
    if row is None:
        raise HTTPException(404, "Not found")
    old_name = row["name"]
    args.append(str(uid))
    q = f"UPDATE zones SET {', '.join(parts)} WHERE id = ${n}"
    try:
        await db.execute(q, *args)
    except UniqueViolationError as e:
        raise HTTPException(409, "Zone name already exists") from e

    if body.name is not None and body.name.strip() != old_name:
        new_name = body.name.strip()
        token_rows = await db.fetch("SELECT id, token_hash, grants FROM tokens")
        for tr in token_rows:
            grants = json.loads(tr["grants"]) if isinstance(tr["grants"], str) else tr["grants"]
            changed = False
            for g in grants:
                if g.get("area") == old_name:
                    g["area"] = new_name
                    changed = True
            if changed:
                await db.execute("UPDATE tokens SET grants = $1 WHERE id = $2", json.dumps(grants), tr["id"])
                await cache_delete(r, tr["token_hash"])


@router.delete("/zones/{zone_id}")
async def delete_zone(
    request: Request,
    sess: FullSession,
    zone_id: str,
    x_csrf_token: str | None = Header(None, alias="X-CSRF-Token"),
):
    csrf_check(sess, x_csrf_token)
    db = request.app.state.db
    r: Redis = request.app.state.redis
    try:
        uid = uuid_mod.UUID(zone_id)
    except ValueError as e:
        raise HTTPException(400, "Invalid id") from e

    if db.is_pg:
        await _delete_zone_pg(db, r, uid)
    else:
        await _delete_zone_sqlite(db, r, uid)

    return {"ok": True}


async def _delete_zone_pg(db: Any, r: Redis, uid: uuid_mod.UUID) -> None:
    async with db.transaction() as conn:
        row = await conn.fetchrow("DELETE FROM zones WHERE id = $1::uuid RETURNING id, name", uid)
        if row is None:
            raise HTTPException(404, "Not found")
        updated = await conn.fetch(
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
            RETURNING token_hash
            """,
            row["name"],
        )
    for u in updated:
        await cache_delete(r, u["token_hash"])


async def _delete_zone_sqlite(db: Any, r: Redis, uid: uuid_mod.UUID) -> None:
    row = await db.fetchrow("SELECT id, name FROM zones WHERE id = $1", str(uid))
    if row is None:
        raise HTTPException(404, "Not found")
    zone_name = row["name"]
    await db.execute("DELETE FROM zones WHERE id = $1", str(uid))

    # Remove zone from token grants in Python
    token_rows = await db.fetch("SELECT id, token_hash, grants FROM tokens")
    for tr in token_rows:
        grants = json.loads(tr["grants"]) if isinstance(tr["grants"], str) else tr["grants"]
        filtered = [g for g in grants if g.get("area") != zone_name]
        if len(filtered) != len(grants):
            await db.execute("UPDATE tokens SET grants = $1 WHERE id = $2", json.dumps(filtered), tr["id"])
            await cache_delete(r, tr["token_hash"])

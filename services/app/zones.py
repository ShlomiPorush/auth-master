from __future__ import annotations

import json
import logging
import uuid
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from app.config import get_settings
from app.db import UniqueViolationError, write_transaction
from app.grants import area_matches_zone, fold_area, grant_area

if TYPE_CHECKING:
    from app.db import Database

log = logging.getLogger(__name__)

ZONE_NAME_TAKEN = "A zone with this name already exists. Zone names are not case-sensitive."


class ZoneNameTaken(Exception):
    """Another zone has this name, or a name that differs from it only in case."""


class ZoneRenameConflict(Exception):
    """The rename would leave these tokens holding the zone more than once."""

    def __init__(self, token_names: list[str]):
        super().__init__(", ".join(token_names))
        self.token_names = token_names


async def allowed_area_names(db: "Database") -> list[str]:
    rows = await db.fetch("SELECT name FROM zones ORDER BY name")
    if rows:
        return [r["name"] for r in rows]
    return get_settings().allowed_areas_list


@asynccontextmanager
async def grant_write_transaction(db: "Database"):
    """The transaction for a token write that stores grants; no zone changes until it ends.

    Check the grants again with allowed_area_names() on the handle it yields, then write
    them on it: a zone delete or rename that commits before the check makes the check
    fail, and one that starts after it waits for this transaction and then rewrites the
    grants it wrote.  SQLite: write_transaction already excludes every other writer.
    PostgreSQL: SHARE mode conflicts with INSERT, UPDATE and DELETE on zones (also when
    the table is empty and ALLOWED_AREAS applies), not with reads or other grant writes.
    """
    async with write_transaction(db) as tx:
        if tx.is_pg:
            await tx.execute("LOCK TABLE zones IN SHARE MODE")
        yield tx


@asynccontextmanager
async def zone_change_transaction(db: "Database"):
    """The transaction for a zone create, rename or delete.

    Zone changes run one at a time, and never while a grant write is in progress:
    PostgreSQL SHARE ROW EXCLUSIVE conflicts with itself, with the SHARE lock of
    grant_write_transaction and with every INSERT, UPDATE and DELETE on zones, but not
    with plain reads.  It is the first statement, so the zone table is always locked
    before any token row.  SQLite: write_transaction already excludes every other writer.
    """
    async with write_transaction(db) as tx:
        if tx.is_pg:
            await tx.execute("LOCK TABLE zones IN SHARE ROW EXCLUSIVE MODE")
        yield tx


def _name_fold_sql(db: Any, column: str) -> str:
    # Zone names are ASCII (the name checks in the routers).  COLLATE "C" makes
    # PostgreSQL's lower() independent of the database locale (a Turkish locale lowers
    # "I" to a dotless i); SQLite's built-in lower() changes ASCII letters only.
    return f'lower({column} COLLATE "C")' if db.is_pg else f"lower({column})"


async def zone_name_taken(tx: Any, name: str, exclude_id: Any = None) -> bool:
    """True when another zone has *name*, or a name that differs from it only in case."""
    sql = f"SELECT 1 FROM zones WHERE {_name_fold_sql(tx, 'name')} = {_name_fold_sql(tx, '$1::text')}"
    args: list[Any] = [name]
    if exclude_id is not None:
        sql += " AND id <> $2::uuid"
        args.append(exclude_id)
    return await tx.fetchval(sql, *args) is not None


async def create_zone(db: "Database", name: str, description: str) -> str:
    """Insert a zone and return its id; ZoneNameTaken when the name is taken."""
    async with zone_change_transaction(db) as tx:
        if await zone_name_taken(tx, name):
            raise ZoneNameTaken(name)
        try:
            if tx.is_sqlite:
                zid: Any = str(uuid.uuid4())
                await tx.execute(
                    "INSERT INTO zones (id, name, description) VALUES ($1, $2, $3)", zid, name, description
                )
            else:
                zid = await tx.fetchval(
                    "INSERT INTO zones (name, description) VALUES ($1, $2) RETURNING id", name, description
                )
        except UniqueViolationError as e:
            raise ZoneNameTaken(name) from e
    return str(zid)


def _decode_grants(value: Any) -> list[Any] | None:
    """The stored grants column decoded as /validate decodes it; None when it is not a list."""
    try:
        if isinstance(value, (bytes, memoryview)):
            value = bytes(value).decode()
        if isinstance(value, str):
            value = json.loads(value)
    except ValueError:
        return None
    return value if isinstance(value, list) else None


async def rewrite_zone_grants(tx: Any, zone_id: Any, area: str, new_area: str | None) -> None:
    """Rename (*new_area*) or remove (None) the grants of the zone that was named *area*.

    Runs on the zone change's transaction (zone_change_transaction), after the zone's
    own UPDATE or DELETE, so no grant write can land between this read and its writes.
    A grant belongs to the zone by app.grants.area_matches_zone, with the fold /validate
    uses.  Elements that are not grants, and rows that are not a JSON list, are kept as
    they are; only rows that change are written.

    A rename that would leave a token holding the new name more than once is refused
    with ZoneRenameConflict before anything is written: /validate would read only the
    first of them, so a permission would stop working.
    """
    zid = str(zone_id)
    others = [r["name"] for r in await tx.fetch("SELECT id, name FROM zones") if str(r["id"]) != zid]
    new_key = fold_area(new_area) if new_area is not None else None
    updates: list[tuple[Any, list[Any]]] = []
    conflicts: list[str] = []
    for row in await tx.fetch("SELECT id, name, grants FROM tokens"):
        grants = _decode_grants(row["grants"])
        if grants is None:
            continue
        kept: list[Any] = []
        matched = 0
        same_as_new = 0
        for elem in grants:
            a = grant_area(elem)
            if a is not None and area_matches_zone(a, area, others):
                matched += 1
                if new_area is None:
                    continue
                elem = {**elem, "area": new_area}
            elif a is not None and new_key is not None and fold_area(a) == new_key:
                same_as_new += 1
            kept.append(elem)
        if not matched:
            continue
        if new_area is not None and matched + same_as_new > 1:
            conflicts.append(row["name"])
            continue
        updates.append((row["id"], kept))
    if conflicts:
        raise ZoneRenameConflict(sorted(conflicts))
    for token_id, kept in updates:
        await tx.execute("UPDATE tokens SET grants = $1::jsonb WHERE id = $2", json.dumps(kept), token_id)


async def duplicate_zone_groups(db: Any) -> list[list[str]]:
    """Zones whose names differ only in case, from before names had to be unique that way."""
    groups: dict[str, list[str]] = {}
    for r in await db.fetch("SELECT name FROM zones ORDER BY name"):
        groups.setdefault(fold_area(r["name"]), []).append(r["name"])
    return [g for g in groups.values() if len(g) > 1]


def _warn_duplicates(groups: list[list[str]]) -> None:
    for g in groups:
        log.warning(
            "Zones whose names differ only in case: %s. A token for one of them passes /validate "
            "for the others. Rename or delete all but one of each group.",
            ", ".join(g),
        )


async def ensure_zone_name_index(db: "Database") -> None:
    """Make zone names unique regardless of case in the database, when the data allows it.

    Zones that differ only in case may exist from earlier versions.  Then the index is
    not created and a warning names them; the service still starts, so an admin can
    rename or delete them, and the index is created at the first start after that.
    The checks in create and rename apply either way.
    """
    groups = await duplicate_zone_groups(db)
    if not groups:
        try:
            await db.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS idx_zones_name_fold ON zones ({_name_fold_sql(db, 'name')})"
            )
            return
        except UniqueViolationError:
            # A zone that differs only in case was added since the scan (an older version).
            groups = await duplicate_zone_groups(db)
    _warn_duplicates(groups)

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from app.config import get_settings
from app.db import write_transaction

if TYPE_CHECKING:
    from app.db import Database


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

"""The installation row: which installation of this service a database holds.

Every database the service uses has the table `installation` with exactly one row: a random
installation id, a status, the id of the move that created it (PostgreSQL databases filled
by a move from SQLite, set by that move) and `switch_nonce`, a random value written while a
connection change proves that a new connection reaches the same live database
(app.db_inspect.same_live_database).

data/database.json records the installation id of the database it names (app.db_config).
At every start the id in the database must match it; otherwise the service does not serve
that database (recovery mode), so a database that was replaced, emptied or swapped for
another installation is never used by mistake.

Status `active`: a normal database.  `pending`: a copy that a move filled but has not
switched to yet; it is never served as it is.
"""
from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from typing import Any

ACTIVE = "active"
PENDING = "pending"


@dataclass(frozen=True)
class Installation:
    id: str
    status: str
    migration_id: str | None


def new_id() -> str:
    return str(uuid.uuid4())


async def ensure_table(db: Any) -> None:
    """Create the table (not the row).  `singleton` keeps it to one row."""
    if db.is_pg:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS installation (
                singleton INTEGER NOT NULL DEFAULT 1 UNIQUE CHECK (singleton = 1),
                id TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('active', 'pending')),
                migration_id TEXT NULL,
                switch_nonce TEXT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
    else:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS installation (
                singleton INTEGER NOT NULL DEFAULT 1 UNIQUE CHECK (singleton = 1),
                id TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('active', 'pending')),
                migration_id TEXT NULL,
                switch_nonce TEXT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)


async def table_exists(db: Any) -> bool:
    if db.is_pg:
        return bool(await db.fetchval("SELECT to_regclass('installation') IS NOT NULL"))
    return await db.fetchval(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'installation'"
    ) is not None


async def read(db: Any) -> Installation | None:
    """The row, or None when the table or the row does not exist."""
    if not await table_exists(db):
        return None
    row = await db.fetchrow("SELECT id, status, migration_id FROM installation LIMIT 1")
    if row is None:
        return None
    return Installation(str(row["id"]), str(row["status"]), row["migration_id"])


async def create(db: Any, installation_id: str, status: str = ACTIVE, migration_id: str | None = None) -> None:
    """Create the table if needed and insert the row.  Run it in the same transaction as
    the schema it marks, where there is one."""
    await ensure_table(db)
    await db.execute(
        "INSERT INTO installation (id, status, migration_id) VALUES ($1, $2, $3)",
        installation_id, status, migration_id,
    )


async def ensure_row(db: Any) -> Installation:
    """The row, created `active` with a new id when missing (DATABASE_URL installs and
    adoption of an older installation).  ensure_table must have run."""
    from app.db import UniqueViolationError

    current = await read(db)
    if current is not None:
        return current
    try:
        await db.execute("INSERT INTO installation (id, status) VALUES ($1, $2)", new_id(), ACTIVE)
    except UniqueViolationError:
        pass  # another process created it first
    current = await read(db)
    assert current is not None
    return current


async def activate(db: Any, installation_id: str, migration_id: str) -> None:
    """The first write to a database a move from SQLite filled (app.db_move): its row
    becomes active.  Does nothing when it is active already."""
    await db.execute(
        "UPDATE installation SET status = $1 WHERE id = $2 AND migration_id = $3 AND status = $4",
        ACTIVE, installation_id, migration_id, PENDING,
    )


async def write_nonce(db: Any) -> str:
    value = secrets.token_hex(16)
    await db.execute("UPDATE installation SET switch_nonce = $1", value)
    return value


async def read_nonce(db: Any) -> str | None:
    return await db.fetchval("SELECT switch_nonce FROM installation LIMIT 1")


async def adopt_sqlite(db: Any, installation_id: str) -> None:
    """Mark an older SQLite installation as this service's: in one transaction, add the
    tables and columns of this version, then the row (unless one exists, after an
    interrupted adoption).  So an installation row never exists without the full schema."""
    from app.db import write_transaction
    from app.schema import ensure_database_schema

    async with write_transaction(db) as tx:
        await ensure_database_schema(tx)
        if await read(tx) is None:
            await create(tx, installation_id)

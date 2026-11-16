"""What a database holds, and whether the service can use it.

inspect() sorts a database into one of:
  empty         no relations in the target schema (PostgreSQL: the schema that
                current_schema() names; objects of extensions such as pgcrypto do not count)
  installation  an installation of this service (the `installation` table with its row)
  older         only this service's tables, without `installation`: an installation of a
                version before 2.3 or a backup of one; it can be adopted with confirmation
  incomplete    the installation row, but without some of the service's tables (a backup
                restored only in part): never used, so nothing is recreated empty
  foreign       anything else: other relations, or the installation table without a row

PostgreSQL checks for a connection the service will use (privilege_problems): the server is
not a standby and the session is not read-only, the user may SELECT, INSERT, UPDATE and
DELETE on every service table, is a member of each table's owner role (start-up runs ALTER
TABLE ... ADD COLUMN IF NOT EXISTS), row-level security does not apply to it, and it may
CREATE in the schema (start-up runs CREATE TABLE IF NOT EXISTS, which checks that before
skipping an existing table).  Nothing is granted or changed automatically.
"""
from __future__ import annotations

import asyncio
import weakref
from dataclasses import dataclass, field
from typing import Any

from app import installation as inst
from app.schema import INSTALLATION_TABLE, SERVICE_TABLES

EMPTY = "empty"
INSTALLATION = "installation"
OLDER = "older"
FOREIGN = "foreign"
# The installation row without tables every installation has: restored only in part.
INCOMPLETE = "incomplete"

# The tables an older installation has at least (every released version created them).
OLDER_REQUIRED = ("admin_users", "tokens")


@dataclass(frozen=True)
class TargetState:
    kind: str
    installation: inst.Installation | None = None
    has_admin: bool = False
    # For `foreign`: up to 5 relation names that are not this service's.
    others: tuple[str, ...] = field(default_factory=tuple)


async def relations(db: Any) -> list[str]:
    if db.is_pg:
        rows = await db.fetch(
            """
            SELECT c.relname FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema()
              AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
              AND NOT EXISTS (
                  SELECT 1 FROM pg_depend d
                  WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e'
              )
            ORDER BY c.relname
            """
        )
        return [r["relname"] for r in rows]
    rows = await db.fetch(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    return [r["name"] for r in rows]


async def has_enabled_admin(db: Any) -> bool:
    return await db.fetchval("SELECT 1 FROM admin_users WHERE totp_enabled = $1 LIMIT 1", True) is not None


async def inspect(db: Any) -> TargetState:
    if db.is_pg and await db.fetchval("SELECT current_schema()") is None:
        # search_path names no existing schema: nothing can be created or found.
        return TargetState(FOREIGN, others=("(no schema on search_path)",))
    names = await relations(db)
    if not names:
        return TargetState(EMPTY)
    known = set(SERVICE_TABLES)
    others = tuple(n for n in names if n not in known and n != INSTALLATION_TABLE)
    if others:
        return TargetState(FOREIGN, others=others[:5])
    if INSTALLATION_TABLE in names:
        row = await inst.read(db)
        if row is None:
            return TargetState(FOREIGN, others=("installation (no row)",))
        # Every table of this version's schema existed when the installation row was written
        # (setup and adoption create them in the same transaction), so a missing one was lost
        # in a restore.  A table that a later version adds goes into a separate list here.
        missing = tuple(t for t in SERVICE_TABLES if t not in names)
        if missing:
            return TargetState(INCOMPLETE, installation=row, others=missing)
        return TargetState(INSTALLATION, installation=row, has_admin=await has_enabled_admin(db))
    if all(t in names for t in OLDER_REQUIRED):
        return TargetState(OLDER, has_admin=await has_enabled_admin(db))
    return TargetState(FOREIGN, others=tuple(names[:5]))


async def privilege_problems(db: Any, *, tables_exist: bool) -> list[str]:
    """What the PostgreSQL user lacks, as short sentences ([] when nothing).  With
    tables_exist False (an empty database the service will fill) only the server, session
    and schema checks apply: the user will own what it creates."""
    if not db.is_pg:
        return []
    problems: list[str] = []
    if await db.fetchval("SELECT pg_is_in_recovery()"):
        problems.append("The server is a standby (read-only replica). Connect to the primary server.")
    if (await db.fetchval("SHOW transaction_read_only")) != "off":
        problems.append("The session is read-only (default_transaction_read_only). Use a user that can write.")
    schema = await db.fetchval("SELECT current_schema()")
    if schema is None:
        problems.append("The user's search_path names no existing schema.")
        return problems
    if not await db.fetchval("SELECT has_schema_privilege(current_schema(), 'CREATE')"):
        problems.append(f"The user cannot create tables in the schema {schema} (CREATE privilege).")
    if not tables_exist:
        return problems
    rows = await db.fetch(
        """
        SELECT t.name,
               c.oid IS NOT NULL AS present,
               COALESCE(has_table_privilege(c.oid, 'SELECT'), false) AS s,
               COALESCE(has_table_privilege(c.oid, 'INSERT'), false) AS i,
               COALESCE(has_table_privilege(c.oid, 'UPDATE'), false) AS u,
               COALESCE(has_table_privilege(c.oid, 'DELETE'), false) AS d,
               COALESCE(pg_has_role(c.relowner, 'USAGE'), false) AS owner,
               COALESCE(row_security_active(c.oid), false) AS rls
        FROM unnest($1::text[]) AS t(name)
        LEFT JOIN pg_class c ON c.relname = t.name
             AND c.relnamespace = (SELECT oid FROM pg_namespace WHERE nspname = current_schema())
        ORDER BY t.name
        """,
        [*SERVICE_TABLES, INSTALLATION_TABLE],
    )
    missing_dml = [r["name"] for r in rows if r["present"] and not (r["s"] and r["i"] and r["u"] and r["d"])]
    not_owner = [r["name"] for r in rows if r["present"] and not r["owner"]]
    rls = [r["name"] for r in rows if r["present"] and r["rls"]]
    if missing_dml:
        problems.append("The user lacks SELECT, INSERT, UPDATE or DELETE on: " + ", ".join(missing_dml) + ".")
    if not_owner:
        problems.append("The user is not a member of the owner role of: " + ", ".join(not_owner) + ".")
    if rls:
        problems.append("Row-level security applies to the user on: " + ", ".join(rls) + ".")
    return problems


# One proof at a time per event loop: two checks writing switch_nonce at once would read
# each other's value and see a copy where there is none.  (One service process per data
# directory, so this process is the only writer.)
_nonce_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()


async def same_live_database(current: Any, candidate: Any) -> bool:
    """Whether `candidate` reaches the same live database as `current`: a random value
    written through each connection must be read back through the other.  A restored
    copy or a replica of the database holds the same installation id but fails this."""
    loop = asyncio.get_running_loop()
    lock = _nonce_locks.get(loop)
    if lock is None:
        lock = _nonce_locks[loop] = asyncio.Lock()
    async with lock:
        first = await inst.write_nonce(current)
        if await inst.read_nonce(candidate) != first:
            return False
        second = await inst.write_nonce(candidate)
        return await inst.read_nonce(current) == second

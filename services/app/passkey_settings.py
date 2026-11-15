"""The passkey address saved on the admin Settings page.

Stored in the table passkey_settings, one row per saved version: a save inserts the next
version and nothing is updated or deleted.  The current address is the row with the
highest version (its origin may be NULL: the address was removed); an empty table is
version 0 with no address.  PASSKEY_ORIGIN, when set and not empty, wins over the table
and the page shows the field locked.

Every passkey outcome (sign-in, step-up, registration) runs in passkey_transaction() and
reads the address there, so an address change, which runs in address_change_transaction(),
never interleaves with one: on PostgreSQL the SHARE lock of an outcome conflicts with the
SHARE ROW EXCLUSIVE lock of a change (the pattern of app.zones); on SQLite
write_transaction already excludes every other writer.  The settings table is always
locked before any passkey row.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from app.config import get_settings
from app.db import Database, write_transaction
from app.passkey_origin import InvalidOrigin, canonical_origin, rp_id_of


@dataclass(frozen=True)
class Address:
    """The effective passkey address.  version is the stored version (0: none saved)."""

    origin: str | None
    version: int
    locked: bool
    actor: str | None = None
    created_at: Any = None

    @property
    def rp_id(self) -> str | None:
        return rp_id_of(self.origin) if self.origin else None


class InvalidStoredAddress(RuntimeError):
    pass


_SELECT_CURRENT = (
    "SELECT version, origin, actor, created_at FROM passkey_settings ORDER BY version DESC LIMIT 1"
)


async def read_current(db: Any) -> Address:
    """The effective address, read on `db` (a Database or a transaction handle)."""
    row = await db.fetchrow(_SELECT_CURRENT)
    version = int(row["version"]) if row else 0
    env = get_settings().passkey_origin_env
    if env is not None:
        return Address(env, version, True)
    if row is None or row["origin"] is None:
        return Address(None, version, False, row["actor"] if row else None, row["created_at"] if row else None)
    try:
        origin = canonical_origin(row["origin"])
    except InvalidOrigin as e:
        raise InvalidStoredAddress(
            f"Passkey address version {version} in the table passkey_settings is invalid: {e}"
        ) from e
    if origin != row["origin"]:
        raise InvalidStoredAddress(
            f"Passkey address version {version} in the table passkey_settings is not in its normal form."
        )
    return Address(origin, version, False, row["actor"], row["created_at"])


@asynccontextmanager
async def passkey_transaction(db: Database):
    """The transaction of a passkey outcome; no address change until it ends."""
    async with write_transaction(db) as tx:
        if tx.is_pg:
            await tx.execute("LOCK TABLE passkey_settings IN SHARE MODE")
        yield tx


@asynccontextmanager
async def address_change_transaction(db: Database):
    """The transaction of an address change; waits for every passkey outcome in progress."""
    async with write_transaction(db) as tx:
        if tx.is_pg:
            await tx.execute("LOCK TABLE passkey_settings IN SHARE ROW EXCLUSIVE MODE")
        yield tx


async def insert_version(tx: Any, version: int, origin: str | None, actor: str, ip_address: str | None) -> None:
    """Save `origin` as `version` on `tx`, an address_change_transaction.

    The lock of that transaction makes the version read before it the newest one, so the
    insert cannot meet an existing version; if it ever does, UniqueViolationError rolls
    the whole transaction back.
    """
    await tx.execute(
        "INSERT INTO passkey_settings (version, origin, actor, ip_address) VALUES ($1, $2, $3, $4)",
        version, origin, actor, ip_address,
    )

"""Summary of access_logs in 15-minute UTC slots, so the Usage page does not read every row.

access_log_rollup holds, per chunk of rows summarised, one row per (slot, token_id, area,
level, result, ip_address) with the number of calls, the time of the newest row, and the
newest non-empty token_name with the created_at and id of the row it came from. A group can
have several summary rows (one per chunk that saw it); readers add them up.

access_logs.rolled marks the rows that a summary row counts. A row is marked and counted in
the same transaction, so at every snapshot each row is either marked and in exactly one
summary row, or unmarked and in none. Readers use the summary only for whole slots and read
the unmarked rows of those slots from access_logs, so the figures do not depend on when a
row was written or committed, or on the clock.

The purge deletes rows, so a summary row of a slot it reaches may count deleted rows.
access_log_rollup_state.valid_from (a slot start, never moved back) marks where the summary
is exact: purge_access_logs moves it past the slot of every row it deletes, in the
transaction that deletes the row. Readers use no summary row of a slot before valid_from and
read those slots' rows from access_logs, marked or not.

Known limitation: valid_from is not moved back. When the retention is increased after a
purge, the rows still kept in the slots before valid_from are read from access_logs. This
is slow only when such a slot holds a very large number of rows, for example a purge that
stopped inside a slot with hundreds of thousands of calls. It lasts until those slots leave
the retention period. The figures stay exact.

Only rows from the first whole slot after the retention cutoff to the start of the current
slot are summarised. Writers are serialised: on SQLite by BEGIN IMMEDIATE
(write_transaction), on PostgreSQL by a transaction-level advisory lock taken first in every
transaction that writes the summary or its state.
"""
from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from app.db import Database, write_transaction

SLOT_SECONDS = 15 * 60
# Rows summarised per transaction: short enough that /validate log writes, which wait at
# most a few seconds for the SQLite write lock, are not held up.
CHUNK_ROWS = 5000
# Pause between chunks, so other writers get the lock.
CHUNK_PAUSE_SEC = 0.05
_pause = asyncio.sleep
# Summary rows per INSERT statement (11 parameters each, well under both databases' limits).
_INSERT_ROWS = 500
# pg_advisory_xact_lock key for every transaction that writes access_log_rollup.
_PG_LOCK_KEY = 0x75736167  # "usag"
_SQLITE_TS = "%Y-%m-%d %H:%M:%S"
_COLUMNS = "slot, token_id, area, level, result, ip_address, calls, last_at, token_name, name_at, name_id"


def floor_slot(t: datetime) -> datetime:
    """Start of the 15-minute UTC slot that holds t."""
    s = int(t.timestamp() // SLOT_SECONDS * SLOT_SECONDS)
    return datetime.fromtimestamp(s, timezone.utc)


def ceil_slot(t: datetime) -> datetime:
    """t when it starts a slot, otherwise the start of the next slot."""
    f = floor_slot(t)
    return f if f == t else f + timedelta(seconds=SLOT_SECONDS)


def bind_time(db: Database, t: datetime) -> Any:
    """A time as created_at stores it: UTC text on SQLite, a timestamp on PostgreSQL."""
    return t.astimezone(timezone.utc).strftime(_SQLITE_TS) if db.is_sqlite else t


def unrolled(db: Database) -> str:
    """The condition of the partial index idx_access_logs_unrolled, as each database needs it."""
    return "rolled = 0" if db.is_sqlite else "NOT rolled"


def _slot_of_created_at(db: Database) -> str:
    if db.is_sqlite:
        return f"datetime((CAST(strftime('%s', created_at) AS INTEGER) / {SLOT_SECONDS}) * {SLOT_SECONDS}, 'unixepoch')"
    return f"to_timestamp(FLOOR(EXTRACT(EPOCH FROM created_at) / {SLOT_SECONDS}) * {SLOT_SECONDS})"


async def _lock(db: Database, conn: Any) -> None:
    if not db.is_sqlite:
        await conn.execute("SELECT pg_advisory_xact_lock($1)", _PG_LOCK_KEY)


def _summarise(rows: Sequence[Any]) -> list[tuple[Any, ...]]:
    """One summary row per group of the given access log rows."""
    groups: dict[tuple[Any, ...], list[Any]] = {}
    for r in rows:
        key = (r["slot"], r["token_id"], r["area"], r["level"], bool(r["result"]), r["ip_address"])
        g = groups.get(key)
        if g is None:
            # calls, last_at, token_name, name_at, name_id
            g = groups[key] = [0, r["created_at"], None, None, None]
        g[0] += 1
        if r["created_at"] > g[1]:
            g[1] = r["created_at"]
        if r["token_name"] is not None:
            # The same order as the report: newest created_at, then the greatest id.
            mark = (r["created_at"], _id_order(r["id"]))
            if g[3] is None or mark > (g[3], _id_order(g[4])):
                g[2], g[3], g[4] = r["token_name"], r["created_at"], r["id"]
    return [key + tuple(g) for key, g in groups.items()]


def _id_order(v: Any) -> Any:
    # SQLite stores the id as text and orders it as text; PostgreSQL orders uuid by its bytes,
    # which is the order of the canonical text too. Text works for both.
    return str(v)


async def _roll_chunk(db: Database, start: Any, before: Any, limit: int) -> int:
    """Mark and summarise up to limit unmarked rows with start <= created_at < before."""
    async with write_transaction(db) as conn:
        await _lock(db, conn)
        if db.is_sqlite:
            pick = (
                "SELECT rowid FROM access_logs INDEXED BY idx_access_logs_unrolled "
                f"WHERE {unrolled(db)} AND created_at >= $1 AND created_at < $2 ORDER BY created_at DESC LIMIT $3"
            )
            rows = await conn.fetch(
                f"UPDATE access_logs SET rolled = 1 WHERE rowid IN ({pick}) "
                f"RETURNING id, token_id, token_name, area, level, result, ip_address, created_at, {_slot_of_created_at(db)} AS slot",
                start, before, limit,
            )
        else:
            rows = await conn.fetch(
                "UPDATE access_logs SET rolled = true WHERE id IN ("
                f"SELECT id FROM access_logs WHERE {unrolled(db)} AND created_at >= $1 AND created_at < $2 "
                "ORDER BY created_at DESC LIMIT $3) "
                f"RETURNING id, token_id, token_name, area, level, result, ip_address, created_at, {_slot_of_created_at(db)} AS slot",
                start, before, limit,
            )
        summary = _summarise(rows)
        for i in range(0, len(summary), _INSERT_ROWS):
            part = summary[i:i + _INSERT_ROWS]
            marks = ", ".join(
                "(" + ", ".join(f"${n * 11 + k + 1}" for k in range(11)) + ")" for n in range(len(part))
            )
            await conn.execute(
                f"INSERT INTO access_log_rollup ({_COLUMNS}) VALUES {marks}",
                *[v for row in part for v in row],
            )
        return len(rows)


async def roll_up(
    db: Database,
    retention_days: int,
    now: datetime | None = None,
    *,
    chunk_rows: int = CHUNK_ROWS,
    pause: float = CHUNK_PAUSE_SEC,
) -> int:
    """Summarise every unmarked row of the whole slots between the retention cutoff and now.

    Runs chunk after chunk until none is left, so it also summarises rows written before
    this version; newest rows first, so the shorter periods of the Usage page are fast
    soon after an upgrade. Returns the number of rows summarised. Safe to run concurrently with
    itself, the purge, reports and /validate writes; a cancelled run leaves whole chunks.
    """
    now = now or datetime.now(timezone.utc)
    start = bind_time(db, ceil_slot(now - timedelta(days=retention_days)))
    before = bind_time(db, floor_slot(now))
    total = 0
    while True:
        n = await _roll_chunk(db, start, before, chunk_rows)
        total += n
        if n < chunk_rows:
            return total
        await _pause(pause)


def _as_time(db: Database, v: Any) -> datetime:
    return datetime.strptime(v, _SQLITE_TS).replace(tzinfo=timezone.utc) if db.is_sqlite else v


async def purge_access_logs(
    db: Database, cutoff: datetime, *, chunk_rows: int = CHUNK_ROWS, pause: float = CHUNK_PAUSE_SEC
) -> None:
    """Delete the access_logs rows older than cutoff, and the summary rows no report reads.

    Rows go oldest first, at most chunk_rows per transaction. The transaction that deletes a
    row also moves valid_from past that row's slot, so no report reads a summary row that
    counts a deleted row, and a stop between transactions leaves valid_from where the
    deleted rows end. Then the summary rows before valid_from, which no report reads, go in
    transactions of at most chunk_rows rows.
    """
    if db.is_sqlite:
        rows_sql = (
            "DELETE FROM access_logs WHERE rowid IN (SELECT rowid FROM access_logs INDEXED BY idx_access_logs_created_at "
            "WHERE created_at < $1 ORDER BY created_at LIMIT $2) RETURNING created_at"
        )
        summary_sql = (
            "DELETE FROM access_log_rollup WHERE rowid IN (SELECT rowid FROM access_log_rollup "
            "WHERE slot < $1 LIMIT $2) RETURNING 1"
        )
    else:
        rows_sql = (
            "DELETE FROM access_logs WHERE id IN (SELECT id FROM access_logs WHERE created_at < $1 "
            "ORDER BY created_at LIMIT $2) RETURNING created_at"
        )
        summary_sql = (
            "DELETE FROM access_log_rollup WHERE ctid = ANY(ARRAY("
            "SELECT ctid FROM access_log_rollup WHERE slot < $1 LIMIT $2)) RETURNING 1"
        )
    while True:
        async with write_transaction(db) as conn:
            await _lock(db, conn)
            gone = await conn.fetch(rows_sql, bind_time(db, cutoff), chunk_rows)
            if gone:
                newest = max(_as_time(db, r["created_at"]) for r in gone)
                await conn.execute(
                    "UPDATE access_log_rollup_state SET valid_from = $1::timestamptz "
                    "WHERE id = 1 AND (valid_from IS NULL OR valid_from < $1::timestamptz)",
                    bind_time(db, floor_slot(newest) + timedelta(seconds=SLOT_SECONDS)),
                )
        if len(gone) < chunk_rows:
            break
        await _pause(pause)
    while True:
        async with write_transaction(db) as conn:
            await _lock(db, conn)
            v = await conn.fetchval("SELECT valid_from FROM access_log_rollup_state WHERE id = 1")
            n = 0 if v is None else len(await conn.fetch(summary_sql, v, chunk_rows))
        if n < chunk_rows:
            break
        await _pause(pause)


async def summary_valid_from(db: Database) -> datetime | None:
    """valid_from: no summary row of a slot before it may be read (None: every slot)."""
    v = await db.fetchval("SELECT valid_from FROM access_log_rollup_state WHERE id = 1")
    return None if v is None else _as_time(db, v)

"""
Unified async database abstraction for PostgreSQL (asyncpg) and SQLite (aiosqlite).

The backend is auto-detected from DATABASE_URL:
- ``sqlite:///path/to/db``  → aiosqlite
- ``postgres://...``        → asyncpg

All SQL should use PostgreSQL-style ``$1, $2`` placeholders.
When running on SQLite the driver transparently rewrites them to ``?``.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class UniqueViolationError(Exception):
    """Raised when an INSERT/UPDATE violates a UNIQUE constraint."""


# ── Row wrapper (dict-like access) ─────────────────────────────

class Row(dict):
    """Dict that also supports attribute & index access like asyncpg Record."""

    def __getattr__(self, key: str) -> Any:
        try:
            return self[key]
        except KeyError as e:
            raise AttributeError(key) from e

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


# ── SQL rewriting helpers ──────────────────────────────────────

_PG_PARAM = re.compile(r"\$(\d+)")
_UUID_CAST = re.compile(r"\$(\d+)::uuid")
_JSONB_CAST = re.compile(r"\$(\d+)::jsonb")


def _pg_to_sqlite_sql(sql: str) -> str:
    """Rewrite PG-dialect SQL to work on SQLite."""
    # Remove ::uuid and ::jsonb casts
    sql = _UUID_CAST.sub(r"$\1", sql)
    sql = _JSONB_CAST.sub(r"$\1", sql)
    # Remove other :: casts  e.g.  $2::text
    sql = re.sub(r"\$(\d+)::\w+", r"$\1", sql)
    # Replace $N with ?
    sql = _PG_PARAM.sub("?", sql)
    # Replace PG-specific functions
    sql = sql.replace("gen_random_uuid()", "?")  # we pass uuid from Python
    sql = sql.replace("now()", "datetime('now')")
    sql = sql.replace("TIMESTAMPTZ", "TEXT")
    sql = sql.replace("JSONB", "TEXT")
    sql = sql.replace("::jsonb", "")
    sql = sql.replace("'[]'::text", "'[]'")
    return sql


def _serialize_arg(v: Any) -> Any:
    """Serialize Python types to SQLite-storable values."""
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, bool):
        return int(v)
    return v


# ── Protocol ───────────────────────────────────────────────────

class Database:
    """Abstract base — see PgDatabase / SqliteDatabase."""

    is_pg: bool = False
    is_sqlite: bool = False

    async def execute(self, sql: str, *args: Any) -> None:
        raise NotImplementedError

    async def fetchrow(self, sql: str, *args: Any) -> Row | None:
        raise NotImplementedError

    async def fetchval(self, sql: str, *args: Any) -> Any:
        raise NotImplementedError

    async def fetch(self, sql: str, *args: Any) -> list[Row]:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


# ── PostgreSQL (asyncpg) implementation ────────────────────────

class PgDatabase(Database):
    is_pg = True

    def __init__(self, pool: Any):
        self._pool = pool

    async def execute(self, sql: str, *args: Any) -> None:
        import asyncpg as _asyncpg
        async with self._pool.acquire() as conn:
            try:
                await conn.execute(sql, *args)
            except _asyncpg.UniqueViolationError as e:
                raise UniqueViolationError(str(e)) from e

    async def fetchrow(self, sql: str, *args: Any) -> Row | None:
        async with self._pool.acquire() as conn:
            rec = await conn.fetchrow(sql, *args)
        if rec is None:
            return None
        return Row(dict(rec))

    async def fetchval(self, sql: str, *args: Any) -> Any:
        async with self._pool.acquire() as conn:
            return await conn.fetchval(sql, *args)

    async def fetch(self, sql: str, *args: Any) -> list[Row]:
        async with self._pool.acquire() as conn:
            recs = await conn.fetch(sql, *args)
        return [Row(dict(r)) for r in recs]

    async def close(self) -> None:
        await self._pool.close()

    @asynccontextmanager
    async def transaction(self):
        """Provide a connection-level transaction context."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                yield _PgConn(conn)


class _PgConn:
    """Thin wrapper around an asyncpg Connection inside a transaction."""

    def __init__(self, conn: Any):
        self._conn = conn

    async def execute(self, sql: str, *args: Any) -> None:
        await self._conn.execute(sql, *args)

    async def fetchrow(self, sql: str, *args: Any) -> Row | None:
        rec = await self._conn.fetchrow(sql, *args)
        return Row(dict(rec)) if rec else None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return await self._conn.fetchval(sql, *args)

    async def fetch(self, sql: str, *args: Any) -> list[Row]:
        recs = await self._conn.fetch(sql, *args)
        return [Row(dict(r)) for r in recs]


# ── SQLite (aiosqlite) implementation ──────────────────────────

class SqliteDatabase(Database):
    is_sqlite = True

    def __init__(self, conn: Any):
        self._conn = conn

    async def execute(self, sql: str, *args: Any) -> None:
        sql = _pg_to_sqlite_sql(sql)
        args = tuple(_serialize_arg(a) for a in args)
        try:
            await self._conn.execute(sql, args)
        except sqlite3.IntegrityError as e:
            if "UNIQUE" in str(e).upper():
                raise UniqueViolationError(str(e)) from e
            raise
        await self._conn.commit()

    async def fetchrow(self, sql: str, *args: Any) -> Row | None:
        sql = _pg_to_sqlite_sql(sql)
        args = tuple(_serialize_arg(a) for a in args)
        cursor = await self._conn.execute(sql, args)
        row = await cursor.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cursor.description]
        return Row(zip(cols, row))

    async def fetchval(self, sql: str, *args: Any) -> Any:
        row = await self.fetchrow(sql, *args)
        if row is None:
            return None
        return list(row.values())[0]

    async def fetch(self, sql: str, *args: Any) -> list[Row]:
        sql = _pg_to_sqlite_sql(sql)
        args = tuple(_serialize_arg(a) for a in args)
        cursor = await self._conn.execute(sql, args)
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [Row(zip(cols, r)) for r in rows]

    async def close(self) -> None:
        await self._conn.close()

    @asynccontextmanager
    async def transaction(self):
        """Provide a transaction context for SQLite."""
        # aiosqlite auto-commits; we use manual BEGIN/COMMIT
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield _SqliteConn(self._conn)
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise


class _SqliteConn:
    """Thin wrapper for SQLite inside a transaction."""

    def __init__(self, conn: Any):
        self._conn = conn

    async def execute(self, sql: str, *args: Any) -> None:
        sql = _pg_to_sqlite_sql(sql)
        args = tuple(_serialize_arg(a) for a in args)
        try:
            await self._conn.execute(sql, args)
        except sqlite3.IntegrityError as e:
            if "UNIQUE" in str(e).upper():
                raise UniqueViolationError(str(e)) from e
            raise

    async def fetchrow(self, sql: str, *args: Any) -> Row | None:
        sql = _pg_to_sqlite_sql(sql)
        args = tuple(_serialize_arg(a) for a in args)
        cursor = await self._conn.execute(sql, args)
        row = await cursor.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cursor.description]
        return Row(zip(cols, row))

    async def fetchval(self, sql: str, *args: Any) -> Any:
        row = await self.fetchrow(sql, *args)
        if row is None:
            return None
        return list(row.values())[0]

    async def fetch(self, sql: str, *args: Any) -> list[Row]:
        sql = _pg_to_sqlite_sql(sql)
        args = tuple(_serialize_arg(a) for a in args)
        cursor = await self._conn.execute(sql, args)
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [Row(zip(cols, r)) for r in rows]


# ── Factory ────────────────────────────────────────────────────

async def create_database(url: str) -> Database:
    """Create the right Database backend from a URL string."""
    if url.startswith("sqlite"):
        import aiosqlite  # type: ignore[import-untyped]

        # sqlite:///path or sqlite:////abs/path
        path = url.split("///", 1)[-1] if "///" in url else "auth.db"
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path)
        conn.row_factory = None  # we handle row mapping ourselves
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        return SqliteDatabase(conn)
    else:
        import asyncpg  # type: ignore[import-untyped]

        pool = await asyncpg.create_pool(url, min_size=1, max_size=10)
        return PgDatabase(pool)

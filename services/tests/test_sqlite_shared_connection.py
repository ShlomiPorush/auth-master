"""The shared SQLite request connection (app.db.SqliteDatabase): a write sent while a read
of another request is still open waits for the write lock, instead of failing at once with
"database is locked".

The read and the write are sent together (asyncio.gather), so aiosqlite queues the write
between the read's execute and its fetch, while another connection holds the write lock or
commits.  Requires pytest.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import aiosqlite
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def _database(path: Path):
    from app.db import create_database

    db = await create_database(f"sqlite:///{path}")
    await db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    for v in ("a", "b", "c"):
        await db.execute("INSERT INTO t (v) VALUES ($1)", v)
    return db


@pytest.mark.parametrize("read", ["fetch", "fetchrow"])
def test_a_write_waits_for_the_lock_while_another_read_is_open(tmp_path, read):
    async def go():
        db = await _database(tmp_path / "t.db")
        other = await aiosqlite.connect(tmp_path / "t.db", isolation_level=None)
        try:
            await other.execute("BEGIN IMMEDIATE")
            await other.execute("UPDATE t SET v = 'x' WHERE id = 3")

            async def release():
                await asyncio.sleep(0.3)
                await other.execute("COMMIT")

            releasing = asyncio.ensure_future(release())
            rows, _ = await asyncio.gather(
                getattr(db, read)("SELECT v FROM t ORDER BY id"),
                db.execute("UPDATE t SET v = 'y' WHERE id = 1"),
            )
            await releasing
            assert rows
            assert await db.fetchval("SELECT v FROM t WHERE id = 1") == "y"
        finally:
            await other.close()
            await db.close()

    asyncio.run(go())


def test_a_write_after_another_connection_committed_during_a_read(tmp_path):
    async def go():
        db = await _database(tmp_path / "t.db")
        other = await aiosqlite.connect(tmp_path / "t.db", isolation_level=None)
        try:
            for i in range(20):
                await asyncio.gather(
                    db.fetch("SELECT v FROM t ORDER BY id"),
                    other.execute("UPDATE t SET v = $1 WHERE id = 3".replace("$1", "?"), (f"o{i}",)),
                    db.execute("UPDATE t SET v = $1 WHERE id = 1", f"y{i}"),
                )
            assert await db.fetchval("SELECT v FROM t WHERE id = 1") == "y19"
        finally:
            await other.close()
            await db.close()

    asyncio.run(go())

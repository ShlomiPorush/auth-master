"""Regression tests for SQLite placeholder rewriting in app.db.

Bug: _pg_to_sqlite_sql replaced every ``$N`` with a positional ``?`` without
accounting for repeated parameters, so any query reusing a placeholder (e.g.
``LOWER(token_name) LIKE $2 OR LOWER(area) LIKE $2`` in the admin logs
endpoints) failed on SQLite with "Incorrect number of bindings supplied".

Runnable via pytest or directly: ``python services/tests/test_db_sqlite_params.py``
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import create_database


async def _make_db():
    tmp = tempfile.mkdtemp()
    db = await create_database(f"sqlite:///{tmp}/test.db")
    await db.execute(
        """CREATE TABLE access_logs (
            id TEXT PRIMARY KEY, token_id TEXT NULL, token_name TEXT NULL,
            area TEXT NOT NULL, level TEXT NOT NULL, result INTEGER NOT NULL,
            ip_address TEXT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.execute("CREATE TABLE tokens (id TEXT PRIMARY KEY, name TEXT NOT NULL)")
    await db.execute(
        "INSERT INTO access_logs (id, token_id, token_name, area, level, result, ip_address) "
        "VALUES ('L1', 'T1', 'payments-svc', 'billing', 'read', 0, '1.2.3.4')"
    )
    return db


def test_repeated_placeholder_in_where_clause():
    """The exact query shape of GET /admin/api/logs/access?search=...&result=..."""

    async def run():
        db = await _make_db()
        try:
            where = "WHERE result = $1 AND (LOWER(token_name) LIKE $2 OR LOWER(area) LIKE $2)"
            args = [0, "%pay%"]

            total = await db.fetchval(f"SELECT COUNT(*) FROM access_logs {where}", *args)
            assert total == 1

            rows = await db.fetch(
                f"""SELECT al.id, COALESCE(al.token_name, t.name) AS token_name, al.result
                    FROM access_logs al LEFT JOIN tokens t ON al.token_id = t.id
                    {where} ORDER BY al.created_at DESC LIMIT $3 OFFSET $4""",
                *args, 30, 0,
            )
            assert len(rows) == 1
            assert rows[0]["token_name"] == "payments-svc"
        finally:
            await db.close()

    asyncio.run(run())


def test_placeholder_reused_three_times():
    """The query shape of GET /admin/api/logs/activity?search=... ($N used 3x)."""

    async def run():
        db = await _make_db()
        try:
            rows = await db.fetch(
                "SELECT id FROM access_logs "
                "WHERE (LOWER(token_name) LIKE $1 OR LOWER(area) LIKE $1 OR LOWER(level) LIKE $1)",
                "%bill%",
            )
            assert len(rows) == 1
        finally:
            await db.close()

    asyncio.run(run())


def test_sequential_placeholders_still_work():
    async def run():
        db = await _make_db()
        try:
            row = await db.fetchrow(
                "SELECT id FROM access_logs WHERE area = $1 AND level = $2",
                "billing", "read",
            )
            assert row is not None and row["id"] == "L1"

            row = await db.fetchrow("SELECT COUNT(*) AS c FROM access_logs")
            assert row["c"] == 1
        finally:
            await db.close()

    asyncio.run(run())


if __name__ == "__main__":
    for fn in (
        test_repeated_placeholder_in_where_clause,
        test_placeholder_reused_three_times,
        test_sequential_placeholders_still_work,
    ):
        fn()
        print(f"PASS {fn.__name__}")

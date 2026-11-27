from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_URL = os.environ.get("TEST_DATABASE_URL", "")
SCHEMA_RUN_LIMIT_SEC = 10

pytestmark = pytest.mark.skipif(
    not _URL.startswith("postgres"), reason="needs TEST_DATABASE_URL on PostgreSQL"
)


def test_schema_run_on_current_database_does_not_wait_for_table_locks():
    asyncio.run(_run_schema_while_tables_are_vacuum_locked())


async def _run_schema_while_tables_are_vacuum_locked() -> None:
    import asyncpg

    from app.db import create_database
    from app.schema import SERVICE_TABLES, ensure_database_schema

    db = await create_database(_URL)
    holder = await asyncpg.connect(_URL)
    try:
        await ensure_database_schema(db)
        vacuum_like = holder.transaction()
        await vacuum_like.start()
        try:
            await holder.execute(f"LOCK TABLE {', '.join(SERVICE_TABLES)} IN SHARE UPDATE EXCLUSIVE MODE")
            await asyncio.wait_for(ensure_database_schema(db), SCHEMA_RUN_LIMIT_SEC)
        finally:
            await vacuum_like.rollback()
    finally:
        await holder.close()
        await db.close()

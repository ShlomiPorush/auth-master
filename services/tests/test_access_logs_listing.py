"""Regression tests for the admin access-log listing (GET /admin/api/logs/access).

Bug: the listing joined ``access_logs.token_id`` (TEXT) to ``tokens.id`` (UUID
on PostgreSQL) without a cast, so every request failed on PostgreSQL with
``operator does not exist: text = uuid``. SQLite stores both columns as TEXT
and was not affected.

The tests call the real route handler against a schema built by
``ensure_database_schema``, so they exercise the production query.

The SQLite test always runs. The PostgreSQL test runs only when
``AUTH_TEST_POSTGRES_URL`` is set, and is skipped otherwise. Point it at a new,
empty, throwaway PostgreSQL database for every run: the test creates the
schema and inserts rows, and it does not remove them. Generate that server's
password for each run with a cryptographically secure generator, for example
``openssl rand -hex 32``, and pass the URL only through the environment. Never
write the password or the URL to a file in the repository.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import create_database
from app.routers.admin_logs import get_access_logs
from app.schema import ensure_database_schema

PG_URL_ENV = "AUTH_TEST_POSTGRES_URL"


def _at(seconds: int) -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)


async def _seed(db) -> str:
    # PostgreSQL-dialect SQL; the SQLite driver strips the casts and stores
    # bool and datetime arguments as SQLite values.
    token_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO tokens (id, name, token_hash) VALUES ($1::uuid, $2, $3)",
        token_id, "payments-svc", "hash-" + token_id,
    )

    entries = [
        # (token_id, created_at); token_name stays NULL so the name comes from the join
        (token_id, _at(3)),  # live token
        (str(uuid.uuid4()), _at(2)),  # deleted token
        ("not-a-uuid", _at(1)),  # malformed id
        (None, _at(0)),  # unknown token
    ]
    for tid, created in entries:
        await db.execute(
            "INSERT INTO access_logs (id, token_id, area, level, result, ip_address, created_at) "
            "VALUES ($1::uuid, $2, $3, $4, $5, $6, $7)",
            str(uuid.uuid4()), tid, "billing", "read", True, "10.0.0.1", created,
        )
    return token_id


async def _list(db, **filters):
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=db)))
    params = {"page": 1, "limit": 50, "result": None, "search": None, "token_id": None}
    params.update(filters)
    return await get_access_logs(request, {}, **params)


async def _check_listing(db) -> None:
    await ensure_database_schema(db)
    token_id = await _seed(db)

    data = await _list(db)
    assert data["total"] == 4
    names = [log["tokenName"] for log in data["logs"]]
    assert names == ["payments-svc", "Unknown / Deleted", "Unknown / Deleted", "Unknown / Deleted"]
    assert data["logs"][0]["tokenId"] == token_id

    filtered = await _list(db, token_id=token_id, search="bill", result=True)
    assert filtered["total"] == 1
    assert [log["tokenName"] for log in filtered["logs"]] == ["payments-svc"]


def test_access_log_listing_sqlite():
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            db = await create_database(f"sqlite:///{tmp}/test.db")
            try:
                await _check_listing(db)
            finally:
                await db.close()

    asyncio.run(run())


@pytest.mark.skipif(not os.environ.get(PG_URL_ENV), reason=f"{PG_URL_ENV} is not set")
def test_access_log_listing_postgres():
    async def run():
        db = await create_database(os.environ[PG_URL_ENV])
        try:
            await _check_listing(db)
        finally:
            await db.close()

    asyncio.run(run())

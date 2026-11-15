"""The one-time copy of ALLOWED_AREAS into the zones table (app.zones.copy_allowed_areas_once).

Zones come only from the zones table.  On the first start of a database the areas older
versions used when the table was empty (ALLOWED_AREAS, or its default) are added, once:
not again after every zone was deleted, and not at all when zones exist.  Entries that
differ only in case are one zone, with the first spelling, as zone names are unique
regardless of case.  An interrupted copy leaves nothing behind and runs again; two starts
at once copy once.

Every secret is generated per run with ``secrets``.  PostgreSQL cases run when
TEST_DATABASE_URL is set (they delete the zones and app_state rows there).
Requires pytest.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import sys
import tempfile
from pathlib import Path

import pytest

_TMP_DIR = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
os.environ.setdefault("SESSION_SECRET", secrets.token_urlsafe(48))
os.environ.setdefault("APP_ENCRYPTION_KEY", secrets.token_hex(16))
os.environ.setdefault("REDIS_URL", f"redis://:{secrets.token_hex(32)}@127.0.0.1:6379/0")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP_DIR.name}/zones.db")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PG_URL = os.environ.get("TEST_DATABASE_URL")
BACKENDS = ["sqlite", pytest.param("postgres", marks=pytest.mark.skipif(not PG_URL, reason="TEST_DATABASE_URL is not set"))]


def use_allowed_areas(monkeypatch, value: str | None) -> None:
    from app import zones
    from app.config import Settings

    if value is None:
        monkeypatch.delenv("ALLOWED_AREAS", raising=False)
    else:
        monkeypatch.setenv("ALLOWED_AREAS", value)
    s = Settings(_env_file=None)
    monkeypatch.setattr(zones, "get_settings", lambda: s)


async def _open(url):
    from app.db import create_database
    from app.schema import ensure_database_schema

    db = await create_database(url)
    await ensure_database_schema(db)
    return db


async def _fresh(url):
    db = await _open(url)
    if db.is_pg:
        for table in ("zones", "app_state", "activity_logs"):
            await db.execute(f"DELETE FROM {table}")
    return db


def _url(backend, tmp_path):
    return f"sqlite:///{tmp_path}/zones.db" if backend == "sqlite" else PG_URL


async def _names(db):
    return [r["name"] for r in await db.fetch("SELECT name FROM zones ORDER BY name")]


@pytest.mark.parametrize("backend", BACKENDS)
def test_copies_once_with_duplicates_in_any_case_removed(backend, tmp_path, monkeypatch):
    from app.zones import allowed_area_names, copy_allowed_areas_once

    use_allowed_areas(monkeypatch, " Orders, billing,orders,,BILLING,ORDERS ")

    async def scenario():
        db = await _fresh(_url(backend, tmp_path))
        try:
            assert await copy_allowed_areas_once(db) == ["Orders", "billing"]
            assert await allowed_area_names(db) == sorted(["Orders", "billing"])
            rows = await db.fetch("SELECT actor, action, entity_name FROM activity_logs ORDER BY entity_name")
            assert sorted((r["actor"], r["action"], r["entity_name"]) for r in rows) == sorted(
                ("environment", "zone_create", n) for n in ("Orders", "billing")
            )
            # Done once: a later start changes nothing, also after every zone was deleted.
            assert await copy_allowed_areas_once(db) is None
            await db.execute("DELETE FROM zones")
            assert await copy_allowed_areas_once(db) is None
            assert await allowed_area_names(db) == []
        finally:
            await db.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("backend", BACKENDS)
def test_existing_zones_are_kept_as_they_are(backend, tmp_path, monkeypatch):
    from app.zones import copy_allowed_areas_once

    use_allowed_areas(monkeypatch, "orders,billing")

    async def scenario():
        db = await _fresh(_url(backend, tmp_path))
        try:
            await db.execute("INSERT INTO zones (id, name) VALUES ($1::uuid, $2)", "00000000-0000-0000-0000-00000000000a", "payments")
            assert await copy_allowed_areas_once(db) == []
            assert await _names(db) == ["payments"]
            assert await copy_allowed_areas_once(db) is None
        finally:
            await db.close()

    asyncio.run(scenario())


def test_default_areas_when_allowed_areas_is_unset(tmp_path, monkeypatch):
    from app.zones import copy_allowed_areas_once

    use_allowed_areas(monkeypatch, None)

    async def scenario():
        db = await _fresh(_url("sqlite", tmp_path))
        try:
            assert await copy_allowed_areas_once(db) == ["orders", "billing", "webhooks"]
            source = await db.fetchval("SELECT details FROM activity_logs LIMIT 1")
            assert '"default"' in source
        finally:
            await db.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("backend", BACKENDS)
def test_interrupted_copy_leaves_nothing_and_runs_again(backend, tmp_path, monkeypatch):
    import uuid

    from app import zones

    use_allowed_areas(monkeypatch, "orders,billing")
    real_uuid4 = uuid.uuid4
    calls = []

    def failing_second(*a):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("simulated stop in the middle of the copy")
        return real_uuid4()

    async def scenario():
        db = await _fresh(_url(backend, tmp_path))
        try:
            monkeypatch.setattr(zones.uuid, "uuid4", failing_second)
            with pytest.raises(RuntimeError, match="simulated"):
                await zones.copy_allowed_areas_once(db)
            assert await _names(db) == []
            assert await db.fetchval("SELECT COUNT(*) FROM app_state WHERE key = $1", zones.COPY_MARKER) == 0
            monkeypatch.setattr(zones.uuid, "uuid4", real_uuid4)
            assert await zones.copy_allowed_areas_once(db) == ["orders", "billing"]
        finally:
            await db.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("backend", BACKENDS)
def test_two_starts_at_once_copy_once(backend, tmp_path, monkeypatch):
    from app.zones import copy_allowed_areas_once

    use_allowed_areas(monkeypatch, "orders,billing")

    async def scenario():
        url = _url(backend, tmp_path)
        first = await _fresh(url)
        second = await _open(url)  # another process: its own connection
        try:
            results = await asyncio.gather(copy_allowed_areas_once(first), copy_allowed_areas_once(second))
            assert sorted(results, key=lambda r: r is None) == [["orders", "billing"], None]
            assert await _names(first) == ["billing", "orders"]
        finally:
            await second.close()
            await first.close()

    asyncio.run(scenario())


def test_empty_zones_table_has_no_fallback(tmp_path, monkeypatch):
    from app.zones import allowed_area_names

    use_allowed_areas(monkeypatch, "orders,billing")

    async def scenario():
        db = await _fresh(_url("sqlite", tmp_path))
        try:
            assert await allowed_area_names(db) == []
        finally:
            await db.close()

    asyncio.run(scenario())

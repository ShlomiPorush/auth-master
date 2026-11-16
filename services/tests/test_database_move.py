"""Moving a running installation from SQLite to PostgreSQL (app.db_move): the type map,
conversion, the bounded reads, the fence of the old file, the move itself (also under
load), its failure paths before and after the commit point, and how start-up, the
recovery retry, the recovery page and DATABASE_URL treat what a move leaves behind.

Uses the harness of test_database_setup (temporary data directory, the app with its full
lifespan, fake Redis; PostgreSQL cases need DBUI_TEST_ADMIN_URL, a superuser connection to
a throwaway server).  Every secret and password is generated per run with ``secrets``.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from test_database_setup import (  # noqa: F401  (the harness sets the environment first)
    ADMIN_URL,
    _edit,
    Pg,
    fresh_code,
    needs_pg,
    pg_database,
    run,
    service,
    setup_sqlite,
    wait_job,
    workdir,
)



def _dm():
    """app.db_move, imported when a test runs: importing the app while pytest collects would
    read the settings before other test modules set their environment."""
    from app import db_move

    return db_move


def _operation_error():
    from app.db_runtime import OperationError

    return OperationError


# ── Unit: map, conversion, bounded reads, fence ─────────────────

async def _build_sqlite(path: Path) -> None:
    from app.db import create_database
    from app.installation import create
    from app.schema import ensure_database_schema

    db = await create_database(f"sqlite:///{path}")
    await ensure_database_schema(db)
    await create(db, str(uuid.uuid4()))
    await db.close()


def _sqlite_schema(path: Path) -> None:
    run(_build_sqlite(path))


def test_the_map_names_every_sqlite_column(tmp_path):
    path = tmp_path / "auth.db"
    _sqlite_schema(path)
    conn = sqlite3.connect(path)
    try:
        for table, cols in _dm().MAP.items():
            have = {r[1] for r in conn.execute(f"PRAGMA table_xinfo({table})")}
            assert have == {c for c, _t in cols}, table
    finally:
        conn.close()


def _row(table: str, **values):
    cols = [c for c, _t in _dm().MAP[table]]
    return (1, *[values.get(c) for c in cols])


def test_conversion_of_each_type():
    counts = _dm().Counts()
    tid = str(uuid.uuid4())
    out = _dm()._convert("tokens", _row(
        "tokens", id=tid.encode(), name=b"n", token_hash=b"h", grants=b'[{"area":"a","level":"read"}]',
        expires_at=b"2026-01-02T03:04:05+02:00", is_active=1, created_at=b"2026-01-02 03:04:05", description=b"",
    ), counts)
    assert out[0] == uuid.UUID(tid)
    assert out[5] == datetime(2026, 1, 2, 1, 4, 5, tzinfo=timezone.utc)
    assert out[6] is True
    assert out[7] == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)  # naive = UTC
    assert out[4] == '[{"area":"a","level":"read"}]'


@pytest.mark.parametrize("table, values, column", [
    ("tokens", {"id": b"NOT-A-UUID"}, "id"),
    ("tokens", {"id": str(uuid.uuid4()).upper().encode()}, "id"),
    ("tokens", {"name": b"a\x00b"}, "name"),
    ("tokens", {"name": b"\xff\xfe"}, "name"),
    ("tokens", {"grants": b"[NaN]"}, "grants"),
    ("tokens", {"grants": b'["\\u0000"]'}, "grants"),
    ("tokens", {"grants": b'["\\ud800"]'}, "grants"),
    ("tokens", {"is_active": 2}, "is_active"),
    ("tokens", {"created_at": b"yesterday"}, "created_at"),
    ("access_log_rollup", {"calls": 2 ** 31}, "calls"),
])
def test_values_that_cannot_move_are_named(table, values, column):
    base = {
        "tokens": dict(id=str(uuid.uuid4()).encode(), name=b"n", token_hash=b"h", grants=b"[]", is_active=1,
                       created_at=b"2026-01-01 00:00:00", description=b""),
        "access_log_rollup": dict(slot=b"2026-01-01 00:00:00", area=b"a", level=b"read", result=1, calls=1,
                                  last_at=b"2026-01-01 00:00:00"),
    }[table]
    base.update(values)
    with pytest.raises(_operation_error()) as e:
        _dm()._convert(table, _row(table, **base), _dm().Counts())
    assert e.value.code == "row_invalid" and e.value.extra["column"] == column, e.value.text


def test_nul_and_invalid_text_in_logs_are_replaced_and_counted():
    counts = _dm().Counts()
    out = _dm()._convert("access_logs", _row(
        "access_logs", id=str(uuid.uuid4()).encode(), area=b"a\x00b", level=b"read", result=0,
        token_name=b"\xff", created_at=b"2026-01-01 00:00:00", rolled=0,
    ), counts)
    assert out[3] == "a�b" and out[2] == "�"
    assert counts.replaced == {"access_logs": 1}


def test_a_row_larger_than_the_limit_is_refused_by_sqlite_and_named(tmp_path, monkeypatch):
    path = tmp_path / "auth.db"
    _sqlite_schema(path)
    conn = sqlite3.connect(path)
    big_id = str(uuid.uuid4())
    for i in range(5):
        conn.execute("INSERT INTO zones (id, name) VALUES (?, ?)", (str(uuid.uuid4()), f"z{i}"))
    conn.execute("INSERT INTO zones (id, name, description) VALUES (?, ?, ?)", (big_id, "big", "x" * (2 * 1024 * 1024)))
    big_rowid = conn.execute("SELECT rowid FROM zones WHERE id = ?", (big_id,)).fetchone()[0]
    conn.commit()
    conn.close()

    src = _dm().Source(str(path))
    src._open()
    try:
        with pytest.raises(_operation_error()) as e:
            _dm()._scan(src, ("zones",))
        assert e.value.code == "row_too_large" and f"rowid {big_rowid}" in e.value.text
    finally:
        src.conn.close()


def test_an_oversized_first_row_is_named_too(tmp_path):
    path = tmp_path / "auth.db"
    _sqlite_schema(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO zones (id, name, description) VALUES (?, ?, ?)", (str(uuid.uuid4()), "big", "x" * (2 * 1024 * 1024)))
    rowid = conn.execute("SELECT rowid FROM zones").fetchone()[0]
    conn.commit()
    conn.close()
    src = _dm().Source(str(path))
    src._open()
    try:
        with pytest.raises(_operation_error()) as e:
            _dm()._scan(src, ("zones",))
        assert e.value.code == "row_too_large" and f"rowid {rowid}" in e.value.text
    finally:
        src.conn.close()


def test_rows_at_rowid_zero_and_below_are_read(tmp_path):
    path = tmp_path / "auth.db"
    _sqlite_schema(path)
    conn = sqlite3.connect(path)
    for rowid in (-7, 0, 5):
        conn.execute("INSERT INTO zones (rowid, id, name) VALUES (?, ?, ?)", (rowid, str(uuid.uuid4()), f"z{rowid}"))
    conn.commit()
    conn.close()
    src = _dm().Source(str(path))
    src._open()
    try:
        assert _dm()._scan(src, ("zones",)) == {"zones": 3}
        assert src.count("zones", None, src.max_rowid("zones")) == 3
    finally:
        src.conn.close()


def test_a_chunk_stays_within_its_byte_bound(tmp_path, monkeypatch):
    path = tmp_path / "auth.db"
    _sqlite_schema(path)
    conn = sqlite3.connect(path)
    row = "y" * (900 * 1024)
    for i in range(30):
        conn.execute("INSERT INTO zones (id, name, description) VALUES (?, ?, ?)", (str(uuid.uuid4()), f"z{i}", row))
    conn.commit()
    conn.close()
    src = _dm().Source(str(path))
    src._open()
    try:
        lo, total = 0, 0
        while True:
            rows = src.chunk("zones", lo, src.max_rowid("zones"))
            if not rows:
                break
            size = sum(len(v) for r in rows for v in r if isinstance(v, bytes))
            assert size <= _dm().CHUNK_BYTES + _dm().ROW_LIMIT_BYTES
            total += len(rows)
            lo = rows[-1][0]
        assert total == 30
    finally:
        src.conn.close()


def test_the_fence_refuses_writes_and_is_idempotent(tmp_path):
    path = tmp_path / "auth.db"
    _sqlite_schema(path)
    conn = sqlite3.connect(path)
    iid = conn.execute("SELECT id FROM installation").fetchone()[0]
    conn.close()
    mid = str(uuid.uuid4())

    assert _dm().fence_file(str(path), iid, mid) == "fenced"
    assert _dm().fence_file(str(path), iid, mid) == "already"
    assert _dm().fenced_by(str(path), mid, iid)
    conn = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="moved to PostgreSQL"):
            conn.execute("INSERT INTO zones (id, name) VALUES (?, ?)", (str(uuid.uuid4()), "late"))
        with pytest.raises(sqlite3.DatabaseError, match="moved to PostgreSQL"):
            conn.execute("UPDATE access_log_rollup_state SET valid_from = NULL")
    finally:
        conn.close()
    # Another move's id or another installation: refused, nothing changed.
    with pytest.raises(_dm().SourceMismatch):
        _dm().fence_file(str(path), iid, str(uuid.uuid4()))
    other = tmp_path / "other.db"
    _sqlite_schema(other)
    with pytest.raises(_dm().SourceMismatch):
        _dm().fence_file(str(other), iid, mid)
    conn = sqlite3.connect(other)
    try:
        assert conn.execute("SELECT status FROM installation").fetchone()[0] == "active"
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger'").fetchone()[0] == 0
    finally:
        conn.close()
    assert _dm().fence_file(str(tmp_path / "missing.db"), iid, mid) == "missing"


def _fence_data_auth_db(workdir: Path) -> tuple[str, str]:
    path = workdir / "data" / "auth.db"
    conn = sqlite3.connect(path)
    iid = conn.execute("SELECT id FROM installation").fetchone()[0]
    conn.close()
    mid = str(uuid.uuid4())
    _dm().fence_file(str(path), iid, mid)
    return iid, mid


def test_a_moved_sqlite_file_is_never_served_again(workdir, monkeypatch):
    async def go():
        async with service(monkeypatch) as svc:
            await setup_sqlite(svc)
        iid, _mid = _fence_data_auth_db(workdir)
        async with service(monkeypatch) as svc:
            assert svc.runtime.mode == "recovery" and svc.runtime.failure_code == "database_moved"
            assert svc.runtime.known_installation_id == iid
        # Without data/database.json (lost, or an older backup of the data directory without
        # it): the legacy adoption does not serve it either.
        (workdir / "data" / "database.json").unlink()
        async with service(monkeypatch) as svc:
            assert svc.runtime.mode == "recovery" and svc.runtime.failure_code == "database_moved"
            assert svc.runtime.known_installation_id == iid
            assert not (workdir / "data" / "database.json").exists()

    run(go())


def test_database_url_refuses_a_moved_sqlite_file(workdir, monkeypatch):
    from app.config import get_settings

    async def go():
        async with service(monkeypatch) as svc:
            await setup_sqlite(svc)
        _fence_data_auth_db(workdir)
        monkeypatch.setattr(get_settings(), "database_url", "sqlite:///data/auth.db")
        with pytest.raises(RuntimeError, match="moved to PostgreSQL"):
            async with service(monkeypatch):
                pass

    run(go())


# ── PostgreSQL: the move ────────────────────────────────────────

async def _seed(svc, c) -> dict:
    """Data of each kind in the SQLite database, through the API and the log writers."""
    from app.logger import log_access, log_activity

    db = svc.app.state.db
    zones = []
    for name in ("alpha", "beta"):
        res = await c.post("/admin/api/zones", json={"name": name, "description": f"{name} zone"})
        assert res.status_code == 201, res.text
        zones.append(res.json())
    tokens = []
    for i in range(3):
        res = await c.post("/admin/api/tokens", json={
            "name": f"t{i}", "grants": [{"area": "alpha", "level": "read"}], "description": f"d{i}",
            "token": f"tok-{secrets.token_hex(8)}",
        })
        assert res.status_code == 201, res.text
        tokens.append(res.json())
    for i in range(50):
        await log_access(db, tokens[0]["id"], "t0", "alpha", "read", True, "10.0.0.1")
    await log_access(db, None, None, "nul\x00area", "read", False, "10.0.0.2")
    # A row an older version wrote with NUL (the writers replace it now).
    await db.execute(
        "INSERT INTO access_logs (id, area, level, result, ip_address) VALUES ($1, $2, $3, $4, $5)",
        str(uuid.uuid4()), "old\x00nul", "read", 0, "10.0.0.3",
    )
    await log_activity(db, "admin", "test.seed", "settings", None, "seed", {"k": "v"}, "10.0.0.1")
    return {"zones": zones, "tokens": tokens}


async def _signed_in_sqlite(svc):
    await setup_sqlite(svc)
    admin_id, totp = await svc.add_admin()
    return admin_id, totp, await svc.signed_in(admin_id)


async def _move(c, pg: Pg, totp: str, **extra) -> dict:
    """Start the move and wait for its job.  Read from the runtime, not the job route: a
    move that ends in recovery mode leaves the Settings routes answering 503."""
    res = await c.post("/admin/api/settings/database/move", json={"postgres": pg.body(), "code": fresh_code(totp), **extra})
    assert res.status_code == 202, res.text
    rt = c._transport.app.state.runtime
    job = rt.jobs.get(res.json()["job"]["id"])
    deadline = asyncio.get_running_loop().time() + 120
    while job.state == "running":
        assert asyncio.get_running_loop().time() < deadline, job.view()
        await asyncio.sleep(0.05)
    return job.view()


def _sqlite_counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in _dm().MAP}
    finally:
        conn.close()


async def _pg_counts(pg: Pg) -> dict[str, int]:
    conn = await pg.admin(pg.name)
    try:
        return {t: await conn.fetchval(f"SELECT COUNT(*) FROM {t}") for t in _dm().MAP}
    finally:
        await conn.close()


@needs_pg
def test_the_move_copies_everything_and_switches(workdir, monkeypatch, capsys):
    async def go():
        async with pg_database() as pg:
            async with service(monkeypatch) as svc:
                admin_id, totp, c = await _signed_in_sqlite(svc)
                seeded = await _seed(svc, c)
                view = (await c.get("/admin/api/settings/database")).json()
                assert view["movable"] and not view["editable"]

                res = await c.post("/admin/api/settings/database/move/check", json={"postgres": pg.body()})
                check = res.json()
                assert check["ok"] and check["target"] == "empty" and check["problems"] == [], check
                assert check["counts"]["tokens"] == 3 and check["counts"]["access_logs"] == 52

                for rowid in (-3, 0):
                    await svc.app.state.db.execute(
                        "INSERT INTO zones (rowid, id, name) VALUES ($1, $2, $3)", rowid, str(uuid.uuid4()), f"low{rowid}")
                before = _sqlite_counts(workdir / "data" / "auth.db")
                job = await _move(c, pg, totp)
                assert job["state"] == "done", job
                result = job["result"]
                assert result["action"] == "database.migrated"
                assert result["textReplaced"] == {"access_logs": 1}
                assert svc.app.state.db.is_pg and svc.runtime.mode == "ready"

                cfg = svc.config()
                assert cfg["backend"] == "postgres" and cfg["migrationId"]
                # Rows below rowid 1 were copied too (seeded below the API rows).
                assert await svc.app.state.db.fetchval("SELECT COUNT(*) FROM zones WHERE name LIKE 'low%'") == 2
                assert Path(cfg["movedFromSqlite"]).resolve() == (workdir / "data" / "auth.db").resolve()
                assert pg.password not in svc.config_text()

                after = await _pg_counts(pg)
                # The move's own activity row is the one extra row.
                assert after == {**before, "activity_logs": before["activity_logs"] + 1}, (before, after)
                db = svc.app.state.db
                assert await db.fetchval("SELECT status FROM installation") == "active"
                assert await db.fetchval("SELECT area FROM access_logs WHERE ip_address = '10.0.0.3'") == "old�nul"
                details = json.loads(await db.fetchval("SELECT details FROM activity_logs WHERE action = 'database.migrated'"))
                assert details["rows"]["tokens"] == 3 and pg.password not in json.dumps(details)

                # The same session works, and the copied data serves.
                assert (await c.get("/admin/api/settings/database")).status_code == 200
                tok = seeded["tokens"][0]
                res = await c.post(f"/admin/api/tokens/{tok['id']}/reveal", json={"code": fresh_code(totp)})
                assert res.status_code == 200 and res.json()["token"] == tok["token"], res.text
                res = await svc.client().post("/validate", json={"token": tok["token"], "area": "alpha", "level": "read"})
                assert res.json()["result"] is True
                res = await svc.client().post("/validate", json={"token": tok["token"], "area": "x\x00", "level": "read"})
                assert res.status_code == 200
                assert svc.runtime.tasks, "background work runs on PostgreSQL"

            # The old file is fenced: its row names the move and every write is refused.
            conn = sqlite3.connect(workdir / "data" / "auth.db")
            try:
                assert conn.execute("SELECT status, migration_id FROM installation").fetchone() == ("pending", cfg["migrationId"])
                with pytest.raises(sqlite3.DatabaseError, match="moved to PostgreSQL"):
                    conn.execute("DELETE FROM tokens")
            finally:
                conn.close()
            async with service(monkeypatch) as svc:
                assert svc.runtime.mode == "ready" and svc.app.state.db.is_pg
        assert pg.password not in capsys.readouterr().out

    run(go())


@needs_pg
def test_the_move_under_load_loses_no_acknowledged_write(workdir, monkeypatch):
    """Requests and admin writes run through the whole move; every one succeeds and every
    acknowledged write is in PostgreSQL."""
    from app.routers import validate as validate_route

    # The route holds its own reference to the limiter.
    monkeypatch.setattr(validate_route, "allow_validate", lambda ip: True)

    async def go():
        async with pg_database() as pg:
            async with service(monkeypatch) as svc:
                admin_id, totp, c = await _signed_in_sqlite(svc)
                seeded = await _seed(svc, c)
                token = seeded["tokens"][0]["token"]
                statuses: list[int] = []
                unexpected: list[tuple] = []

                def note(path, res):
                    statuses.append(res.status_code)
                    if res.status_code not in (200, 201, 204):
                        unexpected.append((path, res.status_code, res.text[:200], svc.runtime.mode))
                live: dict[str, str] = {}  # token id -> last acknowledged description
                deleted: set[str] = set()
                validated = 0
                stop = asyncio.Event()

                async def admin_writes():
                    w = await svc.signed_in(admin_id)
                    i = 0
                    while not stop.is_set():
                        i += 1
                        res = await w.post("/admin/api/tokens", json={
                            "name": f"load{i}", "grants": [{"area": "alpha", "level": "read"}], "description": "new",
                        })
                        note("create", res)
                        if res.status_code != 201:
                            continue
                        tid = res.json()["id"]
                        live[tid] = "new"
                        res = await w.patch(f"/admin/api/tokens/{tid}", json={"description": f"edited{i}"})
                        note("patch", res)
                        if res.status_code == 200:
                            live[tid] = f"edited{i}"
                        if i % 3 == 0:
                            res = await w.delete(f"/admin/api/tokens/{tid}")
                            note("delete", res)
                            if res.status_code in (200, 204):
                                live.pop(tid)
                                deleted.add(tid)

                async def validations():
                    nonlocal validated
                    v = svc.client()
                    while not stop.is_set():
                        res = await v.post("/validate", json={"token": token, "area": "alpha", "level": "read"})
                        note("validate", res)
                        if res.status_code == 200:
                            validated += 1

                async def health():
                    h = svc.client()
                    while not stop.is_set():
                        note("health", await h.get("/health"))
                        await asyncio.sleep(0)

                logs_before = _sqlite_counts(workdir / "data" / "auth.db")["access_logs"]
                workers = [asyncio.create_task(f()) for f in (admin_writes, validations, health)]
                try:
                    await asyncio.sleep(0.3)
                    job = await _move(c, pg, totp)
                    await asyncio.sleep(0.3)
                finally:
                    stop.set()
                    await asyncio.gather(*workers)
                assert job["state"] == "done", job
                assert not unexpected, unexpected[:5]
                assert validated > 10 and len(live) + len(deleted) > 3
                db = svc.app.state.db
                assert db.is_pg
                for tid, description in live.items():
                    assert await db.fetchval("SELECT description FROM tokens WHERE id = $1::uuid", tid) == description
                for tid in deleted:
                    assert await db.fetchval("SELECT 1 FROM tokens WHERE id = $1::uuid", tid) is None
                assert await db.fetchval("SELECT COUNT(*) FROM access_logs") == logs_before + validated
                print(f"move under load: pause {job['result']['pauseMs']} ms, final step {job['result']['finalStepMs']} ms")

    run(go())


@needs_pg
def test_a_target_that_is_not_empty_or_not_utf8_is_refused(workdir, monkeypatch):
    async def go():
        async with pg_database() as pg:
            async with service(monkeypatch) as svc:
                _admin, totp, c = await _signed_in_sqlite(svc)
                await pg.sql(f"CREATE TABLE other_app (id int); ALTER TABLE other_app OWNER TO {pg.user}")
                res = (await c.post("/admin/api/settings/database/move/check", json={"postgres": pg.body()})).json()
                assert not res["ok"] and res["code"] == "target_not_empty", res
                job = await _move(c, pg, totp)
                assert job["state"] == "failed" and job["code"] == "target_not_empty"
                assert svc.runtime.mode == "ready" and svc.app.state.db.is_sqlite and svc.runtime.tasks

                latin = "dbui_" + secrets.token_hex(6)
                conn = await pg.admin()
                try:
                    await conn.execute(f"CREATE DATABASE {latin} OWNER {pg.user} ENCODING 'LATIN1' "
                                       "LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0")
                finally:
                    await conn.close()
                pg.extra_dbs.append(latin)
                await pg.sql(f"ALTER SCHEMA public OWNER TO {pg.user}", latin)
                res = (await c.post("/admin/api/settings/database/move/check",
                                    json={"postgres": pg.body(database=latin)})).json()
                assert not res["ok"] and res["code"] == "target_encoding", res

    run(go())


@needs_pg
def test_a_row_that_cannot_move_is_named_before_anything_changes(workdir, monkeypatch):
    async def go():
        async with pg_database() as pg:
            async with service(monkeypatch) as svc:
                _admin, totp, c = await _signed_in_sqlite(svc)
                bad = str(uuid.uuid4())
                await svc.app.state.db.execute("INSERT INTO zones (id, name) VALUES ($1, $2)", bad, "bad\x00zone")
                res = (await c.post("/admin/api/settings/database/move/check", json={"postgres": pg.body()})).json()
                assert not res["ok"] and res["code"] == "row_invalid" and bad in res["detail"], res
                job = await _move(c, pg, totp)
                assert job["state"] == "failed" and job["code"] == "row_invalid", job
                assert svc.app.state.db.is_sqlite and svc.runtime.tasks
                assert (await _pg_counts_or_empty(pg)) == {}
                assert await svc.app.state.db.fetchval(
                    "SELECT COUNT(*) FROM activity_logs WHERE action = 'database.migration_failed'") == 1

    run(go())


async def _pg_counts_or_empty(pg: Pg) -> dict:
    conn = await pg.admin(pg.name)
    try:
        names = await conn.fetch("SELECT relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                                 "WHERE n.nspname = 'public' AND c.relkind = 'r'")
        return {r["relname"]: True for r in names}
    finally:
        await conn.close()


@needs_pg
def test_the_move_needs_the_second_factor(workdir, monkeypatch):
    async def go():
        async with pg_database() as pg:
            async with service(monkeypatch) as svc:
                _admin, _totp, c = await _signed_in_sqlite(svc)
                res = await c.post("/admin/api/settings/database/move", json={"postgres": pg.body()})
                assert res.status_code == 400, res.text
                assert svc.app.state.db.is_sqlite

    run(go())


@needs_pg
def test_too_large_for_a_live_move_changes_nothing(workdir, monkeypatch):
    monkeypatch.setattr(_dm(), "FINAL_STEP_LIMIT_SEC", 0.000001)

    async def go():
        async with pg_database() as pg:
            async with service(monkeypatch) as svc:
                _admin, totp, c = await _signed_in_sqlite(svc)
                await _seed(svc, c)
                job = await _move(c, pg, totp)
                assert job["state"] == "failed" and job["code"] == "too_large_for_live_move", job
                assert svc.app.state.db.is_sqlite and svc.runtime.mode == "ready" and svc.runtime.tasks
                assert (await _pg_counts_or_empty(pg)) == {}
                assert svc.config()["backend"] == "sqlite"

    run(go())


@needs_pg
def test_a_fence_that_certainly_failed_stays_on_sqlite_and_a_new_move_replaces_the_copy(workdir, monkeypatch):
    async def go():
        async with pg_database() as pg:
            async with service(monkeypatch) as svc:
                _admin, totp, c = await _signed_in_sqlite(svc)
                await _seed(svc, c)
                real = _dm().fence_file

                def refuse(*_a):
                    raise _dm().SourceMismatch()

                monkeypatch.setattr(_dm(), "fence_file", refuse)
                job = await _move(c, pg, totp)
                assert job["state"] == "failed" and job["code"] == "move_source_mismatch", job
                assert svc.app.state.db.is_sqlite and svc.runtime.mode == "ready" and svc.runtime.tasks
                assert svc.config()["backend"] == "sqlite"
                # The committed copy is pending and is never served.
                conn = await pg.admin(pg.name)
                try:
                    assert await conn.fetchval("SELECT status FROM installation") == "pending"
                finally:
                    await conn.close()
                res = (await c.post("/admin/api/settings/database/move/check", json={"postgres": pg.body()})).json()
                assert res["ok"] and res["target"] == "earlier_copy", res

                monkeypatch.setattr(_dm(), "fence_file", real)
                await svc.app.state.db.execute("INSERT INTO zones (id, name) VALUES ($1, $2)", str(uuid.uuid4()), "late")
                job = await _move(c, pg, totp)
                assert job["state"] == "done", job
                assert await svc.app.state.db.fetchval("SELECT COUNT(*) FROM zones WHERE name = 'late'") == 1

    run(go())


@needs_pg
@pytest.mark.parametrize("failure", ["fence_unknown", "publish", "publish_uncertain", "activate"])
def test_a_failure_after_the_commit_point_is_completed_by_the_retry(workdir, monkeypatch, failure):
    """Fence outcome unknown, data/database.json not written, or activation failed: recovery
    mode (never back to SQLite), and the retry finishes the move once the cause is gone."""
    from app import db_config, db_runtime, installation

    monkeypatch.setattr(db_runtime, "RECOVERY_RETRY_SEC", 0.2)

    async def go():
        async with pg_database() as pg:
            async with service(monkeypatch) as svc:
                _admin, totp, c = await _signed_in_sqlite(svc)
                await _seed(svc, c)
                broken = {"on": True}
                if failure == "fence_unknown":
                    real = _dm().fence_file

                    def fence(*a):
                        result = real(*a)
                        if broken["on"]:
                            raise OSError("disk")
                        return result

                    def state(*_a):
                        raise OSError("disk")

                    monkeypatch.setattr(_dm(), "fence_file", fence)
                    monkeypatch.setattr(_dm(), "fence_state", state)
                elif failure in ("publish", "publish_uncertain"):
                    real_publish = db_config.publish_config

                    def publish(cfg):
                        if broken["on"] and cfg.backend == "postgres":
                            if failure == "publish":
                                raise OSError("disk full")
                            real_publish(cfg)
                            raise db_config.PublishUncertain()
                        return real_publish(cfg)

                    monkeypatch.setattr(db_config, "publish_config", publish)
                else:
                    real_activate = installation.activate

                    async def activate(*a):
                        if broken["on"]:
                            raise OSError("server")
                        return await real_activate(*a)

                    monkeypatch.setattr(installation, "activate", activate)

                job = await _move(c, pg, totp)
                assert job["state"] == "failed", job
                assert svc.runtime.mode == "recovery", svc.runtime.failure_code
                assert svc.runtime.failure_code in ("move_incomplete", "attach_failed")
                # Never back to SQLite: the old file is fenced.
                conn = sqlite3.connect(workdir / "data" / "auth.db")
                try:
                    with pytest.raises(sqlite3.DatabaseError, match="moved to PostgreSQL"):
                        conn.execute("DELETE FROM zones")
                finally:
                    conn.close()
                assert (await svc.client().get("/health")).status_code == 503

                broken["on"] = False
                for _ in range(100):
                    if svc.runtime.mode == "ready":
                        break
                    await asyncio.sleep(0.1)
                assert svc.runtime.mode == "ready" and svc.app.state.db.is_pg, svc.runtime.failure_code
                assert svc.config()["backend"] == "postgres" and svc.config()["migrationId"]
                assert await svc.app.state.db.fetchval("SELECT status FROM installation") == "active"
                assert await svc.app.state.db.fetchval("SELECT COUNT(*) FROM tokens") == 3

    run(go())


@needs_pg
def test_start_up_completes_a_move_whose_file_names_it(workdir, monkeypatch):
    """A crash after data/database.json was written and before activation: the next start
    fences the old file (again) and activates the copy."""
    async def go():
        async with pg_database() as pg:
            async with service(monkeypatch) as svc:
                _admin, totp, c = await _signed_in_sqlite(svc)
                await _seed(svc, c)
                job = await _move(c, pg, totp)
                assert job["state"] == "done", job
            # The state such a crash leaves: the copy pending, the old file not fenced yet.
            await pg.sql("UPDATE installation SET status = 'pending'")
            conn = sqlite3.connect(workdir / "data" / "auth.db")
            for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall():
                conn.execute(f"DROP TRIGGER {name}")
            conn.execute("UPDATE installation SET status = 'active', migration_id = NULL")
            conn.commit()
            conn.close()
            async with service(monkeypatch) as svc:
                assert svc.runtime.mode == "ready" and svc.app.state.db.is_pg, svc.runtime.failure_code
                assert await svc.app.state.db.fetchval("SELECT status FROM installation") == "active"
            conn = sqlite3.connect(workdir / "data" / "auth.db")
            try:
                with pytest.raises(sqlite3.DatabaseError, match="moved to PostgreSQL"):
                    conn.execute("DELETE FROM zones")
            finally:
                conn.close()

    run(go())


@needs_pg
def test_the_recovery_page_completes_a_move_when_the_file_was_lost(workdir, monkeypatch):
    """A crash after the fence and before data/database.json named PostgreSQL: start-up
    finds the fenced file and waits; the recovery page takes the PostgreSQL connection."""
    async def go():
        async with pg_database() as pg:
            async with service(monkeypatch) as svc:
                _admin, totp, c = await _signed_in_sqlite(svc)
                await _seed(svc, c)
                job = await _move(c, pg, totp)
                assert job["state"] == "done", job
            await pg.sql("UPDATE installation SET status = 'pending'")
            cfg = json.loads((workdir / "data" / "database.json").read_text())
            sqlite_cfg = {"format": 1, "backend": "sqlite", "sqlite": {"path": "data/auth.db"},
                          "installationId": cfg["installationId"], "updatedAt": cfg["updatedAt"]}
            (workdir / "data" / "database.json").write_text(json.dumps(sqlite_cfg))
            async with service(monkeypatch) as svc:
                assert svc.runtime.mode == "recovery" and svc.runtime.failure_code == "database_moved"
                r = svc.recovery_client()
                res = await r.post("/admin/api/recovery/save", json={"backend": "postgres", "postgres": pg.body(), "confirm": True})
                assert res.status_code == 202, res.text
                job = await wait_job(r, f"/admin/api/recovery/jobs/{res.json()['job']['id']}")
                assert job["state"] == "done" and job["result"]["action"] == "database.move_completed", job
                assert svc.runtime.mode == "ready" and svc.app.state.db.is_pg
                assert await svc.app.state.db.fetchval("SELECT status FROM installation") == "active"
                assert svc.config()["migrationId"] == cfg["migrationId"]

    run(go())


@needs_pg
def test_database_url_refuses_an_unfinished_copy(workdir, monkeypatch):
    from app.config import get_settings

    async def go():
        async with pg_database() as pg:
            async with service(monkeypatch) as svc:
                _admin, totp, c = await _signed_in_sqlite(svc)
                job = await _move(c, pg, totp)
                assert job["state"] == "done", job
            await pg.sql("UPDATE installation SET status = 'pending'")
            from urllib.parse import quote

            url = f"postgres://{pg.user}:{quote(pg.password)}@{pg.host}:{pg.port}/{pg.name}"
            monkeypatch.setattr(get_settings(), "database_url", url)
            with pytest.raises(RuntimeError, match="unfinished copy"):
                async with service(monkeypatch):
                    pass
            conn = await pg.admin(pg.name)
            try:
                assert await conn.fetchval("SELECT status FROM installation") == "pending"
            finally:
                await conn.close()

    run(go())


# ── Real process kills at each point of the switch ──────────────

@needs_pg
@pytest.mark.parametrize("point", ["after_commit", "after_fence", "after_publish", "after_activate"])
def test_a_move_killed_at_each_point_ends_in_one_correct_state(workdir, monkeypatch, point):
    """The child process runs a move and dies with os._exit at `point` (move_kill_child.py);
    a new service on the same files then serves the right database, or waits in recovery
    for the PostgreSQL connection, and never serves the old SQLite file after its fence."""
    import os
    import subprocess
    import sys

    child = Path(__file__).resolve().parent / "move_kill_child.py"

    from app.config import get_settings

    s = get_settings()
    # The secrets this process uses (another test module may have changed the environment
    # since they were read): the child encrypts data/database.json with them.
    secrets_env = {"SESSION_SECRET": s.session_secret, "APP_ENCRYPTION_KEY": s.app_encryption_key,
                   "ADMIN_API_KEY": s.admin_api_key}

    async def go():
        async with pg_database() as pg:
            env = dict(os.environ, MOVE_KILL_PG=json.dumps(pg.body()), PYTHONDONTWRITEBYTECODE="1", **secrets_env)
            proc = await asyncio.to_thread(
                subprocess.run, [sys.executable, str(child), point, str(workdir)],
                env=env, timeout=300, capture_output=True, text=True,
            )
            assert proc.returncode == 9, (proc.returncode, proc.stdout[-3000:], proc.stderr[-3000:])
            assert pg.password not in proc.stdout + proc.stderr
            sqlite_file = workdir / "data" / "auth.db"
            async with service(monkeypatch) as svc:
                rt = svc.runtime
                if point == "after_commit":
                    assert rt.mode == "ready" and svc.app.state.db.is_sqlite, rt.failure_code
                    assert await svc.app.state.db.fetchval("SELECT COUNT(*) FROM tokens") == 1
                    conn = await pg.admin(pg.name)
                    try:
                        assert await conn.fetchval("SELECT status FROM installation") == "pending"
                    finally:
                        await conn.close()
                elif point == "after_fence":
                    assert rt.mode == "recovery" and rt.failure_code == "database_moved"
                    r = svc.recovery_client()
                    res = await r.post("/admin/api/recovery/save", json={"backend": "postgres", "postgres": pg.body(), "confirm": True})
                    assert res.status_code == 202, res.text
                    job = await wait_job(r, f"/admin/api/recovery/jobs/{res.json()['job']['id']}")
                    assert job["state"] == "done" and job["result"]["action"] == "database.move_completed", job
                    assert rt.mode == "ready" and svc.app.state.db.is_pg
                else:
                    assert rt.mode == "ready" and svc.app.state.db.is_pg, rt.failure_code
                if svc.app.state.db.is_pg:
                    assert await svc.app.state.db.fetchval("SELECT status FROM installation") == "active"
                    assert await svc.app.state.db.fetchval("SELECT COUNT(*) FROM tokens") == 1
            conn = sqlite3.connect(sqlite_file)
            try:
                if point == "after_commit":
                    assert conn.execute("SELECT status FROM installation").fetchone()[0] == "active"
                else:
                    with pytest.raises(sqlite3.DatabaseError, match="moved to PostgreSQL"):
                        conn.execute("DELETE FROM zones")
            finally:
                conn.close()

    run(go())


# ── Measurement (run on request: MOVE_PERF=1) ───────────────────

class _SlowProxy:
    """A TCP proxy that holds each forwarded chunk for `delay` seconds: a slow network to
    the target."""

    def __init__(self, host: str, port: int, delay: float) -> None:
        self.host, self.port, self.delay = host, port, delay
        self.server = None

    async def start(self) -> int:
        async def pipe(reader, writer):
            try:
                while data := await reader.read(65536):
                    await asyncio.sleep(self.delay)
                    writer.write(data)
                    await writer.drain()
            except Exception:
                pass
            finally:
                writer.close()

        async def handle(reader, writer):
            up_r, up_w = await asyncio.open_connection(self.host, self.port)
            await asyncio.gather(pipe(reader, up_w), pipe(up_r, writer))

        self.server = await asyncio.start_server(handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]


@needs_pg
@pytest.mark.skipif(not __import__("os").environ.get("MOVE_PERF"), reason="measurement: set MOVE_PERF=1")
@pytest.mark.parametrize("delay", [0.0, 0.002])
def test_measure_the_pause_with_a_million_log_rows(workdir, monkeypatch, delay, capsys):
    import time

    async def go():
        async with pg_database() as pg:
            async with service(monkeypatch) as svc:
                await setup_sqlite(svc)
            path = workdir / "data" / "auth.db"
            conn = sqlite3.connect(path)
            conn.executemany(
                "INSERT INTO tokens (id, name, token_hash, grants, description) VALUES (?, ?, ?, '[]', ?)",
                ((str(uuid.uuid4()), f"t{i}", secrets.token_hex(32), "d" * 40) for i in range(10_000)),
            )
            conn.executemany(
                "INSERT INTO access_logs (id, token_id, token_name, area, level, result, ip_address, created_at) "
                "VALUES (?, NULL, 'tok', 'zone-a', 'read', 1, '10.1.2.3', '2026-11-01 10:00:00')",
                ((str(uuid.uuid4()),) for _ in range(1_000_000)),
            )
            conn.commit()
            conn.close()
            proxy_port = None
            if delay:
                proxy = _SlowProxy(pg.host, pg.port, delay)
                proxy_port = await proxy.start()
            async with service(monkeypatch) as svc:
                admin_id, totp = await svc.add_admin()
                c = await svc.signed_in(admin_id)
                body = pg.body(host="127.0.0.1", port=proxy_port) if proxy_port else pg.body()
                t0 = time.monotonic()
                res = await c.post("/admin/api/settings/database/move", json={"postgres": body, "code": fresh_code(totp)})
                assert res.status_code == 202, res.text
                job = svc.runtime.jobs.get(res.json()["job"]["id"])
                while job.state == "running":
                    await asyncio.sleep(0.2)
                total = time.monotonic() - t0
                assert job.state == "done", job.view()
                with capsys.disabled():
                    print(f"\nmove with 1,000,000 access_logs and 10,000 tokens, added delay {delay * 1000:g} ms per chunk: "
                          f"total {total:.1f} s, final step {job.result['finalStepMs']} ms, pause {job.result['pauseMs']} ms")

    run(go())


@needs_pg
def test_after_a_move_the_recovery_page_refuses_a_sqlite_file(workdir, monkeypatch):
    """A SQLite file put back at the old path (a backup from before the move) is never
    chosen in recovery while the saved configuration records the move."""
    async def go():
        async with pg_database() as pg:
            async with service(monkeypatch) as svc:
                _admin, totp, c = await _signed_in_sqlite(svc)
                job = await _move(c, pg, totp)
                assert job["state"] == "done", job
                migration_id = svc.config()["migrationId"]
                # A later connection change keeps the record of the move.
                job = await _edit(svc, c, pg.body(), totp)
                assert job["state"] == "done" and job["result"]["action"] == "database.connection_changed", job
                assert svc.config()["migrationId"] == migration_id
            backup = workdir / "data" / "auth.db"
            backup.unlink()
            for extra in ("-wal", "-shm"):
                Path(str(backup) + extra).unlink(missing_ok=True)
            # PostgreSQL cannot be used now (another installation's row there), and a SQLite
            # file from before the move is back at the old path; the page is asked to use it.
            await pg.sql("UPDATE installation SET id = 'someone-else'")
            await _build_sqlite(backup)
            async with service(monkeypatch) as svc:
                assert svc.runtime.mode == "recovery" and svc.runtime.failure_code == "other_installation"
                r = svc.recovery_client()
                res = await r.post("/admin/api/recovery/save", json={"backend": "sqlite", "confirm": True})
                assert res.status_code == 202, res.text
                job = await wait_job(r, f"/admin/api/recovery/jobs/{res.json()['job']['id']}")
                assert job["state"] == "failed" and job["code"] == "database_moved", job
                assert svc.runtime.mode == "recovery"

    run(go())


@needs_pg
def test_a_failed_stop_of_background_work_starts_it_again(workdir, monkeypatch):
    from app.db_runtime import OperationError

    async def go():
        async with pg_database() as pg:
            async with service(monkeypatch) as svc:
                _admin, totp, c = await _signed_in_sqlite(svc)
                rt = svc.runtime
                real_stop = rt._stop_background

                async def busy_stop():
                    await real_stop()
                    raise OperationError("background_busy")

                monkeypatch.setattr(rt, "_stop_background", busy_stop)
                job = await _move(c, pg, totp)
                assert job["state"] == "failed" and job["code"] == "background_busy", job
                for _ in range(50):
                    if rt.tasks:
                        break
                    await asyncio.sleep(0.05)
                assert rt.tasks and any(not t.done() for t in rt.tasks), "background work runs again"
                assert svc.app.state.db.is_sqlite

    run(go())


@needs_pg
def test_adopting_an_older_postgres_database_after_a_move_keeps_its_record(workdir, monkeypatch):
    """A backup without the installation table, adopted from Settings after a move, still
    records the move (installation row and data/database.json)."""
    async def go():
        from urllib.parse import quote

        from app.db import create_database
        from app.schema import ensure_database_schema

        async with pg_database() as pg:
            older = await pg.add_database()
            db = await create_database(f"postgres://{pg.user}:{quote(pg.password)}@{pg.host}:{pg.port}/{older}")
            try:
                await ensure_database_schema(db)
                await db.execute("DROP TABLE installation")
            finally:
                await db.close()
            async with service(monkeypatch) as svc:
                _admin, totp, c = await _signed_in_sqlite(svc)
                job = await _move(c, pg, totp)
                assert job["state"] == "done", job
                migration_id = svc.config()["migrationId"]
                res = await c.put("/admin/api/settings/database",
                                  json={"postgres": pg.body(database=older), "code": fresh_code(totp), "confirmCopy": True})
                assert res.status_code == 202, res.text
                # Read from the runtime: the adopted copy has no admin, so this session ends.
                job = svc.runtime.jobs.get(res.json()["job"]["id"])
                while job.state == "running":
                    await asyncio.sleep(0.05)
                assert job.state == "done" and job.result["action"] == "database.adopted", job.view()
                assert svc.config()["migrationId"] == migration_id
                assert await svc.app.state.db.fetchval("SELECT migration_id FROM installation") == migration_id

    run(go())

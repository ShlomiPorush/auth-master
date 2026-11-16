"""General settings on the admin Settings page: time zone, Secure session cookie, and log
retention (app.general_settings, /admin/api/settings/general), and what uses them.

- Validation and environment locks, including an empty or invalid TZ.
- Startup copy of environment values into the stored settings, with lost saves retried
  and a start that fails after too many.
- HTTP: save, conflict, locked, confirmations bound to the values (shorter retention,
  Secure cookie over plain HTTP), activity log entries.
- Purge: uses the stored retention only, skips when it cannot read it or this process
  differs, is a no-op when a version is saved in between, and keeps the access log purge
  boundary (raised before deleting, never by an empty purge, never lowered).
- Usage: data start after a retention increase, the report's own time zone.
- The Secure flag of issued cookies follows the setting.

Every secret is generated per run with ``secrets``; Redis is faked.  PostgreSQL cases run
when TEST_DATABASE_URL is set (they delete the rows of the tables they use there).
Requires pytest and httpx.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
import tempfile
import uuid
from dataclasses import replace
from functools import partial
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_TMP_DIR = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)

# Used only when this module is the first to load the settings.
os.environ.setdefault("SESSION_SECRET", secrets.token_urlsafe(48))
os.environ.setdefault("APP_ENCRYPTION_KEY", secrets.token_hex(16))
os.environ.setdefault("REDIS_URL", f"redis://:{secrets.token_hex(32)}@127.0.0.1:6379/0")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP_DIR.name}/general.db")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

PG_URL = os.environ.get("TEST_DATABASE_URL")
BACKENDS = ["sqlite", pytest.param("postgres", marks=pytest.mark.skipif(not PG_URL, reason="TEST_DATABASE_URL is not set"))]
ENV_NAMES = ("TZ", "COOKIE_SECURE", "ACTIVITY_LOG_RETENTION_DAYS", "ACCESS_LOG_RETENTION_DAYS")
DAY = timedelta(days=1)


def _gs():
    from app import general_settings

    return general_settings


def use_env(monkeypatch, **env: str) -> None:
    """The general settings see exactly these of ENV_NAMES set (the rest unset)."""
    from app.config import Settings

    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    s = Settings(_env_file=None)
    monkeypatch.setattr(_gs(), "get_settings", lambda: s)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    use_env(monkeypatch)
    _gs().reset_state()
    yield
    _gs().reset_state()


def _run(coro):
    return asyncio.run(coro)


# ── Validation ──────────────────────────────────────────────────

@pytest.mark.parametrize("name,value", [
    ("timeZone", "Not/AZone"), ("timeZone", ""), ("timeZone", " UTC"), ("timeZone", 5),
    ("cookieSecure", 1), ("cookieSecure", "true"), ("cookieSecure", None),
    ("activityLogRetentionDays", 0), ("activityLogRetentionDays", 3651), ("activityLogRetentionDays", True),
    ("accessLogRetentionDays", -1), ("accessLogRetentionDays", 1.5), ("accessLogRetentionDays", "30"),
])
def test_invalid_values_are_refused(name, value):
    gs = _gs()
    with pytest.raises(gs.InvalidSettings) as e:
        gs.check(name, value)
    assert e.value.field == name


@pytest.mark.parametrize("name,value", [
    ("timeZone", "Asia/Jerusalem"), ("timeZone", "UTC"), ("cookieSecure", False),
    ("activityLogRetentionDays", 1), ("accessLogRetentionDays", 3650),
])
def test_valid_values_are_kept(name, value):
    assert _gs().check(name, value) == value


@pytest.mark.parametrize("text", [
    "nope", "[]", '{"format": 2}',
    '{"format": 1, "timeZone": null, "cookieSecure": null, "activityLogRetentionDays": null}',
    '{"format": 1, "timeZone": "Mars/Base", "cookieSecure": null, "activityLogRetentionDays": null, "accessLogRetentionDays": null}',
    '{"format": 1, "timeZone": null, "cookieSecure": null, "activityLogRetentionDays": 0, "accessLogRetentionDays": null}',
    '{"format": 1, "timeZone": null, "cookieSecure": null, "activityLogRetentionDays": null, "accessLogRetentionDays": null, "x": 1}',
])
def test_stored_value_is_checked(text):
    gs = _gs()
    with pytest.raises(gs.InvalidSettings):
        gs.parse_value(text)


def test_stored_value_round_trips():
    gs = _gs()
    values = {"timeZone": "Asia/Jerusalem", "cookieSecure": True, "activityLogRetentionDays": 90, "accessLogRetentionDays": None}
    assert gs.parse_value(gs.dump_value(values)) == values


# ── Locks and effective values ──────────────────────────────────

def test_defaults_when_nothing_is_set():
    gs = _gs()
    eff = gs.effective(gs.EMPTY)
    assert (eff.time_zone, eff.cookie_secure, eff.activity_log_retention_days, eff.access_log_retention_days) == ("UTC", False, 30, 30)
    assert gs.locked_fields() == dict.fromkeys(gs.FIELDS, False)


def test_environment_locks_and_wins(monkeypatch):
    gs = _gs()
    use_env(monkeypatch, TZ="Asia/Kolkata", ACCESS_LOG_RETENTION_DAYS="7")
    stored = gs.Stored(4, {"timeZone": "Europe/Paris", "cookieSecure": True, "activityLogRetentionDays": 60, "accessLogRetentionDays": 90})
    eff = gs.effective(stored)
    assert gs.locked_fields() == {"timeZone": True, "cookieSecure": False, "activityLogRetentionDays": False, "accessLogRetentionDays": True}
    assert (eff.version, eff.time_zone, eff.cookie_secure, eff.activity_log_retention_days, eff.access_log_retention_days) == (
        4, "Asia/Kolkata", True, 60, 7,
    )


@pytest.mark.parametrize("tz", ["", "   ", "Not/AZone", "../../etc/passwd"])
def test_empty_or_invalid_tz_locks_the_field_and_uses_utc(monkeypatch, tz):
    gs = _gs()
    use_env(monkeypatch, TZ=tz)
    assert gs.locked_fields()["timeZone"] and gs.env_time_zone_invalid()
    assert gs.env_value("timeZone") is None
    stored = replace(gs.EMPTY, values={**gs.EMPTY.values, "timeZone": "Asia/Jerusalem"})
    assert gs.effective(stored).time_zone == "UTC"


def test_tz_with_surrounding_space_is_the_zone(monkeypatch):
    gs = _gs()
    use_env(monkeypatch, TZ=" Asia/Jerusalem ")
    assert not gs.env_time_zone_invalid()
    assert gs.effective(gs.EMPTY).time_zone == "Asia/Jerusalem"


def test_apply_never_goes_back():
    gs = _gs()
    assert gs.apply(replace(gs.DEFAULT_EFFECTIVE, version=3, time_zone="Asia/Jerusalem"))
    assert not gs.apply(replace(gs.DEFAULT_EFFECTIVE, version=2, time_zone="UTC"))
    assert gs.current().time_zone == "Asia/Jerusalem"


# ── Storage ─────────────────────────────────────────────────────

async def _open(url):
    from app import db as db_mod
    from app.schema import ensure_database_schema

    main = await db_mod.create_database(url)
    await ensure_database_schema(main)
    if main.is_pg:
        for table in ("app_settings", "app_state", "activity_logs", "access_logs", "access_log_rollup"):
            await main.execute(f"DELETE FROM {table}")
        await main.execute("UPDATE access_log_rollup_state SET valid_from = NULL")
    settings_db, own = await db_mod.open_settings_database(main)
    return main, settings_db, own


async def _close(main, settings_db, own):
    if own:
        await settings_db.close()
    await main.close()


def _url(backend, tmp_path):
    return f"sqlite:///{tmp_path}/store.db" if backend == "sqlite" else PG_URL


async def _settings_events(main):
    rows = await main.fetch(
        "SELECT actor, details FROM activity_logs WHERE action = 'settings.update' AND entity_name = 'general' ORDER BY created_at"
    )
    return [(r["actor"], json.loads(r["details"])) for r in rows]


@pytest.mark.parametrize("backend", BACKENDS)
def test_empty_cookie_secure_is_not_set_and_keeps_the_saved_value(backend, tmp_path, monkeypatch):
    """An empty COOKIE_SECURE (what the bundled compose file passes when it is not set) does
    not lock the field, the saved value applies, and a start copies nothing over it."""
    gs = _gs()

    async def scenario():
        main, sdb, own = await _open(_url(backend, tmp_path))
        try:
            await gs.load(sdb, main)
            stored = await gs.read_current(sdb)
            saved = gs.dump_value({**stored.values, "cookieSecure": True})
            assert await gs.insert_version(sdb, stored.version + 1, saved, "admin", None)
            for value in ("", "   "):
                use_env(monkeypatch, COOKIE_SECURE=value)
                assert gs.locked_fields()["cookieSecure"] is False
                await gs.load(sdb, main)
                assert (await gs.read_current(sdb)).version == stored.version + 1
                assert gs.current().cookie_secure is True
            assert await _settings_events(main) == []
        finally:
            await _close(main, sdb, own)

    _run(scenario())


@pytest.mark.parametrize("backend", BACKENDS)
def test_startup_copies_environment_values_once(backend, tmp_path, monkeypatch):
    gs = _gs()
    use_env(monkeypatch, ACCESS_LOG_RETENTION_DAYS="45", COOKIE_SECURE="false", TZ="Not/AZone")

    async def scenario():
        main, sdb, own = await _open(_url(backend, tmp_path))
        try:
            await gs.load(sdb, main)
            stored = await gs.read_current(sdb)
            # The invalid TZ is not copied; COOKIE_SECURE=false is a value like any other.
            assert stored.version == 1 and stored.actor == "environment"
            assert stored.values == {"timeZone": None, "cookieSecure": False, "activityLogRetentionDays": None, "accessLogRetentionDays": 45}
            events = await _settings_events(main)
            assert len(events) == 1 and events[0][0] == "environment"
            assert events[0][1]["version"] == 1 and events[0][1]["to"]["accessLogRetentionDays"] == 45
            assert gs.current().access_log_retention_days == 45 and gs.current().time_zone == "UTC"

            # A second start with the same environment writes nothing.
            await gs.load(sdb, main)
            assert (await gs.read_current(sdb)).version == 1

            # Once the variable is removed, the last value stays instead of the default.
            use_env(monkeypatch)
            await gs.load(sdb, main)
            assert (await gs.read_current(sdb)).version == 1
            assert gs.current().access_log_retention_days == 45
        finally:
            await _close(main, sdb, own)

    _run(scenario())


@pytest.mark.parametrize("backend", BACKENDS)
def test_concurrent_starts_save_the_copy_once(backend, tmp_path, monkeypatch):
    gs = _gs()
    from app import db as db_mod

    use_env(monkeypatch, ACTIVITY_LOG_RETENTION_DAYS="12")

    async def scenario():
        main, sdb, own = await _open(_url(backend, tmp_path))
        other, other_own = await db_mod.open_settings_database(main)
        try:
            await asyncio.gather(gs.sync_from_environment(sdb, main), gs.sync_from_environment(other, main))
            assert await main.fetchval("SELECT COUNT(*) FROM app_settings") == 1
            assert (await gs.read_current(sdb)).values["activityLogRetentionDays"] == 12
        finally:
            if other_own:
                await other.close()
            await _close(main, sdb, own)

    _run(scenario())


def test_lost_copies_are_retried_then_stop_the_start(tmp_path, monkeypatch):
    gs = _gs()
    use_env(monkeypatch, ACTIVITY_LOG_RETENTION_DAYS="12")
    calls = []

    async def always_lost(*args):
        calls.append(args[1])
        return False

    monkeypatch.setattr(gs, "insert_version", always_lost)

    async def scenario():
        main, sdb, own = await _open(_url("sqlite", tmp_path))
        try:
            with pytest.raises(RuntimeError, match="ACTIVITY_LOG_RETENTION_DAYS"):
                await gs.load(sdb, main)
        finally:
            await _close(main, sdb, own)

    _run(scenario())
    assert len(calls) == gs.SYNC_ATTEMPTS


def test_a_lost_copy_is_rechecked_and_retried(tmp_path, monkeypatch):
    gs = _gs()
    use_env(monkeypatch, ACTIVITY_LOG_RETENTION_DAYS="12")
    real_insert = gs.insert_version

    async def scenario():
        main, sdb, own = await _open(_url("sqlite", tmp_path))
        lost = []

        async def first_lost(db, version, value, actor, ip):
            if not lost:
                # Another process saves another value first.
                lost.append(version)
                await real_insert(db, version, gs.dump_value({**gs.EMPTY.values, "cookieSecure": True}), "other", None)
                return False
            return await real_insert(db, version, value, actor, ip)

        monkeypatch.setattr(gs, "insert_version", first_lost)
        try:
            await gs.load(sdb, main)
            stored = await gs.read_current(sdb)
            assert stored.version == 2
            # The other save is kept and the environment value is added on top of it.
            assert stored.values["cookieSecure"] is True and stored.values["activityLogRetentionDays"] == 12
        finally:
            await _close(main, sdb, own)

    _run(scenario())


def test_invalid_stored_settings_stop_the_start_and_reload_keeps_the_last_good(tmp_path):
    gs = _gs()

    async def scenario():
        main, sdb, own = await _open(_url("sqlite", tmp_path))
        try:
            await gs.load(sdb, main)
            await main.execute(
                "INSERT INTO app_settings (version, value, actor) VALUES (1, $1, 'x')",
                gs.dump_value({**gs.EMPTY.values, "timeZone": "Asia/Jerusalem"}),
            )
            await gs.reload_once(sdb)
            assert gs.current().time_zone == "Asia/Jerusalem" and gs.status.last_error is None
            await main.execute("INSERT INTO app_settings (version, value, actor) VALUES (2, 'broken', 'x')")
            await gs.reload_once(sdb)
            assert gs.current().version == 1 and "version 2" in gs.status.last_error
            with pytest.raises(RuntimeError, match="version 2 in the table app_settings"):
                await gs.load(sdb, main)
        finally:
            await _close(main, sdb, own)

    _run(scenario())


# ── Purge ───────────────────────────────────────────────────────

async def _add_logs(main, table, ages_days):
    now = datetime.now(timezone.utc)
    for age in ages_days:
        at = now - timedelta(days=age)
        stored = at.strftime("%Y-%m-%d %H:%M:%S") if main.is_sqlite else at
        if table == "activity_logs":
            await main.execute(
                "INSERT INTO activity_logs (id, actor, action, created_at) VALUES ($1::uuid, $2, $3, $4::timestamptz)",
                str(uuid.uuid4()), "t", "test", stored,
            )
        else:
            await main.execute(
                "INSERT INTO access_logs (id, area, level, result, created_at) VALUES ($1::uuid, $2, $3, $4, $5::timestamptz)",
                str(uuid.uuid4()), "orders", "read", True, stored,
            )


async def _count(main, table):
    return int(await main.fetchval(f"SELECT COUNT(*) FROM {table}"))


async def _boundary(sdb):
    from app.logger import read_purged_before

    mark = await read_purged_before(sdb)
    return None if mark is None else mark.at


async def _save(sdb, **values):
    gs = _gs()
    stored = await gs.read_current(sdb)
    assert await gs.insert_version(sdb, stored.version + 1, gs.dump_value({**stored.values, **values}), "admin", None)
    return stored.version + 1


@pytest.mark.parametrize("backend", BACKENDS)
def test_purge_uses_the_stored_retention_and_raises_the_boundary(backend, tmp_path):
    gs = _gs()
    from app.logger import purge_old_logs

    async def scenario():
        main, sdb, own = await _open(_url(backend, tmp_path))
        try:
            await gs.load(sdb, main)
            await _add_logs(main, "activity_logs", [1, 5, 15, 40])
            await _add_logs(main, "access_logs", [1, 5, 15, 40])
            await _save(sdb, activityLogRetentionDays=10, accessLogRetentionDays=3)
            await gs.reload_once(sdb)
            before = datetime.now(timezone.utc)
            await purge_old_logs(main, sdb)
            assert await _count(main, "activity_logs") == 2
            assert await _count(main, "access_logs") == 1
            assert gs.purge_status["activity"].state == gs.purge_status["access"].state == "ok"
            boundary = await _boundary(sdb)
            assert abs((boundary - (before - 3 * DAY)).total_seconds()) < 60

            # Nothing older than the cutoff: the boundary stays where it is.
            await asyncio.sleep(1.1)
            await purge_old_logs(main, sdb)
            assert await _boundary(sdb) == boundary

            # A longer retention later never moves the boundary back.
            await _add_logs(main, "access_logs", [8])
            await _save(sdb, accessLogRetentionDays=5)
            await gs.reload_once(sdb)
            await purge_old_logs(main, sdb)
            assert await _count(main, "access_logs") == 1
            await _save(sdb, accessLogRetentionDays=30)
            await gs.reload_once(sdb)
            await _add_logs(main, "access_logs", [20])
            await purge_old_logs(main, sdb)
            assert await _boundary(sdb) > before - 3 * DAY - timedelta(seconds=60)
        finally:
            await _close(main, sdb, own)

    _run(scenario())


async def _add_access_at(main, times):
    from app.usage_rollup import bind_time

    for at in times:
        await main.execute(
            "INSERT INTO access_logs (id, area, level, result, created_at) VALUES ($1::uuid, $2, $3, $4, $5::timestamptz)",
            str(uuid.uuid4()), "orders", "read", True, bind_time(main, at),
        )


@pytest.mark.parametrize("backend", BACKENDS)
def test_purge_runs_in_chunks_and_never_hides_kept_rows(backend, tmp_path, monkeypatch):
    """Each chunk holds the write lock briefly; between chunks the boundary is the oldest
    kept row, marked when the chunk ended inside the rows of that time, and once nothing
    older is left it is the cutoff."""
    gs = _gs()
    from app import logger, usage_rollup
    from app.logger import purge_old_logs, read_purged_before
    from app.usage_rollup import PurgeMark

    async def scenario():
        main, sdb, own = await _open(_url(backend, tmp_path))
        try:
            await gs.load(sdb, main)
            await _add_logs(main, "access_logs", [1])
            # Old rows at five times, three at the same middle time: the first chunk of three
            # ends inside them.
            start = (datetime.now(timezone.utc) - 40 * DAY).replace(microsecond=0)
            middle = timedelta(seconds=2, microseconds=123)
            offsets = [timedelta(0), timedelta(seconds=1), middle, middle, middle, timedelta(seconds=3), timedelta(seconds=4)]
            await _add_access_at(main, [start + o for o in offsets])
            await _save(sdb, accessLogRetentionDays=3, activityLogRetentionDays=30)
            await gs.reload_once(sdb)
            monkeypatch.setattr(logger, "purge_access_logs", partial(usage_rollup.purge_access_logs, chunk_rows=3))
            seen = []

            async def watch(_seconds):
                # Between two chunks, each committed.
                left = await main.fetchval("SELECT MIN(created_at) FROM access_logs")
                seen.append((await read_purged_before(sdb), usage_rollup._as_time(main, left)))

            monkeypatch.setattr(usage_rollup, "_pause", watch)
            before = datetime.now(timezone.utc)
            await purge_old_logs(main, sdb)
            assert await _count(main, "access_logs") == 1
            # SQLite keeps whole seconds.
            mid = start + (timedelta(seconds=2) if main.is_sqlite else middle)
            four = start + timedelta(seconds=4)
            assert seen == [(PurgeMark(mid, at_too=True), mid), (PurgeMark(four), four)]
            assert abs((await _boundary(sdb) - (before - 3 * DAY)).total_seconds()) < 60
        finally:
            await _close(main, sdb, own)

    _run(scenario())


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("tie", [False, True], ids=["split-day", "split-time"])
def test_purge_stopped_between_chunks_counts_every_kept_row(backend, tie, tmp_path, monkeypatch):
    """A save stops the purge after a chunk that deleted the first rows of a day (split-time:
    some of the rows of its first moment, midnight): the Usage report, with the boundary and
    the summary as the page reads them, counts every kept row of that day, marks it partial,
    and marks the day before, whose rows are all gone, empty."""
    from zoneinfo import ZoneInfo

    gs = _gs()
    from app import db as db_mod, logger, usage_rollup
    from app.logger import purge_old_logs, read_purged_before
    from app.routers.admin_usage import _Period, _token_usage
    from app.usage_rollup import PurgeMark

    async def scenario():
        main, sdb, own = await _open(_url(backend, tmp_path))
        try:
            await gs.load(sdb, main)
            now = datetime.now(timezone.utc)
            midnight = (now - 10 * DAY).replace(hour=0, minute=0, second=0, microsecond=0)
            # PostgreSQL keeps microseconds, SQLite whole seconds.
            first, kept = (midnight, midnight + timedelta(seconds=1)) if main.is_sqlite else (
                midnight + timedelta(microseconds=100), midnight + timedelta(microseconds=300))
            if tie:
                first = kept = midnight
            await _add_access_at(main, [midnight - timedelta(seconds=1)] * 2 + [first] * 2 + [kept] * 3)
            await usage_rollup.roll_up(main, 30, now, pause=0)
            await _save(sdb, accessLogRetentionDays=3, activityLogRetentionDays=30)
            await gs.reload_once(sdb)
            monkeypatch.setattr(logger, "purge_access_logs", partial(usage_rollup.purge_access_logs, chunk_rows=4))

            async def save_after_first_chunk(_seconds):
                await _save(sdb, accessLogRetentionDays=30)

            monkeypatch.setattr(usage_rollup, "_pause", save_after_first_chunk)
            await purge_old_logs(main, sdb)
            assert await _count(main, "access_logs") == 3
            async with db_mod.report_database(main) as report:
                boundary = await read_purged_before(report)
                assert boundary == PurgeMark(kept, at_too=tie)
                p = _Period("30d", now=now, tz=ZoneInfo("UTC"), retention_days=30, purged_before=boundary)
                body = await _token_usage(report, p, None)
            i = next(i for i, (start, _end) in enumerate(p.buckets) if start == midnight)
            assert body["series"][i]["coverage"] == "partial"
            assert body["series"][i - 1]["coverage"] == "none"
            assert body["series"][i]["allowed"] == 3 and body["totals"]["calls"] == 3
        finally:
            await _close(main, sdb, own)

    _run(scenario())


@pytest.mark.parametrize("backend", BACKENDS)
def test_purge_is_a_no_op_when_a_version_is_saved_in_between(backend, tmp_path, monkeypatch):
    gs = _gs()
    from app.logger import purge_old_logs

    async def scenario():
        main, sdb, own = await _open(_url(backend, tmp_path))
        try:
            await gs.load(sdb, main)
            await _add_logs(main, "activity_logs", [40])
            await _add_logs(main, "access_logs", [40])
            real_read = gs.read_current

            async def read_then_someone_saves(db):
                stored = await real_read(db)
                await main.execute(
                    "INSERT INTO app_settings (version, value, actor) VALUES ($1, $2, 'other')",
                    stored.version + 1, gs.dump_value({**stored.values, "accessLogRetentionDays": 365, "activityLogRetentionDays": 365}),
                )
                return stored

            monkeypatch.setattr(gs, "read_current", read_then_someone_saves)
            boundary = await _boundary(sdb)
            await purge_old_logs(main, sdb)
            assert await _count(main, "activity_logs") == 1
            assert await _count(main, "access_logs") == 1
            # Nothing was deleted, so the Usage page must not treat the kept row as deleted.
            assert await _boundary(sdb) == boundary
        finally:
            await _close(main, sdb, own)

    _run(scenario())


def test_purge_skips_when_the_settings_cannot_be_read(tmp_path):
    gs = _gs()
    from app.logger import purge_old_logs

    async def scenario():
        main, sdb, own = await _open(_url("sqlite", tmp_path))
        try:
            await gs.load(sdb, main)
            await _add_logs(main, "activity_logs", [40])
            await main.execute("INSERT INTO app_settings (version, value, actor) VALUES (1, 'broken', 'x')")
            await purge_old_logs(main, sdb)
            assert await _count(main, "activity_logs") == 1
            assert gs.purge_status["activity"].state == "skipped"
            assert "version 1" in gs.purge_status["activity"].reason
        finally:
            await _close(main, sdb, own)

    _run(scenario())


def test_purge_skips_when_this_process_differs_from_the_saved_value(tmp_path, monkeypatch):
    gs = _gs()
    from app.logger import purge_old_logs

    async def scenario():
        main, sdb, own = await _open(_url("sqlite", tmp_path))
        try:
            await gs.load(sdb, main)
            await _add_logs(main, "activity_logs", [20])
            await _add_logs(main, "access_logs", [20])
            # Saved by another process, not reloaded here yet: this process still keeps 30 days.
            await _save(sdb, activityLogRetentionDays=60, accessLogRetentionDays=10)
            await purge_old_logs(main, sdb)
            assert await _count(main, "access_logs") == 1
            assert gs.purge_status["access"].state == "skipped"
            assert "10 days" in gs.purge_status["access"].reason
            # Nothing older than 60 days, but the rule is the same: this process differs.
            assert gs.purge_status["activity"].state == "skipped"

            # An environment value that differs from the saved one never deletes by itself.
            use_env(monkeypatch, ACCESS_LOG_RETENTION_DAYS="5")
            await gs.reload_once(sdb)
            assert gs.current().access_log_retention_days == 5
            await purge_old_logs(main, sdb)
            assert await _count(main, "access_logs") == 1
            assert "ACCESS_LOG_RETENTION_DAYS" in gs.purge_status["access"].reason
        finally:
            await _close(main, sdb, own)

    _run(scenario())


def test_environment_default_value_without_a_saved_one_does_not_purge(tmp_path, monkeypatch):
    gs = _gs()
    from app.logger import purge_old_logs

    async def scenario():
        main, sdb, own = await _open(_url("sqlite", tmp_path))
        try:
            await gs.load(sdb, main)  # nothing set: nothing stored
            use_env(monkeypatch, ACCESS_LOG_RETENTION_DAYS="30")
            await _add_logs(main, "access_logs", [40])
            await purge_old_logs(main, sdb)
            # Locked but not saved yet (the copy runs at the next start): not this process's call.
            assert gs.purge_status["access"].state == "skipped"
            assert await _count(main, "access_logs") == 1
        finally:
            await _close(main, sdb, own)

    _run(scenario())


@pytest.mark.parametrize("backend", BACKENDS)
def test_initial_boundary_is_set_once(backend, tmp_path):
    from app.logger import init_purged_before

    async def scenario():
        main, sdb, own = await _open(_url(backend, tmp_path))
        try:
            before = datetime.now(timezone.utc)
            await init_purged_before(sdb, 30)
            first = await _boundary(sdb)
            assert abs((first - (before - 30 * DAY)).total_seconds()) < 60
            await init_purged_before(sdb, 5)
            assert await _boundary(sdb) == first
        finally:
            await _close(main, sdb, own)

    _run(scenario())


# ── HTTP ────────────────────────────────────────────────────────

class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, k):
        return self.data.get(k)

    async def setex(self, k, _ttl, v):
        self.data[k] = v

    async def delete(self, *keys):
        for k in keys:
            self.data.pop(k, None)

    async def ping(self):
        return True

    async def aclose(self):
        pass


class Admin:
    def __init__(self, client, fake) -> None:
        self.client = client
        self.fake = fake
        self.csrf = ""

    def login(self) -> None:
        import app.main as main_mod
        from app.sessions import COOKIE_NAME, session_record

        # A full session counts only for an existing admin with MFA enabled (one per database).
        admin_id = self.client.portal.call(main_mod.app.state.db.fetchval, "SELECT id FROM admin_users WHERE totp_enabled = $1", True)
        if admin_id is None:
            admin_id = str(uuid.uuid4())
            self.client.portal.call(
                main_mod.app.state.db.execute,
                "INSERT INTO admin_users (id, username, password_hash, totp_enabled) VALUES ($1, $2, $3, $4)",
                admin_id, "settings-admin", "unused", True,
            )
        sid = secrets.token_hex(32)
        self.csrf = secrets.token_hex(24)
        key, value = session_record(sid, {"kind": "full", "adminId": str(admin_id), "csrf": self.csrf})
        self.fake.data[key] = value
        self.client.cookies.set(COOKIE_NAME, sid)

    def get(self):
        return self.client.get("/admin/api/settings/general")

    def put(self, body, https=False, csrf=True):
        headers = {"X-Forwarded-Proto": "https" if https else "http", "X-Forwarded-For": "10.9.9.9"}
        if csrf:
            headers["X-CSRF-Token"] = self.csrf
        return self.client.put("/admin/api/settings/general", json=body, headers=headers)

    def db(self, sql, *args):
        import app.main as main_mod

        return self.client.portal.call(main_mod.app.state.db.fetch, sql, *args)

    def call(self, fn, *args):
        return self.client.portal.call(fn, *args)


@pytest.fixture()
def admin(monkeypatch, tmp_path):
    import app.main as main_mod
    from app import db as db_mod
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    fake = FakeRedis()
    monkeypatch.setattr(main_mod.redis, "from_url", lambda *a, **k: fake)
    url = f"sqlite:///{tmp_path}/http.db"
    create_database = db_mod.create_database
    monkeypatch.setattr(main_mod, "create_database", lambda _url: create_database(url))

    async def _no_cleanup(*_a, **_k):
        return None

    monkeypatch.setattr(main_mod, "cleanup_expired_tokens", _no_cleanup)
    with TestClient(ProxyHeadersMiddleware(main_mod.app, trusted_hosts="*")) as c:
        a = Admin(c, fake)
        yield a


def test_needs_a_full_session_and_csrf(admin):
    assert admin.get().status_code == 401
    assert admin.put({"baseVersion": 0, "accessLogRetentionDays": 40}).status_code == 401
    admin.login()
    assert admin.put({"baseVersion": 0, "accessLogRetentionDays": 40}, csrf=False).status_code == 403


def test_view_has_every_field(admin):
    admin.login()
    view = admin.get().json()
    assert view["version"] == 0 and view["savedBy"] is None
    assert view["fields"]["timeZone"] == {"value": "UTC", "source": "default", "locked": False, "envName": "TZ", "default": "UTC", "envInvalid": False}
    assert view["fields"]["cookieSecure"] == {"value": False, "source": "default", "locked": False, "envName": "COOKIE_SECURE", "default": False}
    assert view["fields"]["accessLogRetentionDays"] == {
        "value": 30, "source": "default", "locked": False, "envName": "ACCESS_LOG_RETENTION_DAYS", "default": 30, "min": 1, "max": 3650,
    }
    assert "Asia/Jerusalem" in view["timeZones"] and view["timeZones"] == sorted(view["timeZones"])
    assert view["applied"]["version"] == 0 and view["applied"]["lastReloadError"] is None
    assert set(view["purge"]) == {"activity", "access"}
    assert view["purge"]["access"]["state"] in ("ok", "skipped", "not_run")
    assert view["rootPath"]["envName"] == "ROOT_PATH"


def test_save_applies_and_records(admin):
    gs = _gs()
    admin.login()
    res = admin.put({"baseVersion": 0, "timeZone": "Asia/Jerusalem", "activityLogRetentionDays": 90})
    assert res.status_code == 200, res.text
    view = res.json()
    assert view["version"] == 1 and view["savedFrom"] == "10.9.9.9"
    assert view["fields"]["timeZone"]["value"] == "Asia/Jerusalem" and view["fields"]["timeZone"]["source"] == "stored"
    assert view["fields"]["activityLogRetentionDays"]["value"] == 90
    assert gs.current().time_zone == "Asia/Jerusalem" and gs.current().version == 1
    # Dates are now shown in the saved zone.
    assert view["savedAt"].endswith(("+02:00", "+03:00"))

    events = admin.db("SELECT actor, details FROM activity_logs WHERE action = 'settings.update' AND entity_name = 'general'")
    assert len(events) == 1
    details = json.loads(events[0]["details"])
    assert details["version"] == 1
    assert details["from"] == {"timeZone": None, "cookieSecure": None, "activityLogRetentionDays": None, "accessLogRetentionDays": None}
    assert details["to"] == {"timeZone": "Asia/Jerusalem", "cookieSecure": None, "activityLogRetentionDays": 90, "accessLogRetentionDays": None}

    # The same values again write nothing.
    assert admin.put({"baseVersion": 1, "timeZone": "Asia/Jerusalem"}).json()["version"] == 1


def test_stale_version_is_a_conflict(admin):
    admin.login()
    assert admin.put({"baseVersion": 0, "activityLogRetentionDays": 60}).status_code == 200
    res = admin.put({"baseVersion": 0, "timeZone": "Asia/Jerusalem"})
    assert res.status_code == 409 and res.json()["code"] == "conflict"
    assert res.json()["current"]["fields"]["activityLogRetentionDays"]["value"] == 60


def test_two_saves_from_one_version_one_conflict(admin):
    gs = _gs()
    admin.login()
    import app.main as main_mod

    sdb = main_mod.app.state.settings_db

    async def both():
        return await asyncio.gather(
            gs.insert_version(sdb, 1, gs.dump_value({**gs.EMPTY.values, "activityLogRetentionDays": 40}), "a", None),
            gs.insert_version(sdb, 1, gs.dump_value({**gs.EMPTY.values, "activityLogRetentionDays": 50}), "b", None),
        )

    assert sorted(admin.call(both)) == [False, True]
    res = admin.put({"baseVersion": 0, "activityLogRetentionDays": 60})
    assert res.status_code == 409 and res.json()["code"] == "conflict"


def test_invalid_input_is_refused(admin):
    admin.login()
    for body, field in [
        ({"baseVersion": 0, "timeZone": "Mars/Base"}, "timeZone"),
        ({"baseVersion": 0, "timeZone": "Factory"}, "timeZone"),  # in tzdata, but not a place
        ({"baseVersion": 0, "activityLogRetentionDays": 0}, "activityLogRetentionDays"),
        ({"baseVersion": 0, "accessLogRetentionDays": 3651}, "accessLogRetentionDays"),
        ({"baseVersion": 0, "cookieSecure": None}, "cookieSecure"),
    ]:
        res = admin.put(body)
        assert res.status_code == 422, body
        assert res.json()["code"] == "invalid" and res.json()["field"] == field
    for body in ({"baseVersion": 0, "accessLogRetentionDays": "30"}, {"baseVersion": 0, "cookieSecure": 1}, {"baseVersion": 0, "x": 1}, {}):
        assert admin.put(body).status_code == 422, body
    assert admin.get().json()["version"] == 0


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_cookie_secure_leaves_the_field_on_the_page(admin, monkeypatch, value):
    """The bundled compose file passes COOKIE_SECURE empty when it is not set."""
    use_env(monkeypatch, COOKIE_SECURE=value)
    admin.login()
    view = admin.get().json()
    assert view["fields"]["cookieSecure"] == {"value": False, "source": "default", "locked": False, "envName": "COOKIE_SECURE", "default": False}
    res = admin.put({"baseVersion": view["version"], "cookieSecure": True}, https=True)
    assert res.status_code == 200, res.text
    assert res.json()["fields"]["cookieSecure"] == {"value": True, "source": "stored", "locked": False, "envName": "COOKIE_SECURE", "default": False}


def test_locked_fields_cannot_be_sent(admin, monkeypatch):
    use_env(monkeypatch, COOKIE_SECURE="true", TZ="")
    admin.login()
    view = admin.get().json()
    assert view["fields"]["cookieSecure"] == {"value": True, "source": "env", "locked": True, "envName": "COOKIE_SECURE", "default": False}
    assert view["fields"]["timeZone"] == {"value": "UTC", "source": "env", "locked": True, "envName": "TZ", "default": "UTC", "envInvalid": True}
    for body, field in (({"baseVersion": 0, "cookieSecure": False}, "cookieSecure"), ({"baseVersion": 0, "timeZone": "UTC"}, "timeZone")):
        res = admin.put(body)
        assert res.status_code == 409 and res.json() == {**res.json(), "code": "locked", "field": field}
    assert admin.put({"baseVersion": 0, "activityLogRetentionDays": 40}).status_code == 200


def test_shorter_retention_needs_a_confirmation_bound_to_the_values(admin):
    import app.main as main_mod

    admin.login()
    admin.call(_add_logs, main_mod.app.state.db, "access_logs", [1, 12, 20])
    admin.call(_add_logs, main_mod.app.state.db, "activity_logs", [1, 12])

    res = admin.put({"baseVersion": 0, "accessLogRetentionDays": 10})
    assert res.status_code == 409
    body = res.json()
    assert body["code"] == "confirm_purge" and body["detail"]
    assert body["estimate"] == {"activityLogs": None, "accessLogs": 2}
    assert body["confirm"] == {"activityLogRetentionDays": 30, "accessLogRetentionDays": 10}
    assert admin.get().json()["version"] == 0

    # A confirmation of other values asks again.
    for wrong in ({"activityLogRetentionDays": 30, "accessLogRetentionDays": 11}, {"activityLogRetentionDays": 31, "accessLogRetentionDays": 10}):
        res = admin.put({"baseVersion": 0, "accessLogRetentionDays": 10, "confirmPurge": wrong})
        assert res.status_code == 409 and res.json()["code"] == "confirm_purge"
    # The same confirmation with a further change asks again, with both estimates.
    res = admin.put({"baseVersion": 0, "accessLogRetentionDays": 10, "activityLogRetentionDays": 5, "confirmPurge": body["confirm"]})
    assert res.status_code == 409
    assert res.json()["estimate"] == {"activityLogs": 1, "accessLogs": 2}
    assert res.json()["confirm"] == {"activityLogRetentionDays": 5, "accessLogRetentionDays": 10}

    res = admin.put({"baseVersion": 0, "accessLogRetentionDays": 10, "confirmPurge": body["confirm"]})
    assert res.status_code == 200 and res.json()["fields"]["accessLogRetentionDays"]["value"] == 10
    # A longer retention needs no confirmation.
    assert admin.put({"baseVersion": 1, "accessLogRetentionDays": 400}).status_code == 200


def test_secure_cookie_needs_a_confirmation_only_over_http(admin):
    admin.login()
    res = admin.put({"baseVersion": 0, "cookieSecure": True})
    assert res.status_code == 409
    assert res.json()["code"] == "confirm_cookie_secure"
    assert "COOKIE_SECURE=false" in res.json()["detail"]
    assert admin.put({"baseVersion": 0, "cookieSecure": True, "confirmCookieSecure": True}).status_code == 200
    assert admin.put({"baseVersion": 1, "cookieSecure": False}).status_code == 200
    # Over HTTPS (here through the trusted proxy's X-Forwarded-Proto) nothing is asked.
    res = admin.put({"baseVersion": 2, "cookieSecure": True}, https=True)
    assert res.status_code == 200 and res.json()["fields"]["cookieSecure"]["value"] is True


def test_issued_cookies_follow_the_setting(admin):
    from fastapi import Response

    from app.sessions import clear_sid_cookie, set_sid_cookie

    def issued(fn, *args):
        r = Response()
        fn(r, *args)
        return r.headers["set-cookie"]

    assert "secure" not in issued(set_sid_cookie, "x" * 64, 60).lower()
    admin.login()
    assert admin.put({"baseVersion": 0, "cookieSecure": True}, https=True).status_code == 200
    assert "secure" in issued(set_sid_cookie, "x" * 64, 60).lower()
    assert "secure" in issued(clear_sid_cookie).lower()
    # A real sign-in step issues it too: the first-admin registration, open again once
    # the signed-in admin is gone.
    import app.main as main_mod

    admin.call(main_mod.app.state.db.execute, "DELETE FROM admin_users")
    res = admin.client.post(
        "/admin/api/bootstrap/register",
        json={"username": "first", "password": secrets.token_urlsafe(16)},
        headers={"X-Forwarded-Proto": "https"},
    )
    assert res.status_code == 200, res.text
    assert "secure" in res.headers["set-cookie"].lower()


def test_activity_copy_failure_still_saves(admin, monkeypatch):
    gs = _gs()

    async def broken(*_a, **_k):
        raise RuntimeError("simulated")

    monkeypatch.setattr(gs, "log_change", broken)
    admin.login()
    res = admin.put({"baseVersion": 0, "activityLogRetentionDays": 40})
    assert res.status_code == 200 and res.json()["warning"] and res.json()["version"] == 1


def test_root_path_row_shows_the_environment(admin, monkeypatch):
    from app.config import Settings
    from app.routers import admin_settings

    admin.login()
    assert admin.get().json()["rootPath"] == {"envName": "ROOT_PATH", "value": None}
    monkeypatch.setenv("ROOT_PATH", "/auth")
    s = Settings(_env_file=None)
    monkeypatch.setattr(admin_settings, "get_settings", lambda: s)
    assert admin.get().json()["rootPath"] == {"envName": "ROOT_PATH", "value": "/auth"}


def test_startup_records_the_copy_in_the_activity_log(monkeypatch, tmp_path):
    import app.main as main_mod
    from app import db as db_mod

    use_env(monkeypatch, ACTIVITY_LOG_RETENTION_DAYS="21")
    monkeypatch.setattr(main_mod.redis, "from_url", lambda *a, **k: FakeRedis())
    url = f"sqlite:///{tmp_path}/start.db"
    create_database = db_mod.create_database
    monkeypatch.setattr(main_mod, "create_database", lambda _url: create_database(url))
    with TestClient(main_mod.app) as c:
        rows = c.portal.call(
            main_mod.app.state.db.fetch,
            "SELECT actor, details FROM activity_logs WHERE action = 'settings.update' AND entity_name = 'general'",
        )
        assert [r["actor"] for r in rows] == ["environment"]
        assert json.loads(rows[0]["details"])["to"]["activityLogRetentionDays"] == 21


# ── Usage ───────────────────────────────────────────────────────

def _usage(admin, boundary_days_ago=None, retention=30):
    import app.main as main_mod
    from app.logger import PURGED_BEFORE_KEY
    from app.usage_rollup import PurgeMark

    db = main_mod.app.state.db
    admin.call(db.execute, "DELETE FROM app_state WHERE key = $1", PURGED_BEFORE_KEY)
    if boundary_days_ago is not None:
        mark = PurgeMark(datetime.now(timezone.utc) - timedelta(days=boundary_days_ago))
        admin.call(db.execute, "INSERT INTO app_state (key, value) VALUES ($1, $2)", PURGED_BEFORE_KEY, mark.encode())
    gs = _gs()
    gs.apply(replace(gs.current(), access_log_retention_days=retention))
    return admin.client.get("/admin/api/usage/tokens", params={"range": "30d"}).json()


def test_usage_starts_at_the_purge_boundary_after_a_retention_increase(admin):
    import app.main as main_mod

    admin.login()
    db = main_mod.app.state.db
    tid = str(uuid.uuid4())
    admin.call(db.execute, "INSERT INTO tokens (id, name, token_hash) VALUES ($1::uuid, $2, $3)", tid, "alpha", secrets.token_hex(32))
    for age in (1, 8):
        at = (datetime.now(timezone.utc) - timedelta(days=age)).strftime("%Y-%m-%d %H:%M:%S")
        admin.call(
            db.execute,
            "INSERT INTO access_logs (id, token_id, token_name, area, level, result, created_at) VALUES ($1, $2, $3, $4, $5, $6, $7)",
            str(uuid.uuid4()), tid, "alpha", "orders", "read", True, at,
        )

    # Retention 30 days, nothing purged: every row counts.
    body = _usage(admin, boundary_days_ago=60)
    assert body["dataFromReason"] == "retention"
    assert next(t for t in body["tokens"] if t["name"] == "alpha")["calls"] == 2

    # Rows before 5 days ago were deleted under an earlier 5-day retention.
    body = _usage(admin, boundary_days_ago=5)
    assert body["dataFromReason"] == "purge" and body["partial"] is True
    assert body["retentionDays"] == 30
    assert next(t for t in body["tokens"] if t["name"] == "alpha")["calls"] == 1
    cover = [b["coverage"] for b in body["series"]]
    assert cover.count("partial") == 1 and cover[0] == "none" and cover[-1] == "full"
    assert body["dataFrom"] is not None

    detail = admin.client.get(f"/admin/api/usage/tokens/{tid}", params={"range": "30d"}).json()
    assert detail["dataFromReason"] == "purge" and detail["totals"]["calls"] == 1


def test_usage_reports_the_zone_it_was_computed_in(admin):
    gs = _gs()
    admin.login()
    gs.apply(replace(gs.current(), time_zone="Asia/Jerusalem"))
    body = admin.client.get("/admin/api/usage/tokens", params={"range": "7d"}).json()
    assert body["timeZone"] == "Asia/Jerusalem"
    # Buckets start at local midnight, and the labels carry the zone's offset.
    for b in body["series"]:
        assert b["start"][11:19] == "00:00:00" and b["start"][-6:] in ("+02:00", "+03:00")
    gs.apply(replace(gs.current(), time_zone="UTC"))
    assert admin.client.get("/admin/api/usage/tokens", params={"range": "7d"}).json()["timeZone"] == "UTC"

"""GET /admin/api/usage/tokens and /admin/api/usage/tokens/{id}: token usage statistics.

Rows are written to access_logs in the form the logger stores them (created_at text in
UTC on SQLite). The period and bucket helpers are also tested directly with a fixed
clock, for daylight saving days and a zone with a 45-minute offset.

Runs on a temporary SQLite database. With USAGE_TEST_DATABASE_URL set to a PostgreSQL
URL of a throwaway database named usage_test... (for example in a container with a
password generated for that run), the same tests run on PostgreSQL; they delete every
token and access log row in it. Every secret the app reads is generated per run with
``secrets``; Redis is an in-memory fake. Requires pytest and httpx.
"""
from __future__ import annotations

import os
import secrets
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402

_SECRET_FIELDS = ("admin_api_key", "session_secret", "app_encryption_key")
_PLACEHOLDERS = {
    "",
    "generate-a-long-random-string",
    "at-least-32-characters-long-secret-key",
    "min-32-chars-secret-change-me!!",
    "change-me-in-production",
    "0123456789abcdef0123456789abcdef",
} | {Settings.model_fields[f].default for f in _SECRET_FIELDS}

# Removed when the test process exits.
_TMP_DIR = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
_DATABASE_URL = os.environ.get("USAGE_TEST_DATABASE_URL") or f"sqlite:///{_TMP_DIR.name}/usage.db"
# Every test deletes all tokens and access log rows: only a database made for these tests.
if os.environ.get("USAGE_TEST_DATABASE_URL") and not urlsplit(_DATABASE_URL).path.lstrip("/").startswith("usage_test"):
    raise RuntimeError("USAGE_TEST_DATABASE_URL must name a throwaway database whose name starts with usage_test")
_SECRETS = {
    "ADMIN_API_KEY": secrets.token_urlsafe(32),
    "SESSION_SECRET": secrets.token_urlsafe(48),
    "APP_ENCRYPTION_KEY": secrets.token_hex(16),
}
for _name, _value in _SECRETS.items():
    if _value in _PLACEHOLDERS:
        raise RuntimeError(f"{_name} is empty or a placeholder")
os.environ.update(_SECRETS)
os.environ.setdefault("DATABASE_URL", _DATABASE_URL)
os.environ.setdefault("REDIS_URL", f"redis://:{secrets.token_hex(32)}@127.0.0.1:6379/0")


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, k):
        return self.data.get(k)

    async def setex(self, k, _ttl, v):
        self.data[k] = v

    async def delete(self, k):
        self.data.pop(k, None)

    async def ping(self):
        return True

    async def aclose(self):
        pass


class Env:
    def __init__(self, client, db, fake) -> None:
        self.c = client
        self.db = db
        self.fake = fake

    def run(self, fn, *args):
        return self.c.portal.call(fn, *args)

    def token(self, name: str, *, active: bool = True, expires_at: datetime | None = None) -> str:
        tid = str(uuid.uuid4())
        exp: object = expires_at
        if self.db.is_sqlite and expires_at is not None:
            exp = expires_at.strftime("%Y-%m-%d %H:%M:%S")
        self.run(
            self.db.execute,
            "INSERT INTO tokens (id, name, token_hash, grants, expires_at, is_active) VALUES ($1::uuid, $2, $3, $4, $5, $6)",
            tid, name, secrets.token_hex(32), "[]", exp, active,
        )
        return tid

    def log(self, token_id, token_name, area, level, result, *, ago=timedelta(seconds=5), ip="10.0.0.1", n=1, at=None, rid=None):
        at = at or datetime.now(timezone.utc) - ago
        stored: object = at.strftime("%Y-%m-%d %H:%M:%S") if self.db.is_sqlite else at
        for _ in range(n):
            self.run(
                self.db.execute,
                "INSERT INTO access_logs (id, token_id, token_name, area, level, result, ip_address, created_at) "
                "VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8::timestamptz)",
                rid or str(uuid.uuid4()), token_id, token_name, area, level, result, ip, stored,
            )

    def get(self, path: str, **params):
        return self.c.get(path, params=params)


@pytest.fixture()
def env(monkeypatch):
    from fastapi.testclient import TestClient

    import app.main as main_mod
    from app.config import get_settings
    from app.sessions import COOKIE_NAME, session_record

    s = get_settings()
    for field in _SECRET_FIELDS:
        if getattr(s, field) in _PLACEHOLDERS:
            raise RuntimeError(f"{field} is empty or a placeholder")
    monkeypatch.setenv("TZ", "UTC")
    create_database = main_mod.create_database
    monkeypatch.setattr(main_mod, "create_database", lambda _url: create_database(_DATABASE_URL))
    fake = FakeRedis()
    monkeypatch.setattr(main_mod.redis, "from_url", lambda *a, **k: fake)

    async def _no_cleanup(*_a, **_k):
        return None

    monkeypatch.setattr(main_mod, "cleanup_expired_tokens", _no_cleanup)
    with TestClient(main_mod.app) as c:
        db = main_mod.app.state.db
        e = Env(c, db, fake)
        e.run(db.execute, "DELETE FROM access_logs")
        e.run(db.execute, "DELETE FROM access_log_rollup")
        e.run(db.execute, "UPDATE access_log_rollup_state SET valid_from = NULL")
        e.run(db.execute, "DELETE FROM tokens")
        # A full session counts only for an existing admin with MFA enabled.
        admin_id = str(uuid.uuid4())
        e.run(
            db.execute,
            "INSERT INTO admin_users (id, username, password_hash, totp_enabled) VALUES ($1, $2, $3, $4)",
            admin_id, "usage-admin-" + secrets.token_hex(4), "unused", True,
        )
        sid = secrets.token_hex(32)
        key, value = session_record(sid, {"kind": "full", "adminId": admin_id, "csrf": secrets.token_hex(24)})
        fake.data[key] = value
        c.cookies.set(COOKIE_NAME, sid)
        try:
            yield e
        finally:
            e.run(db.execute, "DELETE FROM access_logs")
            e.run(db.execute, "DELETE FROM access_log_rollup")
            e.run(db.execute, "UPDATE access_log_rollup_state SET valid_from = NULL")
            e.run(db.execute, "DELETE FROM tokens")
            e.run(db.execute, "DELETE FROM admin_users WHERE id = $1", admin_id)


def _row(body, name):
    return next(t for t in body["tokens"] if t["name"] == name)


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


def test_requires_a_full_admin_session(env):
    from app.sessions import COOKIE_NAME, session_record

    paths = ["/admin/api/usage/tokens", f"/admin/api/usage/tokens/{uuid.uuid4()}", "/admin/api/usage/tokens/unknown"]
    env.c.cookies.clear()
    for p in paths:
        assert env.get(p).status_code == 401
    # Signed in with the password, MFA not done yet.
    sid = secrets.token_hex(32)
    key, value = session_record(sid, {"kind": "partial", "adminId": str(uuid.uuid4()), "csrf": secrets.token_hex(24)})
    env.fake.data[key] = value
    env.c.cookies.set(COOKIE_NAME, sid)
    for p in paths:
        assert env.get(p).status_code == 401
    # The master API key is not an admin session.
    env.c.cookies.clear()
    for p in paths:
        assert env.c.get(p, headers={"Authorization": "Bearer " + _SECRETS["ADMIN_API_KEY"]}).status_code == 401


def test_rejects_an_unknown_range(env):
    assert env.get("/admin/api/usage/tokens", range="90d").status_code == 422
    assert env.get("/admin/api/usage/tokens/unknown", range="1h").status_code == 422


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


def test_overview_counts_calls_areas_and_groups(env):
    a = env.token("alpha")
    env.token("idle")
    c = env.token("grants-only")
    gone = str(uuid.uuid4())

    env.log(a, "alpha", "orders", "read", True, n=3)
    env.log(a, "alpha", "Billing", "write", False, ip="10.0.0.2")
    env.log(a, "alpha", " billing\t", "read", True)
    env.log(a, "alpha", "*", "grants", True, n=2)
    env.log(c, "grants-only", "*", "grants", True)
    env.log(None, None, "orders", "read", False, n=2, ip="192.0.2.9")
    env.log(gone, "old-name", "orders", "read", True, ago=timedelta(minutes=10))
    env.log(gone, "new-name", "orders", "read", True, ago=timedelta(minutes=1))

    res = env.get("/admin/api/usage/tokens", range="7d")
    assert res.status_code == 200
    body = res.json()

    alpha = _row(body, "alpha")
    assert (alpha["calls"], alpha["allowed"], alpha["denied"], alpha["grantsChecks"]) == (5, 4, 1, 2)
    assert alpha["status"] == "active"
    assert alpha["ipCount"] == 2
    assert alpha["areasCount"] == 2
    assert alpha["topAreas"] == [
        {"area": "orders", "truncated": False, "calls": 3},
        {"area": "Billing", "truncated": False, "calls": 2},
    ]
    assert alpha["lastCallAt"]

    idle = _row(body, "idle")
    assert (idle["calls"], idle["areasCount"], idle["topAreas"], idle["lastCallAt"]) == (0, 0, [], None)

    only = _row(body, "grants-only")
    assert (only["calls"], only["grantsChecks"], only["topAreas"], only["ipCount"]) == (0, 1, [], 0)

    deleted = _row(body, "new-name")
    assert deleted["status"] == "deleted" and deleted["tokenId"] == gone and deleted["calls"] == 2
    assert not any(t["name"] == "old-name" for t in body["tokens"])

    unknown = _row(body, "Unknown token")
    assert unknown["tokenId"] is None and unknown["status"] == "unknown"
    assert (unknown["calls"], unknown["denied"]) == (2, 2)

    t = body["totals"]
    assert (t["calls"], t["allowed"], t["denied"], t["grantsChecks"]) == (9, 6, 3, 3)
    assert (t["unknownCalls"], t["tokensUsed"], t["tokensTotal"]) == (2, 1, 3)
    assert body["areasIncomplete"] is False

    series = body["series"]
    assert len(series) == 7
    assert sum(b["allowed"] for b in series) == t["allowed"]
    assert sum(b["denied"] for b in series) == t["denied"]
    assert all(b["coverage"] == "full" for b in series)
    assert body["partial"] is False


def test_hourly_and_monthly_periods(env):
    a = env.token("alpha")
    env.log(a, "alpha", "orders", "read", True)
    env.log(a, "alpha", "orders", "read", True, ago=timedelta(days=8))
    env.log(a, "alpha", "orders", "read", True, ago=timedelta(days=40))

    day = env.get("/admin/api/usage/tokens", range="24h").json()
    assert len(day["series"]) == 24
    assert _row(day, "alpha")["calls"] == 1

    week = env.get("/admin/api/usage/tokens", range="7d").json()
    assert _row(week, "alpha")["calls"] == 1

    month = env.get("/admin/api/usage/tokens", range="30d").json()
    assert len(month["series"]) == 30
    assert _row(month, "alpha")["calls"] == 2
    assert sum(b["allowed"] for b in month["series"]) == 2


def test_rows_older_than_retention_are_not_counted(env, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "access_log_retention_days", 2)
    a = env.token("alpha")
    env.log(a, "alpha", "orders", "read", True)
    # Not purged yet, but older than the retention period.
    env.log(a, "alpha", "orders", "read", True, ago=timedelta(days=3))

    body = env.get("/admin/api/usage/tokens", range="7d").json()
    assert body["partial"] is True and body["retentionDays"] == 2
    assert _row(body, "alpha")["calls"] == 1
    assert sum(b["allowed"] for b in body["series"]) == 1
    cover = [b["coverage"] for b in body["series"]]
    assert cover[0] == "none" and cover[-1] == "full"
    assert cover.count("partial") == 1
    assert cover.index("partial") > cover.index("none")

    detail = env.get(f"/admin/api/usage/tokens/{a}", range="7d").json()
    assert detail["totals"]["calls"] == 1


def test_expired_and_disabled_status(env):
    env.token("old", expires_at=datetime.now(timezone.utc) - timedelta(hours=1))
    env.token("off", active=False)
    body = env.get("/admin/api/usage/tokens").json()
    assert _row(body, "old")["status"] == "expired"
    assert _row(body, "off")["status"] == "disabled"


def test_long_area_names_are_cut_after_grouping(env):
    a = env.token("alpha")
    long_area = "x" * 300
    env.log(a, "alpha", long_area, "read", True)
    env.log(a, "alpha", long_area + "y", "read", True)
    top = _row(env.get("/admin/api/usage/tokens").json(), "alpha")["topAreas"]
    assert len(top) == 2
    assert all(t["truncated"] and len(t["area"]) == 120 for t in top)


def test_too_many_area_groups_marks_areas_incomplete(env, monkeypatch):
    import app.routers.admin_usage as usage

    monkeypatch.setattr(usage, "MAX_AREA_GROUPS", 2)
    a = env.token("alpha")
    for area in ("a1", "a2", "a3"):
        env.log(a, "alpha", area, "read", True)
    body = env.get("/admin/api/usage/tokens").json()
    assert body["areasIncomplete"] is True
    alpha = _row(body, "alpha")
    assert alpha["topAreas"] is None and alpha["areasCount"] is None
    assert alpha["calls"] == 3

    detail = env.get(f"/admin/api/usage/tokens/{a}").json()
    assert detail["areasIncomplete"] is True and detail["areas"] is None
    assert detail["totals"]["calls"] == 3


# ---------------------------------------------------------------------------
# One token
# ---------------------------------------------------------------------------


def test_token_detail(env):
    a = env.token("alpha")
    env.log(a, "alpha", "orders", "read", True, n=2)
    env.log(a, "alpha", "Orders", "write", False, ip="10.0.0.2")
    env.log(a, "alpha", "billing", "delete", True, ip="10.0.0.2")
    env.log(a, "alpha", "*", "grants", True)

    res = env.get(f"/admin/api/usage/tokens/{a}", range="24h")
    assert res.status_code == 200
    body = res.json()
    assert body["token"] == {"id": a, "name": "alpha", "status": "active"}
    assert body["totals"]["calls"] == 4 and body["totals"]["grantsChecks"] == 1 and body["totals"]["ipCount"] == 2
    assert body["areasCount"] == 2
    orders, billing = body["areas"]
    assert orders == {
        "area": "orders", "truncated": False, "calls": 3, "allowed": 2, "denied": 1,
        "levels": {"read": {"allowed": 2, "denied": 0}, "write": {"allowed": 0, "denied": 1}},
    }
    assert billing["area"] == "billing" and billing["levels"] == {"delete": {"allowed": 1, "denied": 0}}
    assert [x["address"] for x in body["addresses"]] == ["10.0.0.1", "10.0.0.2"]
    assert body["addresses"][1] == {
        "address": "10.0.0.2", "truncated": False, "calls": 2, "denied": 1, "lastCallAt": body["addresses"][1]["lastCallAt"],
    }
    assert len(body["series"]) == 24
    assert sum(b["allowed"] + b["denied"] for b in body["series"]) == 4


def test_unknown_and_deleted_detail(env):
    gone = str(uuid.uuid4())
    env.log(None, None, "orders", "read", False, n=2)
    env.log(gone, "removed", "orders", "read", True)

    unknown = env.get("/admin/api/usage/tokens/unknown").json()
    assert unknown["token"] == {"id": None, "name": "Unknown token", "status": "unknown"}
    assert unknown["totals"]["denied"] == 2

    deleted = env.get(f"/admin/api/usage/tokens/{gone}").json()
    assert deleted["token"] == {"id": gone, "name": "removed", "status": "deleted"}
    assert deleted["totals"]["calls"] == 1


def test_detail_not_found(env):
    assert env.get(f"/admin/api/usage/tokens/{uuid.uuid4()}").status_code == 404
    assert env.get("/admin/api/usage/tokens/not-a-token").status_code == 404
    a = env.token("alpha")
    assert env.get(f"/admin/api/usage/tokens/{a.upper()}").status_code == 404
    # A token with no calls still has a page.
    assert env.get(f"/admin/api/usage/tokens/{a}").status_code == 200


# ---------------------------------------------------------------------------
# Periods and buckets (no database)
# ---------------------------------------------------------------------------


def _hours(b):
    return (b[1] - b[0]).total_seconds() / 3600


def test_days_follow_daylight_saving_in_jerusalem():
    from app.routers.admin_usage import _buckets

    tz = ZoneInfo("Asia/Jerusalem")
    # Clocks go forward on Friday 27 March 2026 and back on Sunday 25 October 2026.
    spring = _buckets("7d", datetime(2026, 3, 29, 12, tzinfo=timezone.utc), tz)
    autumn = _buckets("30d", datetime(2026, 10, 30, 12, tzinfo=timezone.utc), tz)
    assert [_hours(b) for b in spring].count(23) == 1
    assert [_hours(b) for b in autumn].count(25) == 1
    for b in spring + autumn:
        local = b[0].astimezone(tz)
        assert (local.hour, local.minute) == (0, 0)
    # Buckets are contiguous.
    assert all(x[1] == y[0] for x, y in zip(autumn, autumn[1:]))


def test_slots_fall_in_exact_local_days_in_kathmandu():
    from app.routers.admin_usage import _Period

    tz = ZoneInfo("Asia/Kathmandu")  # UTC+05:45
    now = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
    p = _Period("7d", now=now, tz=tz, retention_days=30)
    midnight = datetime(2026, 11, 13, tzinfo=tz).astimezone(timezone.utc)
    assert (midnight.hour, midnight.minute) == (18, 15)
    slots = [
        # The last slot of 12 November local time, and the first of 13 November.
        {"slot": (midnight - timedelta(minutes=15)).timestamp(), "calls": 3, "allowed": 1},
        {"slot": midnight.timestamp(), "calls": 5, "allowed": 5},
    ]
    series = p.series(slots)
    assert (series[-2]["allowed"], series[-2]["denied"]) == (1, 2)
    assert (series[-1]["allowed"], series[-1]["denied"]) == (5, 0)


def test_hour_buckets_in_a_half_hour_zone():
    from app.routers.admin_usage import _buckets

    tz = ZoneInfo("Asia/Kolkata")  # UTC+05:30
    hours = _buckets("24h", datetime(2026, 11, 13, 12, 40, tzinfo=timezone.utc), tz)
    assert len(hours) == 24
    assert all(_hours(b) == 1 for b in hours)
    assert all(b[0].astimezone(tz).minute == 0 for b in hours)
    assert hours[-1][0] <= datetime(2026, 11, 13, 12, 40, tzinfo=timezone.utc) < hours[-1][1]


def test_hours_follow_a_half_hour_clock_change():
    from app.routers.admin_usage import _buckets

    tz = ZoneInfo("Australia/Lord_Howe")  # 02:00 becomes 02:30 on 4 October 2026
    now = datetime(2026, 10, 4, 10, 10, tzinfo=tz).astimezone(timezone.utc)
    hours = _buckets("24h", now, tz)
    assert len(hours) == 24
    assert all(x[1] == y[0] for x, y in zip(hours, hours[1:]))
    # 02:00 to 02:30 does not exist that night: hour 02 starts at 02:30 and lasts 30 minutes
    starts = [(b[0].astimezone(tz).strftime("%H:%M"), _hours(b)) for b in hours]
    assert ("02:30", 0.5) in starts
    assert all(m == "00" for (hm, _h) in starts if hm != "02:30" for m in [hm[3:]])
    assert sorted(h for _hm, h in starts) == [0.5] + [1.0] * 23
    assert hours[-1][0] <= now < hours[-1][1]


def test_coverage_marks_the_retention_edge():
    from app.routers.admin_usage import _Period

    tz = ZoneInfo("Asia/Jerusalem")
    now = datetime(2026, 11, 13, 10, 30, tzinfo=timezone.utc)
    p = _Period("7d", now=now, tz=tz, retention_days=1)
    cover = [p.coverage(s, e) for s, e in p.buckets]
    assert cover == ["none"] * 5 + ["partial", "full"]
    assert p.start == now - timedelta(days=1)


# ---------------------------------------------------------------------------
# Summary of the access log (app.usage_rollup)
# ---------------------------------------------------------------------------


def _report(env, now, tz):
    """Every figure of both endpoints at one moment: the overview and each listed token's page, per range."""
    from app.db import report_database
    from app.routers import admin_usage as u

    async def run():
        out = {}
        async with report_database(env.db) as db:
            for rng in ("24h", "7d", "30d"):
                p = u._Period(rng, now=now, tz=tz)
                overview = await u._overview(db, p)
                out[(rng, "overview")] = overview
                for t in overview["tokens"]:
                    if t["calls"] or t["grantsChecks"]:
                        out[(rng, t["tokenId"])] = await u._token_usage(db, p, t["tokenId"])
        return out

    return env.run(run)


def _raw_report(env, now, tz):
    """_report with nothing summarised: every row is read from access_logs."""
    env.run(env.db.execute, "DELETE FROM access_log_rollup")
    env.run(env.db.execute, "UPDATE access_logs SET rolled = $1", False)
    return _report(env, now, tz)


def _roll(env, now, **kw):
    from app import usage_rollup
    from app.config import get_settings

    async def run():
        return await usage_rollup.roll_up(env.db, get_settings().access_log_retention_days, now, pause=0, **kw)

    return env.run(run)


def _scalar(env, sql):
    return env.run(env.db.fetchval, sql)


def _consistent(env):
    """From the summary's valid_from on, every marked row is counted by exactly one summary call."""
    from app import usage_rollup

    since = env.run(usage_rollup.summary_valid_from, env.db) or datetime(1970, 1, 1, tzinfo=timezone.utc)
    at = usage_rollup.bind_time(env.db, since)
    marked = env.run(
        env.db.fetchval,
        f"SELECT COUNT(*) FROM access_logs WHERE NOT ({usage_rollup.unrolled(env.db)}) AND created_at >= $1::timestamptz",
        at,
    )
    counted = env.run(env.db.fetchval, "SELECT COALESCE(SUM(calls), 0) FROM access_log_rollup WHERE slot >= $1::timestamptz", at)
    assert int(counted) == int(marked)


_LOW_ID = "00000000-0000-4000-8000-000000000001"
_HIGH_ID = "ffffffff-ffff-4fff-bfff-ffffffffffff"


def _fill(env, now):
    """Rows of every kind across 40 days before now, a few after now, with repeated groups."""
    a = env.token("alpha")
    b = env.token("beta")
    gone = str(uuid.uuid4())
    start = now - timedelta(days=40)
    areas = ["orders", "Orders", " billing\t", "Billing", "x" * 300, "*"]
    levels = ["read", "write", "delete", "all"]
    t = start
    i = 0
    while t < now:
        tok, name = (a, "alpha") if i % 3 else (b, "beta")
        level = "grants" if i % 11 == 0 else levels[i % 4]
        area = "*" if level == "grants" else areas[i % 5]
        ip = None if i % 13 == 0 else f"10.0.0.{i % 3}"
        env.log(tok, name, area, level, i % 4 != 1, ip=ip, at=t, n=1 + i % 3)
        if i % 7 == 0:
            env.log(None, None, "orders", "read", False, ip="192.0.2.9", at=t)
        t += timedelta(hours=5, minutes=17, seconds=31)
        i += 1
    cutoff = now - timedelta(days=30)
    # Around the retention cutoff and the ends of the period.
    for at in (cutoff - timedelta(minutes=1), cutoff + timedelta(minutes=1), cutoff + timedelta(minutes=16)):
        env.log(a, "alpha", "edge", "read", True, at=at)
    last_slot = datetime.fromtimestamp(now.timestamp() // 900 * 900, timezone.utc)
    for at in (last_slot - timedelta(seconds=1), last_slot, now - timedelta(seconds=1), now, now + timedelta(minutes=5)):
        env.log(a, "alpha", "edge", "write", True, at=at)
    # A deleted token: two names at the same moment in different groups (the greater id wins),
    # then a later row without a name in the first one's group, which does not count for the name.
    same = (now - timedelta(days=2)).replace(microsecond=0)
    assert (same.minute % 15) < 10
    env.log(gone, "low-id-name", "orders", "read", True, at=same, rid=_LOW_ID)
    env.log(gone, "high-id-name", "billing", "write", False, at=same, rid=_HIGH_ID)
    env.log(gone, None, "orders", "read", True, at=same + timedelta(minutes=5))
    env.log(gone, "older-name", "orders", "read", True, at=same - timedelta(hours=3), n=4)
    return a, gone


_NOW = datetime(2026, 10, 27, 9, 7, 13, tzinfo=timezone.utc)


@pytest.mark.parametrize("tz_name", ["Asia/Jerusalem", "Asia/Kathmandu"])
@pytest.mark.parametrize("chunk_rows", [5000, 7])
def test_summary_gives_the_same_figures(env, tz_name, chunk_rows):
    # Jerusalem moved its clocks back on 25 October 2026; Kathmandu is UTC+05:45.
    tz = ZoneInfo(tz_name)
    _a, gone = _fill(env, _NOW)
    before = _report(env, _NOW, tz)
    assert _report(env, _NOW, tz) == before

    _roll(env, _NOW, chunk_rows=chunk_rows)
    _consistent(env)
    assert int(_scalar(env, "SELECT MAX(calls) FROM access_log_rollup")) > 1
    assert _report(env, _NOW, tz) == before
    assert before[("30d", gone)]["token"]["name"] == "high-id-name"

    # Summarised by a clock two hours ahead: rows after now are marked, still not counted,
    # and the rows of the last partial slot are read from access_logs.
    _roll(env, _NOW + timedelta(hours=2), chunk_rows=chunk_rows)
    _consistent(env)
    assert _report(env, _NOW, tz) == before
    assert _raw_report(env, _NOW, tz) == before


def test_summary_keeps_the_area_group_limit(env, monkeypatch):
    import app.routers.admin_usage as usage

    tz = ZoneInfo("UTC")
    a = env.token("alpha")
    at = _NOW - timedelta(days=1)
    for area in ("a1", "a2", "a3"):
        env.log(a, "alpha", area, "read", True, at=at, n=2)
    env.log(a, "alpha", "a1", "write", False, at=at)
    _roll(env, _NOW)
    for cap in (2, 3, 4):
        monkeypatch.setattr(usage, "MAX_AREA_GROUPS", cap)
        summarised = _report(env, _NOW, tz)
        raw = _raw_report(env, _NOW, tz)
        _roll(env, _NOW)
        assert summarised == raw
    # Three (token, area) groups, four (area, level) groups.
    monkeypatch.setattr(usage, "MAX_AREA_GROUPS", 3)
    r = _report(env, _NOW, tz)
    assert r[("7d", "overview")]["areasIncomplete"] is False
    assert r[("7d", a)]["areasIncomplete"] is True


def test_rows_written_after_their_slot_was_summarised(env):
    tz = ZoneInfo("UTC")
    a = env.token("alpha")
    at = _NOW - timedelta(hours=3)
    env.log(a, "alpha", "orders", "read", True, at=at, n=3)
    _roll(env, _NOW)
    # Late rows in a summarised slot, and one older than every summarised row.
    env.log(a, "alpha", "orders", "read", False, at=at, n=2)
    env.log(a, "alpha", "orders", "read", True, at=_NOW - timedelta(days=3))
    mixed = _report(env, _NOW, tz)
    assert mixed[("24h", "overview")]["totals"]["calls"] == 5
    assert mixed[("7d", "overview")]["totals"]["calls"] == 6
    _roll(env, _NOW)
    _consistent(env)
    assert _report(env, _NOW, tz) == mixed
    assert _raw_report(env, _NOW, tz) == mixed


def test_concurrent_and_cancelled_summaries(env):
    import asyncio

    from app import usage_rollup
    from app.config import get_settings

    tz = ZoneInfo("UTC")
    _fill(env, _NOW)
    before = _report(env, _NOW, tz)
    days = get_settings().access_log_retention_days

    async def cancelled():
        task = asyncio.create_task(usage_rollup.roll_up(env.db, days, _NOW, chunk_rows=3, pause=0.01))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    env.run(cancelled)
    _consistent(env)
    assert _report(env, _NOW, tz) == before

    async def twice():
        return await asyncio.gather(
            usage_rollup.roll_up(env.db, days, _NOW, chunk_rows=5, pause=0),
            usage_rollup.roll_up(env.db, days, _NOW, chunk_rows=5, pause=0),
        )

    env.run(twice)
    _consistent(env)
    left = env.run(
        env.db.fetchval,
        f"SELECT COUNT(*) FROM access_logs WHERE {usage_rollup.unrolled(env.db)} "
        "AND created_at >= $1::timestamptz AND created_at < $2::timestamptz",
        usage_rollup.bind_time(env.db, _NOW - timedelta(days=29)),
        usage_rollup.bind_time(env.db, _NOW - timedelta(hours=1)),
    )
    assert left == 0
    assert _report(env, _NOW, tz) == before


def test_purge_drops_the_summary_of_deleted_rows(env, monkeypatch):
    from app.config import get_settings
    from app.logger import purge_old_logs
    from app.usage_rollup import floor_slot

    import app.logger as logger_mod
    from app import usage_rollup
    from app.usage_rollup import bind_time

    tz = ZoneInfo("UTC")
    a = env.token("alpha")
    # The purge's clock, 7 minutes into a slot: the cutoff splits a slot that has rows on both sides.
    now = floor_slot(datetime.now(timezone.utc)) - timedelta(minutes=8)
    cutoff = now - timedelta(days=2)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(logger_mod, "datetime", Clock)
    t = now - timedelta(days=4)
    while t < now:
        env.log(a, "alpha", "orders", "read", True, at=t, n=2)
        t += timedelta(minutes=7)
    t = floor_slot(cutoff)
    while t < floor_slot(cutoff) + timedelta(minutes=15):
        env.log(a, "alpha", "billing", "write", False, at=t)
        t += timedelta(seconds=40)
    _roll(env, now)
    _consistent(env)

    monkeypatch.setattr(get_settings(), "access_log_retention_days", 2)
    env.run(purge_old_logs, env.db, 30, 2)
    _consistent(env)
    kept = _scalar(env, "SELECT MIN(slot) FROM access_log_rollup")
    kept_at = kept if isinstance(kept, datetime) else datetime.strptime(kept, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    assert kept_at == floor_slot(cutoff) + timedelta(minutes=15)
    # The summary of the cutoff's slot is no longer read; its rows after the cutoff are kept.
    assert env.run(usage_rollup.summary_valid_from, env.db) == floor_slot(cutoff) + timedelta(minutes=15)
    left = env.run(
        env.db.fetchval,
        "SELECT COUNT(*) FROM access_logs WHERE "
        "created_at >= $1::timestamptz AND created_at < $2::timestamptz",
        bind_time(env.db, cutoff), bind_time(env.db, floor_slot(cutoff) + timedelta(minutes=15)),
    )
    assert left >= 10
    # A purge with a longer retention (or an earlier clock) never moves valid_from back.
    env.run(purge_old_logs, env.db, 30, 5)
    assert env.run(usage_rollup.summary_valid_from, env.db) == floor_slot(cutoff) + timedelta(minutes=15)
    # Nor does deleting a row written late with an old time (a clock that was behind).
    env.log(a, "alpha", "orders", "read", True, at=cutoff - timedelta(hours=5))
    env.run(purge_old_logs, env.db, 30, 2)
    assert env.run(usage_rollup.summary_valid_from, env.db) == floor_slot(cutoff) + timedelta(minutes=15)
    monkeypatch.setattr(get_settings(), "access_log_retention_days", 5)
    assert _report(env, now, tz) == _raw_report(env, now, tz)
    _roll(env, now)

    # A longer retention after the purge shows exactly the rows that are left.
    for days in (2, 5):
        monkeypatch.setattr(get_settings(), "access_log_retention_days", days)
        later = datetime.now(timezone.utc)
        summarised = _report(env, later, tz)
        assert summarised == _raw_report(env, later, tz)
        _roll(env, later)
        _consistent(env)


@pytest.mark.parametrize("stop_after", [1, 2, None])
def test_purge_in_chunks_with_a_stale_summary_run(env, monkeypatch, stop_after):
    """Between the purge's transactions a summary run with an older clock (a run that started
    before the cutoff moved) summarises rows the purge is about to delete; the purge may also
    stop after any transaction. Every committed state keeps the summary and the rows in step."""
    import asyncio

    from app import usage_rollup
    from app.config import get_settings

    tz = ZoneInfo("UTC")
    a = env.token("alpha")
    now = usage_rollup.floor_slot(datetime.now(timezone.utc)) - timedelta(minutes=8)
    t = now - timedelta(days=3)
    while t < now:
        env.log(a, "alpha", "orders", "read", True, at=t, n=2)
        t += timedelta(minutes=9)
    # A busy cutoff slot: more rows on each side of the cutoff than one purge transaction takes.
    t = usage_rollup.floor_slot(now - timedelta(days=2))
    while t < usage_rollup.floor_slot(now - timedelta(days=2)) + timedelta(minutes=15):
        env.log(a, "alpha", "busy", "read", True, at=t)
        t += timedelta(seconds=6)
    _roll(env, now - timedelta(days=1))
    calls = {"sleeps": 0}

    async def between(_s):
        calls["sleeps"] += 1
        await usage_rollup.roll_up(env.db, 2, now - timedelta(minutes=40), pause=0)
        if stop_after is not None and calls["sleeps"] >= stop_after:
            raise asyncio.CancelledError

    pause = usage_rollup._pause
    monkeypatch.setattr(usage_rollup, "_pause", between)

    async def purge():
        try:
            await usage_rollup.purge_access_logs(env.db, now - timedelta(days=2), chunk_rows=40)
        except asyncio.CancelledError:
            pass

    env.run(purge)
    monkeypatch.setattr(usage_rollup, "_pause", pause)
    assert calls["sleeps"] >= (stop_after or 2)
    _consistent(env)
    for days in (2, 5):
        monkeypatch.setattr(get_settings(), "access_log_retention_days", days)
        assert _report(env, now, tz) == _raw_report(env, now, tz)
        _roll(env, now - timedelta(days=1))
        _consistent(env)


def test_a_stopped_purge_keeps_the_summary_in_use(env, monkeypatch):
    """A purge to a shorter retention that stops early, followed by a longer retention again,
    leaves the summary in use for every slot whose rows were not deleted."""
    import asyncio

    from app import usage_rollup
    from app.config import get_settings

    tz = ZoneInfo("UTC")
    a = env.token("alpha")
    now = usage_rollup.floor_slot(datetime.now(timezone.utc)) - timedelta(minutes=8)
    t = now - timedelta(days=4)
    while t < now:
        env.log(a, "alpha", "orders", "read", True, at=t)
        t += timedelta(minutes=10)
    _roll(env, now)

    async def stop(_s):
        raise asyncio.CancelledError

    monkeypatch.setattr(usage_rollup, "_pause", stop)

    async def purge():
        with pytest.raises(asyncio.CancelledError):
            await usage_rollup.purge_access_logs(env.db, now - timedelta(days=1), chunk_rows=30)

    env.run(purge)
    # 30 rows, one every 10 minutes, are gone: valid_from ends after the slot of the last one.
    first = usage_rollup.floor_slot(now - timedelta(days=4))
    valid_from = env.run(usage_rollup.summary_valid_from, env.db)
    assert first + timedelta(hours=4) <= valid_from <= first + timedelta(hours=5, minutes=15)
    _consistent(env)
    monkeypatch.setattr(get_settings(), "access_log_retention_days", 5)
    summarised = _report(env, now, tz)
    assert summarised == _raw_report(env, now, tz)
    assert summarised[("7d", "overview")]["totals"]["calls"] == 4 * 144 - 30


def test_periods_without_a_whole_slot(env, monkeypatch):
    from app.config import get_settings

    tz = ZoneInfo("UTC")
    a = env.token("alpha")
    for minutes in (1, 20, 200):
        env.log(a, "alpha", "orders", "read", True, at=_NOW - timedelta(minutes=minutes))
    env.log(a, "alpha", "orders", "read", True, at=_NOW)
    _roll(env, _NOW)
    for days in (0, 1):
        monkeypatch.setattr(get_settings(), "access_log_retention_days", days)
        assert _report(env, _NOW, tz) == _raw_report(env, _NOW, tz)
        _roll(env, _NOW)
    monkeypatch.setattr(get_settings(), "access_log_retention_days", 0)
    r = _report(env, _NOW, tz)
    assert r[("24h", "overview")]["totals"]["calls"] == 1
    res = env.get("/admin/api/usage/tokens", range="24h")
    assert res.status_code == 200


def test_shutdown_stops_the_first_cleanup(monkeypatch):
    import asyncio

    from fastapi.testclient import TestClient

    import app.main as main_mod

    monkeypatch.setenv("TZ", "UTC")
    create_database = main_mod.create_database
    monkeypatch.setattr(main_mod, "create_database", lambda _url: create_database(_DATABASE_URL))
    monkeypatch.setattr(main_mod.redis, "from_url", lambda *a, **k: FakeRedis())
    events: list[str] = []

    async def slow_cleanup(_db):
        events.append("started")
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            events.append("cancelled")
            raise

    monkeypatch.setattr(main_mod, "cleanup_expired_tokens", slow_cleanup)
    with TestClient(main_mod.app) as c:
        db = main_mod.app.state.db
        close = db.close

        async def closing():
            events.append("closed")
            await close()

        monkeypatch.setattr(db, "close", closing)
        for _ in range(100):
            if events:
                break
            c.portal.call(asyncio.sleep, 0.01)
        assert events == ["started"]
    assert events == ["started", "cancelled", "closed"]

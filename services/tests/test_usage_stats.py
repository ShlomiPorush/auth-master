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

    def log(self, token_id, token_name, area, level, result, *, ago=timedelta(seconds=5), ip="10.0.0.1", n=1):
        at = datetime.now(timezone.utc) - ago
        stored: object = at.strftime("%Y-%m-%d %H:%M:%S") if self.db.is_sqlite else at
        for _ in range(n):
            self.run(
                self.db.execute,
                "INSERT INTO access_logs (id, token_id, token_name, area, level, result, ip_address, created_at) "
                "VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8::timestamptz)",
                str(uuid.uuid4()), token_id, token_name, area, level, result, ip, stored,
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
        e.run(db.execute, "DELETE FROM tokens")
        sid = secrets.token_hex(32)
        key, value = session_record(sid, {"kind": "full", "adminId": str(uuid.uuid4()), "csrf": secrets.token_hex(24)})
        fake.data[key] = value
        c.cookies.set(COOKIE_NAME, sid)
        try:
            yield e
        finally:
            e.run(db.execute, "DELETE FROM access_logs")
            e.run(db.execute, "DELETE FROM tokens")


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

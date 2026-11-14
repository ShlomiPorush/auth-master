"""Requests rejected by the /validate rate limit are reported, not only answered.

- The limiter logs a summary line at most once per window per process, and the
  rejections at the end of a burst are reported once any later request arrives.
- Memory for addresses stays bounded; rejections beyond it are counted, not named.
- GET /admin/api/rate-limit reports totals to a fully signed-in admin only.
- Rejected requests are answered 429 with Retry-After and write no access-log row.

Limits are fixed when app.rate_limit is imported, and another test module may have
imported it first with other values, so the tests set the limits with
rate_limit.reset_state(Limits(...)) and pass the clock in.  Every secret the app needs is generated per
run with ``secrets``; Redis is faked.  Requires pytest and httpx.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

_TMP_DIR = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)

# Used only when this module is the first to load the settings.
os.environ.setdefault("ADMIN_API_KEY", secrets.token_urlsafe(32))
os.environ.setdefault("SESSION_SECRET", secrets.token_urlsafe(48))
os.environ.setdefault("APP_ENCRYPTION_KEY", secrets.token_hex(16))
os.environ.setdefault("REDIS_URL", f"redis://:{secrets.token_hex(32)}@127.0.0.1:6379/0")
# The HTTP tests write only here: the client fixture makes the app open this file,
# whatever database the cached settings name.
_DATABASE_URL = f"sqlite:///{_TMP_DIR.name}/rl.db"
os.environ.setdefault("DATABASE_URL", _DATABASE_URL)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

LOGGER = "app.rate_limit"
T0 = 1_800_000_000_000.0  # a fixed clock, in ms


def _app():
    """Imported on first use, not at collection: whichever test module imports the
    app first fixes its settings, and other modules set theirs before importing."""
    import app.main as main_mod
    from app import rate_limit
    from app.sessions import COOKIE_NAME, PREFIX

    return main_mod, rate_limit, COOKIE_NAME, PREFIX


class _Clock:
    """Stands in for the time module in app.rate_limit: wall and monotonic, in ms."""

    def __init__(self) -> None:
        self.wall = T0
        self.mono = 5_000.0

    def time(self) -> float:
        return self.wall / 1000

    def monotonic(self) -> float:
        return self.mono / 1000


class _Limiter:
    """The rate_limit module, with allow_validate called at a given time.

    `t` sets both clocks to T0 + offset; `wall_shift` moves only the wall clock,
    as an NTP correction or a manual clock change would."""

    def __init__(self, module, clock: _Clock) -> None:
        self._m = module
        self._clock = clock
        self.wall_shift = 0.0

    def __getattr__(self, name):
        return getattr(self._m, name)

    def allow_validate(self, ip: str, t: float) -> bool:
        self._clock.mono = 5_000.0 + (t - T0)
        self._clock.wall = t + self.wall_shift
        return self._m.allow_validate(ip)


@pytest.fixture()
def limiter(monkeypatch):
    _, rate_limit, _, _ = _app()
    clock = _Clock()
    monkeypatch.setattr(rate_limit, "time", clock)
    rate_limit.reset_state(rate_limit.Limits(0, 500, 60_000))
    yield _Limiter(rate_limit, clock)
    monkeypatch.undo()
    rate_limit.reset_state()


def _lines(caplog):
    return [r for r in caplog.records if r.name == LOGGER]


# ── Summary cadence ─────────────────────────────────────────────

def test_burst_shaped_like_production_is_reported_in_full(limiter, caplog):
    """13.11 09:15: 500 admitted in 44 s, rejections until the window reset, then
    ordinary traffic.  The first rejection is reported at once; the rest of the
    burst at the first request one window later, admitted or not."""
    caplog.set_level(logging.WARNING, logger=LOGGER)
    ip = "172.18.0.50"
    t = T0
    for i in range(500):
        assert limiter.allow_validate(ip, T0 + i * 88)  # 500 within 44 s
    rejected = 0
    t = T0 + 44_000
    while t <= T0 + 60_000:  # 50 per second until the window ends
        assert not limiter.allow_validate(ip, t)
        rejected += 1
        t += 20
    assert len(_lines(caplog)) == 1
    assert "rejected 1 requests" in _lines(caplog)[0].getMessage()

    # The window resets; ordinary traffic resumes and is admitted.
    t = T0 + 61_000
    while t < T0 + 104_000:
        assert limiter.allow_validate(ip, t)
        t += 2_000
    assert len(_lines(caplog)) == 1  # less than one window since the first line

    assert limiter.allow_validate(ip, T0 + 104_100)  # admitted, one window after the line
    lines = _lines(caplog)
    assert len(lines) == 2
    assert f"rejected {rejected - 1} requests" in lines[1].getMessage()
    assert f"{ip}={rejected - 1}" in lines[1].getMessage()
    stats = limiter.rejection_stats()
    assert stats["rejectedTotal"] == rejected
    assert stats["topAddresses"] == [{"address": ip, "rejected": rejected}]


def test_at_most_one_line_per_window(limiter, caplog, monkeypatch):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    limiter.reset_state(limiter.Limits(0, 1, 60_000))
    line_times = []
    for i in range(6_000):  # ten minutes, ten requests a second, one admitted per window
        limiter.allow_validate("10.0.0.1", T0 + i * 100)
        t = limiter._rejections.last_summary_mono_ms
        if t and (not line_times or line_times[-1] != t):
            line_times.append(t)
    assert len(_lines(caplog)) == len(line_times)
    assert 9 <= len(line_times) <= 11
    assert all(b - a >= 60_000 for a, b in zip(line_times, line_times[1:]))
    total = sum(int(r.getMessage().split("rejected ")[1].split(" ")[0]) for r in _lines(caplog))
    pending = limiter._rejections.pending.total
    assert total + pending == limiter.rejection_stats()["rejectedTotal"]


def test_wall_clock_change_does_not_hold_back_summaries(limiter, caplog):
    """The wall clock goes back an hour after a summary: the next one still comes
    one window later, timed by the monotonic clock."""
    caplog.set_level(logging.WARNING, logger=LOGGER)
    for i in range(500):
        limiter.allow_validate("10.0.0.1", T0 + i)
    assert not limiter.allow_validate("10.0.0.1", T0 + 1_000)
    assert len(_lines(caplog)) == 1
    limiter.wall_shift = -3_600_000.0
    for s in range(2, 70):  # rejected: the bucket's window started in the wall clock's future
        limiter.allow_validate("10.0.0.1", T0 + s * 1_000)
    assert len(_lines(caplog)) == 2


def test_retry_after_counts_down_to_the_window_end(limiter):
    for i in range(500):
        limiter.allow_validate("10.0.0.1", T0 + i)
    limiter.allow_validate("10.0.0.1", T0 + 1_000)  # rejected; the window began at T0
    for offset, expected in ((1_000, 59), (30_500, 30), (59_999, 1), (60_000, 1), (75_000, 1)):
        limiter._clock.wall = T0 + offset
        assert limiter.retry_after_seconds("10.0.0.1") == expected, offset
    assert limiter.retry_after_seconds("10.9.9.9") == 1  # no window: never more than a second


def test_no_line_without_rejections(limiter, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    for i in range(500):
        assert limiter.allow_validate("10.0.0.1", T0 + i)
    assert limiter.allow_validate("10.0.0.1", T0 + 120_000)
    assert _lines(caplog) == []
    assert limiter.rejection_stats()["rejectedTotal"] == 0
    assert limiter.rejection_stats()["lastRejectedAt"] is None


# ── Bounded memory ──────────────────────────────────────────────

def test_many_addresses_stay_bounded_and_one_line_per_window(limiter, caplog, monkeypatch):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    limiter.reset_state(limiter.Limits(0, 1, 60_000))
    n = limiter.MAX_TRACKED_KEYS + 500
    for k in range(n):
        ip = f"2001:db8::{k:x}"
        assert limiter.allow_validate(ip, T0 + k)
        assert not limiter.allow_validate(ip, T0 + k)
    assert len(_lines(caplog)) == 1
    stats = limiter.rejection_stats()
    assert stats["rejectedTotal"] == n
    assert stats["trackedAddresses"] == limiter.MAX_TRACKED_KEYS
    assert stats["untrackedRejected"] == 500
    assert len(stats["topAddresses"]) == limiter.STATS_TOP_KEYS
    assert len(limiter._rejections.pending.by_key) <= limiter.MAX_TRACKED_KEYS

    # The next line reports the pending part, untracked addresses included.
    limiter.allow_validate("2001:db8::ffff:1", T0 + 70_000)
    msg = _lines(caplog)[1].getMessage()
    assert f"rejected {n - 1} requests" in msg
    assert "more from other addresses" in msg


# ── HTTP: admin endpoint and unchanged answers ──────────────────

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


@pytest.fixture()
def client(monkeypatch):
    main_mod, rate_limit, _, _ = _app()
    fake = FakeRedis()
    monkeypatch.setattr(main_mod.redis, "from_url", lambda *a, **k: fake)
    # The cached settings may come from another module or the environment: whatever
    # they name, the app opens this module's own database file.
    create_database = main_mod.create_database
    monkeypatch.setattr(main_mod, "create_database", lambda _url: create_database(_DATABASE_URL))
    rate_limit.reset_state(rate_limit.Limits(0, 2, 60_000))
    with TestClient(main_mod.app) as c:
        c.fake_redis = fake
        # A full session counts only for an existing admin with MFA enabled.
        c.admin_id = str(uuid.uuid4())
        db = main_mod.app.state.db
        c.portal.call(
            db.execute,
            "INSERT INTO admin_users (id, username, password_hash, totp_enabled) VALUES ($1, $2, $3, $4)",
            c.admin_id, "rl-admin", "unused", True,
        )
        try:
            yield c
        finally:
            c.portal.call(db.execute, "DELETE FROM admin_users WHERE id = $1", c.admin_id)
    rate_limit.reset_state()


def _session(client, kind: str) -> None:
    _, _, COOKIE_NAME, PREFIX = _app()
    sid = secrets.token_hex(32)
    client.fake_redis.data[PREFIX + sid] = json.dumps({"kind": kind, "adminId": client.admin_id, "csrf": secrets.token_hex(24)})
    client.cookies.set(COOKIE_NAME, sid)


@pytest.mark.parametrize("kind", [None, "setup", "partial", "bogus"])
def test_endpoint_needs_a_full_session(client, kind):
    if kind:
        _session(client, kind)
    res = client.get("/admin/api/rate-limit")
    assert res.status_code == 401
    assert "rejectedTotal" not in res.text


def test_endpoint_reports_rejections(client):
    _session(client, "full")
    body = {"token": "x" * 40, "area": "orders", "level": "read"}
    answers = [client.post("/validate", json=body) for _ in range(4)]
    assert [a.status_code for a in answers] == [200, 200, 429, 429]
    assert [a.json() for a in answers[2:]] == [{"detail": "Too many requests"}] * 2
    res = client.get("/admin/api/rate-limit")
    assert res.status_code == 200
    data = res.json()
    assert data["limit"] == 2
    assert data["windowMs"] == 60_000
    assert data["rejectedTotal"] == 2
    assert data["lastRejectedAt"]
    assert data["since"]
    assert data["untrackedRejected"] == 0
    assert [a["rejected"] for a in data["topAddresses"]] == [2]


def test_rejected_answers_are_429_and_not_logged(client):
    main_mod, rate_limit, _, _ = _app()
    body = {"token": "y" * 40, "area": "orders", "level": "read"}
    for _ in range(2):
        client.post("/validate", json=body)
    count = client.portal.call(main_mod.app.state.db.fetchval, "SELECT COUNT(*) FROM access_logs")

    for res in (
        client.post("/validate", json=body),
        client.get("/validate", params=body),
        client.post("/validate/grants", json={"token": "y" * 40}),
    ):
        assert (res.status_code, res.json()) == (429, {"detail": "Too many requests"})
        assert 1 <= int(res.headers["retry-after"]) <= 60

    assert client.portal.call(main_mod.app.state.db.fetchval, "SELECT COUNT(*) FROM access_logs") == count
    assert rate_limit.rejection_stats()["rejectedTotal"] == 3

def test_openapi_documents_the_429(client):
    paths = client.get("/openapi.json").json()["paths"]
    for path, method in (("/validate", "post"), ("/validate", "get"), ("/validate/grants", "post")):
        answer = paths[path][method]["responses"]["429"]
        assert "Retry-After" in answer["headers"], (path, method)

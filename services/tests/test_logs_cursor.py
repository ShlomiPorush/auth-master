"""Cursor paging for the admin log listings (/admin/api/logs/activity and /access).

Rows are ordered by (created_at DESC, id DESC). ``before=<cursor>`` continues
after a row, ``since=<cursor>`` returns rows from 60 seconds before a row's
timestamp onwards, and both combine for a refresh that is read page by page.

The scenario tests call the real route handlers against a schema built by
``ensure_database_schema``. They always run on a temporary SQLite database, and
also on PostgreSQL when ``AUTH_TEST_POSTGRES_URL`` is set (see
test_access_logs_listing.py for how to provide a throwaway server; the log
tables are emptied before every test). The HTTP tests run on SQLite only.

Every secret the app reads is generated per run with ``secrets``; Redis is
replaced by an in-memory fake. Requires pytest and httpx.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.db import create_database  # noqa: E402
from app.routers.admin_logs import get_access_logs, get_activity_logs  # noqa: E402
from app.schema import ensure_database_schema  # noqa: E402

PG_URL_ENV = "AUTH_TEST_POSTGRES_URL"

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
_HTTP_DATABASE_URL = f"sqlite:///{_TMP_DIR.name}/http.db"
_SECRETS = {
    "ADMIN_API_KEY": secrets.token_urlsafe(32),
    "SESSION_SECRET": secrets.token_urlsafe(48),
    "APP_ENCRYPTION_KEY": secrets.token_hex(16),
}
for _name, _value in _SECRETS.items():
    if _value in _PLACEHOLDERS:
        raise RuntimeError(f"{_name} is empty or a placeholder")
os.environ.update(_SECRETS)
os.environ["DATABASE_URL"] = _HTTP_DATABASE_URL
os.environ["REDIS_URL"] = f"redis://:{secrets.token_hex(32)}@127.0.0.1:6379/0"
os.environ["RATE_LIMIT_MAX_REQ"] = "100000"

SQLITE_TS = "%Y-%m-%d %H:%M:%S"
# Recent, so the startup log purge never removes the rows; whole seconds plus
# microseconds, which only PostgreSQL keeps.
BASE = datetime.now(timezone.utc).replace(microsecond=123456) - timedelta(hours=1)

BACKENDS = [
    "sqlite",
    pytest.param("postgres", marks=pytest.mark.skipif(not os.environ.get(PG_URL_ENV), reason=f"{PG_URL_ENV} is not set")),
]


# ── Harness ─────────────────────────────────────────────────────

@pytest.fixture(params=BACKENDS)
def run(request, tmp_path):
    """Run an async scenario against a fresh schema with empty log tables."""

    def runner(scenario):
        async def main():
            url = f"sqlite:///{tmp_path}/logs.db" if request.param == "sqlite" else os.environ[PG_URL_ENV]
            db = await create_database(url)
            try:
                await ensure_database_schema(db)
                await db.execute("DELETE FROM activity_logs")
                await db.execute("DELETE FROM access_logs")
                await scenario(db)
            finally:
                await db.close()

        asyncio.run(main())

    return runner


def _stored(db, created: datetime):
    """created_at as each backend stores it."""
    return created.strftime(SQLITE_TS) if db.is_sqlite else created


async def _add_activity(db, created: datetime, action: str = "token.create", actor: str = "alice") -> str:
    row_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO activity_logs (id, actor, action, entity_name, created_at) VALUES ($1::uuid, $2, $3, $4, $5)",
        row_id, actor, action, "entity", _stored(db, created),
    )
    return row_id


async def _add_access(db, created: datetime, action: str = "orders", actor: str = "alice") -> str:
    # For access logs, "action" is the area and "actor" the token name.
    row_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO access_logs (id, token_id, token_name, area, level, result, ip_address, created_at) "
        "VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8)",
        row_id, None, actor, action, "read", True, "10.0.0.1", _stored(db, created),
    )
    return row_id


def _request(db):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=db)))


async def _list_activity(db, **params):
    args = {"page": 1, "limit": 50, "action": None, "search": None, "before": None, "since": None}
    args.update(params)
    return await get_activity_logs(_request(db), {}, **args)


async def _list_access(db, **params):
    args = {"page": 1, "limit": 50, "result": None, "search": None, "token_id": None, "before": None, "since": None}
    args.update(params)
    return await get_access_logs(_request(db), {}, **args)


KINDS = {
    "activity": (_add_activity, _list_activity),
    "access": (_add_access, _list_access),
}


@pytest.fixture(params=sorted(KINDS))
def kind(request):
    return KINDS[request.param]


def _order(rows: list[tuple[datetime, str]], db) -> list[str]:
    """Expected ids in (created_at DESC, id DESC) order, at the stored precision."""
    return [i for _, i in sorted(((_stored(db, c), i) for c, i in rows), reverse=True)]


def _ids(data) -> list[str]:
    return [log["id"] for log in data["logs"]]


async def _page_all(list_fn, db, limit: int, before: str | None = None, **params) -> list[str]:
    """Follow nextCursor with before= until a short page."""
    ids: list[str] = []
    while True:
        data = await list_fn(db, limit=limit, before=before, **params)
        ids.extend(_ids(data))
        if len(data["logs"]) < limit:
            return ids
        before = data["nextCursor"]


async def _refresh(list_fn, db, head: str, limit: int = 500, **params) -> tuple[list[str], str | None]:
    """A client refresh: since=head, continued with before= until a short page."""
    ids: list[str] = []
    data = await list_fn(db, limit=limit, since=head, **params)
    while True:
        ids.extend(_ids(data))
        if len(data["logs"]) < limit:
            return ids, data["nextCursor"]
        data = await list_fn(db, limit=limit, since=head, before=data["nextCursor"], **params)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _cursor(t, i) -> str:
    return _b64(json.dumps({"t": t, "i": i}, separators=(",", ":")).encode())


# ── Scenarios ───────────────────────────────────────────────────

def test_cursor_encoding_and_empty_listing(run, kind):
    add, list_fn = kind

    async def scenario(db):
        empty = await list_fn(db)
        assert empty["logs"] == [] and empty["nextCursor"] is None and empty["headCursor"] is None

        older = await add(db, BASE - timedelta(seconds=5))
        newer = await add(db, BASE)
        data = await list_fn(db)
        assert _ids(data) == [newer, older]
        for key, row_id, created in (("headCursor", newer, BASE), ("nextCursor", older, BASE - timedelta(seconds=5))):
            value = data[key]
            assert "=" not in value
            raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            assert raw == json.dumps(json.loads(raw), separators=(",", ":")).encode()
            decoded = json.loads(raw)
            assert decoded["i"] == row_id
            if db.is_sqlite:
                assert decoded["t"] == created.strftime(SQLITE_TS)
            else:
                assert datetime.fromisoformat(decoded["t"]) == created

    run(scenario)


def test_equal_timestamps_have_a_deterministic_order(run, kind):
    add, list_fn = kind

    async def scenario(db):
        rows = []
        for k in range(12):
            created = BASE if k < 10 else BASE - timedelta(seconds=k)
            rows.append((created, await add(db, created)))
        expected = _order(rows, db)
        assert _ids(await list_fn(db)) == expected
        assert _ids(await list_fn(db)) == expected

    run(scenario)


def test_before_paging_returns_every_row_once(run, kind):
    add, list_fn = kind

    async def scenario(db):
        # 75 rows, 15 sharing each of 5 timestamps.
        rows = []
        for k in range(75):
            created = BASE - timedelta(seconds=k // 15)
            rows.append((created, await add(db, created)))
        expected = _order(rows, db)
        for limit in (10, 15, 7, 75):
            assert await _page_all(list_fn, db, limit) == expected

        # A last page that ends exactly at the end: the next one is empty.
        last = await list_fn(db, limit=75)
        tail = await list_fn(db, limit=10, before=last["nextCursor"])
        assert tail["logs"] == [] and tail["nextCursor"] is None and tail["total"] == 75

    run(scenario)


def test_inserts_between_pages_cause_no_repeats_or_gaps(run, kind):
    add, list_fn = kind

    async def scenario(db):
        rows = []
        for k in range(30):
            created = BASE - timedelta(seconds=k // 4)
            rows.append((created, await add(db, created)))

        first = await list_fn(db, limit=10)
        # New rows above the first page (including one in the head's second),
        # which offset paging would have repeated, and one further down that
        # the next pages have not reached yet.
        for k in range(5):
            await add(db, BASE + timedelta(seconds=1 + k))
        await add(db, BASE)
        late_lower = BASE - timedelta(seconds=6)
        rows.append((late_lower, await add(db, late_lower)))

        rest = await _page_all(list_fn, db, 10, before=first["nextCursor"])
        assert _ids(first) + rest == _order(rows, db)

    run(scenario)


def test_offset_paging_would_repeat_rows(run, kind):
    """Documents why cursors exist: page 2 after an insert repeats a row."""
    add, list_fn = kind

    async def scenario(db):
        for k in range(20):
            await add(db, BASE - timedelta(seconds=k))
        page1 = _ids(await list_fn(db, page=1, limit=10))
        await add(db, BASE + timedelta(seconds=1))
        page2 = _ids(await list_fn(db, page=2, limit=10))
        assert page1[-1] == page2[0]

    run(scenario)


def test_since_returns_rows_within_the_lookback(run, kind):
    add, list_fn = kind

    async def scenario(db):
        offsets = (0, 30, 59, 60, 61, 120)
        ids = {s: await add(db, BASE - timedelta(seconds=s)) for s in offsets}
        head = (await list_fn(db, limit=1))["headCursor"]
        data = await list_fn(db, since=head)
        assert _ids(data) == [ids[s] for s in (0, 30, 59, 60)]
        assert data["total"] == len(offsets)

    run(scenario)


def test_late_commit_older_than_head_is_returned_by_since(run, kind):
    add, list_fn = kind

    async def scenario(db):
        for s in (0, 10, 20):
            await add(db, BASE - timedelta(seconds=s))
        shown = await list_fn(db, limit=30)
        head = shown["headCursor"]

        # Committed after the list was shown, with an earlier created_at.
        late = await add(db, BASE - timedelta(seconds=5))
        refreshed = _ids(await list_fn(db, since=head))
        assert late in refreshed
        assert set(_ids(shown)) <= set(refreshed)

    run(scenario)


def test_large_same_second_group_then_load_more(run, kind):
    add, list_fn = kind

    async def scenario(db):
        history = []
        for k in range(40):
            created = BASE - timedelta(minutes=10, seconds=k)
            history.append((created, await add(db, created)))
        burst = []
        for _ in range(501):
            burst.append((BASE, await add(db, BASE)))
        burst_order = _order(burst, db)
        full_order = _order(history + burst, db)

        shown = await list_fn(db, limit=30)
        assert _ids(shown) == burst_order[:30]
        head = shown["headCursor"]

        first, bottom_cursor = await _refresh(list_fn, db, head)
        assert first == burst_order
        second, second_cursor = await _refresh(list_fn, db, head)
        assert second == first and second_cursor == bottom_cursor

        # Merged list: union of the shown prefix and the refresh, in server order.
        merged = sorted(set(_ids(shown)) | set(first), key=full_order.index)
        assert merged == burst_order
        # Load more from the bottom row of the merged list reaches the history.
        loaded = await _page_all(list_fn, db, 30, before=bottom_cursor)
        assert merged + loaded == full_order

    run(scenario)


def test_limit_caps(run, kind):
    add, list_fn = kind

    async def scenario(db):
        for k in range(120):
            await add(db, BASE - timedelta(seconds=k % 30))
        assert len((await list_fn(db, limit=100))["logs"]) == 100
        head = (await list_fn(db, limit=1))["headCursor"]
        since = await list_fn(db, limit=500, since=head)
        assert len(since["logs"]) == 120 and since["limit"] == 500

    run(scenario)


def test_limit_above_100_without_since_is_refused(run, kind):
    add, list_fn = kind

    async def scenario(db):
        await add(db, BASE)
        # The original contract: more than 100 rows per request is refused, not cut
        with pytest.raises(HTTPException) as exc:
            await list_fn(db, limit=101)
        assert exc.value.status_code == 422
        # A since refresh may ask for up to 500
        head = (await list_fn(db, limit=1))["headCursor"]
        assert (await list_fn(db, limit=500, since=head))["limit"] == 500

    run(scenario)


def test_page_parameter_still_works(run, kind):
    add, list_fn = kind

    async def scenario(db):
        rows = []
        for k in range(25):
            created = BASE - timedelta(seconds=k // 3)
            rows.append((created, await add(db, created)))
        expected = _order(rows, db)
        data = await list_fn(db, page=2, limit=10)
        assert _ids(data) == expected[10:20]
        assert (data["total"], data["page"], data["limit"]) == (25, 2, 10)
        assert set(data) >= {"logs", "total", "page", "limit", "nextCursor", "headCursor"}
        # nextCursor continues after this page; with a cursor, page is ignored.
        rest = await list_fn(db, page=7, limit=10, before=data["nextCursor"])
        assert _ids(rest) == expected[20:]
        assert _ids(await list_fn(db, page=3, limit=10)) == expected[20:]

    run(scenario)


def test_activity_filters_combine_with_cursors(run):
    async def scenario(db):
        rows = {"token.create": [], "token.delete": []}
        for k in range(40):
            action = "token.create" if k % 3 else "token.delete"
            actor = "bob" if k % 2 else "alice"
            created = BASE - timedelta(seconds=k // 5)
            rows[action].append((created, actor, await _add_activity(db, created, action=action, actor=actor)))

        creates = [(c, i) for c, _, i in rows["token.create"]]
        assert await _page_all(_list_activity, db, 4, action="token.create") == _order(creates, db)
        data = await _list_activity(db, limit=4, action="token.create")
        assert data["total"] == len(creates)

        bob_deletes = [(c, i) for c, a, i in rows["token.delete"] if a == "bob"]
        assert await _page_all(_list_activity, db, 2, action="token.delete", search="BOB") == _order(bob_deletes, db)

        head = (await _list_activity(db, limit=1))["headCursor"]
        assert _ids(await _list_activity(db, since=head, action="token.delete", search="bob")) == _order(bob_deletes, db)

    run(scenario)


def test_access_filters_combine_with_cursors(run):
    async def scenario(db):
        token_id = str(uuid.uuid4())
        expected = []
        for k in range(30):
            created = BASE - timedelta(seconds=k // 4)
            row_id = str(uuid.uuid4())
            tid = token_id if k % 2 else None
            allowed = k % 3 != 0
            await db.execute(
                "INSERT INTO access_logs (id, token_id, token_name, area, level, result, ip_address, created_at) "
                "VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8)",
                row_id, tid, "svc", "billing" if k % 5 else "orders", "read", allowed, None, _stored(db, created),
            )
            if tid == token_id and allowed and k % 5:
                expected.append((created, row_id))

        params = {"token_id": token_id, "result": True, "search": "bill"}
        assert await _page_all(_list_access, db, 3, **params) == _order(expected, db)
        assert (await _list_access(db, **params))["total"] == len(expected)
        head = (await _list_access(db, limit=1))["headCursor"]
        assert _ids(await _list_access(db, since=head, **params)) == _order(expected, db)

    run(scenario)


def _malformed(db) -> list[str]:
    good_id = str(uuid.uuid4())
    good_t = BASE.strftime(SQLITE_TS) if db.is_sqlite else BASE.isoformat()
    # A valid timestamp of the other backend's form is malformed here.
    other_t = BASE.isoformat() if db.is_sqlite else BASE.strftime(SQLITE_TS)
    return [
        "",
        "garbage!",
        "' OR 1=1 --",
        "abc=",
        _cursor(good_t, good_id) + "==",
        "A" * 600,
        "A",
        _b64(b"hello, not json"),
        _b64(b"\xff\xfe\x00"),
        _b64(b"[1, 2]"),
        _b64(b'"just a string"'),
        _b64(json.dumps({"t": good_t}).encode()),
        _b64(json.dumps({"t": good_t, "i": good_id, "x": 1}).encode()),
        _cursor(1700000000, good_id),
        _cursor(good_t, 42),
        _cursor(None, good_id),
        _cursor(good_t, "not-a-uuid"),
        _cursor(good_t, good_id.upper()),
        _cursor(good_t, "{" + good_id + "}"),
        _cursor(good_t, good_id.replace("-", "")),
        _cursor(good_t, "1' OR '1'='1"),
        _cursor(good_t + "' OR '1'='1", good_id),
        _cursor("2026-13-01 00:00:00", good_id),
        _cursor("2026-1-1 0:0:0", good_id),
        _cursor("yesterday", good_id),
        _cursor(other_t, good_id),
        _cursor("0001-01-01T00:00:00+00:00" if not db.is_sqlite else "0001-01-01 00:00:00", good_id),
    ]


def test_malformed_cursors_are_rejected(run, kind):
    add, list_fn = kind

    async def scenario(db):
        await add(db, BASE)
        for value in _malformed(db):
            for param in ("before", "since"):
                with pytest.raises(HTTPException) as exc:
                    await list_fn(db, **{param: value})
                assert exc.value.status_code == 400, (param, value)
                assert exc.value.detail == "Invalid cursor"
        assert (await list_fn(db))["total"] == 1

    run(scenario)


# ── HTTP ────────────────────────────────────────────────────────

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


@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient

    import app.main as main_mod
    from app.admin_deps import full_session
    from app.config import get_settings

    s = get_settings()
    for field in _SECRET_FIELDS:
        if getattr(s, field) in _PLACEHOLDERS:
            raise RuntimeError(f"{field} is empty or a placeholder")
    # Always this run's own database, whatever DATABASE_URL the cached settings hold.
    create_database_ = main_mod.create_database
    monkeypatch.setattr(main_mod, "create_database", lambda _url: create_database_(_HTTP_DATABASE_URL))
    fake = FakeRedis()
    monkeypatch.setattr(main_mod.redis, "from_url", lambda *a, **k: fake)

    async def _no_cleanup(*_a, **_k):
        return None

    monkeypatch.setattr(main_mod, "cleanup_expired_tokens", _no_cleanup)
    with TestClient(main_mod.app) as c:
        c.db = main_mod.app.state.db
        c.portal.call(_wipe, c.db)
        main_mod.app.dependency_overrides[full_session] = lambda: {}
        try:
            yield c
        finally:
            main_mod.app.dependency_overrides.pop(full_session, None)
            c.portal.call(_wipe, c.db)


async def _wipe(db):
    for table in ("activity_logs", "access_logs"):
        await db.execute(f"DELETE FROM {table}")


@pytest.mark.parametrize("path", ["/admin/api/logs/activity", "/admin/api/logs/access"])
def test_http_cursor_paging_and_errors(client, path):
    add = _add_activity if path.endswith("activity") else _add_access
    rows = []
    for k in range(12):
        created = BASE - timedelta(seconds=k // 4)
        rows.append((created, client.portal.call(add, client.db, created)))
    expected = [i for _, i in sorted(((c.strftime(SQLITE_TS), i) for c, i in rows), reverse=True)]

    first = client.get(path, params={"limit": 5})
    assert first.status_code == 200
    body = first.json()
    second = client.get(path, params={"limit": 5, "before": body["nextCursor"]}).json()
    third = client.get(path, params={"limit": 5, "before": second["nextCursor"]}).json()
    assert [log["id"] for b in (body, second, third) for log in b["logs"]] == expected

    since = client.get(path, params={"limit": 500, "since": body["headCursor"]})
    assert since.status_code == 200 and len(since.json()["logs"]) == 12

    for bad in ("garbage!", _b64(b"not json"), _cursor("2026-01-01 00:00:00", "1' OR '1'='1")):
        for param in ("before", "since"):
            res = client.get(path, params={param: bad})
            assert res.status_code == 400
            assert res.json() == {"detail": "Invalid cursor"}

    assert client.get(path, params={"limit": 501, "since": body["headCursor"]}).status_code == 422
    # Without since the original contract holds: at most 100, more is refused
    assert client.get(path, params={"limit": 500}).status_code == 422
    assert client.get(path, params={"limit": 101}).status_code == 422
    assert len(client.get(path, params={"limit": 100}).json()["logs"]) == 12


def test_http_logs_require_a_session(client):
    import app.main as main_mod
    from app.admin_deps import full_session

    main_mod.app.dependency_overrides.pop(full_session, None)
    assert client.get("/admin/api/logs/activity", params={"before": "garbage!"}).status_code == 401
    assert client.get("/admin/api/logs/access").status_code == 401

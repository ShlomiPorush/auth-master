"""Token usage statistics for the admin Usage page, read from access_logs.

Definitions (the same everywhere in this module):

- A call is an access_logs row with level <> 'grants': one /validate request that was
  written to the log. Rows from /validate/grants (level 'grants', area '*') are counted
  only as grants checks. Requests rejected by the rate limit or with an invalid level
  are never written to the log, so they are not counted.
- Calls are attributed by token_id. Rows without a token_id (a token the service did not
  recognise) form one "unknown" group. A token_id that is no longer in tokens is a deleted
  token, named by the newest token_name logged for it.
- An area is grouped like validation compares it (grants.grant_covers): the logged text,
  stripped and lower-cased by Python. SQL groups by the exact text and Python merges.
- Every query reads rows from the same start: the first bucket of the period, or the
  retention cutoff (now - ACCESS_LOG_RETENTION_DAYS) when that is later. Rows older than
  the cutoff that the purge has not deleted yet are never counted.
- Periods are calendar buckets in the app time zone (TZ): the last 24 local hours, or the
  last 7 or 30 local days including today. SQL counts calls in 15-minute UTC slots, and
  every UTC offset in use is a multiple of 15 minutes, so each slot falls in exactly one
  local hour or day, also on daylight saving days.
- The rows are read through _source: the summary rows of access_log_rollup for the whole
  slots of the period (app.usage_rollup), the rows of those slots that no summary counts
  yet, and the rows of the partial slots at both ends. Each summary row stands for `w`
  calls, so every count is a sum of w.
"""
from __future__ import annotations

import uuid
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request

from app.admin_deps import FullSession
from app.config import get_settings
from app.datetime_utils import fmt_datetime, get_tz_name, is_expired
from app.db import report_database
from app.usage_rollup import bind_time, ceil_slot, floor_slot, summary_valid_from, unrolled

router = APIRouter(prefix="/admin/api/usage", tags=["admin-usage"])

Range = Literal["24h", "7d", "30d"]

SLOT_SECONDS = 15 * 60
# More (token, area) groups than this in a period: area figures are marked incomplete.
MAX_AREA_GROUPS = 50_000
# Area names and addresses come from callers; longer text is cut for display.
MAX_TEXT = 120
OVERVIEW_TOP_AREAS = 3
DETAIL_TOP_AREAS = 50
DETAIL_TOP_ADDRESSES = 10
_LEVELS = ("read", "readwrite", "write", "delete", "all")

# result is BOOLEAN on PostgreSQL and 0/1 on SQLite; both read as a condition.
# w is the number of calls a row of _source stands for.
_CALL = "level <> 'grants'"
_CALLS = f"SUM(CASE WHEN {_CALL} THEN w ELSE 0 END)"
_ALLOWED = f"SUM(CASE WHEN {_CALL} AND result THEN w ELSE 0 END)"
_GRANTS = "SUM(CASE WHEN level = 'grants' THEN w ELSE 0 END)"
_ALLOWED_OF_CALLS = "SUM(CASE WHEN result THEN w ELSE 0 END)"


# ---------------------------------------------------------------------------
# Period and buckets
# ---------------------------------------------------------------------------


def _buckets(range_: str, now: datetime, tz: ZoneInfo) -> list[tuple[datetime, datetime]]:
    """UTC (start, end) of each bucket of the period, oldest first; the last one holds now."""
    local_now = now.astimezone(tz)
    if range_ == "24h":
        # A local hour starts where the local (date, hour) changes. Walking 15-minute
        # slots finds every start, also around a 30-minute clock change (Lord Howe),
        # where one local hour lasts 30 or 90 minutes.
        step = timedelta(seconds=SLOT_SECONDS)
        t = datetime.fromtimestamp(now.timestamp() // SLOT_SECONDS * SLOT_SECONDS, timezone.utc) - timedelta(hours=27)
        stop = now + timedelta(hours=2)
        edges: list[datetime] = []
        prev = None
        while t <= stop:
            loc = t.astimezone(tz)
            key = (loc.date(), loc.hour)
            if prev is not None and key != prev:
                edges.append(t)
            prev = key
            t += step
        last = bisect_right(edges, now) - 1
        hours = edges[last - 23:last + 2]
        return list(zip(hours[:-1], hours[1:]))
    days = 7 if range_ == "7d" else 30
    today = local_now.date()
    dates = [today - timedelta(days=k) for k in range(days - 1, -1, -1)]
    dates.append(today + timedelta(days=1))
    # Local midnight; where midnight is skipped (fold=0), the day starts at the transition.
    edges = [datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(timezone.utc) for d in dates]
    return list(zip(edges[:-1], edges[1:]))


def _slot_expr(db) -> str:
    """Start of the 15-minute UTC slot of t, as Unix seconds."""
    if db.is_sqlite:
        # Times are stored as "YYYY-MM-DD HH:MM:SS" in UTC.
        return f"(CAST(strftime('%s', t) AS INTEGER) / {SLOT_SECONDS}) * {SLOT_SECONDS}"
    return f"FLOOR(EXTRACT(EPOCH FROM t) / {SLOT_SECONDS}) * {SLOT_SECONDS}"


class _Params:
    """Query arguments; each value is bound once and named by its key."""

    def __init__(self, db) -> None:
        self.db = db
        self.args: list[Any] = []
        self._index: dict[Any, int] = {}

    def __call__(self, key: Any, value: Any) -> str:
        if key not in self._index:
            self.args.append(value)
            self._index[key] = len(self.args)
        return f"${self._index[key]}"

    def time(self, key: str, value: datetime) -> str:
        return self(key, bind_time(self.db, value)) + "::timestamptz"


_ANY = object()
_RAW = (
    "token_id, area, level, result, ip_address, 1 AS w, created_at AS t, created_at AS last_at, "
    "token_name, created_at AS name_at, id AS name_id"
)
_SUMMARY = "token_id, area, level, result, ip_address, calls AS w, slot AS t, last_at, token_name, name_at, name_id"


def _source(db, p: "_Period", params: _Params, token: Any = _ANY) -> str:
    """Every access log row of the period, as rows of (token_id, area, level, result,
    ip_address, w, t, last_at, token_name, name_at, name_id).

    Raw rows have w = 1 and t, last_at, name_at = created_at, name_id = id. A summary row
    stands for w rows of its group: t is its slot, last_at the newest created_at, and
    (name_at, name_id) the created_at and id of the row its token_name came from.

    The whole slots [r1, r2) of the period come from the summary plus the rows no summary
    counts yet; the partial slots [start, r1) and [r2, now] come from access_logs, whether
    summarised or not. r1 is never before the summary's valid_from (app.usage_rollup). A
    summary row's slot is the slot of every row it counts, so each row of [start, now] is
    read exactly once. token: _ANY, None (the unknown group) or a token id.
    """
    if token is _ANY:
        tf = ""
    elif token is None:
        tf = " AND token_id IS NULL"
    else:
        tf = f" AND token_id = {params('token', token)}"
    by_time = "access_logs INDEXED BY idx_access_logs_created_at" if db.is_sqlite else "access_logs"
    start, now = params.time("start", p.start), params.time("now", p.now)
    r1, r2 = ceil_slot(p.start), floor_slot(p.now)
    if p.valid_from is not None:
        r1 = max(r1, p.valid_from)
    if r2 <= r1:
        # No whole slot in the period: read it like before the summary existed.
        return f"SELECT {_RAW} FROM {by_time} WHERE created_at >= {start} AND created_at <= {now}{tf}"
    first, last = params.time("r1", r1), params.time("r2", r2)
    by_flag = "access_logs INDEXED BY idx_access_logs_unrolled" if db.is_sqlite else "access_logs"
    return (
        f"SELECT {_SUMMARY} FROM access_log_rollup WHERE slot >= {first} AND slot < {last}{tf}"
        f" UNION ALL SELECT {_RAW} FROM {by_flag} WHERE {unrolled(db)} AND created_at >= {first} AND created_at < {last}{tf}"
        f" UNION ALL SELECT {_RAW} FROM {by_time} WHERE created_at >= {start} AND created_at < {first}{tf}"
        f" UNION ALL SELECT {_RAW} FROM {by_time} WHERE created_at >= {last} AND created_at <= {now}{tf}"
    )


class _Period:
    def __init__(self, range_: str, now: datetime | None = None, tz: ZoneInfo | None = None, retention_days: int | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        self.range = range_
        self.tz = tz or ZoneInfo(get_tz_name())
        self.buckets = _buckets(range_, now, self.tz)
        self.retention_days = get_settings().access_log_retention_days if retention_days is None else retention_days
        self.cutoff = now - timedelta(days=self.retention_days)
        self.first = self.buckets[0][0]
        self.start = max(self.first, self.cutoff)
        self.now = now
        # Set from the database in the report's snapshot (app.usage_rollup.summary_valid_from).
        self.valid_from: datetime | None = None

    def coverage(self, start: datetime, end: datetime) -> str:
        if end <= self.cutoff:
            return "none"
        if start < self.cutoff:
            return "partial"
        return "full"

    def head(self) -> dict[str, Any]:
        return {
            "range": self.range,
            "from": fmt_datetime(self.first),
            "to": fmt_datetime(self.now),
            "dataFrom": fmt_datetime(self.start),
            "retentionDays": self.retention_days,
            "partial": self.cutoff > self.first,
        }

    def series(self, slots: list[Any]) -> list[dict[str, Any]]:
        starts = [b[0].timestamp() for b in self.buckets]
        allowed = [0] * len(self.buckets)
        denied = [0] * len(self.buckets)
        for r in slots:
            i = bisect_right(starts, float(r["slot"])) - 1
            if 0 <= i < len(self.buckets) and float(r["slot"]) < self.buckets[i][1].timestamp():
                a, c = int(r["allowed"] or 0), int(r["calls"] or 0)
                allowed[i] += a
                denied[i] += c - a
        return [
            {
                "start": fmt_datetime(s),
                "end": fmt_datetime(e),
                "allowed": allowed[i],
                "denied": denied[i],
                "coverage": self.coverage(s, e),
            }
            for i, (s, e) in enumerate(self.buckets)
        ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cut(text: Any) -> tuple[str, bool]:
    s = "" if text is None else str(text)
    return (s[:MAX_TEXT], True) if len(s) > MAX_TEXT else (s, False)


def _area_key(area: Any) -> str:
    return ("" if area is None else str(area)).strip().lower()


class _AreaGroup:
    __slots__ = ("calls", "allowed", "spellings", "levels")

    def __init__(self) -> None:
        self.calls = 0
        self.allowed = 0
        self.spellings: dict[str, int] = defaultdict(int)
        self.levels: dict[str, list[int]] = {}

    def add(self, raw: Any, calls: int, allowed: int, level: str | None = None) -> None:
        self.calls += calls
        self.allowed += allowed
        self.spellings[("" if raw is None else str(raw)).strip()] += calls
        if level is not None:
            lv = self.levels.setdefault(level, [0, 0])
            lv[0] += allowed
            lv[1] += calls - allowed

    def label(self) -> str:
        # The most frequent spelling; ties go to the alphabetically first one.
        return min(self.spellings.items(), key=lambda kv: (-kv[1], kv[0]))[0]

    def out(self, with_levels: bool) -> dict[str, Any]:
        text, truncated = _cut(self.label())
        item: dict[str, Any] = {"area": text, "truncated": truncated, "calls": self.calls}
        if with_levels:
            item["allowed"] = self.allowed
            item["denied"] = self.calls - self.allowed
            item["levels"] = {
                lv: {"allowed": v[0], "denied": v[1]} for lv, v in sorted(self.levels.items(), key=lambda kv: _LEVELS.index(kv[0]) if kv[0] in _LEVELS else len(_LEVELS))
            }
        return item


def _ranked(groups: dict[str, _AreaGroup]) -> list[_AreaGroup]:
    return sorted(groups.values(), key=lambda g: (-g.calls, g.label()))


def _status(row: Any) -> str:
    if not bool(row["is_active"]):
        return "disabled"
    if is_expired(row["expires_at"]):
        return "expired"
    return "active"


def _counts(row: Any | None) -> dict[str, int]:
    calls = int(row["calls"] or 0) if row else 0
    allowed = int(row["allowed"] or 0) if row else 0
    return {
        "calls": calls,
        "allowed": allowed,
        "denied": calls - allowed,
        "grantsChecks": int(row["grants_checks"] or 0) if row else 0,
    }


_TOTALS_COLS = f"""
    {_CALLS} AS calls,
    {_ALLOWED} AS allowed,
    {_GRANTS} AS grants_checks,
    COUNT(DISTINCT CASE WHEN {_CALL} THEN ip_address END) AS ip_count,
    MAX(CASE WHEN {_CALL} THEN last_at END) AS last_call_at
"""


async def _query(db, p: "_Period", sql: str, token: Any = _ANY) -> list[Any]:
    """sql with {src} replaced by the period's rows (_source) for these tokens."""
    params = _Params(db)
    src = _source(db, p, params, token)
    return await db.fetch(sql.format(src=f"({src}) AS u"), *params.args)


async def _slots(db, p: "_Period", token: Any = _ANY) -> list[Any]:
    return await _query(
        db, p,
        f"SELECT {_slot_expr(db)} AS slot, SUM(w) AS calls, {_ALLOWED_OF_CALLS} AS allowed "
        f"FROM {{src}} WHERE {_CALL} GROUP BY 1",
        token,
    )


async def _newest_names(db, p: "_Period", token_ids: list[str]) -> dict[str, str]:
    """The newest token_name logged in the period for each of these (deleted) token ids."""
    names: dict[str, str] = {}
    for i in range(0, len(token_ids), 500):
        chunk = token_ids[i:i + 500]
        params = _Params(db)
        src = _source(db, p, params)
        marks = ", ".join(params(("id", n), tid) for n, tid in enumerate(chunk))
        rows = await db.fetch(
            f"""
            SELECT token_id, token_name FROM (
                SELECT token_id, token_name,
                       ROW_NUMBER() OVER (PARTITION BY token_id ORDER BY name_at DESC, name_id DESC) AS rn
                FROM ({src}) AS u
                WHERE token_id IN ({marks}) AND token_name IS NOT NULL
            ) x WHERE rn = 1
            """,
            *params.args,
        )
        names.update({r["token_id"]: r["token_name"] for r in rows})
    return names


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/tokens")
async def usage_overview(request: Request, _sess: FullSession, range_: Range = Query(default="7d", alias="range")):
    """Calls per token in the period, with the top areas and the calls over time."""
    async with report_database(request.app.state.db) as db:
        return await _overview(db, _Period(range_))


async def _overview(db, p: "_Period") -> dict[str, Any]:
    p.valid_from = await summary_valid_from(db)
    per_token = await _query(db, p, f"SELECT token_id, {_TOTALS_COLS} FROM {{src}} GROUP BY token_id")
    area_rows = await _query(
        db, p,
        f"SELECT token_id, area, SUM(w) AS calls FROM {{src}} WHERE {_CALL} GROUP BY token_id, area "
        f"ORDER BY calls DESC LIMIT {MAX_AREA_GROUPS + 1}",
    )
    slots = await _slots(db, p)
    tokens = await db.fetch("SELECT id, name, is_active, expires_at FROM tokens")

    areas_incomplete = len(area_rows) > MAX_AREA_GROUPS
    areas: dict[str | None, dict[str, _AreaGroup]] = defaultdict(dict)
    if not areas_incomplete:
        for r in area_rows:
            groups = areas[r["token_id"]]
            key = _area_key(r["area"])
            groups.setdefault(key, _AreaGroup()).add(r["area"], int(r["calls"]), 0)

    stats = {r["token_id"]: r for r in per_token}
    token_ids = {str(t["id"]) for t in tokens}
    names = await _newest_names(db, p, sorted(t for t in stats if t is not None and t not in token_ids))

    def row(token_id: str | None, name: str, status: str) -> dict[str, Any]:
        r = stats.get(token_id)
        groups = areas.get(token_id, {})
        top = [g.out(False) for g in _ranked(groups)[:OVERVIEW_TOP_AREAS]]
        return {
            "tokenId": token_id,
            "name": name,
            "status": status,
            **_counts(r),
            "ipCount": int(r["ip_count"] or 0) if r else 0,
            "lastCallAt": fmt_datetime(r["last_call_at"]) if r else None,
            "areasCount": None if areas_incomplete else len(groups),
            "topAreas": None if areas_incomplete else top,
        }

    out = []
    known: set[str] = set()
    for t in tokens:
        tid = str(t["id"])
        known.add(tid)
        out.append(row(tid, t["name"], _status(t)))
    for tid in stats:
        if tid is None or tid in known:
            continue
        out.append(row(tid, names.get(tid) or "Deleted token", "deleted"))
    if None in stats:
        out.append(row(None, "Unknown token", "unknown"))

    sum_calls = sum(x["calls"] for x in out)
    sum_allowed = sum(x["allowed"] for x in out)
    unknown = stats.get(None)
    return {
        **p.head(),
        "areasIncomplete": areas_incomplete,
        "totals": {
            "calls": sum_calls,
            "allowed": sum_allowed,
            "denied": sum_calls - sum_allowed,
            "grantsChecks": sum(x["grantsChecks"] for x in out),
            "unknownCalls": _counts(unknown)["calls"],
            "tokensUsed": sum(1 for x in out if x["calls"] and x["status"] not in ("deleted", "unknown")),
            "tokensTotal": len(tokens),
        },
        "series": p.series(slots),
        "tokens": out,
    }


@router.get("/tokens/{token_id}")
async def usage_token(request: Request, token_id: str, _sess: FullSession, range_: Range = Query(default="7d", alias="range")):
    """One token's calls in the period: areas with levels, addresses, calls over time.

    token_id is a token id, or "unknown" for calls with a token the service did not know.
    """
    if token_id == "unknown":
        tid: str | None = None
    else:
        try:
            tid = str(uuid.UUID(token_id))
        except ValueError as e:
            raise HTTPException(404, "Token not found") from e
        if tid != token_id:
            raise HTTPException(404, "Token not found")
    async with report_database(request.app.state.db) as db:
        return await _token_usage(db, _Period(range_), tid)


async def _token_usage(db, p: "_Period", tid: str | None) -> dict[str, Any]:
    p.valid_from = await summary_valid_from(db)
    totals = (await _query(db, p, f"SELECT {_TOTALS_COLS} FROM {{src}}", tid))[0]
    token = None
    if tid is not None:
        token = await db.fetchrow("SELECT id, name, is_active, expires_at FROM tokens WHERE id = $1::uuid", tid)
    has_rows = bool(totals and ((totals["calls"] or 0) or (totals["grants_checks"] or 0)))
    if tid is not None and token is None and not has_rows:
        raise HTTPException(404, "Token not found")
    if tid is None:
        name, status = "Unknown token", "unknown"
    elif token is not None:
        name, status = token["name"], _status(token)
    else:
        newest = await _query(
            db, p,
            "SELECT token_name FROM {src} WHERE token_name IS NOT NULL ORDER BY name_at DESC, name_id DESC LIMIT 1",
            tid,
        )
        name, status = (newest[0]["token_name"] if newest else None) or "Deleted token", "deleted"

    area_rows = await _query(
        db, p,
        f"SELECT area, level, SUM(w) AS calls, {_ALLOWED_OF_CALLS} AS allowed "
        f"FROM {{src}} WHERE {_CALL} GROUP BY area, level "
        f"ORDER BY calls DESC LIMIT {MAX_AREA_GROUPS + 1}",
        tid,
    )
    areas_incomplete = len(area_rows) > MAX_AREA_GROUPS
    groups: dict[str, _AreaGroup] = {}
    if not areas_incomplete:
        for r in area_rows:
            groups.setdefault(_area_key(r["area"]), _AreaGroup()).add(
                r["area"], int(r["calls"]), int(r["allowed"] or 0), r["level"]
            )
    ranked = _ranked(groups)

    addr_rows = await _query(
        db, p,
        f"SELECT ip_address, SUM(w) AS calls, {_ALLOWED_OF_CALLS} AS allowed, "
        f"MAX(last_at) AS last_call_at FROM {{src}} WHERE {_CALL} "
        f"GROUP BY ip_address ORDER BY calls DESC, ip_address LIMIT {DETAIL_TOP_ADDRESSES}",
        tid,
    )
    addresses = []
    for r in addr_rows:
        text, truncated = _cut(r["ip_address"] or "unknown")
        calls = int(r["calls"])
        addresses.append({
            "address": text,
            "truncated": truncated,
            "calls": calls,
            "denied": calls - int(r["allowed"] or 0),
            "lastCallAt": fmt_datetime(r["last_call_at"]),
        })

    slots = await _slots(db, p, tid)
    return {
        **p.head(),
        "token": {"id": tid, "name": name, "status": status},
        "totals": {
            **_counts(totals),
            "ipCount": int(totals["ip_count"] or 0) if totals else 0,
            "lastCallAt": fmt_datetime(totals["last_call_at"]) if totals else None,
        },
        "areasIncomplete": areas_incomplete,
        "areasCount": None if areas_incomplete else len(ranked),
        "areas": None if areas_incomplete else [g.out(True) for g in ranked[:DETAIL_TOP_AREAS]],
        "addresses": addresses,
        "series": p.series(slots),
    }

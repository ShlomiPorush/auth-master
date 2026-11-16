"""Moving a running installation from its SQLite file to an empty PostgreSQL database
(Settings > Database, "Move to PostgreSQL").

The service keeps serving while the data is copied; requests wait a few seconds only at
the switch.  Order of work (run_move):

  1. Checks (check_move, also run again at the start): the target is empty or holds an
     unfinished copy of this installation (a `pending` installation row with this id, left
     by an earlier attempt that never switched), it is a writable primary in UTF8 with fsync
     on, and every source row can be copied (MAP below, the conversion rules in _convert).
  2. Background work stops for the whole move.  From then on the log tables and the
     settings histories only get new rows (INSERT), at growing rowids, and the Usage summary
     does not change, so they are copied while serving, in passes up to a bound fixed at
     each pass's start.
  3. One PostgreSQL transaction on one connection holds everything; nothing is visible
     before its COMMIT.  A rehearsal of the final step runs in a savepoint and must finish
     within FINAL_STEP_LIMIT_SEC.
  4. The switch (Runtime.switch): with the request gate closed, the small tables that
     requests change (MUTABLE) are copied whole and the new log rows added; the
     installation row goes in as `pending` with this move's id; COMMIT.  A failure up to
     here leaves the service on SQLite and changes nothing there.
  5. The commit point: the old SQLite file is fenced (fence_file): its installation row
     becomes `pending` with the move's id and triggers refuse every write, durably
     (synchronous FULL).  From here on the service never serves that file again.
  6. data/database.json names PostgreSQL (with migrationId and movedFromSqlite), the
     PostgreSQL installation row is activated as its first write, and the service attaches.
     A failure from step 5 on leads to recovery mode; start-up and the recovery retry
     complete the move (Runtime._complete_move).

SQLite is read through its own connection of the standard sqlite3 module, in a worker
thread, with SQLITE_LIMIT_LENGTH set to ROW_LIMIT_BYTES: SQLite refuses a larger row
instead of returning it, so one oversized row cannot exhaust memory, and each chunk stays
below CHUNK_ROWS rows or about CHUNK_BYTES + ROW_LIMIT_BYTES bytes.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from app import db_config, db_inspect, installation
from app.db_config import Config, PgParams
from app.db_runtime import READY, Job, NotFenced, OperationError, Runtime
from app.schema import INSTALLATION_TABLE, SERVICE_TABLES

UUID, TEXT, BOOL, TS, JSONB, INT, BIGINT = "uuid", "text", "boolean", "timestamptz", "jsonb", "integer", "bigint"

_SETTINGS_HISTORY = (("version", INT), ("value", TEXT), ("actor", TEXT), ("ip_address", TEXT), ("created_at", TS))

# Every column of every service table and its PostgreSQL type.  test_database_move checks
# it against the schema both backends create; the move checks it against the source file
# and the created target, and refuses on any difference.
MAP: dict[str, tuple[tuple[str, str], ...]] = {
    "admin_users": (("id", UUID), ("username", TEXT), ("password_hash", TEXT), ("totp_secret_enc", TEXT),
                    ("totp_enabled", BOOL), ("created_at", TS)),
    "tokens": (("id", UUID), ("name", TEXT), ("token_hash", TEXT), ("token_enc", TEXT), ("grants", JSONB),
               ("expires_at", TS), ("is_active", BOOL), ("created_at", TS), ("last_used_at", TS),
               ("description", TEXT)),
    "zones": (("id", UUID), ("name", TEXT), ("description", TEXT), ("created_at", TS)),
    "api_keys": (("id", UUID), ("name", TEXT), ("key_hash", TEXT), ("key_enc", TEXT), ("scopes", TEXT),
                 ("created_at", TS)),
    "activity_logs": (("id", UUID), ("actor", TEXT), ("action", TEXT), ("entity_type", TEXT), ("entity_id", TEXT),
                      ("entity_name", TEXT), ("details", TEXT), ("ip_address", TEXT), ("created_at", TS)),
    "access_logs": (("id", UUID), ("token_id", TEXT), ("token_name", TEXT), ("area", TEXT), ("level", TEXT),
                    ("result", BOOL), ("ip_address", TEXT), ("created_at", TS), ("rolled", BOOL)),
    "access_log_rollup": (("slot", TS), ("token_id", TEXT), ("area", TEXT), ("level", TEXT), ("result", BOOL),
                          ("ip_address", TEXT), ("calls", INT), ("last_at", TS), ("token_name", TEXT),
                          ("name_at", TS), ("name_id", UUID)),
    "access_log_rollup_state": (("id", INT), ("valid_from", TS)),
    "rate_limit_settings": _SETTINGS_HISTORY,
    "app_settings": _SETTINGS_HISTORY,
    "redis_settings": _SETTINGS_HISTORY,
    "app_state": (("key", TEXT), ("value", TEXT)),
    "passkeys": (("id", UUID), ("admin_id", UUID), ("credential_id", TEXT), ("public_key", TEXT),
                 ("sign_count", BIGINT), ("transports", TEXT), ("backed_up", BOOL), ("rp_id", TEXT),
                 ("name", TEXT), ("created_at", TS), ("last_used_at", TS)),
    "passkey_settings": (("version", INT), ("origin", TEXT), ("actor", TEXT), ("ip_address", TEXT),
                         ("created_at", TS)),
}

# information_schema udt_name for each type.
_UDT = {UUID: "uuid", TEXT: "text", BOOL: "bool", TS: "timestamptz", JSONB: "jsonb", INT: "int4", BIGINT: "int8"}

# Rows only added while background work is stopped (requests and jobs only INSERT; purge
# and the Usage summary are background work): copied by rowid while serving.  The
# settings histories use `version INTEGER PRIMARY KEY`, so rowid is the version.
APPEND = ("access_logs", "activity_logs", "rate_limit_settings", "app_settings", "redis_settings", "passkey_settings")
# Written only by the Usage summary (stopped): copied once while serving.
FROZEN = ("access_log_rollup",)
# Changed by requests: copied whole inside the closed gate.  Insert order; deleted in reverse.
MUTABLE = ("admin_users", "passkeys", "zones", "tokens", "api_keys", "app_state", "access_log_rollup_state")
# NUL and undecodable text are replaced (and counted) only here (D9); elsewhere refused.
LOG_TABLES = ("access_logs", "activity_logs")
# Tables whose rowids SQLite assigns (no INTEGER PRIMARY KEY): near the largest rowid SQLite
# picks random free ones, which the watermarks cannot follow.
AUTO_ROWID = ("access_logs", "activity_logs", "access_log_rollup")
KEY = {t: "version" for t in ("rate_limit_settings", "app_settings", "redis_settings", "passkey_settings")}
KEY.update({"app_state": "key", "access_log_rollup": "rowid"})

ROW_LIMIT_BYTES = 1024 * 1024
CHUNK_ROWS = 1000
CHUNK_BYTES = 8 * 1024 * 1024
ROWID_CEILING = 2 ** 62
ROWID_MAX = 2 ** 63 - 1
TAIL_ROWS = 5000
MAX_PASSES = 10
BULK_LIMIT_SEC = 60 * 60
FINAL_STEP_LIMIT_SEC = 3.0
SERVER_STATEMENT_TIMEOUT_SEC = 120
SERVER_IDLE_TIMEOUT_SEC = 120
PG_PREFETCH = 1000
# pg_advisory_xact_lock key: two moves into one database never run at once.
ADVISORY_KEY = 0x6D6F7665  # "move"

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_INT_RANGE = {INT: (-(2 ** 31), 2 ** 31 - 1), BIGINT: (-(2 ** 63), 2 ** 63 - 1)}

assert set(MAP) == set(SERVICE_TABLES)
assert set(APPEND) | set(FROZEN) | set(MUTABLE) == set(SERVICE_TABLES)

MESSAGES = {
    "not_movable": "Only a database the service uses from data/database.json as SQLite can be moved.",
    "target_not_empty": "The PostgreSQL database is not empty. Use an empty database.",
    "target_in_use": "The PostgreSQL database already holds this installation in use. Nothing to move.",
    "target_not_durable": "The PostgreSQL server runs with fsync off, so committed data can be lost. Use a server with fsync on.",
    "target_encoding": "The PostgreSQL database does not use UTF8, so it cannot store every text the service receives. Create it with ENCODING 'UTF8'.",
    "schema_mismatch": "The PostgreSQL tables created for the move do not match what this version expects.",
    "source_schema_mismatch": "The SQLite file has a column this version does not know, or lacks one it needs.",
    "rowid_exhausted": "A log table of the SQLite file has row numbers near SQLite's limit, so new rows cannot be followed during a move.",
    "row_too_large": "A row of the SQLite file is larger than the move allows.",
    "row_refused": "PostgreSQL refused a row.",
    "row_invalid": "A value in the SQLite file cannot be moved.",
    "compare_failed": "The copy in PostgreSQL differs from the SQLite data.",
    "too_many_changes": "Log entries arrive faster than they can be copied. Try again at a quieter time.",
    "too_large_for_live_move": "The tables that requests change are too large to switch within a few seconds.",
    "move_failed": "The move did not finish. The service stays on SQLite and nothing changed there.",
    "move_source_mismatch": "The old SQLite file belongs to another installation or another move, so it was not changed.",
}


class SourceMismatch(Exception):
    """The SQLite file is not the one this move came from (fence_file)."""


class _TooBig(Exception):
    def __init__(self, after: int) -> None:
        self.after = after


def _error(code: str, text: str | None = None, **extra: Any) -> OperationError:
    return OperationError(code, text or MESSAGES[code], **extra)


def _qi(name: str) -> str:
    assert re.fullmatch(r"[a-z_]+", name), name
    return f'"{name}"'


# ── Conversion ──────────────────────────────────────────────────

@dataclass
class Counts:
    rows: dict[str, int] = field(default_factory=dict)
    replaced: dict[str, int] = field(default_factory=dict)  # log rows whose text was changed

    def add(self, table: str, n: int) -> None:
        self.rows[table] = self.rows.get(table, 0) + n


def _reject_constant(name: str) -> Any:
    raise ValueError(name)


def _json_ok(value: Any) -> bool:
    """jsonb refuses NUL and unpaired surrogates in strings (keys too)."""
    stack = [value]
    while stack:
        v = stack.pop()
        if isinstance(v, str):
            if "\x00" in v or any(0xD800 <= ord(c) <= 0xDFFF for c in v):
                return False
        elif isinstance(v, dict):
            stack.extend(v.keys())
            stack.extend(v.values())
        elif isinstance(v, list):
            stack.extend(v)
    return True


def _convert(table: str, row: tuple, counts: Counts | None) -> tuple:
    """A source row (rowid first, then MAP's columns, text as bytes) as PostgreSQL values.
    Raises OperationError naming table, row and column."""
    cols = MAP[table]
    out = []
    changed = False
    key = _row_key(table, row)
    for (name, typ), raw in zip(cols, row[1:]):
        def bad(what: str) -> OperationError:
            return _error("row_invalid", f"{table}, row {key}, column {name}: {what}. Fix or delete the row, then try again.",
                          table=table, row=key, column=name)

        if raw is None:
            out.append(None)
            continue
        if typ in (INT, BIGINT):
            if type(raw) is not int:
                raise bad("not a whole number")
            lo, hi = _INT_RANGE[typ]
            if not lo <= raw <= hi:
                raise bad("number out of range")
            out.append(raw)
            continue
        if typ == BOOL:
            if type(raw) is not int or raw not in (0, 1):
                raise bad("not 0 or 1")
            out.append(bool(raw))
            continue
        if type(raw) is int:
            raw = str(raw).encode()
        if not isinstance(raw, (bytes, bytearray)):
            raise bad("unexpected value type")
        try:
            text = bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            if typ != TEXT or table not in LOG_TABLES:
                raise bad("not valid UTF-8 text") from None
            text, changed = bytes(raw).decode("utf-8", "replace"), True
        if "\x00" in text:
            if typ != TEXT or table not in LOG_TABLES:
                raise bad("contains a NUL character")
            text, changed = text.replace("\x00", "�"), True
        if typ == TEXT:
            out.append(text)
        elif typ == UUID:
            if not _UUID_RE.match(text):
                raise bad("not a lowercase uuid")
            out.append(uuid.UUID(text))
        elif typ == TS:
            try:
                ts = datetime.fromisoformat(text)
            except ValueError:
                raise bad("not a date and time") from None
            out.append(ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc))
        elif typ == JSONB:
            try:
                ok = _json_ok(json.loads(text, parse_constant=_reject_constant))
            except (ValueError, RecursionError):
                ok = False
            if not ok:
                raise bad("not JSON that PostgreSQL accepts")
            out.append(text)
        else:  # pragma: no cover - MAP holds only the types above
            raise bad("unknown type")
    if changed and counts is not None:
        counts.replaced[table] = counts.replaced.get(table, 0) + 1
    return tuple(out)


def _row_key(table: str, row: tuple) -> str:
    k = KEY.get(table, "id")
    if k == "rowid":
        return f"rowid {row[0]}"
    names = [c for c, _t in MAP[table]]
    v = row[1 + names.index(k)]
    if isinstance(v, (bytes, bytearray)):
        v = bytes(v).decode("utf-8", "replace")
    return f"{k} {v}"


def _norm(typ: str, v: Any) -> Any:
    if v is None:
        return None
    if typ == UUID:
        return str(v)
    if typ == TS:
        return v.astimezone(timezone.utc).isoformat()
    if typ == JSONB:
        return json.dumps(json.loads(v), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return v


class Digest:
    """Order-independent digest of a multiset of rows (duplicates counted): the sum of each
    row's SHA-256 modulo 2**256, and the number of rows."""

    def __init__(self, table: str) -> None:
        self.types = [t for _c, t in MAP[table]]
        self.total = 0
        self.n = 0

    def add(self, values: tuple) -> None:
        canon = json.dumps([_norm(t, v) for t, v in zip(self.types, values)], ensure_ascii=False, default=str)
        self.total = (self.total + int.from_bytes(hashlib.sha256(canon.encode()).digest(), "big")) % (1 << 256)
        self.n += 1

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Digest) and (self.total, self.n) == (other.total, other.n)


# ── Source: the SQLite file, read in a worker thread ────────────

class Source:
    """Read-only access to the SQLite file with bounded memory (see the module text)."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    async def call(self, fn: Callable[..., Any], *args: Any) -> Any:
        def run() -> Any:
            with self._lock:
                return fn(*args)
        return await asyncio.to_thread(run)

    def _open(self) -> None:
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None, check_same_thread=False)
        try:
            conn.text_factory = bytes
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, ROW_LIMIT_BYTES)
            conn.execute("PRAGMA query_only = ON")
        except BaseException:
            conn.close()
            raise
        self.conn = conn

    async def open(self) -> None:
        await self.call(self._open)

    def interrupt(self) -> None:
        """From any thread: stops the statement running now (sqlite3 documents it)."""
        if self.conn is not None:
            try:
                self.conn.interrupt()
            except Exception:
                pass

    async def close(self) -> None:
        def close() -> None:
            if self.conn is not None:
                self.conn.close()
                self.conn = None
        await self.call(close)

    # The methods below run in the worker thread (through call).

    def begin(self) -> None:
        self.conn.execute("BEGIN")

    def end(self) -> None:
        if self.conn.in_transaction:
            self.conn.execute("COMMIT")

    def columns(self, table: str) -> list[str]:
        rows = self.conn.execute(f"PRAGMA table_xinfo({_qi(table)})").fetchall()
        return [bytes(r[1]).decode() if isinstance(r[1], (bytes, bytearray)) else r[1] for r in rows]

    def tables(self) -> set[str]:
        rows = self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        return {bytes(r[0]).decode() if isinstance(r[0], (bytes, bytearray)) else r[0] for r in rows}

    def max_rowid(self, table: str) -> int:
        return self.conn.execute(f"SELECT COALESCE(MAX(rowid), 0) FROM {_qi(table)}").fetchone()[0]

    def count(self, table: str, lo: int | None, hi: int) -> int:
        """Rows with lo < rowid <= hi; lo None: from the first row (rowids can be 0 or
        negative, SQLite allows any signed 64-bit rowid)."""
        where, args = _range(lo, hi)
        return self.conn.execute(f"SELECT COUNT(*) FROM {_qi(table)} WHERE {where}", args).fetchone()[0]

    def chunk(self, table: str, lo: int | None, hi: int) -> list[tuple]:
        """Rows with lo < rowid <= hi (lo None: from the first) in rowid order: at most
        CHUNK_ROWS rows, stopping once CHUNK_BYTES are held.  Each fetch asks only for as
        many rows as still fit, at ROW_LIMIT_BYTES each, so the chunk stays below
        CHUNK_BYTES + ROW_LIMIT_BYTES."""
        cols = ", ".join(_qi(c) for c, _t in MAP[table])
        where, args = _range(lo, hi)
        rows: list[tuple] = []
        size = 0
        cur = None
        try:
            # execute() already steps to the first row, so a first row that is too large is
            # refused here, not in fetchmany.
            cur = self.conn.execute(f"SELECT rowid, {cols} FROM {_qi(table)} WHERE {where} ORDER BY rowid", args)
            while len(rows) < CHUNK_ROWS and size < CHUNK_BYTES:
                n = max(1, min(100, CHUNK_ROWS - len(rows), (CHUNK_BYTES - size) // ROW_LIMIT_BYTES))
                batch = cur.fetchmany(n)
                if not batch:
                    break
                for r in batch:
                    size += sum(len(v) for v in r if isinstance(v, (bytes, bytearray)))
                rows.extend(batch)
        except (sqlite3.DataError, sqlite3.OperationalError) as e:
            if "too big" not in str(e):
                raise
            raise _TooBig(rows[-1][0] if rows else lo) from None
        finally:
            if cur is not None:
                cur.close()
        return rows

    def oversized_row(self, table: str, after: int | None, hi: int) -> int | None:
        """The rowid of the first row after `after` that SQLite refuses to return whole."""
        cols = ", ".join(_qi(c) for c, _t in MAP[table])
        where, args = _range(after, hi)
        ids = [r[0] for r in self.conn.execute(
            f"SELECT rowid FROM {_qi(table)} WHERE {where} ORDER BY rowid LIMIT ?", (*args, CHUNK_ROWS + 1),
        ).fetchall()]
        for rowid in ids:
            try:
                self.conn.execute(f"SELECT {cols} FROM {_qi(table)} WHERE rowid = ?", (rowid,)).fetchone()
            except (sqlite3.DataError, sqlite3.OperationalError) as e:
                if "too big" in str(e):
                    return rowid
                raise
        return None


def _range(lo: int | None, hi: int) -> tuple[str, tuple]:
    if lo is None:
        return "rowid <= ?", (hi,)
    return "rowid > ? AND rowid <= ?", (lo, hi)


async def _chunks(src: Source, table: str, lo: int | None, hi: int):
    """Async iterator over the chunks of rows lo < rowid <= hi (lo None: from the first)."""
    while lo is None or lo < hi:
        try:
            rows = await src.call(src.chunk, table, lo, hi)
        except _TooBig as e:
            rowid = await src.call(src.oversized_row, table, e.after, hi)
            raise _error(
                "row_too_large",
                f"{table}, rowid {rowid}: the row is larger than {ROW_LIMIT_BYTES // 1024} KiB. "
                "Shorten or delete it, then try again.",
                table=table, row=f"rowid {rowid}",
            ) from None
        if not rows:
            return
        yield rows
        lo = rows[-1][0]


# ── Target: PostgreSQL ──────────────────────────────────────────

async def _connect(p: PgParams) -> Any:
    try:
        return await db_config.connect_postgres(p)
    except Exception as e:
        raise OperationError(db_config.classify(e)) from None


async def _target_checks(conn: Any, installation_id: str) -> tuple[str, list[str]]:
    """("empty" | "earlier_copy", problems).  Raises for a target that cannot be used."""
    from app.db import _PgConn

    db = _PgConn(conn)
    state = await db_inspect.inspect(db)
    if state.kind == db_inspect.EMPTY:
        target = "empty"
    elif state.kind == db_inspect.INSTALLATION and state.installation.id == installation_id:
        if state.installation.status == installation.ACTIVE:
            raise _error("target_in_use")
        target = "earlier_copy"
    elif state.kind == db_inspect.INSTALLATION:
        raise OperationError("other_installation")
    elif state.kind == db_inspect.FOREIGN:
        raise _error("target_not_empty", others=list(state.others))
    else:
        raise _error("target_not_empty")
    problems = await db_inspect.privilege_problems(db, tables_exist=target == "earlier_copy")
    if (await conn.fetchval("SHOW fsync")) != "on":
        raise _error("target_not_durable")
    if (await conn.fetchval("SHOW server_encoding")) != "UTF8":
        raise _error("target_encoding")
    return target, problems


def _source_problems(src: Source) -> None:
    """Runs in the worker thread: columns and rowid limits (P18, P14)."""
    tables = src.tables()
    for table in SERVICE_TABLES:
        if table not in tables:
            raise _error("source_schema_mismatch", f"The SQLite file has no table {table}.")
        have = src.columns(table)
        want = [c for c, _t in MAP[table]]
        extra = [c for c in have if c not in want]
        missing = [c for c in want if c not in have]
        if extra or missing:
            raise _error(
                "source_schema_mismatch",
                f"{table}: " + "; ".join(
                    ([f"unknown column {', '.join(extra)}"] if extra else [])
                    + ([f"missing column {', '.join(missing)}"] if missing else [])
                ) + ". This version cannot move it.",
            )
    for table in AUTO_ROWID:
        if src.max_rowid(table) > ROWID_CEILING:
            raise _error("rowid_exhausted", f"{table}: {MESSAGES['rowid_exhausted']}")


def _scan(src: Source, tables: tuple[str, ...]) -> dict[str, int]:
    """Runs in the worker thread: converts every row of `tables` (refusals name the row)."""
    counts: dict[str, int] = {}
    for table in tables:
        lo, hi, n = None, src.max_rowid(table), 0
        while lo is None or lo < hi:
            try:
                rows = src.chunk(table, lo, hi)
            except _TooBig as e:
                rowid = src.oversized_row(table, e.after, hi)
                raise _error(
                    "row_too_large",
                    f"{table}, rowid {rowid}: the row is larger than {ROW_LIMIT_BYTES // 1024} KiB. "
                    "Shorten or delete it, then try again.",
                ) from None
            if not rows:
                break
            for r in rows:
                _convert(table, r, None)
            n += len(rows)
            lo = rows[-1][0]
        counts[table] = n
    return counts


@dataclass
class CheckView:
    target: str
    problems: list[str]
    counts: dict[str, int]

    def view(self) -> dict[str, Any]:
        # The log tables are counted, not read: reading every log row takes minutes on a large
        # installation; the move reads each one and stops before anything changes on a problem.
        return {"target": self.target, "problems": self.problems, "counts": self.counts,
                "uncheckedTables": list(LOG_TABLES)}


def _movable(rt: Runtime) -> Config:
    from app.config import get_settings

    cfg = rt.config
    if (rt.mode != READY or rt.source != "file" or cfg is None or cfg.backend != db_config.SQLITE
            or not cfg.sqlite_path or get_settings().env_value("database_url") is not None):
        raise _error("not_movable")
    return cfg


async def check_move(rt: Runtime, params: PgParams) -> CheckView:
    """Read-only: the target and the source as the move will find them."""
    cfg = _movable(rt)
    conn = await _connect(params)
    try:
        try:
            target, problems = await _target_checks(conn, cfg.installation_id)
        except OperationError:
            raise
        except Exception as e:
            raise OperationError(db_config.classify(e)) from None
    finally:
        conn.terminate()
    src = Source(cfg.sqlite_path)
    await src.open()
    try:
        await src.call(_source_problems, src)

        def scan() -> dict[str, int]:
            # No transaction around it: each chunk is its own short read, so the check holds
            # no snapshot that would keep the WAL from being reset while requests write.
            counts = _scan(src, MUTABLE + FROZEN + ("rate_limit_settings", "app_settings", "redis_settings", "passkey_settings"))
            for t in LOG_TABLES:
                counts[t] = src.count(t, None, src.max_rowid(t))
            return counts

        counts = await src.call(scan)
    finally:
        await src.close()
    return CheckView(target, problems, counts)


# ── Copy ────────────────────────────────────────────────────────

async def _copy_rows(conn: Any, table: str, rows: list[tuple], counts: Counts, digest: Digest | None) -> None:
    """Converted rows into PostgreSQL in one savepoint; a refused chunk is retried row by
    row (each in its own savepoint) to name the refused row."""
    import asyncpg

    # PostgresError: refused by the server; ValueError: asyncpg's own input check (its
    # client-side DataError is a ValueError).
    refused = (asyncpg.PostgresError, ValueError)
    values = [_convert(table, r, counts) for r in rows]
    cols = [c for c, _t in MAP[table]]
    try:
        async with conn.transaction():
            await conn.copy_records_to_table(table, records=values, columns=cols)
    except refused:
        for raw, v in zip(rows, values):
            try:
                async with conn.transaction():
                    await conn.copy_records_to_table(table, records=[v], columns=cols)
            except refused as e:
                column = getattr(e, "column_name", None)
                code = getattr(e, "sqlstate", None) or type(e).__name__
                raise _error(
                    "row_refused",
                    f"PostgreSQL refused {table}, {_row_key(table, raw)}"
                    + (f", column {column}" if column else "") + f" (error {code}). Fix or delete the row, then try again.",
                    table=table,
                ) from None
        raise _error("row_refused", f"PostgreSQL refused a chunk of {table}, but no single row of it.") from None
    counts.add(table, len(values))
    if digest is not None:
        for v in values:
            digest.add(v)


async def _pg_digest(conn: Any, table: str) -> Digest:
    """The digest of the table in PostgreSQL, read with a cursor whose batches stay near
    CHUNK_BYTES: the batch size comes from the largest row's text length (octet_length,
    uncompressed), measured first."""
    d = Digest(table)
    cols = ", ".join(_qi(c) for c, _t in MAP[table])
    widths = " + ".join(f"COALESCE(octet_length({_qi(c)}::text), 0)" for c, _t in MAP[table])
    widest = await conn.fetchval(f"SELECT COALESCE(MAX({widths}), 0) FROM {_qi(table)}")
    prefetch = max(1, min(PG_PREFETCH, CHUNK_BYTES // max(1, widest)))
    async for r in conn.cursor(f"SELECT {cols} FROM {_qi(table)}", prefetch=prefetch):
        d.add(tuple(r))
    return d


async def _compare(conn: Any, table: str, source: Digest) -> None:
    if await _pg_digest(conn, table) != source:
        raise _error("compare_failed", f"The copy of {table} in PostgreSQL differs from the SQLite data.")


async def _check_target_types(conn: Any) -> None:
    rows = await conn.fetch(
        "SELECT table_name, column_name, udt_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = ANY($1::text[])",
        list(SERVICE_TABLES),
    )
    have: dict[str, dict[str, str]] = {}
    for r in rows:
        have.setdefault(r["table_name"], {})[r["column_name"]] = r["udt_name"]
    for table, cols in MAP.items():
        want = {c: _UDT[t] for c, t in cols}
        if have.get(table) != want:
            raise _error("schema_mismatch", f"{MESSAGES['schema_mismatch']} ({table})")


@dataclass
class Move:
    rt: Runtime
    cfg: Config
    params: PgParams
    conn: Any
    src: Source
    migration_id: str
    counts: Counts = field(default_factory=Counts)
    marks: dict[str, int] = field(default_factory=dict)  # watermark: highest rowid copied
    final_step_sec: float = 0.0

    async def bulk(self, job: Job) -> None:
        started = time.monotonic()
        for table in FROZEN:
            hi = await self.src.call(self.src.max_rowid, table)
            d = Digest(table)
            async for rows in _chunks(self.src, table, None, hi):
                await _copy_rows(self.conn, table, rows, self.counts, d)
            await self._complete(table, d, hi)
        digests = {t: Digest(t) for t in APPEND}
        self.marks = {t: None for t in APPEND}
        for n in range(MAX_PASSES):
            bounds = {t: await self.src.call(self.src.max_rowid, t) for t in APPEND}
            if n > 0:
                new = {t: await self.src.call(self.src.count, t, self.marks[t], bounds[t]) for t in APPEND}
                if all(v <= TAIL_ROWS for v in new.values()):
                    break
            for table in APPEND:
                job.step = f"copying {table}"
                async for rows in _chunks(self.src, table, self.marks[table], bounds[table]):
                    await _copy_rows(self.conn, table, rows, self.counts, digests[table])
                    if time.monotonic() - started > BULK_LIMIT_SEC:
                        raise _error("too_many_changes")
                self.marks[table] = bounds[table]
        else:
            raise _error("too_many_changes")
        job.step = "comparing the copy"
        for table in APPEND:
            if self.marks[table] is None:  # an empty table: nothing copied yet
                self.marks[table] = await self.src.call(self.src.max_rowid, table)
            await self._complete(table, digests[table], self.marks[table])

    async def _complete(self, table: str, d: Digest, hi: int) -> None:
        """The copy equals what was read (digest), and what was read is every source row up
        to `hi` (count)."""
        if await self.src.call(self.src.count, table, None, hi) != d.n:
            raise _error("compare_failed", f"Not every row of {table} was read from the SQLite file.")
        await _compare(self.conn, table, d)

    async def final_step(self, bounds: dict[str, int] | None = None) -> None:
        """Mutable tables whole, new rows of the append tables, zone index.  `bounds` fixes
        the append ranges (the rehearsal); inside the closed gate they are read here."""
        from app.db import _PgConn
        from app.zones import ensure_zone_name_index

        for table in reversed(MUTABLE):
            await self.conn.execute(f"DELETE FROM {_qi(table)}")
        # One SQLite read transaction for all mutable tables: they are consistent with each
        # other (passkeys and their admin) even while requests still write (the rehearsal).
        await self.src.call(self.src.begin)
        try:
            for table in MUTABLE:
                hi = await self.src.call(self.src.max_rowid, table)
                d = Digest(table)
                async for rows in _chunks(self.src, table, None, hi):
                    await _copy_rows(self.conn, table, rows, self.counts, d)
                await self._complete(table, d, hi)
            if bounds is None:
                bounds = {t: await self.src.call(self.src.max_rowid, t) for t in APPEND}
        except BaseException:
            # Not awaited: on a deadline the caller must not wait for a read still running;
            # it is interrupted, and the transaction ends once it has stopped.
            self.src.interrupt()
            asyncio.ensure_future(self.src.call(self.src.end))
            raise
        await self.src.call(self.src.end)
        for table in APPEND:
            if table in AUTO_ROWID and bounds[table] >= ROWID_MAX:
                raise _error("rowid_exhausted", f"{table}: {MESSAGES['rowid_exhausted']}")
            before = self.counts.rows.get(table, 0)
            async for rows in _chunks(self.src, table, self.marks[table], bounds[table]):
                await _copy_rows(self.conn, table, rows, self.counts, None)
            expected = await self.src.call(self.src.count, table, self.marks[table], bounds[table])
            if self.counts.rows.get(table, 0) - before != expected:
                raise _error("compare_failed", f"The new rows of {table} were not all copied.")
        await ensure_zone_name_index(_PgConn(self.conn))

    async def rehearse(self) -> None:
        """The final step in a savepoint, rolled back, within FINAL_STEP_LIMIT_SEC."""
        bounds = {t: await self.src.call(self.src.max_rowid, t) for t in APPEND}
        saved = (dict(self.counts.rows), dict(self.counts.replaced))

        class _Done(Exception):
            pass

        async def go() -> float:
            t0 = time.monotonic()
            try:
                async with self.conn.transaction():
                    await self.final_step(bounds)
                    raise _Done
            except _Done:
                pass
            return time.monotonic() - t0

        task = asyncio.ensure_future(go())
        done, _ = await asyncio.wait({task}, timeout=FINAL_STEP_LIMIT_SEC)
        if not done:
            # Closing the socket ends every wait on it at once; the whole transaction is gone.
            self.conn.terminate()
            self.src.interrupt()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise _error("too_large_for_live_move", self._too_large_text())
        task.result()
        self.counts.rows, self.counts.replaced = saved

    def _too_large_text(self) -> str:
        sizes = ", ".join(f"{t} {self.counts.rows.get(t, 0)}" for t in MUTABLE if self.counts.rows.get(t))
        return (f"{MESSAGES['too_large_for_live_move']} The final step took longer than "
                f"{FINAL_STEP_LIMIT_SEC:g} seconds" + (f" ({sizes})." if sizes else "."))


# ── The fence of the old SQLite file ────────────────────────────

FENCE_MESSAGE = "moved to PostgreSQL"


def fence_file(path: str, installation_id: str, migration_id: str) -> str:
    """Fence the SQLite file at `path` (idempotent).  "fenced", "already" or "missing".
    Raises SourceMismatch, without changing anything, for a file of another installation or
    one fenced by another move."""
    if not os.path.exists(path):
        return "missing"
    conn = sqlite3.connect(path, timeout=5, isolation_level=None)
    try:
        # A commit is durable once it returns (SQLite documents FULL as durable in WAL mode).
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("BEGIN IMMEDIATE")
        try:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            row = conn.execute("SELECT id, status, migration_id FROM installation").fetchone() \
                if INSTALLATION_TABLE in names else None
            if row is None or row[0] != installation_id:
                raise SourceMismatch()
            if row[1] == installation.ACTIVE and row[2] is None:
                conn.execute("UPDATE installation SET status = ?, migration_id = ?", (installation.PENDING, migration_id))
                result = "fenced"
            elif row[1] == installation.PENDING and row[2] == migration_id:
                result = "already"
            else:
                raise SourceMismatch()
            for table in SERVICE_TABLES:
                if table not in names:
                    continue
                for op in ("INSERT", "UPDATE", "DELETE"):
                    conn.execute(
                        f"CREATE TRIGGER IF NOT EXISTS moved_{table}_{op.lower()} BEFORE {op} ON {_qi(table)} "
                        f"BEGIN SELECT RAISE(ABORT, '{FENCE_MESSAGE}'); END"
                    )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return result


def fence_state(path: str, migration_id: str) -> bool:
    """Whether the file at `path` is fenced by this move (read back after a failed fence)."""
    conn = sqlite3.connect(path, timeout=5, isolation_level=None)
    try:
        row = conn.execute("SELECT status, migration_id FROM installation").fetchone()
        return row is not None and row[0] == installation.PENDING and row[1] == migration_id
    finally:
        conn.close()


def fenced_by(path: str, migration_id: str, installation_id: str) -> bool:
    """Whether the SQLite file at `path` is this installation's, fenced by this move."""
    if not os.path.isfile(path):
        return False
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return False
    try:
        row = conn.execute("SELECT id, status, migration_id FROM installation").fetchone()
        return row is not None and row == (installation_id, installation.PENDING, migration_id)
    except sqlite3.Error:
        return False
    finally:
        conn.close()


# ── The move ────────────────────────────────────────────────────

async def run_move(rt: Runtime, app: Any, job: Job, params: PgParams, actor: str, ip: str | None) -> dict[str, Any]:
    from app.db import _PgConn
    from app.db_operations import _close, _log, _public
    from app.logger import log_activity
    from app.schema import ensure_database_schema

    cfg = _movable(rt)
    assert cfg.sqlite_path is not None
    job.step = "connecting"
    conn = await _connect(params)
    src = Source(cfg.sqlite_path)
    move = Move(rt, cfg, params, conn, src, str(uuid.uuid4()))
    stopped = False
    new_db = None
    try:
        await src.open()
        target, problems = await _target_checks(conn, cfg.installation_id)
        if problems:
            raise OperationError("permission_denied", "The database user cannot be used: " + " ".join(problems),
                                 problems=problems)
        await src.call(_source_problems, src)

        job.step = "stopping background work"
        try:
            await rt._stop_background()
        except BaseException:
            # Tasks that ended are started again once the slow ones have ended too.
            rt._resume_background(app)
            raise
        stopped = True
        # Statements a stopped task left on the shared connections end first: aiosqlite runs
        # each connection's statements one at a time, in order.
        await rt.db.fetchval("SELECT 1")
        if rt.settings_db is not None and rt.settings_db is not rt.db:
            await rt.settings_db.fetchval("SELECT 1")

        job.step = "creating the tables"
        await conn.execute("SET synchronous_commit = on")
        # Server-side limits (PostgreSQL's documented settings), so a statement or an open
        # transaction that this process lost contact with ends on the server too.
        await conn.execute(f"SET statement_timeout = '{SERVER_STATEMENT_TIMEOUT_SEC}s'")
        await conn.execute(f"SET idle_in_transaction_session_timeout = '{SERVER_IDLE_TIMEOUT_SEC}s'")
        tx = conn.transaction()
        await tx.start()
        await conn.execute("SELECT pg_advisory_xact_lock($1)", ADVISORY_KEY)
        target, _problems = await _target_checks(conn, cfg.installation_id)
        if target == "earlier_copy":
            for table in (*SERVICE_TABLES, INSTALLATION_TABLE):
                await conn.execute(f"DROP TABLE IF EXISTS {_qi(table)} CASCADE")
        await ensure_database_schema(_PgConn(conn), zone_fold_index=False)
        await _check_target_types(conn)
        await conn.execute("DELETE FROM access_log_rollup_state")

        await move.bulk(job)
        job.step = "measuring the switch"
        await move.rehearse()

        try:
            new_db = await db_config.open_postgres(params)
        except Exception as e:
            raise OperationError(db_config.classify(e)) from None
        new_cfg = Config(db_config.POSTGRES, cfg.installation_id, pg=params,
                         migration_id=move.migration_id, moved_from_sqlite=cfg.sqlite_path)

        async def verify() -> None:
            t0 = time.monotonic()
            try:
                job.step = "copying the last changes"
                await move.final_step()
                await installation.create(_PgConn(conn), cfg.installation_id, installation.PENDING, move.migration_id)
                move.final_step_sec = time.monotonic() - t0
                await log_activity(
                    _PgConn(conn), actor=actor, action="database.migrated", entity_type="settings",
                    entity_name="database",
                    details={"from": _public(cfg), "to": _public(new_cfg), "rows": move.counts.rows,
                             "textReplaced": move.counts.replaced,
                             "finalStepMs": round(move.final_step_sec * 1000)},
                    ip_address=ip,
                )
                await tx.commit()
            except BaseException:
                # Deadline or failure: the socket closes at once, the server rolls back, and
                # a SQLite read still running is stopped (the gate does not wait for it).
                conn.terminate()
                src.interrupt()
                raise

        async def fence() -> None:
            try:
                await asyncio.to_thread(fence_file, cfg.sqlite_path, cfg.installation_id, move.migration_id)
            except SourceMismatch:
                raise NotFenced("move_source_mismatch", MESSAGES["move_source_mismatch"]) from None
            except BaseException:
                try:
                    fenced = await asyncio.wait_for(asyncio.to_thread(fence_state, cfg.sqlite_path, move.migration_id), 5)
                except Exception:
                    raise  # unknown: treated as fenced
                if not fenced:
                    raise NotFenced("move_failed", MESSAGES["move_failed"]) from None
                raise

        async def activate() -> None:
            await installation.activate(new_db, cfg.installation_id, move.migration_id)

        # From here the switch owns background work: it starts it again on SQLite when it
        # fails before the commit point, and on PostgreSQL after it.
        stopped = False
        await rt.switch(app, job, new_db=new_db, new_config=new_cfg, verify=verify, fence=fence,
                        before_attach=activate)
        await _close_quietly(conn)
        return {
            "action": "database.migrated",
            "rows": move.counts.rows,
            "textReplaced": move.counts.replaced,
            "finalStepMs": round(move.final_step_sec * 1000),
            "pauseMs": round((rt.last_pause_sec or 0) * 1000),
        }
    except BaseException as e:
        conn.terminate()
        if new_db is not None and rt.db is not new_db and not (isinstance(e, OperationError) and e.code in ("attach_failed", "move_incomplete")):
            await _close(new_db)
        if stopped:
            rt._resume_background(app)
        if rt.db is not None and rt.mode == READY and rt.config is cfg:
            # Still on SQLite: the failure is logged there.
            code = e.code if isinstance(e, OperationError) else "move_failed"
            to = _public(Config(db_config.POSTGRES, cfg.installation_id, pg=params))
            await _log(rt.db, actor, "database.migration_failed", {"to": to, "code": code}, ip)
        if isinstance(e, OperationError):
            raise
        if isinstance(e, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise
        print(f"The move to PostgreSQL failed ({type(e).__name__})")
        raise OperationError(db_config.classify(e)) from None
    finally:
        asyncio.ensure_future(src.close())


async def _close_quietly(conn: Any) -> None:
    try:
        await asyncio.wait_for(conn.close(), 5)
    except Exception:
        conn.terminate()

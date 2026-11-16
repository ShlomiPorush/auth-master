"""The database this process serves, and switching it while the service runs.

Modes:
  ready     a database is attached; everything works.
  setup     no database is configured yet (first start): only first-time setup works.
  recovery  the configured database cannot be used: only /health and the recovery page
            (authenticated with ADMIN_API_KEY) work; the dashboard redirects there.

Start-up (start): DATABASE_URL, when set, is used as in earlier versions.  Otherwise
data/database.json (app.db_config) names the database, whose installation id must match;
without the file, an existing data/auth.db of an earlier version is adopted; with nothing,
the service waits for setup.

A database operation (setup, a connection change, a recovery save, an automatic reconnect)
runs as one background job; a second one while it runs gets 409.  The job holds
Registry.exclusive from its first step, so nothing is created or changed in any database
before this process has proved it is the only one.  The switch itself (switch):
  1. outside the gate: the new database is opened and checked while requests are served,
     and background work is stopped (at most STOP_BACKGROUND_SEC);
  2. the request gate closes: new requests wait, requests in progress finish (at most
     DRAIN_BUDGET_SEC, otherwise the gate reopens and nothing changed); the steps below
     share the rest of SWITCH_BUDGET_SEC;
  3. data/database.json is replaced (the commit point, off the event loop);
  4. the new database is attached (settings loaded, background work started) and the gate
     reopens; requests that waited continue on the new database.
If attaching fails after the commit point, the process enters recovery mode at once
(waiting requests get 503); nothing that was acknowledged is lost, because no request ran
between the drain and the switch.

Switches need this process to be the only one using the data directory: every process
holds a lock on its own file in data/.proc, and a switch refuses while any other file
there is locked (Registry).
"""
from __future__ import annotations

import asyncio
import hmac
import os
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import redis.asyncio as redis_asyncio

from app import db_config, db_inspect, display_settings, general_settings, installation, login_limit, rate_limit_settings, redis_settings
from app.config import GENERATE_HINT, get_settings
from app.db import open_settings_database
from app.schema import ensure_database_schema

READY = "ready"
SETUP = "setup"
RECOVERY = "recovery"

# From the moment the gate closes, draining requests (at most DRAIN_BUDGET_SEC), the last
# check, writing data/database.json and attaching the new database share one deadline.  A
# write the disk has not confirmed by then ends in recovery mode.  After the commit point
# attaching gets at least ATTACH_FLOOR_SEC, because it must finish once the file is
# replaced.  So the gate is closed at most SWITCH_BUDGET_SEC + ATTACH_FLOOR_SEC, and a
# request waiting at it gives up only after GATE_WAIT_SEC, which is longer: a switch that
# succeeds fails no request.
SWITCH_BUDGET_SEC = 10.0
DRAIN_BUDGET_SEC = 5.0
ATTACH_FLOOR_SEC = 3.0
# Under the 15 s after which the dashboard gives up on a read (js/common.js).
GATE_WAIT_SEC = 14.0
STOP_BACKGROUND_SEC = 60.0
RECOVERY_RETRY_SEC = 10.0
STARTUP_LOCK_WAIT_SEC = 120.0
CLOSE_TIMEOUT_SEC = 5.0
# Failures that can pass on their own (the server was not up yet, a network outage): in
# recovery mode the saved connection is tried again every RECOVERY_RETRY_SEC.
RETRYABLE = frozenset({"unreachable", "timeout", "attach_failed", "database_missing", "other", "move_incomplete"})

# Short texts for the fixed failure codes; never the driver's message.
MESSAGES = {
    "unreachable": "The database server cannot be reached.",
    "timeout": "The database server did not answer in time.",
    "tls_failed": "The TLS connection to the database server failed. If the server does not use TLS, choose prefer or disable.",
    "auth_failed": "The database server refused the user name or password.",
    "database_missing": "The database does not exist on the server.",
    "permission_denied": "The database user lacks a permission the service needs.",
    "read_only": "The server is a standby or the session is read-only. Connect to the primary server.",
    "sqlite_missing": "The SQLite database file does not exist.",
    "other_installation": "The database holds a different installation of this service.",
    "older_installation": "The database holds this service's data from an older version or a backup, without an installation id.",
    "database_empty": "The database is empty: it was replaced or emptied.",
    "foreign_database": "The database holds data that is not this service's.",
    "installation_pending": "The database holds an unfinished copy from a move.",
    "incomplete_installation": "The database holds this service's installation id, but tables every installation has are missing: it was restored only in part.",
    "legacy_unrecognised": "data/auth.db exists but holds no data of this service.",
    "attach_failed": "The service could not start on the database.",
    "config_invalid": "data/database.json cannot be read.",
    "config_unreadable": "The saved database password cannot be read: SESSION_SECRET or APP_ENCRYPTION_KEY changed.",
    "config_not_durable": "The new connection was written to data/database.json, but the disk did not confirm it.",
    "other": "The database connection failed.",
    "database_moved": "This SQLite database was moved to PostgreSQL. Enter the PostgreSQL connection on this page.",
    "move_incomplete": "The move to PostgreSQL did not finish switching. The service tries again every few seconds; you can also enter the PostgreSQL connection on this page.",
    "move_source_mismatch": "The SQLite file the move came from belongs to another installation or another move, so it was not changed. Put the original file back, or enter the PostgreSQL connection on this page.",
}


def message(code: str) -> str:
    return MESSAGES.get(code, MESSAGES["other"])


class OperationError(Exception):
    """A database operation stopped without changing anything (unless `code` says so)."""

    def __init__(self, code: str, text: str | None = None, **extra: Any) -> None:
        super().__init__(text or message(code))
        self.code = code
        self.text = text or message(code)
        self.extra = extra


class NotFenced(OperationError):
    """Raised by a switch's `fence` step when it certainly did not happen: the service
    stays on the database it uses."""


# ── Process registry ────────────────────────────────────────────

try:  # POSIX (the image)
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
except ImportError:  # Windows, for local runs
    import msvcrt

    def _try_lock(fd: int) -> bool:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass


class NotRegistered(RuntimeError):
    """This process could not register itself in the data directory."""


class Registry:
    """Each process holds an exclusive lock on data/.proc/<random>.lock for its lifetime.
    exclusive() holds data/.switch.lock and refuses when another process's file is locked;
    files whose lock is free belong to processes that ended and are removed."""

    def __init__(self) -> None:
        self._fd: int | None = None
        self._name: str | None = None
        self._dir: Path | None = None

    @property
    def active(self) -> bool:
        return self._fd is not None

    def register(self, directory: Path) -> bool:
        try:
            proc = directory / ".proc"
            proc.mkdir(parents=True, exist_ok=True)
            name = f"{uuid.uuid4()}.lock"
            fd = os.open(proc / name, os.O_RDWR | os.O_CREAT, 0o600)
            if not _try_lock(fd):
                os.close(fd)
                return False
        except OSError as e:
            print(f"Could not register this process in the data directory ({type(e).__name__}); "
                  "database operations are not available.")
            return False
        self._fd, self._name, self._dir = fd, name, directory
        return True

    def close(self) -> None:
        if self._fd is None or self._dir is None or self._name is None:
            return
        path = self._dir / ".proc" / self._name
        try:
            _unlock(self._fd)
        finally:
            os.close(self._fd)
            self._fd = None
        try:
            path.unlink()
        except OSError:
            pass

    @asynccontextmanager
    async def starting(self, directory: Path):
        """At start: register while holding data/.switch.lock, and keep it while the caller
        reads data/database.json.  A switch holds that lock from its scan of the registry to
        its end, so a process that starts meanwhile waits and then reads the new file, and
        one that registered first makes the switch refuse.  A process that cannot register
        does not start: a switch elsewhere could not see it."""
        not_registered = NotRegistered(
            "Configuration error. The service will not start until these are fixed:\n"
            "  - The data directory (data/) is not writable, so this process cannot register itself "
            "there. Make it writable by the service's user, then start the service again."
        )
        try:
            directory.mkdir(parents=True, exist_ok=True)
            fd = os.open(directory / ".switch.lock", os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            raise not_registered from None
        try:
            deadline = time.monotonic() + STARTUP_LOCK_WAIT_SEC
            while not _try_lock(fd):
                if time.monotonic() > deadline:
                    raise RuntimeError(
                        "Another service process is changing the database of this data directory "
                        f"and did not finish within {int(STARTUP_LOCK_WAIT_SEC)} seconds. Start the service again."
                    )
                await asyncio.sleep(0.2)
            if not self.register(directory):
                raise not_registered
            yield
        finally:
            if fd is not None:
                try:
                    _unlock(fd)
                finally:
                    os.close(fd)

    @contextmanager
    def exclusive(self):
        if self._fd is None or self._dir is None:
            raise OperationError(
                "no_registry",
                "This process could not lock its file in the data directory, so it cannot "
                "prove that it is the only process. Make the data directory writable and restart.",
            )
        fd = os.open(self._dir / ".switch.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if not _try_lock(fd):
                raise OperationError("other_process", OTHER_PROCESS_TEXT)
            try:
                for f in sorted((self._dir / ".proc").glob("*.lock")):
                    if f.name == self._name:
                        continue
                    try:
                        other = os.open(f, os.O_RDWR)
                    except FileNotFoundError:
                        continue  # its process ended during the scan
                    except OSError:
                        # A file this process cannot check may belong to a live process.
                        raise OperationError("other_process", OTHER_PROCESS_TEXT) from None
                    try:
                        if not _try_lock(other):
                            raise OperationError("other_process", OTHER_PROCESS_TEXT)
                        _unlock(other)
                    finally:
                        os.close(other)
                    try:
                        f.unlink()  # its process ended
                    except OSError:
                        pass
                yield
            finally:
                _unlock(fd)
        finally:
            os.close(fd)


OTHER_PROCESS_TEXT = (
    "Another service process uses the same data directory. Database changes need a single "
    "process: stop the other instances (or run one worker), then try again."
)


# ── Request gate ────────────────────────────────────────────────

class Gate:
    """Counts requests in progress; while closed, new requests wait for it to reopen."""

    def __init__(self) -> None:
        self._open = asyncio.Event()
        self._open.set()
        self._idle = asyncio.Event()
        self._idle.set()
        self._inflight = 0

    @property
    def is_open(self) -> bool:
        return self._open.is_set()

    async def enter(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not self._open.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(self._open.wait(), remaining)
            except (asyncio.TimeoutError, TimeoutError):
                return False
        self._inflight += 1
        self._idle.clear()
        return True

    def leave(self) -> None:
        self._inflight -= 1
        if self._inflight <= 0:
            self._inflight = 0
            self._idle.set()

    async def close_and_drain(self, timeout: float) -> bool:
        """Close and wait for the requests in progress; on timeout reopen and return False."""
        self._open.clear()
        try:
            await asyncio.wait_for(self._idle.wait(), timeout)
            return True
        except (asyncio.TimeoutError, TimeoutError):
            self._open.set()
            return False

    def reopen(self) -> None:
        self._open.set()


# ── Jobs ────────────────────────────────────────────────────────

@dataclass
class Job:
    id: str
    kind: str
    state: str = "running"  # running | done | failed
    step: str = "starting"
    result: dict[str, Any] | None = None
    error_code: str | None = None
    error: str | None = None
    error_extra: dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "state": self.state,
            "step": self.step,
            "result": self.result,
            "code": self.error_code,
            "error": self.error,
            **({"details": self.error_extra} if self.error_extra else {}),
        }


class Pending:
    """The place of a change request that has not started its job yet.

    The request marks it before its first wait (the second factor), so a page whose
    answer was lost reads "pending" until the request has started its job or ended.
    Only the request that set the mark clears it (Jobs.clear_pending)."""

    def view(self) -> dict[str, Any]:
        return {"state": "pending"}


class Busy(Exception):
    def __init__(self, job: Job | Pending) -> None:
        super().__init__("a database operation is running")
        self.job = job


class Jobs:
    KEEP = 20

    def __init__(self, guard: Callable[[], Any] | None = None) -> None:
        self.current: Job | None = None
        self._recent: dict[str, Job] = {}
        self._tasks: set[asyncio.Task] = set()
        self._pending: Pending | None = None
        # Held for the whole operation (Registry.exclusive): nothing is created or changed
        # in a database before this process has proved that it is the only one.
        self._guard = guard

    def busy(self) -> Job | Pending | None:
        """The running job, else the pending mark of a request about to start one."""
        if self.current is not None and self.current.state == "running":
            return self.current
        return self._pending

    def mark_pending(self) -> Pending:
        """Hold the place of a job this request will start; Busy when one runs or is pending.
        Synchronous: called before the request's first await."""
        busy = self.busy()
        if busy is not None:
            raise Busy(busy)
        mark = Pending()
        self._pending = mark
        return mark

    def clear_pending(self, mark: Pending) -> None:
        """Called on every exit of the request that set `mark`; another request's mark stays."""
        if self._pending is mark:
            self._pending = None

    def start(self, kind: str, fn: Callable[[Job], Awaitable[dict[str, Any] | None]], *, pending: Pending | None = None) -> Job:
        if self.current is not None and self.current.state == "running":
            raise Busy(self.current)
        if self._pending is not None and self._pending is not pending:
            raise Busy(self._pending)
        job = Job(uuid.uuid4().hex, kind)
        self.current = job
        self._recent[job.id] = job
        while len(self._recent) > self.KEEP:
            self._recent.pop(next(iter(self._recent)))
        task = asyncio.create_task(self._run(job, fn))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def _run(self, job: Job, fn: Callable[[Job], Awaitable[dict[str, Any] | None]]) -> None:
        try:
            if self._guard is None:
                job.result = await fn(job) or {}
            else:
                with self._guard():
                    job.result = await fn(job) or {}
            job.state = "done"
        except OperationError as e:
            job.state, job.error_code, job.error, job.error_extra = "failed", e.code, e.text, e.extra
        except asyncio.CancelledError:
            job.state, job.error_code, job.error = "failed", "cancelled", "The service stopped during the operation."
            raise
        except Exception as e:
            # Only the class: driver messages can carry connection details.
            print(f"Database operation {job.kind} failed ({type(e).__name__})")
            code = db_config.classify(e)
            job.state, job.error_code, job.error = "failed", code, message(code)
        finally:
            job.step = "finished"
            job.finished_at = time.time()

    def get(self, job_id: str) -> Job | None:
        return self._recent.get(job_id)

    async def cancel_all(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            try:
                await task
            except BaseException:
                pass


# ── Recovery attempt limit (in process: Redis may be unreachable) ──

class AttemptLimit:
    MAX_FAILURES = 10
    WINDOW_SEC = 15 * 60

    def __init__(self) -> None:
        self._failures: dict[str, deque[float]] = {}

    def _recent(self, key: str) -> deque[float]:
        q = self._failures.setdefault(key, deque())
        cutoff = time.monotonic() - self.WINDOW_SEC
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def blocked(self, key: str) -> int:
        """Seconds until another attempt is allowed (0: allowed)."""
        q = self._recent(key)
        if len(q) < self.MAX_FAILURES:
            return 0
        return max(1, int(q[0] + self.WINDOW_SEC - time.monotonic()) + 1)

    def fail(self, key: str) -> None:
        self._recent(key).append(time.monotonic())

    def reset(self) -> None:
        self._failures.clear()


def master_key_matches(authorization: str | None) -> bool:
    master = get_settings().admin_api_key
    if not master.strip() or not authorization or not authorization.startswith("Bearer "):
        return False
    return hmac.compare_digest(authorization[7:].strip().encode("utf-8"), master.encode("utf-8"))


# ── The runtime ─────────────────────────────────────────────────

StartBackground = Callable[[Any, Any, Any], list[asyncio.Task]]


@dataclass
class Runtime:
    start_background: StartBackground
    mode: str = SETUP
    failure_code: str | None = None
    failure_text: str | None = None
    source: str | None = None  # "env" | "file"
    config: db_config.Config | None = None
    # The installation id the saved file names, when the file could be read only in part.
    known_installation_id: str | None = None
    # The move from SQLite the saved file records (migrationId, movedFromSqlite), kept the
    # same way when the file could be read only in part (move_record).
    known_migration_id: str | None = None
    known_moved_from: str | None = None
    db: Any = None
    settings_db: Any = None
    close_settings: bool = False
    tasks: list[asyncio.Task] = field(default_factory=list)
    gate: Gate = field(default_factory=Gate)
    jobs: Jobs = field(default_factory=Jobs)
    registry: Registry = field(default_factory=Registry)
    attempts: AttemptLimit = field(default_factory=AttemptLimit)
    retry_task: asyncio.Task | None = None
    redis_ready: bool = False
    # Background tasks that were cancelled but had not ended yet, and the task that
    # restarts background work once they have.
    stopping: list[asyncio.Task] = field(default_factory=list)
    restart_task: asyncio.Task | None = None
    # Connections being closed in the background (attach failures).
    closing: set[asyncio.Task] = field(default_factory=set)
    # In recovery after a move passed its commit point (the fence of the old SQLite file)
    # but data/database.json was not written: the retry writes `config` first.
    unpublished: bool = False
    # How long the request gate was closed in the last switch (seconds).
    last_pause_sec: float | None = None

    def __post_init__(self) -> None:
        self.jobs = Jobs(guard=self.registry.exclusive)

    # ── Start and stop ──

    async def start(self, app: Any, create_database: Callable[[str], Awaitable[Any]]) -> None:
        app.state.runtime = self
        app.state.db = None
        app.state.settings_db = None
        app.state.redis = None
        app.state.login_redis = None
        url = get_settings().env_value("database_url")
        if url is not None:
            # As in earlier versions; the Settings page shows the connection locked.  Registered
            # before the database opens, so a switch by another process on this data directory
            # sees this one; a process that cannot register does not start (NotRegistered).
            async with self.registry.starting(db_config.data_dir()):
                pass
            db = await create_database(url)
            try:
                await _refuse_unfinished(db)
                await ensure_database_schema(db)
                await installation.ensure_row(db)
                await self.attach(app, db, run_schema=False)
            except BaseException:
                await db.close()
                raise
            self.source = "env"
            return

        cfg, error = None, None
        legacy = db_config.default_sqlite_path()
        async with self.registry.starting(db_config.data_dir()):
            # Under the switch lock: a setup in progress elsewhere would hold it.
            _remove_setup_leftovers()
            try:
                cfg = db_config.read_config()
                if cfg is not None:
                    # A file whose write the disk never confirmed (config_not_durable before a
                    # restart) is confirmed now, or the service stays in recovery mode.
                    db_config.confirm_durable()
            except db_config.ConfigError as e:
                error = e
            except OSError:
                error = db_config.ConfigError("config_not_durable", message("config_not_durable"))
                self.config = cfg
            legacy_exists = legacy.exists()
        if error is not None:
            self.known_installation_id = _installation_id_in_file()
            self.known_migration_id, self.known_moved_from = _move_record_in_file()
            await self._enter_recovery(app, error.code, error.message)
            return
        if cfg is not None:
            await self._start_from_config(app, cfg)
            return
        if legacy_exists:
            await self._adopt_legacy(app, legacy)
            return
        self.mode = SETUP
        await self._build_redis_without_database(app, required=True)

    async def stop(self, app: Any) -> None:
        if self.retry_task is not None:
            self.retry_task.cancel()
            try:
                await self.retry_task
            except BaseException:
                pass
        await self.jobs.cancel_all()
        try:
            await self._stop_background()
        except OperationError:
            pass  # shutting down: the tasks were cancelled and end with the process
        await self._close_handles(self.db, self.settings_db, self.close_settings)
        self.db = self.settings_db = None
        for name in ("login_redis", "redis"):
            client = getattr(app.state, name, None)
            if client is not None:
                try:
                    await client.aclose()
                except Exception:
                    pass
        self.registry.close()

    # ── Attaching a database ──

    async def attach(self, app: Any, db: Any, *, run_schema: bool = True, switching: bool = False) -> None:
        """Bring the service up on `db` (start-up and every switch)."""
        if run_schema:
            await ensure_database_schema(db)
        settings_db, close_settings = await open_settings_database(db)
        try:
            # Invalid stored settings stop here: the intended limits are unknown.  After a
            # switch the new database's settings replace the old ones whatever their version.
            await rate_limit_settings.load(settings_db, force=switching)
            # Copies the values the environment sets, before the first purge.
            await general_settings.load(settings_db, db)
            # Never stops the start: unusable stored values give the default formats.
            await display_settings.load(settings_db, db)
            from app.logger import init_purged_before
            from app.zones import copy_allowed_areas_once

            await copy_allowed_areas_once(db)
            await init_purged_before(settings_db, general_settings.current().access_log_retention_days)
            if not self.redis_ready:
                # The Redis client is built once, from the URL in effect now.
                redis_settings.reset_state()
                connection = redis_settings.resolve(await redis_settings.read_current(settings_db))
                _build_redis(app, connection)
                self.redis_ready = True
            await redis_settings.sync_from_environment(settings_db, db)
        except BaseException:
            if close_settings:
                # In the background, bounded: a statement stuck on a slow disk must not hold
                # up the switch's way into recovery mode (asyncio.wait_for waits for this).
                closing = asyncio.ensure_future(self._close_handles(None, settings_db, True))
                self.closing.add(closing)
                closing.add_done_callback(self.closing.discard)
            raise
        self.db, self.settings_db, self.close_settings = db, settings_db, close_settings
        self.mode = READY
        self.failure_code = self.failure_text = None
        app.state.db = db
        app.state.settings_db = settings_db
        self.tasks = self.start_background(app, db, settings_db)

    async def _stop_background(self) -> None:
        """Cancel the background tasks and wait for them, together with tasks of an earlier
        stop that had not ended yet.  On timeout (background_busy) they stay in `stopping`."""
        if self.restart_task is not None:
            self.restart_task.cancel()
            self.restart_task = None
        tasks = self.tasks + self.stopping
        self.tasks, self.stopping = [], []
        for t in tasks:
            t.cancel()
        if not tasks:
            return
        try:
            done, pending = await asyncio.wait(tasks, timeout=STOP_BACKGROUND_SEC)
        except asyncio.CancelledError:
            # Keep the ones still ending, so the next stop waits for them too.
            self.stopping = [t for t in tasks if not t.done()]
            raise
        for t in done:
            if not t.cancelled() and t.exception() is not None:
                print(f"Background task ended with {type(t.exception()).__name__}")
        if pending:
            self.stopping = list(pending)
            raise OperationError(
                "background_busy",
                f"Background work (log cleanup or the usage summary) did not stop within "
                f"{int(STOP_BACKGROUND_SEC)} seconds. Try again in a few minutes.",
            )

    def _resume_background(self, app: Any) -> None:
        """Start background work again on the database in use, once every task of the
        last stop has ended, so no task runs twice."""
        db, settings_db = self.db, self.settings_db
        if db is None:
            return
        if not self.stopping:
            self.tasks = self.start_background(app, db, settings_db)
            return
        waiting = list(self.stopping)

        async def restart() -> None:
            await asyncio.wait(waiting)
            self.stopping = [t for t in self.stopping if t not in waiting]
            if self.db is db and self.mode == READY and not self.tasks:
                self.tasks = self.start_background(app, db, settings_db)

        self.restart_task = asyncio.create_task(restart())

    @staticmethod
    async def _close_handles(db: Any, settings_db: Any, close_settings: bool) -> None:
        """Close, each within CLOSE_TIMEOUT_SEC.  A PostgreSQL pool that does not close in time
        is terminated, as asyncpg documents for Pool.close()."""
        handles = ([settings_db] if close_settings and settings_db is not None else []) + ([db] if db is not None else [])
        for handle in handles:
            try:
                await asyncio.wait_for(handle.close(), CLOSE_TIMEOUT_SEC)
            except (asyncio.TimeoutError, TimeoutError):
                if hasattr(handle, "terminate"):
                    handle.terminate()
            except Exception:
                pass

    async def _build_redis_without_database(self, app: Any, *, required: bool) -> None:
        """Setup and recovery: Redis from REDIS_URL, else REDIS_DEFAULT_URL."""
        if self.redis_ready:
            return
        env = redis_settings.env_url()
        default = redis_settings.default_url()
        url, source = (env, "env") if env is not None else (default, "default")
        if url is None:
            if required:
                raise RuntimeError(
                    "Configuration error. The service will not start until these are fixed:\n"
                    "  - Setup needs Redis: set REDIS_URL (or REDIS_DEFAULT_URL, which the bundled "
                    "docker-compose.yml sets).\n" + GENERATE_HINT
                )
            return
        redis_settings.reset_state()
        connection = redis_settings.Connection(url, source)
        _build_redis(app, connection)
        self.redis_ready = True

    # ── Start-up paths ──

    async def _start_from_config(self, app: Any, cfg: db_config.Config) -> None:
        self.config = cfg
        self.known_installation_id = cfg.installation_id
        try:
            db = await asyncio.wait_for(db_config.open_config(cfg), db_config.CONNECT_TIMEOUT_SEC + 4)
        except FileNotFoundError:
            await self._enter_recovery(app, "sqlite_missing")
            return
        except Exception as e:
            print(f"The configured database cannot be opened ({type(e).__name__})")
            await self._enter_recovery(app, db_config.classify(e))
            return
        try:
            code = await _check_identity(db, cfg.installation_id)
            if code == "installation_pending":
                code = await self._pending(db, cfg)
            if code is None:
                await self.attach(app, db)
                self.source = "file"
                return
        except Exception as e:
            if isinstance(e, RuntimeError):
                print(str(e))  # the service's own text (invalid saved settings, no Redis URL)
            else:
                print(f"The configured database cannot be used ({type(e).__name__})")
            code = "attach_failed"
        await self._close_handles(db, None, False)
        await self._enter_recovery(app, code)

    async def _adopt_legacy(self, app: Any, path: Path) -> None:
        """data/auth.db from an earlier version, without data/database.json: keep using it."""
        db = await db_config.open_sqlite(str(path))
        try:
            state = await db_inspect.inspect(db)
            if state.kind == db_inspect.INSTALLATION and _moved(state.installation):
                await db.close()
                self.known_installation_id = state.installation.id
                self.known_migration_id, self.known_moved_from = state.installation.migration_id, str(path)
                await self._enter_recovery(app, "database_moved")
                return
            if state.kind == db_inspect.INSTALLATION and state.installation.status == installation.ACTIVE:
                iid = state.installation.id
            elif state.kind == db_inspect.OLDER:
                await installation.adopt_sqlite(db, installation.new_id())
                iid = (await installation.read(db)).id
            else:
                await db.close()
                await self._enter_recovery(
                    app, "legacy_unrecognised",
                    "data/auth.db exists but holds no data of this service. Move it out of the "
                    "data directory, then restart the service to run setup.",
                )
                return
            cfg = db_config.Config(db_config.SQLITE, iid, sqlite_path=str(path))
            db_config.publish_config(cfg)
            await self.attach(app, db)
        except BaseException:
            if self.mode != READY:
                await db.close()
            raise
        self.config, self.source = cfg, "file"

    async def _enter_recovery(self, app: Any, code: str, text: str | None = None) -> None:
        self.mode = RECOVERY
        self.failure_code = code
        self.failure_text = text or message(code)
        self.db = self.settings_db = None
        app.state.db = None
        app.state.settings_db = None
        print(f"Recovery mode: {self.failure_text} Open /recovery with ADMIN_API_KEY to fix the connection.")
        await self._build_redis_without_database(app, required=False)
        if self.config is not None and code in RETRYABLE and (self.retry_task is None or self.retry_task.done()):
            self.retry_task = asyncio.create_task(self._retry_loop(app))

    async def _retry_loop(self, app: Any) -> None:
        """While in recovery mode for a failure that can pass, try the saved connection again."""
        while self.mode == RECOVERY and self.config is not None and self.failure_code in RETRYABLE:
            await asyncio.sleep(RECOVERY_RETRY_SEC)
            if self.mode != RECOVERY or self.failure_code not in RETRYABLE:
                continue
            cfg = self.config

            async def reconnect(job: Job, cfg: db_config.Config = cfg) -> dict[str, Any]:
                # The job is reserved before anything waits, so a recovery save cannot run
                # meanwhile; one that finished before it makes this attempt stale.
                if self.mode != RECOVERY or self.config is not cfg:
                    return {"mode": self.mode, "skipped": True}
                try:
                    db = await asyncio.wait_for(db_config.open_config(cfg), db_config.CONNECT_TIMEOUT_SEC + 4)
                except Exception as e:
                    code = "sqlite_missing" if isinstance(e, FileNotFoundError) else db_config.classify(e)
                    self.failure_code, self.failure_text = code, message(code)
                    return {"mode": self.mode}
                try:
                    code = await _check_identity(db, cfg.installation_id)
                    if code == "installation_pending":
                        code = await self._pending(db, cfg)
                except asyncio.CancelledError:
                    await self._close_handles(db, None, False)
                    raise
                except Exception:
                    code = "attach_failed"
                if code is not None:
                    await self._close_handles(db, None, False)
                    self.failure_code, self.failure_text = code, message(code)
                    return {"mode": self.mode}
                try:
                    await self.switch(app, job, new_db=db, new_config=cfg, publish=self.unpublished)
                except BaseException as e:
                    # As db_operations._switch: only while it is not attached or closed already.
                    if self.db is not db and not (isinstance(e, OperationError) and e.code == "attach_failed"):
                        await self._close_handles(db, None, False)
                    raise
                return {"mode": self.mode}

            try:
                self.jobs.start("reconnect", reconnect)
            except Busy:
                continue

    async def _pending(self, db: Any, cfg: db_config.Config) -> str | None:
        """The code for a database whose installation row is not active, or None once a
        move whose commit point passed is completed (the row is then active)."""
        row = await installation.read(db)
        if db.is_sqlite and _moved(row):
            # The fenced file names the move: kept, so every later configuration records it
            # (move_record), also when no file was written for PostgreSQL yet.
            self.known_migration_id, self.known_moved_from = row.migration_id, cfg.sqlite_path
            return "database_moved"
        if cfg.backend == db_config.POSTGRES and cfg.migration_id and row is not None \
                and row.migration_id == cfg.migration_id:
            return await self._complete_move(db, cfg)
        return "installation_pending"

    async def _complete_move(self, db: Any, cfg: db_config.Config) -> str | None:
        """Finish a move from SQLite after its commit point: fence the old file (again; it is
        idempotent, and a missing file counts as fenced), then activate the PostgreSQL row.
        None when done, else a failure code."""
        from app import db_move

        assert cfg.migration_id is not None and cfg.moved_from_sqlite is not None
        try:
            result = await asyncio.to_thread(
                db_move.fence_file, cfg.moved_from_sqlite, cfg.installation_id, cfg.migration_id
            )
        except db_move.SourceMismatch:
            return "move_source_mismatch"
        except Exception as e:
            print(f"Locking the old SQLite file failed ({type(e).__name__})")
            return "move_incomplete"
        if result == "missing":
            print(f"The SQLite file the move came from ({cfg.moved_from_sqlite}) is missing; nothing to lock")
        await installation.activate(db, cfg.installation_id, cfg.migration_id)
        return None

    def move_record(self) -> tuple[str | None, str | None]:
        """(migration id, SQLite path) of the move from SQLite this installation made, from
        the configuration in use or, when the saved file could be read only in part, from
        that file; (None, None) when there was none."""
        cfg = self.config
        if cfg is not None and cfg.migration_id:
            return cfg.migration_id, cfg.moved_from_sqlite
        return self.known_migration_id, self.known_moved_from

    # ── Switching ──

    async def switch(
        self,
        app: Any,
        job: Job,
        *,
        new_db: Any,
        new_config: db_config.Config,
        verify: Callable[[], Awaitable[None]] | None = None,
        publish: bool = True,
        run_schema: bool = True,
        after: Callable[[], Awaitable[None]] | None = None,
        fence: Callable[[], Awaitable[None]] | None = None,
        before_attach: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Switch the service to `new_db` (opened and checked by the caller, which closes it
        when this raises before the commit point).  Runs inside a job, which holds
        Registry.exclusive for the whole operation.

        The move from SQLite (app.db_move) adds two steps.  `fence` runs after `verify`,
        inside the closed gate, and is the commit point: it is never cancelled and is
        awaited until it returns, also past the deadline.  NotFenced means it certainly did
        not happen (the gate reopens on the old database); any other error leaves its
        outcome open, so the service goes to recovery mode, as after the commit point.
        `before_attach` runs after data/database.json is written, before the new database
        is attached (the first write to it)."""
        loop = asyncio.get_running_loop()
        old = (self.db, self.settings_db, self.close_settings)
        had_db = self.db is not None
        if had_db:
            job.step = "stopping background work"
            try:
                await self._stop_background()
            except OperationError:
                self._resume_background(app)
                raise
        try:
            job.step = "waiting for requests to finish"
            # One deadline from the moment the gate closes.
            closed_at = loop.time()
            deadline = closed_at + SWITCH_BUDGET_SEC
            if not await self.gate.close_and_drain(DRAIN_BUDGET_SEC):
                raise OperationError(
                    "busy",
                    f"Requests did not finish within {int(DRAIN_BUDGET_SEC)} seconds, so nothing "
                    "was changed. Try again.",
                )
            try:
                if verify is not None:
                    job.step = "checking"
                    await asyncio.wait_for(verify(), max(0.1, deadline - loop.time()))
            except BaseException:
                self.gate.reopen()
                raise
        except BaseException:
            if had_db:
                self._resume_background(app)
            raise

        cancelled = False
        if fence is not None:
            job.step = "locking the old database file"
            fencing = asyncio.ensure_future(fence())
            while not fencing.done():
                try:
                    await asyncio.shield(fencing)
                except asyncio.CancelledError:
                    cancelled = True
                except BaseException:
                    break
            error = None if fencing.cancelled() else fencing.exception()
            if isinstance(error, NotFenced):
                self.gate.reopen()
                if had_db:
                    self._resume_background(app)
                raise error
            if error is not None or fencing.cancelled():
                print(f"Locking the old database file failed ({type(error).__name__}); its outcome is not known")
                self.unpublished = True
                await self._recover_after_commit(app, new_db, new_config, old, had_db, "move_incomplete", None)
                raise OperationError("move_incomplete")

        # From the commit point on, a cancellation (shutdown) must not stop the switch half
        # way: the rest runs as its own task, and this one waits for it, however often it is
        # cancelled, so the job keeps Registry.exclusive until the outcome is final.
        finish = asyncio.ensure_future(
            self._commit(app, job, new_db, new_config, old, had_db, publish, run_schema, deadline, after,
                         fenced=fence is not None, before_attach=before_attach, closed_at=closed_at)
        )
        while not finish.done():
            try:
                await asyncio.shield(finish)
            except asyncio.CancelledError:
                cancelled = True
            except BaseException:
                break
        if cancelled:
            raise asyncio.CancelledError
        finish.result()

    async def _commit(
        self, app: Any, job: Job, new_db: Any, new_config: db_config.Config, old: tuple[Any, Any, bool],
        had_db: bool, publish: bool, run_schema: bool, deadline: float,
        after: Callable[[], Awaitable[None]] | None = None,
        fenced: bool = False, before_attach: Callable[[], Awaitable[None]] | None = None,
        closed_at: float | None = None,
    ) -> None:
        """Publish data/database.json (the commit point), then attach the new database, all
        within the switch deadline.  The gate is closed on entry and open on every exit."""
        loop = asyncio.get_running_loop()
        # The retry of a move whose file was not written yet is past the commit point too.
        fenced = fenced or self.unpublished
        if publish:
            job.step = "saving the configuration"
            # Off the event loop, so a slow disk does not stop other requests' timers.
            writing = asyncio.ensure_future(asyncio.to_thread(db_config.publish_config, new_config))
            done, _ = await asyncio.wait({writing}, timeout=max(0.5, deadline - loop.time()))
            error = writing.exception() if done else None
            if not done or isinstance(error, db_config.PublishUncertain):
                # The file may already name the new database, but the disk has not confirmed
                # it (or the write has not ended): a power failure could bring the old file
                # back, so no write may be acknowledged on either database.
                if fenced:
                    # After a move's commit point the retry loop writes the file again and
                    # finishes the switch once the disk answers.
                    self.unpublished = True
                    await self._recover_after_commit(app, new_db, new_config, old, had_db, "move_incomplete", None)
                else:
                    await self._recover_after_commit(
                        app, new_db, new_config, old, had_db, "config_not_durable",
                        "The new connection was written to data/database.json, but the disk did not "
                        "confirm it in time. Check the disk, then save the connection again on this page.",
                    )
                if not done:
                    # Nothing else may write the file before this write has ended.
                    await asyncio.wait({writing})
                raise OperationError(
                    "attach_failed",
                    "The new connection was written, but the disk did not confirm it. The service is "
                    "in recovery mode; open /recovery to save it again.",
                )
            if error is not None and fenced:
                # The old SQLite file is fenced already: no way back to it.  The retry loop
                # writes the file again (unpublished) and finishes the switch.
                print(f"Writing data/database.json failed after the move's commit point ({type(error).__name__})")
                self.unpublished = True
                await self._recover_after_commit(app, new_db, new_config, old, had_db, "move_incomplete", None)
                raise OperationError("move_incomplete")
            if error is not None:
                # Failed before the replace: the old file and the old database stay in use.
                self.gate.reopen()
                if had_db:
                    self._resume_background(app)
                raise error
        if publish:
            self.unpublished = False
        job.step = "switching"

        async def bring_up() -> None:
            if before_attach is not None:
                await before_attach()
            await self.attach(app, new_db, run_schema=run_schema, switching=True)

        try:
            await asyncio.wait_for(bring_up(), max(ATTACH_FLOOR_SEC, deadline - loop.time()))
        except Exception as e:
            print(f"Switching to the new database failed after it was saved ({type(e).__name__})")
            await self._recover_after_commit(app, new_db, new_config, old, had_db, "attach_failed", None)
            raise OperationError(
                "attach_failed",
                "The new connection was saved, but the service could not start on it. "
                "The service is in recovery mode; open /recovery to fix the connection.",
            ) from None
        self.config, self.source = new_config, "file"
        self.known_installation_id = new_config.installation_id
        self.gate.reopen()
        if closed_at is not None:
            self.last_pause_sec = loop.time() - closed_at
        if had_db:
            await self._close_handles(*old)
        if after is not None:
            # Part of the switch that happened (the activity log entry): runs even when the
            # job was cancelled meanwhile.
            await after()

    async def _recover_after_commit(
        self, app: Any, new_db: Any, new_config: db_config.Config, old: tuple[Any, Any, bool],
        had_db: bool, code: str, text: str | None,
    ) -> None:
        """After the commit point the old database must not be served again: recovery mode
        in this process (waiting requests get 503), both connections closed."""
        self.config = new_config
        self.known_installation_id = new_config.installation_id
        # Recovery mode and the open gate first: waiting requests get 503 now, whatever
        # closing the connections takes.
        await self._enter_recovery(app, code, text)
        self.gate.reopen()
        await self._close_handles(new_db, None, False)
        if had_db:
            await self._close_handles(*old)


def _build_redis(app: Any, connection: redis_settings.Connection) -> None:
    app.state.redis = redis_asyncio.from_url(connection.url, decode_responses=True)
    app.state.login_redis = redis_asyncio.from_url(
        connection.url, decode_responses=True, **login_limit.COUNTER_CLIENT_OPTIONS
    )
    redis_settings.state.connection = connection


def _moved(row: Any) -> bool:
    """A SQLite installation row that a move to PostgreSQL fenced (app.db_move.fence_file)."""
    return row is not None and row.status == installation.PENDING and bool(row.migration_id)


async def _refuse_unfinished(db: Any) -> None:
    """DATABASE_URL start-up: never serve a database whose installation row is not active,
    before anything in it is changed."""
    if not await installation.table_exists(db):
        return
    row = await installation.read(db)
    if row is None or row.status == installation.ACTIVE:
        return
    if db.is_sqlite and _moved(row):
        text = ("DATABASE_URL names a SQLite database that was moved to PostgreSQL. Remove DATABASE_URL "
                "or point it to the PostgreSQL database.")
    else:
        text = ("DATABASE_URL names a database that holds an unfinished copy from a move to PostgreSQL. "
                "Remove DATABASE_URL so the service uses data/database.json, or point it to the database in use.")
    raise RuntimeError(text)


async def _check_identity(db: Any, installation_id: str) -> str | None:
    """None when `db` holds the active installation `installation_id`, else a failure code."""
    state = await db_inspect.inspect(db)
    if state.kind == db_inspect.INSTALLATION:
        row = state.installation
        if row.id != installation_id:
            return "other_installation"
        if row.status != installation.ACTIVE:
            return "installation_pending"
        return None
    return {
        db_inspect.EMPTY: "database_empty",
        db_inspect.OLDER: "older_installation",
        db_inspect.INCOMPLETE: "incomplete_installation",
    }.get(state.kind, "foreign_database")


def _installation_id_in_file() -> str | None:
    """The installationId of a file that cannot be used as a whole (for example, its
    password cannot be decrypted), so recovery can still match the right database."""
    import json

    try:
        data = json.loads(db_config.config_path().read_text(encoding="utf-8"))
        iid = data.get("installationId")
        return iid if isinstance(iid, str) and iid else None
    except Exception:
        return None


def _move_record_in_file() -> tuple[str | None, str | None]:
    """migrationId and movedFromSqlite of a file that cannot be used as a whole."""
    import json

    try:
        data = json.loads(db_config.config_path().read_text(encoding="utf-8"))
        mid, moved = data.get("migrationId"), data.get("movedFromSqlite")
        if isinstance(mid, str) and mid:
            return mid, moved if isinstance(moved, str) and moved else None
    except Exception:
        pass
    return None, None


def _remove_setup_leftovers() -> None:
    """Temporary files of a setup that was interrupted before it published data/auth.db."""
    for f in db_config.data_dir().glob(f"{db_config.SQLITE_NAME}.setup-*"):
        try:
            f.unlink()
        except OSError:
            pass

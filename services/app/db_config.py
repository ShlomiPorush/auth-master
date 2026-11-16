"""Which database the service uses: data/database.json, and opening its connection.

The database is chosen in first-time setup, or changed on the Settings page or the
recovery page; the choice is saved in data/database.json.  DATABASE_URL in the environment
overrides it (the Settings page then shows the connection locked).

The file (format 1), mode 0600:
    {"format": 1, "backend": "sqlite" | "postgres",
     "sqlite": {"path": "data/auth.db"},                      # backend sqlite
     "postgres": {"host", "port", "database", "user",         # backend postgres
                  "passwordEnc", "sslmode", "caPem"},
     "installationId": str, "updatedAt": str,
     "migrationId": str, "movedFromSqlite": str}            # after a move from SQLite
migrationId names the move (app.db_move) that filled the PostgreSQL database, and
movedFromSqlite the SQLite file it was moved from; versions before the move ignore both.
The password is encrypted with app.crypto_tokens (AES-GCM under SESSION_SECRET and
APP_ENCRYPTION_KEY), so changing either key makes the saved connection unreadable, as it
does for the other saved secrets; the recovery page then takes the password again.

The file is written whole and replaced atomically (publish): a crash leaves the old file
or the new one, never a mix.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import ssl
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import _is_public
from app.crypto_tokens import decrypt_token_value, encrypt_token_value

FORMAT = 1
CONFIG_NAME = "database.json"
SQLITE_NAME = "auth.db"
SQLITE = "sqlite"
POSTGRES = "postgres"
SSLMODES = ("prefer", "disable", "require", "verify-full")
DEFAULT_SSLMODE = "prefer"
CONNECT_TIMEOUT_SEC = 8
COMMAND_TIMEOUT_SEC = 30
MAX_HOST = 255
MAX_NAME = 63
MAX_PASSWORD = 1024
MAX_CA_PEM = 32 * 1024

# Overridden by tests; relative to the working directory, like the default SQLite path
# of earlier versions (sqlite:///data/auth.db).
DATA_DIR = Path("data")


def data_dir() -> Path:
    return DATA_DIR


def config_path() -> Path:
    return data_dir() / CONFIG_NAME


def default_sqlite_path() -> Path:
    return data_dir() / SQLITE_NAME


class ConfigError(Exception):
    """The saved file cannot be used.  `code` is one of the fixed error codes."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PgParams:
    host: str
    port: int
    database: str
    user: str
    password: str  # in memory only; the file holds it encrypted
    sslmode: str = DEFAULT_SSLMODE
    ca_pem: str | None = None

    def public(self) -> dict[str, Any]:
        """What views, logs and the activity log may show: never the password."""
        return {
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "user": self.user,
            "sslmode": self.sslmode,
            "caPem": self.ca_pem is not None,
        }


@dataclass(frozen=True)
class Config:
    backend: str
    installation_id: str
    sqlite_path: str | None = None
    pg: PgParams | None = None
    # Set by a move from SQLite (app.db_move): the move's id and the old SQLite file.
    migration_id: str | None = None
    moved_from_sqlite: str | None = None


# ── Validation of connection fields ─────────────────────────────

def params_problem(p: PgParams) -> tuple[str, str] | None:
    """(field, message) for the first field that cannot be used, or None."""
    if not p.host or len(p.host) > MAX_HOST or any(ch.isspace() or ord(ch) < 32 for ch in p.host) or "/" in p.host:
        return "host", "Enter the server's host name or IP address."
    if not 1 <= p.port <= 65535:
        return "port", "Enter a port from 1 to 65535."
    for field, value in (("database", p.database), ("user", p.user)):
        if not value or len(value.encode("utf-8")) > MAX_NAME or any(ord(ch) < 32 for ch in value):
            return field, f"Enter a name of 1 to {MAX_NAME} bytes."
    if not p.password:
        return "password", "Enter the password of the database user."
    if len(p.password) > MAX_PASSWORD or "\x00" in p.password:
        return "password", f"Use a password of at most {MAX_PASSWORD} characters."
    if _is_public(p.password):
        return "password", (
            "This password is publicly known. Change the database user's password on the "
            "PostgreSQL server (ALTER ROLE ... PASSWORD ...), then enter the new one."
        )
    if p.sslmode not in SSLMODES:
        return "sslmode", "Choose one of the TLS options."
    if p.ca_pem is not None:
        if len(p.ca_pem) > MAX_CA_PEM:
            return "caPem", f"Use a CA certificate file of at most {MAX_CA_PEM // 1024} KB."
        if p.sslmode != "verify-full":
            return "caPem", "A CA certificate is used only with verify-full."
        try:
            ssl.create_default_context().load_verify_locations(cadata=p.ca_pem)
        except (ssl.SSLError, ValueError):
            return "caPem", "This is not a PEM CA certificate."
    return None


def ssl_argument(p: PgParams) -> Any:
    """asyncpg's documented `ssl` argument: an sslmode name, or an SSLContext that checks
    the server certificate and host name (verify-full) against the system trust store and
    the optional CA."""
    if p.sslmode != "verify-full":
        return p.sslmode
    context = ssl.create_default_context()
    if p.ca_pem:
        context.load_verify_locations(cadata=p.ca_pem)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


# ── The file ────────────────────────────────────────────────────

def _dump(cfg: Config) -> str:
    data: dict[str, Any] = {
        "format": FORMAT,
        "backend": cfg.backend,
        "installationId": cfg.installation_id,
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if cfg.backend == SQLITE:
        data["sqlite"] = {"path": cfg.sqlite_path}
    else:
        p = cfg.pg
        assert p is not None
        data["postgres"] = {
            "host": p.host,
            "port": p.port,
            "database": p.database,
            "user": p.user,
            "passwordEnc": encrypt_token_value(p.password),
            "sslmode": p.sslmode,
            "caPem": p.ca_pem,
        }
    if cfg.migration_id is not None:
        data["migrationId"] = cfg.migration_id
        data["movedFromSqlite"] = cfg.moved_from_sqlite
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


_INVALID = "data/database.json is not a valid database configuration of this version"


def _parse(text: str) -> Config:
    try:
        data = json.loads(text)
    except ValueError:
        raise ConfigError("config_invalid", f"{_INVALID} (not JSON).") from None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ConfigError("config_invalid", f"{_INVALID} (format).")
    iid = data.get("installationId")
    if not isinstance(iid, str) or not iid:
        raise ConfigError("config_invalid", f"{_INVALID} (installationId).")
    backend = data.get("backend")
    if backend == SQLITE:
        s = data.get("sqlite")
        path = s.get("path") if isinstance(s, dict) else None
        if not isinstance(path, str) or not path:
            raise ConfigError("config_invalid", f"{_INVALID} (sqlite.path).")
        return Config(SQLITE, iid, sqlite_path=path)
    if backend != POSTGRES:
        raise ConfigError("config_invalid", f"{_INVALID} (backend).")
    pg = data.get("postgres")
    if not isinstance(pg, dict):
        raise ConfigError("config_invalid", f"{_INVALID} (postgres).")
    try:
        host, port, database, user = pg["host"], pg["port"], pg["database"], pg["user"]
        enc, sslmode, ca = pg["passwordEnc"], pg.get("sslmode", DEFAULT_SSLMODE), pg.get("caPem")
        if not (isinstance(host, str) and type(port) is int and isinstance(database, str)
                and isinstance(user, str) and isinstance(enc, str) and sslmode in SSLMODES
                and (ca is None or isinstance(ca, str))):
            raise TypeError
    except (KeyError, TypeError):
        raise ConfigError("config_invalid", f"{_INVALID} (postgres fields).") from None
    try:
        password = decrypt_token_value(enc)
    except Exception:
        raise ConfigError(
            "config_unreadable",
            "The saved database password cannot be read: SESSION_SECRET or APP_ENCRYPTION_KEY "
            "changed since it was saved. Enter the connection again on the recovery page.",
        ) from None
    migration_id, moved_from = data.get("migrationId"), data.get("movedFromSqlite")
    if migration_id is not None and not (isinstance(migration_id, str) and migration_id
                                         and isinstance(moved_from, str) and moved_from):
        raise ConfigError("config_invalid", f"{_INVALID} (migrationId).")
    return Config(POSTGRES, iid, pg=PgParams(host, port, database, user, password, sslmode, ca),
                  migration_id=migration_id, moved_from_sqlite=moved_from if migration_id else None)


def read_config() -> Config | None:
    """The saved configuration, None when there is no file, ConfigError when it cannot be used."""
    try:
        text = config_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError("config_invalid", f"data/database.json cannot be read ({type(e).__name__}).") from None
    return _parse(text)



def _fsync_dir(path: Path) -> None:
    if os.name != "posix":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish_config(cfg: Config) -> None:
    """Write the file atomically with mode 0600: a new file in the same directory, flushed
    to disk, renamed over the old one, then the directory flushed."""
    directory = data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    text = _dump(cfg)
    fd, tmp = tempfile.mkstemp(prefix=".database.json.", dir=directory)
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, config_path())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # The new file is in place: a failure from here on does not undo that.
    try:
        _fsync_dir(directory)
    except OSError as e:
        raise PublishUncertain() from e


def confirm_durable() -> None:
    """Flush data/database.json and its directory to disk; OSError when the disk refuses."""
    if os.name != "posix":
        return
    fd = os.open(config_path(), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_dir(data_dir())


class PublishUncertain(Exception):
    """data/database.json was replaced, but its directory could not be flushed to disk: the
    new file is the configuration now, though a power failure could still revert it."""


# ── Connections ─────────────────────────────────────────────────

def classify(error: BaseException) -> str:
    """A fixed code for a connection error.  Only the class is used: driver messages can
    carry connection details."""
    import asyncpg

    if isinstance(error, (asyncpg.InvalidPasswordError, asyncpg.InvalidAuthorizationSpecificationError)):
        return "auth_failed"
    if isinstance(error, asyncpg.InvalidCatalogNameError):
        return "database_missing"
    if isinstance(error, asyncpg.InsufficientPrivilegeError):
        return "permission_denied"
    if isinstance(error, getattr(asyncpg.exceptions, "TargetServerAttributeNotMatched", ())):
        return "read_only"
    if isinstance(error, asyncpg.ReadOnlySQLTransactionError):
        return "read_only"
    if isinstance(error, (ssl.SSLError, ssl.CertificateError)):
        return "tls_failed"
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    if isinstance(error, ConnectionError) and "ssl" in str(error).lower():
        # asyncpg: the server rejected the TLS upgrade (sslmode require or verify-full).
        return "tls_failed"
    if isinstance(error, (socket.gaierror, OSError)):
        return "unreachable"
    if isinstance(error, asyncpg.PostgresError):
        return "other"
    return "other"


async def open_postgres(p: PgParams, *, min_size: int = 1, max_size: int = 10) -> Any:
    """A PgDatabase for `p`.  Raises the driver's error (classify it).

    target_session_attrs="read-write" (asyncpg's documented option) refuses a server in
    recovery (a standby) or one that is read-only.  Every connection detail is passed
    explicitly, so asyncpg's environment and password-file fallbacks are not used.
    """
    import asyncpg

    from app.db import PgDatabase

    pool = await asyncpg.create_pool(
        host=p.host,
        port=p.port,
        user=p.user,
        password=p.password,
        database=p.database,
        ssl=ssl_argument(p),
        timeout=CONNECT_TIMEOUT_SEC,
        command_timeout=COMMAND_TIMEOUT_SEC,
        target_session_attrs="read-write",
        min_size=min_size,
        max_size=max_size,
    )
    return PgDatabase(pool)


async def connect_postgres(p: PgParams) -> Any:
    """One asyncpg connection (not a pool) with the options of open_postgres, for work that
    must stay on one connection, such as the move's single transaction (app.db_move)."""
    import asyncpg

    return await asyncpg.connect(
        host=p.host,
        port=p.port,
        user=p.user,
        password=p.password,
        database=p.database,
        ssl=ssl_argument(p),
        timeout=CONNECT_TIMEOUT_SEC,
        command_timeout=COMMAND_TIMEOUT_SEC,
        target_session_attrs="read-write",
    )


async def open_sqlite(path: str) -> Any:
    from app.db import create_database

    return await create_database(f"sqlite:///{path}")


async def open_config(cfg: Config) -> Any:
    if cfg.backend == SQLITE:
        assert cfg.sqlite_path is not None
        if not Path(cfg.sqlite_path).is_file():
            raise FileNotFoundError(cfg.sqlite_path)
        return await open_sqlite(cfg.sqlite_path)
    assert cfg.pg is not None
    return await open_postgres(cfg.pg)

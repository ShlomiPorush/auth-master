"""Idempotent DDL on app startup.  Works on both PostgreSQL and SQLite."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from app.db import UniqueViolationError

if TYPE_CHECKING:
    from app.db import Database

# At most one admin completes MFA enrollment: bootstrap creates the only admin, and this
# index makes concurrent or stale enrollments fail instead of creating a second one.
_ONE_ENABLED_ADMIN_MSG = (
    "The admin_users table has more than one admin with MFA enabled, which this version "
    "does not allow (index admin_users_one_enabled). Delete the admin rows that should not "
    "exist, together with any API keys they created, then start the service again."
)


async def pg_index_exists(db: "Database", name: str) -> bool:
    return bool(await db.fetchval(
        "SELECT 1 FROM pg_indexes WHERE schemaname = current_schema() AND indexname = $1", name
    ))


async def _pg_index(db: "Database", name: str, definition: str) -> None:
    if not await pg_index_exists(db, name):
        await db.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {definition}")


async def _pg_add_column(db: "Database", table: str, column: str, definition: str) -> None:
    exists = await db.fetchval(
        "SELECT 1 FROM information_schema.columns"
        " WHERE table_schema = current_schema() AND table_name = $1 AND column_name = $2",
        table, column,
    )
    if not exists:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}")


async def _ensure_one_enabled_admin_index(db: "Database", where: str) -> None:
    if db.is_pg and await pg_index_exists(db, "admin_users_one_enabled"):
        return
    try:
        await db.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS admin_users_one_enabled ON admin_users (totp_enabled) WHERE {where}"
        )
    except UniqueViolationError as e:
        raise RuntimeError(_ONE_ENABLED_ADMIN_MSG) from e


async def _sqlite_add_column_if_not_exists(db: "Database", table: str, column: str, col_type: str, default: str) -> None:
    """SQLite does not support ADD COLUMN IF NOT EXISTS before 3.35, so check pragma."""
    rows = await db.fetch(f"PRAGMA table_info({table})")
    names = {r["name"] for r in rows}
    if column not in names:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type} NOT NULL DEFAULT {default}")


# Every table this service creates, except `installation` (app.installation), which marks a
# database as this service's and is checked before anything else is created.  A database
# that holds only these tables, without `installation`, is an installation of an older
# version or a backup of one (app.db_inspect).  test_database_setup.py checks this list
# against the tables ensure_database_schema creates.
SERVICE_TABLES = (
    "admin_users",
    "tokens",
    "zones",
    "api_keys",
    "activity_logs",
    "access_logs",
    "access_log_rollup",
    "access_log_rollup_state",
    "rate_limit_settings",
    "app_settings",
    "redis_settings",
    "app_state",
    "passkeys",
    "passkey_settings",
)
INSTALLATION_TABLE = "installation"


async def ensure_database_schema(db: "Database", *, zone_fold_index: bool = True) -> None:
    """`zone_fold_index` False: the case-fold index on zone names is left to the caller (the
    move from SQLite creates it after copying the zones, app.db_move)."""
    from app.installation import ensure_table
    from app.zones import ensure_zone_name_index

    if db.is_pg:
        await _ensure_pg(db)
    else:
        await _ensure_sqlite(db)
    await ensure_table(db)
    if zone_fold_index:
        await ensure_zone_name_index(db)


async def _ensure_pg(db: "Database") -> None:
    await db.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    await db.execute("""
        CREATE TABLE IF NOT EXISTS admin_users (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            totp_secret_enc TEXT NULL,
            totp_enabled BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    await _ensure_one_enabled_admin_index(db, "totp_enabled")

    await db.execute("""
        CREATE TABLE IF NOT EXISTS tokens (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            token_enc TEXT NULL,
            grants JSONB NOT NULL DEFAULT '[]'::jsonb,
            expires_at TIMESTAMPTZ NULL,
            is_active BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_used_at TIMESTAMPTZ NULL
        )
    """)
    await _pg_index(db, "idx_tokens_token_hash", "tokens (token_hash)")
    await _pg_add_column(db, "tokens", "token_enc", "TEXT NULL")
    await _pg_add_column(db, "tokens", "description", "TEXT NOT NULL DEFAULT ''")

    await db.execute("""
        CREATE TABLE IF NOT EXISTS zones (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    await _pg_index(db, "idx_zones_name", "zones (name)")
    await _pg_add_column(db, "zones", "description", "TEXT NOT NULL DEFAULT ''")

    await db.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name TEXT NOT NULL,
            key_hash TEXT NOT NULL UNIQUE,
            key_enc TEXT NULL,
            scopes TEXT NOT NULL DEFAULT 'validate,tokens:read,zones:read',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    await _pg_index(db, "idx_api_keys_key_hash", "api_keys (key_hash)")
    await _pg_add_column(db, "api_keys", "key_enc", "TEXT NULL")

    await db.execute("""
        CREATE TABLE IF NOT EXISTS activity_logs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            entity_type TEXT NULL,
            entity_id TEXT NULL,
            entity_name TEXT NULL,
            details TEXT NULL,
            ip_address TEXT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    await _pg_index(db, "idx_activity_logs_created_at", "activity_logs (created_at DESC)")

    await db.execute("""
        CREATE TABLE IF NOT EXISTS access_logs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            token_id TEXT NULL,
            token_name TEXT NULL,
            area TEXT NOT NULL,
            level TEXT NOT NULL,
            result BOOLEAN NOT NULL,
            ip_address TEXT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    await _pg_index(db, "idx_access_logs_created_at", "access_logs (created_at DESC)")
    await _pg_index(db, "idx_access_logs_token_id", "access_logs (token_id)")
    # The Usage page's summary of access_logs (app.usage_rollup). rolled marks the rows it
    # counts; the partial index finds the rows it does not count yet.
    await _pg_add_column(db, "access_logs", "rolled", "BOOLEAN NOT NULL DEFAULT false")
    await _pg_index(db, "idx_access_logs_unrolled", "access_logs (created_at) WHERE NOT rolled")
    await db.execute("""
        CREATE TABLE IF NOT EXISTS access_log_rollup (
            slot TIMESTAMPTZ NOT NULL,
            token_id TEXT NULL,
            area TEXT NOT NULL,
            level TEXT NOT NULL,
            result BOOLEAN NOT NULL,
            ip_address TEXT NULL,
            calls INTEGER NOT NULL,
            last_at TIMESTAMPTZ NOT NULL,
            token_name TEXT NULL,
            name_at TIMESTAMPTZ NULL,
            name_id UUID NULL
        )
    """)
    await _pg_index(db, "idx_access_log_rollup_slot", "access_log_rollup (slot)")
    await db.execute("""
        CREATE TABLE IF NOT EXISTS access_log_rollup_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            valid_from TIMESTAMPTZ NULL
        )
    """)
    await db.execute("INSERT INTO access_log_rollup_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING")

    # Rate limit settings from the admin Settings page: one row per saved version, never
    # updated or deleted, so it is also the history of who changed what (app.rate_limit_settings).
    # The same shape for the general settings (app.general_settings) and the Redis
    # connection (app.redis_settings).  Each is its own table, so an older image that
    # does not know them still starts.
    for table in ("rate_limit_settings", "app_settings", "redis_settings"):
        await db.execute(f"""
            CREATE TABLE IF NOT EXISTS {table} (
                version INTEGER PRIMARY KEY CHECK (version > 0),
                value TEXT NOT NULL,
                actor TEXT NOT NULL,
                ip_address TEXT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)

    # Internal state: markers of one-time steps, the access log purge boundary, and one
    # setting, the date and time formats of the admin pages (key display_settings,
    # app.display_settings), kept here so that the schema does not change for it.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS app_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    # Passkeys (app.passkeys): one row per WebAuthn credential of an admin.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS passkeys (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            admin_id UUID NOT NULL REFERENCES admin_users (id) ON DELETE CASCADE,
            credential_id TEXT NOT NULL UNIQUE,
            public_key TEXT NOT NULL,
            sign_count BIGINT NOT NULL DEFAULT 0,
            transports TEXT NOT NULL DEFAULT '[]',
            backed_up BOOLEAN NOT NULL DEFAULT false,
            rp_id TEXT NOT NULL,
            name TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_used_at TIMESTAMPTZ NULL
        )
    """)
    await _pg_index(db, "idx_passkeys_admin_id", "passkeys (admin_id)")
    # The passkey address from the Settings page: one row per saved version, never updated
    # or deleted (app.passkey_settings).  A NULL origin is a version that removed the address.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS passkey_settings (
            version INTEGER PRIMARY KEY CHECK (version > 0),
            origin TEXT NULL,
            actor TEXT NOT NULL,
            ip_address TEXT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)


async def _ensure_sqlite(db: "Database") -> None:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS admin_users (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            totp_secret_enc TEXT NULL,
            totp_enabled INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    await _ensure_one_enabled_admin_index(db, "totp_enabled = 1")

    await db.execute("""
        CREATE TABLE IF NOT EXISTS tokens (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            token_enc TEXT NULL,
            grants TEXT NOT NULL DEFAULT '[]',
            expires_at TEXT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            last_used_at TEXT NULL
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_tokens_token_hash ON tokens (token_hash)")
    await _sqlite_add_column_if_not_exists(db, "tokens", "token_enc", "TEXT", "NULL")
    await _sqlite_add_column_if_not_exists(db, "tokens", "description", "TEXT", "''")

    await db.execute("""
        CREATE TABLE IF NOT EXISTS zones (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_zones_name ON zones (name)")
    await _sqlite_add_column_if_not_exists(db, "zones", "description", "TEXT", "''")

    await db.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            key_hash TEXT NOT NULL UNIQUE,
            key_enc TEXT NULL,
            scopes TEXT NOT NULL DEFAULT 'validate,tokens:read,zones:read',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_key_hash ON api_keys (key_hash)")
    # key_enc is nullable – can't use _sqlite_add_column_if_not_exists (adds NOT NULL)
    rows = await db.fetch("PRAGMA table_info(api_keys)")
    if "key_enc" not in {r["name"] for r in rows}:
        await db.execute("ALTER TABLE api_keys ADD COLUMN key_enc TEXT")

    await db.execute("""
        CREATE TABLE IF NOT EXISTS activity_logs (
            id TEXT PRIMARY KEY,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            entity_type TEXT NULL,
            entity_id TEXT NULL,
            entity_name TEXT NULL,
            details TEXT NULL,
            ip_address TEXT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_activity_logs_created_at ON activity_logs (created_at)")

    await db.execute("""
        CREATE TABLE IF NOT EXISTS access_logs (
            id TEXT PRIMARY KEY,
            token_id TEXT NULL,
            token_name TEXT NULL,
            area TEXT NOT NULL,
            level TEXT NOT NULL,
            result INTEGER NOT NULL,
            ip_address TEXT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_access_logs_created_at ON access_logs (created_at)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_access_logs_token_id ON access_logs (token_id)")
    # See _ensure_pg.
    await _sqlite_add_column_if_not_exists(db, "access_logs", "rolled", "INTEGER", "0")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_access_logs_unrolled ON access_logs (created_at) WHERE rolled = 0")
    await db.execute("""
        CREATE TABLE IF NOT EXISTS access_log_rollup (
            slot TEXT NOT NULL,
            token_id TEXT NULL,
            area TEXT NOT NULL,
            level TEXT NOT NULL,
            result INTEGER NOT NULL,
            ip_address TEXT NULL,
            calls INTEGER NOT NULL,
            last_at TEXT NOT NULL,
            token_name TEXT NULL,
            name_at TEXT NULL,
            name_id TEXT NULL
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_access_log_rollup_slot ON access_log_rollup (slot)")
    await db.execute("""
        CREATE TABLE IF NOT EXISTS access_log_rollup_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            valid_from TEXT NULL
        )
    """)
    await db.execute("INSERT INTO access_log_rollup_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING")

    # See _ensure_pg.
    for table in ("rate_limit_settings", "app_settings", "redis_settings"):
        await db.execute(f"""
            CREATE TABLE IF NOT EXISTS {table} (
                version INTEGER PRIMARY KEY CHECK (version > 0),
                value TEXT NOT NULL,
                actor TEXT NOT NULL,
                ip_address TEXT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS app_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    # See _ensure_pg.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS passkeys (
            id TEXT PRIMARY KEY,
            admin_id TEXT NOT NULL REFERENCES admin_users (id) ON DELETE CASCADE,
            credential_id TEXT NOT NULL UNIQUE,
            public_key TEXT NOT NULL,
            sign_count INTEGER NOT NULL DEFAULT 0,
            transports TEXT NOT NULL DEFAULT '[]',
            backed_up INTEGER NOT NULL DEFAULT 0,
            rp_id TEXT NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            last_used_at TEXT NULL
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_passkeys_admin_id ON passkeys (admin_id)")
    await db.execute("""
        CREATE TABLE IF NOT EXISTS passkey_settings (
            version INTEGER PRIMARY KEY CHECK (version > 0),
            origin TEXT NULL,
            actor TEXT NOT NULL,
            ip_address TEXT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

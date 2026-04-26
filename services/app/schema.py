"""Idempotent DDL on app startup.  Works on both PostgreSQL and SQLite."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.db import Database


async def _sqlite_add_column_if_not_exists(db: "Database", table: str, column: str, col_type: str, default: str) -> None:
    """SQLite does not support ADD COLUMN IF NOT EXISTS before 3.35, so check pragma."""
    rows = await db.fetch(f"PRAGMA table_info({table})")
    names = {r["name"] for r in rows}
    if column not in names:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type} NOT NULL DEFAULT {default}")


async def ensure_database_schema(db: "Database") -> None:
    if db.is_pg:
        await _ensure_pg(db)
    else:
        await _ensure_sqlite(db)


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
    await db.execute("CREATE INDEX IF NOT EXISTS idx_tokens_token_hash ON tokens (token_hash)")
    await db.execute("ALTER TABLE IF EXISTS tokens ADD COLUMN IF NOT EXISTS token_enc TEXT NULL")
    await db.execute("ALTER TABLE IF EXISTS tokens ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT ''")

    await db.execute("""
        CREATE TABLE IF NOT EXISTS zones (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_zones_name ON zones (name)")
    await db.execute("ALTER TABLE IF EXISTS zones ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT ''")

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
    await db.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_key_hash ON api_keys (key_hash)")
    await db.execute("ALTER TABLE IF EXISTS api_keys ADD COLUMN IF NOT EXISTS key_enc TEXT NULL")


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

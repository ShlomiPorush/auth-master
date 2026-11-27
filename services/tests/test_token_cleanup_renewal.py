from __future__ import annotations

import asyncio
import os
import secrets
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_TMP_DIR = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)

for _name, _value in {
    "ADMIN_API_KEY": secrets.token_urlsafe(32),
    "SESSION_SECRET": secrets.token_urlsafe(48),
    "APP_ENCRYPTION_KEY": secrets.token_hex(16),
    "REDIS_URL": f"redis://:{secrets.token_hex(32)}@127.0.0.1:6379/0",
}.items():
    os.environ.setdefault(_name, _value)

PAST = "2001-01-01 00:00:00"
RENEWED = "2999-01-01 00:00:00"


def test_a_token_renewed_between_the_cleanup_read_and_its_delete_is_kept():
    asyncio.run(_renew_during_cleanup())


async def _renew_during_cleanup() -> None:
    from app import main as main_mod
    from app.db import create_database
    from app.schema import ensure_database_schema

    db = await create_database(f"sqlite:///{_TMP_DIR.name}/{uuid.uuid4().hex}.db")
    try:
        await ensure_database_schema(db)
        renewed_id, expired_id = str(uuid.uuid4()), str(uuid.uuid4())
        for token_id in (renewed_id, expired_id):
            await db.execute(
                "INSERT INTO tokens (id, name, token_hash, grants, expires_at) VALUES ($1, $2, $3, $4, $5)",
                token_id, "cleanup-race", secrets.token_hex(16), "[]", PAST,
            )

        read_tokens = db.fetch

        async def read_then_renew(sql, *args):
            rows = await read_tokens(sql, *args)
            if "FROM tokens" in sql:
                await db.execute("UPDATE tokens SET expires_at = $1 WHERE id = $2", RENEWED, renewed_id)
            return rows

        db.fetch = read_then_renew
        await main_mod.cleanup_expired_tokens(db)
        db.fetch = read_tokens

        left = {r["id"]: r["expires_at"] for r in await db.fetch("SELECT id, expires_at FROM tokens")}
        assert left == {renewed_id: RENEWED}
    finally:
        await db.close()

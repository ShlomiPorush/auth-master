from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Depends, Header, HTTPException, Request

from app.config import get_settings
from app.crypto_tokens import sha256_hex

if TYPE_CHECKING:
    pass

# All valid scopes
ALL_SCOPES = frozenset({"validate", "tokens:read", "tokens:write", "zones:read", "zones:write"})


async def _resolve_api_key(request: Request, authorization: str | None = Header(None)) -> list[str]:
    """Resolve Bearer token → list of scopes.  Checks ENV key first, then DB."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # ENV key = full access
    if token == get_settings().admin_api_key:
        return list(ALL_SCOPES)

    # DB key — lookup by hash
    h = sha256_hex(token)
    db = request.app.state.db
    row = await db.fetchrow("SELECT scopes FROM api_keys WHERE key_hash = $1", h)
    if not row:
        raise HTTPException(status_code=401, detail="Unauthorized")

    scopes_str = row["scopes"] or ""
    return [s.strip() for s in scopes_str.split(",") if s.strip()]


async def require_admin_api_key(
    request: Request, authorization: str | None = Header(None)
) -> None:
    """Verify that a valid API key is present (any scope)."""
    scopes = await _resolve_api_key(request, authorization)
    request.state.api_scopes = scopes


def require_scope(scope: str):
    """Dependency factory: require a specific scope on the resolved API key."""

    async def _check(request: Request, authorization: str | None = Header(None)) -> None:
        scopes = await _resolve_api_key(request, authorization)
        request.state.api_scopes = scopes
        if scope not in scopes:
            raise HTTPException(status_code=403, detail=f"Missing scope: {scope}")

    return Depends(_check)

"""Shared date/time formatting utilities — timezone-aware."""

import os
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo


def _get_tz() -> ZoneInfo:
    """Return the ZoneInfo for the TZ env-var (default: UTC)."""
    tz_name = os.environ.get("TZ", "UTC").strip()
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("UTC")


def fmt_datetime(val: Any) -> str | None:
    """Convert a DB datetime value (str or datetime) to an ISO string in the configured TZ.

    - If *val* is a string (SQLite), it is parsed as UTC and converted to the configured TZ.
    - If *val* is a datetime (Postgres), it is converted (assuming UTC if naive).
    """
    if val is None:
        return None

    tz = _get_tz()

    if isinstance(val, str):
        # SQLite stores as "YYYY-MM-DD HH:MM:SS" (UTC, no tzinfo)
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
            try:
                dt = datetime.strptime(val, fmt).replace(tzinfo=timezone.utc)
                return dt.astimezone(tz).isoformat()
            except ValueError:
                continue
        # If it already has timezone info (e.g., from PG text representation), return as-is
        return val

    if isinstance(val, datetime):
        if val.tzinfo is None:
            val = val.replace(tzinfo=timezone.utc)
        return val.astimezone(tz).isoformat()

    return str(val)


def is_expired(expires_at: Any) -> bool:
    """Check if a datetime value (str or datetime) is in the past (UTC comparison)."""
    if expires_at is None:
        return False

    if isinstance(expires_at, str):
        # Handle SQLite storing strings, replace Z for compatibility
        val = expires_at.replace("Z", "+00:00")
        # Handle possible space instead of T in SQLite datetime representation
        if " " in val and "T" not in val:
            val = val.replace(" ", "T")
        try:
            dt = datetime.fromisoformat(val)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
                try:
                    dt = datetime.strptime(expires_at, fmt).replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue
            else:
                return False
    else:
        dt = expires_at

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() <= datetime.now(timezone.utc).timestamp()

